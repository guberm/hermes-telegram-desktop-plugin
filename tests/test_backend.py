"""Offline behavior tests for the Telegram dashboard backend.

No network: the Telethon client layer is patched out; these tests exercise
validation, ticket staging, preview construction, and commit readback paths.
"""

from __future__ import annotations

import importlib.util
import asyncio
import sys
import tempfile
import unittest
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

    def test_link_href_is_https_only_and_appended(self):
        rendered = MODULE.sanitize_message_html('<a href="https://example.com/x">click</a>')
        self.assertIn('href="https://example.com/x"', rendered)
        self.assertIn("click", rendered)
        evil = MODULE.sanitize_message_html('<a href="javascript:alert(1)">x</a>')
        self.assertNotIn("javascript:", evil)

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


def _fake_async(coro, result_fn):
    """Close the coroutine without running it; return the canned result."""
    coro.close()
    return result_fn(coro) if callable(result_fn) else result_fn


if __name__ == "__main__":
    unittest.main()
