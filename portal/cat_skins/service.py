"""Account-scoped 16×16 AI cat skin generation and persistence."""
from __future__ import annotations

from datetime import date, datetime
import colorsys
import hashlib
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
# 头部装饰形状由程序统一控制，AI 只负责选择类型和配色，避免把皇冠、
# 光环、角、帽子画成糊成一团的实心像素。
HEADWEAR_TEMPLATES = {
    "crown": [
        (6, 0, "A"), (10, 0, "A"), (14, 0, "A"),
        (5, 1, "A"), (6, 1, "A"), (8, 1, "A"), (9, 1, "A"), (10, 1, "A"),
        (12, 1, "A"), (13, 1, "A"), (14, 1, "A"),
        (5, 2, "A"), (6, 2, "A"), (7, 2, "A"), (8, 2, "A"), (9, 2, "A"),
        (10, 2, "A"), (11, 2, "A"), (12, 2, "A"), (13, 2, "A"), (14, 2, "A"), (15, 2, "A"),
        (10, 3, "N"),
    ],
    "halo": [
        (7, 0, "A"), (8, 0, "A"), (9, 0, "A"), (10, 0, "A"), (11, 0, "A"),
        (12, 0, "A"), (13, 0, "A"), (14, 0, "A"), (15, 0, "A"),
    ],
    "horns": [
        (5, 0, "A"), (6, 0, "A"), (5, 1, "A"),
        (14, 0, "A"), (15, 0, "A"), (15, 1, "A"),
    ],
    "cap": [
        (7, 0, "A"), (8, 0, "A"), (9, 0, "A"), (10, 0, "A"), (11, 0, "A"),
        (12, 0, "A"), (13, 0, "A"), (14, 0, "A"), (15, 0, "A"),
        (8, 1, "A"), (9, 1, "A"), (10, 1, "A"), (11, 1, "A"), (12, 1, "A"), (13, 1, "A"),
    ],
}
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

# 名称只是标签，不足以可靠指导像素设计。这里维护“概念语义层”：
# 先把名称解析成可执行的视觉 brief，再交给 AI/本地 fallback 渲染。这样“胖猫”
# 不会因为模型自由联想而变成任意颜色，而是稳定保留网络热梗的蓝色猫头像语义。
CONCEPT_BRIEFS = {
    "胖猫": {
        "category": "hot",
        "pattern": "solid",
        "visual_anchors": [
            "网络热梗猫头像", "亮蓝色主体", "浅蓝色腹部和面部高光",
            "圆润憨厚、略显委屈的表情", "不要加入职业装、皇冠或其他无关道具",
        ],
        "visual_direction": "蓝色是第一识别特征；主体必须是明显的亮蓝/中蓝，辅以浅蓝，不得使用橘黄、紫黑或随机霓虹作为主色。",
    },
    "胖猫猫": {
        "category": "hot",
        "pattern": "solid",
        "visual_anchors": [
            "网络热梗猫头像", "亮蓝色主体", "浅蓝色腹部和面部高光",
            "圆润憨厚、略显委屈的表情", "不要加入职业装、皇冠或其他无关道具",
        ],
        "visual_direction": "蓝色是第一识别特征；主体必须是明显的亮蓝/中蓝，辅以浅蓝，不得使用橘黄、紫黑或随机霓虹作为主色。",
    },
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


class CatDailyTaskRequiredError(CatDailyLimitError):
    pass


class CatGenerationBusyError(RuntimeError):
    pass


class CatSkinGenerator:
    """Generate theme variations on the frozen Classic Black Master V1 body.

    The renderer always owns the 16×16 anatomy and both gait frames.  The model
    may only propose palette roles plus coordinate operations inside explicitly
    allowed pattern/accessory cells; it can never redraw the cat silhouette.
    """

    def __init__(self, root: Path, key_loader: Callable[[], str] | None = None, concept_store=None):
        self.root = root
        self.anatomy = _read_json(root / "cat-anatomy-v1.json")
        self.classic = _read_json(root / "classic-black-v1.json")
        self.catalog = _read_json(root / "catalog-v1.json")
        self.master = _read_json(root / "master-template-v1.json")
        self.master_cells = {(cell["x"], cell["y"]): cell for cell in self.master["cells"]}
        self.key_loader = key_loader or (lambda: "")
        self.concept_store = concept_store
        self._recent_silhouettes: list[tuple[str, ...]] = []
        self._recent_visuals: list[tuple[str, ...]] = []
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
            body["max_completion_tokens"] = int(os.environ.get("CAT_SKIN_MAX_TOKENS", "2200"))
        else:
            body["max_tokens"] = int(os.environ.get("CAT_SKIN_MAX_TOKENS", "2200"))
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        timeout = float(os.environ.get("CAT_SKIN_TIMEOUT_SECONDS", "15"))
        max_attempts = max(1, int(os.environ.get("CAT_SKIN_HTTP_ATTEMPTS", "2")))
        started = time.monotonic()
        last_network_error: Exception | None = None
        for attempt in range(max_attempts):
            req = urllib.request.Request(
                url,
                data=payload_bytes,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                print(f"  [cat-skin] {provider}/{model} responded in {time.monotonic() - started:.1f}s", flush=True)
                return payload["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                raise CatGenerationError(f"{provider} API 返回 {exc.code}: {detail}") from exc
            except Exception as exc:
                last_network_error = exc
                if attempt < max_attempts - 1:
                    print(f"  [cat-skin] {provider} attempt {attempt + 1} failed ({exc}), retrying", flush=True)
                    time.sleep(0.4)
                    continue
        raise CatGenerationError(f"调用猫咪生成模型失败: {last_network_error}")

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
    def _hex_to_hls(value: object) -> tuple[float, float, float] | None:
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            return None
        red, green, blue = (int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))
        hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
        return hue, lightness, saturation

    @staticmethod
    def _hls_hex(hue: float, lightness: float, saturation: float) -> str:
        red, green, blue = colorsys.hls_to_rgb(hue % 1.0, max(0.0, min(1.0, lightness)), max(0.0, min(1.0, saturation)))
        return "#{:02X}{:02X}{:02X}".format(round(red * 255), round(green * 255), round(blue * 255))

    @staticmethod
    def _hue_distance(left: float, right: float) -> float:
        distance = abs(left - right) % 1.0
        return min(distance, 1.0 - distance)

    @staticmethod
    def _semantic_color_hint(text: str, role: str) -> dict | None:
        """Extract a coarse color intention from Chinese visual anchors.

        This deliberately recognizes color *roles* rather than every word in a
        concept.  For example, ``绿色眼睛`` may steer I but must never turn the
        whole Egyptian Mau green.
        """
        lower = text.lower()
        color_words = (
            ("紫貂", 0.075, 0.40, 0.45), ("貂色", 0.075, 0.40, 0.45),
            ("铜金", 0.105, 0.72, 0.58), ("玫瑰金", 0.035, 0.58, 0.67),
            ("深夜蓝", 0.625, 0.48, 0.24), ("星蓝", 0.615, 0.62, 0.48),
            ("天蓝", 0.555, 0.62, 0.68), ("青蓝", 0.535, 0.66, 0.50),
            ("青绿", 0.455, 0.62, 0.48), ("奶油", 0.115, 0.34, 0.82),
            ("奶白", 0.105, 0.18, 0.88), ("银白", 0.610, 0.12, 0.80),
            ("蓝灰", 0.610, 0.22, 0.52), ("银灰", 0.610, 0.13, 0.58),
            ("棕灰", 0.080, 0.20, 0.46), ("黑灰", 0.610, 0.10, 0.27),
            ("深灰", 0.610, 0.10, 0.30), ("浅灰", 0.610, 0.10, 0.70),
            ("明黄", 0.145, 0.76, 0.60), ("金色", 0.125, 0.72, 0.58),
            ("金", 0.125, 0.70, 0.56), ("黄色", 0.145, 0.70, 0.60),
            ("黄", 0.145, 0.68, 0.58), ("橘", 0.075, 0.66, 0.58),
            ("橙", 0.065, 0.70, 0.56), ("赤", 0.010, 0.68, 0.52),
            ("红棕", 0.035, 0.52, 0.44), ("棕红", 0.030, 0.52, 0.44),
            ("红", 0.985, 0.66, 0.54), ("粉", 0.955, 0.55, 0.68),
            ("紫", 0.775, 0.58, 0.54), ("蓝", 0.605, 0.62, 0.56),
            ("青", 0.505, 0.62, 0.52), ("绿", 0.355, 0.58, 0.54),
            ("棕", 0.075, 0.42, 0.42), ("灰", 0.610, 0.08, 0.52),
            ("白", 0.105, 0.10, 0.88), ("黑", 0.620, 0.08, 0.22),
        )
        role_markers = {
            "eye": ("眼睛", "眼", "瞳"),
            "body": ("主体", "身体", "毛", "皮毛", "短毛"),
            "secondary": ("斑点", "条纹", "虎斑", "色块", "面罩", "袜", "花纹"),
            "accent": ("光效", "流光", "光带", "霓虹", "粒子", "装饰"),
        }
        markers = role_markers.get(role, ())
        clauses = re.split(r"[，、；;。|/\s]+", lower)
        relevant = [clause for clause in clauses if any(marker in clause for marker in markers)]
        # Explicit role phrases win. If none exists, only accents may consume a
        # free-standing color list such as “青绿紫流光色带”.
        haystacks = relevant or ([lower] if role == "accent" else [])
        for clause in haystacks:
            for word, hue, saturation, lightness in color_words:
                if word in clause:
                    return {"word": word, "h": hue, "s": saturation, "l": lightness}
        return None

    def _harmonize_concept_palette(self, value: object, fallback: dict, max_colors: int, concept: dict) -> dict:
        """Turn an AI suggestion into a semantic, readable and coordinated palette.

        AI remains useful for choosing a theme direction, but code owns color
        roles and contrast. Visual anchors such as “绿色眼睛” and “白色主体”
        override arbitrary model colors, while category styles prevent natural
        breeds and food cats from receiving unrelated neon combinations.
        """
        source = value if isinstance(value, dict) else {}
        parsed = {key: self._hex_to_hls(source.get(key)) for key in ("O", "F", "S", "I", "A", "W")}
        fallback_parsed = {key: self._hex_to_hls(fallback.get(key)) for key in ("O", "F", "S", "I", "A", "W")}
        identity = "|".join(str(concept.get(key) or "") for key in ("id", "name", "category", "source_title"))
        digest = hashlib.sha256(identity.encode("utf-8")).digest()
        fallback_hue = (digest[0] / 255.0 + 0.03) % 1.0
        category = str(concept.get("category") or "abstract")
        concept_id = str(concept.get("id") or "")
        text = " ".join(str(concept.get(key) or "") for key in ("id", "name", "source_title")) + " " + " ".join(map(str, concept.get("visual_anchors") or []))
        lower = text.lower()

        body_hint = self._semantic_color_hint(text, "body")
        eye_hint = self._semantic_color_hint(text, "eye")
        secondary_hint = self._semantic_color_hint(text, "secondary")
        accent_hint = self._semantic_color_hint(text, "accent")

        # Prefer an explicit body anchor, then the AI fur hue, then any usable
        # proposal. Identical/grayscale model output gets a concept-stable hue.
        candidates = [parsed.get("F"), parsed.get("S"), parsed.get("A"), fallback_parsed.get("F")]
        seed = next((item for item in candidates if item and item[2] >= 0.12), None)
        hue = body_hint["h"] if body_hint else (seed[0] if seed else fallback_hue)
        raw_f = parsed.get("F")
        natural_categories = {"breed", "historical_breed"}
        if category in natural_categories and not body_hint and not seed:
            # Grayscale/empty AI output for a real breed should become a
            # plausible coat, not a saturated random rainbow color.
            natural_furs = (
                (0.075, 0.38, 0.48),  # warm brown
                (0.105, 0.34, 0.68),  # sand / cream
                (0.600, 0.16, 0.56),  # blue gray
                (0.080, 0.18, 0.44),  # brown gray
                (0.120, 0.16, 0.78),  # pale cream
            )
            hue, natural_saturation, natural_lightness = natural_furs[digest[0] % len(natural_furs)]
        else:
            natural_saturation = natural_lightness = None

        category_style = {
            "breed": (0.34, 0.56), "historical_breed": (0.32, 0.54),
            "food": (0.48, 0.64), "object": (0.38, 0.50),
            "profession": (0.44, 0.53), "abstract": (0.58, 0.52), "hot": (0.56, 0.55),
        }
        saturation, lightness = category_style.get(category, (0.48, 0.54))
        if natural_saturation is not None:
            saturation, lightness = natural_saturation, natural_lightness
        if raw_f and raw_f[2] >= 0.12:
            saturation = max(0.20, min(0.50 if category in natural_categories else 0.72, raw_f[2]))
            lightness = max(0.34, min(0.72, raw_f[1]))
        if body_hint:
            saturation, lightness = body_hint["s"], body_hint["l"]

        dark_theme = any(token in lower for token in ("黑猫", "黑豹", "bombay", "暗影", "黑暗", "夜猫", "him", "煤炭"))
        light_theme = any(token in lower for token in ("白猫", "雪猫", "snow", "白色主体", "奶白", "云朵"))
        if dark_theme and not body_hint:
            hue, saturation, lightness = 0.62, 0.12, 0.24
        elif light_theme and not body_hint:
            saturation, lightness = min(saturation, 0.20), 0.86

        # A few multi-colour concepts describe a relationship rather than just
        # one color word. These are general visual rules, not fixed skin themes.
        is_rgb_object = category == "object" and any(token in lower for token in ("rgb", "电路", "霓虹"))
        is_aurora = "极光" in lower or ("深夜蓝主体" in lower and "流光" in lower)
        is_banana = "香蕉" in lower or "明黄色香蕉" in lower
        if is_rgb_object:
            hue, saturation, lightness = 0.61, 0.11, 0.29
        if is_aurora:
            hue, saturation, lightness = 0.625, 0.48, 0.26
        if is_banana:
            hue, saturation, lightness = 0.145, 0.68, 0.60

        direction = -1 if digest[1] % 2 else 1
        outline_lightness = max(0.055, min(0.27, lightness - 0.32))
        outline_saturation = min(0.38, max(0.08, saturation * 0.55))

        # Natural breeds and foods stay analogous. Objects use a neutral body
        # plus one vivid signal color; abstract concepts may use wider harmony.
        if secondary_hint:
            secondary_hue, secondary_saturation, secondary_lightness = secondary_hint["h"], secondary_hint["s"], secondary_hint["l"]
        elif is_rgb_object:
            secondary_hue, secondary_saturation, secondary_lightness = 0.50, 0.72, 0.54
        elif is_aurora:
            secondary_hue, secondary_saturation, secondary_lightness = 0.46, 0.68, 0.57
        elif is_banana:
            secondary_hue, secondary_saturation, secondary_lightness = 0.105, 0.34, 0.82
        elif category in {"breed", "historical_breed", "food"}:
            secondary_hue = (hue + direction * (0.035 + digest[2] / 5100.0)) % 1.0
            secondary_saturation = max(0.16, min(0.52, saturation * 0.72))
            secondary_lightness = 0.78 if lightness < 0.60 else max(0.35, lightness - 0.25)
        else:
            secondary_raw = parsed.get("S")
            if secondary_raw and secondary_raw[2] >= 0.18 and self._hue_distance(hue, secondary_raw[0]) >= 0.06:
                secondary_hue = secondary_raw[0]
                secondary_saturation = max(0.28, min(0.68, secondary_raw[2]))
            else:
                spread = 0.10 if category in {"object", "profession"} else 0.18
                secondary_hue = (hue + direction * spread) % 1.0
                secondary_saturation = max(0.26, min(0.68, saturation * 0.92))
            secondary_lightness = max(0.30, min(0.82, lightness + (0.25 if lightness < 0.58 else -0.22)))

        if eye_hint:
            eye_hue, eye_saturation, eye_lightness = eye_hint["h"], max(0.48, eye_hint["s"]), max(0.58, min(0.72, eye_hint["l"] + 0.08))
        elif category in natural_categories:
            # Real cats mostly read better with amber, green or blue eyes than
            # with a mathematically complementary neon color.
            natural_eyes = ((0.105, 0.58, 0.62), (0.345, 0.48, 0.61), (0.585, 0.52, 0.65))
            eye_hue, eye_saturation, eye_lightness = natural_eyes[digest[3] % len(natural_eyes)]
        else:
            eye_hue = (hue + (0.38 if digest[3] % 2 else 0.48)) % 1.0
            eye_saturation, eye_lightness = 0.68, 0.68
            if parsed.get("I") and parsed["I"][2] >= 0.30 and self._hue_distance(hue, parsed["I"][0]) >= 0.16:
                eye_hue = parsed["I"][0]

        accessory_hue = accent_hint["h"] if accent_hint else (hue + direction * 0.14) % 1.0
        if not accent_hint and parsed.get("A") and parsed["A"][2] >= 0.25:
            accessory_hue = parsed["A"][0]
        if is_rgb_object:
            accessory_hue = 0.50
        elif is_aurora:
            accessory_hue = 0.78
        elif is_banana:
            accessory_hue = 0.105

        palette = {
            "O": self._hls_hex(hue, outline_lightness, outline_saturation),
            "F": self._hls_hex(hue, lightness, saturation),
            "I": self._hls_hex(eye_hue, eye_lightness, eye_saturation),
            "P": self._hls_hex(eye_hue, 0.13, 0.52),
            "N": self._hls_hex(0.975 + (digest[4] / 2550.0), 0.62, 0.54),
            "S": self._hls_hex(secondary_hue, secondary_lightness, secondary_saturation),
        }
        if max_colors >= 7 and ("A" in source or "A" in fallback):
            palette["A"] = self._hls_hex(accessory_hue, 0.60, 0.72)
        if max_colors >= 8 and ("W" in source or "W" in fallback):
            wing_hue = 0.78 if is_aurora else ((accessory_hue + 0.065) % 1.0)
            palette["W"] = self._hls_hex(wing_hue, 0.82, 0.40)

        # Semantic profiles are hard constraints, not suggestions. The name
        # “胖猫” is a recognizable blue internet-meme cat; letting either the
        # model palette or the hash-derived fallback choose another body hue
        # destroys the identity even when the pixel layout is valid.
        if concept.get("semantic_profile") == "hot_meme_blue_cat":
            palette["O"] = "#183B61"
            palette["F"] = "#358ED0"
            palette["S"] = "#8BD0F4"
            palette["I"] = "#F7D85C"
            palette["P"] = "#142A3C"
            palette["N"] = "#E27A8E"
            if max_colors >= 7:
                palette["A"] = "#5DB7F0"
            if max_colors >= 8:
                palette["W"] = "#A9E3FF"
        return palette

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

    @staticmethod
    def _concept_source(concept: dict) -> dict:
        return {
            key: concept.get(key)
            for key in ("source_name", "source_title", "source_id", "source_url", "collected_at", "rank")
            if concept.get(key) not in (None, "")
        }

    @staticmethod
    def _clean_generated_name(raw_name: object, concept: dict, blocked_names: set[str]) -> str:
        if concept.get("name_locked"):
            return str(concept.get("name") or "像素猫")
        name = re.sub(r"[\s《》【】（）()，,。.!！?？:：]+", "", str(raw_name or ""))
        if not name.endswith("猫") or not (2 <= len(name) <= 4) or name in blocked_names:
            base = str(concept.get("name") or "像素猫")
            if base not in blocked_names:
                return base
            stem = base[:-1] if base.endswith("猫") else base
            for suffix in ("影猫", "灵猫", "像素猫", "小猫"):
                candidate = (stem[:5] + suffix)[:10]
                if candidate not in blocked_names:
                    return candidate
            return base
        return name

    @staticmethod
    def _visual_signature(skin: dict) -> tuple[str, ...]:
        frames = skin.get("frames") if isinstance(skin, dict) else {}
        rows = frames.get("a") if isinstance(frames, dict) else None
        palette = skin.get("palette") if isinstance(skin, dict) else {}
        if not isinstance(rows, list) or len(rows) != 16 or not isinstance(palette, dict):
            return tuple()
        # Compare rendered colors, not semantic letters. This keeps fixed-body
        # cats distinguishable when their palette changes but their anatomy does not.
        return tuple(palette.get(code, ".") for row in rows[:12] for code in str(row)[:16])

    @staticmethod
    def _visual_distance(left: tuple[str, ...], right: tuple[str, ...]) -> int:
        if not left or not right:
            return 999
        return sum(a != b for a, b in zip(left, right))

    def _is_distinct_visual(self, skin: dict, recent_skins: list[dict]) -> bool:
        signature = self._visual_signature(skin)
        known = [self._visual_signature(item) for item in recent_skins[-20:]]
        with self._recent_lock:
            known.extend(self._recent_visuals)
        threshold = int(os.environ.get("CAT_SKIN_MIN_PIXEL_DIFFERENCE", "14"))
        return all(self._visual_distance(signature, other) >= threshold for other in known if other)

    def _remember_open_skin(self, skin: dict) -> None:
        signature = self._visual_signature(skin)
        with self._recent_lock:
            self._recent_visuals.append(signature)
            self._recent_names.append(str(skin.get("name") or ""))
            del self._recent_visuals[:-24]
            del self._recent_names[:-20]

    def _pattern_coordinate_pool(self) -> list[tuple[int, int]]:
        """Only fur-fill cells; outline, face foreground, tail geometry and gait stay frozen."""
        return [
            (cell["x"], cell["y"])
            for cell in self.master["cells"]
            if cell.get("pattern_allowed")
            and not cell.get("final_face_foreground")
            and cell.get("base_code") in {"F", "S"}
            and cell.get("base_part") not in {"tail", "tail_root", "leg", "paw"}
        ]

    def _procedural_concept_paint(self, concept: dict, rarity: str, rng: random.Random) -> list[list[object]]:
        """Template-safe emergency markings when AI is absent or returns no usable operations."""
        pool = self._pattern_coordinate_pool()
        rng.shuffle(pool)
        amount = {"common": 5, "rare": 8, "epic": 12, "legendary": 16}[rarity]
        return [[x, y, "S"] for x, y in pool[:amount]]

    @staticmethod
    def _normalize_operations(value: object) -> list[tuple[int, int, str]]:
        result = []
        if not isinstance(value, list):
            return result
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                continue
            try:
                x, y = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
            code = str(item[2])[:1]
            if 0 <= x < 16 and 0 <= y < 16 and code:
                result.append((x, y, code))
        return result

    @staticmethod
    def _largest_component(points: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
        by_xy = {(x, y): code for x, y, code in points}
        remaining = set(by_xy)
        components = []
        while remaining:
            stack = [remaining.pop()]
            component = set(stack)
            while stack:
                x, y = stack.pop()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        neighbor = (x + dx, y + dy)
                        if neighbor in remaining:
                            remaining.remove(neighbor)
                            component.add(neighbor)
                            stack.append(neighbor)
            components.append(component)
        if not components:
            return []
        largest = max(components, key=len)
        return [(x, y, by_xy[(x, y)]) for x, y in sorted(largest, key=lambda p: (p[1], p[0]))]

    @staticmethod
    def _resolve_headwear_style(accessory: dict, concept: dict) -> str | None:
        """Pick a headwear template. Explicit AI style wins, then concept hints."""
        style = str(accessory.get("style") or "").strip().lower()
        if style in HEADWEAR_TEMPLATES:
            return style
        text = " ".join(str(concept.get(key) or "") for key in ("id", "name", "source_title")) + " " + " ".join(map(str, concept.get("visual_anchors") or []))
        lower = text.lower()
        if any(token in lower for token in ("国王", "王冠", "皇冠", "金冠", "小王", "king", "crown")):
            return "crown"
        if any(token in lower for token in ("光环", "天使", "halo", "angel")):
            return "halo"
        if any(token in lower for token in ("恶魔", "角", "horns", "demon", "devil")):
            return "horns"
        if any(token in lower for token in ("帽子", "法师帽", "cap", "hat")):
            return "cap"
        return None

    @staticmethod
    def _deblob_ops(ops: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
        """Keep the outline of an over-dense decoration so it never renders as a blob."""
        coords = {(x, y) for x, y, _ in ops}
        if len(coords) < 4:
            return ops
        min_x = min(x for x, _ in coords)
        max_x = max(x for x, _ in coords)
        min_y = min(y for _, y in coords)
        max_y = max(y for _, y in coords)
        area = (max_x - min_x + 1) * (max_y - min_y + 1)
        if area <= 0 or len(coords) / area < 0.62:
            return ops
        outlined = []
        for x, y, code in ops:
            filled_neighbors = sum(
                1 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                if (dx or dy) and (x + dx, y + dy) in coords
            )
            if filled_neighbors < 8:
                outlined.append((x, y, code))
        return outlined if len(outlined) >= 2 else ops

    def _sanitize_template_plan(self, raw: object, rarity: str, palette: dict, concept: dict, rng: random.Random) -> tuple[list[tuple[int, int, str]], str, list[tuple[int, int, str]], bool]:
        raw = raw if isinstance(raw, dict) else {}
        pattern_allowed = set(self._pattern_coordinate_pool())
        paint_limit = {"common": 8, "rare": 14, "epic": 20, "legendary": 26}[rarity]
        paint = []
        seen = set()
        for x, y, code in self._normalize_operations(raw.get("paint")):
            if (x, y) in pattern_allowed and code in palette and code not in {".", "O", "I", "P", "N"} and (x, y) not in seen:
                seen.add((x, y))
                paint.append((x, y, code))
            if len(paint) >= paint_limit:
                break
        if not paint:
            paint = [tuple(item) for item in self._procedural_concept_paint(concept, rarity, rng)]

        accessory = raw.get("accessory") if isinstance(raw.get("accessory"), dict) else {}
        zone = str(accessory.get("zone") or "none")
        if zone == "none" and rarity in {"epic", "legendary"} and self._resolve_headwear_style(accessory, concept):
            # 概念明确要求皇冠/光环/角/帽子时，即使 AI 未选装饰也补上
            zone = "headwear"
        floating = bool(accessory.get("floating")) and zone == "headwear"
        if rarity not in {"epic", "legendary"} or zone not in self.master.get("overlay_zones", {}):
            return paint, "none", [], False
        contract = self.master["overlay_zones"][zone]
        if rarity not in contract.get("rarities", []):
            return paint, "none", [], False
        allowed = {tuple(point) for point in contract.get("allowed", [])}
        if zone == "headwear":
            style = self._resolve_headwear_style(accessory, concept)
            if style:
                template_ops = [op for op in HEADWEAR_TEMPLATES[style] if op[:2] in allowed and op[2] in palette]
                if len(template_ops) >= 3:
                    return paint, "headwear", template_ops, False
        # These are the user's frozen identity/anatomy pixels. Overlay contracts
        # may be expanded later, but no accessory may ever obscure them.
        globally_protected = {
            "eye", "pupil", "nose", "mouth_corner", "mouth_center", "chin",
            "tail", "tail_root", "rump_boundary", "leg", "paw",
        }
        forbidden = set(contract.get("forbidden_parts", [])) | globally_protected
        max_pixels = 18 if rarity == "epic" else 26
        ops = []
        seen = set()
        for x, y, code in self._normalize_operations(accessory.get("pixels")):
            cell = self.master_cells.get((x, y), {})
            if (x, y) not in allowed or cell.get("base_part") in forbidden or code not in palette or code == "." or (x, y) in seen:
                continue
            seen.add((x, y))
            ops.append((x, y, code))
            if len(ops) >= max_pixels:
                break
        ops = self._largest_component(ops)
        if len(ops) < 2:
            return paint, "none", [], False
        ops = self._deblob_ops(ops)
        coords = {(x, y) for x, y, _ in ops}
        if zone == "wing":
            attachment = {tuple(point) for point in contract.get("attachment_zone", [])}
            if not coords & attachment:
                return paint, "none", [], False
        elif not floating:
            base = {(x, y) for y, row in enumerate(self.classic["frames"]["a"]) for x, code in enumerate(row) if code != "."}
            if not coords & base and not any((x + dx, y + dy) in base for x, y in coords for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                return paint, "none", [], False
        return paint, zone, ops, floating

    def _render_template_concept(self, rarity: str, concept: dict, raw: object, rng: random.Random, blocked_names: set[str], fallback_palette: dict | None = None) -> dict:
        limits = self.anatomy["rarity_limits"][rarity]
        raw = raw if isinstance(raw, dict) else {}
        fallback_palette = fallback_palette or self.classic["palette"]
        palette = self._harmonize_concept_palette(raw.get("palette"), fallback_palette, limits["max_palette_colors"], concept)
        paint, accessory_zone, accessory_ops, floating = self._sanitize_template_plan(raw, rarity, palette, concept, rng)
        matrix_a = [list(row) for row in self.classic["frames"]["a"]]
        matrix_b = [list(row) for row in self.classic["frames"]["b"]]
        for x, y, code in [*paint, *accessory_ops]:
            matrix_a[y][x] = code
            matrix_b[y][x] = code
        name = self._clean_generated_name(raw.get("name"), concept, blocked_names)
        effect = str(raw.get("effect") or "none")
        valid_effects = {"spark", "star", "halo", "web", "royal", "shadow", "coin", "flame"}
        if rarity == "common" or effect not in valid_effects:
            effect = "none" if rarity == "common" else ("star" if rarity in {"epic", "legendary"} else "spark")
        skin = json.loads(json.dumps(self.classic))
        skin.update({
            "name": name,
            "rarity": rarity,
            "theme": "open:" + str(concept.get("id") or "unknown"),
            "pattern_type": str(concept.get("pattern") or "complex"),
            "palette": palette,
            "frames": {"a": ["".join(row) for row in matrix_a], "b": ["".join(row) for row in matrix_b]},
            "floating_regions": ([{"type": "halo", "pixels": [[x, y] for x, y, _ in accessory_ops]}] if floating else []),
            "effect": effect,
            "concept_id": str(concept.get("id") or ""),
            "concept_name": str(concept.get("name") or ""),
            "concept_category": str(concept.get("category") or "abstract"),
            "concept_anchors": [str(item) for item in concept.get("visual_anchors", []) if str(item).strip()],
            "concept_source": self._concept_source(concept),
            "semantic_profile": str(concept.get("semantic_profile") or ""),
            "visual_direction": str(concept.get("visual_direction") or ""),
            "design_recipe": {
                "template": "classic-black-master-v1", "generator": "ai-coordinate-plan",
                "concept_id": str(concept.get("id") or ""), "pattern_operations": [list(op) for op in paint],
                "accessory_zone": accessory_zone, "accessory_operations": [list(op) for op in accessory_ops],
            },
            "design_notes": {"recognizable_features": list(concept.get("visual_anchors") or []), "animation_change": "固定经典黑猫猫体与步态；AI仅提交获准坐标的配色、花纹和装饰。"},
        })
        if accessory_zone == "wing":
            skin["parts"]["wing"] = [[x, y] for x, y, _ in accessory_ops]
        elif accessory_zone == "headwear":
            skin["parts"]["head_accessory"] = [[x, y] for x, y, _ in accessory_ops]
        errors = validate_data(skin, self.anatomy)
        if errors:
            raise CatGenerationError("固定模板概念稿未通过结构校验：" + "；".join(errors[:5]))
        return skin

    @staticmethod
    def enrich_concept(concept: dict) -> dict:
        """Resolve a human label into a stable, renderer-facing visual brief."""
        enriched = dict(concept or {})
        name = str(enriched.get("name") or "").strip()
        brief = CONCEPT_BRIEFS.get(name)
        if not brief:
            return enriched
        for key, value in brief.items():
            if key == "visual_anchors":
                existing = [str(item).strip() for item in enriched.get(key, []) if str(item).strip()]
                enriched[key] = list(dict.fromkeys(list(value) + existing))
            elif not enriched.get(key):
                enriched[key] = value
        enriched["semantic_profile"] = "hot_meme_blue_cat" if name in {"胖猫", "胖猫猫"} else name
        return enriched

    def _generate_from_concept(self, rarity: str, concept: dict, rng: random.Random, blocked_names: set[str], variation_hint: str = "") -> dict:
        concept = self.enrich_concept(concept)
        limits = self.anatomy["rarity_limits"][rarity]
        fallback = self._base_for(rarity, str(concept.get("id") or ""), rng)
        anchors = [str(item) for item in concept.get("visual_anchors", []) if str(item).strip()]
        pattern_coords = [list(point) for point in self._pattern_coordinate_pool()]
        zones = {
            name: contract.get("allowed", [])
            for name, contract in self.master.get("overlay_zones", {}).items()
            if rarity in contract.get("rarities", [])
        }
        prompt = f"""把开放概念设计成固定经典黑猫模板的一套像素操作计划。绝对不要输出完整图片或frame。
概念：{concept.get('name')}；类别：{concept.get('category', 'abstract')}；稀有度：{rarity}。
视觉锚点：{'；'.join(anchors) or '提炼最有辨识度的颜色、花纹或物件'}；来源：{concept.get('source_title') or '本地概念库'}。
视觉执行方向：{concept.get('visual_direction') or '先从名称和视觉锚点提炼唯一主视觉，再选择配色和花纹；不要凭空添加与概念无关的职业、人物或道具。'}
这是一个“视觉 brief → 受限像素操作”的任务，不是根据名字自由改编；主视觉必须能从最终调色板和花纹中一眼看出。
{variation_hint}
猫的头、圆颚、平下巴、双眼双竖瞳、小鼻子、两点猫嘴、身体、封闭臀部、腿部动画和尾巴已经由程序永久锁定，禁止重画、删除、移动或返回frame_a。
你只能在这些毛色坐标中选择像素：{json.dumps(pattern_coords, ensure_ascii=False, separators=(',', ':'))}
可用装饰区及精确坐标：{json.dumps(zones, ensure_ascii=False, separators=(',', ':')) if zones else '本稀有度不允许装饰'}
只输出JSON：{{"name":"自然简洁且以猫结尾的中文名","palette":{{"O":"#RRGGBB","F":"#RRGGBB","I":"#RRGGBB","P":"#RRGGBB","N":"#RRGGBB","S":"#RRGGBB","A":"#RRGGBB","W":"#RRGGBB"}},"paint":[[x,y,"S或A或W"]],"accessory":{{"zone":"none|headwear|wing|cape|face_costume","style":"crown|halo|horns|cap|none","pixels":[[x,y,"A或W"]],"floating":false}},"effect":"none|spark|star|halo|web|royal|shadow|coin|flame"}}。
颜色只用于表达主题方向，程序会自动校正明度、轮廓对比、眼睛可读性和整体和谐度；不得把多个语义色设成相同颜色。最多{limits['max_palette_colors']}种颜色。普通/稀有不得画装饰；史诗/传说装饰必须全部落在同一个允许区并连成整体，翅膀必须触碰连接区。头部装饰只填写 style，其形状由程序模板控制，无需在 pixels 里画。花纹不能是孤立噪点。命名要求：{'必须原样使用“'+str(concept.get('name'))+'”' if concept.get('name_locked') else '2到4个汉字且以“猫”结尾，贴合造型、简洁自然，并避开近期名字：'+('、'.join(sorted(blocked_names)) or '无')}。"""
        raw = _extract_json(self._chat([
            {"role": "system", "content": "你只为固定16×16猫模板提交受限坐标操作，不画轮廓，不输出frame。只返回JSON。"},
            {"role": "user", "content": prompt},
        ]))
        return self._render_template_concept(rarity, concept, raw, rng, blocked_names, fallback.get("palette", self.classic["palette"]))

    def _fallback_for_concept(self, rarity: str, concept: dict, rng: random.Random, blocked_names: set[str]) -> dict:
        """No-key/model-error fallback still uses the exact frozen body template."""
        concept = self.enrich_concept(concept)
        fallback = self._base_for(rarity, str(concept.get("id") or ""), rng)
        raw = {
            "name": str(concept.get("name") or "像素猫"),
            "palette": fallback.get("palette", self.classic["palette"]),
            "paint": self._procedural_concept_paint(concept, rarity, rng),
            "accessory": {"zone": "none", "pixels": [], "floating": False},
            "effect": "star" if rarity in {"epic", "legendary"} else "spark" if rarity == "rare" else "none",
        }
        skin = self._render_template_concept(rarity, concept, raw, rng, blocked_names, fallback.get("palette", self.classic["palette"]))
        skin["generation_fallback"] = True
        return skin

    def _generate_legacy(self, recent_skins: list[dict], rarity: str, rng: random.Random) -> dict:
        stored_themes = [str(skin.get("theme") or "") for skin in recent_skins[-5:]]
        stored_signatures = {self._stored_signature(skin) for skin in recent_skins[-20:]}
        stored_signatures.discard("")
        stored_names = {str(skin.get("name") or "") for skin in recent_skins[-20:]}
        stored_names.discard("")
        with self._recent_lock:
            blocked_themes = set(stored_themes + self._recent_themes[-5:])
            blocked_signatures = stored_signatures | set(self._recent_signatures[-20:])
            blocked_names = stored_names | set(self._recent_names[-20:])
        candidates = [item for item in RARITY_THEMES[rarity] if item[0] not in blocked_themes] or list(RARITY_THEMES[rarity])
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

    @staticmethod
    def _clean_concept_name(value: object) -> str:
        name = re.sub(r"[\s《》【】\[\]（）()：:，,。.!！?？‘’“”\-—_]+", "", str(value or ""))
        if name.endswith("猫") and 2 <= len(name) <= 4:
            return name
        return ""

    @staticmethod
    def _ai_concept_id(name: str, anchors: list[str]) -> str:
        key = name + "|" + "|".join(sorted(str(item) for item in anchors))
        return "ai_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def _validated_ai_concept(self, raw: object, blocked_names: set[str]) -> dict | None:
        raw = raw if isinstance(raw, dict) else {}
        name = self._clean_concept_name(raw.get("name"))
        if not name or name in blocked_names:
            return None
        category = str(raw.get("category") or "")
        if category not in {"breed", "food", "object", "profession", "abstract", "hot"}:
            return None
        anchors = [str(item).strip() for item in raw.get("visual_anchors") or [] if str(item).strip()]
        if not anchors:
            return None
        pattern = str(raw.get("pattern") or "complex")
        return {
            "id": self._ai_concept_id(name, anchors[:3]),
            "name": name,
            "category": category,
            "pattern": pattern,
            "visual_anchors": anchors[:3],
            "source_name": "AI概念合成",
            "source_title": "由现有概念组合/扩写而来",
        }

    def _ai_free_concept(self, seeds: list[dict], blocked_names: set[str]) -> dict | None:
        seed_lines = "\n".join(
            f"- {c.get('name')}（{c.get('category')}）：{'；'.join(str(a) for a in c.get('visual_anchors') or [])}"
            for c in seeds
        )
        blocked = '、'.join(sorted(blocked_names)) or '无'
        prompt = f"""基于下面的灵感概念，发散创造一个全新的、有辨识度的猫咪概念。不要复制灵感概念本身。
灵感概念：
{seed_lines}
要求：
- 名称 2-6 个汉字，以“猫”结尾，简洁自然，避开近期名字：{blocked}
- 分类从 breed/food/object/profession/abstract/hot 中选一个
- pattern 从 solid/tabby/tuxedo/point/calico/spotted/dorsal/patchwork/panel/spider/glitch/scale/complex 中选一个
- 给出 2-3 个视觉锚点，分别描述颜色、花纹、标志物件或气质
只输出JSON：{{"name":"...","category":"...","pattern":"...","visual_anchors":["...","..."]}}"""
        raw = _extract_json(self._chat([
            {"role": "system", "content": "你是猫咪皮肤系统的概念设计师。只返回JSON，不要解释。"},
            {"role": "user", "content": prompt},
        ]))
        return self._validated_ai_concept(raw, blocked_names)

    def _ai_combine_concept(self, left: dict, right: dict, blocked_names: set[str]) -> dict | None:
        def describe(concept: dict) -> str:
            anchors = '；'.join(str(a) for a in concept.get('visual_anchors') or [])
            return f"{concept.get('name')}（{concept.get('category')}）：{anchors}"
        blocked = '、'.join(sorted(blocked_names)) or '无'
        prompt = f"""基于两个概念组合或变异出一个全新的猫咪概念。可以融合两者特征，或保留一个的核心意象再加另一个的元素，但不能只是简单拼接名字。
概念A：{describe(left)}
概念B：{describe(right)}
要求：
- 名称 2-6 个汉字，以“猫”结尾，简洁自然，避开近期名字：{blocked}
- 分类从 breed/food/object/profession/abstract/hot 中选一个
- pattern 从 solid/tabby/tuxedo/point/calico/spotted/dorsal/patchwork/panel/spider/glitch/scale/complex 中选一个
- 给出 2-3 个视觉锚点，分别描述颜色、花纹、标志物件或气质
只输出JSON：{{"name":"...","category":"...","pattern":"...","visual_anchors":["...","..."]}}"""
        raw = _extract_json(self._chat([
            {"role": "system", "content": "你是猫咪皮肤系统的概念设计师。只返回JSON，不要解释。"},
            {"role": "user", "content": prompt},
        ]))
        return self._validated_ai_concept(raw, blocked_names)

    def _ai_synthesize_concept(self, rarity: str, rng: random.Random, recent_ids: set[str], blocked_names: set[str]) -> dict | None:
        concepts = self.concept_store.all_concepts()
        available = [c for c in concepts if str(c.get("id") or "") not in recent_ids]
        if not available:
            available = concepts
        if not available:
            return None
        try:
            if len(available) >= 2 and rng.random() < 0.6:
                left, right = rng.sample(available, 2)
                concept = self._ai_combine_concept(left, right, blocked_names)
            else:
                seeds = rng.sample(available, min(2, len(available)))
                concept = self._ai_free_concept(seeds, blocked_names)
            if concept is not None:
                concept["rarity"] = rarity
                return concept
        except (CatGenerationError, ValueError, json.JSONDecodeError) as exc:
            print(f"  [cat-skin] AI concept synthesis failed: {exc}", flush=True)
        return None

    def _ai_category_concept(self, category: str, hint: str, blocked_names: set[str]) -> dict | None:
        """Invent one concrete concept inside a free category such as food/object."""
        blocked = '、'.join(sorted(blocked_names)) or '无'
        prompt = f"""在大类“{hint or category}”里，自由想一个具体的猫咪概念。
要求：
- 名称 2-4 个汉字，以“猫”结尾，简洁自然，避开近期名字：{blocked}
- 给出 1-2 个视觉锚点，分别描述颜色、花纹、标志物件或气质
- 不要与近期名字重复，要有记忆点
只输出JSON：{{"name":"...","visual_anchors":["...","..."]}}"""
        raw = _extract_json(self._chat([
            {"role": "system", "content": "你是猫咪皮肤系统的概念设计师。只返回JSON，不要解释。"},
            {"role": "user", "content": prompt},
        ]))
        name = self._clean_concept_name(raw.get("name") if isinstance(raw, dict) else "")
        if not name or name in blocked_names:
            return None
        anchors = [str(item).strip() for item in (raw.get("visual_anchors") if isinstance(raw, dict) else []) or [] if str(item).strip()]
        if not anchors:
            return None
        return {
            "id": category + "_ai_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12],
            "name": name,
            "category": category,
            "pattern": "complex",
            "visual_anchors": anchors[:2],
            "source_name": "AI大类生成",
            "source_title": hint,
        }

    def generate_with_history(self, recent_skins: list[dict] | None = None, rng: random.Random | None = None) -> dict:
        """Generate from an open concept; the old theme assembler is fallback only."""
        rng = rng or random.SystemRandom()
        recent_skins = recent_skins or []
        rarity = self._draw_rarity(rng)
        if self.concept_store is None:
            return self._generate_legacy(recent_skins, rarity, rng)

        recent_ids = {str(item.get("concept_id") or "") for item in recent_skins[-30:]}
        recent_ids.discard("")
        blocked_names = {str(item.get("name") or "") for item in recent_skins[-20:]}
        with self._recent_lock:
            blocked_names.update(self._recent_names[-20:])
        concept = self.concept_store.choose(rarity, recent_ids, rng)
        if not concept:
            return self._generate_legacy(recent_skins, rarity, rng)
        # free 大类（食物/物品/职业/抽象）：抽中后交给 AI 现场生成一个具体概念
        if concept.get("_ai_category"):
            category = str(concept.get("category") or "")
            hint = str(concept.get("hint") or "")
            ai_concept = None
            if self.provider_info()["configured"]:
                ai_concept = self._ai_category_concept(category, hint, blocked_names)
            if ai_concept is None:
                fallback = self.concept_store.category_concept("breed", recent_ids, rng)
                if fallback is None:
                    return self._generate_legacy(recent_skins, rarity, rng)
                concept = fallback
            else:
                concept = ai_concept

        if not self.provider_info()["configured"]:
            return self._fallback_for_concept(rarity, concept, rng, blocked_names)

        attempts = max(1, min(3, int(os.environ.get("CAT_SKIN_AI_ATTEMPTS", "2"))))
        last_error = None
        for attempt in range(attempts):
            try:
                hint = "" if attempt == 0 else "上一稿与近期猫过于相似；请改变主副色、毛色坐标布局或获准装饰区内的主题符号，但绝不能改变固定猫体。"
                skin = self._generate_from_concept(rarity, concept, rng, blocked_names, hint)
                if self._is_distinct_visual(skin, recent_skins):
                    self._remember_open_skin(skin)
                    return skin
                last_error = CatGenerationError("像素布局与最近猫咪过于相似")
            except (CatGenerationError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                print(f"  [cat-skin] open concept attempt {attempt + 1} failed: {exc}", flush=True)
        print(f"  [cat-skin] open concept fallback for {concept.get('id')}: {last_error or 'too similar'}", flush=True)
        return self._fallback_for_concept(rarity, concept, rng, blocked_names)

    def generate(self, rng: random.Random | None = None) -> dict:
        return self.generate_with_history([], rng)


class CatSkinManager:
    def __init__(self, state_path: Path, classic_path: Path, generator: CatSkinGenerator, today_fn: Callable[[], date] | None = None, task_completed_fn: Callable[[dict, str], bool] | None = None):
        self.state_path = state_path
        self.classic = _read_json(classic_path)
        self.catalog = _read_json(classic_path.parent / "catalog-v1.json")
        self.catalog_by_id = {skin["id"]: skin for skin in self.catalog}
        self.generator = generator
        self.today_fn = today_fn or date.today
        self.task_completed_fn = task_completed_fn or (lambda user, today: False)
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

    @staticmethod
    def _opens_today(state: dict, today: str) -> int:
        # Backward compatible with the old one-open-per-day schema.
        if state.get("open_date") == today:
            try:
                return max(0, min(2, int(state.get("open_count") or 0)))
            except (TypeError, ValueError):
                return 0
        return 1 if state.get("last_open_date") == today else 0

    def _task_completed(self, user: dict, today: str) -> bool:
        try:
            return bool(self.task_completed_fn(user, today))
        except Exception:
            return False

    @staticmethod
    def _limit_error(opens_today: int, task_completed: bool) -> None:
        if opens_today >= 2:
            raise CatDailyLimitError("今日领取猫咪已达上限")
        if opens_today == 1 and not task_completed:
            raise CatDailyTaskRequiredError("完成每日任务后可再次领取猫咪")

    def wardrobe(self, user: dict) -> dict:
        with self._lock:
            state = self._user_state(self._load(), user["user_id"])
            today = self.today_fn().isoformat()
            is_admin = user.get("role") == "admin"
            opens_today = self._opens_today(state, today)
            task_completed = is_admin or self._task_completed(user, today)
            can_open = is_admin or opens_today == 0 or (opens_today == 1 and task_completed)
            builtins = json.loads(json.dumps(self.catalog))
            generated = json.loads(json.dumps(state.get("skins", [])))
            for skin in builtins:
                skin["releasable"] = False
            for skin in generated:
                skin["releasable"] = True
            return {
                "ok": True,
                "is_admin": is_admin,
                "can_open": can_open,
                "next_open_date": "不限次数" if is_admin else (today if can_open else "明天"),
                "opens_today": opens_today,
                "daily_limit": None if is_admin else 2,
                "daily_task": {
                    "label": "完成一次飞书任务 Agent 生成任务",
                    "completed": task_completed,
                },
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
            opens_today = self._opens_today(state, today)
            task_completed = is_admin or self._task_completed(user, today)
            if not is_admin:
                self._limit_error(opens_today, task_completed)
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
                "description": (
                    "从“" + str(skin.get("concept_name") or skin.get("name") or "开放概念") + "”提炼意象，由 AI 逐像素设计的 " + RARITY_LABELS[skin["rarity"]] + " 猫咪。"
                    if skin.get("concept_id") and not skin.get("generation_fallback")
                    else THEME_DESCRIPTIONS.get(
                        str(skin.get("theme") or ""),
                        "由开放概念生成的 " + RARITY_LABELS[skin["rarity"]] + " 猫咪。",
                    )
                ),
                "effect": skin.get("effect") or ("star" if skin["rarity"] in {"epic", "legendary"} else "spark" if skin["rarity"] == "rare" else "none"),
                "created_at": now,
            })
            with self._lock:
                data = self._load()
                state = self._user_state(data, user_id)
                # Re-check after the slow model call to close the concurrent-request gap.
                opens_today = self._opens_today(state, today)
                task_completed = is_admin or self._task_completed(user, today)
                if not is_admin:
                    self._limit_error(opens_today, task_completed)
                state.setdefault("skins", []).append(skin)
                state["equipped_skin_id"] = skin["id"]
                if not is_admin:
                    state["last_open_date"] = today
                    state["open_date"] = today
                    state["open_count"] = opens_today + 1
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
