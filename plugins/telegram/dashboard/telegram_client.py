"""Single-owner Telethon session bridge for the Telegram desktop plugin.

The synchronous FastAPI routes submit work to one dedicated asyncio loop. The
client, its loop-bound primitives, and the SQLite session remain owned by that
loop for the lifetime of the dashboard process.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import threading
from pathlib import Path
from typing import Any, Callable


class SessionOwnershipError(RuntimeError):
    """Another process already owns the dedicated Telethon SQLite session."""


class TelegramClientManager:
    def __init__(self, session_path: Path, credentials: Callable[[], tuple[int, str]], *, client_factory: Any = None):
        self.session_path = Path(session_path)
        self.credentials = credentials
        self.client_factory = client_factory
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._startup_error: BaseException | None = None
        self._lock: asyncio.Lock | None = None
        self._owner_fd: int | None = None
        self._closed = False

    def _start(self) -> None:
        with self._start_lock:
            if self._closed:
                raise RuntimeError("Telegram client manager is closed")
            if self.loop is not None and self.loop.is_running():
                return
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(target=self._thread_main, name="telegram-client-loop", daemon=True)
            self._thread.start()
            if not self._ready.wait(15):
                raise TimeoutError("Telegram client loop failed to start")
            if self._startup_error:
                raise self._startup_error

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        self._lock = asyncio.Lock()
        try:
            loop.run_until_complete(self._initialize())
        except BaseException as exc:
            self._startup_error = exc
            self._release_owner()
            self._ready.set()
            loop.close()
            self.loop = None
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self.loop = None

    async def _initialize(self) -> None:
        self.session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.session_path.parent.chmod(0o700)
        except OSError:
            pass
        lock_path = Path(str(self.session_path) + ".owner.lock")
        self._owner_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(self._owner_fd, 0o600)
            fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            self._release_owner()
            raise SessionOwnershipError("another dashboard process owns this Telegram session") from exc
        api_id, api_hash = self.credentials()
        if self.client_factory is None:
            from telethon import TelegramClient
            factory = TelegramClient
        else:
            factory = self.client_factory
        self.client = factory(str(self.session_path), api_id, api_hash, receive_updates=True,
                              auto_reconnect=True, connection_retries=3, request_retries=3,
                              flood_sleep_threshold=0)
        await self.client.connect()
        self.harden_session_permissions()

    def harden_session_permissions(self) -> None:
        """Restrict the Telethon session artifacts to their owner.

        Telethon appends ``.session`` to the path it is given, so chmod-ing the
        bare ``session_path`` leaves the real file (plus SQLite's ``-wal`` /
        ``-shm`` sidecars) at the umask default.
        """
        base = f"{self.session_path}.session"
        for candidate in (base, f"{base}-wal", f"{base}-shm"):
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                pass

    def _release_owner(self) -> None:
        if self._owner_fd is not None:
            try:
                fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._owner_fd)
            except OSError:
                pass
            self._owner_fd = None

    async def run_serialized(self, coroutine: Any) -> Any:
        if self.client is None or self._lock is None:
            raise RuntimeError("Telegram client manager is not initialized")
        async with self._lock:
            if not self.client.is_connected():
                await self.client.connect()
            return await coroutine

    def run(self, coroutine: Any, *, timeout: float = 60.0) -> Any:
        self._start()
        loop = self.loop
        if loop is None:
            raise RuntimeError("Telegram client loop is unavailable")
        if threading.current_thread() is self._thread:
            raise RuntimeError("synchronous bridge cannot block its owner loop")
        future = asyncio.run_coroutine_threadsafe(self.run_serialized(coroutine), loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError("Telegram request exceeded the bounded operation time") from None

    async def replace_client(self) -> None:
        """Replace a logged-out client while retaining process ownership."""
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()
        api_id, api_hash = self.credentials()
        if self.client_factory is None:
            from telethon import TelegramClient
            factory = TelegramClient
        else:
            factory = self.client_factory
        self.client = factory(str(self.session_path), api_id, api_hash, receive_updates=True,
                              auto_reconnect=True, connection_retries=3, request_retries=3,
                              flood_sleep_threshold=0)
        await self.client.connect()
        self.harden_session_permissions()

    def submit(self, coroutine: Any) -> Any:
        """Schedule an independent owner-loop coroutine (e.g. QR waiter)."""
        self._start()
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def cancel_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None or self.loop is None or not self.loop.is_running():
            return
        self.loop.call_soon_threadsafe(task.cancel)

    async def close_async(self) -> None:
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()
        self.client = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop, thread = self.loop, self._thread
        if loop is not None and loop.is_running() and threading.current_thread() is not thread:
            future = asyncio.run_coroutine_threadsafe(self.close_async(), loop)
            try:
                future.result(timeout=15)
            finally:
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)
        self._release_owner()
        self._thread = None
        self.client = None
