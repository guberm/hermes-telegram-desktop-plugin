"""Offline behavior tests for the Telegram dashboard backend.

No network: the Telethon client layer is patched out; these tests exercise
validation, ticket staging, preview construction, and commit readback paths.
"""

from __future__ import annotations

import importlib.util
import asyncio
import json
import random
import sys
import tempfile
import time
import unittest
from base64 import b64decode
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "plugins/telegram/dashboard/plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_telegram"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, BACKEND)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = MODULE
SPEC.loader.exec_module(MODULE)
sys.modules["plugin_api"] = MODULE
AUTH_PATH = ROOT / "plugins/telegram/dashboard/plugin_auth.py"
AUTH_SPEC = importlib.util.spec_from_file_location("plugin_auth", AUTH_PATH)
assert AUTH_SPEC is not None and AUTH_SPEC.loader is not None
AUTH = importlib.util.module_from_spec(AUTH_SPEC)
sys.modules["plugin_auth"] = AUTH
AUTH_SPEC.loader.exec_module(AUTH)


def make_message(mid=101, text="hello world", mine=False, unread=True, reply_to=None):
    msg = SimpleNamespace(
        id=mid,
        message=text,
        date=None,
        out=mine,
        unread=unread,
        sender=SimpleNamespace(first_name="Ada", last_name="Lovelace", username="ada", title=None),
        chat=SimpleNamespace(id=-100123, title="Test Group", first_name=None, last_name=None, username=None),
        media=None,
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None,
        sender_id=999 if not mine else 1,
    )
    return msg


def public(msg):
    return MODULE._public_message(msg, 1)


class SanitizerTests(unittest.TestCase):
    def test_plain_text_is_escaped(self):
        rendered = MODULE.sanitize_message_html("a < b & c")
        self.assertIn("&lt;", rendered)
        self.assertNotIn("a < b", rendered)

    def test_script_content_is_dropped_to_text(self):
        rendered = MODULE.sanitize_message_html("<b>bold</b> and <script>alert(1)</script>rest")
        self.assertIn("<b>bold</b>", rendered)
        self.assertNotIn("script", rendered)

    def test_link_is_rendered_once_and_clickable(self):
        # The link text must appear exactly once; no bold wrap and no
        # parenthesized duplicate of the URL (the old behavior printed the
        # href a second time, so a bare-URL link showed the URL 3x).
        rendered = MODULE.sanitize_message_html('<a href="https://example.com/x">click</a>')
        self.assertEqual(rendered, '<a href="https://example.com/x">click</a>')
        self.assertEqual(rendered.count("click"), 1)
        self.assertNotIn("(", rendered)
        self.assertNotIn("<b>", rendered)

    def test_bare_url_link_is_not_duplicated(self):
        # When the link text is the URL itself, the output is exactly the
        # anchor with the URL as its text — no bolded copy, no appended
        # parenthesized copy. The old code printed the URL three times.
        url = "https://telegram.org/blog/1-11"
        rendered = MODULE.sanitize_message_html(f'<a href="{url}">{url}</a>')
        self.assertEqual(rendered, f'<a href="{url}">{url}</a>')

    def test_link_href_is_https_only(self):
        self.assertNotIn("javascript:", MODULE.sanitize_message_html('<a href="javascript:alert(1)">x</a>'))
        self.assertNotIn("href", MODULE.sanitize_message_html('<a href="file:///etc/passwd">x</a>'))

    def test_output_is_bounded(self):
        rendered = MODULE.sanitize_message_html("x" * 20000)
        self.assertLessEqual(len(rendered), 8192)


class PeerKeyTests(unittest.TestCase):
    def test_username_and_id_keys(self):
        self.assertEqual(MODULE._peer_key(SimpleNamespace(id=5, username="ada", title=None)), "@ada")
        self.assertEqual(MODULE._peer_key(SimpleNamespace(id=-100123, username=None, title="G")), "id:-100123")

    def test_display_name_prefers_title(self):
        self.assertEqual(MODULE._peer_display_name(SimpleNamespace(title="Group", first_name=None, last_name=None, username="g")), "Group")


class DedicatedSessionTests(unittest.TestCase):
    def _call_with_fake_client(self, authorized):
        seen = {}

        class FakeClient:
            def __init__(self):
                seen["session"] = "/dedicated/plugin-auth"
                seen["api_id"] = 123
                seen["api_hash"] = "api-hash"
                seen["connected"] = True
            async def is_user_authorized(self):
                return authorized
            def is_connected(self):
                return True

        client = FakeClient()
        manager = SimpleNamespace(client=client)
        async def handler(active_client):
            self.assertIs(active_client, client)
            return "handled"

        with patch.object(MODULE, "_telegram_manager", return_value=manager), \
             patch.object(MODULE, "_dedicated_session_path", return_value=Path("/dedicated/plugin-auth")), \
             patch.object(MODULE, "_load_telegram_credentials", return_value=(123, "api-hash")):
            result = asyncio.run(MODULE._with_client(handler))
        return seen, result

    def test_api_client_uses_dedicated_session_not_rss_path(self):
        seen, result = self._call_with_fake_client(True)
        self.assertEqual(result, "handled")
        self.assertEqual(seen["session"], "/dedicated/plugin-auth")
        self.assertEqual(seen["api_id"], 123)
        self.assertEqual(seen["api_hash"], "api-hash")
        self.assertTrue(seen["connected"])

    def test_unauthorized_dedicated_session_fails_closed(self):
        with self.assertRaises(MODULE._AuthUnavailable):
            self._call_with_fake_client(False)

    def test_auth_client_uses_the_shared_session_and_credentials_only(self):
        class Factory:
            def __init__(self, session, api_id, api_hash, **kwargs):
                self.values = (session, api_id, api_hash, kwargs)
        manager = SimpleNamespace(client=Factory("/dedicated/plugin-auth", 123, "api-hash"))
        with patch.object(MODULE, "_telegram_manager", return_value=manager):
            self.assertIs(AUTH._manager(), manager)
            self.assertIs(MODULE._telegram_manager(), manager)
        self.assertEqual(manager.client.values[:3], ("/dedicated/plugin-auth", 123, "api-hash"))

    def test_auth_state_never_persists_codes_or_pending_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth_state.json"
            AUTH._VOLATILE_STATE = {}
            with patch.object(AUTH, "_state_path", return_value=path):
                AUTH._write_state({
                    "stage": "sent-code", "phone": "+15551234567",
                    "code": "678901", "password": "not-stored",
                    "phoneCodeHash": "transient-hash",
                })
                persisted = path.read_text(encoding="utf-8")
                self.assertNotIn("678901", persisted)
                self.assertNotIn("not-stored", persisted)
                self.assertNotIn("transient-hash", persisted)
                self.assertEqual(AUTH._read_state()["phoneCodeHash"], "transient-hash")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                AUTH._write_state({"stage": "done", "phone": "+15551234567"})
                self.assertEqual(AUTH._VOLATILE_STATE, {})


class DialogFolderTests(unittest.TestCase):
    def test_dialog_page_filters_backend_folder_before_applying_limit(self):
        data = {
            "folders": [{"id": "all"}, {"id": "42"}, {"id": "archive"}],
            "dialogs": [
                {"key": "@first", "folderIds": ["all"]},
                {"key": "@match-1", "unread": 2, "folderIds": ["all", "42"]},
                {"key": "@match-2", "unread": 0, "folderIds": ["all", "42"]},
                {"key": "@archived", "folderIds": ["archive"]},
            ],
        }
        result = MODULE._dialog_page(data, "42", 1)
        self.assertEqual(result["activeFolder"], "42")
        self.assertEqual([item["key"] for item in result["dialogs"]], ["@match-1"])
        unread = MODULE._dialog_page(data, "all", 1, unread_only=True)
        self.assertEqual([item["key"] for item in unread["dialogs"]], ["@match-1"])
        fallback = MODULE._dialog_page(data, "999", 2)
        self.assertEqual(fallback["activeFolder"], "all")
        self.assertEqual([item["key"] for item in fallback["dialogs"]], ["@first", "@match-1"])

    def test_unmuted_filter_is_applied_before_limit_and_can_be_disabled(self):
        data = {
            "folders": [{"id": "all"}],
            "dialogs": [
                {"key": "@muted-1", "muted": True, "folderIds": ["all"]},
                {"key": "@visible-1", "muted": False, "folderIds": ["all"]},
            ],
        }
        request = SimpleNamespace(query_params={})
        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_dialog_cache_get", return_value=data),
        ):
            filtered = MODULE.dialogs(request, scope="test-scope", limit=1, folder="all", unmutedOnly=True)
            restored = MODULE.dialogs(request, scope="test-scope", limit=1, folder="all", unmutedOnly=False)
        self.assertEqual([row["key"] for row in filtered["dialogs"]], ["@visible-1"])
        self.assertEqual([row["key"] for row in restored["dialogs"]], ["@muted-1"])

    def test_custom_folder_ids_are_local_memberships_and_unread_is_distinct(self):
        class User:
            def __init__(self, ident, username, contact=False):
                self.id = ident
                self.username = username
                self.first_name = username
                self.last_name = None
                self.title = None
                self.bot = False
                self.contact = contact
                self.mutual_contact = False

        class DialogFilter:
            def __init__(self, ident, title, **flags):
                self.id = ident
                self.title = SimpleNamespace(text=title)
                self.include_peers = flags.pop("include_peers", [])
                self.exclude_peers = flags.pop("exclude_peers", [])
                self.pinned_peers = flags.pop("pinned_peers", [])
                for name in ("contacts", "non_contacts", "groups", "broadcasts", "bots",
                             "exclude_muted", "exclude_read", "exclude_archived"):
                    setattr(self, name, flags.get(name, False))

        class DialogFilterChatlist:
            def __init__(self, ident, title, include_peers):
                self.id = ident
                self.title = SimpleNamespace(text=title)
                self.include_peers = include_peers
                self.pinned_peers = []

        class DialogFilterDefault:
            pass

        alice = SimpleNamespace(entity=User(101, "alice", contact=True), id=1, unread_count=0,
                                folder_id=0, notify_settings=SimpleNamespace(mute_until=None))
        bob = SimpleNamespace(entity=User(102, "bob"), id=2, unread_count=3,
                              folder_id=0, notify_settings=SimpleNamespace(mute_until=None))
        archived = SimpleNamespace(entity=User(103, "old"), id=3, unread_count=1,
                                    folder_id=1, notify_settings=SimpleNamespace(mute_until=None))
        filters = [
            DialogFilterDefault(),
            DialogFilter(7, "Contacts", contacts=True, exclude_archived=True),
            DialogFilter(8, "Explicit people", include_peers=[bob.entity]),
            DialogFilterChatlist(9, "Shared", [archived.entity]),
        ]

        folders, rows = MODULE._apply_dialog_filters([alice, bob, archived], filters)
        self.assertEqual([(folder["id"], folder["title"]) for folder in folders], [
            ("all", "All"), ("7", "Contacts"), ("8", "Explicit people"),
            ("9", "Shared"), ("archive", "Archive"),
        ])
        by_key = {row["key"]: set(row["folderIds"]) for row in rows}
        self.assertIn("7", by_key["@alice"])
        self.assertNotIn("7", by_key["@bob"])
        self.assertIn("8", by_key["@bob"])
        self.assertIn("9", by_key["@old"])
        self.assertEqual(by_key["@old"], {"archive", "9"})

    def test_muted_state_reads_telethon_dialog_wrapper(self):
        wrapped = SimpleNamespace(
            entity=SimpleNamespace(id=101, username="muted", first_name="Muted", title=None),
            unread_count=1, folder_id=0,
            dialog=SimpleNamespace(notify_settings=SimpleNamespace(mute_until=4_000_000_000)),
        )
        _, rows = MODULE._apply_dialog_filters([wrapped], [])
        self.assertTrue(rows[0]["muted"])

    def test_expired_mute_timestamp_is_not_muted(self):
        past = SimpleNamespace(notify_settings=SimpleNamespace(mute_until=100))
        future = SimpleNamespace(notify_settings=SimpleNamespace(mute_until=500))
        self.assertFalse(MODULE._dialog_is_muted(past, now=200))
        self.assertTrue(MODULE._dialog_is_muted(future, now=200))
        self.assertFalse(MODULE._dialog_is_muted(SimpleNamespace(notify_settings=None), now=200))

    def test_custom_unread_rule_keeps_its_own_stable_folder_id(self):
        entity = SimpleNamespace(id=201, username="new", first_name="New", title=None)
        unread_rule = SimpleNamespace(
            id=10, title=SimpleNamespace(text="Important unread"),
            include_peers=[entity], exclude_peers=[], pinned_peers=[], exclude_read=True,
            exclude_muted=False, exclude_archived=True, contacts=False,
            non_contacts=False, groups=False, broadcasts=False, bots=False,
        )
        dialog = SimpleNamespace(entity=entity, unread_count=2, folder_id=0, notify_settings=None)
        folders, rows = MODULE._apply_dialog_filters([dialog], [unread_rule])
        self.assertNotIn({"id": "unread", "title": "Unread", "kind": "unread"}, folders)
        self.assertIn("10", rows[0]["folderIds"])


class PublicMessageTests(unittest.TestCase):
    def test_telegram_entities_are_rendered_in_sanitized_preview(self):
        from telethon.tl.types import MessageEntityBold

        msg = make_message()
        msg.message = "bold"
        msg.entities = [MessageEntityBold(offset=0, length=4)]
        self.assertEqual(public(msg)["htmlPreview"], "<b>bold</b>")

    def test_recent_messages_are_returned_oldest_to_newest(self):
        class Client:
            async def get_entity(self, peer):
                return SimpleNamespace(id=-100123, title="Test Group")

            async def get_me(self):
                return SimpleNamespace(id=1)

            async def get_dialogs(self, limit=None):
                return []

            async def iter_messages(self, entity, **kwargs):
                for mid in (3, 2, 1):
                    yield make_message(mid)

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.messages(
                SimpleNamespace(query_params={}), scope="test-scope", peer="@testgroup", limit=3, topicId=0,
            )
        self.assertEqual([item["id"] for item in result["messages"]], [1, 2, 3])

    def test_message_history_starts_at_read_cursor_and_excludes_older_posts(self):
        entity = SimpleNamespace(id=123, title="Test Group", username="testgroup")
        dialog = SimpleNamespace(
            id=123, entity=entity,
            dialog=SimpleNamespace(read_inbox_max_id=5),
        )
        seen = {}

        class Client:
            async def get_entity(self, peer):
                return entity

            async def get_me(self):
                return SimpleNamespace(id=1)

            async def get_dialogs(self, limit=None):
                return [dialog]

            async def iter_messages(self, active_entity, **kwargs):
                seen.update(kwargs)
                ids = [2, 3, 4, 5, 6, 7, 8]
                if kwargs.get("reverse"):
                    ids = [mid for mid in ids if mid > kwargs.get("min_id", 0)]
                else:
                    ids = list(reversed(ids))
                for mid in ids[:kwargs["limit"]]:
                    yield make_message(mid)

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.messages(
                SimpleNamespace(query_params={}), scope="test-scope", peer="@testgroup", limit=3, topicId=0,
            )

        self.assertEqual(seen.get("min_id"), 4, "cursor message itself must be included")
        self.assertTrue(seen.get("reverse"), "fetch forward from the read cursor")
        self.assertEqual([item["id"] for item in result["messages"]], [5, 6, 7])

    def test_forum_thread_history_starts_at_topic_read_cursor(self):
        entity = SimpleNamespace(id=123, title="Test Group", username="testgroup", forum=True)
        seen = {}

        class Client:
            async def get_entity(self, peer):
                return entity

            async def get_me(self):
                return SimpleNamespace(id=1)

            async def __call__(self, request):
                self.request = request
                return SimpleNamespace(topics=[SimpleNamespace(id=44, read_inbox_max_id=5)])

            async def iter_messages(self, active_entity, **kwargs):
                seen.update(kwargs)
                for mid in (5, 6, 7):
                    yield make_message(mid)

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.messages(
                SimpleNamespace(query_params={}), scope="test-scope", peer="@testgroup", limit=3, topicId=44,
            )
        self.assertEqual(type(client.request).__name__, "GetForumTopicsByIDRequest")
        self.assertEqual(client.request.topics, [44])
        self.assertEqual(seen.get("reply_to"), 44)
        self.assertEqual(seen.get("min_id"), 4)
        self.assertTrue(seen.get("reverse"))
        self.assertEqual([item["id"] for item in result["messages"]], [5, 6, 7])

    def test_general_forum_topic_history_filters_other_topics(self):
        entity = SimpleNamespace(id=123, title="Test Group", username="testgroup", forum=True)
        seen = {}

        class Client:
            async def get_entity(self, peer):
                return entity

            async def get_me(self):
                return SimpleNamespace(id=1)

            async def __call__(self, request):
                return SimpleNamespace(topics=[SimpleNamespace(id=1, read_inbox_max_id=0)])

            async def iter_messages(self, active_entity, **kwargs):
                seen.update(kwargs)
                general_early = make_message(5)
                other_thread = make_message(6)
                other_thread.reply_to = SimpleNamespace(reply_to_msg_id=55, reply_to_top_id=55)
                general_late = make_message(7)
                for message in (general_late, other_thread, general_early):
                    yield message

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.messages(
                SimpleNamespace(query_params={}), scope="test-scope", peer="@testgroup", limit=2, topicId=1,
            )
        self.assertNotIn("reply_to", seen, "General is not a message thread")
        self.assertGreater(seen["limit"], 2, "read a bounded surplus to filter other topics")
        self.assertEqual([item["id"] for item in result["messages"]], [5, 7])

    def test_messages_include_bounded_raster_photo_previews(self):
        MODULE._media_previews.clear()
        from base64 import b64decode

        image_bytes = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jE0cAAAAASUVORK5CYII="
        )
        message = make_message(101)
        message.media = SimpleNamespace()
        message.photo = SimpleNamespace()
        message.document = None

        class Client:
            async def get_entity(self, peer):
                return SimpleNamespace(id=-100123, title="Test Group")

            async def get_me(self):
                return SimpleNamespace(id=1)

            async def get_dialogs(self, limit=None):
                return []

            async def iter_messages(self, entity, **kwargs):
                yield message

            async def download_media(self, target, file, *, thumb):
                self_thumb = thumb
                assert target is message and file is bytes and self_thumb == -1
                return image_bytes

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.messages(
                SimpleNamespace(query_params={}), scope="test-scope", peer="@testgroup", limit=1, topicId=0,
            )

        preview = result["messages"][0]["mediaPreview"]
        self.assertTrue(preview.startswith("data:image/png;base64,"))
        self.assertLessEqual(len(preview), MODULE._MAX_MEDIA_PREVIEW_DATA_URI_LENGTH)

    def test_media_preview_cache_avoids_re_downloading_on_refresh(self):
        # A refresh poll must not re-download images the backend already
        # fetched: the second /messages call serves the cached data URI and
        # performs no download at all.
        MODULE._media_previews.clear()
        image_bytes = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jE0cAAAAASUVORK5CYII="
        )
        message = make_message(101)
        message.media = SimpleNamespace()
        message.photo = SimpleNamespace()
        message.document = None

        class Client:
            downloads = 0

            async def get_entity(self, peer):
                return SimpleNamespace(id=-100123, title="Test Group")

            async def get_me(self):
                return SimpleNamespace(id=1)

            async def get_dialogs(self, limit=None):
                return []

            async def iter_messages(self, entity, **kwargs):
                yield message

            async def download_media(self, target, file, *, thumb):
                type(self).downloads += 1
                return image_bytes

        client = Client()

        async def with_client(handler):
            return await handler(client)

        patches = (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        )
        for p in patches:
            p.start()
        try:
            for _ in range(2):
                result = MODULE.messages(
                    SimpleNamespace(query_params={}), scope="cache-scope", peer="@testgroup", limit=1, topicId=0,
                )
                self.assertTrue(result["messages"][0]["mediaPreview"].startswith("data:image/png;base64,"))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(client.downloads, 1, "second refresh must not re-download")

    def test_media_preview_cache_ttl_and_lru_bound(self):
        MODULE._media_previews.clear()
        uri = "data:image/png;base64,AA"
        # LRU: entries beyond the cap are evicted oldest-first.
        for i in range(MODULE._MEDIA_PREVIEW_CACHE_MAX + 2):
            MODULE._media_preview_put("scope", i, uri)
        self.assertIsNone(MODULE._media_preview_get("scope", 0))
        self.assertEqual(MODULE._media_preview_get("scope", MODULE._MEDIA_PREVIEW_CACHE_MAX + 1), uri)
        # TTL: a stale entry is treated as a miss.
        MODULE._media_previews.clear()
        MODULE._media_preview_put("scope", 1, uri)
        with patch.object(MODULE, "_now", return_value=time.time() + MODULE._MEDIA_PREVIEW_CACHE_TTL_SECONDS + 1):
            self.assertIsNone(MODULE._media_preview_get("scope", 1))

    def test_oversized_photo_is_reencoded_to_fit_budget(self):
        # A large full-size photo (thumb=-1) exceeds the raw byte budget; it
        # must be downscaled/re-encoded rather than dropped, so the pane keeps
        # a real-resolution image instead of the old blurry 128px thumbnail.
        # We synthesize a complex 2400x1600 photo (crisp shapes that compress
        # like a real picture) whose raw JPEG is >160KB but downscales back
        # under budget at quality 88.
        MODULE._media_previews.clear()
        from PIL import Image, ImageDraw
        import io

        def shapes(w, h, n, seed):
            img = Image.new("RGB", (w, h), (210, 180, 140))
            d = ImageDraw.Draw(img)
            rnd = random.Random(seed)
            for _ in range(n):
                x0 = rnd.randrange(0, w)
                y0 = rnd.randrange(0, h)
                r = rnd.randrange(20, min(w, h) // 6)
                d.ellipse([x0 - r, y0 - r, x0 + r, y0 + r],
                          fill=(rnd.randrange(60, 220), rnd.randrange(60, 220), rnd.randrange(60, 220)),
                          outline=(30, 30, 30), width=2)
                d.line([x0, y0, x0 + r, y0 - r], fill=(20, 20, 20), width=3)
            return img

        img = shapes(2400, 1600, 60, seed=7)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92, optimize=True)
        big_jpeg = buf.getvalue()
        self.assertGreater(len(big_jpeg), MODULE._MAX_MEDIA_PREVIEW_BYTES)

        message = make_message(101)
        message.media = SimpleNamespace()
        message.photo = SimpleNamespace()
        message.document = None

        class Client:
            async def get_entity(self, peer):
                return SimpleNamespace(id=-100123, title="Test Group")

            async def get_me(self):
                return SimpleNamespace(id=1)

            async def get_dialogs(self, limit=None):
                return []

            async def iter_messages(self, entity, **kwargs):
                yield message

            async def download_media(self, target, file, *, thumb):
                assert thumb == -1
                return big_jpeg

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.messages(
                SimpleNamespace(query_params={}), scope="test-scope", peer="@testgroup", limit=1, topicId=0,
            )

        preview = result["messages"][0]["mediaPreview"]
        self.assertTrue(preview.startswith("data:image/jpeg;base64,"))
        self.assertLessEqual(len(preview), MODULE._MAX_MEDIA_PREVIEW_DATA_URI_LENGTH)

    def test_unrecognized_image_preview_is_not_returned(self):
        MODULE._media_previews.clear()
        message = make_message(101)
        message.media = SimpleNamespace()
        message.photo = SimpleNamespace()
        message.document = None

        class Client:
            async def get_entity(self, peer):
                return SimpleNamespace(id=-100123, title="Test Group")

            async def get_me(self):
                return SimpleNamespace(id=1)

            async def get_dialogs(self, limit=None):
                return []

            async def iter_messages(self, entity, **kwargs):
                yield message

            async def download_media(self, target, file, *, thumb):
                return b"<svg onload=alert(1)>"

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.messages(
                SimpleNamespace(query_params={}), scope="test-scope", peer="@testgroup", limit=1, topicId=0,
            )

        self.assertNotIn("mediaPreview", result["messages"][0])

    def test_public_message_shape(self):
        item = public(make_message())
        self.assertEqual(item["sender"], "Ada Lovelace")
        self.assertEqual(item["peer"], "Test Group")
        self.assertFalse(item["mine"])
        self.assertEqual(item["text"], "hello world")

    def test_snapshot_is_deterministic(self):
        first = MODULE._message_snapshot(public(make_message()))
        second = MODULE._message_snapshot(public(make_message()))
        self.assertEqual(first, second)


class TicketTests(unittest.TestCase):
    def test_prepare_request_discriminator_and_limits(self):
        scope = "s" * 30
        body = MODULE.SendPrepare(scope=scope, action="send", peer="@ada", message="hi")
        self.assertEqual(body.action, "send")
        with self.assertRaises(Exception):
            MODULE.SendPrepare(scope=scope, action="send", peer="@ada", message="x" * 5000)
        with self.assertRaises(Exception):
            MODULE.SendPrepare(scope=scope, action="send", peer="@ada", message="ok", extra=1)

    def test_commit_requires_explicit_true(self):
        scope = "s" * 30
        with self.assertRaises(Exception):
            MODULE.CommitRequest(scope=scope, confirmationToken="t" * 30, confirmed=1)
        ok = MODULE.CommitRequest(scope=scope, confirmationToken="t" * 30, confirmed=True)
        self.assertIs(ok.confirmed, True)

    def test_consume_ticket_expires_and_scope_binds(self):
        token = MODULE._store_ticket(MODULE.Ticket("scope-a" * 3, "send", "me", {}, {}, None, MODULE._now() + 60))
        # A scope-mismatched commit does not consume the ticket.
        with self.assertRaises(HTTPException):
            MODULE._consume_ticket(token, "other" * 5)
        ticket = MODULE._consume_ticket(token, "scope-a" * 3)
        self.assertEqual(ticket.action, "send")
        with self.assertRaises(HTTPException):
            MODULE._consume_ticket(token, "scope-a" * 3)  # consumed exactly once


class CommitFlowTests(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(query_params={})
        self.scope = "scope" * 4
        self.binding = MODULE._ScopeBinding("backend", "/home", "me", 0)

    def _prepare(self, action="send", **kwargs):
        defaults = {"message": "hi"} if action in {"send", "reply"} else {}
        payload = {"scope": self.scope, "action": action, "peer": "@ada", **defaults, **kwargs}
        body = {
            "send": lambda: MODULE.SendPrepare(**payload),
            "reply": lambda: MODULE.ReplyPrepare(messageId=100, **payload),
            "delete": lambda: MODULE.DeletePrepare(messageId=100, **payload),
            "mark-read": lambda: MODULE.MarkReadPrepare(maxId=100, **payload),
        }[action]()
        with patch.object(MODULE, "_binding", return_value=self.binding), \
             patch.object(MODULE, "_provider_context", return_value=("me", "/home")), \
             patch.object(MODULE, "_run_async", side_effect=lambda coro: _fake_async(coro, self._coro_result)):
            prepared = MODULE.prepare_action(self.request, body)
        return prepared

    def _coro_result(self, coro):
        return self.coro_return

    def test_send_prepare_previews_exact_peer_and_message(self):
        self.coro_return = ({"action": "send", "me": "me", "peer": "Ada", "peerKey": "@ada", "message": "hi"}, None)
        prepared = self._prepare("send")
        self.assertEqual(prepared["preview"]["peerKey"], "@ada")
        self.assertEqual(prepared["preview"]["message"], "hi")
        self.assertIn("confirmationToken", prepared)

    def test_commit_send_verifies_readback_text(self):
        ticket = MODULE.Ticket(self.scope, "send", "me", {"action": "send", "peer": "@ada", "message": "hi"}, {}, None, MODULE._now() + 60)
        token = MODULE._store_ticket(ticket)
        self.coro_return = {"sentId": 77, "peer": "Ada"}
        with patch.object(MODULE, "_binding", return_value=self.binding), \
             patch.object(MODULE, "_consume_ticket", return_value=ticket), \
             patch.object(MODULE, "_provider_context", return_value=("me", "/home")), \
             patch.object(MODULE, "_run_async", side_effect=lambda coro: _fake_async(coro, self._commit_coro(coro))):
            result = MODULE.commit_action(self.request, MODULE.CommitRequest(scope=self.scope, confirmationToken=token, confirmed=True))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["id"], 77)

    def _commit_coro(self, coro):
        # The verify coroutine resolves peer and re-reads the sent message.
        return {"status": "verified", "id": 77, "peer": "Ada", "text": "hi"}

    def test_commit_stale_ticket_is_rejected(self):
        with patch.object(MODULE, "_binding", return_value=self.binding):
            with self.assertRaises(HTTPException) as raised:
                MODULE.commit_action(self.request, MODULE.CommitRequest(scope=self.scope, confirmationToken="missing-token-12345678", confirmed=True))
        self.assertEqual(raised.exception.status_code, 409)


class MarkReadActionTests(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(query_params={})
        self.scope = "mark-read-scope" * 2
        self.binding = MODULE._ScopeBinding("backend", "/home", "me", 0)
        self.entity = SimpleNamespace(id=123, title="Test Group", username="group", forum=True)
        self.target = make_message(321, "exact target")
        self.dialog = SimpleNamespace(
            id=123, entity=self.entity, unread_count=2,
            dialog=SimpleNamespace(read_inbox_max_id=300),
            message=SimpleNamespace(id=330),
        )
        self.reads = []
        self.topic_reads = []
        self.topic = SimpleNamespace(id=11, read_inbox_max_id=300, unread_count=2)

        class Client:
            async def get_entity(inner, peer):
                return self.entity

            async def get_me(inner):
                return SimpleNamespace(id=1)

            async def get_dialogs(inner, limit=None):
                return [self.dialog]

            async def get_messages(inner, entity, ids):
                return self.target if ids == 321 else None

            async def send_read_acknowledge(inner, entity, max_id=None):
                self.reads.append((entity, max_id))
                self.dialog.dialog.read_inbox_max_id = max_id
                # Simulate newer unread posts remaining after the bounded ack.
                self.dialog.unread_count = 2

            async def __call__(inner, request):
                request_name = type(request).__name__
                if request_name == "GetForumTopicsByIDRequest":
                    return SimpleNamespace(topics=[self.topic] if 11 in request.topics else [])
                if request_name == "ReadDiscussionRequest":
                    self.topic_reads.append((request.peer, request.msg_id, request.read_max_id))
                    self.topic.read_inbox_max_id = request.read_max_id
                    return True
                raise AssertionError(f"Unexpected Telegram request: {request_name}")

        self.client = Client()

        async def with_client(handler):
            return await handler(self.client)

        self.patches = (
            patch.object(MODULE, "_binding", return_value=self.binding),
            patch.object(MODULE, "_provider_context", return_value=("me", "/home")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def test_prepare_preview_is_exact_and_confirmed_commit_preserves_newer_unread(self):
        body = MODULE.MarkReadPrepare(
            scope=self.scope, action="mark-read", peer="@group", maxId=321,
        )
        prepared = MODULE.prepare_action(self.request, body)
        ticket = MODULE._tickets[prepared["confirmationToken"]]
        self.assertEqual(prepared["preview"]["maxId"], 321)
        self.assertEqual(prepared["preview"]["targetMessage"]["id"], 321)
        self.assertIsInstance(ticket.message_snapshot, dict)
        self.assertEqual(ticket.message_snapshot["id"], 321)
        self.assertEqual(self.reads, [], "prepare must not change Telegram read state")

        result = MODULE.commit_action(self.request, MODULE.CommitRequest(
            scope=self.scope, confirmationToken=prepared["confirmationToken"], confirmed=True,
        ))
        self.assertEqual(self.reads, [(self.entity, 321)])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["readCursor"], 321)
        self.assertEqual(result["unread"], 2, "posts after the selected target remain unread")

    def test_forum_topic_mark_read_uses_thread_scoped_rpc(self):
        self.target.reply_to = SimpleNamespace(reply_to_msg_id=11, reply_to_top_id=11)
        prepared = MODULE.prepare_action(self.request, MODULE.MarkReadPrepare(
            scope=self.scope, action="mark-read", peer="@group", maxId=321, topicId=11,
        ))
        self.assertEqual(prepared["preview"]["topicId"], 11)
        self.assertEqual(self.reads, [])

        result = MODULE.commit_action(self.request, MODULE.CommitRequest(
            scope=self.scope, confirmationToken=prepared["confirmationToken"], confirmed=True,
        ))
        self.assertEqual(self.reads, [], "a topic action must not acknowledge the whole group")
        self.assertEqual(self.topic_reads, [(self.entity, 11, 321)])
        self.assertEqual(result["readCursor"], 321)

    def test_prepare_rejects_target_from_a_different_forum_topic(self):
        self.target.reply_to = SimpleNamespace(reply_to_msg_id=55, reply_to_top_id=22)
        with self.assertRaises(HTTPException) as raised:
            MODULE.prepare_action(self.request, MODULE.MarkReadPrepare(
                scope=self.scope, action="mark-read", peer="@group", maxId=321, topicId=11,
            ))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.topic_reads, [])

    def test_commit_rejects_target_moved_to_another_forum_topic(self):
        self.target.reply_to = SimpleNamespace(reply_to_msg_id=55, reply_to_top_id=11)
        prepared = MODULE.prepare_action(self.request, MODULE.MarkReadPrepare(
            scope=self.scope, action="mark-read", peer="@group", maxId=321, topicId=11,
        ))
        self.target.reply_to = SimpleNamespace(reply_to_msg_id=55, reply_to_top_id=22)
        with self.assertRaises(HTTPException) as raised:
            MODULE.commit_action(self.request, MODULE.CommitRequest(
                scope=self.scope, confirmationToken=prepared["confirmationToken"], confirmed=True,
            ))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.topic_reads, [], "a topic change after preview must not be acknowledged")

    def test_general_topic_read_is_rejected_without_group_wide_ack(self):
        with self.assertRaises(HTTPException) as raised:
            MODULE.prepare_action(self.request, MODULE.MarkReadPrepare(
                scope=self.scope, action="mark-read", peer="@group", maxId=321, topicId=1,
            ))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.topic_reads, [])

    def test_commit_rejects_changed_target_before_read_ack(self):
        prepared = MODULE.prepare_action(self.request, MODULE.MarkReadPrepare(
            scope=self.scope, action="mark-read", peer="@group", maxId=321,
        ))
        self.target.message = "target changed after preview"
        with self.assertRaises(HTTPException) as raised:
            MODULE.commit_action(self.request, MODULE.CommitRequest(
                scope=self.scope, confirmationToken=prepared["confirmationToken"], confirmed=True,
            ))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.reads, [], "stale preview must not mark anything read")

    def test_commit_mark_read_readback_fetches_dialogs_once(self):
        # Latency: the readback needs one bounded dialogs fetch for both the
        # unread count and the read cursor; a second full fetch doubles it.
        calls: list[int] = []
        prepared = MODULE.prepare_action(self.request, MODULE.MarkReadPrepare(
            scope=self.scope, action="mark-read", peer="@group", maxId=321,
        ))
        # Wrap the mock client's get_dialogs to count calls.
        async def counting_get_dialogs(inner, limit=0):
            calls.append(int(limit))
            return [self.dialog]
        self.client.__class__.get_dialogs = counting_get_dialogs  # type: ignore[method-assign]
        try:
            result = MODULE.commit_action(self.request, MODULE.CommitRequest(
                scope=self.scope, confirmationToken=prepared["confirmationToken"], confirmed=True,
            ))
        finally:
            del self.client.__class__.get_dialogs  # restore original mock method
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(calls), 1, f"expected one get_dialogs call, got {calls}")
        self.assertLessEqual(calls[0], 200, "readback fetch must stay bounded")


class SaveActionTests(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(query_params={})
        self.scope = "save-scope" * 3
        self.binding = MODULE._ScopeBinding("backend", "/home", "me", 0)
        self.entity = SimpleNamespace(id=123, title="Test Group", username="group", forum=True)
        self.target = make_message(321, "keep me")
        self.saved = None  # type: ignore[assignment]
        self.forwards = []

        class Client:
            _self_id = 1
            saved_dialogs = None
            _dialogs_calls = 0
            get_dialogs = None  # type: ignore[assignment]

            async def get_entity(inner, peer):
                return self.entity

            async def get_me(inner):
                return SimpleNamespace(id=1)

            async def get_messages(inner, entity, ids):
                if ids == 321:
                    return self.target
                if ids == 777:
                    return self.saved
                return None

            async def forward_messages(inner, dest, messages, from_peer=None):
                self.forwards.append((dest, messages, from_peer))
                message = SimpleNamespace(
                    id=777,
                    message="keep me",
                    date=None,
                    out=True,
                    unread=False,
                    sender=SimpleNamespace(id=1, first_name="Me", last_name=None, username=None, title=None),
                    chat=SimpleNamespace(id=777001, title="Saved Messages", first_name=None, last_name=None, username=None),
                    media=None,
                    reply_to=None,
                    sender_id=1,
                    forward=SimpleNamespace(id=321),
                )
                self.saved = message
                return [message]

        self.client = Client()

        async def with_client(handler):
            return await handler(self.client)

        self.patches = (
            patch.object(MODULE, "_binding", return_value=self.binding),
            patch.object(MODULE, "_provider_context", return_value=("me", "/home")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def test_prepare_and_commit_save_to_saved_messages_with_readback(self):
        prepared = MODULE.prepare_action(self.request, MODULE.SavePrepare(
            scope=self.scope, action="save", peer="@group", messageId=321,
        ))
        self.assertEqual(prepared["preview"]["action"], "save")
        self.assertEqual(prepared["preview"]["targetMessage"]["id"], 321)
        self.assertEqual(self.forwards, [], "prepare must not forward anything")
        result = MODULE.commit_action(self.request, MODULE.CommitRequest(
            scope=self.scope, confirmationToken=prepared["confirmationToken"], confirmed=True,
        ))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["id"], 777)
        # from_peer must be forwarded: without it Telethon raises ValueError
        # before the RPC is sent, so the live Save action would always fail.
        self.assertEqual(self.forwards, [("me", [321], self.entity)])

    def test_prepare_save_missing_source_fails_closed(self):
        # A vanished source message must be rejected before any ticket exists.
        with self.assertRaises(HTTPException) as raised:
            MODULE.prepare_action(self.request, MODULE.SavePrepare(
                scope=self.scope, action="save", peer="@group", messageId=999,
            ))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.forwards, [], "no ticket, no forward")

    def test_verify_ignores_forward_header_mismatch(self):
        # Regression: the readback used to require forward.id == source
        # messageId, but forward.id is the ORIGINAL SENDER's user id, so a
        # successful save always tripped the check and surfaced as 502
        # "outcome is uncertain" after the message was already stored. The
        # commit already proves the forward landed; the readback only has to
        # prove the message still exists. Pin the readback to a message whose
        # forward header points at a different user id than the source
        # message — the old check would have raised "readback mismatch".
        mismatch = SimpleNamespace(id=777, message="keep me",
                                   fwd_from=SimpleNamespace(from_id=42))
        self.saved = mismatch
        # Keep the readback pinned: setUp's forward mock would otherwise
        # replace self.saved with its own (matching) message.
        async def forward_no_side_effect(dest, messages, from_peer=None):
            self.forwards.append((dest, messages, from_peer))
            return [SimpleNamespace(id=777)]

        self.client.forward_messages = forward_no_side_effect

        prepared = MODULE.prepare_action(self.request, MODULE.SavePrepare(
            scope=self.scope, action="save", peer="@group", messageId=321,
        ))
        result = MODULE.commit_action(self.request, MODULE.CommitRequest(
            scope=self.scope, confirmationToken=prepared["confirmationToken"], confirmed=True,
        ))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["id"], 777)
        self.assertEqual(self.forwards, [("me", [321], self.entity)])

    def test_verify_falls_back_to_dialogs_snapshot(self):
        # When the direct fetch comes back empty (MessagesNotModified for a
        # cached entry), the dialogs snapshot's Saved Messages dialog must
        # confirm the message.
        saved = make_message(777, "keep me")
        self.client._self_id = 1
        # Dialog must match what get_entity(self) resolves to (setUp's mock
        # returns the same group entity for every peer).
        self.client.saved_dialogs = [  # type: ignore[assignment]
            SimpleNamespace(id=self.entity.id, entity=self.entity, message=saved),
        ]
        self.client._dialogs_calls = 0

        async def get_dialogs(limit):
            self.client._dialogs_calls += 1
            return self.client.saved_dialogs

        self.client.get_dialogs = get_dialogs  # type: ignore[assignment]
        # Pin the direct fetch to miss: the setUp forward mock would otherwise
        # repopulate self.saved during commit, hiding the fallback path.
        async def get_messages_miss(entity, ids):
            if ids == 777:
                return None
            return self.target if ids == 321 else None

        self.client.get_messages = get_messages_miss  # type: ignore[assignment]

        prepared = MODULE.prepare_action(self.request, MODULE.SavePrepare(
            scope=self.scope, action="save", peer="@group", messageId=321,
        ))
        result = MODULE.commit_action(self.request, MODULE.CommitRequest(
            scope=self.scope, confirmationToken=prepared["confirmationToken"], confirmed=True,
        ))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["id"], 777)
        self.assertEqual(self.client._dialogs_calls, 1, "fallback must fetch dialogs once")


class ForumTopicsRouteTests(unittest.TestCase):
    def test_topics_serialization_and_nonforum_detection(self):
        from telethon.tl.types import ForumTopic, ForumTopicDeleted, PeerChannel, PeerNotifySettings, PeerUser

        entity = SimpleNamespace(id=123, title="Test Group", username="group", forum=True)
        topic = ForumTopic(
            id=11, date=None, peer=PeerChannel(123), title="Announcements", icon_color=0,
            top_message=99, read_inbox_max_id=80, read_outbox_max_id=0,
            unread_count=3, unread_mentions_count=0, unread_reactions_count=0,
            unread_poll_votes_count=0, from_id=PeerUser(1), notify_settings=PeerNotifySettings(),
            pinned=True, closed=False,
        )
        class Client:
            requests = []

            async def get_entity(self, peer):
                return entity

            async def __call__(self, request):
                self.requests.append(request)
                return SimpleNamespace(topics=[topic, ForumTopicDeleted(id=12)])

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.topics(SimpleNamespace(query_params={}), scope="a" * 32, peer="@group", limit=12)
            entity.forum = False
            nonforum = MODULE.topics(SimpleNamespace(query_params={}), scope="a" * 32, peer="@group", limit=12)

        self.assertEqual(type(client.requests[0]).__name__, "GetForumTopicsRequest")
        self.assertEqual(client.requests[0].limit, 12)
        self.assertEqual(result["topics"], [{
            "id": 11, "title": "Announcements", "topMessage": 99,
            "readInboxMaxId": 80, "unread": 3, "pinned": True, "closed": False,
        }])
        self.assertEqual(json.loads(json.dumps(result))["isForum"], True)
        self.assertEqual(nonforum, {"peer": "Test Group", "isForum": False, "topics": [], "me": "Me"})
        self.assertEqual(len(client.requests), 1, "non-forum chats must not issue forum-topic RPCs")

    def test_topics_preserve_safe_peer_not_found_error(self):
        class Client:
            async def get_entity(self, peer):
                raise ValueError("missing peer")

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            with self.assertRaises(HTTPException) as raised:
                MODULE.topics(SimpleNamespace(query_params={}), scope="a" * 32, peer="@missing", limit=12)
        self.assertEqual(raised.exception.status_code, 404)


class ForcedRefreshRouteTests(unittest.TestCase):
    def test_all_folder_collects_dialogs_from_the_client(self):
        scope = "b" * 32
        entity = SimpleNamespace(id=456, title="Test Group", username="group", forum=False)
        dialog = SimpleNamespace(
            id=456, entity=entity, unread_count=2, folder_id=None, date=None,
            message=SimpleNamespace(id=9), dialog=SimpleNamespace(notify_settings=None),
        )

        class Client:
            async def iter_dialogs(self, limit):
                self.limit = limit
                yield dialog

            async def __call__(self, request):
                self.request = request
                return SimpleNamespace(filters=[])

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.dialogs(
                SimpleNamespace(query_params={}), scope=scope, limit=40,
                folder="all", unreadOnly=False, refresh=True,
            )
        self.assertEqual(result["activeFolder"], "all")
        self.assertEqual([row["name"] for row in result["dialogs"]], ["Test Group"])
        # Entity username is surfaced so the UI can build per-post t.me links.
        self.assertEqual(result["dialogs"][0]["webUsername"], "group")
        self.assertEqual(client.limit, MODULE._DIALOG_SNAPSHOT_LIMIT)
        self.assertEqual(type(client.request).__name__, "GetDialogFiltersRequest")

    def test_forum_dialog_reports_unread_topic_count_not_message_total(self):
        # Forum badge semantics: the dialogs row must carry the *number of
        # topics with unread messages*, not the raw message total (which is
        # what Dialog.unread_count is). A non-forum dialog must not pay the
        # forum-topic RPC.
        from telethon.tl import functions as tg_functions
        from telethon.tl import types as tg_types

        def _topic(unread, top_message, tid):
            return tg_types.ForumTopic(
                id=tid, date=None, peer=None, title=f"t{tid}", icon_color=0,
                top_message=top_message, read_inbox_max_id=0, read_outbox_max_id=0,
                unread_count=unread, unread_mentions_count=0, unread_reactions_count=0,
                unread_poll_votes_count=0, from_id=None, notify_settings=None,
            )

        MODULE._DIALOG_CACHE.clear()
        scope = "c" * 32
        forum_entity = SimpleNamespace(id=700, title="Forum Group", username="forum", forum=True, megagroup=True)
        plain_entity = SimpleNamespace(id=701, title="Plain Group", username="plain", forum=False, megagroup=True)
        forum_dialog = SimpleNamespace(
            id=700, entity=forum_entity, unread_count=15, folder_id=None, date=None,
            message=SimpleNamespace(id=1), dialog=SimpleNamespace(notify_settings=None),
        )
        plain_dialog = SimpleNamespace(
            id=701, entity=plain_entity, unread_count=4, folder_id=None, date=None,
            message=SimpleNamespace(id=2), dialog=SimpleNamespace(notify_settings=None),
        )
        forum_topics = [
            _topic(unread=3, top_message=101, tid=10),
            _topic(unread=0, top_message=102, tid=11),
            _topic(unread=1, top_message=103, tid=12),
        ]

        class Client:
            async def iter_dialogs(self, limit):
                yield forum_dialog
                yield plain_dialog

            async def __call__(self, request):
                self.requests = getattr(self, "requests", [])
                self.requests.append(request)
                if isinstance(request, tg_functions.messages.GetForumTopicsRequest):
                    return SimpleNamespace(topics=list(forum_topics))
                if isinstance(request, tg_functions.messages.GetDialogFiltersRequest):
                    return SimpleNamespace(filters=[])
                raise AssertionError(f"unexpected request {type(request)}")

        client = Client()

        async def with_client(handler):
            return await handler(client)

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", side_effect=with_client),
            patch.object(MODULE, "_run_async", side_effect=asyncio.run),
        ):
            result = MODULE.dialogs(
                SimpleNamespace(query_params={}), scope=scope, limit=40,
                folder="all", unreadOnly=False, refresh=True,
            )
        by_name = {row["name"]: row for row in result["dialogs"]}
        self.assertEqual(by_name["Forum Group"]["unreadTopics"], 2, "2 topics have unread messages")
        self.assertEqual(by_name["Forum Group"]["unread"], 15, "raw message total is preserved")
        self.assertEqual(by_name["Plain Group"]["unreadTopics"], 0, "no topics for a plain dialog")
        self.assertEqual(by_name["Plain Group"]["unread"], 4)
        # Exactly one forum-topic scan, and only against the forum peer.
        forum_topic_calls = [r for r in client.requests if isinstance(r, tg_functions.messages.GetForumTopicsRequest)]
        self.assertEqual(len(forum_topic_calls), 1, "a non-forum must not trigger a topic scan")
        self.assertIs(forum_topic_calls[0].peer, forum_entity)
        self.assertTrue(
            any(isinstance(r, tg_functions.messages.GetDialogFiltersRequest) for r in client.requests),
            "dialog filters still fetched",
        )

    def test_refresh_true_bypasses_cached_snapshot(self):
        MODULE._DIALOG_CACHE.clear()
        scope = "a" * 32
        stale = {"dialogs": [], "folders": [], "fetchedAt": time.time() - 5}
        MODULE._DIALOG_CACHE[scope] = stale

        payload = {"dialogs": [], "folders": [], "fetchedAt": time.time()}
        collected = {"called": False}

        def fake_run(_coro):
            return payload

        def fake_with_client(_handler):
            collected["called"] = True
            return payload

        with (
            patch.object(MODULE, "_binding", return_value=object()),
            patch.object(MODULE, "_provider_context", return_value=("Me", "/profile")),
            patch.object(MODULE, "_with_client", new=fake_with_client),
            patch.object(MODULE, "_run_async", new=fake_run),
        ):
            # refresh=True must not consult the cache and must reach the client.
            MODULE.dialogs(SimpleNamespace(query_params={}), scope=scope, limit=20, folder="all", unreadOnly=False, refresh=True)
            self.assertTrue(collected["called"], "refresh=True must invoke the collector")

            # A normal (refresh=False) request within TTL must hit the cache.
            self.assertIsNotNone(MODULE._dialog_cache_get(scope))


def _fake_async(coro, result_fn):
    """Close the coroutine without running it; return the canned result."""
    coro.close()
    return result_fn(coro) if callable(result_fn) else result_fn


if __name__ == "__main__":
    unittest.main()
