from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LocalGatewayConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module(
            "local_gateway_config_test", ROOT / "shared" / "local_gateway.py"
        )

    def test_config_file_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "local_ai.env"
            config.write_text(
                'AIPORT_BASE_URL="http://model-machine.local:8801"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(self.module.os.environ, {}, clear=True), \
                    mock.patch.object(self.module, "CONFIG_PATH", config):
                self.module.load_config()
                self.assertEqual(
                    self.module.configured_url(),
                    "http://model-machine.local:8801",
                )

    def test_explicit_environment_wins_over_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "local_ai.env"
            config.write_text(
                "AIPORT_BASE_URL=http://model-machine.local:8801\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                self.module.os.environ,
                {"AIPORT_BASE_URL": "http://192.168.1.50:8801"},
                clear=True,
            ), mock.patch.object(self.module, "CONFIG_PATH", config):
                self.assertEqual(
                    self.module.configured_url(),
                    "http://192.168.1.50:8801",
                )

    def test_invalid_url_falls_back_to_default(self):
        with mock.patch.dict(
            self.module.os.environ,
            {"AIPORT_BASE_URL": "not a valid url"},
            clear=True,
        ):
            self.assertEqual(self.module.configured_url(), self.module.DEFAULT_URL)

    def test_force_ipv4_resolves_hostname(self):
        with mock.patch.object(
            self.module.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.168.1.50", 8801))],
        ):
            self.assertEqual(
                self.module.force_ipv4("http://model-machine.local:8801/api"),
                "http://192.168.1.50:8801/api",
            )

    def test_probe_uses_resolved_health_url(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        response = Response()

        with mock.patch.dict(
            self.module.os.environ,
            {"AIPORT_BASE_URL": "http://192.168.1.50:8801"},
            clear=True,
        ), mock.patch.object(
            self.module.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            ok, error = self.module.probe(timeout=0.1)
            self.assertTrue(ok)
            self.assertEqual(error, "")
            self.assertTrue(
                urlopen.call_args[0][0].startswith(
                    "http://192.168.1.50:8801/api/modules"
                )
            )


    def test_all_consumers_use_shared_local_gateway(self):
        for relative in (
            "seedance/app.py",
            "nano-banana/app.py",
            "volcengine-portrait/app.py",
            "director/app.py",
            "portal/app.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("from shared import local_gateway", source, relative)

    def test_launchers_reference_machine_local_env_file(self):
        for relative in (
            "Start All.bat",
            "Start All.command",
            "portal_watchdog.ps1",
            "local_ai_backends_watchdog.ps1",
            "deploy/ai-portal.service",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("local_ai.env", source, relative)

if __name__ == "__main__":
    unittest.main()