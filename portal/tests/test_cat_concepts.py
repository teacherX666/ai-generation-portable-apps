from __future__ import annotations

import io
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

PORTAL = Path(__file__).resolve().parents[1]
if str(PORTAL) not in sys.path:
    sys.path.insert(0, str(PORTAL))

from cat_skins.concepts import CATEGORY_SPECS, CatConceptStore
from cat_skins.service import CatSkinGenerator
from cat_skins.validate_skin import validate_data


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class CatConceptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seed_path = self.root / "seeds.json"
        self.state_path = self.root / "state" / "trends.json"
        self.seeds = [
            {"id": "breed_a", "name": "甲猫", "category": "breed", "visual_anchors": ["甲"]},
            {"id": "abstract_b", "name": "乙猫", "category": "abstract", "visual_anchors": ["乙"]},
        ]
        self.seed_path.write_text(json.dumps(self.seeds, ensure_ascii=False), "utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_choose_category_and_category_concept(self):
        store = CatConceptStore(self.seed_path, self.state_path)
        self.assertEqual(2, store.status()["seed_count"])
        rng = random.Random(2)
        category = store.choose_category("common", rng)
        self.assertIn(category, CATEGORY_SPECS)
        concept = store.category_concept("breed", set(), rng)
        self.assertIsNotNone(concept)
        self.assertEqual("breed", concept["category"])
        self.assertTrue(concept["name"].endswith("猫"))

    def test_extract_common_douyin_shapes(self):
        rows = CatConceptStore._extract_topic_rows({"data": {"word_list": [
            {"sentence": "新热点", "sentence_id": "88", "rank": 3}, "另一个热点"
        ]}})
        self.assertEqual(["新热点", "另一个热点"], [row["title"] for row in rows])
        self.assertEqual(3, rows[0]["rank"])

    def test_exclude_serious_filters_disaster_and_politics(self):
        topics = [
            {"title": "西藏泥石流已致16死", "rank": 1},
            {"title": "国安部披露间谍案", "rank": 2},
            {"title": "某明星新歌发布", "rank": 3},
            {"title": "开学与家人临别那一刻", "rank": 4},
            {"title": "8月制造业PMI上升", "rank": 5},
        ]
        store = CatConceptStore(self.seed_path, self.state_path)
        titles = [topic["title"] for topic in store._filter_topics(topics)]
        self.assertNotIn("西藏泥石流已致16死", titles)
        self.assertNotIn("国安部披露间谍案", titles)
        self.assertNotIn("8月制造业PMI上升", titles)
        self.assertIn("某明星新歌发布", titles)
        self.assertIn("开学与家人临别那一刻", titles)

    def test_topic_filter_is_applied_before_conversion(self):
        payload = {"data": {"word_list": [
            {"sentence": "娱乐梗A", "sentence_id": "a", "rank": 1},
            {"sentence": "无聊日常B", "sentence_id": "b", "rank": 2},
        ]}}
        store = CatConceptStore(
            self.seed_path, self.state_path,
            opener=lambda *_a, **_k: FakeResponse(payload),
            topic_filter=lambda topics: [topics[0]],
        )
        with mock.patch.dict(os.environ, {"CAT_TREND_FEED_URL": "https://feed.test/hot"}, clear=False):
            result = store.refresh()
        self.assertTrue(result["ok"])
        saved = json.loads(self.state_path.read_text("utf-8"))
        self.assertEqual(1, len(saved["hot_concepts"]))
        self.assertEqual("娱乐梗A", saved["hot_concepts"][0]["source_title"])

    def test_common_and_rare_prefer_breed_category(self):
        store = CatConceptStore(self.seed_path, self.state_path)
        rng = random.Random(7)
        common_picks = [store.choose_category("common", rng) for _ in range(500)]
        self.assertGreater(common_picks.count("breed") / len(common_picks), 0.60)
        rare_picks = [store.choose_category("rare", rng) for _ in range(500)]
        self.assertGreater(rare_picks.count("breed") / len(rare_picks), 0.40)

    def test_topic_concept_keeps_provenance_without_review_field(self):
        concept = CatConceptStore._topic_to_concept({
            "title": "悲情事件成为热梗", "rank": 1,
            "raw": {"sentence_id": "123", "share_url": "https://example.test/topic"},
        }, "2026-08-31T12:00:00+08:00")
        self.assertTrue(concept["name"].endswith("猫"))
        self.assertEqual("悲情事件成为热梗", concept["source_title"])
        self.assertEqual("123", concept["source_id"])
        self.assertNotIn("review", concept)
        self.assertNotIn("approved", concept)

    def test_topic_concept_uses_ai_cat_name_when_valid(self):
        concept = CatConceptStore._topic_to_concept({
            "title": "开学与家人临别那一刻", "cat_name": "离乡猫",
            "raw": {"sentence_id": "456"},
        }, "2026-08-31T12:00:00+08:00")
        self.assertEqual("离乡猫", concept["name"])

    def test_topic_concept_ignores_invalid_ai_cat_name(self):
        concept = CatConceptStore._topic_to_concept({
            "title": "开学与家人临别那一刻", "cat_name": "离乡猫咪咪喵",
            "raw": {"sentence_id": "789"},
        }, "2026-08-31T12:00:00+08:00")
        self.assertNotEqual("离乡猫咪咪喵", concept["name"])
        self.assertTrue(concept["name"].endswith("猫"))

    def test_refresh_writes_source_and_failure_preserves_old_topics(self):
        payload = {"data": {"list": [{"sentence": "今日新梗", "sentence_id": "hot-1", "rank": 1}]}}
        store = CatConceptStore(self.seed_path, self.state_path, opener=lambda *_a, **_k: FakeResponse(payload))
        with mock.patch.dict(os.environ, {"CAT_TREND_FEED_URL": "https://feed.test/hot"}, clear=False):
            result = store.refresh()
        self.assertTrue(result["ok"])
        saved = json.loads(self.state_path.read_text("utf-8"))
        self.assertEqual("今日新梗", saved["hot_concepts"][0]["source_title"])
        store.opener = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("offline"))
        with mock.patch.dict(os.environ, {"CAT_TREND_FEED_URL": "https://feed.test/hot"}, clear=False):
            result = store.refresh()
        self.assertFalse(result["ok"])
        after = json.loads(self.state_path.read_text("utf-8"))
        self.assertEqual("今日新梗", after["hot_concepts"][0]["source_title"])

    def test_unconfigured_status_does_not_remove_local_seeds(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            store = CatConceptStore(self.seed_path, self.state_path)
            self.assertFalse(store.status()["configured"])
            self.assertIsNotNone(store.choose("epic", set(), random.Random(1)))


class OpenConceptGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        seed_path = root / "seeds.json"
        seed_path.write_text(json.dumps([{
            "id": "meme_pangmao", "name": "胖猫", "category": "hot",
            "name_locked": True, "pattern": "spotted",
            "source_title": "胖猫", "source_id": "pangmao",
            "visual_anchors": ["蓝色猫头像意象", "悲情黑色幽默"],
        }], ensure_ascii=False), "utf-8")
        self.store = CatConceptStore(seed_path, root / "trends.json")
        self.generator = CatSkinGenerator(PORTAL / "cat_skins", key_loader=lambda: "test", concept_store=self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_ai_open_concept_uses_frozen_template_and_ignores_full_frame(self):
        # A hostile old-format full frame must be ignored. Only allowed paint
        # coordinates may change semantic color codes on the frozen cat body.
        concept = {
            "id": "test_breed", "name": "橘猫", "category": "breed",
            "pattern": "complex", "visual_anchors": ["橘色"], "source_name": "测试",
        }
        raw = {
            "name": "坏猫",
            "palette": {"O":"#17233A", "F":"#3284D6", "I":"#F2E45C", "P":"#101218", "N":"#E77286", "S":"#A9D8FF"},
            "frame_a": ["AAAAAAAAAAAAAAAA"] * 16,
            "paint": [[8, 3, "S"], [9, 3, "S"], [0, 0, "S"], [1, 6, "S"], [10, 15, "S"]],
            "accessory": {"zone": "wing", "pixels": [[0, 1, "S"]]},
            "effect": "shadow",
        }
        self.generator._chat = lambda _messages: json.dumps(raw, ensure_ascii=False)
        skin = self.generator._generate_from_concept("common", concept, random.Random(7), set())
        classic = self.generator.classic
        self.assertEqual("坏猫", skin["name"])
        self.assertEqual("test_breed", skin["concept_id"])
        self.assertEqual("测试", skin["concept_source"]["source_name"])
        self.assertEqual("classic-black-master-v1", skin["design_recipe"]["template"])
        self.assertEqual("ai-coordinate-plan", skin["design_recipe"]["generator"])
        self.assertEqual([[8, 3, "S"], [9, 3, "S"]], skin["design_recipe"]["pattern_operations"])
        self.assertEqual([], skin["design_recipe"]["accessory_operations"])
        # Common cats cannot add background pixels, move the tail, alter gait,
        # or replace any occupied cell with transparency.
        for frame in ("a", "b"):
            for y in range(16):
                for x in range(16):
                    before = classic["frames"][frame][y][x]
                    after = skin["frames"][frame][y][x]
                    self.assertEqual(before == ".", after == ".", (frame, x, y))
        for x, y in classic["parts"]["tail"]:
            self.assertEqual(classic["frames"]["a"][y][x], skin["frames"]["a"][y][x])
        self.assertEqual([], validate_data(skin))

    def test_epic_accessory_is_filtered_to_declared_zone_and_face_stays_readable(self):
        concept = self.store.all_concepts()[0]
        raw = {
            "name": "胖猫",
            "palette": {"O":"#17233A", "F":"#3284D6", "I":"#F2E45C", "P":"#101218", "N":"#E77286", "S":"#A9D8FF", "A":"#E8D56A", "W":"#74BDE8"},
            "paint": [[8, 3, "S"]],
            "accessory": {"zone": "wing", "pixels": [[4, 6, "A"], [5, 7, "A"], [6, 7, "W"], [1, 6, "A"], [11, 7, "A"]]},
            "effect": "halo",
        }
        skin = self.generator._render_template_concept("epic", concept, raw, random.Random(2), set())
        self.assertEqual([[4, 6, "A"], [5, 7, "A"], [6, 7, "W"]], skin["design_recipe"]["accessory_operations"])
        for part in ("eyes", "pupils", "nose", "mouth", "chin"):
            for x, y in self.generator.classic["parts"][part]:
                self.assertEqual(self.generator.classic["frames"]["a"][y][x], skin["frames"]["a"][y][x])
        self.assertEqual([], validate_data(skin))

    def test_all_black_ai_palette_is_harmonized_and_readable(self):
        concept = self.store.all_concepts()[0]
        raw = {
            "name": "胖猫",
            "palette": {key: "#000000" for key in ("O", "F", "I", "P", "N", "S")},
            "paint": [[8, 3, "S"], [9, 3, "S"]],
            "accessory": {"zone": "none", "pixels": []},
        }
        skin = self.generator._render_template_concept("common", concept, raw, random.Random(3), set())
        palette = skin["palette"]
        self.assertGreaterEqual(len(set(palette.values())), 5)
        self.assertNotEqual(palette["O"], palette["F"])
        self.assertNotEqual(palette["I"], palette["P"])
        self.assertNotEqual(palette["F"], palette["S"])
        outline_l = self.generator._hex_to_hls(palette["O"])[1]
        fur_l = self.generator._hex_to_hls(palette["F"])[1]
        self.assertGreater(fur_l - outline_l, 0.18)
        self.assertEqual([], validate_data(skin))

    def test_same_bad_palette_gets_concept_specific_harmony(self):
        first = self.store.all_concepts()[0]
        second = dict(first, id="abstract_sunrise", name="日出猫", category="abstract")
        raw = {"palette": {key: "#1A1A1A" for key in ("O", "F", "I", "P", "N", "S")}}
        left = self.generator._harmonize_concept_palette(raw["palette"], self.generator.classic["palette"], 6, first)
        right = self.generator._harmonize_concept_palette(raw["palette"], self.generator.classic["palette"], 6, second)
        self.assertNotEqual(left["F"], right["F"])
        self.assertNotEqual(left["S"], right["S"])

    def test_visual_anchor_colors_override_bad_ai_palette(self):
        bad = {key: "#000000" for key in ("O", "F", "I", "P", "N", "S", "A", "W")}
        cases = {
            "bombay": {
                "id": "breed_bombay", "name": "孟买猫", "category": "breed",
                "visual_anchors": ["漆黑短毛", "铜金色眼睛", "小黑豹意象"],
            },
            "mau": {
                "id": "breed_egyptian_mau", "name": "埃及猫", "category": "breed",
                "visual_anchors": ["天然点状斑纹", "额头圣甲虫纹意象", "绿色眼睛"],
            },
            "van": {
                "id": "breed_turkish_van", "name": "土耳其梵猫", "category": "breed",
                "visual_anchors": ["白色主体", "头顶和尾巴有色块", "喜水意象"],
            },
            "snowshoe": {
                "id": "breed_snowshoe", "name": "雪鞋猫", "category": "breed",
                "visual_anchors": ["深色重点面罩", "四只白袜", "蓝色眼睛"],
            },
            "banana": {
                "id": "meme_banana", "name": "香蕉猫", "category": "hot",
                "visual_anchors": ["明黄色香蕉意象", "夸张悲伤或呆滞表情", "黄色粒子"],
            },
            "gpu": {
                "id": "object_graphics_card", "name": "显卡猫", "category": "object",
                "visual_anchors": ["黑灰电路身体", "双风扇斑纹", "RGB霓虹光效"],
            },
            "aurora": {
                "id": "abstract_aurora", "name": "极光猫", "category": "abstract",
                "visual_anchors": ["青绿紫流光色带", "深夜蓝主体", "上扬的光幕尾巴"],
            },
        }
        palettes = {name: self.generator._harmonize_concept_palette(bad, self.generator.classic["palette"], 8, concept) for name, concept in cases.items()}

        def hls(name, role):
            return self.generator._hex_to_hls(palettes[name][role])

        self.assertLess(hls("bombay", "F")[1], 0.32)
        self.assertLess(self.generator._hue_distance(hls("bombay", "I")[0], 0.105), 0.04)
        self.assertLess(self.generator._hue_distance(hls("mau", "I")[0], 0.355), 0.04)
        self.assertGreater(hls("van", "F")[1], 0.82)
        self.assertLess(self.generator._hue_distance(hls("snowshoe", "I")[0], 0.605), 0.04)
        self.assertLess(self.generator._hue_distance(hls("banana", "F")[0], 0.145), 0.04)
        self.assertLess(hls("banana", "S")[2], 0.45)  # 奶油副色，不是高饱和粉色
        self.assertLess(hls("gpu", "F")[2], 0.16)
        self.assertLess(self.generator._hue_distance(hls("gpu", "S")[0], 0.50), 0.04)
        self.assertLess(hls("aurora", "F")[1], 0.32)
        self.assertLess(self.generator._hue_distance(hls("aurora", "S")[0], 0.46), 0.04)
        self.assertLess(self.generator._hue_distance(hls("aurora", "A")[0], 0.78), 0.04)
        for palette in palettes.values():
            self.assertNotEqual(palette["O"], palette["F"])
            self.assertNotEqual(palette["I"], palette["P"])

    def test_ai_concept_validation_rejects_invalid(self):
        generator = self.generator
        ok = generator._validated_ai_concept({
            "name": "星海猫", "category": "abstract", "pattern": "complex",
            "visual_anchors": ["星空蓝主体", "星点花纹"],
        }, set())
        self.assertEqual("星海猫", ok["name"])
        self.assertEqual("AI概念合成", ok["source_name"])
        self.assertTrue(ok["id"].startswith("ai_"))
        self.assertIsNone(generator._validated_ai_concept(
            {"name": "星海", "category": "abstract", "visual_anchors": ["星"]}, set()))
        self.assertIsNone(generator._validated_ai_concept(
            {"name": "星海猫", "category": "unknown", "visual_anchors": ["星"]}, set()))
        self.assertIsNone(generator._validated_ai_concept(
            {"name": "星海猫", "category": "abstract", "visual_anchors": []}, set()))
        self.assertIsNone(generator._validated_ai_concept(
            {"name": "星海猫", "category": "abstract", "visual_anchors": ["星"]}, {"星海猫"}))

    def test_ai_free_concept_uses_model_and_keeps_anchors(self):
        seeds = [{"name": "极光猫", "category": "abstract", "visual_anchors": ["青绿流光"]}]
        self.generator._chat = lambda _messages: json.dumps({
            "name": "星海猫", "category": "abstract", "pattern": "complex",
            "visual_anchors": ["星空蓝主体", "星点花纹"],
        }, ensure_ascii=False)
        concept = self.generator._ai_free_concept(seeds, set())
        self.assertEqual("星海猫", concept["name"])
        self.assertEqual("abstract", concept["category"])
        self.assertEqual(["星空蓝主体", "星点花纹"], concept["visual_anchors"])

    def test_ai_synthesize_returns_none_on_bad_model(self):
        self.generator._chat = lambda _messages: '{"broken": "json"'
        concept = self.generator._ai_synthesize_concept("epic", random.Random(1), set(), set())
        self.assertIsNone(concept)

    def test_king_concept_uses_procedural_crown_not_blob(self):
        concept = {
            "id": "custom_king", "name": "国王猫", "category": "hot",
            "visual_anchors": ["金色皇冠", "红色宝石"],
        }
        raw = {
            "name": "国王猫",
            "palette": {"O": "#17233A", "F": "#3284D6", "I": "#F2E45C", "P": "#101218", "N": "#E77286", "S": "#A9D8FF", "A": "#E8D56A", "W": "#74BDE8"},
            "paint": [],
            "accessory": {"zone": "headwear", "pixels": [
                [5, 0, "A"], [6, 0, "A"], [7, 0, "A"], [8, 0, "A"], [9, 0, "A"], [10, 0, "A"],
                [11, 0, "A"], [12, 0, "A"], [13, 0, "A"], [14, 0, "A"], [15, 0, "A"],
                [5, 1, "A"], [6, 1, "A"], [7, 1, "A"], [8, 1, "A"], [9, 1, "A"], [10, 1, "A"],
            ]},
        }
        skin = self.generator._render_template_concept("legendary", concept, raw, random.Random(1), set())
        ops = skin["design_recipe"]["accessory_operations"]
        top_row = [op for op in ops if op[1] == 0]
        self.assertEqual(3, len(top_row))  # 三个尖顶，而不是一整排糊块
        self.assertEqual("headwear", skin["design_recipe"]["accessory_zone"])
        self.assertEqual([], validate_data(skin))

    def test_headwear_style_resolution_covers_common_types(self):
        resolver = self.generator._resolve_headwear_style
        self.assertEqual("crown", resolver({}, {"name": "国王猫", "visual_anchors": ["金色皇冠"]}))
        self.assertEqual("halo", resolver({}, {"name": "天使猫", "visual_anchors": ["金色光环"]}))
        self.assertEqual("horns", resolver({}, {"name": "恶魔猫", "visual_anchors": ["双角"]}))
        self.assertEqual("cap", resolver({}, {"name": "法师猫", "visual_anchors": ["尖顶法师帽"]}))
        self.assertEqual("crown", resolver({"style": "crown"}, {"name": "随便猫"}))
        self.assertIsNone(resolver({}, {"name": "神秘猫", "visual_anchors": ["无"]}))

    def test_dense_accessory_block_is_deblobbed_to_outline(self):
        ops = [(x, y, "A") for x in range(5, 11) for y in range(0, 4)]
        result = self.generator._deblob_ops(ops)
        coords = {(x, y) for x, y, _ in result}
        self.assertNotIn((7, 1), coords)  # 内部像素被去掉
        self.assertIn((5, 0), coords)  # 边界像素保留

    def test_no_key_still_returns_local_concept_fallback(self):
        generator = CatSkinGenerator(PORTAL / "cat_skins", key_loader=lambda: "", concept_store=self.store)
        skin = generator.generate_with_history([], random.Random(4))
        self.assertTrue(skin["generation_fallback"])
        self.assertTrue(skin["name"].endswith("猫"))
        self.assertEqual([], validate_data(skin))


if __name__ == "__main__":
    unittest.main()
