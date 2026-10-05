"""Staged, profile-scoped Telegram API for the Hermes desktop plugin.

Mirrors the reviewed prepare/commit pattern of the Hermes Gmail desktop
plugin: every mutation requires an explicit preview ticket, an explicit
confirmed commit, and a readback verification of the exact resulting state.
The Telethon user session is resolved at request time from the active
profile's Hermes home; this module never copies or persists credentials.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

router = APIRouter()

_TELETHON_TIMEOUT_SECONDS = 20.0
_TICKET_TTL_SECONDS = 300.0
_MAX_TICKETS = 256
_MAX_SCOPES = 128
_MAX_PROVIDER_TEXT = 16 * 1024
_TOPIC_ROOT_RE = re.compile(r"^https?://t\.me/c/(\d+)/(?:\d+/)?(\d+)")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
_BACKEND_INSTANCE = secrets.token_urlsafe(24)
_lock = threading.RLock()
_scopes: "OrderedDict[str, _ScopeBinding]" = OrderedDict()
_tickets: "OrderedDict[str, Ticket]" = OrderedDict()
_now: Callable[[], float] = time.time

ScopeText = Annotated[str, StringConstraints(min_length=20, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SendPrepare(_StrictModel):
    scope: ScopeText
    action: Literal["send"]
    peer: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    message: Annotated[str, StringConstraints(max_length=4096)]


class ReplyPrepare(_StrictModel):
    scope: ScopeText
    action: Literal["reply"]
    peer: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    messageId: int = Field(ge=1, le=10_000_000_000)
    message: Annotated[str, StringConstraints(max_length=4096)]


class MarkReadPrepare(_StrictModel):
    scope: ScopeText
    action: Literal["mark-read"]
    peer: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    maxId: int = Field(ge=1, le=10_000_000_000)


class DeletePrepare(_StrictModel):
    scope: ScopeText
    action: Literal["delete"]
    peer: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    messageId: int = Field(ge=1, le=10_000_000_000)


PrepareRequest = Annotated[
    SendPrepare | ReplyPrepare | MarkReadPrepare | DeletePrepare,
    Field(discriminator="action"),
]


class CommitRequest(_StrictModel):
    scope: ScopeText
    confirmationToken: ScopeText
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_boolean_consent(cls, value: Any) -> bool:
        # Literal[True] otherwise accepts 1 and 1.0 even with strict=True.
        if value is not True:
            raise ValueError("explicit boolean confirmation is required")
        return value


@dataclass(frozen=True)
class _ScopeBinding:
    backend: str
    home: str
    me: str
    created: float


@dataclass(frozen=True)
class Ticket:
    scope: str
    action: str
    me: str
    payload: dict[str, Any]
    preview: dict[str, Any]
    message_snapshot: dict[str, Any] | None
    expires: float


class _AuthUnavailable(RuntimeError):
    pass


class _TelegramError(RuntimeError):
    pass
# --- HTML preview sanitization -----------------------------------------------


class _InertPreviewSanitizer(HTMLParser):
    """Project untrusted message HTML to a bounded inert text/links tree."""

    _VOID = {"br", "hr", "img"}
    _ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
                "code", "pre", "a", "br", "hr", "span"}
    _SAFE_URL = re.compile(r"^https?://[^\s<>\"']+$")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open_tags: list[str] = []
        self._link_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self.out.append("\n")
            return
        if tag == "hr":
            self.out.append("\n---\n")
            return
        if tag not in self._ALLOWED:
            return
        if tag == "a":
            href = next((value for name, value in attrs if name and name.lower() == "href"), None)
            if href and self._SAFE_URL.match(href) and len(href) <= 2048:
                self._link_href = href
            else:
                self._link_href = ""
            self.out.append("<b>")
            self.open_tags.append("b")
            return
        if tag == "code" or tag == "pre":
            self.out.append(f"<{tag}>")
            self.open_tags.append(tag)
            return
        if tag in {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span"}:
            mapped = {"strong": "b", "em": "i", "ins": "u", "strike": "s", "del": "s", "span": "span"}
            out_tag = mapped.get(tag, tag)
            self.out.append(f"<{out_tag}>")
            self.open_tags.append(out_tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a":
            href = self._link_href
            self._link_href = None
            if self.open_tags and self.open_tags[-1] == "b":
                self.open_tags.pop()
                self.out.append("</b>")
            if href:
                self.out.append(f' (<a href="{escape(href, quote=True)}">{escape(href)}</a>)')
            return
        if tag in {"code", "pre"} and tag in self.open_tags:
            self.open_tags.remove(tag)
            self.out.append(f"</{tag}>")
        elif tag in {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span"}:
            if self.open_tags and self.open_tags[-1] in {"b", "i", "u", "s", "span"}:
                closing = self.open_tags.pop()
                self.out.append(f"</{closing}>")

    def handle_data(self, data: str) -> None:
        text = data
        if self._link_href is not None:
            # Inside a link: text is already escaped by the parser; the href is
            # appended after the closing tag, so keep text inert.
            self.out.append(escape(unescape(text)))
            return
        self.out.append(escape(text))

    def result(self) -> str:
        while self.open_tags:
            closing = self.open_tags.pop()
            self.out.append(f"</{closing}>")
        return "".join(self.out)[:8192]


def sanitize_message_html(source: str | None) -> str:
    if not source:
        return ""
    parser = _InertPreviewSanitizer()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        return escape(unescape(source))[:8192]
    return parser.result()


# --- Hermes home / Telethon session ------------------------------------------


def _current_home() -> str:
    from hermes_constants import get_hermes_home

    return str(Path(get_hermes_home()).expanduser().resolve(strict=False))


def _load_telegram_config() -> dict[str, Any]:
    path = Path(_current_home()) / "rss_reader" / "config.json"
    if not path.is_file():
        raise _AuthUnavailable("missing Telegram session configuration")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        raise _AuthUnavailable("invalid Telegram session configuration") from exc
    if not isinstance(config, dict):
        raise _AuthUnavailable("invalid Telegram session configuration")
    api_id = config.get("api_id")
    api_hash = config.get("api_hash")
    session_path = config.get("session_path")
    if not isinstance(api_id, int) or not api_hash or not isinstance(session_path, str) or not session_path:
        raise _AuthUnavailable("incomplete Telegram session configuration")
    return {"api_id": api_id, "api_hash": str(api_hash), "session_path": session_path}


async def _with_client(handler: Callable[[Any], Any]) -> Any:
    from telethon import TelegramClient

    config = _load_telegram_config()
    session_path = Path(config["session_path"])
    client = TelegramClient(
        str(session_path),
        config["api_id"],
        config["api_hash"],
        receive_updates=False,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise _AuthUnavailable("Telegram session is not authorized")
        return await handler(client)
    finally:
        await client.disconnect()


def _run_async(coroutine: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:  # pragma: no cover - FastAPI runs handlers in worker threads
        raise RuntimeError("must not run inside a running event loop")
    return asyncio.run(asyncio.wait_for(coroutine, timeout=_TELETHON_TIMEOUT_SECONDS))


def _public_message(message: Any, me_id: int) -> dict[str, Any]:
    sender = message.sender
    sender_name = ""
    if sender is not None:
        first = getattr(sender, "first_name", None) or ""
        last = getattr(sender, "last_name", None) or ""
        title = getattr(sender, "title", None) or ""
        sender_name = title or f"{first} {last}".strip() or getattr(sender, "username", "") or ""
    text = message.message or ""
    media_kind = ""
    if message.media:
        media_kind = message.media.__class__.__name__
    return {
        "id": int(message.id),
        "peer": _peer_display_name(message.chat if message.chat is not None else message.sender),
        "date": message.date.isoformat() if message.date else "",
        "out": bool(message.out),
        "unread": not message.out and bool(message.unread),
        "sender": sender_name,
        "text": text[:4096],
        "htmlPreview": sanitize_message_html(text[:4096]),
        "media": media_kind,
        "replyTo": int(message.reply_to.reply_to_msg_id) if message.reply_to else None,
        "mine": bool(message.out) or (message.sender_id == me_id if message.sender_id is not None else False),
    }


def _peer_display_name(entity: Any) -> str:
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    username = getattr(entity, "username", None) or ""
    name = f"{first} {last}".strip()
    return name or (f"@{username}" if username else "")


def _peer_key(entity: Any) -> str:
    if entity is None:
        return ""
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    identifier = getattr(entity, "id", None)
    if identifier is None:
        return ""
    # Channels/supergroups are negative; keep a plain deterministic key.
    return f"id:{identifier}"


def _clean_provider_text(value: Any, limit: int = _MAX_PROVIDER_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _peer_ref_error(detail: str = "Telegram peer could not be resolved.") -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def _provider_error(*, mutation_started: bool = False) -> HTTPException:
    if mutation_started:
        return HTTPException(
            status_code=502,
            detail="Action outcome is uncertain. Check Telegram before trying again.",
        )
    return HTTPException(status_code=502, detail="Telegram service is unavailable.")


def _reject_query_extras(request: Request, allowed: set[str]) -> None:
    # Hermes consumes profile as its authenticated routing envelope.
    extras = set(request.query_params.keys()) - allowed - {"profile"}
    if extras:
        raise HTTPException(status_code=422, detail="Unexpected query parameter.")


def _binding(scope: str) -> _ScopeBinding:
    if not _TOKEN_RE.fullmatch(scope):
        raise HTTPException(status_code=422, detail="Invalid scope.")
    home = _current_home()
    with _lock:
        binding = _scopes.get(scope)
        if binding is not None:
            if binding.backend == _BACKEND_INSTANCE and binding.home == home:
                _scopes.move_to_end(scope)
                return binding
            del _scopes[scope]
    raise HTTPException(status_code=409, detail="Telegram connection changed; refresh status.")


def _new_scope(home: str, me: str) -> str:
    scope = secrets.token_urlsafe(24)
    with _lock:
        _scopes[scope] = _ScopeBinding(_BACKEND_INSTANCE, home, me, _now())
        while len(_scopes) > _MAX_SCOPES:
            _scopes.popitem(last=False)
    return scope


def _store_ticket(ticket: Ticket) -> str:
    token = secrets.token_urlsafe(24)
    with _lock:
        _tickets[token] = ticket
        while len(_tickets) > _MAX_TICKETS:
            _tickets.popitem(last=False)
    return token


def _consume_ticket(token: str, scope: str) -> Ticket:
    # A scope-mismatched token is not consumed: it stays valid for its own
    # scope. Only an expired or successfully used ticket is removed.
    with _lock:
        ticket = _tickets.get(token)
        if ticket is None or ticket.scope != scope:
            raise HTTPException(status_code=409, detail="Confirmation expired or missing; review the action again.")
        if ticket.expires < _now():
            _tickets.pop(token, None)
            raise HTTPException(status_code=409, detail="Confirmation expired; review the action again.")
        _tickets.pop(token, None)
    return ticket


def _provider_context(binding: _ScopeBinding | None = None) -> tuple[str, str]:
    """Return (me, home) after verifying the authorized Telethon session."""

    home = _current_home()

    async def _probe(client: Any) -> str:
        me = await client.get_me()
        if me is None:
            raise _AuthUnavailable("no Telegram account")
        name = _peer_display_name(me)
        if not name:
            raise ValueError("invalid account")
        return name

    try:
        me = _run_async(_with_client(_probe))
    except _AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Telegram backend unavailable: {exc}") from None
    except Exception:
        raise _provider_error() from None
    if binding and (binding.backend != _BACKEND_INSTANCE or binding.home != home or binding.me != me):
        raise HTTPException(status_code=409, detail="Telegram connection changed; refresh status.")
    return me, home


def _message_snapshot(message: dict[str, Any]) -> dict[str, Any]:
    return dict(sorted(message.items()))


def _resolve_peer(client: Any, peer: str) -> Any:
    """Resolve a peer reference string to a Telethon entity, bounded and exact."""

    value = peer.strip()
    if value.startswith("id:"):
        raw = value[3:]
        if not re.fullmatch(r"-?\d{1,15}", raw):
            raise ValueError("invalid peer id")
        return client.get_entity(int(raw))
    if value.startswith("@"):
        username = value[1:]
        if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", username):
            raise ValueError("invalid username")
        return client.get_entity(f"@{username}")
    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        match = _TOPIC_ROOT_RE.match(value)
        if match:
            return client.get_entity(int(f"-100{match.group(1)}"))
        username = value.rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9_]{4,64}", username):
            return client.get_entity(f"@{username}")
        raise ValueError("unsupported t.me link")
    raise ValueError("peer must be @username, id:N, or a t.me link")


def _peer_out(client: Any, peer_ref: str) -> tuple[Any, str]:
    try:
        entity = _resolve_peer(client, peer_ref)
    except Exception:
        raise _peer_ref_error() from None
    if entity is None:
        raise _peer_ref_error()
    name = _peer_display_name(entity)
    if not name:
        raise _peer_ref_error()
    return entity, name


# --- Read routes --------------------------------------------------------------


@router.get("/status")
def status(request: Request) -> dict[str, str]:
    _reject_query_extras(request, set())
    me, home = _provider_context()
    with _lock:
        scope = next(
            (key for key, value in _scopes.items() if value.home == home and value.me == me),
            None,
        )
        if scope is None:
            scope = _new_scope(home, me)
    return {"scope": scope, "me": me}


@router.get("/dialogs")
def dialogs(request: Request, scope: ScopeText, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> dict[str, Any]:
    _reject_query_extras(request, {"scope", "limit"})
    binding = _binding(scope)
    me, _ = _provider_context(binding)

    async def _collect(client: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            unread = int(getattr(dialog, "unread_count", 0) or 0)
            result.append({
                "key": _peer_key(entity),
                "name": _peer_display_name(entity),
                "unread": unread,
                "kind": entity.__class__.__name__,
                "lastMessageDate": dialog.date.isoformat() if getattr(dialog, "date", None) else "",
            })
        return result

    try:
        items = _run_async(_with_client(_collect))
    except _AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Telegram backend unavailable: {exc}") from None
    except Exception:
        raise _provider_error() from None
    return {"dialogs": items[:limit], "me": me}


@router.get("/messages")
def messages(
    request: Request,
    scope: ScopeText,
    peer: Annotated[str, Query(max_length=256)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    topicId: Annotated[int, Query(ge=1, le=10_000_000_000)] = 0,
) -> dict[str, Any]:
    _reject_query_extras(request, {"scope", "peer", "limit", "topicId"})
    binding = _binding(scope)
    me, _ = _provider_context(binding)

    async def _collect(client: Any) -> list[dict[str, Any]]:
        entity, _ = _peer_out(client, peer)
        kwargs: dict[str, Any] = {"limit": limit}
        if topicId:
            kwargs["reply_to"] = topicId
        me_entity = await client.get_me()
        own_id = int(getattr(me_entity, "id", 0) or 0)
        found: list[dict[str, Any]] = []
        async for message in client.iter_messages(entity, **kwargs):
            found.append(_public_message(message, own_id or 0))
        return found

    try:
        items = _run_async(_with_client(_collect))
    except _AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Telegram backend unavailable: {exc}") from None
    except HTTPException:
        raise
    except Exception:
        raise _provider_error() from None
    return {"messages": items, "me": me}


def _me_id(client: Any) -> int:
    me = client.get_me(input_peer=True)
    return int(getattr(me, "user_id", 0) or 0)


# --- Prepare / commit ---------------------------------------------------------


@router.post("/actions/prepare")
def prepare_action(request: Request, body: PrepareRequest) -> dict[str, Any]:
    _reject_query_extras(request, set())
    binding = _binding(body.scope)
    me, _ = _provider_context(binding)
    expires = _now() + _TICKET_TTL_SECONDS

    async def _inspect(client: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if body.action in {"send", "reply", "delete", "mark-read"}:
            entity, name = _peer_out(client, body.peer)
        if body.action == "send":
            preview = {
                "action": "send",
                "me": me,
                "peer": name,
                "peerKey": _peer_key(entity),
                "message": body.message,
            }
            return preview, None
        if body.action == "reply":
            target = await client.get_messages(entity, ids=body.messageId)
            if target is None:
                raise _peer_ref_error("Reply target message no longer exists.")
            snapshot = _public_message(target, _me_id(client))
            preview = {
                "action": "reply",
                "me": me,
                "peer": name,
                "peerKey": _peer_key(entity),
                "targetMessage": snapshot,
                "message": body.message,
            }
            return preview, snapshot
        if body.action == "mark-read":
            unread_now = await _unread_count_for(client, entity)
            preview = {
                "action": "mark-read",
                "me": me,
                "peer": name,
                "peerKey": _peer_key(entity),
                "maxId": body.maxId,
                "unreadNow": unread_now,
            }
            return preview, None
        # delete
        target = await client.get_messages(entity, ids=body.messageId)
        if target is None:
            raise _peer_ref_error("Message to delete no longer exists.")
        snapshot = _public_message(target, _me_id(client))
        preview = {
            "action": "delete",
            "me": me,
            "peer": name,
            "peerKey": _peer_key(entity),
            "targetMessage": snapshot,
        }
        return preview, snapshot

    try:
        preview, snapshot = _run_async(_with_client(_inspect))
    except _AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Telegram backend unavailable: {exc}") from None
    except HTTPException:
        raise
    except Exception:
        raise _provider_error() from None

    ticket = Ticket(body.scope, body.action, me, body.model_dump(), preview, snapshot, expires)
    token = _store_ticket(ticket)
    return {"scope": body.scope, "confirmationToken": token, "expiresAt": expires, "preview": preview}


async def _unread_count_for(client: Any, entity: Any) -> int:
    for dialog in await client.get_dialogs(limit=200):
        if dialog.id == entity.id:
            return int(getattr(dialog, "unread_count", 0) or 0)
    return 0


@router.post("/actions/commit")
def commit_action(request: Request, body: CommitRequest) -> dict[str, Any]:
    _reject_query_extras(request, set())
    binding = _binding(body.scope)
    ticket = _consume_ticket(body.confirmationToken, body.scope)
    # Account/home verification occurs after consumption and before any effect.
    me, _ = _provider_context(binding)
    if ticket.me != me:
        raise HTTPException(status_code=409, detail="Telegram connection changed; refresh status.")

    payload = ticket.payload
    action = ticket.action

    async def _mutate(client: Any) -> dict[str, Any]:
        entity, name = _peer_out(client, payload["peer"])
        if action == "send":
            sent = await client.send_message(entity, payload["message"])
            return {"sentId": int(sent.id), "peer": name}
        if action == "reply":
            sent = await client.send_message(
                entity,
                payload["message"],
                reply_to=payload["messageId"],
            )
            return {"sentId": int(sent.id), "peer": name}
        if action == "mark-read":
            await client.send_read_acknowledge(entity, max_id=payload["maxId"])
            return {"peer": name, "maxId": payload["maxId"]}
        # delete
        target = await client.get_messages(entity, ids=payload["messageId"])
        if target is None:
            raise HTTPException(status_code=409, detail="Message already deleted; nothing to do.")
        await client.delete_messages(entity, payload["messageId"])
        return {"deletedId": payload["messageId"], "peer": name}

    try:
        outcome = _run_async(_with_client(_mutate))
    except _AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Telegram backend unavailable: {exc}") from None
    except HTTPException:
        raise
    except Exception:
        raise _provider_error(mutation_started=True) from None

    # Read back the exact resulting state; the RPC return is not proof.
    async def _verify(client: Any) -> dict[str, Any]:
        entity, name = _peer_out(client, payload["peer"])
        if action in {"send", "reply"}:
            message = await client.get_messages(entity, ids=outcome["sentId"])
            if message is None or (message.message or "") != payload["message"]:
                raise ValueError("sent message mismatch")
            if action == "reply" and message.reply_to and int(message.reply_to.reply_to_msg_id) != payload["messageId"]:
                raise ValueError("reply linkage mismatch")
            return {
                "status": "verified",
                "id": int(message.id),
                "peer": name,
                "text": (message.message or "")[:200],
            }
        if action == "mark-read":
            unread_after = await _unread_count_for(client, entity)
            if unread_after != 0:
                raise ValueError("read state mismatch")
            return {"status": "verified", "peer": name, "unread": unread_after}
        # delete
        message = await client.get_messages(entity, ids=payload["messageId"])
        if message is not None:
            raise ValueError("delete readback mismatch")
        return {"status": "verified", "deletedId": payload["messageId"], "peer": name}

    try:
        result = _run_async(_with_client(_verify))
    except Exception:
        raise _provider_error(mutation_started=True) from None
    return result
