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


class CatConceptStore:
    """Mixes durable seeds with a periodically refreshed Douyin trend cache.

    No moderation queue is used. Source title, URL/id and collection time are
    retained so a generated concept remains traceable instead of being invented
    by the model without provenance.
    """

    DOUYIN_HOT_URL = "https://open.douyin.com/hotsearch/trending/sentences/"
    DOUYIN_TOKEN_URL = "https://open.douyin.com/oauth/client_token/"

    def __init__(self, seed_path: Path, state_path: Path, opener: Callable | None = None):
        self.seed_path = seed_path
        self.state_path = state_path
        self.opener = opener or urllib.request.urlopen
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.seeds = _read_json(seed_path, [])
        if not isinstance(self.seeds, list):
            self.seeds = []

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

    def choose(self, rarity: str, recent_ids: set[str], rng: random.Random) -> dict | None:
        concepts = self.all_concepts()
        available = [c for c in concepts if str(c.get("id")) not in recent_ids]
        if not available:
            available = concepts
        if not available:
            return None
        rarity_weight = {
            "common": {"breed": 7, "food": 3, "object": 2, "profession": 2, "abstract": 1, "hot": 1, "historical_breed": 1},
            "rare": {"breed": 4, "food": 2, "object": 3, "profession": 3, "abstract": 2, "hot": 2, "historical_breed": 3},
            "epic": {"breed": 1, "food": 1, "object": 2, "profession": 3, "abstract": 5, "hot": 6, "historical_breed": 3},
            "legendary": {"breed": 1, "food": 1, "object": 2, "profession": 2, "abstract": 7, "hot": 5, "historical_breed": 5},
        }[rarity]
        weights = [rarity_weight.get(str(c.get("category") or "abstract"), 1) for c in available]
        return dict(rng.choices(available, weights=weights, k=1)[0])

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
        return {
            "id": "hot_" + _safe_id(topic_id),
            "name": _short_cat_name(title),
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
