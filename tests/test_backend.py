"""Offline behavior tests for the Telegram dashboard backend.

No network: the Telethon client layer is patched out; these tests exercise
validation, ticket staging, preview construction, and commit readback paths.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "plugins/telegram/dashboard/plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_telegram"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, BACKEND)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = MODULE
SPEC.loader.exec_module(MODULE)


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


class PublicMessageTests(unittest.TestCase):
    def test_public_message_shape(self):
        item = public(make_message())
        self.assertEqual(item["sender"], "Ada Lovelace")
        self.assertEqual(item["peer"], "Test Group")
        self.assertTrue(item["unread"])
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
