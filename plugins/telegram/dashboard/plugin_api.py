"""Staged, profile-scoped Telegram API for the Hermes desktop plugin.

Mirrors the reviewed prepare/commit pattern of the Hermes Gmail desktop
plugin: every mutation requires an explicit preview ticket, an explicit
confirmed commit, and a readback verification of the exact resulting state.
The Telethon user session is resolved at request time from the active
profile's Hermes home; this module never copies or persists credentials.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

router = APIRouter()

# Structured, no-secrets debug log. The Hermes agent captures stderr, so this
# is the single place to see what the backend actually did on each mutation —
# peer, message id, commit/verify outcomes, timings, and the exact exception
# that gets wrapped into a 502. Log at DEBUG; the handler is attached once at
# import time (idempotent) so it does not depend on host log config.
logger = logging.getLogger("hermes.telegram_desktop")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S",
    ))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

# One shared TelegramClient per dedicated profile session; each instance is
# serialized by its own owner loop and guarded by the on-disk process lock.
_TELEGRAM_MANAGERS: dict[str, Any] = {}
_TELEGRAM_MANAGER_LOCK = threading.Lock()


def _telegram_manager() -> Any:
    session_path = _dedicated_session_path().expanduser().resolve(strict=False)
    key = str(session_path)
    with _TELEGRAM_MANAGER_LOCK:
        manager = _TELEGRAM_MANAGERS.get(key)
        if manager is None:
            module_name = "hermes_dashboard_plugin_telegram_client"
            client_module = sys.modules.get(module_name)
            if client_module is None:
                client_path = Path(__file__).resolve().parent / "telegram_client.py"
                spec = importlib.util.spec_from_file_location(module_name, client_path)
                if spec is None or spec.loader is None:
                    raise RuntimeError("cannot locate telegram_client.py")
                client_module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = client_module
                spec.loader.exec_module(client_module)
            credentials = _load_telegram_credentials()
            manager = client_module.TelegramClientManager(session_path, lambda: credentials)
            _TELEGRAM_MANAGERS[key] = manager
        return manager


def _shutdown_managers() -> None:
    with _TELEGRAM_MANAGER_LOCK:
        managers = list(_TELEGRAM_MANAGERS.values())
        _TELEGRAM_MANAGERS.clear()
    for manager in managers:
        try:
            manager.close()
        except Exception:
            continue


def _mount_auth_router() -> None:
    module_name = "hermes_dashboard_plugin_telegram_auth"
    existing = sys.modules.get(module_name)
    if existing is None:
        auth_path = Path(__file__).resolve().parent / "plugin_auth.py"
        spec = importlib.util.spec_from_file_location(module_name, auth_path)
        if spec is None or spec.loader is None:
            return
        existing = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = existing
        try:
            spec.loader.exec_module(existing)
        except Exception:
            sys.modules.pop(spec.name, None)
            return
    router.include_router(existing.router)


_mount_auth_router()

_TELETHON_TIMEOUT_SECONDS = 20.0
_DIALOG_SNAPSHOT_LIMIT = 500
# Snapshot cache: manual refresh bypasses it (refresh=1), but folder/tab
# switches and auto-refresh polls (30/60 s) reuse it. A 60 s TTL covers a full
# 30 s poll cycle (and most of the 60 s one), so the poll never pays the
# full Telegram RTT cost of a fresh dialog snapshot — only a forced refresh
# does. ponytail: stale-unread up to 60 s off-manual-refresh; add per-folder
# TTLs if stale badges start to matter.
_DIALOG_CACHE_TTL_SECONDS = 60.0
_TICKET_TTL_SECONDS = 300.0
_MAX_TICKETS = 256
_MAX_SCOPES = 128
_MAX_PROVIDER_TEXT = 16 * 1024
# Media previews are bounded per page by payload size, not by image count:
# every image post in the visible window carries its picture while the
# budget lasts, and the 64-entry cache (~15 MB) keeps repeat polls cheap.
# ~1.5 MB of base64 per page keeps the /messages JSON payload bounded.
_MAX_MEDIA_PREVIEW_PAGE_BYTES = 1_500_000
# Forum topic scan: GetForumTopicsRequest pages at 100 at a time; bound the
# total so a pathological forum can't stall the dialogs snapshot.
_FORUM_TOPIC_PAGE = 100
_FORUM_TOPIC_SCAN_MAX = 1000
# Raw photo bytes may be up to 160 KB (a full-size JPEG fits the budget); the
# base64 data URI bound keeps the JSON payload bounded at ~220 KB.
_MAX_MEDIA_PREVIEW_BYTES = 160 * 1024
_MAX_MEDIA_PREVIEW_DATA_URI_LENGTH = 240_000
# Media previews are cached per (backend, scope, message id): a refresh cycle
# re-downloads only images the backend has never fetched before. TTL is
# generous — photos don't change — and 64 entries bounds the cache at ~15 MB
# (each entry is at most _MAX_MEDIA_PREVIEW_DATA_URI_LENGTH).
_MEDIA_PREVIEW_CACHE_TTL_SECONDS = 300.0
_MEDIA_PREVIEW_CACHE_MAX = 64
_media_previews: "OrderedDict[tuple[str, str, int], tuple[float, str]]" = OrderedDict()
_media_preview_cache_lock = threading.Lock()


def _media_preview_get(scope: str, message_id: int) -> str | None:
    key = (_BACKEND_INSTANCE, scope, message_id)
    now = _now()
    with _media_preview_cache_lock:
        entry = _media_previews.get(key)
        if entry is None:
            return None
        if now - entry[0] > _MEDIA_PREVIEW_CACHE_TTL_SECONDS:
            _media_previews.pop(key, None)
            return None
        _media_previews.move_to_end(key)
        return entry[1]


def _media_preview_put(scope: str, message_id: int, data_uri: str) -> None:
    key = (_BACKEND_INSTANCE, scope, message_id)
    with _media_preview_cache_lock:
        _media_previews[key] = (_now(), data_uri)
        _media_previews.move_to_end(key)
        while len(_media_previews) > _MEDIA_PREVIEW_CACHE_MAX:
            _media_previews.popitem(last=False)
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
    topicId: int = Field(default=0, ge=0, le=10_000_000_000)


class DeletePrepare(_StrictModel):
    scope: ScopeText
    action: Literal["delete"]
    peer: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    messageId: int = Field(ge=1, le=10_000_000_000)


class SavePrepare(_StrictModel):
    scope: ScopeText
    action: Literal["save"]
    peer: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    messageId: int = Field(ge=1, le=10_000_000_000)


PrepareRequest = Annotated[
    SendPrepare | ReplyPrepare | MarkReadPrepare | DeletePrepare | SavePrepare,
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
            # Emit a real anchor carrying the safe href; the link text flows
            # through handle_data and is rendered exactly once (no bold wrap,
            # no parenthesized duplicate URL).
            href = next((value for name, value in attrs if name and name.lower() == "href"), None)
            if href and self._SAFE_URL.match(href) and len(href) <= 2048:
                self.out.append(f'<a href="{escape(href, quote=True)}">')
                self.open_tags.append("a")
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
            if "a" in self.open_tags:
                # Close the most recent open anchor only.
                self.open_tags.pop(len(self.open_tags) - 1 - self.open_tags[::-1].index("a"))
                self.out.append("</a>")
            return
        if tag in {"code", "pre"} and tag in self.open_tags:
            self.open_tags.remove(tag)
            self.out.append(f"</{tag}>")
        elif tag in {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span"}:
            if self.open_tags and self.open_tags[-1] in {"b", "i", "u", "s", "span"}:
                closing = self.open_tags.pop()
                self.out.append(f"</{closing}>")

    def handle_data(self, data: str) -> None:
        self.out.append(escape(data))

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


def _load_telegram_credentials() -> tuple[int, str]:
    """Resolve API credentials without requiring or reading an RSS session path."""

    path = Path(_current_home()) / "rss_reader" / "config.json"
    if not path.is_file():
        raise _AuthUnavailable("missing Telegram API configuration")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        raise _AuthUnavailable("invalid Telegram API configuration") from exc
    if not isinstance(config, dict):
        raise _AuthUnavailable("invalid Telegram API configuration")
    api_id = config.get("api_id")
    api_hash = config.get("api_hash")
    if not isinstance(api_id, int) or api_id <= 0 or not isinstance(api_hash, str) or not api_hash:
        raise _AuthUnavailable("incomplete Telegram API configuration")
    return api_id, api_hash


def _dedicated_session_path() -> Path:
    """Return the one session path shared by auth and all plugin API calls."""

    from hermes_constants import get_hermes_home

    directory = Path(get_hermes_home()).expanduser() / "telegram-plugin-auth"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory / "telegram-plugin-auth"


async def _with_client(handler: Callable[[Any], Any]) -> Any:
    client = _telegram_manager().client
    if client is None:
        raise RuntimeError("Telegram client manager is not initialized")
    if not await client.is_user_authorized():
        raise _AuthUnavailable("Telegram session is not authorized")
    return await handler(client)


def _run_async(coroutine: Any) -> Any:
    return _telegram_manager().run(coroutine)


def _media_preview_data_uri(data: Any) -> str | None:
    """Return a bounded data URI only for common raster image formats.

    When the raw bytes already fit the budget they pass through untouched.
    Oversized photos are re-encoded (downscaled to at most ~1600 px, JPEG at
    a bounded quality) so the preview keeps real resolution instead of being
    dropped. GIFs are never re-encoded to preserve animation.
    """
    if not isinstance(data, bytes) or not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        media_type = "image/gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        media_type = "image/webp"
    else:
        return None
    # Fast path: raw bytes already fit the budget.
    if len(data) <= _MAX_MEDIA_PREVIEW_BYTES:
        encoded = base64.b64encode(data).decode("ascii")
        preview = f"data:{media_type};base64,{encoded}"
        return preview if len(preview) <= _MAX_MEDIA_PREVIEW_DATA_URI_LENGTH else None
    # Slow path: downscale + re-encode an oversized photo to fit the budget.
    if media_type == "image/gif":
        return None
    reencoded = _reencode_preview(data)
    if reencoded is None:
        return None
    out_type, out_bytes = reencoded
    encoded = base64.b64encode(out_bytes).decode("ascii")
    preview = f"data:{out_type};base64,{encoded}"
    return preview if len(preview) <= _MAX_MEDIA_PREVIEW_DATA_URI_LENGTH else None


def _reencode_preview(data: bytes) -> tuple[str, bytes] | None:
    """Downscale/re-encode an oversized raster to at most ~1600 px on a side.

    Returns (media_type, bytes) or None. Lossy formats (JPEG/WebP) are
    re-encoded as JPEG with bounded quality; animated/lossless content that
    cannot be re-encoded cheaply returns None (the caller drops the preview).
    """
    try:
        import io
        from PIL import Image, ImageOps
    except Exception:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        return None
    try:
        image = ImageOps.exif_transpose(image)
        if image.mode in ("P", "PA"):
            image = image.convert("RGBA")
        if image.mode in ("RGB", "RGBA"):
            width, height = image.size
            longest = max(width, height)
            if longest > 1600:
                scale = 1600 / float(longest)
                image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
            out = io.BytesIO()
            if image.mode == "RGBA":
                image = image.convert("RGB")
            quality = 88
            while quality >= 55:
                out.seek(0)
                out.truncate(0)
                image.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
                if out.tell() <= _MAX_MEDIA_PREVIEW_BYTES:
                    return "image/jpeg", out.getvalue()
                quality -= 12
            return "image/jpeg", out.getvalue()
    except Exception:
        return None
    return None


def _message_has_image_media(message: Any) -> bool:
    if getattr(message, "photo", None) is not None:
        return True
    document = getattr(message, "document", None)
    mime_type = getattr(document, "mime_type", "") if document is not None else ""
    return mime_type in {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _message_is_general_topic(message: Any) -> bool:
    reply = getattr(message, "reply_to", None)
    topic_root_id = getattr(reply, "reply_to_top_id", None)
    if topic_root_id and int(topic_root_id) != 1:
        return False
    action = getattr(message, "action", None)
    if action is not None:
        from telethon.tl.types import MessageActionTopicCreate

        if isinstance(action, MessageActionTopicCreate):
            return False
    return True


def _message_belongs_to_forum_topic(message: Any, topic_id: int) -> bool:
    if int(getattr(message, "id", 0) or 0) == topic_id:
        return True
    reply = getattr(message, "reply_to", None)
    top_id = getattr(reply, "reply_to_top_id", None)
    if top_id:
        return int(top_id) == topic_id
    return int(getattr(reply, "reply_to_msg_id", 0) or 0) == topic_id


def _public_message(message: Any, me_id: int) -> dict[str, Any]:
    sender = message.sender
    sender_name = ""
    if sender is not None:
        first = getattr(sender, "first_name", None) or ""
        last = getattr(sender, "last_name", None) or ""
        title = getattr(sender, "title", None) or ""
        sender_name = title or f"{first} {last}".strip() or getattr(sender, "username", "") or ""
    text = message.message or ""
    display_text = text[:4096]
    try:
        from telethon.extensions.html import unparse

        formatted_text = unparse(display_text, getattr(message, "entities", None) or [])
    except Exception:
        formatted_text = escape(display_text)
    html_preview = sanitize_message_html(formatted_text)
    media_kind = ""
    if message.media:
        media_kind = message.media.__class__.__name__
    return {
        "id": int(message.id),
        "peer": _peer_display_name(message.chat if message.chat is not None else message.sender),
        "date": message.date.isoformat() if message.date else "",
        "out": bool(message.out),
        "sender": sender_name,
        "text": display_text,
        "htmlPreview": html_preview,
        "media": media_kind,
        "replyTo": int(message.reply_to.reply_to_msg_id) if message.reply_to else None,
        "replyToTop": int(message.reply_to.reply_to_top_id) if getattr(message.reply_to, "reply_to_top_id", None) else None,
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
    try:
        from telethon import utils as tg_utils

        # Marked id (channels get -100 prefix) so get_entity(int) can resolve
        # it back from the session cache.
        return f"id:{tg_utils.get_peer_id(entity)}"
    except Exception:
        identifier = getattr(entity, "id", None)
        return f"id:{identifier}" if identifier is not None else ""


def _peer_identity(entity: Any) -> int | None:
    if entity is None:
        return None
    try:
        from telethon import utils as tg_utils

        return int(tg_utils.get_peer_id(entity))
    except Exception:
        identifier = getattr(entity, "id", None)
        return int(identifier) if isinstance(identifier, int) else None


def _dialog_is_muted(dialog: Any, now: float | None = None) -> bool:
    wrapped_dialog = getattr(dialog, "dialog", dialog)
    settings = getattr(wrapped_dialog, "notify_settings", None)
    mute_until = getattr(settings, "mute_until", None) if settings is not None else None
    if not mute_until:
        return False
    if isinstance(mute_until, datetime):
        timestamp = mute_until.replace(tzinfo=mute_until.tzinfo or timezone.utc).timestamp()
    else:
        try:
            timestamp = float(mute_until)
        except (TypeError, ValueError):
            return False
    return timestamp > (time.time() if now is None else now)


def _dialog_is_group(entity: Any) -> bool:
    name = type(entity).__name__
    return name == "Chat" or bool(getattr(entity, "megagroup", False))


def _filter_membership(dialog: Any, definition: Any) -> tuple[bool, bool]:
    """Return (matches, pinned) for standard Telegram smart/chatlist filters."""

    entity = getattr(dialog, "entity", None)
    identity = _peer_identity(entity)
    includes = list(getattr(definition, "include_peers", None) or [])
    excludes = list(getattr(definition, "exclude_peers", None) or [])
    pinned = list(getattr(definition, "pinned_peers", None) or [])
    include_ids = {peer_id for peer_id in (_peer_identity(peer) for peer in includes) if peer_id is not None}
    exclude_ids = {peer_id for peer_id in (_peer_identity(peer) for peer in excludes) if peer_id is not None}
    pinned_ids = {peer_id for peer_id in (_peer_identity(peer) for peer in pinned) if peer_id is not None}
    if identity is None or identity in exclude_ids:
        return False, False

    is_pinned = identity in pinned_ids
    if type(definition).__name__ == "DialogFilterChatlist":
        matches = identity in include_ids or is_pinned
    else:
        entity_is_user = type(entity).__name__ == "User" or hasattr(entity, "first_name")
        category_match = False
        if entity_is_user:
            is_bot = bool(getattr(entity, "bot", False))
            is_contact = bool(getattr(entity, "contact", False) or getattr(entity, "mutual_contact", False))
            category_match = (
                (bool(getattr(definition, "bots", False)) and is_bot)
                or (bool(getattr(definition, "contacts", False)) and is_contact and not is_bot)
                or (bool(getattr(definition, "non_contacts", False)) and not is_contact and not is_bot)
            )
        elif _dialog_is_group(entity):
            category_match = bool(getattr(definition, "groups", False))
        elif bool(getattr(entity, "broadcast", False)):
            category_match = bool(getattr(definition, "broadcasts", False))

        matches = identity in include_ids or is_pinned or category_match

    if not matches:
        return False, is_pinned
    if bool(getattr(definition, "exclude_muted", False)) and _dialog_is_muted(dialog):
        return False, is_pinned
    if bool(getattr(definition, "exclude_read", False)) and int(getattr(dialog, "unread_count", 0) or 0) <= 0:
        return False, is_pinned
    folder_id = getattr(dialog, "folder_id", None)
    if bool(getattr(definition, "exclude_archived", False)) and folder_id == 1:
        return False, is_pinned
    return True, is_pinned


def _apply_dialog_filters(dialogs: list[Any], telegram_filters: list[Any], unread_topics: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach backend-computed memberships to dialogs and return stable tabs.

    ``unread_topics`` maps a dialog key to the number of its forum topics that
    currently have unread messages; it is only present for forum dialogs.
    """

    unread_topics = unread_topics or {}

    folders: list[dict[str, Any]] = [{"id": "all", "title": "All", "kind": "all"}]
    custom: list[tuple[str, Any]] = []
    for definition in telegram_filters:
        name = type(definition).__name__
        if name == "DialogFilterDefault":
            continue
        ident = getattr(definition, "id", None)
        title_obj = getattr(definition, "title", None)
        title = getattr(title_obj, "text", None) or ""
        if not isinstance(ident, int) or not title:
            continue
        kind = "chatlist" if name == "DialogFilterChatlist" else "telegram"
        folders.append({"id": str(ident), "title": str(title), "kind": kind})
        custom.append((str(ident), definition))

    folders.append({"id": "archive", "title": "Archive", "kind": "archive"})

    rows: list[dict[str, Any]] = []
    for dialog in dialogs:
        entity = getattr(dialog, "entity", None)
        unread = int(getattr(dialog, "unread_count", 0) or 0)
        dialog_key = _peer_key(entity)
        raw_folder = getattr(dialog, "folder_id", None)
        archived = raw_folder == 1
        folder_ids = ["archive" if archived else "all"]
        folder_pins: list[str] = []
        for ident, definition in custom:
            matches, pinned = _filter_membership(dialog, definition)
            if matches:
                folder_ids.append(ident)
                if pinned:
                    folder_pins.append(ident)
        is_forum = bool(getattr(entity, "forum", False))
        unread_topics_value = int(unread_topics.get(dialog_key, 0) or 0)
        logger.info(
            "dialog row key=%s name=%s unread=%d unreadTopics=%d isForum=%s topMessageId=%s",
            dialog_key, _peer_display_name(entity), unread, unread_topics_value,
            is_forum, int(getattr(getattr(dialog, "message", None), "id", 0) or 0),
        )
        rows.append({
            "key": dialog_key,
            "name": _peer_display_name(entity),
            "unread": unread,
            # For a forum the badge should show the number of topics that have
            # new messages, not the raw message total; for a plain dialog it is
            # absent and the UI falls back to the message count.
            "unreadTopics": unread_topics_value,
            "muted": _dialog_is_muted(dialog),
            "folder": int(raw_folder) if raw_folder is not None else 0,
            "folderIds": folder_ids,
            "folderPins": folder_pins,
            "kind": entity.__class__.__name__ if entity is not None else "",
            "webUsername": str(getattr(entity, "username", "") or "") if entity is not None else "",
            "isForum": is_forum,
            "topMessageId": int(getattr(getattr(dialog, "message", None), "id", 0) or 0),
            "lastMessageDate": dialog.date.isoformat() if getattr(dialog, "date", None) else "",
        })
    return folders, rows


def _dialog_page(
    data: dict[str, Any], requested_folder: str, limit: int,
    unread_only: bool = False, unmuted_only: bool = False,
) -> dict[str, Any]:
    folders = list(data.get("folders") or [])
    valid_ids = {str(folder.get("id")) for folder in folders if isinstance(folder, dict)}
    selected = requested_folder if requested_folder in valid_ids else "all"
    rows: list[dict[str, Any]] = []
    for dialog in data.get("dialogs") or []:
        if not isinstance(dialog, dict):
            continue
        if unread_only and int(dialog.get("unread", 0) or 0) <= 0:
            continue
        if unmuted_only and dialog.get("muted") is True:
            continue
        folder_ids = dialog.get("folderIds")
        if isinstance(folder_ids, list) and selected in folder_ids:
            rows.append(dialog)
    rows.sort(key=lambda dialog: not (
        isinstance(dialog.get("folderPins"), list) and selected in dialog["folderPins"]
    ))
    return {"dialogs": rows[:limit], "folders": folders, "activeFolder": selected}


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
    """Return (me, home) after verifying the authorized Telethon session.

    The account name is served from a short-lived TTL cache keyed on the live
    client (see ``_me_name``), so the ~6 get_me() calls a single action makes
    across endpoints collapse to one round-trip.
    """

    home = _current_home()

    async def _probe(client: Any) -> str:
        name, _ = await _me_name(client, home)
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


async def _resolve_peer(client: Any, peer: str) -> Any:
    """Resolve a peer reference string to a Telethon entity, bounded and exact."""

    value = peer.strip()
    if value.startswith("id:"):
        raw = value[3:]
        if not re.fullmatch(r"-?\d{1,15}", raw):
            raise ValueError("invalid peer id")
        return await client.get_entity(int(raw))
    if value.startswith("@"):
        username = value[1:]
        if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", username):
            raise ValueError("invalid username")
        return await client.get_entity(f"@{username}")
    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        match = _TOPIC_ROOT_RE.match(value)
        if match:
            return await client.get_entity(int(f"-100{match.group(1)}"))
        username = value.rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9_]{4,64}", username):
            return await client.get_entity(f"@{username}")
        raise ValueError("unsupported t.me link")
    raise ValueError("peer must be @username, id:N, or a t.me link")


async def _peer_out(client: Any, peer_ref: str) -> tuple[Any, str]:
    try:
        entity = await _resolve_peer(client, peer_ref)
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
def dialogs(
    request: Request,
    scope: ScopeText,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    folder: Annotated[str, Query(max_length=64)] = "all",
    unreadOnly: bool = False,
    unmutedOnly: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    _reject_query_extras(request, {"scope", "limit", "folder", "unreadOnly", "unmutedOnly", "refresh"})
    binding = _binding(scope)
    me, _ = _provider_context(binding)

    # Folder/tab switches reuse the snapshot for a few seconds: switching tabs
    # must be instant like the native client; refresh=1 bypasses the cache.
    cached = None if refresh else _dialog_cache_get(scope)
    if cached is None:
        async def _collect(client: Any) -> dict[str, Any]:
            from telethon import functions
            from telethon.tl.types import DialogFilter, DialogFilterChatlist

            # Bounded snapshot: a full unbounded iter_dialogs over hundreds of
            # chats dominated request latency; the native client also works from
            # a bounded, cached dialog list. Folders beyond the snapshot window
            # are acceptable for a read pane.
            result: list[Any] = []
            async for dialog in client.iter_dialogs(limit=_DIALOG_SNAPSHOT_LIMIT):
                result.append(dialog)
            # For forum dialogs, count the topics that actually have unread
            # messages so the badge matches the native client (topic count,
            # not the raw message total). Only real forums pay this cost — a
            # non-forum megagroup would make the scan pointless.
            unread_topics: dict[str, int] = {}
            forum_count = 0
            for dialog in result:
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                is_forum = getattr(entity, "forum", False)
                if not is_forum:
                    continue
                forum_count += 1
                logger.info(
                    "forum scan key=%s entityId=%s title=%s",
                    _peer_key(entity), entity.id, getattr(entity, "title", "?"),
                )
                try:
                    topic_count = await _unread_topics_count(client, entity)
                    unread_topics[_peer_key(entity)] = topic_count
                    logger.info("forum scan ok topicsWithUnread=%s key=%s", topic_count, _peer_key(entity))
                except Exception:
                    logger.exception("forum scan failed key=%s (falling back to raw unread)", _peer_key(entity))
            logger.info(
                "forum scan summary forumDialogs=%d keysBuilt=%d snapshotSize=%d refresh=%s",
                forum_count, len(unread_topics), len(result), refresh,
            )
            response = await client(functions.messages.GetDialogFiltersRequest())
            telegram_filters = [
                definition for definition in (getattr(response, "filters", None) or [])
                if isinstance(definition, (DialogFilter, DialogFilterChatlist))
            ]
            folders, rows = _apply_dialog_filters(result, telegram_filters, unread_topics)
            return {"dialogs": rows, "folders": folders, "fetchedAt": time.time()}

        try:
            data = _run_async(_with_client(_collect))
        except _AuthUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"Telegram backend unavailable: {exc}") from None
        except Exception:
            raise _provider_error() from None
        _dialog_cache_put(scope, data)
    else:
        data = cached
    page = _dialog_page(data, folder, limit, unread_only=unreadOnly, unmuted_only=unmutedOnly)
    return {**page, "me": me}


_DIALOG_CACHE_LOCK = threading.Lock()
_DIALOG_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _dialog_cache_get(scope: str) -> dict[str, Any] | None:
    with _DIALOG_CACHE_LOCK:
        entry = _DIALOG_CACHE.get(scope)
        if entry is None:
            return None
        if time.time() - entry["fetchedAt"] > _DIALOG_CACHE_TTL_SECONDS:
            _DIALOG_CACHE.pop(scope, None)
            return None
        _DIALOG_CACHE.move_to_end(scope)
        return entry["data"]


def _dialog_cache_put(scope: str, data: dict[str, Any]) -> None:
    with _DIALOG_CACHE_LOCK:
        _DIALOG_CACHE[scope] = {"fetchedAt": data.get("fetchedAt", time.time()), "data": data}
        while len(_DIALOG_CACHE) > 8:
            _DIALOG_CACHE.popitem(last=False)


@router.get("/topics")
def topics(
    request: Request,
    scope: ScopeText,
    peer: Annotated[str, Query(max_length=256)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """List forum topics for a forum-enabled group/supergroup."""
    _reject_query_extras(request, {"scope", "peer", "limit"})
    binding = _binding(scope)
    me, _ = _provider_context(binding)

    async def _collect(client: Any) -> dict[str, Any]:
        from telethon import functions
        from telethon.tl import types as tl_types

        entity, name = await _peer_out(client, peer)
        if not bool(getattr(entity, "forum", False)):
            return {"peer": name, "isForum": False, "topics": []}
        response = await client(functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=limit,
        ))
        topics: list[dict[str, Any]] = []
        for topic in getattr(response, "topics", None) or []:
            if isinstance(topic, tl_types.ForumTopic):
                topics.append({
                    "id": int(topic.id),
                    "title": str(topic.title),
                    "topMessage": int(topic.top_message),
                    "readInboxMaxId": int(topic.read_inbox_max_id or 0),
                    "unread": int(topic.unread_count or 0),
                    "pinned": bool(topic.pinned),
                    "closed": bool(topic.closed),
                })
            elif isinstance(topic, tl_types.ForumTopicDeleted):
                continue
        topics.sort(key=lambda item: (not item["pinned"], -item["id"]))
        return {"peer": name, "isForum": True, "topics": topics[:limit]}

    try:
        data = _run_async(_with_client(_collect))
    except _AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Telegram backend unavailable: {exc}") from None
    except HTTPException:
        raise
    except Exception:
        raise _provider_error() from None
    return {**data, "me": me}


@router.get("/messages")
def messages(
    request: Request,
    scope: ScopeText,
    peer: Annotated[str, Query(max_length=256)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    topicId: Annotated[int, Query(ge=0, le=10_000_000_000)] = 0,
) -> dict[str, Any]:
    _reject_query_extras(request, {"scope", "peer", "limit", "topicId"})
    binding = _binding(scope)
    me, _ = _provider_context(binding)

    async def _collect(client: Any) -> list[dict[str, Any]]:
        entity, _ = await _peer_out(client, peer)
        cursor = await _message_read_cursor(client, entity, topicId)
        general_topic = topicId == 1
        # ponytail: General scans at most 500 posts to filter sparse forums; paginate if that ceiling proves too small.
        iterator_limit = min(500, max(100, limit * 10)) if general_topic else limit
        kwargs: dict[str, Any] = {"limit": iterator_limit}
        if topicId > 1:
            kwargs["reply_to"] = topicId
        if cursor > 0:
            # Telethon excludes min_id itself, so use cursor-1 to include the
            # last-read post when it is still available.
            kwargs.update(min_id=cursor - 1, reverse=True)
        own_id = await _me_id(client)
        found: list[dict[str, Any]] = []
        preview_bytes = 0
        async for message in client.iter_messages(entity, **kwargs):
            if general_topic and not _message_is_general_topic(message):
                continue
            item = _public_message(message, own_id or 0)
            if _message_has_image_media(message):
                # ponytail: page budget is base64 chars (~1.5 MB), not an image
                # count — every image post in the visible window keeps its
                # picture while the budget lasts. Bump the constant, not the
                # code, if a huge image-dense page proves too heavy.
                message_id = item.get("id")
                cached_preview = _media_preview_get(scope, message_id) if isinstance(message_id, int) else None
                if cached_preview is not None and preview_bytes + len(cached_preview) <= _MAX_MEDIA_PREVIEW_PAGE_BYTES:
                    item["mediaPreview"] = cached_preview
                    preview_bytes += len(cached_preview)
                elif preview_bytes < _MAX_MEDIA_PREVIEW_PAGE_BYTES:
                    try:
                        # -1 = the largest available photo size (native Telegram
                        # uses this for full-size viewing); the old thumb=0
                        # grabbed the smallest ~128 px thumbnail, so images were
                        # blurry in the pane.
                        raw_preview = await client.download_media(message, file=bytes, thumb=-1)
                    except Exception:
                        raw_preview = None
                    preview = _media_preview_data_uri(raw_preview)
                    if preview and preview_bytes + len(preview) <= _MAX_MEDIA_PREVIEW_PAGE_BYTES:
                        item["mediaPreview"] = preview
                        preview_bytes += len(preview)
                        if isinstance(message_id, int):
                            _media_preview_put(scope, message_id, preview)
            found.append(item)
            if len(found) >= limit:
                break
        # With no cursor, Telethon's default newest-first window is reversed
        # locally. Cursor mode requested reverse=True and is already ascending.
        if cursor <= 0:
            found.reverse()
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


async def _me_id(client: Any) -> int:
    # get_me() is a network round-trip; the id is stable for the life of the
    # client object (re-login creates a new client), so cache it per client.
    cached = getattr(client, "_cached_me_id", None)
    if cached is not None:
        return int(cached)
    me = await client.get_me()
    cached = int(getattr(me, "id", 0) or 0)
    client._cached_me_id = cached
    return cached


# get_me() is a GetUsers RPC every call — the session name/id never change for
# the life of the client. Cache the display name per (backend instance, home,
# client) with a TTL so a single action's ~6 redundant get_me() calls collapse
# to one. A re-login creates a new client object, so stale entries age out via
# the TTL and the id() key is never reused within a session.
_ME_NAME_TTL_SECONDS = 300.0
_ME_NAME_CACHE_LOCK = threading.Lock()
_ME_NAME_CACHE: "OrderedDict[tuple[str, str, int], tuple[float, str, int]]" = OrderedDict()


async def _me_name(client: Any, home: str) -> tuple[str, int]:
    key = (_BACKEND_INSTANCE, home, id(client))
    with _ME_NAME_CACHE_LOCK:
        entry = _ME_NAME_CACHE.get(key)
        if entry is not None and time.time() - entry[0] <= _ME_NAME_TTL_SECONDS:
            _ME_NAME_CACHE.move_to_end(key)
            return entry[1], entry[2]
    me = await client.get_me()
    if me is None:
        raise _AuthUnavailable("no Telegram account")
    name = _peer_display_name(me)
    if not name:
        raise ValueError("invalid account")
    me_id = int(getattr(me, "id", 0) or 0)
    client._cached_me_id = me_id
    with _ME_NAME_CACHE_LOCK:
        _ME_NAME_CACHE[key] = (time.time(), name, me_id)
        _ME_NAME_CACHE.move_to_end(key)
        while len(_ME_NAME_CACHE) > 32:
            _ME_NAME_CACHE.popitem(last=False)
    return name, me_id


async def _unread_topics_count(client: Any, entity: Any) -> int:
    """Number of forum topics with unread messages.

    Telegram does not expose a per-forum topic-count in the dialogs list (the
    raw Dialog carries only a total message unread_count), so count
    GetForumTopicsRequest results. Bound the scan so a huge forum stays fast;
    the first 1000 topics always dominate the unread signal a human would act
    on, and the result rides on the 20 s dialog snapshot cache.
    """
    from telethon import functions
    from telethon.tl import types as tl_types

    unread_topics = 0
    offset_date, offset_id, offset_topic = None, 0, 0
    scanned = 0
    while scanned < _FORUM_TOPIC_SCAN_MAX:
        response = await client(functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=offset_date, offset_id=offset_id,
            offset_topic=offset_topic, limit=_FORUM_TOPIC_PAGE,
        ))
        topics = list(getattr(response, "topics", None) or [])
        for topic in topics:
            if isinstance(topic, tl_types.ForumTopic) and int(getattr(topic, "unread_count", 0) or 0) > 0:
                unread_topics += 1
        if len(topics) < _FORUM_TOPIC_PAGE:
            break
        last = next((t for t in reversed(topics) if isinstance(t, tl_types.ForumTopic)), None)
        if last is None:
            break
        offset_date = getattr(last, "date", None)
        offset_id = int(getattr(last, "top_message", 0) or 0)
        offset_topic = int(getattr(last, "id", 0) or 0)
        scanned += len(topics)
    return unread_topics


# --- Prepare / commit ---------------------------------------------------------


@router.post("/actions/prepare")
def prepare_action(request: Request, body: PrepareRequest) -> dict[str, Any]:
    _reject_query_extras(request, set())
    binding = _binding(body.scope)
    me, _ = _provider_context(binding)
    expires = _now() + _TICKET_TTL_SECONDS

    async def _inspect(client: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if body.action in {"send", "reply", "delete", "mark-read", "save"}:
            entity, name = await _peer_out(client, body.peer)
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
            snapshot = _public_message(target, await _me_id(client))
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
            if body.topicId == 1:
                raise HTTPException(
                    status_code=409,
                    detail="The General forum topic cannot be marked read separately; return to the forum and choose the chat-wide action.",
                )
            unread_now = await _unread_count_for(client, entity)
            target = await client.get_messages(entity, ids=body.maxId)
            if target is None:
                raise HTTPException(status_code=409, detail="Read target no longer exists; review the action again.")
            if body.topicId:
                await _validate_topic_target(client, entity, body.topicId, target)
            snapshot = _public_message(target, await _me_id(client))
            preview = {
                "action": "mark-read",
                "me": me,
                "peer": name,
                "peerKey": _peer_key(entity),
                "maxId": body.maxId,
                "topicId": body.topicId,
                "unreadNow": unread_now,
                "targetMessage": snapshot,
            }
            return preview, snapshot
        if body.action == "save":
            target = await client.get_messages(entity, ids=body.messageId)
            if target is None:
                raise _peer_ref_error("Message to save no longer exists.")
            snapshot = _public_message(target, await _me_id(client))
            preview = {
                "action": "save",
                "me": me,
                "peer": name,
                "peerKey": _peer_key(entity),
                "targetMessage": snapshot,
            }
            return preview, snapshot
        # delete
        target = await client.get_messages(entity, ids=body.messageId)
        if target is None:
            raise _peer_ref_error("Message to delete no longer exists.")
        snapshot = _public_message(target, await _me_id(client))
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


def _dialog_match(dialog: Any, entity: Any) -> bool:
    """Match a custom Dialog to a resolved entity.

    Telethon's custom Dialog.id is the *marked* peer id
    (``utils.get_peer_id``: users +1e13, groups +2e13, channels -100 prefix),
    which differs from the raw entity id for most supergroups/channels.
    """
    if dialog is None or entity is None:
        return False
    if getattr(dialog, "id", None) == getattr(entity, "id", None):
        return True
    dialog_entity = getattr(dialog, "entity", None)
    return (
        _peer_identity(dialog_entity) == _peer_identity(entity)
        or _peer_identity(getattr(dialog, "dialog", dialog_entity)) == _peer_identity(entity)
    )


def _dialog_read_cursor(dialog: Any) -> int:
    """Inbox read cursor (raw TL Dialog.read_inbox_max_id) for readback."""
    wrapped = getattr(dialog, "dialog", dialog)
    cursor = getattr(wrapped, "read_inbox_max_id", None)
    return int(cursor or 0)


async def _forum_topic_by_id(client: Any, entity: Any, topic_id: int) -> Any:
    from telethon import functions

    response = await client(functions.messages.GetForumTopicsByIDRequest(
        peer=entity, topics=[topic_id],
    ))
    return next(
        (item for item in (getattr(response, "topics", None) or [])
         if int(getattr(item, "id", 0) or 0) == topic_id),
        None,
    )


async def _validate_topic_target(client: Any, entity: Any, topic_id: int, target: Any) -> None:
    if not bool(getattr(entity, "forum", False)) or await _forum_topic_by_id(client, entity, topic_id) is None:
        raise HTTPException(status_code=409, detail="The requested forum topic is unavailable.")
    if not _message_belongs_to_forum_topic(target, topic_id):
        raise HTTPException(status_code=409, detail="The selected message is not in the requested forum topic.")


async def _message_read_cursor(client: Any, entity: Any, topic_id: int = 0) -> int:
    """Read the exact chat/topic cursor; return 0 when no cursor is available."""
    if topic_id:
        topic = await _forum_topic_by_id(client, entity, topic_id)
        return int(getattr(topic, "read_inbox_max_id", 0) or 0)
    dialog = await _find_dialog(client, entity)
    return _dialog_read_cursor(dialog) if dialog is not None else 0


async def _unread_count_for(client: Any, entity: Any) -> int:
    for dialog in await client.get_dialogs(limit=200):
        if _dialog_match(dialog, entity):
            return int(getattr(dialog, "unread_count", 0) or 0)
    return 0


async def _find_dialog(client: Any, entity: Any) -> Any:
    """Locate the custom Dialog for entity, matching by marked peer id."""
    for dialog in await client.get_dialogs(limit=200):
        if _dialog_match(dialog, entity):
            return dialog
    return None


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
        entity, name = await _peer_out(client, payload["peer"])
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
            target = await client.get_messages(entity, ids=payload["maxId"])
            if target is None:
                raise HTTPException(status_code=409, detail="Read target no longer exists; review the action again.")
            topic_id = int(payload.get("topicId") or 0)
            if topic_id == 1:
                raise HTTPException(status_code=409, detail="General forum topic read is not available as a scoped action.")
            if topic_id:
                await _validate_topic_target(client, entity, topic_id, target)
            current = _public_message(target, await _me_id(client))
            if (
                ticket.message_snapshot is None
                or int(ticket.message_snapshot.get("id", 0) or 0) != int(payload["maxId"])
                or _message_snapshot(current) != ticket.message_snapshot
            ):
                raise HTTPException(status_code=409, detail="Read target changed; review the action again.")
            if topic_id:
                from telethon import functions

                await client(functions.messages.ReadDiscussionRequest(
                    peer=entity, msg_id=topic_id, read_max_id=payload["maxId"],
                ))
            else:
                await client.send_read_acknowledge(entity, max_id=payload["maxId"])
            logger.info(
                "mark-read committed peer=%s maxId=%s topicId=%s",
                name, payload["maxId"], topic_id,
            )
            return {"peer": name, "maxId": payload["maxId"]}
        if action == "save":
            # from_peer is required when forwarding by integer IDs: it tells
            # Telethon which chat the message belongs to. Without it, the
            # client raises ValueError before the RPC is sent.
            logger.info("save commit peer=%s messageId=%s", name, payload["messageId"])
            target = await client.get_messages(entity, ids=payload["messageId"])
            if target is None:
                raise _peer_ref_error("Message to save no longer exists.")
            forwarded = await client.forward_messages("me", [payload["messageId"]], from_peer=entity)
            sent = forwarded[0] if isinstance(forwarded, list) else forwarded
            logger.info("save commit ok sentId=%s peer=%s", sent.id, name)
            return {"sentId": int(sent.id), "peer": name}
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
        logger.exception("commit failed action=%s peer=%s", payload.get("action"), payload.get("peer"))
        raise _provider_error(mutation_started=True) from None

    # Read back the exact resulting state; the RPC return is not proof.
    async def _verify(client: Any) -> dict[str, Any]:
        entity, name = await _peer_out(client, payload["peer"])
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
            topic_id = int(payload.get("topicId") or 0)
            max_id = int(payload["maxId"])
            if topic_id:
                topic = await _forum_topic_by_id(client, entity, topic_id)
                if topic is None:
                    raise ValueError("forum topic readback is unavailable")
                unread_after = int(getattr(topic, "unread_count", 0) or 0)
                cursor = int(getattr(topic, "read_inbox_max_id", 0) or 0)
            else:
                # One bounded dialogs fetch serves both the unread count and
                # the read cursor; a second full fetch doubled the latency.
                fresh = await client.get_dialogs(limit=200)
                after_dialog = next((d for d in fresh if _dialog_match(d, entity)), None)
                unread_after = int(getattr(after_dialog, "unread_count", 0) or 0) if after_dialog is not None else 0
                cursor = _dialog_read_cursor(after_dialog) if after_dialog is not None else 0
            logger.info(
                "mark-read readback peer=%s maxId=%s cursor=%s unreadAfter=%s",
                name, max_id, cursor, unread_after,
            )
            # A bounded read must advance the exact chat/topic cursor through
            # the requested post; newer posts may remain unread.
            if cursor < max_id:
                raise ValueError("read cursor did not advance through the requested message")
            result = {"status": "verified", "peer": name, "unread": unread_after, "readCursor": cursor}
            if topic_id:
                result["topicId"] = topic_id
            return result
        if action == "save":
            # The commit already proves the forward landed: ForwardMessages
            # returned a new message id in Saved Messages. The readback must
            # only prove the message is still THERE — it must not re-derive
            # "what was forwarded from" off the read: `forward.id` is the
            # ORIGINAL SENDER's user id from the source message, so comparing
            # it to the source message id can never hold and turned every
            # successful save into a false 502. Keep the header in the log
            # for diagnostics, and fall back to the dialogs readback if the
            # direct fetch comes back empty (MessagesNotModified on a
            # previously-cached entry).
            saved = await client.get_messages("me", ids=outcome["sentId"])
            if saved is None:
                # The message may be cached server-side as not-modified; the
                # dialogs snapshot carries the live top message of each chat.
                me_entity = await client.get_entity(client._self_id)
                dialogs = await client.get_dialogs(limit=200)
                saved_dialog = next((d for d in dialogs if _dialog_match(d, me_entity)), None)
                saved = getattr(saved_dialog, "message", None)
                if saved is not None and int(getattr(saved, "id", 0) or 0) != int(outcome["sentId"]):
                    saved = None
            header = getattr(saved, "fwd_from", None)
            logger.info(
                "save readback sentId=%s found=%s forwardFrom=%s forwardDate=%s",
                outcome["sentId"], saved is not None,
                getattr(header, "from_id", None), getattr(header, "date", None),
            )
            if saved is None:
                raise ValueError("saved message not found on readback")
            return {"status": "verified", "id": int(saved.id), "peer": name}
        # delete
        message = await client.get_messages(entity, ids=payload["messageId"])
        if message is not None:
            raise ValueError("delete readback mismatch")
        return {"status": "verified", "deletedId": payload["messageId"], "peer": name}

    try:
        result = _run_async(_with_client(_verify))
    except Exception:
        logger.exception("verify failed action=%s peer=%s", action, payload.get("peer"))
        raise _provider_error(mutation_started=True) from None
    logger.info("mutated action=%s peer=%s result=%s", action, payload.get("peer"), result)
    if action == "mark-read":
        _DIALOG_CACHE.pop(body.scope, None)
    return result
