"""Admin-only AI cat generation experiment service.

The experiment is deliberately side-effect free: it never writes wardrobe state
and therefore never consumes a user's daily gift opportunity.  Administrators
pick a rarity and type a concept name such as 张雪峰猫; the AI designs the cat
on the frozen classic-black-master-v1 body.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path


RARITY_LABELS = {"common": "普通", "rare": "稀有", "epic": "史诗", "legendary": "传说"}


class CatExperimentService:
    def __init__(self, root: Path, generator=None):
        self.root = root
        self.generator = generator
        self.classic = json.loads((root / "classic-black-v1.json").read_text("utf-8"))
        self.anatomy = json.loads((root / "cat-anatomy-v1.json").read_text("utf-8"))

    @staticmethod
    def _clean_name(value: object) -> str:
        name = re.sub(r"[\s《》【】\[\]（）()：:，,。.!！?？‘’“”\-—_]+", "", str(value or ""))
        if not name:
            return ""
        if not name.endswith("猫"):
            name = name + "猫"
        return name if 2 <= len(name) <= 10 else ""

    def config(self) -> dict:
        configured = bool(self.generator is not None and self.generator.provider_info()["configured"])
        return {
            "ok": True,
            "template_id": "classic-black-master-v1",
            "rarities": [{"value": rarity, "label": label} for rarity, label in RARITY_LABELS.items()],
            "configured": configured,
            "note": "管理员自定义名称，AI 按固定猫体设计；不写入衣柜，不消耗每日机会。",
        }

    def generate(self, rarity: str, name: str, seed: int | None = None) -> dict:
        if rarity not in RARITY_LABELS:
            raise KeyError("未知稀有度")
        cleaned = self._clean_name(name)
        if not cleaned:
            raise ValueError("猫名需为 2-10 个汉字")
        if self.generator is None:
            raise RuntimeError("生成服务未初始化")

        seed = int(seed if seed is not None else random.SystemRandom().randrange(1, 2**31))
        rng = random.Random(seed)
        concept = {
            "id": "custom_" + hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:12],
            "name": cleaned,
            "name_locked": True,
            "category": "hot",
            "pattern": "complex",
            "visual_anchors": [],
            "source_name": "管理员自定义实验",
            "source_title": cleaned,
        }

        fallback_used = False
        if self.generator.provider_info()["configured"]:
            try:
                skin = self.generator._generate_from_concept(rarity, concept, rng, set())
            except Exception as exc:
                print(f"  [cat-experiment] AI generation failed: {exc}", flush=True)
                skin = self.generator._fallback_for_concept(rarity, concept, rng, set())
                fallback_used = True
        else:
            skin = self.generator._fallback_for_concept(rarity, concept, rng, set())
            fallback_used = True

        recipe = skin.get("design_recipe") or {}
        return {
            "ok": True,
            "rarity": rarity,
            "rarity_label": RARITY_LABELS[rarity],
            "name": cleaned,
            "seed": seed,
            "skin": skin,
            "design_gene": {
                "rarity": rarity,
                "pattern_family": skin.get("pattern_type", "complex"),
                "accessory_family": recipe.get("accessory_zone", "none"),
                "generation_fallback": fallback_used or bool(skin.get("generation_fallback")),
            },
            "operations": {
                "pattern": recipe.get("pattern_operations", []),
                "accessory": recipe.get("accessory_operations", []),
            },
            "validation": {"passed": True, "errors": []},
            "persisted": False,
            "consumed_daily_chance": False,
            "generation_fallback": fallback_used or bool(skin.get("generation_fallback")),
        }
