from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "portal" / "static" / "index.html"
SHELL = ROOT / "portal" / "static" / "js" / "portal-shell.js"
ENHANCEMENTS = ROOT / "portal" / "static" / "js" / "portal-enhancements.js"
STYLES = ROOT / "portal" / "static" / "styles.css"


class PortalStateContractTests(unittest.TestCase):
    def test_prompt_optimizer_footer_entry_stays_removed(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertNotIn('id="optimizeBtn"', html)

    def test_navigation_has_one_activation_path(self):
        source = SHELL.read_text(encoding="utf-8")
        self.assertIn("new CustomEvent('portal:tabchange'", source)
        self.assertNotIn("portalHomeBtn.addEventListener", source)
        self.assertEqual(
            source.count("btn.addEventListener('click', () => activatePortalTab(btn))"),
            1,
        )

    def test_job_status_uses_one_controller_and_poll(self):
        source = ENHANCEMENTS.read_text(encoding="utf-8")
        self.assertEqual(source.count("// === Portal job status controller ==="), 1)
        self.assertEqual(source.count("setInterval(refresh, POLL_MS)"), 1)
        self.assertEqual(source.count("/api/platform/history?limit=200&days=30"), 1)
        self.assertEqual(source.count("/api/platform/queue"), 1)
        self.assertNotIn("// === Running-task indicators", source)
        self.assertNotIn("// === RedCraft nav badge v", source)

    def test_unread_acknowledgement_rules_are_explicit(self):
        source = ENHANCEMENTS.read_text(encoding="utf-8")
        self.assertIn("setTimeout(() =>", source)
        self.assertIn("}, 2000));", source)
        self.assertIn("['pointerdown', 'keydown', 'input', 'change', 'submit']", source)
        self.assertIn("interactionFrames = new WeakSet()", source)
        self.assertIn("if (!interactionFrames.has(iframe))", source)
        self.assertNotIn("activeCount ? String(activeCount) : (tone ? '!' : '')", source)
    def test_home_navigation_visual_contract(self):
        styles = STYLES.read_text(encoding="utf-8")
        self.assertRegex(
            styles,
            r"(?s)\.portal-nav__topline \.portal-nav__home.*?font-size:\s*28px\s*!important;.*?font-weight:\s*650\s*!important;.*?letter-spacing:\s*\.1em\s*!important;",
        )
    def test_home_shell_has_one_panel_rule(self):
        styles = STYLES.read_text(encoding="utf-8")
        self.assertEqual(
            len(re.findall(r"^body\.portal-home-active \.portal-home-panel\s*\{", styles, re.M)),
            1,
        )
    def test_status_badge_has_one_component_definition(self):
        styles = STYLES.read_text(encoding="utf-8")
        self.assertEqual(
            len(re.findall(r"^\.portal-nav__badges\s+\.portal-nav__badge--status\s*\{", styles, re.M)),
            1,
        )


if __name__ == "__main__":
    unittest.main()