"""Offline auth protocol and shared-session ownership regression tests."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "plugins/telegram/dashboard/plugin_api.py"
API_NAME = "hermes_dashboard_plugin_telegram"
if API_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(API_NAME, API_PATH)
    api = importlib.util.module_from_spec(spec)
    sys.modules[API_NAME] = api
    spec.loader.exec_module(api)
else:
    api = sys.modules[API_NAME]
AUTH_NAME = "hermes_dashboard_plugin_telegram_auth"
auth = sys.modules.get(AUTH_NAME)
if auth is None:
    spec = importlib.util.spec_from_file_location(AUTH_NAME, ROOT / "plugins/telegram/dashboard/plugin_auth.py")
    auth = importlib.util.module_from_spec(spec)
    sys.modules[AUTH_NAME] = auth
    spec.loader.exec_module(auth)


class SentCode:
    def __init__(self, phone_code_hash="h1", type_name="SentCodeTypeSms", timeout=30, next_type=None):
        self.phone_code_hash = phone_code_hash
        self.type = type(type_name, (), {"length": 6})()
        self.timeout = timeout
        self.next_type = next_type


class SentCodeSuccess:
    def __init__(self, authorization):
        self.authorization = authorization


class SentCodePaymentRequired:
    pass


class Authorization:
    def __init__(self, user):
        self.user = user


class AuthorizationSignUpRequired:
    pass


class AuthTypes:
    SentCode = SentCode
    SentCodeSuccess = SentCodeSuccess
    SentCodePaymentRequired = SentCodePaymentRequired
    Authorization = Authorization
    AuthorizationSignUpRequired = AuthorizationSignUpRequired


class AuthStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.types_patch = patch.object(auth, "_auth_types", return_value=AuthTypes)
        self.types_patch.start()
        self.addCleanup(self.types_patch.stop)
        auth._VOLATILE_STATE.clear()
        auth._QR_TASK.clear()

    async def test_sent_code_union_keeps_challenge_only_in_memory(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "auth_state.json"
            with patch.object(auth, "_state_path", return_value=state_path):
                result = await auth._handle_sent_code(SimpleNamespace(), "+15551234567", SentCode("new-hash", timeout=20))
                self.assertEqual(result["stage"], "sent-code")
                self.assertEqual(result["codeType"], "SentCodeTypeSms")
                self.assertIn(result["resendIn"], {19, 20})
                self.assertEqual(auth._read_state()["phoneCodeHash"], "new-hash")
                saved = state_path.read_text()
                self.assertNotIn("new-hash", saved)
                self.assertNotIn("+15551234567", saved)

    async def test_sent_code_without_hash_fails_closed(self):
        with self.assertRaises(auth._AuthFlowError):
            await auth._handle_sent_code(SimpleNamespace(), "+15551234567", SentCode(""))

    async def test_sent_code_success_initializes_same_client(self):
        user = SimpleNamespace(first_name="Ada")
        calls = []
        async def login(value):
            calls.append(value)
        client = SimpleNamespace(_on_login=login)
        result = await auth._handle_sent_code(client, "+15551234567", SentCodeSuccess(Authorization(user)))
        self.assertEqual(result["stage"], "done")
        self.assertEqual(result["me"], "Ada")
        self.assertEqual(calls, [user])

    async def test_payment_required_is_not_misread_as_a_code_challenge(self):
        result = await auth._handle_sent_code(SimpleNamespace(), "+15551234567", SentCodePaymentRequired())
        self.assertEqual(result["stage"], "payment-required")
        self.assertNotIn("phoneCodeHash", auth._VOLATILE_STATE)

    async def test_email_setup_and_email_code_are_explicit_stages(self):
        for type_name, expected in (("SentCodeTypeSetUpEmailRequired", "email-setup"),
                                    ("SentCodeTypeEmailCode", "email-code")):
            result = await auth._handle_sent_code(SimpleNamespace(), "+15551234567", SentCode(type_name=type_name))
            self.assertEqual(result["stage"], expected)

    async def test_signup_required_never_claims_authorization(self):
        calls = []
        client = SimpleNamespace(_on_login=lambda value: calls.append(value), get_me=lambda: calls.append("get_me"))
        result = await auth._finish_authorization(client, AuthorizationSignUpRequired(), "+15551234567")
        self.assertEqual(result["stage"], "signup-required")
        self.assertEqual(calls, [])

    async def test_authorization_logs_in_on_current_client(self):
        user = SimpleNamespace(first_name="Ada")
        calls = []
        async def login(value):
            calls.append(value)
        client = SimpleNamespace(_on_login=login)
        result = await auth._finish_authorization(client, Authorization(user), "+15551234567")
        self.assertEqual(result["stage"], "done")
        self.assertEqual(calls, [user])

    async def test_qr_status_poll_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(auth, "_state_path", return_value=Path(temp) / "state.json"):
                auth._write_state({"stage": "qr-pending", "qr_token_b64": "volatile", "qr_started": time.time()})
                before = auth._VOLATILE_STATE.copy()
                result = await auth._qr_poll()
                self.assertEqual(result["stage"], "qr-pending")
                self.assertEqual(before, auth._VOLATILE_STATE)

    async def test_qr_tokens_use_unpadded_base64url_bytes(self):
        self.assertEqual(auth._qr_token_b64(SimpleNamespace(token=b"\xfb\xff")), "-_8")

    async def test_phone_and_qr_bearer_fields_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            with patch.object(auth, "_state_path", return_value=path):
                auth._write_state({"stage": "sent-code", "phone": "+155****4567", "phoneCodeHash": "hash", "code": "123456"})
                data = path.read_text()
                for secret in ("+155****4567", "hash", "123456"):
                    self.assertNotIn(secret, data)
                self.assertEqual(auth._read_state()["stage"], "sent-code")
                auth._VOLATILE_STATE.clear()
                auth._QR_TASK.clear()
                self.assertEqual(auth._read_state()["stage"], "idle")

    async def test_password_stage_survives_backend_restart_without_secrets(self):
        # The password stage carries no transient secret (SRP material is
        # fetched fresh at submit), so a backend restart must keep the UI on
        # the password field instead of bouncing back to the login screen.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            with patch.object(auth, "_state_path", return_value=path):
                auth._write_state({"stage": "password", "hint": "first pet", "hasRecovery": True, "emailPattern": "x@y.com"})
                persisted = json.loads(path.read_text())
                self.assertEqual(persisted.get("stage"), "password")
                self.assertNotIn("hint", persisted)  # hint is display-only, stays volatile
                auth._VOLATILE_STATE.clear()
                auth._QR_TASK.clear()
                resumed = auth._read_state()
                self.assertEqual(resumed["stage"], "password")
                # cleanup the persisted marker
                auth._write_state({"stage": "idle"})

    async def test_two_profile_paths_isolate_challenges_tokens_and_qr_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = {name: Path(temp) / name for name in ("profile-a", "profile-b")}
            paths = {name: root / "auth_state.json" for name, root in roots.items()}
            for path in paths.values():
                path.parent.mkdir()
            with patch.object(auth, "_state_path", side_effect=lambda: Path(auth._profile_key()).parent / "auth_state.json"):
                for name, stage, phone_hash, token_value in (
                    ("profile-a", "sent-code", "hash-a", ""),
                    ("profile-b", "qr-pending", "", "token-b"),
                ):
                    context = auth._ACTIVE_SESSION_KEY.set(str(roots[name] / "session"))
                    try:
                        auth._write_state({"stage": stage, "phone": "+15551234567" if phone_hash else "",
                                           "phoneCodeHash": phone_hash, "qr_token_b64": token_value})
                    finally:
                        auth._ACTIVE_SESSION_KEY.reset(context)

                states = {}
                for name in roots:
                    context = auth._ACTIVE_SESSION_KEY.set(str(roots[name] / "session"))
                    try:
                        states[name] = auth._read_state()
                    finally:
                        auth._ACTIVE_SESSION_KEY.reset(context)
                self.assertEqual((states["profile-a"]["stage"], states["profile-a"]["phoneCodeHash"]), ("sent-code", "hash-a"))
                self.assertNotEqual(states["profile-a"].get("qr_token_b64"), "token-b")
                self.assertEqual((states["profile-b"]["stage"], states["profile-b"]["qr_token_b64"]), ("qr-pending", "token-b"))
                self.assertNotEqual(states["profile-b"].get("phoneCodeHash"), "hash-a")

                class FakeTask:
                    def __init__(self): self.cancelled = False
                    def cancel(self): self.cancelled = True
                task_a, task_b = FakeTask(), FakeTask()
                key_a, key_b = str(roots["profile-a"] / "session"), str(roots["profile-b"] / "session")
                auth._QR_TASK.update({key_a: task_a, key_b: task_b})
                class FakeClient:
                    async def __call__(self, request): return True
                manager = SimpleNamespace(client=FakeClient())
                with patch.object(auth, "_manager", return_value=manager):
                    context = auth._ACTIVE_SESSION_KEY.set(key_a)
                    try:
                        await auth._cancel()
                        self.assertTrue(task_a.cancelled)
                        self.assertFalse(task_b.cancelled)
                        self.assertNotIn(key_a, auth._QR_TASK)
                        self.assertIs(auth._QR_TASK[key_b], task_b)
                        self.assertEqual(auth._read_state()["stage"], "idle")

                        # A new QR flow in A must not cancel or replace B's task.
                        from telethon import types
                        from datetime import datetime, timezone, timedelta
                        class FakeQr:
                            def __init__(self):
                                self._resp = types.auth.LoginToken(expires=datetime.now(timezone.utc) + timedelta(seconds=30), token=b"a")
                                self.token = b"a"
                                self.expires = self._resp.expires
                        class StartClient:
                            async def is_user_authorized(self): return False
                            async def qr_login(self): return FakeQr()
                            def add_event_handler(self, *args): pass
                            def remove_event_handler(self, *args): pass
                        start_manager = SimpleNamespace(client=StartClient(), _lock=asyncio.Lock())
                        async def hold_waiter(client, qr): await asyncio.Event().wait()
                        with patch.object(auth, "_manager", return_value=start_manager):
                            with patch.object(auth, "_qr_wait_loop", new=hold_waiter):
                                result = await auth._qr_start()
                        self.assertEqual(result["stage"], "qr-pending")
                        self.assertIs(auth._QR_TASK[key_b], task_b)
                        self.assertFalse(task_b.cancelled)
                        other_profile = auth._ACTIVE_SESSION_KEY.set(key_b)
                        try:
                            state_b_after_a_cancel_start = auth._read_state()
                        finally:
                            auth._ACTIVE_SESSION_KEY.reset(other_profile)
                        self.assertEqual(state_b_after_a_cancel_start["stage"], "qr-pending")
                        self.assertEqual(state_b_after_a_cancel_start["qr_token_b64"], "token-b")
                        auth._QR_TASK[key_a].cancel()
                        await asyncio.gather(auth._QR_TASK[key_a], return_exceptions=True)
                    finally:
                        auth._ACTIVE_SESSION_KEY.reset(context)
                        auth._QR_TASK.pop(key_a, None)
                        auth._QR_TASK.pop(key_b, None)

    def test_password_recovery_start_post_route_is_registered(self):
        self.assertIn(("POST", "/auth/password-recovery"), {
            (method, route.path)
            for route in auth.router.routes
            for method in getattr(route, "methods", set())
        })


class ManagerTests(unittest.TestCase):
    def test_manager_reuses_one_client_and_serializes_callers(self):
        from plugins.telegram.dashboard.telegram_client import TelegramClientManager
        with tempfile.TemporaryDirectory() as temp:
            instances = []
            class FakeClient:
                def __init__(self, *args, **kwargs):
                    instances.append(self)
                    self.connected = False
                async def connect(self): self.connected = True
                async def disconnect(self): self.connected = False
                def is_connected(self): return self.connected
                async def is_user_authorized(self): return True
            manager = TelegramClientManager(Path(temp) / "session", lambda: (1, "hash"), client_factory=FakeClient)
            self.addCleanup(manager.close)
            entered, active, maximum = [], 0, 0
            guard = threading.Lock()
            async def operation(client, value):
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                await asyncio.sleep(0.03)
                entered.append((id(client), value))
                with guard: active -= 1
                return value
            output = []
            async def initialize(value):
                return value
            manager.run(initialize(0))
            threads = [threading.Thread(target=lambda value=value: output.append(manager.run(operation(manager.client, value)))) for value in (1, 2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=2)
            self.assertEqual(sorted(output), [1, 2])
            self.assertEqual(len(instances), 1)
            self.assertEqual(maximum, 1)
            self.assertEqual(len({item[0] for item in entered}), 1)

    def test_manager_fails_closed_if_another_process_owns_the_session(self):
        import fcntl
        from plugins.telegram.dashboard.telegram_client import SessionOwnershipError, TelegramClientManager
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "session"
            lock_path = Path(str(session) + ".owner.lock")
            held = open(lock_path, "a+")
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            manager = TelegramClientManager(session, lambda: (1, "hash"), client_factory=lambda *a, **kw: None)
            self.addCleanup(manager.close)
            with self.assertRaises(SessionOwnershipError):
                manager._start()
            held.close()


class TelethonQrHelperTests(unittest.IsolatedAsyncioTestCase):
    async def _login(self, results):
        from telethon.tl.custom.qrlogin import QRLogin
        from telethon.tl import types
        class FakeClient:
            api_id, api_hash = 123, "api-hash"
            def __init__(self): self.calls, self.switched, self.logged_in, self.handlers = [], [], [], []
            def add_event_handler(self, handler, event):
                self.handlers.append(handler)
                asyncio.get_running_loop().call_soon(asyncio.create_task, handler(types.UpdateLoginToken()))
            def remove_event_handler(self, handler): self.handlers.remove(handler)
            async def __call__(self, request):
                self.calls.append(request)
                return results.pop(0)
            async def _switch_dc(self, dc_id): self.switched.append(dc_id)
            async def _on_login(self, user): self.logged_in.append(user)
        client = FakeClient()
        qr = QRLogin(client, [])
        qr._resp = types.auth.LoginToken(expires=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), token=b"first")
        user = types.User(id=7, first_name="Ada", last_name="", phone="123", username=None, access_hash=1)
        results.extend([]) if False else None
        await qr.wait(timeout=1)
        return client, user

    async def test_migrate_then_success_imports_on_same_client_and_authorizes(self):
        from telethon.tl import types
        from telethon import functions
        user = types.User(id=7, first_name="Ada", last_name="", phone="123", username=None, access_hash=1)
        authorization = types.auth.Authorization(user=user, tmp_sessions=None, future_auth_token=None)
        # Build the fake client explicitly so the imported token union is observable.
        from telethon.tl.custom.qrlogin import QRLogin
        class FakeClient:
            api_id, api_hash = 123, "api-hash"
            def __init__(self): self.calls, self.switched, self.logged_in, self.handlers = [], [], [], []
            def add_event_handler(self, handler, event): self.handlers.append(handler); asyncio.get_running_loop().call_soon(asyncio.create_task, handler(types.UpdateLoginToken()))
            def remove_event_handler(self, handler): self.handlers.remove(handler)
            async def __call__(self, request):
                self.calls.append(request)
                return (types.auth.LoginTokenMigrateTo(dc_id=4, token=b"migrate")
                        if isinstance(request, functions.auth.ExportLoginTokenRequest)
                        else types.auth.LoginTokenSuccess(authorization=authorization))
            async def _switch_dc(self, dc_id): self.switched.append(dc_id)
            async def _on_login(self, value): self.logged_in.append(value)
        client = FakeClient()
        qr = QRLogin(client, [])
        qr._resp = types.auth.LoginToken(expires=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), token=b"start")
        result = await qr.wait(timeout=1)
        self.assertIs(result, user)
        self.assertEqual(client.switched, [4])
        self.assertEqual(len(client.calls), 2)
        self.assertIsInstance(client.calls[0], functions.auth.ExportLoginTokenRequest)
        self.assertIsInstance(client.calls[1], functions.auth.ImportLoginTokenRequest)
        self.assertEqual(client.logged_in, [user])

    async def test_auth_qr_migration_then_success_imports_on_same_client_and_authorizes(self):
        from telethon import functions, types
        user = types.User(id=7, first_name="Ada", last_name="", phone="123", username=None, access_hash=1)
        authorization = types.auth.Authorization(user=user, tmp_sessions=None, future_auth_token=None)
        class FakeClient:
            def __init__(self): self.calls, self.switched, self.logged_in = [], [], []
            async def __call__(self, request):
                self.calls.append(request)
                return types.auth.LoginTokenSuccess(authorization=authorization)
            async def _switch_dc(self, dc_id): self.switched.append(dc_id)
            async def _on_login(self, value): self.logged_in.append(value)
            async def get_me(self): return user
        client = FakeClient()
        migration = types.auth.LoginTokenMigrateTo(dc_id=4, token=b"migrate")
        result, refreshed = await auth._handle_qr_result(client, migration)
        self.assertEqual(result["stage"], "done")
        self.assertIsNone(refreshed)
        self.assertEqual(client.switched, [4])
        self.assertEqual(len(client.calls), 1)
        self.assertIsInstance(client.calls[0], functions.auth.ImportLoginTokenRequest)
        self.assertEqual(client.logged_in, [user])

    async def test_qr_expiry_refresh_uses_server_deadline_and_recreates(self):
        from datetime import datetime, timezone, timedelta
        from telethon import functions, types
        class FakeClient:
            def __init__(self): self.handlers = []; self.exported = asyncio.Event(); self.calls = []; self.qr = None
            def add_event_handler(self, handler, event): self.handlers.append(handler)
            def remove_event_handler(self, handler): self.handlers.remove(handler)
            async def __call__(self, request):
                self.calls.append(request)
                self.exported.set()
                self.qr.expires = datetime.now(timezone.utc) + timedelta(seconds=30)
                self.qr.token = b"refreshed"
                self.qr.refreshed.set()
                return types.auth.LoginToken(expires=self.qr.expires, token=self.qr.token)
        class FakeQr:
            expires = datetime.now(timezone.utc) - timedelta(seconds=1)
            token = b"old"
            refreshed = asyncio.Event()
        client, qr = FakeClient(), FakeQr()
        client.qr = qr
        manager = SimpleNamespace(_lock=asyncio.Lock())
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(auth, "_manager", return_value=manager), \
             patch.object(auth, "_api_module", return_value=SimpleNamespace(_load_telegram_credentials=lambda: (1, "hash"))), \
             patch.object(auth, "_state_path", return_value=Path(temp) / "state.json"):
            context = auth._ACTIVE_SESSION_KEY.set(str(Path(temp) / "session"))
            try:
                task = asyncio.create_task(auth._qr_wait_loop(client, qr))
                await asyncio.wait_for(qr.refreshed.wait(), timeout=1)
                self.assertEqual(auth._read_state()["qr_token_b64"], "cmVmcmVzaGVk")
                self.assertIsInstance(client.calls[0], functions.auth.ExportLoginTokenRequest)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                auth._ACTIVE_SESSION_KEY.reset(context)
        self.assertEqual(client.handlers, [])

    async def test_qr_expiry_processes_success_migration_and_unsupported_union(self):
        from datetime import datetime, timezone, timedelta
        from telethon import functions, types
        user = types.User(id=7, first_name="Ada", last_name="", phone="123", username=None, access_hash=1)
        authorization = types.auth.Authorization(user=user, tmp_sessions=None, future_auth_token=None)

        async def run_expiry(exported, imported=None):
            class FakeClient:
                def __init__(self): self.handlers = []; self.calls = []; self.finished = asyncio.Event(); self.logged_in = None; self.switched = []
                def add_event_handler(self, handler, event): self.handlers.append(handler)
                def remove_event_handler(self, handler): self.handlers.remove(handler)
                async def __call__(self, request):
                    self.calls.append(request)
                    if isinstance(request, functions.auth.ImportLoginTokenRequest):
                        return imported
                    return exported
                async def _switch_dc(self, dc_id): self.switched.append(dc_id)
                async def _on_login(self, value): self.logged_in = value
                async def get_me(self): return user
            class FakeQr:
                expires = datetime.now(timezone.utc) - timedelta(seconds=1)
                token = b"old"
            client, qr = FakeClient(), FakeQr()
            profile = str(Path(tempfile.gettempdir()) / f"qr-expiry-{id(client)}" / "session")
            context = auth._ACTIVE_SESSION_KEY.set(profile)
            try:
                manager = SimpleNamespace(_lock=asyncio.Lock())
                with tempfile.TemporaryDirectory() as temp:
                    with patch.object(auth, "_manager", return_value=manager):
                        with patch.object(auth, "_api_module", return_value=SimpleNamespace(_load_telegram_credentials=lambda: (1, "hash"))):
                            with patch.object(auth, "_state_path", return_value=Path(temp) / "state.json"):
                                task = asyncio.create_task(auth._qr_wait_loop(client, qr))
                                for _ in range(100):
                                    if client.calls or task.done(): break
                                    await asyncio.sleep(0.001)
                                if not task.done():
                                    await asyncio.sleep(0.01)
                                state = auth._read_state()
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                                return state, client
            finally:
                auth._ACTIVE_SESSION_KEY.reset(context)

        success, client = await run_expiry(types.auth.LoginTokenSuccess(authorization=authorization))
        self.assertEqual(success["stage"], "done")
        self.assertIs(client.logged_in, user)
        self.assertEqual(len(client.calls), 1)

        migration, client = await run_expiry(
            types.auth.LoginTokenMigrateTo(dc_id=4, token=b"migrate"),
            types.auth.LoginTokenSuccess(authorization=authorization),
        )
        self.assertEqual(migration["stage"], "done")
        self.assertEqual(client.switched, [4])
        self.assertEqual(len(client.calls), 2)
        self.assertIsInstance(client.calls[1], functions.auth.ImportLoginTokenRequest)

        unsupported, client = await run_expiry(object())
        self.assertEqual(unsupported["stage"], "error")
        self.assertEqual(len(client.calls), 1)

    async def test_own_qr_result_helper_encodes_token_union_branch(self):
        from datetime import datetime, timezone, timedelta
        from telethon import types
        result = types.auth.LoginToken(expires=datetime.now(timezone.utc) + timedelta(seconds=30), token=b"\xfb\xff")
        state, refreshed = await auth._handle_qr_result(SimpleNamespace(), result)
        self.assertEqual(state["qr_token_b64"], "-_8")
        self.assertIs(refreshed, result)

    async def test_expiry_is_timeout_not_success(self):
        from telethon.tl.custom.qrlogin import QRLogin
        from telethon.tl import types
        class FakeClient:
            api_id, api_hash = 1, "x"
            def add_event_handler(self, handler, event): pass
            def remove_event_handler(self, handler): pass
            async def __call__(self, request): return types.auth.LoginToken(expires=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), token=b"fresh")
        client = FakeClient()
        qr = QRLogin(client, [])
        qr._resp = types.auth.LoginToken(expires=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), token=b"old")
        with self.assertRaises(asyncio.TimeoutError):
            await qr.wait(timeout=0)
        await qr.recreate()
        self.assertEqual(qr.token, b"fresh")


if __name__ == "__main__":
    unittest.main()
