"""Persistent open-world cat concepts and automatic Douyin hot-topic refresh."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return fallback


def _safe_id(value: str) -> str:
    ascii_id = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if ascii_id:
        return ascii_id[:64]
    import hashlib
    return "cn_" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _short_cat_name(title: str) -> str:
    cleaned = re.sub(r"[#@\s《》【】\[\]（）()：:，,。.!！?？‘’“”\-—_]+", "", title)
    cleaned = re.sub(r"^(抖音|热点|热搜|话题)", "", cleaned)
    if cleaned.endswith("猫") and 2 <= len(cleaned) <= 6:
        return cleaned
    return (cleaned[:4] or "热点") + "猫"


def _clean_cat_name(value: object) -> str:
    """Accept an AI-proposed cat name only when it is short and ends with 猫."""
    name = re.sub(r"[\s《》【】\[\]（）()：:，,。.!！?？‘’“”\-—_]+", "", str(value or ""))
    if name.endswith("猫") and 2 <= len(name) <= 4:
        return name
    return ""


# 不适合做成娱乐猫咪皮肤的话题特征。程序先粗筛掉最明显的严肃内容，
# 再由 AI 精筛“是否有梗、是否够娱乐化”。这里刻意不碰“去世/死亡”等
# 可能属于名人梗的词，把这类判断留给 AI。
SERIOUS_TOPIC_PATTERNS = (
    # 灾难与事故
    "地震", "泥石流", "洪水", "火灾", "爆炸", "坠毁", "坍塌", "泄漏", "伤亡", "遇难", "失踪",
    # 时政、司法、军事、外交
    "政府", "国家", "军队", "军事", "国防", "国安", "公安", "法院", "检察院", "纪委", "监委",
    "外交", "政策", "法规", "部长", "主席", "总理", "总统", "战争", "导弹", "军演",
    "间谍", "泄密", "逮捕", "判刑", "立案", "谣言案例", "造谣", "被罚", "处罚", "拘留", "辟谣",
    # 严肃经济与宏观
    "PMI", "GDP", "CPI", "央行", "证监会", "利率", "通胀", "失业率", "制造业", "宏观经济",
    # 严重公共卫生事件
    "疫情", "病毒", "确诊",
)


def _exclude_serious(topics: list[dict]) -> list[dict]:
    kept = []
    for topic in topics:
        title = str(topic.get("title") or "")
        if any(token in title for token in SERIOUS_TOPIC_PATTERNS):
            continue
        kept.append(topic)
    return kept


# 大类目录：free=True 表示该大类不在本地维护具体概念，抽中后交给 AI 现场生成。
CATEGORY_SPECS = {
    "breed": {
        "label": "常见品种",
        "free": False,
        "samples": [
            "橘猫", "蓝猫", "奶牛猫", "白猫", "狸花猫", "黑白猫", "三花猫",
            "暹罗猫", "布偶猫", "英短猫", "美短猫", "缅因猫", "波斯猫",
            "金渐层猫", "银渐层猫", "大橘猫", "奶油猫",
        ],
    },
    "exotic": {
        "label": "稀有/冷门品种",
        "free": False,
        "samples": [
            "斯芬克斯猫", "萨凡纳猫", "玩具虎猫", "彼得秃猫", "曼岛猫", "狼猫",
            "孟买猫", "埃及猫", "挪威森林猫", "柯尼斯卷毛猫", "德文卷毛猫",
            "拉波猫", "塞尔凯克卷毛猫", "墨西哥无毛猫", "俄勒冈卷毛猫", "垂耳猫",
        ],
    },
    "food": {
        "label": "食物",
        "free": True,
        "hint": "任意常见食物或饮品，如西瓜、香蕉、草莓、奶茶、火锅、饺子、冰淇淋",
    },
    "object": {
        "label": "物品",
        "free": True,
        "hint": "任意日常、科技或文化物品，如显卡、路由器、纸箱、红绿灯、青花瓷、手机",
    },
    "profession": {
        "label": "职业",
        "free": True,
        "hint": "任意职业或角色，如程序员、产品经理、剪辑师、考古学家、法师、医生",
    },
    "abstract": {
        "label": "抽象概念",
        "free": True,
        "hint": "任意抽象意象或情绪，如极光、黑洞、404、缓存、周一、台风、思念",
    },
    "hot": {
        "label": "热点",
        "free": False,
        "samples": [],
    },
}

# 第一层：大类抽取权重。权重只决定大类概率，不受该大类概念数量影响。
CATEGORY_WEIGHTS = {
    "common": {"breed": 70, "exotic": 6, "food": 6, "object": 5, "profession": 4, "abstract": 4, "hot": 5},
    "rare": {"breed": 50, "exotic": 15, "food": 8, "object": 8, "profession": 7, "abstract": 6, "hot": 6},
    "epic": {"breed": 5, "exotic": 8, "food": 6, "object": 8, "profession": 10, "abstract": 23, "hot": 40},
    "legendary": {"breed": 4, "exotic": 10, "food": 5, "object": 7, "profession": 8, "abstract": 32, "hot": 34},
}


class CatConceptStore:
    """Mixes durable seeds with a periodically refreshed Douyin trend cache.

    No moderation queue is used. Source title, URL/id and collection time are
    retained so a generated concept remains traceable instead of being invented
    by the model without provenance.
    """

    DOUYIN_HOT_URL = "https://open.douyin.com/hotsearch/trending/sentences/"
    DOUYIN_TOKEN_URL = "https://open.douyin.com/oauth/client_token/"

    def __init__(self, seed_path: Path, state_path: Path, opener: Callable | None = None, topic_filter: Callable | None = None):
        self.seed_path = seed_path
        self.state_path = state_path
        self.opener = opener or urllib.request.urlopen
        self.topic_filter = topic_filter
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.seeds = _read_json(seed_path, [])
        if not isinstance(self.seeds, list):
            self.seeds = []

    def _filter_topics(self, topics: list[dict]) -> list[dict]:
        """Coarse programmatic filter first, then an optional AI entertainment pass."""
        kept = _exclude_serious(topics)
        if self.topic_filter is not None:
            try:
                kept = self.topic_filter(kept)
            except Exception as exc:
                print(f"  [cat-trends] topic filter failed: {exc}", flush=True)
        return kept

    def _load_state(self) -> dict:
        data = _read_json(self.state_path, {})
        return data if isinstance(data, dict) else {}

    def _save_state(self, data: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.state_path)

    def status(self) -> dict:
        with self._lock:
            state = self._load_state()
            hot = state.get("hot_concepts") if isinstance(state.get("hot_concepts"), list) else []
            return {
                "seed_count": len(self.seeds),
                "hot_count": len(hot),
                "last_attempt_at": state.get("last_attempt_at"),
                "last_success_at": state.get("last_success_at"),
                "last_error": state.get("last_error", ""),
                "source": state.get("source", "not_configured"),
                "auto_refresh_hours": int(os.environ.get("CAT_TREND_REFRESH_HOURS", "6")),
                "configured": bool(
                    os.environ.get("DOUYIN_OPEN_ACCESS_TOKEN")
                    or (os.environ.get("DOUYIN_OPEN_CLIENT_KEY") and os.environ.get("DOUYIN_OPEN_CLIENT_SECRET"))
                    or os.environ.get("CAT_TREND_FEED_URL")
                ),
            }

    def all_concepts(self) -> list[dict]:
        with self._lock:
            state = self._load_state()
            hot = state.get("hot_concepts") if isinstance(state.get("hot_concepts"), list) else []
            by_id: dict[str, dict] = {}
            for item in [*self.seeds, *hot]:
                if isinstance(item, dict) and item.get("id") and item.get("name"):
                    by_id[str(item["id"])] = dict(item)
            return list(by_id.values())

    def choose_category(self, rarity: str, rng: random.Random) -> str:
        weights = CATEGORY_WEIGHTS.get(rarity, CATEGORY_WEIGHTS["rare"])
        categories = list(weights)
        return rng.choices(categories, weights=[weights[c] for c in categories], k=1)[0]

    def category_concept(self, category: str, recent_ids: set[str], rng: random.Random) -> dict | None:
        spec = CATEGORY_SPECS.get(category)
        if not spec:
            return None
        if category == "hot":
            state = self._load_state()
            hot = state.get("hot_concepts") if isinstance(state.get("hot_concepts"), list) else []
            available = [c for c in hot if str(c.get("id")) not in recent_ids]
            pool = available or hot
            return dict(rng.choice(pool)) if pool else None
        if spec.get("free"):
            return None
        samples = [s for s in spec.get("samples", []) if _safe_id(s) not in recent_ids]
        pool = samples or spec.get("samples", [])
        if not pool:
            return None
        name = rng.choice(pool)
        return {
            "id": category + "_" + _safe_id(name),
            "name": name,
            "category": category,
            "pattern": "complex",
            "visual_anchors": [],
            "source_name": spec.get("label", category),
        }

    def choose(self, rarity: str, recent_ids: set[str], rng: random.Random) -> dict | None:
        """两步抽取：先按大类权重抽分类，再在该大类内生成具体概念。

        ``free=True`` 的大类（食物/物品/职业/抽象）返回一个 AI 任务标记，
        由生成器现场创造一个概念，不依赖本地维护的具体概念列表。
        """
        category = self.choose_category(rarity, rng)
        concept = self.category_concept(category, recent_ids, rng)
        if concept is not None:
            return concept
        spec = CATEGORY_SPECS.get(category, {})
        return {
            "_ai_category": True,
            "category": category,
            "hint": spec.get("hint", ""),
            "source_name": spec.get("label", category),
        }

    def _get_client_token(self) -> str:
        direct = os.environ.get("DOUYIN_OPEN_ACCESS_TOKEN", "").strip()
        if direct:
            return direct
        key = os.environ.get("DOUYIN_OPEN_CLIENT_KEY", "").strip()
        secret = os.environ.get("DOUYIN_OPEN_CLIENT_SECRET", "").strip()
        if not key or not secret:
            return ""
        payload = json.dumps({"client_key": key, "client_secret": secret, "grant_type": "client_credential"}).encode("utf-8")
        req = urllib.request.Request(self.DOUYIN_TOKEN_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with self.opener(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str((data.get("data") or {}).get("access_token") or data.get("access_token") or "")

    @staticmethod
    def _extract_topic_rows(payload: object) -> list[dict]:
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            rows = data.get("list") or data.get("word_list") or data.get("topics") or data.get("data") or []
        else:
            rows = []
        result = []
        for index, row in enumerate(rows):
            if isinstance(row, str):
                title, raw = row, {}
            elif isinstance(row, dict):
                raw = row
                title = str(row.get("sentence") or row.get("word") or row.get("title") or row.get("name") or "")
            else:
                continue
            title = title.strip()
            if not title:
                continue
            result.append({"title": title, "rank": int(raw.get("rank") or index + 1), "raw": raw})
        return result

    def _fetch_topics(self) -> tuple[str, list[dict]]:
        custom_url = os.environ.get("CAT_TREND_FEED_URL", "").strip()
        if custom_url:
            with self.opener(custom_url, timeout=12) as response:
                return "custom_feed", self._extract_topic_rows(json.loads(response.read().decode("utf-8")))
        token = self._get_client_token()
        if not token:
            raise RuntimeError("未配置抖音开放平台凭证；需要 DOUYIN_OPEN_ACCESS_TOKEN 或 CLIENT_KEY/CLIENT_SECRET")
        url = self.DOUYIN_HOT_URL + "?" + urllib.parse.urlencode({"access-token": token})
        with self.opener(url, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return "douyin_open_api", self._extract_topic_rows(payload)

    @staticmethod
    def _topic_to_concept(topic: dict, collected_at: str) -> dict:
        title = str(topic["title"])
        raw = topic.get("raw") if isinstance(topic.get("raw"), dict) else {}
        topic_id = str(raw.get("sentence_id") or raw.get("id") or _safe_id(title))
        ai_name = _clean_cat_name(topic.get("cat_name"))
        return {
            "id": "hot_" + _safe_id(topic_id),
            "name": ai_name or _short_cat_name(title),
            "category": "hot",
            "source_name": "抖音热点",
            "source_title": title,
            "source_id": topic_id,
            "source_url": str(raw.get("share_url") or raw.get("url") or ""),
            "collected_at": collected_at,
            "rank": topic.get("rank", 0),
            "pattern": "complex",
            "visual_anchors": [f"把“{title}”压缩成一眼可辨的猫化意象", "保留热点最有辨识度的颜色、物件或表情", "不依赖文字也能表达主题"],
        }

    def refresh(self) -> dict:
        attempted = _now_iso()
        try:
            source, topics = self._fetch_topics()
            topics = self._filter_topics(topics)
            concepts = [self._topic_to_concept(topic, attempted) for topic in topics[:100]]
            if not concepts:
                raise RuntimeError("热点接口没有返回可用话题")
            with self._lock:
                state = self._load_state()
                old = state.get("hot_concepts") if isinstance(state.get("hot_concepts"), list) else []
                merged = {str(c.get("id")): c for c in old if isinstance(c, dict) and c.get("id")}
                for concept in concepts:
                    merged[concept["id"]] = concept
                # Keep a rolling archive so a trend remains usable after it
                # leaves today's list, while newest/rank-high concepts win.
                ordered = sorted(merged.values(), key=lambda c: (str(c.get("collected_at") or ""), -int(c.get("rank") or 999)), reverse=True)[:300]
                state.update({"hot_concepts": ordered, "last_attempt_at": attempted, "last_success_at": attempted, "last_error": "", "source": source})
                self._save_state(state)
            return {"ok": True, "source": source, "count": len(concepts)}
        except Exception as exc:
            with self._lock:
                state = self._load_state()
                state.update({"last_attempt_at": attempted, "last_error": str(exc)[:500]})
                self._save_state(state)
            return {"ok": False, "error": str(exc)}

    def scheduler_loop(self) -> None:
        # Refresh shortly after process start, then periodically forever.
        if self._stop.wait(float(os.environ.get("CAT_TREND_START_DELAY_SECONDS", "20"))):
            return
        while not self._stop.is_set():
            result = self.refresh()
            print(f"  [cat-trends] refresh: {result}", flush=True)
            hours = max(1, int(os.environ.get("CAT_TREND_REFRESH_HOURS", "6")))
            self._stop.wait(hours * 3600)

    def stop(self) -> None:
        self._stop.set()
