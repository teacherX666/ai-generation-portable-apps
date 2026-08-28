"""Account-scoped 16×16 AI cat skin generation and persistence."""
from __future__ import annotations

from datetime import date, datetime
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .validate_skin import validate_data

RARITY_TABLE = (("common", 60), ("rare", 28), ("epic", 10), ("legendary", 2))
RARITY_LABELS = {"common": "普通", "rare": "稀有", "epic": "史诗", "legendary": "传说"}
RARITY_THEMES = {
    "common": [
        ("orange_tabby", "橘色条纹猫", "striped", "暖橘、奶油色"),
        ("blue_gray", "蓝灰短毛猫", "solid", "蓝灰、银灰"),
        ("tuxedo", "奶牛猫", "color_block", "黑白撞色"),
        ("cream_spots", "奶油斑点猫", "spotted", "奶油、浅棕"),
    ],
    "rare": [
        ("ragdoll", "布偶猫", "color_block", "奶白、浅灰、蓝眼"),
        ("calico", "三花猫", "complex", "黑、橘、白"),
        ("siamese", "暹罗猫", "color_block", "米白、深棕、蓝眼"),
        ("golden_shaded", "金渐层猫", "complex", "金色、深棕"),
        ("maine_coon", "缅因猫", "striped", "棕灰、深色条纹"),
    ],
    "epic": [
        ("angel", "天使猫", "complex", "奶白、天蓝、金色"),
        ("demon", "恶魔猫", "complex", "暗红、黑、紫"),
        ("spider_hero", "蛛网英雄猫", "complex", "红、蓝、白眼罩"),
        ("arcane_mage", "奥术法师猫", "complex", "深紫、星蓝、金色"),
        ("mecha", "机械猫", "complex", "银灰、青蓝光"),
    ],
    "legendary": [
        ("celestial_king", "星辰神王猫", "complex", "星蓝、金色、白光"),
        ("dragon_lord", "龙王猫", "complex", "赤金、墨黑、龙鳞色"),
        ("time_guardian", "时空守护猫", "complex", "青蓝、紫色、金色"),
        ("seraph", "六翼炽天使猫", "complex", "圣白、金色、虹彩蓝"),
    ],
}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型没有返回 JSON 对象")
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型返回值不是 JSON 对象")
    return value


class CatGenerationError(RuntimeError):
    pass


class CatDailyLimitError(RuntimeError):
    pass


class CatGenerationBusyError(RuntimeError):
    pass


class CatSkinGenerator:
    """Generate a genuinely new 16×16 silhouette, then enforce cat anatomy.

    The model owns every pixel in frame A, including transparent pixels.  The
    server does not copy a catalog silhouette: it only repairs the small set of
    facial/body anchors, derives the running frame and computes parts metadata.
    """

    def __init__(self, root: Path, key_loader: Callable[[], str] | None = None):
        self.root = root
        self.anatomy = _read_json(root / "cat-anatomy-v1.json")
        self.classic = _read_json(root / "classic-black-v1.json")
        self.catalog = _read_json(root / "catalog-v1.json")
        self.key_loader = key_loader or (lambda: "")
        self._recent_silhouettes: list[tuple[str, ...]] = []
        self._recent_lock = threading.Lock()

    def _config(self) -> tuple[str, str, str, str]:
        provider = os.environ.get("CAT_SKIN_PROVIDER", "deepseek").strip().lower()
        key = (os.environ.get("CAT_SKIN_API_KEY") or self.key_loader() or "").strip()
        if provider == "openai":
            return provider, key, os.environ.get("CAT_SKIN_MODEL", "gpt-4.1-mini"), os.environ.get("CAT_SKIN_BASE_URL", "https://api.openai.com/v1/chat/completions")
        return "deepseek", key, os.environ.get("CAT_SKIN_MODEL", "deepseek-chat"), os.environ.get("CAT_SKIN_BASE_URL", "https://api.deepseek.com/v1/chat/completions")

    def provider_info(self) -> dict:
        provider, key, model, _ = self._config()
        return {"provider": provider, "model": model, "configured": bool(key)}

    def _chat(self, messages: list[dict]) -> str:
        provider, key, model, url = self._config()
        if not key:
            raise CatGenerationError("猫咪生成 API Key 尚未配置")
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "response_format": {"type": "json_object"},
        }
        if provider == "openai":
            body["max_completion_tokens"] = 400
        else:
            body["max_tokens"] = 400
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            print(f"  [cat-skin] {provider}/{model} responded in {time.monotonic() - started:.1f}s", flush=True)
            return payload["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise CatGenerationError(f"{provider} API 返回 {exc.code}: {detail}") from exc
        except CatGenerationError:
            raise
        except Exception as exc:
            raise CatGenerationError(f"调用猫咪生成模型失败: {exc}") from exc

    @staticmethod
    def _draw_rarity(rng: random.Random) -> str:
        roll = rng.uniform(0, 100)
        total = 0
        for rarity, weight in RARITY_TABLE:
            total += weight
            if roll < total:
                return rarity
        return "legendary"

    def _base_for(self, rarity: str, theme: str, rng: random.Random) -> dict:
        """Palette fallback only; its pixels are never used as a silhouette."""
        by_id = {skin["id"]: skin for skin in self.catalog}
        if "spider" in theme:
            return by_id["spider-hero"]
        if theme in {"angel", "seraph"}:
            return by_id["little-angel"]
        if rarity == "legendary":
            return by_id["golden-king"]
        if rarity == "epic":
            return by_id[rng.choice(["midnight-nebula", "spider-hero", "little-angel"])]
        if rarity == "rare":
            return by_id["banana-milk"]
        return by_id["classic-black"]

    @staticmethod
    def _valid_palette(value: object, fallback: dict, max_colors: int) -> dict:
        # O/F/I/P/N/S have stable semantic roles so the server can repair the
        # face without knowing which visual theme the model chose.
        required = ("O", "F", "I", "P", "N", "S")
        source = value if isinstance(value, dict) else {}
        palette = {}
        # Semantic keys always come first. Otherwise a model that invents six
        # decorative keys could accidentally push out the eye or outline color.
        for key in required:
            color = source.get(key)
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                color = fallback.get(key, CatSkinGenerator._semantic_fallback(key))
            palette[key] = color.upper()
        for key, color in source.items():
            if len(palette) >= max_colors:
                break
            if key not in palette and isinstance(key, str) and len(key) == 1 and key != "." and isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                palette[key] = color.upper()
        return palette

    @staticmethod
    def _semantic_fallback(key: str) -> str:
        return {
            "O": "#20232A", "F": "#596273", "I": "#79DDA3",
            "P": "#111318", "N": "#D65B70", "S": "#D5C4B7",
        }.get(key, "#FFFFFF")

    @staticmethod
    def _parse_frame(rows: object, palette: dict) -> list[list[str]]:
        """Tolerate harmless row-count mistakes without restoring a template.

        Missing cells remain transparent; unknown color codes are also made
        transparent. The anatomy repair pass then fills only mandatory anchors.
        """
        if not isinstance(rows, list) or not rows:
            raise ValueError("frame_a 必须是像素行数组")
        allowed = set(palette)
        normalized = []
        for raw_row in rows[:16]:
            row = raw_row if isinstance(raw_row, str) else ""
            row = row.strip().replace(" ", "")[:16].ljust(16, ".")
            normalized.append([code if code == "." or code in allowed else "." for code in row])
        while len(normalized) < 16:
            normalized.append(list("................"))
        return normalized

    @staticmethod
    def _coords_in(box: dict):
        for y in range(box["y_min"], box["y_max"] + 1):
            for x in range(box["x_min"], box["x_max"] + 1):
                yield x, y

    @staticmethod
    def _connected_component(matrix: list[list[str]], starts: set[tuple[int, int]], allowed: set[tuple[int, int]] | None = None) -> set[tuple[int, int]]:
        occupied = {(x, y) for y in range(16) for x in range(16) if matrix[y][x] != "."}
        if allowed is not None:
            occupied &= allowed
        queue = [point for point in starts if point in occupied]
        seen = set()
        while queue:
            x, y = queue.pop()
            if (x, y) in seen:
                continue
            seen.add((x, y))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    point = (x + dx, y + dy)
                    if point in occupied and point not in seen:
                        queue.append(point)
        return seen

    def _allowed_pixels(self, rarity: str) -> set[tuple[int, int]]:
        a = self.anatomy["anatomy"]
        allowed = set()
        # Shared cat design envelope. It deliberately leaves meaningful blank
        # space instead of letting a bad model response fill the whole canvas.
        for box in (
            {"x_min": 6, "x_max": 15, "y_min": 0, "y_max": 9},
            {"x_min": 3, "x_max": 11, "y_min": 7, "y_max": 14},
            a["tail"]["design_box"], a["legs"]["animation_box"],
        ):
            allowed.update(self._coords_in(box))
        if rarity in {"rare", "epic", "legendary"}:
            allowed.update(self._coords_in(a["head_accessory"]["design_box"]))
        if rarity in {"epic", "legendary"}:
            allowed.update(self._coords_in(a["wing"]["design_box"]))
        return allowed

    @staticmethod
    def _paint(matrix: list[list[str]], coords: list[list[int]] | set[tuple[int, int]], code: str, only_empty: bool = False) -> None:
        for x, y in coords:
            if not only_empty or matrix[y][x] == ".":
                matrix[y][x] = code

    def _fallback_tail(self, matrix: list[list[str]], rng: random.Random) -> set[tuple[int, int]]:
        routes = [
            [(4,10),(3,10),(2,9),(1,9),(0,8),(0,7),(1,6)],
            [(4,10),(3,9),(2,8),(1,8),(0,7),(1,6),(2,6)],
            [(4,10),(3,10),(2,10),(1,9),(1,8),(2,7),(3,7)],
            [(4,10),(3,10),(2,9),(2,8),(1,7),(0,7),(0,6)],
        ]
        route = rng.choice(routes)
        for index, (x, y) in enumerate(route):
            matrix[y][x] = "O" if index in {0, len(route) - 1} else "F"
        return set(route)

    def _repair_design(self, rows: object, palette: dict, rarity: str, theme: str, rng: random.Random) -> tuple[dict, dict, list[dict]]:
        matrix = self._parse_frame(rows, palette)
        anatomy = self.anatomy["anatomy"]
        allowed = self._allowed_pixels(rarity)
        for y in range(16):
            for x in range(16):
                if (x, y) not in allowed:
                    matrix[y][x] = "."

        # Minimum closed cat anatomy. Existing AI colors inside these regions
        # remain untouched; only transparent holes are filled.
        self._paint(matrix, anatomy["ears"]["left_required"], "O", True)
        self._paint(matrix, anatomy["ears"]["right_required"], "O", True)
        for y_text, ranges in anatomy["head"]["required_occupied_rows"].items():
            y = int(y_text)
            for x_min, x_max in ranges:
                self._paint(matrix, {(x, y) for x in range(x_min, x_max + 1)}, "F", True)
        self._paint(matrix, anatomy["head"]["outline_anchors"], "O")
        self._paint(matrix, anatomy["head"]["flat_chin"], "O")
        self._paint(matrix, anatomy["torso"]["required_core"], "F", True)
        self._paint(matrix, anatomy["torso"]["shoulder_connection"], "F", True)
        self._paint(matrix, anatomy["torso"]["closed_rump_boundary"], "O", True)
        self._paint(matrix, anatomy["tail"]["root"], "O", True)

        # Restore unmistakable feline facial semantics after free pixel design.
        eye = anatomy["eyes"]
        self._paint(matrix, eye["left_eye_box"], "I")
        self._paint(matrix, eye["right_eye_box"], "I")
        self._paint(matrix, eye["left_pupil"], "P")
        self._paint(matrix, eye["right_pupil"], "P")
        self._paint(matrix, eye["separator"], "F")
        self._paint(matrix, anatomy["muzzle"]["nose"], "N")
        self._paint(matrix, anatomy["muzzle"]["mouth_corners"], "S")
        self._paint(matrix, anatomy["muzzle"]["mouth_center"], "F")

        # The AI owns the upper silhouette, torso, tail and accessories. The
        # lower four rows are normalized into a reliable two-frame gait.
        animation_box = anatomy["legs"]["animation_box"]
        for x, y in self._coords_in(animation_box):
            matrix[y][x] = "."
        legs_a = {(6,12),(7,12),(8,12),(9,12),(6,13),(7,13),(8,13),(9,13),
                  (5,14),(6,14),(7,14),(10,14),(11,14),(12,14),
                  (5,15),(6,15),(7,15),(10,15),(11,15),(12,15)}
        for x, y in legs_a:
            matrix[y][x] = "O" if y == 15 or x in {5,12} else "F"
        # The rump boundary overlaps the gait box; restore it after clearing
        # the lower rows so the back of the body remains visibly closed.
        self._paint(matrix, anatomy["torso"]["closed_rump_boundary"], "O", True)

        tail_zone = set(self._coords_in(anatomy["tail"]["design_box"]))
        roots = {tuple(coord) for coord in anatomy["tail"]["root"]}
        tail = self._connected_component(matrix, roots, tail_zone)
        tail = {point for point in tail if point[0] <= 3 or point in roots}
        if len(tail) < 6:
            tail = self._fallback_tail(matrix, rng) | roots

        # Keep the cat body connected; only a small high-level head ornament may
        # legitimately float (for example a halo). Other stray dots are erased.
        main = self._connected_component(matrix, {(11, 7)})
        occupied = {(x, y) for y in range(16) for x in range(16) if matrix[y][x] != "."}
        floating_pixels = occupied - main
        floating = []
        if rarity in {"epic", "legendary"}:
            accessory_box = set(self._coords_in(anatomy["head_accessory"]["design_box"]))
            accepted = floating_pixels & accessory_box
            if accepted:
                floating.append({"type": "head_accessory", "pixels": [list(point) for point in sorted(accepted)]})
            floating_pixels -= accepted
        for x, y in floating_pixels:
            matrix[y][x] = "."

        # Re-evaluate tail after disconnected-pixel cleanup and restore a route
        # if the model drew an invalid detached one.
        tail = {point for point in tail if matrix[point[1]][point[0]] != "."}
        if len(tail) < 6:
            tail = self._fallback_tail(matrix, rng) | roots

        frame_a = ["".join(row) for row in matrix]
        matrix_b = [row[:] for row in matrix]
        for x, y in self._coords_in(animation_box):
            matrix_b[y][x] = "."
        legs_b = {(5,12),(6,12),(7,12),(8,12),(9,12),(10,12),
                  (5,13),(6,13),(7,13),(8,13),(9,13),(10,13),
                  (4,14),(5,14),(6,14),(11,14),(12,14),
                  (4,15),(5,15),(6,15),(11,15),(12,15)}
        for x, y in legs_b:
            matrix_b[y][x] = "O" if y == 15 or x in {4,12} else "F"
        self._paint(matrix_b, anatomy["torso"]["closed_rump_boundary"], "O", True)
        frame_b = ["".join(row) for row in matrix_b]

        occupied_a = {(x, y) for y, row in enumerate(frame_a) for x, code in enumerate(row) if code != "."}
        wing_box = set(self._coords_in(anatomy["wing"]["design_box"]))
        head_box = set(self._coords_in(anatomy["head_accessory"]["design_box"]))
        fixed_head = {tuple(c) for c in anatomy["head"]["outline_anchors"] + anatomy["head"]["flat_chin"]}
        wing = sorted((occupied_a & wing_box) - tail - fixed_head) if rarity in {"epic", "legendary"} else []
        accessory = sorted(occupied_a & head_box)
        parts = {
            "eyes": anatomy["eyes"]["left_eye_box"] + anatomy["eyes"]["right_eye_box"],
            "pupils": anatomy["eyes"]["left_pupil"] + anatomy["eyes"]["right_pupil"],
            "nose": anatomy["muzzle"]["nose"],
            "mouth": anatomy["muzzle"]["mouth_corners"] + anatomy["muzzle"]["mouth_center"],
            "chin": anatomy["head"]["flat_chin"],
            "head_outline": anatomy["head"]["outline_anchors"] + anatomy["head"]["flat_chin"],
            "torso": [list(point) for point in sorted(occupied_a & set(self._coords_in(anatomy["torso"]["design_box"])))],
            "rump_boundary": anatomy["torso"]["closed_rump_boundary"],
            "tail": [list(point) for point in sorted(tail)],
            "legs_a": [list(point) for point in sorted(legs_a)],
            "legs_b": [list(point) for point in sorted(legs_b)],
            "wing": [list(point) for point in wing],
            "head_accessory": [list(point) for point in accessory],
        }
        return {"a": frame_a, "b": frame_b}, parts, floating

    @staticmethod
    def _silhouette(frames: dict) -> tuple[str, ...]:
        return tuple("".join("#" if code != "." else "." for code in row) for row in frames["a"])

    @staticmethod
    def _silhouette_distance(left: tuple[str, ...], right: tuple[str, ...]) -> int:
        return sum(a != b for row_a, row_b in zip(left, right) for a, b in zip(row_a, row_b))

    def _is_distinct_silhouette(self, silhouette: tuple[str, ...], known: set[tuple[str, ...]]) -> bool:
        with self._recent_lock:
            comparisons = list(known) + self._recent_silhouettes
            return all(self._silhouette_distance(silhouette, other) >= 10 for other in comparisons)

    def _remember_silhouette(self, silhouette: tuple[str, ...]) -> None:
        with self._recent_lock:
            self._recent_silhouettes.append(silhouette)
            del self._recent_silhouettes[:-24]

    _DESIGN_OPTIONS = {
        "head": ("round", "soft_cheek", "fluffy"),
        "body": ("balanced", "slim", "fluffy"),
        "tail": ("classic_up", "long_curl", "soft_hook", "plume"),
        "ears": ("classic", "wide", "tufted"),
        "pattern": ("solid", "tabby", "tuxedo", "point", "calico", "spotted"),
    }
    _ACCESSORIES = {
        "common": ("none",),
        "rare": ("none",),
        "epic": ("crown", "halo", "angel_wing", "horns", "cape"),
        "legendary": ("crown", "halo", "angel_wing", "horns", "cape"),
    }
    _PALETTES = {
        "orange_tabby": {"O":"#63361F","F":"#E98B35","I":"#9BE06D","P":"#172019","N":"#D65B70","S":"#FFE0B2"},
        "blue_gray": {"O":"#263240","F":"#71869A","I":"#8DE5C0","P":"#14202A","N":"#D67B8C","S":"#C7D3DE"},
        "tuxedo": {"O":"#16191F","F":"#30343D","I":"#F1C84B","P":"#17191D","N":"#D65B70","S":"#F4F1E8"},
        "cream_spots": {"O":"#5B4634","F":"#EBCF9D","I":"#79CFA9","P":"#1B3028","N":"#D98786","S":"#FFF2D5"},
        "ragdoll": {"O":"#66594F","F":"#F2EDE2","I":"#75C9F1","P":"#244A67","N":"#D9919B","S":"#B8A99A"},
        "calico": {"O":"#2A2522","F":"#F4EEE2","I":"#76C38F","P":"#183424","N":"#D77979","S":"#D9823B"},
        "siamese": {"O":"#45342E","F":"#E8D9BD","I":"#78C9F4","P":"#234A6A","N":"#C98282","S":"#76564A"},
        "golden_shaded": {"O":"#56391E","F":"#DDA94A","I":"#69C993","P":"#173B2A","N":"#D47B72","S":"#F3D991"},
        "maine_coon": {"O":"#3A302A","F":"#806B5A","I":"#8AD49E","P":"#203126","N":"#CC777A","S":"#BDAA94"},
        "angel": {"O":"#866A35","F":"#FFF8E8","I":"#77C7F2","P":"#26577A","N":"#E69A9A","S":"#E7DAB9","A":"#F1C84B","W":"#DDF3FF"},
        "demon": {"O":"#21151F","F":"#8E2F45","I":"#E8C45D","P":"#251018","N":"#D65B70","S":"#C47484","A":"#4B244F","W":"#B84255"},
        "spider_hero": {"O":"#171923","F":"#D7353F","I":"#F8FAFC","P":"#202431","N":"#171923","S":"#2867B2","A":"#F8FAFC"},
        "arcane_mage": {"O":"#211A3A","F":"#49377C","I":"#91FFF1","P":"#241D45","N":"#E78BBE","S":"#7960C6","A":"#F4D76B"},
        "mecha": {"O":"#25313A","F":"#81909B","I":"#6FFFF2","P":"#153039","N":"#E16A70","S":"#C9D4DB","A":"#42BFC7"},
        "celestial_king": {"O":"#2B2444","F":"#536EB2","I":"#A8FFF0","P":"#192647","N":"#E68DA4","S":"#D9E2FF","A":"#F3C849","W":"#EEF5FF"},
        "dragon_lord": {"O":"#261B18","F":"#9E3D2E","I":"#F2D45C","P":"#291713","N":"#D65B70","S":"#D98A45","A":"#F0B83E","W":"#5E2525"},
        "time_guardian": {"O":"#20233D","F":"#416E92","I":"#78F2DE","P":"#182B3B","N":"#DD7FA2","S":"#8D73C9","A":"#F0CE59","W":"#9DEBFA"},
        "seraph": {"O":"#75603B","F":"#FFF9EC","I":"#83D8FF","P":"#315D78","N":"#E89AA4","S":"#E9DDBB","A":"#F4CC4F","W":"#E4F6FF"},
    }

    def _random_design(self, rarity: str, theme: str, rng: random.Random) -> dict:
        accessory = "none"
        if rarity in {"epic", "legendary"}:
            themed = {"angel": "angel_wing", "seraph": "angel_wing", "celestial_king": "crown", "dragon_lord": "horns", "demon": "horns", "arcane_mage": "cape"}
            accessory = themed.get(theme, rng.choice(self._ACCESSORIES[rarity][1:]))
        return {
            "name": next(item[1] for item in RARITY_THEMES[rarity] if item[0] == theme),
            "head": rng.choice(self._DESIGN_OPTIONS["head"]),
            "body": rng.choice(self._DESIGN_OPTIONS["body"]),
            "tail": rng.choice(self._DESIGN_OPTIONS["tail"]),
            "ears": rng.choice(self._DESIGN_OPTIONS["ears"]),
            "pattern": rng.choice(self._DESIGN_OPTIONS["pattern"]),
            "accessory": accessory,
        }

    def _sanitize_design(self, value: object, rarity: str, theme: str, rng: random.Random) -> dict:
        design = self._random_design(rarity, theme, rng)
        raw = value if isinstance(value, dict) else {}
        for field, allowed in self._DESIGN_OPTIONS.items():
            if raw.get(field) in allowed:
                design[field] = raw[field]
        if raw.get("accessory") in self._ACCESSORIES[rarity]:
            design["accessory"] = raw["accessory"]
        name = raw.get("name")
        if isinstance(name, str) and 2 <= len(name.strip()) <= 8:
            design["name"] = name.strip()
        return design

    def _choose_design(self, rarity: str, theme: str, theme_zh: str, rng: random.Random) -> dict:
        fallback = self._random_design(rarity, theme, rng)
        if not self.provider_info()["configured"]:
            return fallback
        allowed_accessories = ",".join(self._ACCESSORIES[rarity])
        prompt = f"""为一只16×16像素猫选择受控部件，不要画图，不要输出像素或颜色。主题是{theme}（{theme_zh}），稀有度{rarity}。
只输出JSON：{{"name":"2到8个汉字","head":"round|soft_cheek|fluffy","body":"balanced|slim|fluffy","tail":"classic_up|long_curl|soft_hook|plume","ears":"classic|wide|tufted","pattern":"solid|tabby|tuxedo|point|calico|spotted","accessory":"{allowed_accessories}"}}。只能使用列出的枚举值。"""
        try:
            raw = _extract_json(self._chat([
                {"role": "system", "content": "你只选择给定枚举并为猫命名，不设计像素。只输出JSON。"},
                {"role": "user", "content": prompt},
            ]))
            return self._sanitize_design(raw, rarity, theme, rng)
        except (CatGenerationError, ValueError, json.JSONDecodeError) as exc:
            print(f"  [cat-skin] design selector fallback: {exc}", flush=True)
            return fallback

    @staticmethod
    def _set_pixels(matrix: list[list[str]], points: list[tuple[int, int, str]]) -> None:
        for x, y, code in points:
            if 0 <= x < 16 and 0 <= y < 16:
                matrix[y][x] = code

    def _assemble_design(self, rarity: str, theme: str, design: dict, rng: random.Random) -> dict:
        matrix = [list(row) for row in self.classic["frames"]["a"]]
        head_patches = {
            "round": [], "soft_cheek": [(6,7,"O"),(7,8,"F")],
            "fluffy": [(6,3,"O"),(6,4,"F"),(6,7,"O"),(7,8,"F")],
        }
        body_patches = {
            "balanced": [], "slim": [(4,8,"."),(3,11,".")],
            "fluffy": [(3,9,"O"),(3,11,"O"),(4,12,"F")],
        }
        ear_patches = {"classic": [], "wide": [(8,1,"O"),(14,1,"O")], "tufted": [(6,1,"O"),(8,0,"O"),(14,0,"O")]}
        tails = {
            "classic_up": [(4,10),(3,10),(2,9),(1,9),(0,8),(0,7)],
            "long_curl": [(4,10),(3,10),(2,9),(1,8),(0,7),(0,6),(1,5),(2,5)],
            "soft_hook": [(4,10),(3,9),(2,8),(1,7),(1,6),(2,5),(3,5)],
            "plume": [(4,10),(3,10),(2,9),(1,8),(0,7),(1,6),(2,6),(3,7)],
        }
        self._set_pixels(matrix, head_patches[design["head"]] + body_patches[design["body"]] + ear_patches[design["ears"]])
        for y in range(5, 12):
            for x in range(0, 5): matrix[y][x] = "."
        route = tails[design["tail"]]
        for index, (x, y) in enumerate(route): matrix[y][x] = "O" if index in {0, len(route)-1} else "F"
        pattern_points = {
            "solid": [], "tabby": [(9,3,"S"),(11,3,"S"),(13,3,"S"),(7,10,"S"),(9,11,"S")],
            "tuxedo": [(10,7,"S"),(11,8,"S"),(12,8,"S"),(8,10,"S"),(9,10,"S")],
            "point": [(8,2,"S"),(14,2,"S"),(8,7,"S"),(14,7,"S"),(6,14,"S"),(11,14,"S")],
            "calico": [(9,3,"S"),(13,4,"S"),(8,10,"S"),(10,11,"S")],
            "spotted": [(9,4,"S"),(13,7,"S"),(7,10,"S"),(9,12,"S")],
        }
        self._set_pixels(matrix, pattern_points[design["pattern"]])
        accessory = design["accessory"]
        accessory_points = {
            "none": [], "crown": [(9,2,"A"),(9,1,"A"),(10,1,"A"),(11,0,"A"),(12,1,"A"),(13,1,"A"),(13,2,"A")],
            "halo": [(8,0,"A"),(9,0,"A"),(10,0,"A"),(11,0,"A"),(12,0,"A"),(13,0,"A"),(14,0,"A")],
            "angel_wing": [(6,7,"W"),(5,6,"W"),(4,5,"W"),(3,4,"W"),(2,4,"W"),(3,5,"W"),(4,6,"W"),(5,7,"W")],
            "horns": [(8,2,"A"),(8,1,"A"),(9,0,"A"),(14,2,"A"),(14,1,"A"),(13,0,"A")],
            "cape": [(5,8,"A"),(4,7,"A"),(3,6,"A"),(2,5,"A"),(3,7,"A"),(4,8,"A")],
        }
        self._set_pixels(matrix, accessory_points[accessory])
        palette_source = dict(self._PALETTES.get(theme, self.classic["palette"]))
        if accessory == "angel_wing" and "W" not in palette_source:
            palette_source["W"] = "#DDF3FF"
        palette = self._valid_palette(palette_source, self.classic["palette"], self.anatomy["rarity_limits"][rarity]["max_palette_colors"])
        frames, parts, floating = self._repair_design(["".join(row) for row in matrix], palette, rarity, theme, rng)
        # The assembler knows exactly which controlled patch is the tail. Do
        # not let the generic legacy repair heuristic mistake a nearby wing or
        # cape pixel for part of the tail.
        occupied_a = {(x, y) for y, row in enumerate(frames["a"]) for x, code in enumerate(row) if code != "."}
        tail_pixels = {(x, y) for x, y in route if (x, y) in occupied_a}
        tail_pixels |= {tuple(point) for point in self.anatomy["anatomy"]["tail"]["root"] if tuple(point) in occupied_a}
        parts["tail"] = [list(point) for point in sorted(tail_pixels)]
        if accessory == "angel_wing":
            parts["wing"] = [[x, y] for x, y, _code in accessory_points[accessory] if (x, y) in occupied_a and (x, y) not in tail_pixels]
        else:
            parts["wing"] = []
        skin = {
            "schema_version": "cat-skin-v1", "name": design["name"], "rarity": rarity, "theme": theme,
            "pattern_type": design["pattern"], "palette": palette, "frames": frames, "parts": parts,
            "floating_regions": floating, "design_recipe": dict(design),
            "design_notes": {"recognizable_features": [theme, design["head"], design["tail"], design["pattern"], accessory], "animation_change": "部件库组装主体，第二帧只切换固定腿部步态。"},
        }
        errors = validate_data(skin, self.anatomy)
        if errors: raise CatGenerationError("部件组装未通过结构校验：" + "；".join(errors[:5]))
        return skin

    def generate(self, rng: random.Random | None = None) -> dict:
        rng = rng or random.SystemRandom()
        rarity = self._draw_rarity(rng)
        theme, theme_zh, _pattern, _colors = rng.choice(RARITY_THEMES[rarity])
        design = self._choose_design(rarity, theme, theme_zh, rng)
        return self._assemble_design(rarity, theme, design, rng)


class CatSkinManager:
    def __init__(self, state_path: Path, classic_path: Path, generator: CatSkinGenerator, today_fn: Callable[[], date] | None = None):
        self.state_path = state_path
        self.classic = _read_json(classic_path)
        self.catalog = _read_json(classic_path.parent / "catalog-v1.json")
        self.catalog_by_id = {skin["id"]: skin for skin in self.catalog}
        self.generator = generator
        self.today_fn = today_fn or date.today
        self._lock = threading.RLock()
        self._generating: set[str] = set()

    def _load(self) -> dict:
        try:
            data = _read_json(self.state_path)
            return data if isinstance(data.get("users"), dict) else {"users": {}}
        except Exception:
            return {"users": {}}

    def _save(self, data: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.state_path)

    def _default_skin(self) -> dict:
        skin = json.loads(json.dumps(self.classic))
        skin.update({"id": "classic-black", "rarity_label": "默认", "description": "最初陪大家跳栅栏的经典黑猫。", "effect": "none", "created_at": None})
        return skin

    @staticmethod
    def _user_state(data: dict, user_id: str) -> dict:
        return data["users"].setdefault(user_id, {"equipped_skin_id": "classic-black", "last_open_date": "", "skins": []})

    def wardrobe(self, user: dict) -> dict:
        with self._lock:
            state = self._user_state(self._load(), user["user_id"])
            today = self.today_fn().isoformat()
            is_admin = user.get("role") == "admin"
            builtins = json.loads(json.dumps(self.catalog))
            generated = json.loads(json.dumps(state.get("skins", [])))
            for skin in builtins:
                skin["releasable"] = False
            for skin in generated:
                skin["releasable"] = True
            return {
                "ok": True,
                "is_admin": is_admin,
                "can_open": is_admin or state.get("last_open_date") != today,
                "next_open_date": "不限次数" if is_admin else today if state.get("last_open_date") != today else "明天",
                "equipped_skin_id": state.get("equipped_skin_id", "classic-black"),
                "skins": [*builtins, *generated],
                "generator": self.generator.provider_info(),
            }

    def open_gift(self, user: dict) -> dict:
        user_id = user["user_id"]
        is_admin = user.get("role") == "admin"
        today = self.today_fn().isoformat()
        with self._lock:
            data = self._load()
            state = self._user_state(data, user_id)
            if not is_admin and state.get("last_open_date") == today:
                raise CatDailyLimitError("今天已经领取过猫咪了，明天再来吧")
            if user_id in self._generating:
                raise CatGenerationBusyError("一只猫咪正在生成中，请稍候")
            self._generating.add(user_id)
        try:
            generated = self.generator.generate()
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            skin = dict(generated)
            skin.update({
                "id": "cat_" + secrets.token_hex(8),
                "rarity_label": RARITY_LABELS[skin["rarity"]],
                "description": "AI 选定主题、由像素部件库组装的 " + RARITY_LABELS[skin["rarity"]] + " 猫咪。",
                "effect": skin.get("effect") or ("star" if skin["rarity"] in {"epic", "legendary"} else "spark" if skin["rarity"] == "rare" else "none"),
                "created_at": now,
            })
            with self._lock:
                data = self._load()
                state = self._user_state(data, user_id)
                # Re-check after the slow model call to close the concurrent-request gap.
                if not is_admin and state.get("last_open_date") == today:
                    raise CatDailyLimitError("今天已经领取过猫咪了，明天再来吧")
                state.setdefault("skins", []).append(skin)
                state["equipped_skin_id"] = skin["id"]
                if not is_admin:
                    state["last_open_date"] = today
                self._save(data)
            return skin
        finally:
            with self._lock:
                self._generating.discard(user_id)

    def equip(self, user: dict, skin_id: str) -> dict:
        with self._lock:
            data = self._load()
            state = self._user_state(data, user["user_id"])
            owned = skin_id in self.catalog_by_id or any(s.get("id") == skin_id for s in state.get("skins", []))
            if not owned:
                raise KeyError("皮肤不存在或不属于当前账号")
            state["equipped_skin_id"] = skin_id
            self._save(data)
        return {"ok": True, "equipped_skin_id": skin_id}

    def release(self, user: dict, skin_id: str) -> dict:
        """Remove one account-owned generated skin.

        Built-in catalog skins are shared reference assets and cannot be
        released. If the released cat is currently equipped, fall back to the
        classic black cat so the game never points at a missing skin.
        """
        with self._lock:
            data = self._load()
            state = self._user_state(data, user["user_id"])
            skins = state.get("skins", [])
            index = next((i for i, skin in enumerate(skins) if skin.get("id") == skin_id), None)
            if index is None:
                if skin_id in self.catalog_by_id:
                    raise PermissionError("默认及内置猫咪不能放生")
                raise KeyError("猫咪不存在或不属于当前账号")
            released = skins.pop(index)
            if state.get("equipped_skin_id") == skin_id:
                state["equipped_skin_id"] = "classic-black"
            self._save(data)
        return {
            "ok": True,
            "released_skin_id": skin_id,
            "released_skin_name": released.get("name") or "这只猫咪",
            "equipped_skin_id": state.get("equipped_skin_id", "classic-black"),
        }
