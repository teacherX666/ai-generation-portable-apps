import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "portal" / "static" / "index.html"


class PortalNavStructureTests(unittest.TestCase):
    def _read(self):
        return INDEX.read_text(encoding="utf-8")

    def test_nav_and_mobile_selector_expose_the_same_modules(self):
        html = self._read()
        nav_tabs = re.findall(r'class="app-tab[^"]*"[^>]*data-tab="([^"]+)"', html)
        mobile_values = re.findall(r'<select id="mobileAppSelect".*?</select>', html, flags=re.S)
        self.assertTrue(mobile_values, "mobileAppSelect should exist")
        mobile_values = re.findall(r'<option value="([^"]+)"', mobile_values[0])
        self.assertEqual(sorted(set(nav_tabs)), sorted(set(mobile_values)))

    def test_every_nav_tab_has_a_matching_panel(self):
        html = self._read()
        nav_tabs = re.findall(r'class="app-tab[^"]*"[^>]*data-tab="([^"]+)"', html)
        panel_ids = set(re.findall(r'<div class="tab-panel[^"]*"[^>]*id="(tab-[^"]+)"', html))
        for tab in nav_tabs:
            self.assertIn(f"tab-{tab}", panel_ids, f"missing panel for {tab}")

    def test_feishu_agent_is_first_and_active(self):
        html = self._read()
        first = re.search(r'<button[^>]*class="app-tab[^"]*"[^>]*data-tab="([^"]+)"', html)
        self.assertIsNotNone(first)
        self.assertEqual(first.group(1), "feishu-generation-agent")
        self.assertIn("portal-nav__item--featured active", html)

    def test_help_and_prompt_optimizer_entries_exist(self):
        html = self._read()
        self.assertIn('id="helpBtn"', html)
        self.assertIn('id="optimizeBtn"', html)


if __name__ == "__main__":
    unittest.main()