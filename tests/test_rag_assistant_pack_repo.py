# -*- coding: utf-8 -*-
"""源码兜底扫描的只读和密钥禁区回归测试。"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK_PATH = ROOT / "rag-assistant" / "rag_agent" / "self_learn" / "pack_repo.py"
spec = importlib.util.spec_from_file_location("rag_assistant_pack_repo_test", PACK_PATH)
pack_repo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pack_repo
assert spec.loader is not None
spec.loader.exec_module(pack_repo)


class PackRepositorySecurityTests(unittest.TestCase):
    def test_sensitive_json_and_env_files_are_never_packed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("print('safe')\n", encoding="utf-8")
            (root / "secrets.json").write_text('{"deepseek_api_key":"DO_NOT_LEAK"}\n', encoding="utf-8")
            (root / "config_secret.py").write_text("API_KEY = 'DO_NOT_LEAK'\n", encoding="utf-8")
            (root / ".env.production").write_text("API_KEY=DO_NOT_LEAK\n", encoding="utf-8")
            (root / "portal.pem").write_text("PRIVATE CERTIFICATE\n", encoding="utf-8")

            packed = pack_repo.pack_repository(root, max_tokens=10_000)

            self.assertIn('path="app.py"', packed)
            self.assertIn("print('safe')", packed)
            self.assertNotIn("DO_NOT_LEAK", packed)
            self.assertNotIn("secrets.json", packed)
            self.assertNotIn("config_secret.py", packed)
            self.assertNotIn(".env.production", packed)
            self.assertNotIn("portal.pem", packed)

    def test_secret_values_in_source_are_redacted_without_modifying_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.py"
            original = (
                'DEEPSEEK_API_KEY = "sk-live-DO_NOT_LEAK_123456789"\n'
                'config = {"app_secret": "APP_SECRET_DO_NOT_LEAK"}\n'
                'print("safe logic")\n'
            )
            source.write_text(original, encoding="utf-8")

            packed = pack_repo.pack_repository(root, max_tokens=10_000)

            self.assertIn("safe logic", packed)
            self.assertIn("[REDACTED]", packed)
            self.assertNotIn("DO_NOT_LEAK", packed)
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_state_directory_is_never_packed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            (state / "secrets.json").write_text('{"key":"DO_NOT_LEAK"}\n', encoding="utf-8")
            (root / "main.py").write_text("raise RuntimeError('safe')\n", encoding="utf-8")

            packed = pack_repo.pack_repository(root, max_tokens=10_000)

            self.assertIn('path="main.py"', packed)
            self.assertNotIn("DO_NOT_LEAK", packed)
            self.assertNotIn("state/secrets.json", packed)

    def test_symlink_is_not_followed_and_scan_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            outside_path = Path(outside) / "leaked.py"
            outside_path.write_text("OUTSIDE_SECRET\n", encoding="utf-8")
            (root / "app.py").write_text("print('safe')\n", encoding="utf-8")
            os.symlink(outside_path, root / "linked.py")
            before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}

            packed = pack_repo.pack_repository(root, max_tokens=10_000)

            after = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
            self.assertIn('path="app.py"', packed)
            self.assertNotIn("OUTSIDE_SECRET", packed)
            self.assertNotIn("linked.py", packed)
            self.assertEqual(before, after)

    def test_sensitive_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            with self.assertRaises(ValueError):
                pack_repo.pack_repository(state, max_tokens=10_000)


if __name__ == "__main__":
    unittest.main()
