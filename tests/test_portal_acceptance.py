import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "portal" / "static" / "index.html"


class PortalAcceptanceTests(unittest.TestCase):
    def _html(self):
        return INDEX.read_text(encoding="utf-8")

    def test_help_entries_exist(self):
        html = self._html()
        for entry in ("id=\"helpBtn\"", "id=\"moduleHelpBtn\"", "id=\"optimizeBtn\"", "id=\"portalNavToggle\""):
            self.assertIn(entry, html, f"missing help entry {entry}")

    def test_portal_script_load_order(self):
        html = self._html()
        scripts = re.findall(r'<script\s+src="([^"]+)"', html)
        expected = [
            "/vendor/petite-vue.iife.js",
            "/js/portal-api.js",
            "/js/portal-utils.js",
            "/js/portal-shell.js",
            "/app.js",
            "/js/portal-enhancements.js",
            "/js/portal-module-registry.js",
        ]
        for src in expected:
            self.assertIn(src, scripts, f"missing script {src}")
        positions = [scripts.index(src) for src in expected]
        self.assertEqual(positions, sorted(positions), "portal scripts are loaded in the wrong order")


    def test_navigation_collapse_contract(self):
        html = self._html()
        shell = (ROOT / "portal" / "static" / "js" / "portal-shell.js").read_text(encoding="utf-8")
        self.assertIn("portal_nav_collapsed", shell)
        self.assertIn("is-collapsed", shell)
        self.assertIn("portal-nav__toggle", html)

    def test_mobile_and_desktop_tabs_consistent(self):
        html = self._html()
        nav_tabs = re.findall(r'class="app-tab[^"]*"[^>]*data-tab="([^"]+)"', html)
        mobile = re.search(r'<select id="mobileAppSelect".*?</select>', html, flags=re.S)
        self.assertIsNotNone(mobile, "mobileAppSelect should exist")
        mobile_values = re.findall(r'<option value="([^"]+)"', mobile.group(0))
        self.assertEqual(sorted(set(nav_tabs)), sorted(set(mobile_values)))


if __name__ == "__main__":
    unittest.main()