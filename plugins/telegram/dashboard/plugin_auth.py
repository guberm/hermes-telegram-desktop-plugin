"""Phone, email, QR and 2FA auth routes using the shared Telethon owner."""
from __future__ import annotations

import asyncio
import base64
import contextvars
import importlib.util
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

router = APIRouter()
_LOCK = threading.RLock()
_VOLATILE_STATE: dict[str, dict[str, Any]] = {}
_QR_TASK: dict[str, asyncio.Task[Any]] = {}
_ACTIVE_SESSION_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar("telegram_auth_session_key", default=None)
_AUTH_TIMEOUT_SECONDS = 90.0
_TRANSIENT_AUTH_FIELDS = {"phone", "phoneCodeHash", "qr_token_b64", "qr_expiry", "codeType", "nextType", "resendAt", "emailPurpose", "email", "emailCodeSent", "hint", "hasRecovery", "emailPattern", "recoveryEmailPattern", "recoveryCode", "password", "code"}
_PHONE_RE_OK = r"^\+[1-9][0-9]{4,14}$"
_CODE_RE_OK = r"^[0-9]{4,8}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartPhoneRequest(_Strict):
    phone: Annotated[str, StringConstraints(min_length=5, max_length=20, pattern=_PHONE_RE_OK)]


class SubmitCodeRequest(_Strict):
    code: Annotated[str, StringConstraints(min_length=4, max_length=8, pattern=_CODE_RE_OK)]


class SubmitPasswordRequest(_Strict):
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class EmailRequest(_Strict):
    email: Annotated[str, StringConstraints(min_length=3, max_length=320)]


class EmailCodeRequest(_Strict):
    code: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class _AuthFlowError(RuntimeError):
    pass


class _FloodWait(_AuthFlowError):
    def __init__(self, seconds: int):
        self.seconds = max(0, int(seconds))
        super().__init__(f"Telegram rate limit. Retry in {self.seconds} seconds.")


def _api_module() -> Any:
    api_path = (Path(__file__).resolve().parent / "plugin_api.py").resolve()
    for candidate in tuple(sys.modules.values()):
        candidate_path = getattr(candidate, "__file__", None)
        if candidate_path and Path(candidate_path).resolve(strict=False) == api_path and hasattr(candidate, "_telegram_manager"):
            return candidate
    existing = sys.modules.get("hermes_dashboard_plugin_telegram")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_telegram", api_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot locate sibling plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manager() -> Any:
    return _api_module()._telegram_manager()


def _auth_dir() -> Path:
    path = Path(_profile_key()).expanduser().resolve(strict=False).parent
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _state_path() -> Path:
    return _auth_dir() / "auth_state.json"


def _profile_key() -> str:
    active = _ACTIVE_SESSION_KEY.get()
    if active:
        return active
    return str(_api_module()._dedicated_session_path().expanduser().resolve(strict=False))


def _read_state() -> dict[str, Any]:
    profile_key = _profile_key()
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {"stage": "idle"}
    except Exception:
        data = {"stage": "idle"}
    changed = False
    for name in ("phone", "phoneCodeHash", "qr_token_b64", "qr_expiry", "emailPurpose", "email", "emailCodeSent", "password", "code", "recoveryCode"):
        if name in data:
            data.pop(name, None)
            changed = True
    if changed:
        _persist(data)
    # Challenges that depend on in-memory secrets (sent-code hash, QR token)
    # die with the process: a restart must require a fresh flow. The password
    # stage carries no secret (SRP material is fetched fresh at submit), so it
    # survives a backend restart and the UI must stay on the password field.
    volatile = _VOLATILE_STATE.get(profile_key, {})
    if data.get("stage") in {"sent-code", "email-setup", "email-code", "password-recovery", "qr-pending"} and not volatile:
        data = {"stage": "idle"}
        _persist(data)
    return {**data, **volatile}


def _persist(state: dict[str, Any]) -> None:
    safe = {key: value for key, value in state.items() if key not in {"code", "password", *_TRANSIENT_AUTH_FIELDS}}
    path = _state_path()
    path.write_text(json.dumps(safe), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _write_state(state: dict[str, Any]) -> None:
    profile_key = _profile_key()
    if state.get("stage") in {"sent-code", "email-setup", "email-code", "password", "password-recovery", "qr-pending"}:
        transient = {key: value for key, value in state.items() if key in _TRANSIENT_AUTH_FIELDS}
        if transient:
            _VOLATILE_STATE[profile_key] = {"stage": state["stage"], **transient}
        elif state.get("stage") not in {"password", "password-recovery"}:
            _VOLATILE_STATE[profile_key] = {"stage": state["stage"]}
        elif profile_key in _VOLATILE_STATE:
            _VOLATILE_STATE[profile_key]["stage"] = state["stage"]
    else:
        _VOLATILE_STATE.pop(profile_key, None)
    _persist(state)


def _masked_phone(phone: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    return f"+{digits[:1]}{'*' * max(0, len(digits) - 3)}{digits[-2:]}" if digits else ""


def _public_stage(state: dict[str, Any]) -> dict[str, Any]:
    result = {"stage": state.get("stage", "idle"), "phone": _masked_phone(state.get("phone", "")),
              "me": state.get("me", ""), "qrToken": state.get("qr_token_b64", ""),
              "qrMatrix": None,
              "codeType": state.get("codeType", ""), "nextType": state.get("nextType", ""),
              "resendIn": max(0, int(state.get("resendAt", 0) - time.time())) if state.get("resendAt") else 0,
              "emailCodeSent": bool(state.get("emailCodeSent", False)),
              "hint": state.get("hint", ""), "hasRecovery": bool(state.get("hasRecovery", False)),
              "emailPattern": state.get("recoveryEmailPattern", state.get("emailPattern", ""))}
    token = result["qrToken"]
    if token and result["stage"] == "qr-pending":
        import qrcode

        qr = qrcode.QRCode(border=0)
        qr.add_data(f"tg://login?token={token}")
        qr.make(fit=True)
        result["qrMatrix"] = qr.get_matrix()
    return result


def _reject_extras(request: Request) -> None:
    extras = set(request.query_params.keys()) - {"profile"}
    if extras:
        raise HTTPException(status_code=422, detail="Unexpected query parameter.")


def _run(coro: Any, *, timeout: float = 90.0) -> Any:
    return _manager().run(coro, timeout=timeout)


def _route(coro: Any, *, timeout: float = 90.0) -> Any:
    profile_key = _profile_key()

    async def run_scoped() -> Any:
        token = _ACTIVE_SESSION_KEY.set(profile_key)
        try:
            return await coro
        finally:
            _ACTIVE_SESSION_KEY.reset(token)

    try:
        return _run(run_scoped(), timeout=timeout)
    except _FloodWait as exc:
        raise HTTPException(status_code=429, detail={"message": "Telegram rate limit.", "retryAfter": exc.seconds}) from None
    except _AuthFlowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception as exc:
        # Typed public errors are mapped below; never leak Telegram RPC payloads,
        # challenge material, or raw exception strings through the API.
        wait = getattr(exc, "seconds", None)
        if type(exc).__name__ == "FloodWaitError" and wait is not None:
            raise HTTPException(status_code=429, detail={"message": "Telegram rate limit.", "retryAfter": int(wait)}) from None
        raise HTTPException(status_code=502, detail="Telegram auth step failed.") from None


def _auth_types() -> Any:
    from telethon.tl import types
    return types.auth


async def _finish_authorization(client: Any, authorization: Any, phone: str = "") -> dict[str, Any]:
    types = _auth_types()
    if isinstance(authorization, types.AuthorizationSignUpRequired):
        state = {"stage": "signup-required"}
        _write_state(state)
        return _public_stage(state)
    if isinstance(authorization, types.Authorization):
        user = authorization.user
        await client._on_login(user)
        state = {"stage": "done", "me": getattr(user, "first_name", "") or ""}
        _write_state(state)
        return _public_stage(state)
    raise _AuthFlowError("Telegram returned an unsupported authorization response.")


async def _password_stage(client: Any) -> dict[str, Any]:
    from telethon import functions
    data = await client(functions.account.GetPasswordRequest())
    volatile = _VOLATILE_STATE.get(_profile_key(), {})
    state = {"stage": "password", "phone": volatile.get("phone", ""),
             "hint": getattr(data, "hint", "") or "", "hasRecovery": bool(getattr(data, "has_recovery", False)),
             "emailPattern": getattr(data, "email_unconfirmed_pattern", "") or ""}
    _write_state(state)
    return _public_stage({**state, **volatile})


async def _handle_sent_code(client: Any, phone: str, sent: Any, *, email_purpose: Any = None) -> dict[str, Any]:
    from telethon import types as telethon_types
    sent_types = _auth_types()
    if isinstance(sent, sent_types.SentCodeSuccess):
        return await _finish_authorization(client, sent.authorization, phone)
    if isinstance(sent, sent_types.SentCodePaymentRequired):
        state = {"stage": "payment-required"}
        _write_state(state)
        return _public_stage(state)
    if not isinstance(sent, sent_types.SentCode):
        raise _AuthFlowError("Telegram returned an unsupported code-delivery response.")
    phone_code_hash = getattr(sent, "phone_code_hash", None)
    if not phone_code_hash:
        raise _AuthFlowError("Telegram did not return a usable code challenge.")
    code_type = type(getattr(sent, "type", None)).__name__
    next_type = type(getattr(sent, "next_type", None)).__name__ if getattr(sent, "next_type", None) else ""
    stage = "email-setup" if code_type == "SentCodeTypeSetUpEmailRequired" else "email-code" if code_type == "SentCodeTypeEmailCode" else "sent-code"
    state = {"stage": stage, "phone": phone, "phoneCodeHash": phone_code_hash,
             "codeType": code_type, "nextType": next_type,
             "resendAt": time.time() + (getattr(sent, "timeout", None) or 0),
             "emailPurpose": email_purpose}
    if stage == "email-setup":
        state["emailPattern"] = getattr(sent.type, "email_pattern", "") or ""
    _write_state(state)
    return _public_stage({**state, **_VOLATILE_STATE.get(_profile_key(), {})})


def _typed_error(exc: BaseException, *, password: bool = False) -> _AuthFlowError | None:
    name = type(exc).__name__
    if name == "FloodWaitError":
        return _FloodWait(getattr(exc, "seconds", 0))
    mapping = {
        "PhoneCodeInvalidError": "Code invalid; check it and retry.",
        "PhoneCodeEmptyError": "Code is empty; enter the code and retry.",
        "PhoneCodeExpiredError": "Code expired; request a fresh code.",
        "PhoneCodeHashEmptyError": "Code challenge expired; request a fresh code.",
        "PhoneNumberInvalidError": "Phone number is invalid.",
        "PhoneNumberBannedError": "This phone number is not permitted to sign in.",
        "PhoneNumberFloodError": "Too many login-code requests; try again later.",
        "PasswordHashInvalidError": "Password incorrect; check the hint and retry.",
        "SrpIdInvalidError": "Password challenge expired; retry with a fresh challenge.",
        "SrpPasswordChangedError": "Password changed in another session; retry with the current password.",
        "SrpPasswordChangedError": "Password changed in another session; retry with the current password.",
        "PhonePasswordFloodError": "Too many password attempts; try again later.",
    }
    message = mapping.get(name)
    return _AuthFlowError(message) if message else None


async def _start_phone(phone: str) -> dict[str, Any]:
    from telethon import functions
    from telethon.tl.types import CodeSettings
    client = _manager().client
    if await client.is_user_authorized():
        me = await client.get_me()
        state = {"stage": "done", "me": getattr(me, "first_name", "") or ""}
        _write_state(state)
        return _public_stage(state)
    api_id, api_hash = _api_module()._load_telegram_credentials()
    try:
        sent = await client(functions.auth.SendCodeRequest(phone, api_id, api_hash, CodeSettings()))
    except Exception as exc:
        mapped = _typed_error(exc)
        if mapped: raise mapped from None
        raise
    return await _handle_sent_code(client, phone, sent)


async def _submit_code(code: str) -> dict[str, Any]:
    from telethon import functions
    from telethon.errors import SessionPasswordNeededError
    state = _read_state()
    if state.get("stage") not in {"sent-code", "email-code"}:
        raise _AuthFlowError("No pending code; request a code first.")
    phone, phone_code_hash = state.get("phone", ""), state.get("phoneCodeHash", "")
    client = _manager().client
    try:
        authorization = await client(functions.auth.SignInRequest(phone, phone_code_hash, code))
    except SessionPasswordNeededError:
        return await _password_stage(client)
    except Exception as exc:
        mapped = _typed_error(exc)
        if mapped:
            if type(exc).__name__ in {"PhoneCodeExpiredError", "PhoneCodeHashEmptyError"}:
                _write_state({"stage": "idle"})
            raise mapped from None
        raise
    return await _finish_authorization(client, authorization, phone)


async def _submit_email_code(code: str) -> dict[str, Any]:
    from telethon import functions, types
    state = _read_state()
    if state.get("stage") != "email-code":
        raise _AuthFlowError("No email verification code is pending.")
    request = functions.auth.SignInRequest(state["phone"], state["phoneCodeHash"], email_verification=types.EmailVerificationCode(code))
    client = _manager().client
    try:
        authorization = await client(request)
    except Exception as exc:
        mapped = _typed_error(exc)
        if mapped: raise mapped from None
        raise
    return await _finish_authorization(client, authorization, state["phone"])


async def _resend_code() -> dict[str, Any]:
    from telethon import functions
    state = _read_state()
    if state.get("stage") not in {"sent-code", "email-code"}:
        raise _AuthFlowError("No resendable code challenge is pending.")
    remaining = max(0, int(state.get("resendAt", 0) - time.time()))
    if remaining:
        raise _FloodWait(remaining)
    client = _manager().client
    try:
        sent = await client(functions.auth.ResendCodeRequest(state["phone"], state["phoneCodeHash"]))
    except Exception as exc:
        mapped = _typed_error(exc)
        if mapped: raise mapped from None
        if type(exc).__name__ == "SendCodeUnavailableError":
            raise _AuthFlowError("Telegram cannot resend by that delivery method yet.") from None
        raise
    return await _handle_sent_code(client, state["phone"], sent)


async def _start_email_setup(email: str) -> dict[str, Any]:
    from telethon import functions, types
    state = _read_state()
    if state.get("stage") != "email-setup":
        raise _AuthFlowError("Telegram has not requested a login email.")
    purpose = types.EmailVerifyPurposeLoginSetup(state["phone"], state["phoneCodeHash"])
    client = _manager().client
    try:
        result = await client(functions.account.SendVerifyEmailCodeRequest(purpose=purpose, email=email))
    except Exception as exc:
        mapped = _typed_error(exc)
        if mapped: raise mapped from None
        raise
    _write_state({**state, "email": email, "emailCodeSent": True})
    return {**_public_stage(_read_state()), "emailCodeSent": True}


async def _verify_email_setup(code: str) -> dict[str, Any]:
    from telethon import functions, types
    state = _read_state()
    if state.get("stage") != "email-setup" or not state.get("emailCodeSent"):
        raise _AuthFlowError("No login-email verification is pending.")
    purpose = types.EmailVerifyPurposeLoginSetup(state["phone"], state["phoneCodeHash"])
    client = _manager().client
    try:
        result = await client(functions.account.VerifyEmailRequest(purpose=purpose, verification=types.EmailVerificationCode(code)))
    except Exception as exc:
        mapped = _typed_error(exc)
        if mapped: raise mapped from None
        raise
    sent = getattr(result, "sent_code", None)
    if sent is None:
        raise _AuthFlowError("Telegram returned no phone-code challenge after email verification.")
    return await _handle_sent_code(client, state["phone"], sent)


async def _password_recovery_request() -> dict[str, Any]:
    from telethon import functions
    if _read_state().get("stage") != "password":
        raise _AuthFlowError("Password recovery is available only at the 2FA step.")
    volatile = _VOLATILE_STATE.get(_profile_key(), {})
    if not volatile.get("hasRecovery"):
        raise _AuthFlowError("No Telegram recovery email is available for this account.")
    client = _manager().client
    try:
        result = await client(functions.auth.RequestPasswordRecoveryRequest())
    except Exception as exc:
        mapped = _typed_error(exc, password=True)
        if mapped: raise mapped from None
        raise
    state = {**volatile, "stage": "password-recovery",
             "recoveryEmailPattern": getattr(result, "email_pattern", "") or ""}
    _write_state(state)
    return _public_stage({**state, **_VOLATILE_STATE.get(_profile_key(), {})})


async def _password_recovery_verify(code: str) -> dict[str, Any]:
    from telethon import functions
    state = _read_state()
    if state.get("stage") != "password-recovery":
        raise _AuthFlowError("No password recovery challenge is pending.")
    client = _manager().client
    try:
        await client(functions.auth.CheckRecoveryPasswordRequest(code))
        authorization = await client(functions.auth.RecoverPasswordRequest(code))
    except Exception as exc:
        mapped = _typed_error(exc, password=True)
        if mapped: raise mapped from None
        if type(exc).__name__ in {"PasswordRecoveryExpiredError", "PasswordRecoveryNaError"}:
            _write_state({"stage": "password"})
            raise _AuthFlowError("Password recovery code expired or is not accepted; request a fresh code.") from None
        raise
    return await _finish_authorization(client, authorization)


async def _submit_password(password: str) -> dict[str, Any]:
    from telethon import functions
    from telethon.errors import SessionPasswordNeededError
    from telethon.password import compute_check
    state = _read_state()
    if state.get("stage") != "password":
        raise _AuthFlowError("No pending password step.")
    client = _manager().client
    try:
        params = await client(functions.account.GetPasswordRequest())
        authorization = await client(functions.auth.CheckPasswordRequest(compute_check(params, password)))
    except Exception as exc:
        name = type(exc).__name__
        mapped = _typed_error(exc, password=True)
        if name == "SrpIdInvalidError":
            try:
                params = await client(functions.account.GetPasswordRequest())
                authorization = await client(functions.auth.CheckPasswordRequest(compute_check(params, password)))
            except Exception as retry_exc:
                mapped_retry = _typed_error(retry_exc, password=True)
                if mapped_retry: raise mapped_retry from None
                raise
        elif name == "SessionPasswordNeededError":
            return await _password_stage(client)
        elif mapped:
            raise mapped from None
        else:
            raise
    return await _finish_authorization(client, authorization, state.get("phone", ""))


async def _handle_qr_result(client: Any, result: Any) -> tuple[dict[str, Any], Any | None]:
    from telethon import functions, types
    if isinstance(result, types.auth.LoginTokenMigrateTo):
        await client._switch_dc(result.dc_id)
        result = await client(functions.auth.ImportLoginTokenRequest(result.token))
        if isinstance(result, types.auth.LoginTokenMigrateTo):
            return {"stage": "error", "error": "Telegram returned a repeated DC migration."}, None
    if isinstance(result, types.auth.LoginTokenSuccess):
        await client._on_login(result.authorization.user)
        me = await client.get_me()
        return {"stage": "done", "me": getattr(me, "first_name", "") or ""}, None
    if isinstance(result, types.auth.LoginToken):
        return {"stage": "qr-pending", "qr_token_b64": _qr_token_b64(result.token),
                "qr_expiry": result.expires.timestamp()}, result
    return {"stage": "error", "error": "Telegram returned an unexpected QR authorization result."}, None


async def _qr_wait_loop(client: Any, qr: Any) -> None:
    profile_key = _profile_key()
    from telethon import events, functions, types
    manager = _manager()
    update_event = asyncio.Event()

    async def on_login_token(_update: Any) -> None:
        update_event.set()

    client.add_event_handler(on_login_token, events.Raw(types.UpdateLoginToken))
    try:
        while True:
            expiry = qr.expires
            remaining = max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds())
            try:
                # Wait outside the session lock, so ordinary API/status requests
                # can use the single client while no QR RPC is in flight.
                await asyncio.wait_for(update_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                async with manager._lock:
                    api_id, api_hash = _api_module()._load_telegram_credentials()
                    result = await client(functions.auth.ExportLoginTokenRequest(api_id, api_hash, []))
                    state, refreshed = await _handle_qr_result(client, result)
                    if refreshed is not None:
                        qr._resp = refreshed
                    _write_state(state)
                if state["stage"] != "qr-pending":
                    return
                continue
            update_event.clear()
            async with manager._lock:
                api_id, api_hash = _api_module()._load_telegram_credentials()
                result = await client(functions.auth.ExportLoginTokenRequest(api_id, api_hash, []))
                state, refreshed = await _handle_qr_result(client, result)
                if refreshed is not None:
                    qr._resp = refreshed
                _write_state(state)
            if state["stage"] != "qr-pending":
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if type(exc).__name__ == "SessionPasswordNeededError":
            await _password_stage(client)
        elif type(exc).__name__ in {"AuthTokenExpiredError", "AuthTokenInvalidError", "AuthTokenInvalid2Error"}:
            _write_state({"stage": "error", "error": "QR token expired or is invalid. Cancel and start a new QR login."})
        else:
            _write_state({"stage": "error", "error": "Telegram QR authorization failed."})
    finally:
        client.remove_event_handler(on_login_token)
        if _QR_TASK.get(profile_key) is asyncio.current_task():
            _QR_TASK.pop(profile_key, None)



def _qr_token_b64(token: Any) -> str:
    raw = getattr(token, "token", token)
    if isinstance(raw, str): raw = raw.encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


async def _qr_start() -> dict[str, Any]:
    profile_key = _profile_key()
    from telethon import errors
    state = _read_state()
    client = _manager().client
    if await client.is_user_authorized():
        me = await client.get_me()
        done = {"stage": "done", "me": getattr(me, "first_name", "") or ""}
        _write_state(done)
        return _public_stage(done)
    old_task = _QR_TASK.get(profile_key)
    if old_task is not None:
        old_task.cancel()
    try:
        qr = await client.qr_login()
    except Exception as exc:
        mapped = _typed_error(exc)
        if mapped: raise mapped from None
        raise
    initial = getattr(qr, "_resp", None)
    from telethon import types
    if isinstance(initial, (types.auth.LoginTokenSuccess, types.auth.LoginTokenMigrateTo)):
        state, refreshed = await _handle_qr_result(client, initial)
        _write_state(state)
        if refreshed is None:
            return _public_stage(state)
        qr._resp = refreshed
    elif not isinstance(initial, types.auth.LoginToken):
        raise _AuthFlowError("Telegram returned an unsupported QR login response.")
    expiry = qr.expires
    pending = {"stage": "qr-pending", "qr_token_b64": _qr_token_b64(qr.token), "qr_expiry": expiry.timestamp()}
    _write_state(pending)
    _QR_TASK[profile_key] = asyncio.create_task(_qr_wait_loop(client, qr), name="telegram-qr-login-waiter")
    return _public_stage({**pending, **_VOLATILE_STATE.get(profile_key, {})})


async def _qr_poll() -> dict[str, Any]:
    state = _read_state()
    if state.get("stage") not in {"qr-pending", "password", "done", "error"}:
        raise _AuthFlowError("QR login not started.")
    return _public_stage(state)


async def _cancel() -> None:
    profile_key = _profile_key()
    state = _read_state()
    client = _manager().client
    qr_task = _QR_TASK.pop(profile_key, None)
    if qr_task is not None:
        qr_task.cancel()
    if state.get("phoneCodeHash") and state.get("stage") in {"sent-code", "email-code", "email-setup"}:
        from telethon import functions
        try:
            await client(functions.auth.CancelCodeRequest(state["phone"], state["phoneCodeHash"]))
        except Exception as exc:
            mapped = _typed_error(exc)
            if mapped: raise mapped from None
    _write_state({"stage": "idle"})


async def _logout() -> None:
    profile_key = _profile_key()
    qr_task = _QR_TASK.pop(profile_key, None)
    if qr_task is not None:
        qr_task.cancel()
    manager = _manager()
    client = manager.client
    if await client.is_user_authorized():
        ok = await client.log_out()
        if not ok:
            raise _AuthFlowError("Telegram logout did not complete.")
    else:
        await client.disconnect()
    await manager.replace_client()
    _write_state({"stage": "idle"})


@router.post("/auth/start-phone")
def auth_start_phone(request: Request, body: StartPhoneRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_start_phone(body.phone))


@router.post("/auth/submit-code")
def auth_submit_code(request: Request, body: SubmitCodeRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_submit_code(body.code))


@router.post("/auth/submit-email-code")
def auth_submit_email_code(request: Request, body: EmailCodeRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_submit_email_code(body.code))


@router.post("/auth/resend-code")
def auth_resend_code(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_resend_code())


@router.post("/auth/email-setup")
def auth_email_setup(request: Request, body: EmailRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_start_email_setup(body.email))


@router.post("/auth/verify-email-setup")
def auth_verify_email_setup(request: Request, body: EmailCodeRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_verify_email_setup(body.code))


@router.post("/auth/password-recovery")
def auth_password_recovery(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_password_recovery_request())


@router.post("/auth/password-recovery/verify")
def auth_password_recovery_verify(request: Request, body: EmailCodeRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_password_recovery_verify(body.code))


@router.post("/auth/submit-password")
def auth_submit_password(request: Request, body: SubmitPasswordRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_submit_password(body.password))


@router.post("/auth/qr-start")
def auth_qr_start(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_qr_start())


@router.get("/auth/qr-poll")
def auth_qr_poll(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_qr_poll(), timeout=10.0)


@router.get("/auth/state")
def auth_state(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _public_stage(_read_state())


@router.post("/auth/cancel")
def auth_cancel(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_cancel())


@router.post("/auth/logout")
def auth_logout(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK: return _route(_logout())


def _shutdown() -> None:
    try:
        _api_module()._shutdown_managers()
    except Exception:
        pass


router.add_event_handler("shutdown", _shutdown)
