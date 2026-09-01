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
        ("orange_tabby", "橘猫", "tabby", "暖橘、奶油色"),
        ("blue_gray", "蓝猫", "solid", "蓝灰、银灰"),
        ("tuxedo", "奶牛猫", "tuxedo", "黑白撞色"),
        ("cream_spots", "奶油猫", "spotted", "奶油、浅棕"),
        ("lihua", "狸花猫", "tabby", "棕灰、黑纹"),
        ("white_cat", "白猫", "solid", "白色、粉色"),
        ("black_white", "黑白猫", "tuxedo", "黑白撞色"),
    ],
    "rare": [
        ("ragdoll", "布偶猫", "point", "奶白、浅灰、蓝眼"),
        ("calico", "三花猫", "calico", "黑、橘、白"),
        ("siamese", "暹罗猫", "point", "米白、深棕、蓝眼"),
        ("golden_shaded", "金渐层猫", "dorsal", "金色、深棕"),
        ("maine_coon", "缅因猫", "tabby", "棕灰、深色条纹"),
        ("big_orange", "大橘猫", "tabby", "深橘、奶油色"),
        ("blue_chubby", "胖猫", "spotted", "亮蓝、浅蓝"),
        ("silver_tabby", "银渐层猫", "dorsal", "银白、深灰"),
    ],
    "epic": [
        ("angel", "天使猫", "solid", "奶白、天蓝、金色"),
        ("demon", "恶魔猫", "dorsal", "暗红、黑、紫"),
        ("spider_hero", "蜘蛛侠猫", "spider", "红、蓝、白眼罩"),
        ("arcane_mage", "法师猫", "dorsal", "深紫、星蓝、金色"),
        ("mecha", "机械猫", "panel", "银灰、青蓝光"),
        ("him", "HIM猫", "glitch", "黑灰、纯白眼"),
        ("cyber", "赛博猫", "panel", "深灰、霓虹青"),
        ("lucky", "招财猫", "patchwork", "白、红、金"),
        ("wukong", "悟空猫", "dorsal", "金棕、赤红"),
    ],
    "legendary": [
        ("celestial_king", "国王猫", "dorsal", "星蓝、金色、白光"),
        ("dragon_lord", "龙王猫", "scale", "赤金、墨黑、龙鳞色"),
        ("time_guardian", "时空猫", "panel", "青蓝、紫色、金色"),
        ("seraph", "六翼天使猫", "solid", "圣白、金色、虹彩蓝"),
        ("phoenix", "凤凰猫", "patchwork", "赤红、金色"),
        ("star_god", "星神猫", "glitch", "深蓝、星白、金色"),
    ],
}

THEME_ACCESSORIES = {
    "angel": "angel_wing",
    "demon": "horns",
    "spider_hero": "spider_mask",
    "arcane_mage": "mage_hat_cape",
    "mecha": "mecha_pack",
    "him": "none",
    "cyber": "mecha_pack",
    "lucky": "lucky_charm",
    "wukong": "headband",
    "celestial_king": "crown",
    "dragon_lord": "dragon_wing",
    "time_guardian": "clock_ring",
    "seraph": "seraph_wing",
    "phoenix": "phoenix_wing",
    "star_god": "star_crown",
}

THEME_EFFECTS = {
    "angel": "halo", "seraph": "halo", "spider_hero": "web",
    "arcane_mage": "star", "mecha": "spark", "cyber": "spark",
    "him": "shadow", "lucky": "coin", "wukong": "flame",
    "celestial_king": "royal", "dragon_lord": "flame",
    "time_guardian": "star", "phoenix": "flame", "star_god": "star",
}

THEME_LABELS = {
    theme: name
    for items in RARITY_THEMES.values()
    for theme, name, _pattern, _colors in items
}

# 名称保持短、自然、能看出主题，但不再把同主题的每只猫强制显示成同一个名字。
# “胖猫”是一个需要保留原名和原故事语境的特殊主题，不做别名改写。
THEME_NAME_POOLS = {
    "orange_tabby": ("橘猫", "小橘猫", "蜜橘猫", "橘团猫"),
    "blue_gray": ("蓝猫", "雾蓝猫", "灰蓝猫", "蓝绒猫"),
    "tuxedo": ("奶牛猫", "黑白猫", "墨点猫", "花脸猫"),
    "cream_spots": ("奶油猫", "奶糖猫", "奶斑猫", "布丁猫"),
    "lihua": ("狸花猫", "小狸猫", "虎纹猫", "山纹猫"),
    "white_cat": ("白猫", "雪球猫", "白绒猫", "云团猫"),
    "black_white": ("黑白猫", "墨白猫", "棋盘猫", "乌云猫"),
    "ragdoll": ("布偶猫", "蓝眼猫", "绒雪猫", "仙布猫"),
    "calico": ("三花猫", "花团猫", "彩斑猫", "小三花"),
    "siamese": ("暹罗猫", "焦糖猫", "蓝眸猫", "重点猫"),
    "golden_shaded": ("金渐层猫", "金绒猫", "小金猫", "金影猫"),
    "maine_coon": ("缅因猫", "长毛猫", "森系猫", "巨绒猫"),
    "big_orange": ("大橘猫", "橘座猫", "橘胖猫", "大橘子"),
    "blue_chubby": ("胖猫",),
    "silver_tabby": ("银渐层猫", "银绒猫", "月银猫", "银影猫"),
    "angel": ("天使猫", "小羽猫", "云翼猫", "晨光猫"),
    "demon": ("恶魔猫", "小魔猫", "赤角猫", "夜魔猫"),
    "spider_hero": ("蜘蛛侠猫", "小蛛猫", "红蛛猫", "蛛丝猫"),
    "arcane_mage": ("法师猫", "星法猫", "紫咒猫", "魔法猫"),
    "mecha": ("机械猫", "机甲猫", "钢爪猫", "芯片猫"),
    "him": ("HIM猫", "白眼猫", "暗影猫", "方块猫"),
    "cyber": ("赛博猫", "霓虹猫", "电光猫", "数据猫"),
    "lucky": ("招财猫", "金币猫", "福气猫", "小财猫"),
    "wukong": ("悟空猫", "金箍猫", "猴王猫", "筋斗猫"),
    "celestial_king": ("国王猫", "金冠猫", "小王猫", "王冠猫"),
    "dragon_lord": ("龙王猫", "龙鳞猫", "赤龙猫", "小龙王"),
    "time_guardian": ("时空猫", "钟摆猫", "时间猫", "裂隙猫"),
    "seraph": ("六翼天使猫", "圣翼猫", "炽天猫", "六羽猫"),
    "phoenix": ("凤凰猫", "火羽猫", "涅槃猫", "赤羽猫"),
    "star_god": ("星神猫", "星辉猫", "神星猫", "夜空猫"),
}

THEME_DESCRIPTIONS = {
    "blue_chubby": "以网络事件中的蓝色猫头像和那段令人惋惜的感情悲剧为原型，保留原版悲情故事与纪念意味。",
    "angel": "头顶金色光环，展开蓝白羽翼，奔跑时留下柔和光点。",
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
        self.master = _read_json(root / "master-template-v1.json")
        self.master_cells = {(cell["x"], cell["y"]): cell for cell in self.master["cells"]}
        self.key_loader = key_loader or (lambda: "")
        self._recent_silhouettes: list[tuple[str, ...]] = []
        self._recent_signatures: list[str] = []
        self._recent_themes: list[str] = []
        self._recent_names: list[str] = []
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
        "pattern": ("solid", "tabby", "tuxedo", "point", "calico", "spotted", "dorsal", "patchwork", "panel", "spider", "glitch", "scale"),
        "palette_variant": ("standard", "bright", "dark", "cool"),
        "pattern_variant": ("v1", "v2", "v3", "v4"),
    }
    _ACCESSORIES = {
        "common": ("none",),
        "rare": ("none",),
        "epic": ("none", "angel_wing", "horns", "spider_mask", "mage_hat_cape", "mecha_pack", "lucky_charm", "headband"),
        "legendary": ("crown", "dragon_wing", "clock_ring", "seraph_wing", "phoenix_wing", "star_crown"),
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
        "lihua": {"O":"#282521","F":"#71685B","I":"#8BCB6D","P":"#182016","N":"#D17A7A","S":"#B5A58D"},
        "white_cat": {"O":"#77747A","F":"#F5F3F0","I":"#72C8A7","P":"#21332D","N":"#E18D9B","S":"#D8D4D4"},
        "black_white": {"O":"#15171B","F":"#292C33","I":"#E9C44E","P":"#15171B","N":"#D65B70","S":"#F4F2EA"},
        "big_orange": {"O":"#633018","F":"#F29A32","I":"#86CA62","P":"#182016","N":"#D65B70","S":"#FFD59B"},
        "blue_chubby": {"O":"#183B61","F":"#358ED0","I":"#F7D85C","P":"#142A3C","N":"#E27A8E","S":"#8BD0F4"},
        "silver_tabby": {"O":"#30343A","F":"#C8CDD1","I":"#74BF8C","P":"#1C2921","N":"#D5838B","S":"#737B83"},
        "angel": {"O":"#866A35","F":"#FFF8E8","I":"#77C7F2","P":"#26577A","N":"#E69A9A","S":"#E7DAB9","A":"#F1C84B","W":"#DDF3FF"},
        "demon": {"O":"#21151F","F":"#8E2F45","I":"#E8C45D","P":"#251018","N":"#D65B70","S":"#C47484","A":"#4B244F","W":"#B84255"},
        "spider_hero": {"O":"#171923","F":"#D7353F","I":"#F8FAFC","P":"#202431","N":"#171923","S":"#2867B2","A":"#F8FAFC"},
        "arcane_mage": {"O":"#211A3A","F":"#49377C","I":"#91FFF1","P":"#241D45","N":"#E78BBE","S":"#7960C6","A":"#F4D76B"},
        "mecha": {"O":"#25313A","F":"#81909B","I":"#6FFFF2","P":"#153039","N":"#E16A70","S":"#C9D4DB","A":"#42BFC7","W":"#B7FFF8"},
        "him": {"O":"#111318","F":"#292D34","I":"#FFFFFF","P":"#FFFFFF","N":"#8B8F98","S":"#555B66","A":"#FFFFFF"},
        "cyber": {"O":"#171C28","F":"#303B4D","I":"#75FFF0","P":"#123D42","N":"#EB6C9B","S":"#935CDE","A":"#30D6D2","W":"#FF4FD8"},
        "lucky": {"O":"#6E3E2E","F":"#FFF4DE","I":"#6DBB73","P":"#213122","N":"#DA7580","S":"#D63D3D","A":"#F0C64B","W":"#D63D3D"},
        "wukong": {"O":"#4B2B1A","F":"#B97838","I":"#E9D657","P":"#2D2116","N":"#D46A66","S":"#F0B047","A":"#C93F35"},
        "celestial_king": {"O":"#2B2444","F":"#536EB2","I":"#A8FFF0","P":"#192647","N":"#E68DA4","S":"#D9E2FF","A":"#F3C849","W":"#EEF5FF"},
        "dragon_lord": {"O":"#261B18","F":"#9E3D2E","I":"#F2D45C","P":"#291713","N":"#D65B70","S":"#D98A45","A":"#F0B83E","W":"#5E2525"},
        "time_guardian": {"O":"#20233D","F":"#416E92","I":"#78F2DE","P":"#182B3B","N":"#DD7FA2","S":"#8D73C9","A":"#F0CE59","W":"#9DEBFA"},
        "seraph": {"O":"#75603B","F":"#FFF9EC","I":"#83D8FF","P":"#315D78","N":"#E89AA4","S":"#E9DDBB","A":"#F4CC4F","W":"#E4F6FF"},
        "phoenix": {"O":"#572018","F":"#D84B2F","I":"#FFE36A","P":"#3A1913","N":"#E47A72","S":"#F19332","A":"#FFD35A","W":"#FF8A35"},
        "star_god": {"O":"#17162F","F":"#34366F","I":"#C6FFFF","P":"#172642","N":"#DE86A8","S":"#777ED8","A":"#F2D45C","W":"#F8FBFF"},
    }

    @staticmethod
    def _pick_name(theme: str, rng: random.Random, blocked_names: set[str] | None = None) -> str:
        blocked_names = blocked_names or set()
        pool = THEME_NAME_POOLS.get(theme) or (THEME_LABELS.get(theme, "像素猫"),)
        available = [name for name in pool if name not in blocked_names]
        return rng.choice(available or list(pool))

    def _random_design(self, rarity: str, theme: str, rng: random.Random, blocked_names: set[str] | None = None) -> dict:
        # High-rarity silhouettes are semantic, never a random unrelated prop.
        # This prevents a mecha cat from accidentally receiving angel wings.
        accessory = THEME_ACCESSORIES.get(theme, "none")
        return {
            "name": self._pick_name(theme, rng, blocked_names),
            "head": rng.choice(self._DESIGN_OPTIONS["head"]),
            "body": rng.choice(self._DESIGN_OPTIONS["body"]),
            "tail": rng.choice(self._DESIGN_OPTIONS["tail"]),
            "ears": rng.choice(self._DESIGN_OPTIONS["ears"]),
            "pattern": rng.choice(self._DESIGN_OPTIONS["pattern"]),
            "palette_variant": rng.choice(self._DESIGN_OPTIONS["palette_variant"]),
            "pattern_variant": rng.choice(self._DESIGN_OPTIONS["pattern_variant"]),
            "accessory": accessory,
        }

    def _sanitize_design(self, value: object, rarity: str, theme: str, rng: random.Random) -> dict:
        design = self._random_design(rarity, theme, rng)
        raw = value if isinstance(value, dict) else {}
        for field, allowed in self._DESIGN_OPTIONS.items():
            if raw.get(field) in allowed:
                design[field] = raw[field]
        # Theme identity wins over model improvisation for silhouettes. A short
        # model name may survive, but verbose title-stacking falls back to the
        # natural Chinese name pool.
        design["accessory"] = THEME_ACCESSORIES.get(theme, "none")
        raw_name = str(raw.get("name") or "").strip()
        if 2 <= len(raw_name) <= 6 and (raw_name.endswith("猫") or theme == "blue_chubby"):
            design["name"] = raw_name
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

    def _master_pattern_operations(self, pattern: str, rng: random.Random, variant: str = "v1") -> list[tuple[int, int, str]]:
        """Paint theme markings only in frozen, non-facial pattern cells."""
        groups = {
            "solid": [[(9,3)],[(14,4)],[(7,10)],[(9,11)]],
            "tabby": [[(9,3),(10,3)],[(13,4),(14,4)],[(7,10),(8,10)],[(6,11),(7,11)],[(1,7),(2,7)]],
            "tuxedo": [[(10,7)],[(9,8),(11,8),(13,8)],[(7,10),(8,10)],[(8,11),(9,11)]],
            "point": [[(8,2),(9,2)],[(13,2),(14,2)],[(7,10),(8,10)],[(8,11),(9,11)]],
            "calico": [[(8,3),(9,3),(8,4)],[(13,3),(14,3),(14,4)],[(6,9),(7,9),(6,10),(7,10)],[(8,11),(9,11)],[(1,7),(2,7),(1,8),(2,8)]],
            "spotted": [[(9,4)],[(13,7)],[(7,10)],[(9,11)],[(2,7)]],
            "dorsal": [[(8,3),(9,3),(10,3)],[(7,4),(8,4)],[(6,9),(7,9),(8,9)],[(7,10),(8,10),(9,10)],[(8,11),(9,11)]],
            "patchwork": [[(8,3),(9,3)],[(13,3),(14,3),(14,4)],[(6,9),(7,9)],[(8,10),(9,10)],[(6,11),(7,11),(8,11)]],
            "panel": [[(9,3),(10,3),(11,3)],[(14,4)],[(8,7)],[(6,9),(7,9)],[(9,10),(10,10)],[(7,11),(9,11)]],
            "spider": [[(9,3),(11,3),(13,3)],[(8,4),(10,4),(12,4),(14,4)],[(8,7),(10,7),(12,7),(14,7)],[(7,10),(9,10)],[(8,11),(10,11)]],
            "glitch": [[(8,3),(10,3),(13,3)],[(14,4)],[(8,7),(14,7)],[(6,9),(8,9)],[(7,10),(10,10)],[(6,11),(9,11)]],
            "scale": [[(8,3),(10,3),(13,3)],[(9,4),(14,4)],[(8,7),(13,7)],[(6,9),(8,9)],[(7,10),(9,10)],[(8,11),(10,11)]],
        }.get(pattern, [])
        if not groups:
            return []
        variant_index = {"v1":0,"v2":1,"v3":2,"v4":3}.get(variant, 0)
        minimum = 2 if pattern in {"tuxedo", "point"} else min(3, len(groups))
        count = min(len(groups), minimum + (variant_index % 2))
        # Rotate before sampling so pattern_variant materially changes pixels.
        rotated = groups[variant_index % len(groups):] + groups[:variant_index % len(groups)]
        # Use the rotated leading groups rather than random sampling: two
        # different variant ids must produce genuinely different pixels.
        selected = rotated[:count]
        operations: list[tuple[int, int, str]] = []
        for group in selected:
            for x, y in group:
                cell = self.master_cells[(x, y)]
                if cell.get("pattern_allowed") and not cell.get("final_face_foreground"):
                    operations.append((x, y, "S"))
        return operations

    @staticmethod
    def _master_accessory_operations(accessory: str) -> list[tuple[int, int, str]]:
        """Theme-specific 16×16 symbols; no unrelated accessory lottery."""
        accessories = {
            "none": [],
            "crown": [(9,2,"A"),(9,1,"A"),(10,1,"A"),(11,0,"A"),(12,1,"A"),(13,1,"A"),(13,2,"A")],
            "star_crown": [(8,2,"A"),(9,1,"A"),(10,1,"A"),(10,2,"A"),(11,0,"W"),(12,1,"A"),(12,2,"A"),(13,1,"A"),(14,2,"A")],
            # A is deliberately shared by the gold halo and blue wing edge;
            # it is still distinct from O, the cat body's dark outline.
            "angel_wing": [(9,0,"A"),(10,0,"A"),(11,0,"A"),(12,0,"A"),(13,0,"A"),(2,4,"A"),(3,4,"A"),(4,4,"A"),(2,5,"A"),(3,5,"W"),(4,5,"W"),(5,5,"A"),(3,6,"A"),(4,6,"W"),(5,6,"A"),(4,7,"A"),(5,7,"W"),(6,7,"A")],
            # Larger wings deliberately overpaint 1-3 upper tail pixels. The
            # tail skeleton remains intact underneath as a logical base layer.
            "seraph_wing": [(1,2,"O"),(2,2,"W"),(3,2,"O"),(0,3,"O"),(1,3,"W"),(2,3,"W"),(3,3,"W"),(4,3,"O"),(0,4,"O"),(1,4,"W"),(2,4,"W"),(3,4,"W"),(4,4,"W"),(5,4,"O"),(1,5,"O"),(2,5,"W"),(3,5,"W"),(4,5,"W"),(5,5,"O"),(2,6,"O"),(3,6,"W"),(4,6,"W"),(5,6,"O"),(4,7,"W"),(5,7,"O")],
            "dragon_wing": [(1,2,"O"),(2,2,"A"),(3,2,"O"),(0,3,"O"),(1,3,"A"),(2,3,"A"),(3,3,"A"),(4,3,"O"),(1,4,"O"),(2,4,"A"),(3,4,"O"),(4,4,"A"),(5,4,"O"),(2,5,"O"),(3,5,"A"),(4,5,"A"),(5,5,"O"),(3,6,"O"),(4,6,"A"),(5,6,"O"),(4,7,"A"),(5,7,"O")],
            "phoenix_wing": [(1,2,"A"),(2,2,"W"),(3,2,"A"),(0,3,"A"),(1,3,"W"),(2,3,"W"),(3,3,"W"),(4,3,"A"),(1,4,"A"),(2,4,"W"),(3,4,"W"),(4,4,"W"),(5,4,"A"),(2,5,"A"),(3,5,"W"),(4,5,"W"),(5,5,"A"),(3,6,"A"),(4,6,"W"),(5,6,"A")],
            "horns": [(8,2,"A"),(8,1,"A"),(9,0,"A"),(14,2,"A"),(14,1,"A"),(13,0,"A")],
            "spider_mask": [(8,4,"O"),(9,4,"O"),(10,4,"O"),(12,4,"O"),(13,4,"O"),(14,4,"O"),(8,5,"A"),(9,5,"A"),(13,5,"A"),(14,5,"A"),(8,6,"A"),(9,6,"A"),(13,6,"A"),(14,6,"A"),(10,7,"O"),(12,7,"O")],
            "mage_hat_cape": [(6,3,"O"),(7,2,"O"),(8,1,"A"),(9,0,"A"),(10,0,"A"),(11,1,"A"),(12,2,"O"),(13,2,"O"),(5,3,"O"),(6,4,"A"),(5,5,"A"),(4,6,"A"),(3,7,"A"),(4,8,"A"),(5,9,"A")],
            "mecha_pack": [(3,3,"O"),(4,3,"A"),(5,3,"O"),(2,4,"O"),(3,4,"A"),(4,4,"W"),(5,4,"O"),(2,5,"O"),(3,5,"W"),(4,5,"A"),(5,5,"O"),(3,6,"O"),(4,6,"A"),(5,6,"O"),(4,7,"W"),(5,7,"O")],
            "clock_ring": [(1,2,"A"),(2,1,"A"),(3,1,"W"),(4,2,"A"),(5,3,"A"),(6,4,"A"),(5,4,"W"),(4,5,"A"),(3,6,"A"),(2,5,"A"),(1,4,"W"),(1,3,"A")],
            "lucky_charm": [(6,2,"A"),(7,1,"A"),(8,2,"A"),(6,3,"A"),(7,3,"W"),(8,3,"A")],
            "headband": [(8,3,"A"),(9,3,"A"),(10,3,"A"),(11,3,"A"),(12,3,"A"),(13,3,"A"),(14,3,"A"),(15,3,"A")],
        }
        return list(accessories.get(accessory, []))

    @staticmethod
    def _palette_variant(palette: dict, variant: str) -> dict:
        """Create visible color variety without adding palette keys."""
        if variant == "standard":
            return palette
        factors = {"bright": 1.18, "dark": 0.78, "cool": 1.0}
        factor = factors.get(variant, 1.0)
        result = dict(palette)
        for key in ("F", "S"):
            raw = result.get(key)
            if not isinstance(raw, str) or len(raw) != 7:
                continue
            r, g, b = (int(raw[i:i+2], 16) for i in (1,3,5))
            if variant == "cool":
                r, g, b = int(r*.82), int(g*.96), min(255, int(b*1.18))
            else:
                r, g, b = (min(255, max(0, int(v*factor))) for v in (r,g,b))
            result[key] = f"#{r:02X}{g:02X}{b:02X}"
        return result

    def _assemble_design(self, rarity: str, theme: str, design: dict, rng: random.Random) -> dict:
        """Render a gift cat through the frozen Master Template V1 anatomy."""
        matrix_a = [list(row) for row in self.classic["frames"]["a"]]
        matrix_b = [list(row) for row in self.classic["frames"]["b"]]
        pattern_ops = self._master_pattern_operations(design["pattern"], rng, design.get("pattern_variant", "v1"))
        accessory_ops = self._master_accessory_operations(design["accessory"])
        for x, y, code in [*pattern_ops, *accessory_ops]:
            matrix_a[y][x] = code
            # All selected markings/accessories stay above the gait-only area.
            matrix_b[y][x] = code

        palette_source = self._palette_variant(
            dict(self._PALETTES.get(theme, self.classic["palette"])),
            design.get("palette_variant", "standard"),
        )
        if design["accessory"] in {"angel_wing", "seraph_wing"}:
            # Medium sky blue survives both the white checkerboard wardrobe
            # background and the tiny 1:1 game rendering.
            palette_source["W"] = "#74BDE8"
        palette = self._valid_palette(
            palette_source,
            self.classic["palette"],
            self.anatomy["rarity_limits"][rarity]["max_palette_colors"],
        )
        skin = json.loads(json.dumps(self.classic))
        skin.update({
            "name": design["name"],
            "rarity": rarity,
            "theme": theme,
            "pattern_type": design["pattern"],
            "palette": palette,
            "frames": {
                "a": ["".join(row) for row in matrix_a],
                "b": ["".join(row) for row in matrix_b],
            },
            "floating_regions": ([{"type": "halo", "pixels": [[x, 0] for x in range(9, 14)]}] if design["accessory"] == "angel_wing" else []),
            "effect": THEME_EFFECTS.get(theme, "star" if rarity in {"epic", "legendary"} else "spark" if rarity == "rare" else "none"),
            "design_recipe": {
                **dict(design),
                "template": "classic-black-master-v1",
                "pattern_operations": [[x, y, code] for x, y, code in pattern_ops],
                "accessory_operations": [[x, y, code] for x, y, code in accessory_ops],
                "visual_tail_overlaps": [[x, y] for x, y, _code in accessory_ops if (x, y) in {tuple(point) for point in self.classic["parts"]["tail"]}],
            },
            "design_notes": {
                "recognizable_features": [theme, design["pattern"], design["accessory"]],
                "animation_change": "Master Template V1 固定猫体，两帧仅使用经典腿部步态。",
            },
        })
        wing_accessories = {"angel_wing", "seraph_wing", "dragon_wing", "phoenix_wing"}
        tail_coords = {tuple(point) for point in skin["parts"]["tail"]}
        skin["parts"]["wing"] = [
            [x, y] for x, y, _code in accessory_ops
            if design["accessory"] in wing_accessories and (x, y) not in tail_coords and y > 0
        ]
        skin["parts"]["head_accessory"] = [
            [x, y] for x, y, _code in accessory_ops
            if design["accessory"] in {"crown", "star_crown", "horns", "spider_mask", "mage_hat_cape", "clock_ring", "lucky_charm", "headband"}
            or (design["accessory"] == "angel_wing" and y == 0)
        ]
        if theme == "him":
            # Controlled facial exception: keep both eye boxes separated but
            # fill eye and pupil cells pure white for the recognizable HIM gaze.
            for x, y in [*self.anatomy["anatomy"]["eyes"]["left_eye_box"], *self.anatomy["anatomy"]["eyes"]["right_eye_box"]]:
                matrix_a[y][x] = "I"
                matrix_b[y][x] = "I"
            skin["frames"] = {"a": ["".join(row) for row in matrix_a], "b": ["".join(row) for row in matrix_b]}
        errors = validate_data(skin, self.anatomy)
        if errors:
            raise CatGenerationError("Master Template V1 生成未通过结构校验：" + "；".join(errors[:5]))
        return skin

    @staticmethod
    def _design_signature(theme: str, design: dict) -> str:
        return "|".join(str(value) for value in (
            theme, design.get("pattern"), design.get("pattern_variant"),
            design.get("palette_variant"), design.get("accessory"),
        ))

    @staticmethod
    def _stored_signature(skin: dict) -> str:
        recipe = skin.get("design_recipe") if isinstance(skin, dict) else None
        if not isinstance(recipe, dict):
            return ""
        return CatSkinGenerator._design_signature(str(skin.get("theme") or ""), recipe)

    def generate_with_history(self, recent_skins: list[dict] | None = None, rng: random.Random | None = None) -> dict:
        """Generate with a five-theme cooldown and twenty-recipe cooldown.

        Recent wardrobe recipes are accepted so restart does not immediately
        bring back the same cat. In-memory history also protects rapid admin
        testing before the next wardrobe read.
        """
        rng = rng or random.SystemRandom()
        recent_skins = recent_skins or []
        stored_themes = [str(skin.get("theme") or "") for skin in recent_skins[-5:]]
        stored_signatures = {self._stored_signature(skin) for skin in recent_skins[-20:]}
        stored_signatures.discard("")
        stored_names = {str(skin.get("name") or "") for skin in recent_skins[-20:]}
        stored_names.discard("")
        rarity = self._draw_rarity(rng)
        with self._recent_lock:
            blocked_themes = set(stored_themes + self._recent_themes[-5:])
            blocked_signatures = stored_signatures | set(self._recent_signatures[-20:])
            blocked_names = stored_names | set(self._recent_names[-20:])
        candidates = [item for item in RARITY_THEMES[rarity] if item[0] not in blocked_themes]
        if not candidates:
            candidates = list(RARITY_THEMES[rarity])

        chosen = None
        for _attempt in range(32):
            theme, _theme_zh, declared_pattern, _colors = rng.choice(candidates)
            design = self._random_design(rarity, theme, rng, blocked_names)
            if declared_pattern in self._DESIGN_OPTIONS["pattern"]:
                design["pattern"] = declared_pattern
            signature = self._design_signature(theme, design)
            chosen = (theme, design, signature)
            if signature not in blocked_signatures:
                break
        assert chosen is not None
        theme, design, signature = chosen
        skin = self._assemble_design(rarity, theme, design, rng)
        with self._recent_lock:
            self._recent_themes.append(theme)
            self._recent_signatures.append(signature)
            self._recent_names.append(design["name"])
            del self._recent_themes[:-5]
            del self._recent_signatures[:-20]
            del self._recent_names[:-20]
        return skin

    def generate(self, rng: random.Random | None = None) -> dict:
        return self.generate_with_history([], rng)


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
                "next_open_date": "不限次数" if is_admin else (today if state.get("last_open_date") != today else "明天"),
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
            if hasattr(self.generator, "generate_with_history"):
                generated = self.generator.generate_with_history(state.get("skins", [])[-20:])
            else:
                generated = self.generator.generate()
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            skin = dict(generated)
            skin.update({
                "id": "cat_" + secrets.token_hex(8),
                "rarity_label": RARITY_LABELS[skin["rarity"]],
                "description": THEME_DESCRIPTIONS.get(
                    str(skin.get("theme") or ""),
                    "AI 选定主题、由像素部件库组装的 " + RARITY_LABELS[skin["rarity"]] + " 猫咪。",
                ),
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
