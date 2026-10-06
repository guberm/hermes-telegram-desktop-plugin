"""Standalone Telegram auth flow for the desktop plugin.

Runs its own TelegramClient (a separate client from the read-only session
bridge) so login/QR/2FA never touches the configured rss_reader session.
State persisted under <hermes_home>/telegram-plugin-auth/:

  auth_state.json : {"stage": idle|sent-code|qr-pending|password|done, ...}
  telegram-plugin-auth.session : Telethon SQLite session of THIS client

Stages:
  POST /auth/start-phone {phone}   -> sends code, stage=sent-code
  POST /auth/submit-code {code}    -> stage=password | done
  POST /auth/submit-password {pw}  -> stage=done
  POST /auth/qr-start              -> exports login token, stage=qr-pending
  GET  /auth/qr-poll               -> exports the token again (Telegram's QR
                                      polling protocol), stage=done when the
                                      scan is accepted
  POST /auth/cancel                -> disconnect + stage=idle
  GET  /auth/state                 -> current stage (no secrets)
  POST /auth/logout                -> logs THIS client out (session wipe)

Only the caller-provided values pass through; nothing secret is logged and
no code/password is persisted to disk.

 ponytail: QR token encoding is Telegram's own base64url token; the desktop
 renders it as tg://login?token=... via an external QR lib-free canvas.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

router = APIRouter()

_AUTH_TIMEOUT_SECONDS = 25.0
_LOCK = threading.RLock()
_VOLATILE_STATE: dict[str, Any] = {}
_TRANSIENT_AUTH_FIELDS = {"phoneCodeHash", "qr_token_b64", "qr_started"}

_PHONE_RE_OK = r"^\+?[0-9]{5,20}$"
_CODE_RE_OK = r"^[0-9]{4,8}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartPhoneRequest(_Strict):
    phone: Annotated[str, StringConstraints(min_length=5, max_length=20, pattern=_PHONE_RE_OK)]


class SubmitCodeRequest(_Strict):
    code: Annotated[str, StringConstraints(min_length=4, max_length=8, pattern=_CODE_RE_OK)]


class SubmitPasswordRequest(_Strict):
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


@router.post("/auth/start-phone")
def auth_start_phone(request: Request, body: StartPhoneRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK:
        try:
            return _run(_start_phone(body.phone))
        except _AuthFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(status_code=502, detail="Telegram auth step failed.") from None


@router.post("/auth/submit-code")
def auth_submit_code(request: Request, body: SubmitCodeRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK:
        try:
            return _run(_submit_code(body.code))
        except _AuthFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(status_code=502, detail="Telegram auth step failed.") from None


@router.post("/auth/submit-password")
def auth_submit_password(request: Request, body: SubmitPasswordRequest) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK:
        try:
            return _run(_submit_password(body.password))
        except _AuthFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(status_code=502, detail="Telegram auth step failed.") from None


@router.post("/auth/qr-start")
def auth_qr_start(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK:
        try:
            return _run(_qr_start())
        except _AuthFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(status_code=502, detail="Telegram auth step failed.") from None


@router.get("/auth/qr-poll")
def auth_qr_poll(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK:
        try:
            return _run(_qr_poll())
        except _AuthFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception:
            raise HTTPException(status_code=502, detail="Telegram auth step failed.") from None


@router.get("/auth/state")
def auth_state(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK:
        state = _read_state()
        return {
            "stage": state.get("stage", "idle"),
            "phone": state.get("phone", ""),
            "me": state.get("me", ""),
            "qrToken": state.get("qr_token_b64", ""),
        }


@router.post("/auth/cancel")
def auth_cancel(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK:
        try:
            _run(_cancel())
        except Exception:
            pass
        _write_state({"stage": "idle"})
        return {"stage": "idle"}


@router.post("/auth/logout")
def auth_logout(request: Request) -> dict[str, Any]:
    _reject_extras(request)
    with _LOCK:
        try:
            _run(_logout())
        except Exception:
            pass
        _write_state({"stage": "idle"})
        return {"stage": "idle", "loggedOut": True}


# --- internals ----------------------------------------------------------------


class _AuthFlowError(RuntimeError):
    pass


def _api_credentials() -> tuple[int, str]:
    """Resolve API credentials without depending on an RSS session_path."""

    import plugin_api as _api

    return _api._load_telegram_credentials()


_API_CREDENTIALS: tuple[int, str] | None = None


def _credentials_cached() -> tuple[int, str]:
    global _API_CREDENTIALS
    if _API_CREDENTIALS is None:
        _API_CREDENTIALS = _api_credentials()
    return _API_CREDENTIALS


def _reject_extras(request: Request) -> None:
    extras = set(request.query_params.keys()) - {"profile"}
    if extras:
        raise HTTPException(status_code=422, detail="Unexpected query parameter.")


def _auth_dir() -> Path:
    from hermes_constants import get_hermes_home

    path = Path(get_hermes_home()) / "telegram-plugin-auth"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _session_path() -> Path:
    import plugin_api as _api

    return _api._dedicated_session_path()


def _state_path() -> Path:
    return _auth_dir() / "auth_state.json"


def _read_state() -> dict[str, Any]:
    global _VOLATILE_STATE
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {"stage": "idle"}
    except Exception:
        data = {"stage": "idle"}
    stale_fields = _TRANSIENT_AUTH_FIELDS.intersection(data)
    if stale_fields:
        # Scrub challenge bearer material left by older plugin versions.
        data = {key: value for key, value in data.items() if key not in _TRANSIENT_AUTH_FIELDS}
        path.write_text(json.dumps(data), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    if data.get("stage") in {"sent-code", "qr-pending"} and not _VOLATILE_STATE:
        # Challenge state cannot safely survive a process restart without its
        # in-memory token; require a new, explicit auth start.
        data = {"stage": "idle"}
        path.write_text(json.dumps(data), encoding="utf-8")
    return {**data, **_VOLATILE_STATE}


def _write_state(state: dict[str, Any]) -> None:
    # OTP/password and Telegram's pending code hash/QR bearer token are never
    # persisted. Keep only the transient challenge material in process memory.
    global _VOLATILE_STATE
    _VOLATILE_STATE = (
        {k: v for k, v in state.items() if k in _TRANSIENT_AUTH_FIELDS}
        if state.get("stage") in {"sent-code", "qr-pending"}
        else {}
    )
    safe = {k: v for k, v in state.items() if k not in {"code", "password", *_TRANSIENT_AUTH_FIELDS}}
    path = _state_path()
    path.write_text(json.dumps(safe), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=_AUTH_TIMEOUT_SECONDS))


def _new_client() -> Any:
    from telethon import TelegramClient

    import plugin_api as _api

    api_id, api_hash = _api._load_telegram_credentials()
    return TelegramClient(
        str(_api._dedicated_session_path()),
        api_id,
        api_hash,
        receive_updates=False,
    )


def _public_stage(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": state.get("stage", "idle"),
        "phone": state.get("phone", ""),
        "me": state.get("me", ""),
        "qrToken": state.get("qr_token_b64", ""),
    }


async def _start_phone(phone: str) -> dict[str, Any]:
    from telethon import functions
    from telethon.tl.types import CodeSettings

    api_id, api_hash = _credentials_cached()
    client = _new_client()
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            state = {"stage": "done", "me": getattr(me, "first_name", "") or ""}
            _write_state(state)
            return _public_stage(state)
        sent = await client(functions.auth.SendCodeRequest(
            phone, api_id, api_hash, CodeSettings(),
        ))
        state = {
            "stage": "sent-code",
            "phone": phone,
            "phoneCodeHash": sent.phone_code_hash,
        }
        _write_state(state)
        return _public_stage(state)
    finally:
        await client.disconnect()


async def _submit_code(code: str) -> dict[str, Any]:
    from telethon import functions

    state = _read_state()
    if state.get("stage") != "sent-code":
        raise _AuthFlowError("No pending code; request a code first.")
    phone = state.get("phone", "")
    phone_code_hash = state.get("phoneCodeHash", "")
    client = _new_client()
    await client.connect()
    try:
        try:
            await client(functions.auth.SignInRequest(phone, phone_code_hash, code))
        except Exception as exc:
            name = type(exc).__name__
            if name in ("SessionPasswordNeededError", "SessionPasswordNeededError2"):
                state = {**state, "stage": "password"}
                _write_state(state)
                return _public_stage(state)
            if name == "PhoneCodeInvalidError":
                raise _AuthFlowError("Code invalid; re-check the SMS/app code.") from None
            if name == "PhoneCodeExpiredError":
                raise _AuthFlowError("Code expired; request a new one.") from None
            raise
        me = await client.get_me()
        state = {"stage": "done", "me": getattr(me, "first_name", "") or "", "phone": phone}
        _write_state(state)
        return _public_stage(state)
    finally:
        await client.disconnect()


async def _submit_password(password: str) -> dict[str, Any]:
    from telethon import functions
    from telethon.password import compute_check

    state = _read_state()
    if state.get("stage") != "password":
        raise _AuthFlowError("No pending password step.")
    client = _new_client()
    await client.connect()
    try:
        pw = await client(functions.account.GetPasswordRequest())
        await client(functions.auth.CheckPasswordRequest(compute_check(pw, password)))
        me = await client.get_me()
        state = {"stage": "done", "me": getattr(me, "first_name", "") or ""}
        _write_state(state)
        return _public_stage(state)
    finally:
        await client.disconnect()


def _qr_token_b64(token: Any) -> str:
    raw = getattr(token, "token", b"")
    if isinstance(raw, str):
        raw = raw.encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def _qr_start() -> dict[str, Any]:
    from telethon import functions

    api_id, api_hash = _credentials_cached()
    client = _new_client()
    await client.connect()
    try:
        result = await client(functions.auth.ExportLoginTokenRequest(api_id, api_hash, []))
        state = {
            "stage": "qr-pending",
            "qr_token_b64": _qr_token_b64(result),
            "qr_started": time.time(),
        }
        _write_state(state)
        return _public_stage(state)
    finally:
        await client.disconnect()


async def _qr_poll() -> dict[str, Any]:
    from telethon import functions
    from telethon.errors import (
        AuthTokenAlreadyAcceptedError, AuthTokenExpiredError, AuthTokenInvalidError,
    )

    state = _read_state()
    if state.get("stage") not in {"qr-pending", "done"}:
        raise _AuthFlowError("QR login not started.")
    if state.get("stage") == "done":
        return _public_stage(state)
    api_id, api_hash = _credentials_cached()
    client = _new_client()
    await client.connect()
    try:
        try:
            result = await client(functions.auth.ExportLoginTokenRequest(api_id, api_hash, []))
        except AuthTokenAlreadyAcceptedError:
            # The scanned token was accepted; the client is authorized.
            me = await client.get_me()
            done = {"stage": "done", "me": getattr(me, "first_name", "") or ""}
            _write_state(done)
            return _public_stage(done)
        except (AuthTokenExpiredError, AuthTokenInvalidError):
            fresh = await client(functions.auth.ExportLoginTokenRequest(api_id, api_hash, []))
            state = {"stage": "qr-pending", "qr_token_b64": _qr_token_b64(fresh), "qr_started": time.time()}
            _write_state(state)
            return _public_stage(state)
        cls = type(result).__name__
        if cls == "AuthLoginTokenSuccess":
            me = await client.get_me()
            done = {"stage": "done", "me": getattr(me, "first_name", "") or ""}
            _write_state(done)
            return _public_stage(done)
        if cls == "AuthLoginTokenMigrateTo":
            await client._switch_dc(result.dc_id)
            await client(functions.auth.ImportLoginTokenRequest(result.token))
            me = await client.get_me()
            done = {"stage": "done", "me": getattr(me, "first_name", "") or ""}
            _write_state(done)
            return _public_stage(done)
        # Still pending: return the same (or refreshed) token for the QR image.
        return _public_stage(state)
    finally:
        await client.disconnect()


async def _cancel() -> None:
    client = _new_client()
    try:
        await client.connect()
    finally:
        await client.disconnect()


async def _logout() -> None:
    client = _new_client()
    await client.connect()
    try:
        if await client.is_user_authorized():
            from telethon import functions

            await client(functions.auth.LogOutRequest())
        session_file = _session_path().with_suffix(".session")
        if session_file.exists():
            session_file.unlink()
    finally:
        await client.disconnect()
