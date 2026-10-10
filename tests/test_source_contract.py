"""Package/source contract tests mirroring the gmail plugin's contract style."""

from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "plugins/telegram/dashboard/plugin_api.py"
PLUGIN = ROOT / "plugins/telegram/desktop/plugin.js"


class ShippedSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend_text = BACKEND.read_text(encoding="utf-8")
        cls.backend_ast = ast.parse(cls.backend_text, filename=str(BACKEND))
        cls.plugin_text = PLUGIN.read_text(encoding="utf-8")

    def test_unified_package_layout(self):
        self.assertTrue((ROOT / "plugins/telegram/plugin.yaml").is_file())
        self.assertTrue((ROOT / "plugins/telegram/__init__.py").is_file())
        self.assertTrue((ROOT / "plugins/telegram/desktop/plugin.js").is_file())
        self.assertTrue((ROOT / "plugins/telegram/dashboard/manifest.json").is_file())

    def test_dashboard_backend_has_no_package_relative_imports(self):
        imports = [
            node for node in ast.walk(self.backend_ast)
            if isinstance(node, ast.ImportFrom) and node.level > 0
        ]
        self.assertEqual(imports, [])

    def test_session_resolves_at_request_time_and_never_persists_secrets(self):
        # Credentials come from the profile's Hermes secret scope; another
        # plugin's config file (the RSS reader's) must never be read.
        self.assertIn("agent.secret_scope", self.backend_text)
        self.assertIn('get_secret("TELEGRAM_API_ID")', self.backend_text)
        self.assertIn('get_secret("TELEGRAM_API_HASH")', self.backend_text)
        self.assertNotIn("rss_reader", self.backend_text)
        self.assertIn('"telegram-plugin-auth"', self.backend_text)
        self.assertIn("_dedicated_session_path()", self.backend_text)
        self.assertIn("_telegram_manager()", self.backend_text)
        self.assertNotIn('config["session_path"]', self.backend_text)
        self.assertIn("get_hermes_home", self.backend_text)
        for forbidden in ("session_path.write", "json.dump", "to_json()"):
            self.assertNotIn(forbidden, self.backend_text)

    def test_telegram_filters_and_desktop_badge_tokens_are_shipped(self):
        self.assertIn("GetDialogFiltersRequest", self.backend_text)
        self.assertIn('"folderIds"', self.backend_text)
        self.assertIn("var(--dt-primary-solid)", self.plugin_text)
        self.assertIn("var(--dt-primary-solid-foreground)", self.plugin_text)
        self.assertNotIn("--ui-accent-foreground", self.plugin_text)

    def test_prepare_commit_pipeline_is_present(self):
        self.assertIn('action: Literal["send"]', self.backend_text)
        self.assertIn('action: Literal["reply"]', self.backend_text)
        self.assertIn('action: Literal["delete"]', self.backend_text)
        self.assertIn('action: Literal["mark-read"]', self.backend_text)
        self.assertIn("confirmed: Literal[True]", self.backend_text)
        self.assertIn('"effect": "Delete for everyone"', self.backend_text)
        self.assertIn("revoke=True", self.backend_text)
        self.assertNotIn("console.debug", self.plugin_text)
        self.assertIn("_consume_ticket", self.backend_text)
        self.assertIn("mutation_started=True", self.backend_text)

    def test_readback_verifications_exist(self):
        self.assertIn("sent message mismatch", self.backend_text)
        self.assertIn("delete readback mismatch", self.backend_text)
        self.assertIn("read cursor did not advance through the requested message", self.backend_text)
        self.assertIn('"readCursor": cursor', self.backend_text)

    def test_desktop_wires_route_nav_palette_and_confirm(self):
        self.assertIn("ROUTES_AREA", self.plugin_text)
        self.assertIn("SIDEBAR_NAV_AREA", self.plugin_text)
        self.assertIn("PALETTE_AREA", self.plugin_text)
        self.assertIn("/telegram-client", self.plugin_text)
        self.assertIn("hasImage", self.plugin_text)
        self.assertIn("Reload image", self.plugin_text)
        self.assertIn("hiddenMessageIds", self.plugin_text)
        self.assertIn("messageKey", self.plugin_text)
        self.assertIn("setHiddenMessageIds(new Set())", self.plugin_text)
        self.assertIn("python_dependencies:", (ROOT / "plugins/telegram/plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("label: 'Telegram'", self.plugin_text)
        self.assertIn("confirmed: true", self.plugin_text)
        self.assertIn("confirmationToken:", self.plugin_text)
        self.assertIn("'status'", self.plugin_text)

    def test_desktop_runtime_uses_only_permitted_imports(self):
        lines = self.plugin_text.splitlines()
        imports, buffer = [], []
        for line in lines:
            if line.startswith("import "):
                buffer = [line]
                if not line.rstrip().endswith(","):
                    imports.append(line)
                    buffer = []
            elif buffer:
                buffer.append(line)
                if not line.rstrip().endswith(","):
                    imports.append("\n".join(buffer))
                    buffer = []
        self.assertTrue(imports)
        joined = "\n".join(imports)
        self.assertNotIn("./", joined)
        for module in ("@hermes/plugin-sdk", "react'", "react/jsx-runtime"):
            self.assertIn(module, joined)

    def test_no_hardcoded_colors_in_frontend(self):
        import re
        # Normal UI must use theme variables. The QR bitmap is intentionally
        # monochrome for scanner contrast; permit only pure black/white there.
        qr_start = self.plugin_text.index("function QrImage(")
        qr_end = self.plugin_text.index("export function AuthPanel(", qr_start)
        qr_code = self.plugin_text[qr_start:qr_end]
        ui_code = self.plugin_text[:qr_start] + self.plugin_text[qr_end:]
        ui_code = re.sub(r"var\([^)]*\)", "", ui_code)
        self.assertEqual(re.findall(r"(#(?:[0-9a-fA-F]{3,6})\b|rgb\()", ui_code), [])
        qr_colors = set(re.findall(r"#(?:[0-9a-fA-F]{3,6})\b", qr_code))
        self.assertEqual(qr_colors, {"#ffffff", "#000000"})

    def test_package_version_present(self):
        text = (ROOT / "plugins/telegram/plugin.yaml").read_text(encoding="utf-8")
        import re
        self.assertRegex(text, r"(?m)^version: \d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
