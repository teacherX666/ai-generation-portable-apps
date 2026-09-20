from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LocalModelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nano = load_module(
            "nano_local_model_integration_test",
            ROOT / "nano-banana" / "app.py",
        )

    def test_zimage_text_mode_maps_to_t2i(self):
        attempt = self.nano._prepare_model_attempt(
            {"mode": "text2img"},
            "zimage_multifunction",
            {},
        )
        self.assertEqual(attempt["zimage_mode"], "t2i")

    def test_zimage_image_mode_maps_to_img2img(self):
        attempt = self.nano._prepare_model_attempt(
            {"mode": "img2img"},
            "zimage_multifunction",
            {"image_1": ("source.png", b"image")},
        )
        self.assertEqual(attempt["zimage_mode"], "img2img")

    def test_portal_health_and_status_ui_are_wired(self):
        portal = (ROOT / "portal" / "app.py").read_text(encoding="utf-8")
        watchdog = (ROOT / "portal_watchdog.ps1").read_text(encoding="utf-8")
        html = (ROOT / "portal" / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "portal" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"/healthz"', portal)
        self.assertIn("/healthz", watchdog)
        self.assertIn("--fail", watchdog)
        self.assertIn("本地模型服务", html)
        self.assertIn("localGateway = res.local_gateway", app_js)

    def test_local_submitters_use_idempotency_keys_and_cancel_timeouts(self):
        checks = (
            ROOT / "nano-banana" / "app.py",
            ROOT / "seedance" / "app.py",
            ROOT / "volcengine-portrait" / "app.py",
        )
        for path in checks:
            source = path.read_text(encoding="utf-8")
            self.assertIn("job_request_id", source, str(path))
            self.assertIn("request_id", source, str(path))
            self.assertIn("/jobs/{local_job_id}/cancel", source, str(path))


if __name__ == "__main__":
    unittest.main()