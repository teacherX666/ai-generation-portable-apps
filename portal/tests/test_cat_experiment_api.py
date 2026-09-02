from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


PORTAL = Path(__file__).resolve().parents[1]
if str(PORTAL) not in sys.path:
    sys.path.insert(0, str(PORTAL))


def load_portal_module(data_dir: str):
    old_data_dir = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = data_dir
    sys.modules.pop("portal_cat_api_test_app", None)
    spec = importlib.util.spec_from_file_location("portal_cat_api_test_app", PORTAL / "app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    if old_data_dir is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = old_data_dir
    return module


class CatExperimentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.mod = load_portal_module(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        sys.modules.pop("portal_cat_api_test_app", None)

    def make_handler(self, body=None):
        handler = self.mod.Handler.__new__(self.mod.Handler)
        handler.responses = []
        handler._json = lambda status, payload: handler.responses.append((status, payload))
        handler._read_json = lambda: body if body is not None else {}
        return handler

    def test_regular_user_cannot_read_experiment_config(self):
        handler = self.make_handler()
        handler._cat_experiment_config({"user_id": "u1", "username": "user", "role": "user"})
        self.assertEqual(403, handler.responses[-1][0])

    def test_regular_user_cannot_generate_experiment_cat(self):
        handler = self.make_handler({"rarity": "epic", "name": "胖猫"})
        handler._cat_experiment_generate({"user_id": "u1", "username": "user", "role": "user"})
        self.assertEqual(403, handler.responses[-1][0])

    def test_admin_generation_is_successful_and_does_not_touch_wardrobe_state(self):
        user = {"user_id": "admin-1", "username": "admin", "role": "admin"}
        before = self.mod.cat_skin_manager.wardrobe(user)
        state_path = self.mod.cat_skin_manager.state_path
        before_bytes = state_path.read_bytes() if state_path.exists() else None

        handler = self.make_handler({"rarity": "epic", "name": "胖猫", "seed": 20260831})
        handler._cat_experiment_generate(user)

        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertTrue(payload["validation"]["passed"])
        self.assertFalse(payload["persisted"])
        self.assertFalse(payload["consumed_daily_chance"])
        after = self.mod.cat_skin_manager.wardrobe(user)
        after_bytes = state_path.read_bytes() if state_path.exists() else None
        self.assertEqual(before["can_open"], after["can_open"])
        self.assertEqual(before["skins"], after["skins"])
        self.assertEqual(before_bytes, after_bytes)

    def test_admin_can_read_experiment_config(self):
        handler = self.make_handler()
        handler._cat_experiment_config({"user_id": "admin-1", "username": "admin", "role": "admin"})
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual("classic-black-master-v1", payload["template_id"])
        self.assertEqual({"common", "rare", "epic", "legendary"}, {item["value"] for item in payload["rarities"]})


if __name__ == "__main__":
    unittest.main()
