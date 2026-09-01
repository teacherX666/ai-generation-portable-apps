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

from cat_skins.experiment import CatExperimentService
from cat_skins.service import (
    CatDailyLimitError, CatDailyTaskRequiredError, CatGenerationError,
    CatSkinGenerator, CatSkinManager,
)
from cat_skins.validate_skin import validate_data
from cat_skins.validate_master_template import validate_master


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
                self.assertEqual(design, {key: skin["design_recipe"][key] for key in design})
                self.assertEqual("classic-black-master-v1", skin["design_recipe"]["template"])
                self.assertEqual(16, len(skin["frames"]["a"]))
                self.assertTrue(all(len(row) == 16 for row in skin["frames"]["a"]))

    def test_different_component_recipes_change_real_pixels(self):
        import random
        first = {"name":"短尾猫", "head":"round", "body":"balanced", "tail":"classic_up", "ears":"classic", "pattern":"solid", "accessory":"none"}
        second = {"name":"绒尾猫", "head":"fluffy", "body":"fluffy", "tail":"long_curl", "ears":"tufted", "pattern":"tabby", "accessory":"none"}
        skin_a = self.generator._assemble_design("common", "blue_gray", first, random.Random(1))
        skin_b = self.generator._assemble_design("common", "blue_gray", second, random.Random(1))
        self.assertNotEqual(skin_a["frames"]["a"], skin_b["frames"]["a"])
        self.assertEqual(self.generator._silhouette(skin_a["frames"]), self.generator._silhouette(skin_b["frames"]))
        self.assertEqual(self.generator._silhouette(self.generator.classic["frames"]), self.generator._silhouette(skin_a["frames"]))


    def test_production_gift_uses_frozen_master_template_without_api_key(self):
        import random
        for seed in range(50):
            skin = self.generator.generate(random.Random(seed))
            self.assertEqual("classic-black-master-v1", skin["design_recipe"]["template"])
            self.assertEqual([], validate_data(skin))
            # Decorations may add silhouette pixels only for epic/legendary;
            # the protected face/body/tail coordinates always match occupancy.
            for cell in self.generator.master["cells"]:
                if cell["permission"] != "locked_occupancy":
                    continue
                x, y = cell["x"], cell["y"]
                self.assertNotEqual(".", skin["frames"]["a"][y][x], (seed, x, y))

    def test_angel_wing_has_dark_outline_and_blue_inner_feathers(self):
        import random
        design = self.generator._random_design("epic", "angel", random.Random(8))
        skin = self.generator._assemble_design("epic", "angel", design, random.Random(8))
        wing_codes = {skin["frames"]["a"][y][x] for x, y in skin["parts"]["wing"]}
        self.assertIn("A", wing_codes)
        self.assertIn("W", wing_codes)
        self.assertNotIn("O", wing_codes)
        self.assertNotEqual(skin["palette"]["A"], skin["palette"]["O"])
        self.assertEqual("#74BDE8", skin["palette"]["W"])
        self.assertGreaterEqual(len(skin["parts"]["wing"]), 12)
        self.assertEqual(
            [{"type": "halo", "pixels": [[9, 0], [10, 0], [11, 0], [12, 0], [13, 0]]}],
            skin["floating_regions"],
        )


    def test_theme_names_are_short_and_chinese_friendly(self):
        from cat_skins.service import RARITY_THEMES
        names = {theme: name for items in RARITY_THEMES.values() for theme, name, _pattern, _colors in items}
        self.assertEqual("蜘蛛侠猫", names["spider_hero"])
        self.assertEqual("天使猫", names["angel"])
        self.assertEqual("国王猫", names["celestial_king"])
        self.assertEqual("HIM猫", names["him"])
        self.assertEqual("胖猫", names["blue_chubby"])


    def test_theme_name_pool_is_short_dynamic_and_preserves_pangmao(self):
        import random
        from cat_skins.service import THEME_NAME_POOLS
        angel_names = {
            self.generator._random_design("epic", "angel", random.Random(seed))["name"]
            for seed in range(24)
        }
        self.assertGreaterEqual(len(angel_names), 3)
        self.assertTrue(all(2 <= len(name) <= 6 for name in angel_names))
        self.assertEqual(("胖猫",), THEME_NAME_POOLS["blue_chubby"])

    def test_solid_variants_change_real_pixels(self):
        import random
        ops = {
            variant: tuple(self.generator._master_pattern_operations("solid", random.Random(1), variant))
            for variant in ("v1", "v2", "v3", "v4")
        }
        self.assertEqual(4, len(set(ops.values())))

    def test_high_rarity_accessories_are_theme_bound(self):
        import random
        expected = {
            "spider_hero": "spider_mask", "arcane_mage": "mage_hat_cape",
            "mecha": "mecha_pack", "him": "none", "angel": "angel_wing",
            "dragon_lord": "dragon_wing", "seraph": "seraph_wing",
        }
        rarity_by_theme = {theme: rarity for rarity, items in __import__('cat_skins.service', fromlist=['RARITY_THEMES']).RARITY_THEMES.items() for theme, *_ in items}
        for theme, accessory in expected.items():
            for seed in range(8):
                design = self.generator._random_design(rarity_by_theme[theme], theme, random.Random(seed))
                self.assertEqual(accessory, design["accessory"], theme)

    def test_large_wing_overlay_keeps_logical_tail_but_records_visual_overlap(self):
        import random
        design = self.generator._random_design("legendary", "seraph", random.Random(8))
        skin = self.generator._assemble_design("legendary", "seraph", design, random.Random(8))
        self.assertEqual([], validate_data(skin))
        self.assertTrue(skin["design_recipe"]["visual_tail_overlaps"])
        self.assertEqual(self.generator.classic["parts"]["tail"], skin["parts"]["tail"])
        self.assertFalse({tuple(p) for p in skin["parts"]["tail"]} & {tuple(p) for p in skin["parts"]["wing"]})

    def test_him_cat_has_separated_solid_white_eyes(self):
        import random
        design = self.generator._random_design("epic", "him", random.Random(3))
        skin = self.generator._assemble_design("epic", "him", design, random.Random(3))
        self.assertEqual([], validate_data(skin))
        for x, y in [*self.generator.anatomy["anatomy"]["eyes"]["left_eye_box"], *self.generator.anatomy["anatomy"]["eyes"]["right_eye_box"]]:
            self.assertEqual("I", skin["frames"]["a"][y][x])

    def test_recent_theme_and_recipe_are_not_repeated(self):
        import random
        generated = []
        for seed in range(30):
            skin = self.generator.generate_with_history(generated, random.Random(seed + 1000))
            signature = self.generator._stored_signature(skin)
            self.assertNotIn(skin["theme"], [item["theme"] for item in generated[-5:]])
            self.assertNotIn(signature, {self.generator._stored_signature(item) for item in generated[-20:]})
            generated.append(skin)

    def test_generate_works_without_ai_key(self):
        import random
        skin = self.generator.generate(random.Random(7))
        self.assertEqual([], validate_data(skin))
        self.assertIn("design_recipe", skin)


class CatExperimentServiceTests(unittest.TestCase):
    def setUp(self):
        self.generator = CatSkinGenerator(PORTAL / "cat_skins", key_loader=lambda: "")
        self.service = CatExperimentService(PORTAL / "cat_skins", self.generator)

    def test_config_exposes_four_rarities(self):
        config = self.service.config()
        self.assertEqual("classic-black-master-v1", config["template_id"])
        self.assertEqual({"common", "rare", "epic", "legendary"}, {item["value"] for item in config["rarities"]})
        self.assertFalse(config["configured"])

    def test_generate_is_side_effect_free_and_locks_name(self):
        result = self.service.generate("epic", "张雪峰猫", 123456)
        self.assertTrue(result["validation"]["passed"], result["validation"]["errors"])
        self.assertFalse(result["persisted"])
        self.assertFalse(result["consumed_daily_chance"])
        self.assertEqual([], validate_data(result["skin"]))
        self.assertEqual("张雪峰猫", result["skin"]["name"])

    def test_name_is_auto_suffixed_with_cat(self):
        result = self.service.generate("rare", "张雪峰", 7)
        self.assertEqual("张雪峰猫", result["skin"]["name"])

    def test_unknown_rarity_is_rejected(self):
        with self.assertRaises(KeyError):
            self.service.generate("unknown", "测试猫", 1)

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.generate("common", "", 1)


class CatSkinManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "cat_skins.json"
        self.generator = FakeGenerator()
        self.task_completed = False
        self.manager = CatSkinManager(
            self.state_path, CLASSIC_PATH, self.generator,
            today_fn=lambda: date(2026, 8, 28),
            task_completed_fn=lambda user, today: self.task_completed,
        )
        self.user_a = {"user_id": "a", "username": "alice", "role": "user"}
        self.user_b = {"user_id": "b", "username": "bob", "role": "user"}
        self.admin = {"user_id": "admin", "username": "admin", "role": "admin"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_classic_reference_is_valid(self):
        skin = json.loads(CLASSIC_PATH.read_text("utf-8"))
        self.assertEqual([], validate_data(skin))

    def test_master_template_covers_every_cell_and_matches_classic(self):
        classic = json.loads(CLASSIC_PATH.read_text("utf-8"))
        master = json.loads((PORTAL / "cat_skins" / "master-template-v1.json").read_text("utf-8"))
        self.assertEqual([], validate_master(master, classic))
        self.assertEqual(256, len(master["cells"]))
        self.assertEqual(256, len({(cell["x"], cell["y"]) for cell in master["cells"]}))

    def test_master_template_protects_face_and_declares_overlay_zones(self):
        master = json.loads((PORTAL / "cat_skins" / "master-template-v1.json").read_text("utf-8"))
        by_xy = {(cell["x"], cell["y"]): cell for cell in master["cells"]}
        for point in ((10,5),(12,5),(11,7),(10,8),(12,8),(9,9),(13,9)):
            self.assertEqual("locked_occupancy", by_xy[point]["permission"], point)
            self.assertTrue(by_xy[point]["final_face_foreground"], point)
        self.assertIn("headwear", by_xy[(11,0)]["overlay_zones"])
        self.assertIn("wing", by_xy[(3,4)]["overlay_zones"])
        self.assertIn("cape", by_xy[(5,10)]["overlay_zones"])

    def test_master_patternable_cells_are_occupied_and_not_face_foreground(self):
        master = json.loads((PORTAL / "cat_skins" / "master-template-v1.json").read_text("utf-8"))
        for cell in master["cells"]:
            if not cell["pattern_allowed"]:
                continue
            self.assertNotEqual(".", cell["base_code"], (cell["x"], cell["y"]))
            self.assertFalse(cell["final_face_foreground"], (cell["x"], cell["y"]))

    def test_second_daily_open_requires_completed_feishu_task(self):
        self.manager.open_gift(self.user_a)
        with self.assertRaises(CatDailyTaskRequiredError):
            self.manager.open_gift(self.user_a)
        wardrobe = self.manager.wardrobe(self.user_a)
        self.assertEqual(1, wardrobe["opens_today"])
        self.assertFalse(wardrobe["daily_task"]["completed"])
        self.assertFalse(wardrobe["can_open"])

    def test_completed_feishu_task_unlocks_second_but_not_third_open(self):
        first = self.manager.open_gift(self.user_a)
        self.task_completed = True
        second = self.manager.open_gift(self.user_a)
        self.assertNotEqual(first["id"], second["id"])
        wardrobe = self.manager.wardrobe(self.user_a)
        self.assertEqual(2, wardrobe["opens_today"])
        self.assertTrue(wardrobe["daily_task"]["completed"])
        self.assertFalse(wardrobe["can_open"])
        with self.assertRaisesRegex(CatDailyLimitError, "今日领取猫咪已达上限"):
            self.manager.open_gift(self.user_a)
        self.assertEqual(2, self.generator.calls)

    def test_admin_can_open_gift_repeatedly_without_daily_limit(self):
        first = self.manager.open_gift(self.admin)
        second = self.manager.open_gift(self.admin)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(2, self.generator.calls)
        wardrobe = self.manager.wardrobe(self.admin)
        self.assertTrue(wardrobe["can_open"])
        self.assertEqual("不限次数", wardrobe["next_open_date"])

    def test_admin_open_does_not_write_daily_limit_date(self):
        self.manager.open_gift(self.admin)
        state = json.loads(self.manager.state_path.read_text("utf-8"))["users"][self.admin["user_id"]]
        self.assertEqual("", state["last_open_date"])

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
        expected_names = {"spider-hero": "蜘蛛侠猫", "little-angel": "天使猫", "golden-king": "国王猫"}
        classic = next(skin for skin in catalog if skin["id"] == "classic-black")
        protected = [(cell["x"], cell["y"]) for cell in json.loads((PORTAL / "cat_skins" / "master-template-v1.json").read_text("utf-8"))["cells"] if cell["permission"] == "locked_occupancy"]
        for skin in catalog:
            self.assertEqual([], validate_data(skin), skin["id"])
            if skin["id"] in expected_names:
                self.assertEqual(expected_names[skin["id"]], skin["name"])
            for x, y in protected:
                self.assertNotEqual(".", skin["frames"]["a"][y][x], (skin["id"], x, y))
            self.assertEqual(classic["parts"]["tail"], skin["parts"]["tail"])

    def test_state_contains_no_api_key(self):
        self.manager.open_gift(self.user_a)
        raw = self.state_path.read_text("utf-8")
        self.assertNotIn("api_key", raw.lower())
        self.assertNotIn("authorization", raw.lower())


    def test_wardrobe_preserves_generated_dynamic_name(self):
        skin = self.manager.open_gift(self.user_a)
        wardrobe_skin = next(item for item in self.manager.wardrobe(self.user_a)["skins"] if item["id"] == skin["id"])
        self.assertEqual(skin["name"], wardrobe_skin["name"])

    def test_pangmao_description_preserves_story_context(self):
        from cat_skins.service import THEME_DESCRIPTIONS
        description = THEME_DESCRIPTIONS["blue_chubby"]
        self.assertIn("蓝色猫头像", description)
        self.assertIn("感情悲剧", description)
        self.assertIn("纪念", description)

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
