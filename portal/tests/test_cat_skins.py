from __future__ import annotations

from datetime import date
import json
import tempfile
import unittest
from pathlib import Path
import sys

PORTAL = Path(__file__).resolve().parents[1]
if str(PORTAL) not in sys.path:
    sys.path.insert(0, str(PORTAL))

from cat_skins.service import CatDailyLimitError, CatGenerationError, CatSkinGenerator, CatSkinManager
from cat_skins.validate_skin import validate_data


CLASSIC_PATH = PORTAL / "cat_skins" / "classic-black-v1.json"


class FakeGenerator:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def provider_info(self):
        return {"provider": "fake", "model": "test", "configured": True}

    def generate(self):
        self.calls += 1
        if self.fail:
            raise CatGenerationError("test failure")
        skin = json.loads(CLASSIC_PATH.read_text("utf-8"))
        skin["name"] = f"测试猫{self.calls}"
        return skin


class CatSkinGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.generator = CatSkinGenerator(PORTAL / "cat_skins", key_loader=lambda: "")

    def test_palette_keeps_required_semantic_codes(self):
        raw = {chr(ord("A") + i): "#123456" for i in range(10)}
        palette = self.generator._valid_palette(raw, self.generator.classic["palette"], 6)
        self.assertEqual({"O", "F", "I", "P", "N", "S"}, set(palette))

    def test_invalid_ai_choices_are_sanitized_to_controlled_enums(self):
        import random
        design = self.generator._sanitize_design(
            {"name": "云尾猫", "head": "dragon", "body": "slim", "tail": "bad", "ears": "tufted", "pattern": "point", "accessory": "laser"},
            "common", "blue_gray", random.Random(4),
        )
        self.assertIn(design["head"], self.generator._DESIGN_OPTIONS["head"])
        self.assertEqual("slim", design["body"])
        self.assertIn(design["tail"], self.generator._DESIGN_OPTIONS["tail"])
        self.assertEqual("none", design["accessory"])

    def test_common_and_rare_never_receive_large_accessories(self):
        import random
        for rarity, theme in (("common", "orange_tabby"), ("rare", "ragdoll")):
            for seed in range(20):
                design = self.generator._random_design(rarity, theme, random.Random(seed))
                self.assertEqual("none", design["accessory"])

    def test_controlled_component_combinations_are_valid(self):
        import random
        cases = [
            ("common", "orange_tabby"), ("rare", "ragdoll"),
            ("epic", "angel"), ("epic", "spider_hero"),
            ("legendary", "celestial_king"), ("legendary", "seraph"),
        ]
        for rarity, theme in cases:
            for seed in range(18):
                rng = random.Random(f"{rarity}-{theme}-{seed}")
                design = self.generator._random_design(rarity, theme, rng)
                skin = self.generator._assemble_design(rarity, theme, design, rng)
                self.assertEqual([], validate_data(skin), (rarity, theme, design))
                self.assertEqual(design, skin["design_recipe"])
                self.assertEqual(16, len(skin["frames"]["a"]))
                self.assertTrue(all(len(row) == 16 for row in skin["frames"]["a"]))

    def test_different_component_recipes_change_real_pixels(self):
        import random
        first = {"name":"短尾猫", "head":"round", "body":"balanced", "tail":"classic_up", "ears":"classic", "pattern":"solid", "accessory":"none"}
        second = {"name":"绒尾猫", "head":"fluffy", "body":"fluffy", "tail":"long_curl", "ears":"tufted", "pattern":"tabby", "accessory":"none"}
        skin_a = self.generator._assemble_design("common", "blue_gray", first, random.Random(1))
        skin_b = self.generator._assemble_design("common", "blue_gray", second, random.Random(1))
        self.assertNotEqual(skin_a["frames"]["a"], skin_b["frames"]["a"])
        self.assertNotEqual(self.generator._silhouette(skin_a["frames"]), self.generator._silhouette(skin_b["frames"]))

    def test_generate_works_without_ai_key(self):
        import random
        skin = self.generator.generate(random.Random(7))
        self.assertEqual([], validate_data(skin))
        self.assertIn("design_recipe", skin)


class CatSkinManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "cat_skins.json"
        self.generator = FakeGenerator()
        self.manager = CatSkinManager(
            self.state_path, CLASSIC_PATH, self.generator,
            today_fn=lambda: date(2026, 8, 28),
        )
        self.user_a = {"user_id": "a", "username": "alice", "role": "user"}
        self.user_b = {"user_id": "b", "username": "bob", "role": "user"}
        self.admin = {"user_id": "admin", "username": "admin", "role": "admin"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_classic_reference_is_valid(self):
        skin = json.loads(CLASSIC_PATH.read_text("utf-8"))
        self.assertEqual([], validate_data(skin))

    def test_regular_user_only_once_per_day(self):
        self.manager.open_gift(self.user_a)
        with self.assertRaises(CatDailyLimitError):
            self.manager.open_gift(self.user_a)
        self.assertEqual(1, self.generator.calls)
        self.assertFalse(self.manager.wardrobe(self.user_a)["can_open"])

    def test_admin_can_open_repeatedly(self):
        first = self.manager.open_gift(self.admin)
        second = self.manager.open_gift(self.admin)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(2, self.generator.calls)
        self.assertTrue(self.manager.wardrobe(self.admin)["can_open"])

    def test_failed_generation_does_not_consume_chance(self):
        self.generator.fail = True
        with self.assertRaises(CatGenerationError):
            self.manager.open_gift(self.user_a)
        self.assertTrue(self.manager.wardrobe(self.user_a)["can_open"])
        self.generator.fail = False
        self.manager.open_gift(self.user_a)
        self.assertFalse(self.manager.wardrobe(self.user_a)["can_open"])

    def test_wardrobes_are_isolated_and_foreign_skin_cannot_be_equipped(self):
        skin = self.manager.open_gift(self.user_a)
        ids_a = {item["id"] for item in self.manager.wardrobe(self.user_a)["skins"]}
        ids_b = {item["id"] for item in self.manager.wardrobe(self.user_b)["skins"]}
        self.assertIn(skin["id"], ids_a)
        self.assertNotIn(skin["id"], ids_b)
        with self.assertRaises(KeyError):
            self.manager.equip(self.user_b, skin["id"])

    def test_builtin_test_skins_are_visible_and_equippable(self):
        wardrobe = self.manager.wardrobe(self.user_a)
        ids = {item["id"] for item in wardrobe["skins"]}
        self.assertTrue({"classic-black", "banana-milk", "midnight-nebula", "spider-hero", "little-angel", "golden-king"}.issubset(ids))
        result = self.manager.equip(self.user_a, "little-angel")
        self.assertEqual("little-angel", result["equipped_skin_id"])
        self.assertEqual("little-angel", self.manager.wardrobe(self.user_a)["equipped_skin_id"])

    def test_catalog_skins_all_pass_validation(self):
        catalog = json.loads((PORTAL / "cat_skins" / "catalog-v1.json").read_text("utf-8"))
        self.assertGreaterEqual(len(catalog), 6)
        for skin in catalog:
            self.assertEqual([], validate_data(skin), skin["id"])

    def test_state_contains_no_api_key(self):
        self.manager.open_gift(self.user_a)
        raw = self.state_path.read_text("utf-8")
        self.assertNotIn("api_key", raw.lower())
        self.assertNotIn("authorization", raw.lower())

    def test_generated_skin_can_be_released_and_equipped_falls_back(self):
        skin = self.manager.open_gift(self.user_a)
        result = self.manager.release(self.user_a, skin["id"])
        self.assertEqual("classic-black", result["equipped_skin_id"])
        wardrobe = self.manager.wardrobe(self.user_a)
        self.assertNotIn(skin["id"], {item["id"] for item in wardrobe["skins"]})
        self.assertEqual("classic-black", wardrobe["equipped_skin_id"])

    def test_builtin_skin_cannot_be_released(self):
        with self.assertRaises(PermissionError):
            self.manager.release(self.user_a, "classic-black")

    def test_other_users_cat_cannot_be_released(self):
        skin = self.manager.open_gift(self.user_a)
        with self.assertRaises(KeyError):
            self.manager.release(self.user_b, skin["id"])

    def test_wardrobe_marks_only_generated_skins_releasable(self):
        generated = self.manager.open_gift(self.user_a)
        by_id = {skin["id"]: skin for skin in self.manager.wardrobe(self.user_a)["skins"]}
        self.assertFalse(by_id["classic-black"]["releasable"])
        self.assertTrue(by_id[generated["id"]]["releasable"])


if __name__ == "__main__":
    unittest.main()
