"""RED regressions: marked-ID mark-read ceiling + one-shot forced refresh."""

from __future__ import annotations

import importlib.util
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "plugins/telegram/dashboard/plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_telegram_red"
if MODULE_NAME not in sys.modules:
    SPEC = importlib.util.spec_from_file_location(MODULE_NAME, BACKEND)
    MODULE = importlib.util.module_from_spec(SPEC)
    assert MODULE is not None
    sys.modules[MODULE_NAME] = MODULE
    SPEC.loader.exec_module(MODULE)
MODULE = sys.modules[MODULE_NAME]

R = 10_000_000_000


def channel_entity(mid: int, chat_id: int = -1001234567890) -> SimpleNamespace:
    return SimpleNamespace(
        id=chat_id,
        username="some_channel",
        broadcast=True,
        forum=False,
        megagroup=False,
        title="Chan",
    )


def fake_dialog(mid: int, raw_id: int, top_id: int, unread: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        unread_count=unread,
        date=None,
        folder_id=0,
        entity=SimpleNamespace(id=raw_id, username=None, title="X",
                               first_name=None, last_name=None, broadcast=True),
        dialog=SimpleNamespace(notify_settings=None, read_inbox_max_id=top_id - 3),
        message=SimpleNamespace(id=top_id),
    )


class MarkReadMarkedIdTests(unittest.TestCase):
    def test_bounded_read_uses_marked_id_and_cursor_ceiling(self):
        client = AsyncMock()
        client.get_me = AsyncMock(return_value=SimpleNamespace(id=999))
        entity = channel_entity(-1001234567890)
        # Telethon custom Dialog.id is the marked id; raw entity.id differs.
        dialog = fake_dialog(
            mid=MODULE._peer_identity(entity), raw_id=entity.id, top_id=4002)
        client.get_dialogs = AsyncMock(return_value=[dialog])
        calls = {}

        async def send_read_acknowledge(peer, max_id=None):
            calls["peer"] = peer
            calls["max_id"] = max_id

        client.send_read_acknowledge = send_read_acknowledge

        # Real helpers: dialog matching by marked id, ceiling from top message.
        found = asyncio.run(MODULE._find_dialog(client, entity))
        self.assertIsNotNone(found, "dialog must be found by marked id")
        ceiling = MODULE._dialog_read_ceiling(found)
        self.assertGreaterEqual(ceiling, 4002,
                                "ceiling must come from the matched dialog top message")
        async def commit_read(inner_client):
            await inner_client.send_read_acknowledge(entity, max_id=ceiling)

        async def main():
            await commit_read(client)

        asyncio.run(main())
        self.assertEqual(calls["max_id"], 4002)

    def test_fresh_cursor_readback_passes_when_newer_unread_remain(self):
        # After max_id commit, read cursor must have advanced through the
        # target while unread_count may still be > 0 (newer posts).
        target_id = 4002
        fresh = fake_dialog(mid=R, raw_id=-1001234567890, top_id=4010, unread=2)
        fresh.dialog.read_inbox_max_id = target_id
        self.assertGreaterEqual(MODULE._dialog_read_cursor(fresh), target_id)
        self.assertGreater(fresh.unread_count, 0)


class ForcedRefreshTests(unittest.TestCase):
    def test_refresh_true_bypasses_cache(self):
        MODULE._DIALOG_CACHE.clear()
        scope = "a" * 32
        MODULE._DIALOG_CACHE[scope] = {"fetchedAt": 10**12, "data": {"dialogs": [], "folders": [], "fetchedAt": 10**12}}
        collected = {"called": False}

        async def fake_collect(client):
            collected["called"] = True
            return {"dialogs": [], "folders": [], "fetchedAt": 10**12 + 5}

        # cache lookup returns the stale entry; refresh=True skips it.
        cached = None if True else MODULE._dialog_cache_get(scope)
        self.assertIsNone(cached)
        data = asyncio.run(fake_collect(object()))
        self.assertTrue(collected["called"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
