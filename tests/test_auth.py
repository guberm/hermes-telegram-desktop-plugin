"""Offline auth protocol and shared-session ownership regression tests."""
from __future__ import annotations

import asyncio
import importlib.util
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
        auth._VOLATILE_STATE = {}

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
                auth._write_state({"stage": "sent-code", "phone": "+15551234567", "phoneCodeHash": "hash", "code": "123456"})
                data = path.read_text()
                for secret in ("+15551234567", "hash", "123456"):
                    self.assertNotIn(secret, data)
                self.assertEqual(auth._read_state()["stage"], "sent-code")
                auth._VOLATILE_STATE = {}
                self.assertEqual(auth._read_state()["stage"], "idle")


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
        from telethon import types
        class FakeClient:
            def __init__(self): self.handlers = []; self.exported = asyncio.Event()
            def add_event_handler(self, handler, event): self.handlers.append(handler)
            def remove_event_handler(self, handler): self.handlers.remove(handler)
        class FakeQr:
            expires = datetime.now(timezone.utc) - timedelta(seconds=1)
            token = b"old"
            async def recreate(self):
                self.token = b"refreshed"
                self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)
                self.refreshed.set()
            refreshed = asyncio.Event()
        client, qr = FakeClient(), FakeQr()
        manager = SimpleNamespace(_lock=asyncio.Lock())
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(auth, "_manager", return_value=manager), \
             patch.object(auth, "_api_module", return_value=SimpleNamespace(_load_telegram_credentials=lambda: (1, "hash"))), \
             patch.object(auth, "_state_path", return_value=Path(temp) / "state.json"):
            task = asyncio.create_task(auth._qr_wait_loop(client, qr))
            await asyncio.wait_for(qr.refreshed.wait(), timeout=1)
            self.assertEqual(auth._read_state()["qr_token_b64"], "cmVmcmVzaGVk")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(client.handlers, [])

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
