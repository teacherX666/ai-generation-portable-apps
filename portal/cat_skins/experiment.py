"""Admin-only Master Template V1 cat generation experiment service.

The experiment is deliberately side-effect free: it never writes wardrobe state
and therefore never consumes a user's daily gift opportunity.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .validate_skin import validate_data


EXPERIMENTS = {
    "orange_tabby": {
        "label": "普通橘色条纹猫",
        "rarity": "common",
        "pattern_family": "striped",
        "palette": {
            "O": "#6B351B", "F": "#E9953D", "I": "#8ED36A",
            "P": "#182116", "N": "#D96867", "S": "#A94F24",
        },
        "effect": "none",
    },
    "rare_calico": {
        "label": "稀有三花猫",
        "rarity": "rare",
        "pattern_family": "calico",
        "palette": {
            "O": "#342821", "F": "#F3DFC3", "I": "#65BFD0",
            "P": "#171717", "N": "#D77A80", "S": "#CE702F",
        },
        "effect": "spark",
    },
    "epic_angel": {
        "label": "史诗天使猫",
        "rarity": "epic",
        "pattern_family": "solid",
        "palette": {
            "O": "#77633D", "F": "#FFF6DE", "I": "#70C7EF",
            "P": "#244F6D", "N": "#DE8E98", "S": "#E4D4AE",
            "W": "#74BDE8", "A": "#F3C94C",
        },
        "effect": "halo",
    },
}


class CatExperimentService:
    def __init__(self, root: Path):
        self.root = root
        self.master = json.loads((root / "master-template-v1.json").read_text("utf-8"))
        self.classic = json.loads((root / "classic-black-v1.json").read_text("utf-8"))
        self.anatomy = json.loads((root / "cat-anatomy-v1.json").read_text("utf-8"))
        self.cells = {(cell["x"], cell["y"]): cell for cell in self.master["cells"]}

    def config(self) -> dict:
        counts: dict[str, int] = {}
        for cell in self.master["cells"]:
            key = cell["permission"]
            counts[key] = counts.get(key, 0) + 1
        return {
            "ok": True,
            "template_id": self.master["template_id"],
            "status": self.master["status"],
            "experiments": [
                {"id": key, "label": value["label"], "rarity": value["rarity"], "pattern_family": value["pattern_family"]}
                for key, value in EXPERIMENTS.items()
            ],
            "permission_counts": counts,
            "note": "实验结果不写入衣柜，也不消耗每日礼盒机会。",
        }

    @staticmethod
    def _paint(matrix: list[list[str]], operations: list[dict]) -> None:
        for op in operations:
            matrix[op["y"]][op["x"]] = op["code"]

    def _allowed_pattern(self, point: tuple[int, int]) -> bool:
        cell = self.cells[point]
        return bool(cell["pattern_allowed"]) and not cell["final_face_foreground"]

    def _pattern_operations(self, experiment_id: str, rng: random.Random) -> list[dict]:
        if experiment_id == "orange_tabby":
            groups = [
                [(9, 3), (10, 3)],
                [(13, 4), (14, 4)],
                [(7, 10), (8, 10)],
                [(6, 11), (7, 11)],
                [(1, 7), (2, 7)],
            ]
            chosen = rng.sample(groups, k=rng.choice((3, 4)))
        elif experiment_id == "rare_calico":
            groups = [
                [(8, 3), (9, 3), (8, 4)],
                [(13, 3), (14, 3), (14, 4)],
                [(6, 9), (7, 9), (6, 10), (7, 10)],
                [(8, 11), (9, 11), (8, 12)],
                [(1, 7), (2, 7), (1, 8), (2, 8)],
            ]
            chosen = rng.sample(groups, k=rng.choice((3, 4)))
        else:
            chosen = [rng.choice([
                [(8, 3), (9, 3)], [(13, 3), (14, 3)], [(7, 10), (8, 10)]
            ])]
        operations = []
        for group in chosen:
            for x, y in group:
                if self._allowed_pattern((x, y)):
                    operations.append({"x": x, "y": y, "role": "fur_secondary", "code": "S"})
        return operations

    def _accessory_operations(self, experiment_id: str) -> tuple[list[dict], list[dict]]:
        if experiment_id != "epic_angel":
            return [], []
        # A compact, connected upper-left wing plus a floating halo. Both are
        # contained by the Master Template V1 overlay zones.
        wing_outline = [(2,4),(3,4),(4,4),(2,5),(5,5),(3,6),(5,6),(4,7),(6,7)]
        wing_fill = [(3,5),(4,5),(4,6),(5,7)]
        wing = [*wing_outline, *wing_fill]
        halo = [(9, 0), (10, 0), (11, 0), (12, 0), (13, 0)]
        operations = [
            *({"x": x, "y": y, "role": "wing_outline", "code": "O", "zone": "wing"} for x, y in wing_outline),
            *({"x": x, "y": y, "role": "wing_fill", "code": "W", "zone": "wing"} for x, y in wing_fill),
            *({"x": x, "y": y, "role": "halo", "code": "A", "zone": "headwear"} for x, y in halo),
        ]
        floating = [{"type": "halo", "pixels": [[x, y] for x, y in halo]}]
        return operations, floating

    def _validate_operations(self, pattern_ops: list[dict], accessory_ops: list[dict], rarity: str) -> list[str]:
        errors: list[str] = []
        seen: set[tuple[int, int]] = set()
        for op in pattern_ops:
            point = (op["x"], op["y"])
            if point in seen:
                errors.append(f"花纹坐标重复: {point}")
            seen.add(point)
            if not self._allowed_pattern(point):
                errors.append(f"花纹越权: {point}")
        if rarity in {"common", "rare"} and accessory_ops:
            errors.append("普通/稀有实验不得使用大型装饰")
        tail = {tuple(point) for point in self.classic["parts"]["tail"]}
        for op in accessory_ops:
            point = (op["x"], op["y"])
            zone = op.get("zone")
            if zone not in self.cells[point]["overlay_zones"]:
                errors.append(f"装饰超出 {zone} 允许区: {point}")
            if zone == "wing" and point in tail:
                errors.append(f"翅膀与尾巴重叠: {point}")
        return errors

    def generate(self, experiment_id: str, seed: int | None = None) -> dict:
        if experiment_id not in EXPERIMENTS:
            raise KeyError("未知实验类型")
        seed = int(seed if seed is not None else random.SystemRandom().randrange(1, 2**31))
        rng = random.Random(seed)
        spec = EXPERIMENTS[experiment_id]
        pattern_ops = self._pattern_operations(experiment_id, rng)
        accessory_ops, floating = self._accessory_operations(experiment_id)
        operation_errors = self._validate_operations(pattern_ops, accessory_ops, spec["rarity"])

        matrix_a = [list(row) for row in self.classic["frames"]["a"]]
        self._paint(matrix_a, [*pattern_ops, *accessory_ops])
        matrix_b = [list(row) for row in self.classic["frames"]["b"]]
        # Non-leg design is copied to B; the classic B-frame leg pose remains.
        for op in [*pattern_ops, *accessory_ops]:
            x, y = op["x"], op["y"]
            if not (4 <= x <= 13 and 12 <= y <= 15):
                matrix_b[y][x] = op["code"]

        skin = json.loads(json.dumps(self.classic))
        skin.update({
            "name": spec["label"], "rarity": spec["rarity"],
            "theme": experiment_id, "pattern_type": spec["pattern_family"],
            "palette": spec["palette"],
            "frames": {"a": ["".join(row) for row in matrix_a], "b": ["".join(row) for row in matrix_b]},
            "floating_regions": floating,
            "effect": spec["effect"],
            "rarity_label": {"common": "普通", "rare": "稀有", "epic": "史诗"}[spec["rarity"]],
            "description": "Master Template V1 管理员实验结果，不会写入衣柜。",
        })
        skin["parts"]["wing"] = [[op["x"], op["y"]] for op in accessory_ops if op.get("zone") == "wing"]
        skin["parts"]["head_accessory"] = [[op["x"], op["y"]] for op in accessory_ops if op.get("zone") == "headwear"]
        validation_errors = [*operation_errors, *validate_data(skin, self.anatomy)]
        return {
            "ok": not validation_errors,
            "experiment_id": experiment_id,
            "seed": seed,
            "design_gene": {
                "rarity": spec["rarity"], "pattern_family": spec["pattern_family"],
                "pattern_density": "medium", "symmetry_bias": "controlled",
                "accessory_family": "angel" if experiment_id == "epic_angel" else "none",
            },
            "operations": {"pattern": pattern_ops, "accessory": accessory_ops},
            "skin": skin,
            "validation": {"passed": not validation_errors, "errors": validation_errors},
            "persisted": False,
            "consumed_daily_chance": False,
        }
