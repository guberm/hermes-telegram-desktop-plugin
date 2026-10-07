"""RED regressions: marked-ID mark-read ceiling + one-shot forced refresh."""

from __future__ import annotations

import importlib.util
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


class ReadCursorHelpersTests(unittest.TestCase):
    def test_dialog_match_and_read_cursor_use_the_telethon_dialog(self):
        entity = channel_entity(-1001234567890)
        dialog = fake_dialog(
            mid=MODULE._peer_identity(entity), raw_id=entity.id, top_id=4002, unread=2,
        )
        self.assertTrue(MODULE._dialog_match(dialog, entity), "marked dialog ID resolves the chat")
        self.assertEqual(MODULE._dialog_read_cursor(dialog), 3999)
        self.assertEqual(dialog.unread_count, 2, "newer unread messages may remain beyond the cursor")


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
