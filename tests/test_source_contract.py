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
        self.assertIn('"rss_reader" / "config.json"', self.backend_text)
        self.assertIn("get_hermes_home", self.backend_text)
        for forbidden in ("session_path.write", "json.dump", "to_json()"):
            self.assertNotIn(forbidden, self.backend_text)

    def test_prepare_commit_pipeline_is_present(self):
        self.assertIn('action: Literal["send"]', self.backend_text)
        self.assertIn('action: Literal["reply"]', self.backend_text)
        self.assertIn('action: Literal["delete"]', self.backend_text)
        self.assertIn('action: Literal["mark-read"]', self.backend_text)
        self.assertIn("confirmed: Literal[True]", self.backend_text)
        self.assertIn("_consume_ticket", self.backend_text)
        self.assertIn("mutation_started=True", self.backend_text)

    def test_readback_verifications_exist(self):
        self.assertIn("sent message mismatch", self.backend_text)
        self.assertIn("delete readback mismatch", self.backend_text)
        self.assertIn("read state mismatch", self.backend_text)

    def test_desktop_wires_route_nav_palette_and_confirm(self):
        self.assertIn("ROUTES_AREA", self.plugin_text)
        self.assertIn("SIDEBAR_NAV_AREA", self.plugin_text)
        self.assertIn("PALETTE_AREA", self.plugin_text)
        self.assertIn("/telegram", self.plugin_text)
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
        # Theme variables only. The single allowed literal is the badge text
        # color fallback inside a var() fallback position.
        code = re.sub(r"var\([^)]*\)", "", self.plugin_text)
        hex_or_rgb = re.findall(r"(#(?:[0-9a-fA-F]{3,6})\b|rgb\()", code)
        self.assertEqual(hex_or_rgb, [])

    def test_package_version_present(self):
        text = (ROOT / "plugins/telegram/plugin.yaml").read_text(encoding="utf-8")
        import re
        self.assertRegex(text, r"(?m)^version: \d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
