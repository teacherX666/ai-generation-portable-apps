#!/usr/bin/env python3
from __future__ import annotations

import collections
import csv
from datetime import datetime, timedelta
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import video_check
except Exception as _video_check_exc:  # cv2 等依赖缺失时不能让 Portal 起不来
    video_check = None
    print(f"  [video-check] 模块不可用: {_video_check_exc}", flush=True)

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared import local_gateway  # noqa: E402
from shared.net import UPSTREAM  # noqa: E402  直连 opener：出站别走系统代理（本机踩过 socks4 坑）

_DATA_BASE = Path(os.environ.get("DATA_DIR", str(ROOT)))
STATIC_DIR = ROOT / "static"

# Ensure `portal/` dir is on sys.path so `import daily_report` works whether
# the process was launched from repo root or from portal/.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import daily_report as _daily_report_module
import remote_sources
import xlsx_export
from cat_skins.experiment import CatExperimentService
from cat_skins.concepts import CatConceptStore
from cat_skins.service import (
    CatDailyLimitError,
    CatDailyTaskRequiredError,
    CatGenerationBusyError,
    CatGenerationError,
    CatSkinGenerator,
    CatSkinManager,
)

_POPEN_EXTRA: dict[str, Any] = {}
if hasattr(subprocess, "CREATE_NO_WINDOW"):
    _POPEN_EXTRA["creationflags"] = subprocess.CREATE_NO_WINDOW

STATE_DIR = _DATA_BASE / "state"
USAGE_PATH = STATE_DIR / "usage.json"
USAGE_JSONL_RETENTION_DAYS = 30
# 按次生成的用量/金额明细（像中转站的使用记录）。追加写 JSONL，不参与 usage.json
# 的热路径改写 —— usage.json 每次统计变动都要整份重写，明细进去会把它撑大。
GENERATIONS_PATH = STATE_DIR / "generations.jsonl"
GENERATIONS_RETENTION_DAYS = 180
# 单价表：按「模型 + 分辨率 + 有/无输入视频」查表算钱。可手改，改完立即生效。
PRICING_PATH = STATE_DIR / "pricing.json"
HISTORY_PATH = STATE_DIR / "history.json"
HISTORY_CAP = 10000
HISTORY_DAYS = 30
USERS_PATH = STATE_DIR / "users.json"
SESSIONS_PATH = STATE_DIR / "sessions.json"
USER_KEYS_PATH = STATE_DIR / "user_keys.json"
CAT_SKINS_DIR = ROOT / "cat_skins"
CAT_SKINS_PATH = STATE_DIR / "cat_skins.json"
CAT_ANATOMY_PATH = CAT_SKINS_DIR / "cat-anatomy-v1.json"
CAT_PROMPT_PATH = CAT_SKINS_DIR / "generation-prompt-v1.md"
CAT_CLASSIC_PATH = CAT_SKINS_DIR / "classic-black-v1.json"
CAT_CONCEPT_SEEDS_PATH = CAT_SKINS_DIR / "concept-seeds-v1.json"
CAT_TRENDS_PATH = STATE_DIR / "cat_trends.json"

SESSION_MAX_AGE = 86400 * 30  # 30 days

# Sub-app registry — read once from portal/apps.json. See portal/app_spec.py
# for the AppSpec schema. Adding a new sub-app is one JSON entry + one folder.
from app_spec import (
    AppSpec,
    classify_job_type,
    load_specs,
    resolve_extra_headers,
)

APPS_JSON = ROOT / "apps.json"
SPECS: list[AppSpec] = load_specs(APPS_JSON, ROOT.parent)
SPEC_BY_NAME: dict[str, AppSpec] = {s.name: s for s in SPECS}

# Legacy dict view — keeps existing `APPS[name]["dir"]` / `APPS[name]["port"]`
# / `APPS.items()` callers working without churn. Values are recomputed lazily
# via AppSpec.port so env overrides at runtime still take effect.
class _AppsView:
    def __init__(self, specs: list[AppSpec]):
        self._specs = specs
        self._by_name = {s.name: s for s in specs}
    def __getitem__(self, name: str) -> dict[str, Any]:
        s = self._by_name[name]
        return {"dir": s.dir_path, "port": s.port, "spec": s}
    def __contains__(self, name: str) -> bool:
        return name in self._by_name
    def __iter__(self):
        return iter(self._by_name)
    def keys(self):
        return self._by_name.keys()
    def items(self):
        for s in self._specs:
            yield s.name, {"dir": s.dir_path, "port": s.port, "spec": s}
    def get(self, name: str, default=None):
        return self[name] if name in self._by_name else default

APPS = _AppsView(SPECS)

PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "9090"))
REDIRECT_PORT = int(os.environ.get("REDIRECT_PORT", "9089"))

# 视频缩略图缓存目录与 ffmpeg 并发上限（历史记录卡片抽帧用）
THUMB_DIR = STATE_DIR / "thumbnails"
_THUMB_SEM = threading.Semaphore(2)

# 视频体检：单次最多取多少字节，超过直接拒，避免把内存和临时盘打满
MAX_VIDEO_CHECK_BYTES = 200 * 1024 * 1024


def _default_allowed_origins() -> frozenset[str]:
    """Origins allowed to make credentialed cross-origin requests.

    Defaults cover LAN IPs (auto-discovered) + loopback. Public deployments
    should set ALLOWED_ORIGINS explicitly (comma-separated), e.g.
    'https://apps.company.com,https://portal.company.com'."""
    override = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if override:
        return frozenset(o.strip() for o in override.split(",") if o.strip())
    # Auto-discover: LAN IP (may fail early during startup — get_lan_ip is safe)
    origins: set[str] = set()
    try:
        lan = get_lan_ip()
        if lan:
            origins.add(f"https://{lan}:{PORTAL_PORT}")
            origins.add(f"http://{lan}:{PORTAL_PORT}")
    except Exception:
        pass
    for host in ("127.0.0.1", "localhost"):
        origins.add(f"https://{host}:{PORTAL_PORT}")
        origins.add(f"http://{host}:{PORTAL_PORT}")
    return frozenset(origins)


# Lazy — get_lan_ip needs ifconfig/ipconfig which may take a beat.
# Refreshed with a TTL (see _cors_headers): the IP may change mid-run (DHCP
# lease), and a whitelist frozen at startup would reject CORS preflights from
# clients that re-discovered the new IP before the next portal restart.
_ALLOWED_ORIGINS: frozenset[str] | None = None
_ALLOWED_ORIGINS_TS: float = 0.0
_CORS_REFRESH_SECONDS = 60.0

# Shared secret between portal and sub-apps for the internal finalize callback.
# Generated fresh per portal launch; injected into each sub-app's env so a
# malicious local user cannot retroactively decrement stats without it.
INTERNAL_TOKEN = os.environ.get("PORTAL_INTERNAL_TOKEN") or secrets.token_hex(32)
os.environ["PORTAL_INTERNAL_TOKEN"] = INTERNAL_TOKEN


def _sign_admin_header(username: str, is_admin: bool, ts: int) -> str:
    """HMAC-SHA256(INTERNAL_TOKEN, "<ts>:<is_admin_flag>:<username>").

    Sub-apps verify this before trusting X-Is-Admin — without a valid sig,
    the X-Is-Admin header is ignored. Timestamp lets sub-apps reject replays
    older than PORTAL_SIG_WINDOW seconds (default 60)."""
    msg = f"{ts}:{'1' if is_admin else '0'}:{username}".encode("utf-8")
    return hmac.new(INTERNAL_TOKEN.encode("utf-8"), msg, hashlib.sha256).hexdigest()


_METADATA_WHITELIST = {
    "prompt", "text", "content", "aspect_ratio", "ratio", "duration",
    "resolution", "image_size", "count", "mode", "style",
    "negative_prompt", "seed", "generate_audio", "model",
    "custom_model", "model_name", "model_version",
    # 供应商：子应用的表单里本来就有（seedance/nano-banana 的 provider 切换），
    # 取到就能在「使用记录」里直接显示，不必靠模型名反推。
    "provider",
}
_MODEL_FIELDS = {"model", "custom_model", "model_name", "model_version"}
_PROVIDER_FIELDS = {"provider"}

# 供应商显示名（把通道名里的写法归一成「提供 key 的一方」）
_PROVIDER_ALIASES = {
    "volcengine": "火山方舟", "ark": "火山方舟", "volc": "火山方舟",
    "t8star": "T8Star", "chiyun": "Chiyun", "gemini": "Chiyun",
    "local": "本地（不消耗外部 key）", "aiport": "本地（不消耗外部 key）",
    "comfyui": "本地（不消耗外部 key）",
    "minimax": "本地（不消耗外部 key）", "feishu": "飞书 Agent（自带 key）",
}


def _read_json_key(rel_path: str, key_path: list[str] | None = None,
                   key_field: str = "") -> str:
    """从本机某份 JSON 里读一个密钥（只在本机、只读，取不到返回空串 → 功能自动关闭）。"""
    try:
        path = REPO_ROOT / rel_path
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    node: Any = data
    for step in key_path or []:
        if not isinstance(node, dict):
            return ""
        node = node.get(step)
    if key_field:
        node = node.get(key_field) if isinstance(node, dict) else None
    return str(node or "").strip()


_ARK_TASKS_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}
_ARK_TASKS_TTL = 600.0
_RELAY_CACHE: dict[str, Any] = {}


def ark_task_index() -> dict[str, dict]:
    """方舟**账号级**任务索引：{任务id: {tokens, model, resolution, has_input_video, duration}}。

    为什么要它：飞书任务的 token 消耗只记在方舟那边（飞书自己不上报 usage），而飞书 Agent 的
    `operations` 表里存着方舟任务号（实测 seedance2.5 560 条里 540 条有 `official_id`）。
    有了这张索引，飞书任务就能补上**真实** token 与金额，连「有/无输入视频」档位都准确
    （列表里成功任务带 `content`，看有没有 video_url 就知道）。

    失败/超时一律返回上次缓存或空表：这是**锦上添花**的数据，绝不能拖垮统计页。
    """
    now = time.time()
    if _ARK_TASKS_CACHE["data"] and now - float(_ARK_TASKS_CACHE["ts"]) < _ARK_TASKS_TTL:
        return _ARK_TASKS_CACHE["data"]
    table = pricing_table()
    src = table.get("ark_source") or {}
    key = _read_json_key(str(src.get("key_file") or ""), key_field=str(src.get("key_field") or ""))
    if not key:
        return _ARK_TASKS_CACHE["data"] or {}
    index: dict[str, dict] = {}
    opener = getattr(UPSTREAM, "open", urllib.request.urlopen)
    for page in range(1, 21):                      # 最多 20 页 × 100 = 2000 条，够覆盖保留期
        url = (f"https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
               f"?page_size=100&page_num={page}")
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with opener(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            break
        items = payload.get("items") or []
        if not items:
            break
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            parts = content.get("content") if isinstance(content.get("content"), list) else []
            index[str(item["id"])] = {
                "tokens": int(usage.get("total_tokens") or usage.get("completion_tokens") or 0),
                "model": str(item.get("model") or ""),
                "resolution": str(item.get("resolution") or ""),
                "duration": int(item.get("duration") or 0),
                "status": str(item.get("status") or ""),
                "has_input_video": any(
                    str(part.get("type") or "") == "video_url"
                    for part in parts if isinstance(part, dict)),
            }
    if index:
        _ARK_TASKS_CACHE["data"] = index
        _ARK_TASKS_CACHE["ts"] = now
    return _ARK_TASKS_CACHE["data"] or {}


def relay_usage_entries() -> dict[str, list[dict]]:
    """中转站（new-api）的**按请求**用量日志：{来源id: [{created_at, model, quota, tokens...}]}。

    chiyun.work / ai.t8star.org 都是 new-api，`GET /api/log/token?key=<令牌>` 返回每次调用的
    模型、额度（quota）、prompt/completion tokens 与时间 —— 这是云代理唯一能拿到用量的口子
    （它们的**生成响应体里没有 usage**，所以以前只能显示「—」）。
    """
    now = time.time()
    table = pricing_table()
    out: dict[str, list[dict]] = {}
    for src in (table.get("relay_sources") or []):
        if not isinstance(src, dict):
            continue
        sid = str(src.get("id") or "")
        if not sid:
            continue
        cache = _RELAY_CACHE.get(sid) or {}
        if cache.get("data") is not None and now - float(cache.get("ts") or 0) < 300.0:
            out[sid] = cache["data"]
            continue
        key = _read_json_key(str(src.get("key_file") or ""),
                             key_path=list(src.get("key_path") or []))
        entries: list[dict] = []
        if key:
            base = str(src.get("base_url") or "").rstrip("/")
            opener = getattr(UPSTREAM, "open", urllib.request.urlopen)
            try:
                url = f"{base}/api/log/token?key={urllib.parse.quote(key)}&p=0&page_size=200"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
                with opener(req, timeout=20) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                for item in (payload.get("data") or []):
                    if not isinstance(item, dict):
                        continue
                    entries.append({
                        "created_at": float(item.get("created_at") or 0),
                        "model": str(item.get("model_name") or ""),
                        "quota": float(item.get("quota") or 0),
                        "prompt_tokens": int(item.get("prompt_tokens") or 0),
                        "completion_tokens": int(item.get("completion_tokens") or 0),
                    })
            except Exception:  # noqa: BLE001
                entries = cache.get("data") or []
        _RELAY_CACHE[sid] = {"ts": now, "data": entries}
        out[sid] = entries
    return out


def relay_amount(quota: float, table: dict | None = None) -> float | None:
    """new-api 的 quota → 人民币。汇率口径写在 pricing.json 里（quota_per_usd / usd_cny）。"""
    table = table or pricing_table()
    per_usd = float(table.get("relay_quota_per_usd") or 0) or 500000.0
    usd_cny = float(table.get("usd_cny") or 0) or 7.2
    if quota <= 0:
        return None
    return round(quota / per_usd * usd_cny, 4)


def _apply_relay_usage(rows: list[dict], window: float = 300.0) -> int:
    """给「我方明明发了请求、但金额留空」的行补上中转站记录的真实花费。

    对齐口径：**模型名相同 + 时间窗内最近的、还没被用掉的**一条日志。中转站日志是按令牌的，
    同账号下的其它令牌也可能出现，所以只做「一对一」消费（用掉就不再匹配第二次），
    并在 note 里写明来源 —— 对不上的宁可留空。
    """
    entries = relay_usage_entries()
    pool: list[dict] = []
    for sid, items in entries.items():
        for item in items:
            pool.append({**item, "source": sid})
    if not pool:
        return 0
    table = pricing_table()
    filled = 0
    for row in rows:
        if isinstance(row.get("amount"), (int, float)):
            continue
        if str(row.get("price_source") or "") not in ("unknown", "untracked", ""):
            continue
        model = str(row.get("model") or "").strip()
        if not model:
            continue
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        best = None
        for item in pool:
            if item.get("used") or item["model"] != model:
                continue
            delta = abs(item["created_at"] - ts)
            if delta > window:
                continue
            if best is None or delta < best[0]:
                best = (delta, item)
        if best is None:
            continue
        item = best[1]
        amount = relay_amount(item["quota"], table)
        if amount is None:
            continue
        item["used"] = True
        row["amount"] = amount
        images = int(row.get("images") or 0)
        row["unit_price"] = (round(amount / images, 4) if images else None)
        row["unit"] = "CNY/张" if images else "CNY/次"
        row["price_source"] = "relay_log"
        note = str(row.get("note") or "")
        extra = (f"金额取自中转站用量日志（{item['source']}，quota={int(item['quota'])}）；"
                 f"该请求上游返回 {item['prompt_tokens']}+{item['completion_tokens']} tokens")
        row["note"] = (note + "；" + extra) if note else extra
        if item.get("completion_tokens"):
            row["tokens"] = int(item["completion_tokens"])
        filled += 1
    return filled


def _apply_ark_usage(rows: list[dict]) -> int:
    """给飞书行补方舟的真实 token 与金额（按 operations 里的方舟任务号精确对齐）。"""
    index = ark_task_index()
    if not index:
        return 0
    table = pricing_table()
    filled = 0
    for row in rows:
        ids = [str(t) for t in (row.get("provider_task_ids") or []) if str(t) in index]
        if not ids:
            continue
        tokens = 0
        model = ""
        resolution = ""
        has_input = False
        task_statuses: list[str] = []
        for task_id in ids:
            info = index[task_id]
            tokens += int(info.get("tokens") or 0)
            model = model or str(info.get("model") or "")
            resolution = resolution or str(info.get("resolution") or "")
            has_input = has_input or bool(info.get("has_input_video"))
            task_statuses.append(str(info.get("status") or "").lower())
        # 状态以**方舟任务**为准（生成结果的权威来源）：全成功=成功，全失败=失败，混合=部分成功。
        # 不能用飞书自己的工作流状态 —— 那会说成「待确认」（2026-10-13 用户反馈）。
        if task_statuses:
            ok = sum(1 for s in task_statuses if s == "succeeded")
            bad = sum(1 for s in task_statuses if s in ("failed", "canceled", "cancelled", "expired"))
            if ok and not bad:
                row["status"] = "succeeded"
            elif bad and not ok:
                row["status"] = "failed"
            elif ok and bad:
                row["status"] = "partial"
            # 其余（running/queued）保持上游状态不动
        row["tokens"] = tokens
        row["model"] = str(row.get("model") or "") or model
        # 补上模型后，供应商也该跟着变（飞书任务实际上就是走方舟 key 出的片）
        row["provider_label"] = provider_label("feishu-generation-agent", row["model"], "")
        row["provider_detail"] = provider_detail("feishu-generation-agent", row["model"], "")
        row["resolution"] = str(row.get("resolution") or "") or resolution
        row["has_input_video"] = has_input
        quote = quote_generation(model=model or str(row.get("model") or ""),
                                 resolution=resolution, has_input_video=has_input,
                                 tokens=tokens, app=str(row.get("app") or ""))
        if isinstance(quote.get("amount"), (int, float)):
            row["unit_price"] = quote.get("unit_price")
            row["unit"] = quote.get("unit")
            row["amount"] = quote.get("amount")
            row["price_source"] = "ark_usage"
            row["price_family"] = quote.get("label") or ""
        note = str(row.get("note") or "")
        extra = f"token 取自方舟任务记录（{len(ids)} 个任务，逐任务精确对齐）"
        row["note"] = (note + "；" + extra) if note else extra
        filled += 1
    return filled


_PROVIDERS_CACHE: dict[str, Any] = {}


def _app_providers(app: str) -> dict[str, dict]:
    """读子应用的 providers.json → {通道id: {host, models}}（按 mtime 缓存）。

    **通道 id ≠ 提供 key 的一方**：nano-banana 里通道叫 `gemini`，但它的 base_url 是
    `https://chiyun.work` —— key 是 Chiyun 提供的。用户要看的正是「谁给我 key」。
    """
    try:
        path = REPO_ROOT / str(app) / "providers.json"
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cache = _PROVIDERS_CACHE.get(app) or {}
    if cache.get("mtime") == mtime:
        return cache.get("data") or {}
    data: dict[str, dict] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        provs = raw.get("providers") or raw
        for name, cfg in (provs.items() if isinstance(provs, dict) else []):
            if not isinstance(cfg, dict):
                continue
            base = str(cfg.get("base_url") or "")
            host = urllib.parse.urlsplit(base).hostname or ""
            models = []
            for item in (cfg.get("models") or []):
                mid = item.get("id") if isinstance(item, dict) else item
                if mid:
                    models.append(str(mid))
            data[str(name)] = {"host": host, "models": models}
    except (OSError, json.JSONDecodeError, AttributeError):
        data = {}
    _PROVIDERS_CACHE[app] = {"mtime": mtime, "data": data}
    return data


def key_vendor_for_host(host: str) -> str:
    """base_url 的主机名 → **提供 key 的供应商**（认不出就原样返回主机名，不编品牌）。"""
    h = str(host or "").strip().lower()
    if not h:
        return ""
    if "volces.com" in h or h.endswith("volcengineapi.com"):
        return "火山方舟"
    if "chiyun.work" in h:
        return "Chiyun"
    if "t8star" in h:
        return "T8Star"
    if h in ("localhost", "127.0.0.1", "0.0.0.0") or h.endswith(".local") or h.startswith("192.168."):
        return "本地（不消耗外部 key）"
    if h.endswith(".cn") or h.endswith(".com") or h.endswith(".ai") or h.endswith(".org"):
        return h
    return h


def _model_vendor(model: str) -> str:
    """老记录没存通道 id 时，按**模型名**去各子应用的 providers.json 反查供应商。"""
    needle = _normalize_model_key(model)
    if not needle:
        return ""
    for app in ("nano-banana", "seedance"):
        for cfg in _app_providers(app).values():
            for mid in cfg.get("models") or []:
                key = _normalize_model_key(mid)
                if key and (key == needle or key in needle):
                    return key_vendor_for_host(cfg.get("host") or "")
    return ""


def provider_info(app: str = "", model: str = "", provider: str = "") -> tuple[str, str]:
    """(供应商, 依据说明)。供应商 = **提供 key 的那一方**（火山方舟 / Chiyun / T8Star / 本地 …）。"""
    raw = str(provider or "").strip()
    if raw:
        cfg = _app_providers(app).get(raw) or _app_providers(app).get(raw.lower()) or {}
        host = str(cfg.get("host") or "")
        vendor = key_vendor_for_host(host)
        if vendor:
            return vendor, f"按通道 {raw} 的 base_url（{host}）判定"
        vendor = _PROVIDER_ALIASES.get(raw.lower().replace("-", "").replace("_", ""), "")
        if vendor:
            return vendor, f"按通道名 {raw} 判定"
        # 通道名认不出：原样显示它（别丢信息），并说明这是未识别的通道名
        return raw[:24], f"未识别的通道名（{raw}），原样显示"
    vendor = _model_vendor(model)
    if vendor:
        return vendor, "按模型名反查各通道的 base_url 判定"
    text = f"{app} {model}".lower()
    if "feishu" in text:
        return "飞书 Agent（自带 key）", "飞书任务由飞书 Agent 自己的 key 发起"
    if any(t in text for t in ("flux2", "zimage", "klein", "krea", "anime2real", "qwen-image",
                               "minimax", "h3_", "comfyui")):
        return "本地（不消耗外部 key）", "本地模型，无外部 key"
    if text.strip().startswith("ep-") or any(t in text for t in ("doubao", "seedance", "seedream")):
        return "火山方舟", "模型名属于火山方舟"
    if "dreamina" in text or "jimeng" in text:
        return "即梦 Dreamina", "即梦官方账号"
    return "", ""


def provider_label(app: str = "", model: str = "", provider: str = "") -> str:
    """「供应商 / 通道」显示名（只取名字，说明见 provider_detail）。"""
    return provider_info(app, model, provider)[0]


def provider_detail(app: str = "", model: str = "", provider: str = "") -> str:
    return provider_info(app, model, provider)[1]
_METADATA_BLOCKLIST_SUBSTR = ("key", "token", "secret", "password")
_METADATA_MAX_BODY = 256 * 1024 * 1024
_METADATA_MAX_TEXT_VALUE = 128 * 1024


def _extract_multipart_text_fields(content_type: str, body: bytes,
                                   file_fields: list[str] | None = None) -> dict:
    """Extract text form fields without decoding large file payloads.

    Job creation often includes multi-megabyte reference videos. The previous
    parser skipped every multipart body above 5 MiB, which silently dropped the
    prompt/model from Portal history. This scanner keeps non-file fields and
    ignores file parts.

    ``file_fields``（可选）只收集文件字段的**名字**（不碰内容），用于判断这次生成
    有没有带参考视频/首尾帧 —— 方舟对「有/无输入视频」是两档单价，差 1.6 倍。
    """
    fields: dict[str, str] = {}
    try:
        match = re.search(r'(?:^|;)\s*boundary=(?:"([^"]+)"|([^;\s]+))', content_type or "")
        if not match:
            return fields
        boundary = (match.group(1) or match.group(2) or "").encode("utf-8", errors="replace")
        if not boundary:
            return fields
        marker = b"--" + boundary
        pos = body.find(marker)
        while pos >= 0:
            start = pos + len(marker)
            if body[start:start + 2] == b"--":
                break
            if body[start:start + 2] == b"\r\n":
                start += 2
            next_pos = body.find(marker, start)
            if next_pos < 0:
                break
            part = body[start:next_pos]
            if part.endswith(b"\r\n"):
                part = part[:-2]
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                pos = next_pos
                continue
            raw_headers = part[:header_end].decode("latin-1", errors="replace")
            disposition = ""
            for line in raw_headers.split("\r\n"):
                if line.lower().startswith("content-disposition:"):
                    disposition = line
                    break
            if "form-data" not in disposition.lower():
                pos = next_pos
                continue
            name_match = re.search(r'name="([^"]*)"', disposition, flags=re.IGNORECASE)
            if not name_match:
                pos = next_pos
                continue
            field_name = name_match.group(1)
            if "filename=" in disposition.lower():
                # 文件部分：只记名字，绝不读内容（body 里可能是几十 MB 的参考视频）
                if file_fields is not None and field_name:
                    file_fields.append(field_name)
                pos = next_pos
                continue
            value_bytes = part[header_end + 4:]
            if len(value_bytes) <= _METADATA_MAX_TEXT_VALUE:
                fields[field_name] = value_bytes.decode("utf-8", errors="replace")
            pos = next_pos
    except Exception:
        return fields
    return fields

def extract_job_metadata(content_type: str, body: bytes) -> dict:
    """从任务创建请求体提取提示词与白名单参数。永不采集密钥类字段。
    解析失败/超大 body 静默降级为空——采集绝不抛异常。"""
    result: dict = {"prompt": "", "model": "", "provider": "", "params": {}}
    try:
        if not body or len(body) > _METADATA_MAX_BODY:
            return result
        fields: dict = {}
        media_fields: list[str] = []
        ctype = (content_type or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            data = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                fields = data
        elif ctype == "multipart/form-data":
            fields = _extract_multipart_text_fields(content_type, body, media_fields)
        else:
            return result
        for key, value in fields.items():
            k = str(key)
            low = k.lower()
            if any(blocked in low for blocked in _METADATA_BLOCKLIST_SUBSTR):
                continue
            if low in _METADATA_WHITELIST:
                if isinstance(value, (str, int, float, bool)):
                    if low == "prompt":
                        result["prompt"] = str(value).strip()
                    elif low in _MODEL_FIELDS:
                        candidate = str(value).strip()
                        if not candidate:
                            continue
                        if low == "custom_model" or not result["model"]:
                            result["model"] = candidate
                    elif low in _PROVIDER_FIELDS:
                        result["provider"] = str(value).strip()[:60]
                    else:
                        result["params"][low] = value
        # 「有没有输入视频」决定查哪一档单价（方舟两档差 1.6 倍）。判据只取**视频类**
        # 文件字段：首帧/尾帧是图，不算输入视频。判错的代价是价格偏高或偏低，所以
        # 原始字段名一并存进明细，方便事后核对。
        result["media_fields"] = media_fields[:20]
        result["has_input_video"] = any(
            ("video" in name.lower()) for name in media_fields
        )
        return result
    except Exception:
        return result


_STATUS_DONE = {"succeeded", "done", "completed", "success"}
_STATUS_FAILED = {"failed", "cancelled", "canceled", "error"}
_STATUS_RUNNING = {"running", "processing", "generating", "uploading"}


def normalize_history_status(status: str) -> str:
    s = (status or "").lower()
    if s in _STATUS_DONE:
        return "done"
    if s in _STATUS_FAILED:
        return "failed"
    if s in _STATUS_RUNNING:
        return "running"
    return "queued"


def history_result_items(data: dict, kind: str) -> list[dict]:
    """从子应用 /api/jobs/{id} 响应提取结果清单（最多 4 条，供弹窗下载）。

    各子应用 results 结构不同：seedance/portrait 顶层有 download_url；
    nano-banana 是 run 级结果，URL 嵌套在 result["images"][i]["download_url"]。
    """
    nested = data.get("job") if isinstance(data.get("job"), dict) else {}
    raw = data.get("results") or nested.get("results") or []
    items = []
    for r in raw[:4]:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or r.get("download_url") or r.get("path")
               or r.get("image_url") or "")
        if not url and isinstance(r.get("images"), list) and r["images"]:
            first = r["images"][0]
            if isinstance(first, dict):
                url = (first.get("download_url") or first.get("image_url")
                       or first.get("url") or "")
        if not url:
            continue
        item_kind = "video" if kind == "video" else "image"
        items.append({"url": str(url), "kind": item_kind})
    return items

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"use_apps", "view_stats_all", "manage_users", "manage_dreamina_accounts"},
    "user":  {"use_apps", "view_stats_own"},
    "viewer": {"view_stats_own"},
}

# Providers recognized by the key vault (maps to sub-app X-Api-Key / X-Access-Key header)
KEY_PROVIDERS = ["t8star", "volcengine", "volcengine_ak", "gemini", "openai", "other"]


# RFC1918 private + RFC6598 shared (100.64-127) blacklist + IPv4 link-local + 240/4 reserved
# 私有段：192.168/16, 10/8, 172.16-31
# 黑名单：240/4 (GoGoJump 等), 100.64/10 (Tailscale CGNAT), 169.254/16 (link-local), 198.18-19 (benchmarking)
_VPN_BLACKLIST_PREFIXES = ("240.", "169.254.", "198.18.", "198.19.")


def _is_private_ipv4(ip: str) -> bool:
    """Return True if `ip` is in RFC1918 private space (excluding the 100.64/10 CGNAT and VPN-tunnel ranges)."""
    if ip.startswith("192.168.") or ip.startswith("10."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".", 2)[1])
        except (ValueError, IndexError):
            return False
        return 16 <= second <= 31
    return False


def _is_vpn_or_reserved(ip: str) -> bool:
    if ip.startswith(_VPN_BLACKLIST_PREFIXES):
        return True
    # 100.64.0.0/10 — CGNAT, used by Tailscale and many VPN providers
    if ip.startswith("100."):
        try:
            second = int(ip.split(".", 2)[1])
        except (ValueError, IndexError):
            return False
        return 64 <= second <= 127
    return False


def get_lan_ip() -> str:
    candidates: list[str] = []
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(["ipconfig"], text=True, stderr=subprocess.DEVNULL, encoding="gbk", errors="replace")
            for line in out.splitlines():
                line = line.strip()
                if "IPv4" in line and "127.0.0.1" not in line:
                    ip = line.rsplit(":", 1)[-1].strip()
                    candidates.append(ip)
        else:
            out = subprocess.check_output(["ifconfig"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "inet " in line and "127.0.0.1" not in line:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        candidates.append(parts[1])
    except Exception:
        pass
    # Prefer private addresses; skip VPN/reserved blacklist.
    for ip in candidates:
        if _is_vpn_or_reserved(ip):
            continue
        if _is_private_ipv4(ip):
            return ip
    # Fallback: outbound socket trick. Apply same filters; if blacklisted, fall through.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if _is_private_ipv4(ip) and not _is_vpn_or_reserved(ip):
            return ip
        print(f"  [WARN] outbound IP {ip} blacklisted (VPN/reserved), falling back to 127.0.0.1", flush=True)
    except Exception:
        pass
    return "127.0.0.1"


def _find_openssl() -> str | None:
    which = shutil.which("openssl")
    if which:
        return which
    if sys.platform == "win32":
        for p in [r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\OpenSSL\bin\openssl.exe", r"C:\Users\123\.workbuddy\binaries\PortableGit\versions\1.2.0\usr\bin\openssl.exe"]:
                return p
    return None


def ensure_certs(cert_dir: Path) -> tuple[Path, Path] | None:
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"
    ip_file = cert_dir / "lan_ip.txt"
    current_ip = get_lan_ip()
    if cert_file.exists() and key_file.exists():
        if ip_file.exists():
            if ip_file.read_text().strip() != current_ip:
                print(f"  LAN IP changed, regenerating cert...")
                cert_file.unlink(missing_ok=True)
                key_file.unlink(missing_ok=True)
            else:
                return cert_file, key_file
        else:
            ip_file.write_text(current_ip)
            return cert_file, key_file
    openssl = _find_openssl()
    if not openssl:
        print("  [WARN] openssl not found — running in HTTP-only mode")
        return None
    cert_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key_file), "-out", str(cert_file),
        "-days", "365", "-nodes",
        "-subj", "/CN=RedCraft",
        "-addext", f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{current_ip}"
    ], check=True, capture_output=True)
    ip_file.write_text(current_ip)
    print(f"  Generated self-signed certificate (LAN IP: {current_ip})")
    return cert_file, key_file


def start_cert_ip_watcher(cert_dir: Path) -> None:
    """Watch for LAN IP changes at runtime and regenerate the TLS cert, then
    exit so launchd (KeepAlive) restarts the portal with the fresh cert.

    ensure_certs() only runs at startup — a mid-run DHCP IP change leaves the
    running server answering on the old IP with a cert whose SAN no longer
    matches, which browsers refuse to bypass ("继续访问" only rescues an
    untrusted issuer, not a SAN mismatch). Regen + clean exit is the recovery:
    launchd brings the process back and the startup path serves the new cert.

    Guards: the change must be confirmed twice (5 min apart) so transient
    flapping (VPN up/down) doesn't trigger a restart loop, and the new IP must
    be a private LAN address (get_lan_ip already filters reserved ranges, this
    double-checks with ipaddress).
    """
    ip_file = cert_dir / "lan_ip.txt"

    def _known_ip() -> str:
        try:
            return ip_file.read_text().strip() if ip_file.exists() else ""
        except Exception:
            return ""

    pending_new_ip = ""
    while True:
        time.sleep(300)
        try:
            current_ip = get_lan_ip()
            ipaddress.ip_address(current_ip)  # raises on junk
        except Exception:
            continue
        if not current_ip or not ipaddress.ip_address(current_ip).is_private:
            pending_new_ip = ""
            continue
        known = _known_ip()
        if not known or current_ip == known:
            pending_new_ip = ""
            continue
        if pending_new_ip == current_ip:
            print(f"  [cert-watch] LAN IP changed {known} -> {current_ip} (confirmed twice), "
                  f"regenerating cert and exiting for restart", flush=True)
            try:
                ensure_certs(cert_dir)
            except Exception as exc:
                print(f"  [cert-watch] cert regeneration failed: {exc}", flush=True)
            os._exit(0)
        else:
            pending_new_ip = current_ip
            print(f"  [cert-watch] LAN IP change detected {known} -> {current_ip}; "
                  f"will act if confirmed on next cycle", flush=True)


# ─── Auth ──────────────────────────────────────────────────────────────────────

class AuthManager:
    def __init__(self):
        self._lock = threading.Lock()

    def _load_users(self) -> dict:
        if USERS_PATH.exists():
            try:
                return json.loads(USERS_PATH.read_text("utf-8"))
            except Exception:
                pass
        return {"users": []}

    def _save_users(self, data: dict):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def _load_sessions(self) -> dict:
        if SESSIONS_PATH.exists():
            try:
                return json.loads(SESSIONS_PATH.read_text("utf-8"))
            except Exception:
                pass
        return {"sessions": {}}

    def _save_sessions(self, data: dict):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def _hash_pw(self, pw: str) -> str:
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 260000)
        return f"pbkdf2:sha256:260000:{salt.hex()}:{dk.hex()}"

    def _verify_pw(self, pw: str, stored: str) -> bool:
        try:
            _, _, iters_str, salt_hex, dk_hex = stored.split(":")
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_str))
            return secrets.compare_digest(dk.hex(), dk_hex)
        except Exception:
            return False

    def first_run(self) -> bool:
        with self._lock:
            return len(self._load_users()["users"]) == 0

    def create_user(self, username: str, pw: str, role: str = "user") -> dict | None:
        with self._lock:
            data = self._load_users()
            if any(u["username"] == username for u in data["users"]):
                return None
            user = {
                "id": str(uuid.uuid4()), "username": username,
                "pw_hash": self._hash_pw(pw), "role": role,
                "enabled": True, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            data["users"].append(user)
            self._save_users(data)
            return user

    def login(self, username: str, pw: str) -> str | None:
        with self._lock:
            data = self._load_users()
            user = next((u for u in data["users"] if u["username"] == username and u.get("enabled", True)), None)
            if not user or not self._verify_pw(pw, user["pw_hash"]):
                return None
            token = secrets.token_hex(32)
            sess = self._load_sessions()
            now = time.time()
            sess["sessions"] = {k: v for k, v in sess["sessions"].items() if v.get("expires", 0) > now}
            sess["sessions"][token] = {
                "user_id": user["id"], "username": user["username"],
                "role": user["role"], "expires": now + SESSION_MAX_AGE,
            }
            self._save_sessions(sess)
            return token

    def logout(self, token: str):
        with self._lock:
            sess = self._load_sessions()
            sess["sessions"].pop(token, None)
            self._save_sessions(sess)

    def get_user(self, token: str) -> dict | None:
        if not token:
            return None
        with self._lock:
            sess = self._load_sessions()
            s = sess["sessions"].get(token)
            if not s or s.get("expires", 0) < time.time():
                return None
            return {"user_id": s["user_id"], "username": s["username"], "role": s["role"]}

    def list_users(self) -> list:
        with self._lock:
            data = self._load_users()
            return [{"id": u["id"], "username": u["username"], "role": u["role"], "enabled": u.get("enabled", True)}
                    for u in data["users"]]

    def update_user(self, user_id: str, **kwargs) -> bool:
        allowed = {"role", "enabled"}
        with self._lock:
            data = self._load_users()
            for u in data["users"]:
                if u["id"] == user_id:
                    for k, v in kwargs.items():
                        if k in allowed:
                            u[k] = v
                    self._save_users(data)
                    return True
            return False

    def reset_password(self, user_id: str, new_pw: str) -> bool:
        with self._lock:
            data = self._load_users()
            for u in data["users"]:
                if u["id"] == user_id:
                    u["pw_hash"] = self._hash_pw(new_pw)
                    self._save_users(data)
                    return True
            return False

    def has_permission(self, user: dict, perm: str) -> bool:
        return perm in ROLE_PERMISSIONS.get(user.get("role", ""), set())

    def signup_enabled(self) -> bool:
        with self._lock:
            return bool(self._load_users().get("signup_enabled", True))

    def set_signup_enabled(self, enabled: bool):
        with self._lock:
            data = self._load_users()
            data["signup_enabled"] = bool(enabled)
            self._save_users(data)


# ─── Key Vault ─────────────────────────────────────────────────────────────────

class KeyManager:
    def __init__(self):
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if USER_KEYS_PATH.exists():
            try:
                return json.loads(USER_KEYS_PATH.read_text("utf-8"))
            except Exception:
                pass
        return {"keys": []}

    def _save(self, data: dict):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        USER_KEYS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    @staticmethod
    def _mask(k: str) -> str:
        if len(k) > 10:
            return k[:6] + "..." + k[-4:]
        return "***"

    def list_keys(self, user_id: str, provider: str | None = None) -> list[dict]:
        with self._lock:
            data = self._load()
            keys = [k for k in data["keys"] if k["user_id"] == user_id]
            if provider:
                keys = [k for k in keys if k["provider"] == provider]
            return [{"id": k["id"], "name": k["name"], "provider": k["provider"],
                     "note": k.get("note", ""), "key_hint": self._mask(k["key"])} for k in keys]

    def add_key(self, user_id: str, name: str, provider: str, key: str, note: str = "") -> dict:
        # Sub-apps that declare `personal_key_disabled` use a company-wide key
        # managed by admin (POST /api/platform/<endpoint>) — reject personal
        # keys to avoid drift between accounts.
        spec = SPEC_BY_NAME.get(provider)
        if spec is not None and spec.personal_key_disabled:
            raise ValueError(f"{spec.display_name}由 admin 统一配置,不支持个人密钥")
        with self._lock:
            data = self._load()
            entry = {
                "id": str(uuid.uuid4()), "user_id": user_id, "name": name,
                "provider": provider, "key": key, "note": note,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            data["keys"].append(entry)
            self._save(data)
            return {"id": entry["id"], "name": entry["name"], "provider": entry["provider"],
                    "note": entry["note"], "key_hint": self._mask(key)}

    def delete_key(self, user_id: str, key_id: str) -> bool:
        with self._lock:
            data = self._load()
            before = len(data["keys"])
            data["keys"] = [k for k in data["keys"] if not (k["id"] == key_id and k["user_id"] == user_id)]
            if len(data["keys"]) < before:
                self._save(data)
                return True
            return False

    def resolve(self, user_id: str, key_id: str) -> str | None:
        """Return plaintext key value. Used only server-side."""
        with self._lock:
            data = self._load()
            k = next((k for k in data["keys"] if k["id"] == key_id and k["user_id"] == user_id), None)
            return k["key"] if k else None


# ─── App Manager ───────────────────────────────────────────────────────────────

class AppManager:
    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}
        self.log_handles: dict[str, Any] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self._unhealthy_strikes: dict[str, int] = {}
        self._stop_event = threading.Event()

    def _kill_port_squatter(self, port: int):
        """Kill any process holding `port` that we didn't start ourselves.

        macOS/Linux use lsof. Windows falls back to netstat + taskkill because
        lsof is usually absent there and orphaned child processes must not wedge
        a port after Portal or an app restarts.
        """
        def _windows_listeners() -> list[int] | None:
            try:
                raw = subprocess.check_output(
                    ["netstat", "-ano", "-p", "TCP"],
                    stderr=subprocess.DEVNULL, text=True, timeout=5,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                return None
            pids: list[int] = []
            suffix = f":{port}"
            for line in raw.splitlines():
                columns = line.split()
                if len(columns) < 5 or columns[0].upper() != "TCP":
                    continue
                if columns[3].upper() not in ("LISTENING", "LISTEN"):
                    continue
                if not columns[1].endswith(suffix):
                    continue
                try:
                    pids.append(int(columns[4]))
                except ValueError:
                    continue
            return pids

        def _query_listeners() -> list[int] | None:
            """Return listener PIDs, or None if no supported probe is available."""
            if os.name == "nt":
                return _windows_listeners()
            lsof_candidates = ["/usr/sbin/lsof", "/usr/bin/lsof", "/opt/homebrew/bin/lsof", "lsof"]
            for lsof_bin in lsof_candidates:
                try:
                    out = subprocess.check_output(
                        [lsof_bin, "-nP", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
                        stderr=subprocess.DEVNULL, text=True, timeout=5,
                    )
                    pids: list[int] = []
                    for line in out.strip().splitlines():
                        try:
                            pids.append(int(line.strip()))
                        except ValueError:
                            continue
                    return pids
                except subprocess.CalledProcessError:
                    return []
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    continue
            return None

        for attempt in range(2):
            pids = _query_listeners()
            if pids is None:
                print(f"  [port-cleanup] WARNING: no supported listener probe for port {port}", flush=True)
                return
            own_pids = {proc.pid for proc in self.processes.values() if proc.poll() is None}
            killed_any = False
            for pid in pids:
                if pid in own_pids or pid == os.getpid():
                    continue
                if os.name == "nt":
                    try:
                        result = subprocess.run(
                            ["taskkill", "/PID", str(pid), "/F", "/T"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, timeout=10,
                        )
                        if result.returncode == 0:
                            killed_any = True
                            print(f"  [port-cleanup] killed orphan PID {pid} on port {port}"
                                  + (f" (retry)" if attempt > 0 else ""), flush=True)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed_any = True
                        print(f"  [port-cleanup] killed orphan PID {pid} on port {port}"
                              + (f" (retry)" if attempt > 0 else ""), flush=True)
                    except (ProcessLookupError, PermissionError):
                        pass
            if not killed_any:
                return
            time.sleep(0.5 if attempt == 0 else 1.0)
    def start_all(self):
        for name, config in APPS.items():
            if config["spec"].managed:
                self.start_app(name, config)
            else:
                alive = self._tcp_probe(config["port"])
                self.status[name] = {
                    "status": "running" if alive else "unavailable",
                    "port": config["port"],
                }
        threading.Thread(target=self._health_loop, daemon=True).start()
        threading.Thread(target=self._log_rotation_loop, daemon=True).start()

    def _log_rotation_loop(self):
        log_dir = STATE_DIR / "logs"
        max_age = 7 * 86400
        while not self._stop_event.is_set():
            try:
                if log_dir.exists():
                    now = time.time()
                    for log_path in log_dir.glob("*.log"):
                        try:
                            if log_path.stat().st_size == 0:
                                continue
                            if now - log_path.stat().st_mtime < max_age:
                                continue
                            size_mb = log_path.stat().st_size / 1024 / 1024
                            with log_path.open("wb"):
                                pass
                            print(f"  [log-rotate] truncated {log_path.name} ({size_mb:.1f} MB, >7d old)", flush=True)
                        except OSError as exc:
                            print(f"  [log-rotate] {log_path.name} skipped: {exc}", flush=True)
            except Exception as exc:
                print(f"  [log-rotate] loop error: {exc}", flush=True)
            self._stop_event.wait(3600)

    def _read_tos_source_keys(self) -> tuple[str, str]:
        """Read TOS AK/SK from whichever sub-app is declared `is_tos_source`
        in apps.json (currently volcengine-portrait). Sub-apps with
        `needs_tos_creds` get these injected via env at start_app time."""
        source_spec = next((s for s in SPECS if s.is_tos_source), None)
        if source_spec is None:
            return "", ""
        try:
            cfg_path = source_spec.dir_path / "config.json"
            if not cfg_path.exists():
                return "", ""
            data = json.loads(cfg_path.read_text("utf-8"))
            return (data.get("access_key") or "").strip(), (data.get("secret_key") or "").strip()
        except Exception:
            return "", ""

    def _read_ark_key(self) -> str:
        """Read Seedance's server-managed Ark Bearer key for child apps.

        Only the child-process environment receives the plaintext value. API
        config responses expose availability as a boolean, never the key.
        """
        seedance_spec = SPEC_BY_NAME.get("seedance")
        if seedance_spec is None:
            return ""
        try:
            secrets_path = seedance_spec.dir_path / "state" / "secrets.json"
            if not secrets_path.exists():
                return ""
            data = json.loads(secrets_path.read_text("utf-8"))
            return str(data.get("volcengine_api_key") or "").strip()
        except Exception:
            return ""

    def start_app(self, name: str, config: dict):
        spec: AppSpec | None = config.get("spec")
        if spec is not None and not spec.managed:
            alive = self._tcp_probe(config["port"])
            self.status[name] = {
                "status": "running" if alive else "unavailable",
                "port": config["port"],
            }
            return
        app_dir = config["dir"]
        if not (app_dir / "app.py").exists():
            self.status[name] = {"status": "missing", "error": "app.py not found"}
            return
        self._kill_port_squatter(config["port"])
        env = os.environ.copy()
        env["PORT"] = str(config["port"])
        env["HOST"] = "127.0.0.1"
        env["CORS"] = "1"
        if "DATA_DIR" in os.environ:
            env["DATA_DIR"] = str(app_dir / "test-data")
        if spec is not None and spec.needs_tos_creds:
            ak, sk = self._read_tos_source_keys()
            if ak and sk:
                env["TOS_ACCESS_KEY"] = ak
                env["TOS_SECRET_KEY"] = sk
        if spec is not None and spec.needs_ark_key:
            ark_key = self._read_ark_key()
            if ark_key:
                env["VOLCENGINE_ARK_API_KEY"] = ark_key
        old_log = self.log_handles.pop(name, None)
        if old_log:
            try: old_log.close()
            except Exception: pass
        log_dir = STATE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / f"{name}.log").open("ab", buffering=0)
        popen_kwargs = dict(_POPEN_EXTRA)
        if hasattr(os, "setsid"):
            popen_kwargs["preexec_fn"] = os.setsid
        # Per-app engine switch: FastAPI variants live in app_fastapi.py and
        # are launched via uvicorn from the shared .venv. Falls back to the
        # stdlib app.py when app_fastapi.py is missing or the env opts out.
        engine_env = f"{name.upper().replace('-', '_')}_ENGINE"
        engine = env.get(engine_env, os.environ.get(engine_env, "stdlib")).lower()
        fastapi_path = app_dir / "app_fastapi.py"
        venv_uvicorn = ROOT.parent / ".venv" / "bin" / "uvicorn"
        expat_lib = "/opt/homebrew/opt/expat/lib"
        if engine == "fastapi" and fastapi_path.exists():
            if sys.platform == "win32":
                uvicorn_cmd = [sys.executable, "-m", "uvicorn"]
            elif venv_uvicorn.exists():
                # DYLD lets Homebrew Python 3.12 load Homebrew expat rather than
                # the (broken) system libexpat. See requirements.txt.
                env["DYLD_LIBRARY_PATH"] = expat_lib + (":" + env["DYLD_LIBRARY_PATH"] if env.get("DYLD_LIBRARY_PATH") else "")
                uvicorn_cmd = [str(venv_uvicorn)]
            else:
                uvicorn_cmd = None
            if uvicorn_cmd is None:
                cmd = [sys.executable, "app.py"]
            else:
                cmd = uvicorn_cmd + [
                    "app_fastapi:app",
                    "--host", "127.0.0.1",
                    "--port", str(config["port"]),
                    "--log-level", "warning",
                ]
        else:
            cmd = [sys.executable, "app.py"]
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(app_dir), env=env,
                stdout=log_file, stderr=subprocess.STDOUT, **popen_kwargs,
            )
        except Exception as exc:
            log_file.close()
            self.status[name] = {"status": "crashed", "error": str(exc), "port": config["port"]}
            return
        self.log_handles[name] = log_file
        self.processes[name] = proc
        self.status[name] = {"status": "starting", "port": config["port"], "pid": proc.pid}
        self._unhealthy_strikes[name] = 0

    def _tcp_probe(self, port: int, timeout: float = 3.0) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (OSError, socket.timeout):
            return False
        finally:
            try: s.close()
            except Exception: pass

    def _http_probe(self, port: int, path: str = "/health", timeout: float = 3.0) -> bool:
        """HTTP 级健康探针：拿到任何 HTTP 响应（含 404）即算存活。

        纯 TCP 探针抓不到「TCP 能连、HTTP 永不响应」的应用层楔死
        （事件循环阻塞/死锁，2026-09-02 人像事故：楔子存活 10+ 分钟
        直到 backlog 打满 TCP 才失败）。路径 404 说明服务器在正常处理
        请求，同样算 healthy。"""
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            try:
                conn.request("GET", path)
                resp = conn.getresponse()
                try:
                    resp.read(2048)
                except Exception:
                    pass
                return True
            finally:
                try: conn.close()
                except Exception: pass
        except Exception:
            return False

    def _health_loop(self):
        while not self._stop_event.is_set():
            time.sleep(15)
            for name, config in APPS.items():
                if not config["spec"].managed:
                    alive = self._tcp_probe(config["port"])
                    self.status[name] = {
                        "status": "running" if alive else "unavailable",
                        "port": config["port"],
                    }
                    continue
                proc = self.processes.get(name)
                if proc and proc.poll() is not None:
                    self.status[name] = {"status": "crashed", "exit_code": proc.returncode, "port": config["port"]}
                    print(f"  [watchdog] {name} exited (code {proc.returncode}), restarting")
                    self.start_app(name, config)
                    continue
                alive = self._http_probe(config["port"])
                if alive:
                    self.status[name] = {"status": "ready", "port": config["port"], "pid": proc.pid if proc else None}
                    self._unhealthy_strikes[name] = 0
                else:
                    if self._tcp_probe(config["port"]):
                        print(f"  [watchdog] {name} HTTP 探针无响应但端口在听——疑似应用层楔死", flush=True)
                    self._unhealthy_strikes[name] = self._unhealthy_strikes.get(name, 0) + 1
                    self.status[name] = {"status": "unhealthy", "port": config["port"],
                                         "strikes": self._unhealthy_strikes[name]}
                    if self._unhealthy_strikes[name] >= 3:
                        print(f"  [watchdog] {name} unhealthy for 3 cycles, restarting")
                        if proc and proc.poll() is None:
                            try: proc.kill()
                            except Exception: pass
                        self.start_app(name, config)

    def shutdown(self):
        self._stop_event.set()
        for _, proc in self.processes.items():
            if proc.poll() is None:
                if hasattr(os, "killpg"):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        continue
                    except (ProcessLookupError, PermissionError):
                        pass
                proc.terminate()
        for _, proc in self.processes.items():
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if hasattr(os, "killpg"):
                    try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception: pass
                proc.kill()
        for handle in self.log_handles.values():
            try: handle.close()
            except Exception: pass
        self.log_handles.clear()


# ─── Usage Tracker ─────────────────────────────────────────────────────────────

def _cache_path_of(target_path: str) -> str:
    """静态资源缓存判定用纯路径：?v= 之类的版本参数拼在 target_path 里
    会让 endswith('.js') 失配（2026-09-10 实锤：app.js?v=20260910-refresh-fix3
    因带 query 不加 no-cache，用户浏览器启发式缓存旧 JS，页面按钮全死）。"""
    return target_path.split("?", 1)[0]


def _append_usage_jsonl(entry: dict, today: str):
    """Append a single usage entry to state/logs/usage-YYYY-MM-DD.jsonl.
    Failures are logged but do NOT propagate — primary usage.json save must not be blocked."""
    try:
        logs_dir = STATE_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"usage-{today}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"  [usage] jsonl append failed for {today}: {exc}", flush=True)


def _prune_old_usage_jsonl(today: str):
    """Delete usage-*.jsonl older than USAGE_JSONL_RETENTION_DAYS. Best-effort."""
    try:
        logs_dir = STATE_DIR / "logs"
        if not logs_dir.exists():
            return
        cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=USAGE_JSONL_RETENTION_DAYS)
        for p in logs_dir.glob("usage-*.jsonl"):
            try:
                d = datetime.strptime(p.stem[len("usage-"):], "%Y-%m-%d")
                if d < cutoff:
                    p.unlink()
            except Exception:
                continue
    except Exception:
        pass


def _append_analytics_jsonl(entry: dict):
    """Append one analytics event to state/logs/analytics-events.jsonl. Best-effort."""
    try:
        logs_dir = STATE_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / "analytics-events.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"  [analytics] jsonl append failed: {exc}", flush=True)

_HISTORY_EVENT_COND = threading.Condition()
_HISTORY_EVENT_SEQ = 0
_HISTORY_EVENTS: list[tuple[int, dict]] = []
_HISTORY_EVENT_LIMIT = 200


def _publish_history_event(record: dict) -> None:
    """Queue a terminal history update for connected portal pages."""
    global _HISTORY_EVENT_SEQ
    try:
        status = normalize_history_status(str(record.get("status") or ""))
        if status not in {"done", "failed"}:
            return
        payload = {
            "app": str(record.get("app") or ""),
            "job_id": str(record.get("job_id") or ""),
            "username": str(record.get("username") or ""),
            "kind": str(record.get("kind") or ""),
            "status": status,
        }
        with _HISTORY_EVENT_COND:
            _HISTORY_EVENT_SEQ += 1
            _HISTORY_EVENTS.append((_HISTORY_EVENT_SEQ, payload))
            if len(_HISTORY_EVENTS) > _HISTORY_EVENT_LIMIT:
                del _HISTORY_EVENTS[:-_HISTORY_EVENT_LIMIT]
            _HISTORY_EVENT_COND.notify_all()
    except Exception:
        pass


# ─── 单价表 + 按次生成明细 ─────────────────────────────────────────────────────
#
# 「像中转站那样一条一条记录用量和价格」的两块地基：
#
# 1) 单价表（state/pricing.json，可手改、改完即时生效）。数值来自方舟控制台导出的
#    「消费明细」：单价 = 原价 ÷ 用量。该表区分**分辨率**与**有无输入视频**——
#    同模型这两档能差 1.6 倍（Seedance 2.0：无输入 44.88 / 有输入 28.00 元每百万
#    token），所以查表必须带这两个维度，不能只按模型给一个价。
#
# 2) 按次明细（state/generations.jsonl，追加写）。之所以不塞进 usage.json：
#    usage.json 每次统计变动都整份重写，明细进去会把它越撑越大（仓库里有过
#    因此把 Portal 拖垮的教训）。
#
# 查不到单价的模型：金额留空并标记 price_source="unknown"，**不猜**——宁可显示
# 「—」也不要给一个看起来合理的错数字。

_DEFAULT_PRICING: dict[str, Any] = {
    "schema_version": 1,
    "currency": "CNY",
    "note": ("单价由方舟控制台消费明细反推（原价÷用量）。families 有序，先匹配到的先用；"
             "endpoint_aliases 把推理接入点 id（ep-xxx）指到模型族。unit 为 video 时价格是"
             "元/百万 token，image 时是元/张。"),
    "endpoint_aliases": {
        "ep-20260912121738-vtd78": "seedance-2.0-mini",
    },
    "families": [
        {
            "id": "seedance-2.5", "label": "Seedance 2.5", "unit": "video",
            "match": ["doubao-seedance-2-5"],
            "resolution_buckets": {"480p": "480p/720p", "720p": "480p/720p"},
            "prices": {"480p/720p": {"no_input_video": 67.17, "input_video": 40.29}},
        },
        {
            "id": "seedance-2.0-fast", "label": "Seedance 2.0 fast", "unit": "video",
            "match": ["doubao-seedance-2-0-fast"],
            "resolution_buckets": {},
            "prices": {"default": {"no_input_video": 37.00, "input_video": 22.00}},
        },
        {
            "id": "seedance-2.0-mini", "label": "Seedance 2.0 mini", "unit": "video",
            "match": ["doubao-seedance-2-0-mini"],
            "resolution_buckets": {},
            "prices": {"default": {"no_input_video": 23.00, "input_video": 14.00}},
        },
        {
            "id": "seedance-2.0", "label": "Seedance 2.0", "unit": "video",
            "match": ["doubao-seedance-2-0", "doubao-seedance-1"],
            "resolution_buckets": {"480p": "default", "720p": "default", "1080p": "1080p", "4k": "4k"},
            "prices": {
                "default": {"no_input_video": 44.88, "input_video": 28.00},
                "1080p": {"no_input_video": 50.50, "input_video": 31.00},
                "4k": {"no_input_video": 26.00, "input_video": 16.00},
            },
        },
        {
            "id": "seedream-5.0-pro", "label": "Seedream 5.0 Pro", "unit": "image",
            "match": ["doubao-seedream-5-0-pro", "seedream-5-0-pro"],
            "image_size_buckets": {"1k": "small", "1.5k": "small", "2k": "medium"},
            "prices": {"small": 0.27, "medium": 0.42},
        },
        # 本地通道的 API 花费确实是 0（不是「不知道」）→ 单价写 0，页面显示 ¥0 而不是「—」。
        # 必须排在最后，免得把云模型的 id 抢走匹配。
        {
            "id": "local-image", "label": "本地图像 · 不计费", "unit": "image",
            "match": ["flux2", "zimage", "klein", "krea", "anime2real", "qwen-image", "comfyui"],
            "image_size_buckets": {},
            "prices": {"default": 0},
        },
        {
            "id": "local-video", "label": "本地视频 · 不计费", "unit": "video",
            "match": ["minimax", "h3_"],
            "prices": {"default": {"no_input_video": 0, "input_video": 0}},
        },
    ],
}

_PRICING_LOCK = threading.Lock()
_PRICING_CACHE: dict[str, Any] = {"mtime": -1.0, "data": _DEFAULT_PRICING}
_GENERATIONS_LOCK = threading.Lock()
_GENERATIONS_LAST_PRUNE = 0.0

# 「只计数」的建任务路径（**精确匹配**，不含子操作）：这些应用能建任务，但不上报
# `X-Job-Id`，Portal 既拿不到 job id 也轮询不了它的进度 —— 于是过去**一条都不统计**
# （2026-10-12 实测：飞书当天新建 35 个 run，Portal 记录 0）。
# 兜底登记只记「次数 / 用户 / 应用 / 时间」，token 与金额如实留空，绝不臆造。
_COUNT_ONLY_JOB_PATHS = ("/api/runs",)

# 飞书任务：**只有真的生成过才计数**（用户 2026-10-12 明确要求「必须完成一次生成任务后
# 才会有记录」）。等审批 = 还没开始生成；排队/创建中/正在生成同理。等确认（waiting_review）
# 意味着成片已经出来了、只是在等人点确认，算生成完成。
_FEISHU_PRE_GENERATION = {"created", "queued", "pending", "running", "waiting_approval",
                          "approving", "planning"}
# 失败但**一件产物都没有** = 没有发生有效生成，不计数；失败但有产物 = 部分成功，计数。
_FEISHU_FAILED_NO_OUTPUT = {"failed", "cancelled", "canceled", "timed_out", "expired"}
# 飞书的**工作流**状态 ≠ 生成结果。waiting_review 的含义是「片子已生成、等人在飞书里确认」，
# 对「用量记录」而言就是成功（2026-10-13 用户反馈：所有飞书任务都被标成「待确认」，
# 而那个状态在用量语义里根本不该存在）。
_FEISHU_STATUS_DONE = {"done", "waiting_review", "succeeded", "completed", "finished"}


def feishu_usage_status(feishu_status: str, has_results: bool = False) -> str:
    """把飞书的工作流状态翻成**用量口径**的状态：成功 / 失败 / 部分成功。"""
    s = str(feishu_status or "").lower()
    if s in _FEISHU_STATUS_DONE:
        return "succeeded"
    if s == "partial":
        return "partial"
    if s in _FEISHU_FAILED_NO_OUTPUT:
        return "failed"
    # 认不出来的：有产物就算成功，否则原样带出去（宁可显示原始值，也不硬塞一个错状态）
    return "succeeded" if has_results else (s or "succeeded")


def pricing_table() -> dict[str, Any]:
    """读单价表；mtime 变了就重读。

    与 shared/model_registry.json 同样的做法：运维手改单价后**不需要重启 Portal**
    （重启会带走所有子应用和运行中的任务）。文件缺失/损坏 → 回落到内置表。
    """
    with _PRICING_LOCK:
        try:
            mtime = PRICING_PATH.stat().st_mtime
        except OSError:
            mtime = -1.0
        if mtime != _PRICING_CACHE["mtime"]:
            data = None
            if mtime >= 0:
                try:
                    loaded = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and loaded.get("families"):
                        data = loaded
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"  [pricing] 读取失败，沿用内置表: {exc}", flush=True)
            _PRICING_CACHE["data"] = data or _DEFAULT_PRICING
            _PRICING_CACHE["mtime"] = mtime
        return _PRICING_CACHE["data"]


def _normalize_model_key(value: str) -> str:
    """把模型 id 归一后比较：大小写、`.`/`_`/`-` 三种分隔符、空格都拉平。

    同一颗模型在文档/控制台/接口里写作 doubao-seedance-2.0-mini /
    doubao-seedance-2-0-mini 都出现过，归一后一个 pattern 就能匹配到。
    """
    text = str(value or "").strip().lower()
    for ch in (".", "_", " "):
        text = text.replace(ch, "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text


def match_price_family(model: str, table: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """把模型 id / 接入点 id 归到某个模型族。families 是有序的，先匹配到的先用
    （所以 mini / fast / 2.5 必须排在通用的 2.0 前面）。"""
    table = table or pricing_table()
    needle = _normalize_model_key(model)
    if not needle:
        return None
    aliases = {_normalize_model_key(k): v for k, v in (table.get("endpoint_aliases") or {}).items()}
    families = table.get("families") or []
    alias = aliases.get(needle)
    if alias:
        for fam in families:
            if str(fam.get("id") or "") == alias:
                return fam
    for fam in families:
        for pattern in fam.get("match") or []:
            if _normalize_model_key(pattern) in needle:
                return fam
    return None


# 某些应用固定走一条通道、任务里却不带模型名时的兜底（应用事实，不是猜）
_APP_DEFAULT_FAMILIES = {
    "drama-pipeline": ("local-video",),   # 出片固定交本机 MiniMax H3，云费用 ¥0
}


def quote_generation(
    *,
    model: str,
    resolution: str = "",
    image_size: str = "",
    has_input_video: bool = False,
    tokens: int = 0,
    images: int = 0,
    app: str = "",
) -> dict[str, Any]:
    """算一次生成的单价与金额。

    返回 {"unit_price", "unit", "amount", "currency", "label", "family_id", "source"}；
    认不出模型族或取不到对应档位时 amount 为 None、source 为 "unknown"。
    """
    table = pricing_table()
    currency = str(table.get("currency") or "CNY")
    blank = {"unit_price": None, "unit": "", "amount": None, "currency": currency,
             "label": "", "family_id": "", "source": "unknown"}
    fam = match_price_family(model, table)
    if not fam:
        # 按应用兜底：有些应用固定走某条通道，但任务里不带模型名
        # （例如 AI 短剧的出片固定是本机 MiniMax H3）——这是应用事实，不是猜测。
        for family in table.get("families") or []:
            if str(family.get("id") or "") in _APP_DEFAULT_FAMILIES.get(str(app or ""), ()):
                fam = family
                break
    if not fam:
        return blank

    label = str(fam.get("label") or fam.get("id") or "")
    family_id = str(fam.get("id") or "")
    unit = str(fam.get("unit") or "video")
    prices = fam.get("prices") or {}

    if unit == "image":
        # 图片按张计价，档位由输出尺寸决定（方舟按 ≤1.5K / >1.5K≤2K 分档）。
        size_key = str(image_size or "").strip().lower().replace(" ", "")
        bucket = (fam.get("image_size_buckets") or {}).get(size_key)
        if bucket is None:
            bucket = "medium" if "medium" in prices else (next(iter(prices), None))
        per_image = prices.get(str(bucket)) if bucket is not None else None
        if not isinstance(per_image, (int, float)):
            return {**blank, "label": label, "family_id": family_id, "unit": "CNY/张"}
        return {
            "unit_price": float(per_image), "unit": "CNY/张",
            "amount": round(float(images or 0) * float(per_image), 4),
            "currency": currency, "label": label, "family_id": family_id, "source": "pricing_table",
        }

    # 视频按 token 计价：先按分辨率归到档位，认不出就退回 default。
    buckets = fam.get("resolution_buckets") or {}
    res_key = str(resolution or "").strip().lower()
    bucket = buckets.get(res_key)
    if bucket is None:
        bucket = "default" if "default" in prices else None
    tier = prices.get(str(bucket)) if bucket is not None else None
    if not isinstance(tier, dict):
        return {**blank, "label": label, "family_id": family_id, "unit": "CNY/百万token"}
    field = "input_video" if has_input_video else "no_input_video"
    unit_price = tier.get(field)
    if not isinstance(unit_price, (int, float)):
        unit_price = tier.get("no_input_video")
        field = "no_input_video"
    if not isinstance(unit_price, (int, float)):
        return {**blank, "label": label, "family_id": family_id, "unit": "CNY/百万token"}
    amount = round(int(tokens or 0) / 1_000_000 * float(unit_price), 4)
    return {
        "unit_price": float(unit_price), "unit": "CNY/百万token", "amount": amount,
        "currency": currency, "label": label, "family_id": family_id,
        "source": "pricing_table", "tier": str(bucket), "input_video": bool(has_input_video),
    }


# 账单「计费项」的命名（照方舟控制台「消费明细」里的写法），用于导出对账。
# 导出的汇总表按这个字段分组，就能和账单逐项对照。
_BILLING_SEEDANCE_BASE = {
    "2.5": "Doubao-Seedance-2.5在线推理",
    "mini": "Doubao-Seedance-2.0-mini在线推理",
    "fast": "Doubao-Seedance-2.0-fast在线推理",
    "2.0": "Doubao-Seedance-2.0在线推理",
}
_BILLING_SEEDANCE_RES = {
    ("2.5", "480p"): "480P/720P",
    ("2.5", "720p"): "480P/720P",
    ("2.0", "1080p"): "1080P",
    ("2.0", "4k"): "4k",
}
_BILLING_SEEDREAM_SIZE = {
    "1k": "不超过1.5K输出图",
    "1.5k": "不超过1.5K输出图",
    "2k": "超过1.5K不超过2K输出图",
}


# ── 明细/汇总导出的列与取值口径（2026-10-13）─────────────────────────────────
# 用户反馈两件事：① 表头是英文看不懂；② 提示词里带换行，在 Excel 里一条记录折成多行、表格错位。
# 所以表头统一中文、**所有文本值把换行折成空格**（保证一条记录一行）。
# 导出列：**默认给全量 24 列**（用户 2026-10-13 明确「不要减少我的数据」）。
# 精简版（17 列）保留但改成 opt-in：?slim=1。
# 排列顺序讲究：短而常用的列在前（时间/用户/模型/用量/单价/金额…），
# 又长又少用的往后放（提示词、上游任务号在最后），这样打开表格不用横向滚就能看到用量与金额。
GENERATION_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("time", "生成时间"),
    ("source", "来源"),
    ("date", "日期"),
    ("username", "用户"),
    ("app", "应用"),
    ("model", "模型"),
    ("provider_label", "供应商"),
    ("status", "状态"),
    ("tokens", "用量(tokens)"),
    ("unit_price", "单价"),
    ("unit", "计价单位"),
    ("amount", "金额"),
    ("currency", "币种"),
    ("resolution", "分辨率"),
    ("has_input_video", "含输入视频"),
    ("price_family", "计费族"),
    ("runs", "生成次数"),
    ("images", "图片数"),
    ("seconds", "时长(秒)"),
    ("kind", "产物类型"),
    ("price_source", "价格来源"),
    ("provider", "供应商代码"),
    ("job_id", "任务编号"),
    ("task_ids", "上游任务号"),
    ("title", "提示词"),
]
# 精简版（一行别太宽时用）：?slim=1。去掉纯内部/可推出的列。
GENERATION_EXPORT_COLUMNS_SLIM: list[tuple[str, str]] = [
    ("time", "生成时间"),
    ("source", "来源"),
    ("username", "用户"),
    ("app", "应用"),
    ("model", "模型"),
    ("provider_label", "供应商"),
    ("status", "状态"),
    ("kind", "产物类型"),
    ("tokens", "用量(tokens)"),
    ("unit_price", "单价"),
    ("unit", "单位"),
    ("amount", "金额"),
    ("resolution", "分辨率"),
    ("has_input_video", "含输入视频"),
    ("seconds", "时长(秒)"),
    ("images", "图片数"),
    ("title", "提示词"),
    ("task_ids", "上游任务号"),
]
_EXPORT_STATUS_CN = {
    "succeeded": "成功", "failed": "失败", "running": "生成中", "submitted": "已提交",
    "cancelled": "已取消", "pending": "排队中", "interrupted": "被打断",
    "partial": "部分成功", "waiting_approval": "待审批", "waiting_review": "待确认",
}
_EXPORT_KIND_CN = {"image": "图片", "video": "视频", "agent": "飞书任务"}


# xlsx 导出的列宽（单位=字符数，Excel/WPS 的列宽口径）。
# 用户 2026-10-13 反馈「间距太宽、很多信息看起来不方便」——CSV 没有列宽信息，
# 打开后要么全按默认窄列截断、要么「最适合列宽」把 200 字的提示词列拉成一条。
# xlsx 由我们把宽度定死：常用的窄、模型与任务号中等、提示词给 60 字宽（一行能看个大概）。
GENERATION_EXPORT_WIDTHS: dict[str, float] = {
    "time": 20, "source": 16, "date": 12, "username": 10, "app": 16, "model": 30, "provider_label": 12,
    "status": 9, "tokens": 13, "unit_price": 10, "unit": 15, "amount": 10, "currency": 8,
    "resolution": 10, "has_input_video": 12, "price_family": 18, "runs": 9, "images": 8,
    "seconds": 10, "kind": 10, "price_source": 13, "provider": 14, "job_id": 34,
    "task_ids": 38, "title": 60,
}
# 这些列在 xlsx 里写成**真数字**（可以直接求和/排序），CSV 里只能当文本
GENERATION_EXPORT_NUMERIC = {"tokens", "unit_price", "amount", "runs", "images", "seconds"}


def export_cell(field: str, value) -> str:
    """一个导出单元格：中文口径 + 换行折成空格（一个记录一行，Excel 里不折行）。"""
    if field == "source":
        # 本机记录没有 source 字段 → 显示本机标签（可配：remote_sources.json 的 local_label，
        # 默认「服务器A」；同步到另一台机器时把它改成那台的名字，例如「服务器B」）
        if value:
            return str(value)
        try:
            return str(remote_sources.load_config().get("local_label") or "服务器A")
        except Exception:  # noqa: BLE001
            return "服务器A"
    if field == "task_ids":
        return "|".join(value or []) if not isinstance(value, str) else value
    if field == "status":
        return _EXPORT_STATUS_CN.get(str(value or ""), str(value or ""))
    if field == "kind":
        return _EXPORT_KIND_CN.get(str(value or ""), str(value or ""))
    if field == "has_input_video":
        if value is None or value == "":
            return ""
        return "是" if value in (True, "True", "true", 1, "1") else "否"
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def billing_item_label(price_family: str, resolution: str = "", image_size: str = "",
                       has_input_video: bool = False) -> str:
    """把「我们这边的档位」翻译成账单里的计费项名。

    方舟把视频按「模型 + 分辨率 + 有无输入视频」拆成不同计费项（单价能差 1.6 倍），
    这里保持同样的拆分，导出的汇总才能和账单对齐。认不出模型族就返回空串（不猜）。
    """
    text = str(price_family or "").lower()
    res = str(resolution or "").strip().lower()
    size = str(image_size or "").strip().lower().replace(" ", "")
    if not text:
        return ""
    if "seedream" in text:
        return f"Doubao-Seedream-5.0-pro-{_BILLING_SEEDREAM_SIZE.get(size, '输出图')}"
    if "2.5" in text:
        key, base = "2.5", _BILLING_SEEDANCE_BASE["2.5"]
    elif "mini" in text:
        key, base = "mini", _BILLING_SEEDANCE_BASE["mini"]
    elif "fast" in text:
        key, base = "fast", _BILLING_SEEDANCE_BASE["fast"]
    elif "seedance" in text or "2.0" in text:
        key, base = "2.0", _BILLING_SEEDANCE_BASE["2.0"]
    else:
        return ""
    res_part = _BILLING_SEEDANCE_RES.get((key, res), "")
    tier = "有输入视频" if has_input_video else "无输入视频"
    return f"{base}{res_part}（{tier}）"


def _append_generation_record(record: dict[str, Any]) -> None:
    """追加写一条按次明细。统计主链路绝不能因为写明细失败而中断。"""
    global _GENERATIONS_LAST_PRUNE
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _GENERATIONS_LOCK:
            with GENERATIONS_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            now = time.time()
            if now - _GENERATIONS_LAST_PRUNE > 6 * 3600:
                _GENERATIONS_LAST_PRUNE = now
                _prune_generation_records()
    except Exception as exc:  # noqa: BLE001
        print(f"  [generations] append failed: {exc}", flush=True)


def _prune_generation_records() -> None:
    """删掉超过保留期的明细行（追加写文件，整份重写一次即可）。调用方已持锁。"""
    try:
        if not GENERATIONS_PATH.exists():
            return
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - GENERATIONS_RETENTION_DAYS * 86400))
        kept: list[str] = []
        with GENERATIONS_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    day = str(json.loads(line).get("date") or "")
                except json.JSONDecodeError:
                    continue
                if day >= cutoff:
                    kept.append(line if line.endswith("\n") else line + "\n")
        tmp = GENERATIONS_PATH.with_name(GENERATIONS_PATH.name + f".tmp{os.getpid()}")
        tmp.write_text("".join(kept), encoding="utf-8")
        os.replace(tmp, GENERATIONS_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"  [generations] prune failed: {exc}", flush=True)


def read_generation_records(start: str, end: str, username: str = "",
                            is_admin: bool = True, limit: int = 2000,
                            offset: int = 0, user_filter: str = "",
                            app_filter: str = "", status_filter: str = "",
                            query: str = "") -> tuple[list[dict[str, Any]], int]:
    """按日期区间读明细（最新在前），支持按用户/应用/状态/关键词筛选与分页。

    返回 (本页行, 命中总数)。文件是追加写的小 JSONL（一次生成一行），全量扫一遍即可；
    分页在内存里切片，这样 total 与筛选口径天然一致。
    """
    rows: list[dict[str, Any]] = []
    if not GENERATIONS_PATH.exists():
        return rows, 0
    needle = str(query or "").strip().lower()
    try:
        with GENERATIONS_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                day = str(item.get("date") or "")
                if day < start or day > end:
                    continue
                if not is_admin and str(item.get("username") or "") != username:
                    continue
                if user_filter and str(item.get("username") or "") != user_filter:
                    continue
                if app_filter and str(item.get("app") or "") != app_filter:
                    continue
                if status_filter and str(item.get("status") or "") != status_filter:
                    continue
                if needle:
                    hay = " ".join(str(item.get(k) or "") for k in
                                   ("title", "model", "price_family", "job_id", "username")).lower()
                    if needle not in hay:
                        continue
                # 供应商一律**读取时重算**：老记录里存的是改规则之前算出来的标签
                # （例如把通道名 gemini 直接当成供应商），不重算就会新旧混在一起。
                label, detail = provider_info(str(item.get("app") or ""),
                                              str(item.get("model") or ""),
                                              str(item.get("provider") or ""))
                if label:
                    item["provider_label"] = label
                    item["provider_detail"] = detail
                elif item.get("provider_label"):
                    item["provider_detail"] = "旧记录：按当时的规则判定"
                rows.append(item)
    except OSError as exc:
        print(f"  [generations] read failed: {exc}", flush=True)
        return [], 0
    rows.sort(key=lambda r: (str(r.get("date") or ""), float(r.get("ts") or 0)), reverse=True)
    total = len(rows)
    page_size = max(1, min(int(limit or 2000), 20000))
    page_offset = max(0, int(offset or 0))
    return rows[page_offset:page_offset + page_size], total


class UsageTracker:
    def __init__(self):
        self._lock = threading.Lock()
        # 历史记录独立锁：history_upsert 会被 register_job（已持 self._lock）
        # 调用，threading.Lock 不可重入，同锁会死锁。
        self._history_lock = threading.Lock()
        self._data = self._load()
        self._pending_jobs: list[dict] = self._restore_pending_jobs()
        # Debounced persistence: hot paths (record/register_job/inc_daily_jobs/
        # finalize_job/_add_user_stat) used to json.dumps + double-write the whole
        # usage.json to disk *while holding self._lock*, on EVERY proxied request
        # (including every static JS/CSS/image of a sub-app). Under load all proxy
        # threads serialized on that lock + disk I/O, and usage.json grows
        # unboundedly (daily/by_user never pruned) so each write got slower. That
        # was the portal-side stall that dragged all four sub-apps down together.
        #
        # Now _save() just marks dirty + wakes a background flusher. The flusher
        # snapshots under the lock (cheap) and does the json.dumps + disk write
        # OUTSIDE the lock, at most once per _FLUSH_INTERVAL. shutdown() forces a
        # final synchronous flush so nothing is lost on restart.
        self._dirty = False
        self._flush_wake = threading.Event()
        self._stop_flush = threading.Event()
        self._FLUSH_INTERVAL = 2.0
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        threading.Thread(target=self._job_poll_loop, daemon=True).start()
        threading.Thread(target=self._auto_backup_loop, daemon=True).start()
        threading.Thread(target=self._remote_sync_loop, daemon=True).start()

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {"records": [], "daily": {}, "by_user": {}, "job_owners": {}}

    @staticmethod
    def _try_parse(path) -> dict[str, Any] | None:
        try:
            if not path.exists():
                return None
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return None

    def _load(self) -> dict[str, Any]:
        """Load usage data with self-healing from .bak if the primary is corrupt.
        Long-term retention: never drop date keys here."""
        bak_path = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".bak")
        primary = self._try_parse(USAGE_PATH)
        if primary is not None and isinstance(primary, dict):
            d = primary
        else:
            backup = self._try_parse(bak_path)
            if backup is not None and isinstance(backup, dict):
                # Quarantine the corrupt primary so a future _save doesn't clobber the good .bak
                if USAGE_PATH.exists():
                    quarantine = USAGE_PATH.with_name(f"usage.corrupt.{int(time.time())}.json")
                    try:
                        USAGE_PATH.rename(quarantine)
                        print(f"  [usage] primary corrupt, quarantined to {quarantine.name}, recovered from .bak", flush=True)
                    except Exception:
                        pass
                d = backup
            else:
                if USAGE_PATH.exists() or bak_path.exists():
                    print("  [usage] both primary and .bak unreadable, starting fresh (existing files left in place)", flush=True)
                return self._empty_data()
        # Schema-tolerant defaults — never remove existing keys
        d.setdefault("records", [])
        d.setdefault("daily", {})
        d.setdefault("by_user", {})
        d.setdefault("job_owners", {})
        return d

    def _save(self):
        """Mark usage data dirty and wake the background flusher.

        MUST be called while holding self._lock (every caller already does).
        This is now O(1) — no json.dumps, no disk I/O on the request thread —
        so hot paths no longer serialize the whole file under the lock. The
        actual write happens in _flush_loop, at most once per _FLUSH_INTERVAL.
        """
        self._dirty = True
        self._flush_wake.set()

    def _write_to_disk(self, payload: str):
        """Atomic double-write: tmp → primary (rename), then copy primary → .bak.
        Runs OUTSIDE self._lock — payload is a pre-serialized snapshot string.
        Long-term retention: do NOT prune any date keys here."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".tmp")
        bak_path = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".bak")
        try:
            tmp_path.write_text(payload, "utf-8")
            os.replace(tmp_path, USAGE_PATH)
        except Exception as exc:
            print(f"  [usage] _save primary write failed: {exc}", flush=True)
            return
        # Refresh .bak (best-effort; failure here doesn't compromise primary)
        try:
            shutil.copyfile(USAGE_PATH, bak_path)
        except Exception:
            pass

    def _flush_now(self) -> bool:
        """Serialize a snapshot under the lock, then write it to disk outside
        the lock. Returns True if a write was performed. Only json.dumps holds
        the lock (unavoidable — must snapshot a consistent view); the two disk
        writes happen lock-free so other threads keep moving."""
        with self._lock:
            if not self._dirty:
                return False
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            self._dirty = False
        self._write_to_disk(payload)
        return True

    def _flush_loop(self):
        """Debounced writer: wake on _flush_wake or every _FLUSH_INTERVAL,
        coalescing bursts of _save() calls into a single disk write. On
        _stop_flush (shutdown), do a final flush and exit."""
        while not self._stop_flush.is_set():
            self._flush_wake.wait(timeout=self._FLUSH_INTERVAL)
            self._flush_wake.clear()
            try:
                self._flush_now()
            except Exception as exc:
                print(f"  [usage] flush loop error: {exc}", flush=True)
        # Final drain on shutdown so nothing dirty is lost across restart.
        try:
            self._flush_now()
        except Exception:
            pass

    def flush(self):
        """Stop the background flusher and force a final synchronous write.

        Reliable barrier: signals the loop to stop, joins it (so it cannot race
        us by grabbing the dirty flag and writing after we return), then does one
        last _flush_now(). Used on shutdown and by tests that assert on-disk
        state right after record()."""
        self._stop_flush.set()
        self._flush_wake.set()
        t = getattr(self, "_flush_thread", None)
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        try:
            self._flush_now()
        except Exception:
            pass

    def _remote_sync_loop(self):
        """定时把「另一台同款平台」的按次明细拉进本机（用户 2026-10-13 需求：两边记录进同一张表）。

        失败**只影响该来源**：remote_sources.sync_all() 内部保留上次数据并返回错误，
        这里再兜一层 try —— 对面挂了绝不能让本机统计页或启动流程受影响。
        """
        if self._stop_flush.wait(30):          # 启动后缓一下，别和启动流程抢资源
            return
        while True:
            interval = remote_sources.DEFAULT_INTERVAL_MIN
            try:
                cfg = remote_sources.load_config()
                interval = max(1, int(cfg.get("interval_minutes") or remote_sources.DEFAULT_INTERVAL_MIN))
            except Exception:
                pass
            try:
                for name, info in (remote_sources.sync_all() or {}).items():
                    if info.get("skipped"):
                        continue
                    if info.get("ok"):
                        print(f"  [remote] {name}: 同步 {info.get('count')} 条", flush=True)
                    else:
                        print(f"  [remote] {name}: 同步失败（保留上次数据）{info.get('error')}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  [remote] sync loop error: {exc}", flush=True)
            if self._stop_flush.wait(max(60, interval * 60)):
                return

    def _auto_backup_loop(self):
        """定期备份：usage.json.bak 同步 + history.json 按天留快照。

        历史记录是**一次损坏就全丢**的单文件 —— 2026-10-12 真丢过 2554 条，而磁盘上
        没有任何副本可恢复（只剩子应用活动日志里的最近 100 条/应用）。所以这里额外留一份
        按天命名的快照，并清理 14 天前的旧快照。启动时先立刻做一次，不等 6 小时。
        """
        while True:
            try:
                if USAGE_PATH.exists() and self._try_parse(USAGE_PATH) is not None:
                    bak_path = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".bak")
                    shutil.copyfile(USAGE_PATH, bak_path)
            except Exception:
                pass
            try:
                if HISTORY_PATH.exists() and self._try_parse(HISTORY_PATH) is not None:
                    snap = HISTORY_PATH.with_name(
                        f"{HISTORY_PATH.stem}.daily-{time.strftime('%Y-%m-%d')}{HISTORY_PATH.suffix}")
                    if not snap.exists():
                        shutil.copyfile(HISTORY_PATH, snap)
                        print(f"  [history] 日备份 → {snap.name}", flush=True)
                    cutoff = time.time() - 14 * 86400
                    for old in HISTORY_PATH.parent.glob(
                            f"{HISTORY_PATH.stem}.daily-*{HISTORY_PATH.suffix}"):
                        try:
                            if old.stat().st_mtime < cutoff:
                                old.unlink()
                        except OSError:
                            pass
            except Exception:
                pass
            time.sleep(6 * 3600)

    def record(self, app: str, client_ip: str, method: str, path: str, username: str = ""):
        today = time.strftime("%Y-%m-%d")
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "app": app, "ip": client_ip,
                 "username": username, "method": method, "path": path}
        with self._lock:
            self._data["records"].append(entry)
            if len(self._data["records"]) > 2000:
                self._data["records"] = self._data["records"][-1000:]
            day_stats = self._data["daily"].setdefault(today, {})
            app_stats = day_stats.setdefault(app, {"requests": 0, "jobs": 0})
            app_stats["requests"] += 1
            self._save()
        # Below the lock: jsonl append + prune are best-effort. Helpers already
        # swallow errors internally; the outer try/except is a second layer so
        # even a monkey-patched replacement (tests) cannot break usage.json save.
        try:
            _append_usage_jsonl(entry, today)
        except Exception:
            pass
        try:
            _prune_old_usage_jsonl(today)
        except Exception:
            pass

    def register_job(self, app: str, job_id: str, username: str, job_type: str = "image", duration_per_item: int = 0, metadata: dict | None = None):
        meta = metadata or {}
        params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
        with self._lock:
            # 去重：同一 (app, job_id) 只能有一个 pending 条目。重复条目会在轮询终态时
            # 重复计账 —— by_user 与按次明细一起被放大（2026-10-12 实测同一 job 写了 6 次）。
            if any(j.get("app") == app and j.get("job_id") == job_id for j in self._pending_jobs):
                print(f"  [usage] 重复注册被忽略: {app}:{job_id}", flush=True)
                return
            self._pending_jobs.append({
                "app": app, "job_id": job_id, "username": username,
                "job_type": job_type, "duration_per_item": duration_per_item,
                "submitted_at": time.time(), "date": time.strftime("%Y-%m-%d"),
                # 按次明细要用的维度：模型 + 分辨率（或图片尺寸）+ 有无输入视频。
                # 方舟同一模型这两档单价差 1.6 倍，缺了就没法算钱。
                "model": str(meta.get("model") or "")[:120],
                "provider": str(meta.get("provider") or "")[:60],
                "resolution": str(params.get("resolution") or "")[:20],
                "image_size": str(params.get("image_size") or "")[:20],
                "has_input_video": bool(meta.get("has_input_video")),
                "media_fields": list(meta.get("media_fields") or [])[:20],
                "title": str(meta.get("title") or meta.get("prompt") or "")[:120],
            })
            owners = self._data.setdefault("job_owners", {})
            owners[f"{app}:{job_id}"] = {"username": username, "ts": time.time()}
            # cap to last 5000 to bound disk growth
            if len(owners) > 5000:
                kept = sorted(owners.items(), key=lambda kv: kv[1].get("ts", 0))[-3000:]
                self._data["job_owners"] = dict(kept)
            # 历史记录 pending 落库（采集失败不影响统计）
            meta = metadata or {}
            self.history_upsert({
                "app": app, "job_id": job_id, "username": username,
                "kind": "video" if job_type == "video" else "image",
                "activity_id": str(meta.get("activity_id", "")).strip()[:100],
                "title": str(meta.get("title", "") or meta.get("prompt", "")).strip()[:200],
                "prompt": str(meta.get("prompt", "")).strip()[:2000],
                "model": str(meta.get("model", ""))[:200],
                "params": meta.get("params") if isinstance(meta.get("params"), dict) else {},
                "status": "pending",
                "submitted_at": time.time(),
                "completed_at": None,
                "duration": 0,
                "thumb_url": "",
                "results": [],
                "error": "",
            })
            self._save()

    def get_job_owner(self, app: str, job_id: str) -> str:
        with self._lock:
            o = self._data.get("job_owners", {}).get(f"{app}:{job_id}")
            return o.get("username", "") if isinstance(o, dict) else ""

    # ── 历史记录（任务级留档，与统计计数互不干扰） ──────────────────────
    def _load_history(self) -> dict:
        if not HISTORY_PATH.exists():
            return {}
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _restore_pending_jobs(self) -> list[dict]:
        """Resume polling non-terminal history records after a Portal restart.

        按 (app, job_id) 去重：同一任务在历史里若有多条记录，**必须只产生一个 pending
        条目** —— 多份条目会让轮询终态时重复计账，把 by_user 和按次明细一起放大
        （2026-10-12：同一 job 被写 5~6 次，token 翻了几倍）。
        """
        restored: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for record in self._load_history().values():
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or "").lower()
            app = str(record.get("app") or "")
            job_id = str(record.get("job_id") or "")
            if status not in {"pending", "queued", "submitted", "running", "processing"}:
                continue
            if not app or app not in APPS or not job_id:
                continue
            if (app, job_id) in seen:
                continue
            seen.add((app, job_id))
            params = record.get("params") if isinstance(record.get("params"), dict) else {}
            try:
                duration_per_item = max(0, int(params.get("duration") or 0))
            except (TypeError, ValueError):
                duration_per_item = 0
            submitted_at = float(record.get("submitted_at") or time.time())
            restored.append({
                "app": app,
                "job_id": job_id,
                "username": str(record.get("username") or ""),
                "job_type": "video" if record.get("kind") == "video" else "image",
                "duration_per_item": duration_per_item,
                "submitted_at": submitted_at,
                "date": time.strftime("%Y-%m-%d", time.localtime(submitted_at)),
                # 恢复的条目也要带上传价维度，否则这些任务在按次明细里会是「未识别计费项」、
                # 算不出金额（2026-10-12 实测：一次重启后 4 条/132 万 token 没有价）。
                # 有无输入视频在历史记录里没有存（不进白名单），只能按「无输入」档保守估算。
                "model": str(record.get("model") or "")[:120],
                "resolution": str(params.get("resolution") or "")[:20],
                "image_size": str(params.get("image_size") or "")[:20],
                "has_input_video": False,
                "media_fields": [],
                "title": str(record.get("title") or "")[:120],
            })
        return restored

    def history_records(self) -> dict:
        """任务级历史记录（区别于统计页按天 get_history）。"""
        return self._load_history()

    def history_update_terminal(self, app: str, job_id: str, status: str,
                                data: dict, job_type: str) -> None:
        """轮询到终态时更新历史记录：状态/结果清单/缩略图/时长/错误。"""
        try:
            rec = self.history_records().get(f"{app}:{job_id}")
            if not rec:
                return
            if _backfill_history_record(rec):
                self.history_upsert(rec)
            nested = data.get("job") if isinstance(data.get("job"), dict) else {}
            done = int(data.get("done") or nested.get("done") or 0)
            per_item = (int(data.get("duration") or 0)
                        or int(data.get("duration_seconds") or 0)
                        or int(nested.get("duration") or 0)
                        or 0)
            results = history_result_items(data, job_type)
            errors = data.get("errors") or nested.get("errors") or []
            error_text = str(data.get("error") or nested.get("error") or "").strip()
            if not error_text and isinstance(errors, list) and errors:
                first = errors[0]
                error_text = str(first.get("message") or first.get("error") or first
                                 if isinstance(first, dict) else first).strip()
            if not str(rec.get("model") or "").strip():
                model = data.get("model") or nested.get("model") or ""
                if model:
                    rec["model"] = str(model).strip()[:200]
            if not str(rec.get("prompt") or "").strip():
                prompt = data.get("prompt") or nested.get("prompt") or rec.get("title") or ""
                if prompt:
                    rec["prompt"] = str(prompt).strip()[:2000]

            rec["status"] = normalize_history_status(status)
            rec["completed_at"] = time.time()
            rec["duration"] = int(done * per_item) if done > 0 and per_item else 0
            rec["thumb_url"] = results[0]["url"] if results else ""
            rec["results"] = results
            rec["error"] = error_text[:500]
            self.history_upsert(rec)
        except Exception:
            pass

    def refresh_history_from_child(self, app: str, job_id: str, status: str) -> None:
        """Synchronize a terminal job callback into the persisted history.

        Child backends call /api/internal/jobs/finalize as soon as a task ends.
        Reading the child job snapshot here makes the history record complete
        immediately instead of waiting for Portal's fallback poll.
        """
        key = f"{app}:{job_id}"
        record = self.history_records().get(key)
        if not isinstance(record, dict):
            return
        conn = None
        data: dict = {}
        try:
            spec = SPEC_BY_NAME.get(app)
            if spec is not None:
                conn = http.client.HTTPConnection("127.0.0.1", spec.port, timeout=5)
                conn.request("GET", "/api/jobs/" + urllib.parse.quote(job_id, safe=""))
                response = conn.getresponse()
                if response.status == 200:
                    parsed = json.loads(response.read().decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict):
                        data = parsed
        except Exception:
            data = {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if data:
            job_type = "video" if record.get("kind") == "video" else "image"
            self.history_update_terminal(app, job_id, status, data, job_type)
            return
        record["status"] = normalize_history_status(status)
        record["completed_at"] = time.time()
        self.history_upsert(record)

    def query_history(self, *, username: str, is_admin: bool,
                      days: int = 30, kind: str = "all", status: str = "all",
                      q: str = "", user_filter: str = "", app: str = "",
                      limit: int = 60, offset: int = 0) -> tuple[list, int]:
        """返回 (items, total)。admin 全量，普通用户强制只看本人。"""
        data = self._load_history()
        cutoff = time.time() - max(1, min(int(days), 365)) * 86400
        rows = []
        for rec in data.values():
            if not isinstance(rec, dict):
                continue
            if float(rec.get("submitted_at") or 0) < cutoff:
                continue
            if not is_admin and rec.get("username") != username:
                continue
            if user_filter and rec.get("username") != user_filter:
                continue
            if app and rec.get("app") != app:
                continue
            if kind != "all" and rec.get("kind") != kind:
                continue
            if status != "all" and rec.get("status") != status:
                continue
            if q:
                hay = f"{rec.get('prompt', '')} {rec.get('job_id', '')}".lower()
                if q.lower() not in hay:
                    continue
            rows.append(rec)
        rows.sort(key=lambda r: float(r.get("submitted_at") or 0), reverse=True)
        total = len(rows)
        return rows[offset:offset + limit], total

    def history_upsert(self, record: dict) -> None:
        """写/更新一条历史记录；同次写入顺带剪枝（>N 天 + 总量上限）。
        任何异常吞掉——历史采集失败绝不影响统计与代理。"""
        try:
            with self._history_lock:
                data = self._load_history()
                key = f"{record['app']}:{record['job_id']}"
                previous = data.get(key) if isinstance(data.get(key), dict) else {}
                previous_status = normalize_history_status(str(previous.get("status") or ""))
                current_status = normalize_history_status(str(record.get("status") or ""))
                should_publish = (
                    current_status in {"done", "failed"}
                    and current_status != previous_status
                )
                data[key] = record
                cutoff = time.time() - HISTORY_DAYS * 86400
                data = {k: v for k, v in data.items()
                        if float(v.get("submitted_at") or 0) >= cutoff}
                if len(data) > HISTORY_CAP:
                    sorted_items = sorted(
                        data.items(),
                        key=lambda kv: float(kv[1].get("submitted_at") or 0),
                    )
                    data = dict(sorted_items[-HISTORY_CAP:])
                HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                # 原子写：tmp 名带 pid+线程，避免并发写互相截断（固定 ".tmp" 在 Windows
                # 上出现过「合法 JSON + 多余 }」的损坏 → 之后 _load_history 解析失败 →
                # 下一次 upsert 把整个历史重写成 1~2 条，**直接丢历史**，2026-10-12 真实发生。
                # os.replace 还会因为读者持有句柄而失败（本仓库记录过的 WinError 5），
                # 所以失败要重试，不能一次失败就算了（那会丢这次更新）。
                tmp = HISTORY_PATH.with_name(f"{HISTORY_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                for attempt in range(6):
                    try:
                        os.replace(tmp, HISTORY_PATH)
                        break
                    except OSError:
                        if attempt == 5:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            if should_publish:
                _publish_history_event(record)
        except Exception:
            pass

    def _job_poll_loop(self):
        while True:
            time.sleep(15)
            with self._lock:
                jobs = list(self._pending_jobs)
            if not jobs:
                continue
            done_ids = []
            for job in jobs:
                try:
                    port = APPS[job["app"]]["port"]
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    conn.request("GET", f"/api/jobs/{job['job_id']}")
                    resp = conn.getresponse()
                    if resp.status == 404:
                        # 子应用内存态 JOBS 已丢（典型：子应用重启）。终态回调
                        # 不会再发生——按失败回滚注册时的 daily.jobs +1，
                        # 立即停止轮询，避免统计虚高为「成功」。
                        try:
                            self.finalize_job(job["app"], job["job_id"], "failed")
                        except Exception:
                            pass
                        # 历史记录：子应用重启导致任务丢失 → 标失败
                        try:
                            hist = self.history_records()
                            rec = hist.get(f"{job['app']}:{job['job_id']}")
                            if rec:
                                rec["status"] = "failed"
                                rec["completed_at"] = time.time()
                                rec["error"] = "任务丢失（子应用可能已重启）"
                                self.history_upsert(rec)
                        except Exception:
                            pass
                        done_ids.append(job["job_id"])
                    elif resp.status == 200:
                        data = json.loads(resp.read())
                        # dreamina nests fields under "job"; tracker prefers top-level
                        # (handle_job_status flattens them) but fall back for safety.
                        nested = data.get("job") if isinstance(data.get("job"), dict) else {}
                        status = data.get("status") or nested.get("status") or ""
                        # cancelled 也是终态（子应用取消任务）：照常按 done 计费
                        # 已完成条目、然后从 pending 移除，否则会空转轮询到 2h 超时
                        if status in ("succeeded", "failed", "completed", "cancelled", "canceled"):
                            # `done` is the count of items the sub-app actually
                            # produced (each successful item does JOBS[id]["done"] += 1),
                            # independent of the final status: a partial success
                            # (e.g. 3 of 4 images, final_status="failed") still
                            # reports done=3, and those 3 were really generated and
                            # billed. A pure failure reports done=0.
                            #
                            # We record the real `done` verbatim. Do NOT floor it to
                            # max(1, ...) — that turned a 0-output failure into one
                            # phantom image in by_user.images (the poll loop only sees
                            # a job here when the sub-app's finalize_job callback failed
                            # to land, so failures do reach this branch). We already
                            # count and roll back the *job* separately in daily.jobs via
                            # inc_daily_jobs/finalize_job; this branch is only for the
                            # per-item images/seconds tally.
                            done = int(data.get("done") or nested.get("done") or 0)
                            # Re-derive job_type from response when possible — dreamina
                            # retry endpoints may have been misclassified at register time.
                            # Only override if task_type carries explicit signal; otherwise
                            # keep the job_type set at register time (e.g. volcengine-portrait
                            # uses "virtual"/"real" which conveys no image/video info).
                            task_type = (data.get("task_type") or nested.get("task_type") or "").lower()
                            job_type = job["job_type"]
                            if "video" in task_type or "frame" in task_type:
                                job_type = "video"
                            elif "text2image" in task_type or "image2image" in task_type:
                                job_type = "image"
                            # done <= 0 means the job produced nothing (pure
                            # failure). Skip the stat write entirely — no phantom
                            # image, no meaningless 0-row — but still drop it from
                            # pending below so we stop polling it.
                            per_item = 0
                            # 方舟 token 用量：子应用把上游 usage 累加到 job 上，随 job
                            # JSON 一起带出来（seedance / nano-banana / volcengine-portrait）。
                            # 非方舟通道不带这个字段 → 0。与 images/seconds 同口径：
                            # 只有真产出（done>0）才计。
                            tokens = int(data.get("tokens") or nested.get("tokens") or 0)
                            # `tallied` 兜底：即使同一 job 意外出现多个 pending 条目（或终态分支
                            # 在写完之后抛异常没走到 done_ids），也绝不重复计账。
                            if done > 0 and not job.get("tallied"):
                                job["tallied"] = True
                                if job_type == "video":
                                    # Prefer per-item duration the subapp reports directly.
                                    per_item = (int(data.get("duration") or 0)
                                                or int(data.get("duration_seconds") or 0)
                                                or int(nested.get("duration") or 0)
                                                or int(job.get("duration_per_item") or 0))
                                    self._add_user_stat(job["date"], job["username"], job["app"], 0, done * per_item, tokens)
                                else:
                                    self._add_user_stat(job["date"], job["username"], job["app"], done, 0, tokens)
                                # 按次明细（像中转站的使用记录）：一次生成一条，带用量与金额。
                                # 写明细失败绝不能影响上面的统计，所以整块兜住异常。
                                try:
                                    self._record_generation(job, done, per_item, tokens, status, data, nested)
                                except Exception as exc:  # noqa: BLE001
                                    print(f"  [generations] record failed: {exc}", flush=True)
                            # 历史记录终态更新（采集失败不影响统计）
                            self.history_update_terminal(
                                job["app"], job["job_id"], status, data, job_type)
                            done_ids.append(job["job_id"])
                    conn.close()
                except Exception:
                    pass
                if time.time() - job["submitted_at"] > 7200:
                    # 轮询 2 小时仍未拿到终态、回调也未到：按失败回滚注册时
                    # 的 daily.jobs +1，避免「永远 pending」的任务虚高为成功。
                    # finalize_job 幂等：若任务之后真的成功并回调，也不会重复回滚。
                    try:
                        self.finalize_job(job["app"], job["job_id"], "failed")
                    except Exception:
                        pass
                    # 历史记录：2 小时超时 → 标失败
                    try:
                        hist = self.history_records()
                        rec = hist.get(f"{job['app']}:{job['job_id']}")
                        if rec:
                            rec["status"] = "failed"
                            rec["completed_at"] = time.time()
                            rec["error"] = "任务超时（2 小时未完成）"
                            self.history_upsert(rec)
                    except Exception:
                        pass
                    done_ids.append(job["job_id"])
            if done_ids:
                with self._lock:
                    self._pending_jobs = [j for j in self._pending_jobs if j["job_id"] not in done_ids]

    def record_untracked_job(self, app: str, username: str, job_type: str = "image",
                             title: str = "") -> None:
        """兜底登记「不上报 X-Job-Id」的生成任务（如飞书 run）。

        Portal 没有它的 job id、**轮询不了进度**，所以这里只做三件事：
          - `daily.jobs` +1（任务数）
          - 写一条按次明细（`status="submitted"`、`tokens=0`、金额留空、`tracked=False`）
          - **不动 by_user** —— 张数/秒数/token 都不知道，写 0 会污染「按用户统计」
        """
        self.inc_daily_jobs(app)
        try:
            _append_generation_record({
                "ts": time.time(),
                "date": time.strftime("%Y-%m-%d"),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "username": username,
                "app": app,
                "job_id": "",
                "task_ids": [],
                "kind": job_type,
                "model": "",
                "title": str(title or "")[:120],
                "resolution": "",
                "has_input_video": False,
                "media_fields": [],
                "runs": 0,
                "images": 0,
                "seconds": 0,
                "tokens": 0,
                "unit_price": None,
                "unit": "",
                "amount": None,
                "currency": "CNY",
                "price_source": "untracked",
                "price_family": "",
                "status": "submitted",
                "tracked": False,
                "note": "该应用不上报 X-Job-Id，Portal 无法轮询进度：只记次数与归属，token/金额留空",
            })
        except Exception as exc:  # noqa: BLE001
            print(f"  [generations] untracked record failed: {exc}", flush=True)

    def _add_user_stat(self, date: str, username: str, app: str, images: int, seconds: int, tokens: int = 0):
        with self._lock:
            by_user = self._data.setdefault("by_user", {}).setdefault(date, {})
            u = by_user.setdefault(username, {})
            # `tokens` 是后加的字段：老数据里没有，一律按 0 兼容（火山方舟按 token
            # 计费；t8star / gemini / 本地 ComfyUI 不产生 token，恒为 0）。
            s = u.setdefault(app, {"images": 0, "seconds": 0, "tokens": 0})
            s["images"] += images
            s["seconds"] += seconds
            s["tokens"] = int(s.get("tokens") or 0) + int(tokens or 0)
            self._save()

    def _record_generation(self, job: dict, done: int, per_item: int, tokens: int,
                           status: str, data: dict, nested: dict) -> None:
        """写一条按次明细（像中转站的使用记录）：一次生成一条。

        字段刻意与计费口径对齐：模型 + 分辨率 + 有无输入视频 → 单价；用量（token
        或张数）→ 金额。认不出单价的模型 amount 为 None（前端显示「—」），
        **不猜价格**。
        """
        job_type = str(job.get("job_type") or "image")
        is_video = job_type == "video"
        quote = quote_generation(
            model=str(job.get("model") or ""),
            resolution=str(job.get("resolution") or ""),
            image_size=str(job.get("image_size") or ""),
            has_input_video=bool(job.get("has_input_video")),
            tokens=int(tokens or 0),
            images=0 if is_video else int(done or 0),
            app=str(job.get("app") or ""),
        )
        task_ids: list[str] = []
        for source in (data, nested):
            raw = source.get("provider_tasks") if isinstance(source, dict) else None
            if not isinstance(raw, list):
                continue
            for item in raw:
                if isinstance(item, dict) and item.get("task_id"):
                    tid = str(item["task_id"])
                    if tid and tid not in task_ids:
                        task_ids.append(tid)
        _append_generation_record({
            "ts": time.time(),
            "date": job.get("date") or time.strftime("%Y-%m-%d"),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "username": job.get("username") or "",
            "app": job.get("app") or "",
            "job_id": job.get("job_id") or "",
            "task_ids": task_ids[:20],
            "kind": job_type,
            "model": job.get("model") or "",
            "provider": job.get("provider") or "",
            "provider_label": provider_label(str(job.get("app") or ""),
                                             str(job.get("model") or ""),
                                             str(job.get("provider") or "")),
            "title": job.get("title") or "",
            "resolution": job.get("resolution") or "",
            "has_input_video": bool(job.get("has_input_video")),
            "media_fields": list(job.get("media_fields") or [])[:20],
            "runs": int(done or 0),
            "images": 0 if is_video else int(done or 0),
            "seconds": (int(done or 0) * int(per_item or 0)) if is_video else 0,
            "tokens": int(tokens or 0),
            "unit_price": quote.get("unit_price"),
            "unit": quote.get("unit") or "",
            "amount": quote.get("amount"),
            "currency": quote.get("currency") or "CNY",
            "price_source": quote.get("source") or "unknown",
            "price_family": quote.get("label") or "",
            "price_family_id": quote.get("family_id") or "",
            "status": status,
        })

    def inc_daily_jobs(self, app: str):
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            day_stats = self._data.setdefault("daily", {}).setdefault(today, {})
            app_stats = day_stats.setdefault(app, {"requests": 0, "jobs": 0})
            app_stats["jobs"] = int(app_stats.get("jobs", 0)) + 1
            self._save()

    def finalize_job(self, app: str, job_id: str, status: str) -> bool:
        """Idempotent: mark a job final. If status indicates failure/cancel,
        roll back the +1 that inc_daily_jobs applied at registration time.
        Returns True if the rollback was applied (i.e. first time we saw
        a failure for this job), False otherwise.

        This must NOT drop the job from _pending_jobs. Sub-apps report
        final_status="failed" whenever a single item errored, so a partial
        success (3 of 4 images generated and billed) arrives here as a failure.
        The two paths write disjoint counters — this one owns daily.jobs, the
        poll loop owns by_user.images/seconds — and the `finalized` flag above
        already makes the daily.jobs rollback idempotent. Removing the job from
        pending would leave those 3 real images uncounted forever.
        """
        status = (status or "").lower()
        is_failure = status in ("failed", "fail", "failure", "cancelled", "canceled", "error")
        with self._lock:
            owners = self._data.setdefault("job_owners", {})
            key = f"{app}:{job_id}"
            owner = owners.get(key)
            if not isinstance(owner, dict):
                return False
            if owner.get("finalized"):
                return False
            owner["finalized"] = True
            owner["final_status"] = status
            rolled = False
            if is_failure:
                ts = owner.get("ts", time.time())
                date = time.strftime("%Y-%m-%d", time.localtime(ts))
                day_stats = self._data.setdefault("daily", {}).setdefault(date, {})
                app_stats = day_stats.setdefault(app, {"requests": 0, "jobs": 0})
                app_stats["jobs"] = max(0, int(app_stats.get("jobs", 0)) - 1)
                rolled = True
            self._save()
            return rolled

    def queue_snapshot(self, username: str = "", is_admin: bool = False) -> list[dict]:
        """Return the current user's active jobs with queue position and ETA.

        Position is relative to active jobs in the same app. ETA is a coarse
        frontend-friendly estimate (not a billing promise) and is intentionally
        marked as an estimate so the UI can show a range.
        """
        with self._lock:
            rows = [dict(j) for j in self._pending_jobs
                    if is_admin or j.get("username") == username]
        rows.sort(key=lambda j: float(j.get("submitted_at") or 0))
        by_app: dict[str, list[dict]] = {}
        for job in rows:
            by_app.setdefault(job.get("app", ""), []).append(job)
        out: list[dict] = []
        for app, jobs in by_app.items():
            for idx, job in enumerate(jobs, start=1):
                job_type = str(job.get("job_type") or "").lower()
                is_video = "video" in job_type or app in ("seedance", "volcengine-portrait")
                base_minutes = 4 if is_video else 1
                ahead_minutes = 3 if is_video else 0.5
                eta_minutes = round(base_minutes + (idx - 1) * ahead_minutes, 1)
                out.append({
                    "app": app,
                    "job_id": job.get("job_id", ""),
                    "job_type": job.get("job_type", ""),
                    "submitted_at": job.get("submitted_at", 0),
                    "queue_position": idx,
                    "eta_minutes": eta_minutes,
                })
        return out
    def _is_job_request(self, method: str, path: str) -> bool:
        if method != "POST":
            return False
        job_patterns = ["/api/jobs", "/api/text2image", "/api/image2image", "/api/text2video",
                        "/api/image2video", "/api/frames2video", "/api/multimodal2video", "/api/multiframe2video",
                        "/api/virtual/jobs", "/api/real/jobs",
                        # infinite-canvas 的画布前端契约固定用 /api/v1/jobs（其 web/src/api/jobs.ts:4），
                        # 而 "/api/v1/jobs".startswith("/api/jobs") 是 False —— 不列在这里就完全不计数。
                        # 注意 /api/v1/jobs/{id}/cancel 也是 POST 且命中本前缀，但取消接口
                        # 刻意不返回 X-Job-Id，因此 :2093-2097 的第三个条件不满足，不会误登记。
                        "/api/v1/jobs"]
        return any(path.startswith(p) for p in job_patterns)

    def _is_count_only_job_request(self, method: str, path: str) -> bool:
        """兜底登记：能建任务、但不上报 X-Job-Id 的应用（见 _COUNT_ONLY_JOB_PATHS）。

        **精确匹配**（去掉 query 与尾部斜杠），所以 `/api/runs/<id>/decision` 这类子操作
        不会被算成新建任务。
        """
        if method != "POST":
            return False
        clean = path.split("?", 1)[0].rstrip("/")
        return clean in _COUNT_ONLY_JOB_PATHS

    def get_stats(self, username: str = "", role: str = "user") -> dict[str, Any]:
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            today_stats = self._data["daily"].get(today, {})
            recent = self._data["records"][-20:]
            by_user_today = self._data.get("by_user", {}).get(today, {})
            if role != "admin" and username:
                by_user_today = {username: by_user_today.get(username, {})}
        total_jobs = sum(v.get("jobs", 0) for v in today_stats.values())
        total_requests = sum(v.get("requests", 0) for v in today_stats.values())
        return {
            "today": today, "today_jobs": total_jobs, "today_requests": total_requests,
            "by_app": today_stats if role == "admin" else {},
            "by_user": by_user_today,
            "recent": recent if role == "admin" else [],
            "daily": dict(list(self._data["daily"].items())[-7:]) if role == "admin" else {},
        }

    def get_day(self, date: str, username: str = "", role: str = "user") -> dict[str, Any]:
        """Return daily + by_user for a specific date string YYYY-MM-DD."""
        with self._lock:
            daily = self._data.get("daily", {}).get(date, {})
            by_user = dict(self._data.get("by_user", {}).get(date, {}))
        if role != "admin" and username:
            by_user = {username: by_user.get(username, {})}
        total_jobs = sum(v.get("jobs", 0) for v in daily.values())
        total_requests = sum(v.get("requests", 0) for v in daily.values())
        return {
            "date": date,
            "total_jobs": total_jobs,
            "total_requests": total_requests,
            "by_app": daily if role == "admin" else {},
            "by_user": by_user,
        }

    def get_history(self, days: int = 30, username: str = "", role: str = "user") -> dict[str, Any]:
        """Return per-user per-app daily values for the last N days.

        Shape:
            {
              "dates":  ["YYYY-MM-DD", ...],          # length=days, ascending, today is last
              "users": {
                "<username>": {
                  "<app>": {"images": [..N..], "seconds": [..N..]},
                  ...
                },
                ...
              }
            }

        Non-admins only see their own row. Missing dates → zero-filled.
        """
        days = max(1, min(int(days or 30), 365))
        # Build ascending date list ending at today
        now_t = time.time()
        dates: list[str] = []
        for i in range(days - 1, -1, -1):
            dates.append(time.strftime("%Y-%m-%d", time.localtime(now_t - i * 86400)))
        date_set = set(dates)
        with self._lock:
            by_user_full = self._data.get("by_user", {})
            # Collect all users that appeared in the window
            user_universe: set[str] = set()
            for d in dates:
                user_universe.update((by_user_full.get(d, {}) or {}).keys())
            if role != "admin":
                user_universe = {username} if username else set()
            users: dict[str, dict[str, dict[str, list[int]]]] = {}
            for u in sorted(user_universe):
                per_app: dict[str, dict[str, list[int]]] = {}
                for d in dates:
                    day_user = (by_user_full.get(d, {}) or {}).get(u, {}) or {}
                    for app_name, stats in day_user.items():
                        slot = per_app.setdefault(app_name, {"images": [0] * len(dates), "seconds": [0] * len(dates), "tokens": [0] * len(dates)})
                        idx = dates.index(d)
                        slot["images"][idx] = int(stats.get("images", 0) or 0)
                        slot["seconds"][idx] = int(stats.get("seconds", 0) or 0)
                        slot["tokens"][idx] = int(stats.get("tokens", 0) or 0)
                users[u] = per_app
        # Hide dates outside the window (the loop above only writes inside the window)
        _ = date_set
        return {"dates": dates, "users": users}

    @staticmethod
    def _validate_date(s: str) -> str | None:
        """Return YYYY-MM-DD if valid, else None."""
        if not s or len(s) != 10 or s[4] != "-" or s[7] != "-":
            return None
        try:
            time.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            return None

    @staticmethod
    def _date_range(start: str, end: str) -> list[str]:
        """Inclusive date list start..end. Caps at 366 days to bound payload."""
        start_t = time.mktime(time.strptime(start, "%Y-%m-%d"))
        end_t = time.mktime(time.strptime(end, "%Y-%m-%d"))
        if end_t < start_t:
            start_t, end_t = end_t, start_t
        days = int((end_t - start_t) // 86400) + 1
        days = max(1, min(days, 366))
        return [time.strftime("%Y-%m-%d", time.localtime(start_t + i * 86400)) for i in range(days)]

    def get_range(self, start: str, end: str, username: str = "", role: str = "user") -> dict[str, Any]:
        """Per-user per-app daily series for any [start, end] window. Same shape as get_history."""
        dates = self._date_range(start, end)
        with self._lock:
            by_user_full = self._data.get("by_user", {})
            user_universe: set[str] = set()
            for d in dates:
                user_universe.update((by_user_full.get(d, {}) or {}).keys())
            if role != "admin":
                user_universe = {username} if username else set()
            users: dict[str, dict[str, dict[str, list[int]]]] = {}
            for u in sorted(user_universe):
                per_app: dict[str, dict[str, list[int]]] = {}
                for idx, d in enumerate(dates):
                    day_user = (by_user_full.get(d, {}) or {}).get(u, {}) or {}
                    for app_name, stats in day_user.items():
                        slot = per_app.setdefault(app_name, {"images": [0] * len(dates), "seconds": [0] * len(dates), "tokens": [0] * len(dates)})
                        slot["images"][idx] = int(stats.get("images", 0) or 0)
                        slot["seconds"][idx] = int(stats.get("seconds", 0) or 0)
                        slot["tokens"][idx] = int(stats.get("tokens", 0) or 0)
                users[u] = per_app
        return {"dates": dates, "users": users}

    def export_range(self, start: str, end: str, app_names: list[str], username: str = "", role: str = "user") -> list[dict[str, Any]]:
        """Wide-format per-user totals for [start, end].
        Returns list[dict] where each dict has username + per-app images/seconds + totals.
        Order is sorted by username; non-admin sees only their own row.
        """
        dates = self._date_range(start, end)
        date_set = set(dates)
        with self._lock:
            by_user_full = self._data.get("by_user", {})
            user_universe: set[str] = set()
            for d in dates:
                user_universe.update((by_user_full.get(d, {}) or {}).keys())
            if role != "admin":
                user_universe = {username} if username else set()
            rows: list[dict[str, Any]] = []
            for u in sorted(user_universe):
                totals: dict[str, dict[str, int]] = {a: {"images": 0, "seconds": 0, "tokens": 0} for a in app_names}
                for d in dates:
                    day_user = (by_user_full.get(d, {}) or {}).get(u, {}) or {}
                    for app_name, stats in day_user.items():
                        slot = totals.setdefault(app_name, {"images": 0, "seconds": 0, "tokens": 0})
                        slot["images"] += int(stats.get("images", 0) or 0)
                        slot["seconds"] += int(stats.get("seconds", 0) or 0)
                        slot["tokens"] += int(stats.get("tokens", 0) or 0)
                row: dict[str, Any] = {"username": u}
                grand_images = 0
                grand_seconds = 0
                grand_tokens = 0
                for a in app_names:
                    row[f"{a}_images"] = totals[a]["images"]
                    row[f"{a}_seconds"] = totals[a]["seconds"]
                    row[f"{a}_tokens"] = totals[a]["tokens"]
                    grand_images += totals[a]["images"]
                    grand_seconds += totals[a]["seconds"]
                    grand_tokens += totals[a]["tokens"]
                # Include any extra apps that appeared in the window but aren't in app_names
                for a, v in totals.items():
                    if a in app_names: continue
                    row[f"{a}_images"] = v["images"]
                    row[f"{a}_seconds"] = v["seconds"]
                    row[f"{a}_tokens"] = v["tokens"]
                    grand_images += v["images"]
                    grand_seconds += v["seconds"]
                    grand_tokens += v["tokens"]
                row["total_images"] = grand_images
                row["total_seconds"] = grand_seconds
                row["total_tokens"] = grand_tokens
                rows.append(row)
        _ = date_set
        return rows


# ─── Singletons ────────────────────────────────────────────────────────────────

def _filter_entertaining_topics(topics: list[dict]) -> list[dict]:
    """Keep only meme-worthy, entertaining hot topics via DeepSeek.

    ``concepts.py`` already ran a coarse programmatic filter, so this pass only
    decides entertainment value. Missing key or model failure falls back to the
    already-filtered input instead of silently wiping today's trend pool.
    """
    if not topics:
        return topics
    key = _daily_report_module._load_deepseek_key(STATE_DIR)
    if not key:
        print("  [cat-trends] no DeepSeek key, skip AI entertainment filter", flush=True)
        return topics
    numbered = {index: str(topic.get("title") or "") for index, topic in enumerate(topics)}
    user_content = "以下是抖音热搜标题（带编号）：\n" + "\n".join(
        f"{index}. {title}" for index, title in numbered.items()
    )
    system_prompt = (
        "你是公司内部猫咪皮肤系统的幽默内容编辑，负责从抖音热搜里挑出适合做成搞笑/娱乐猫咪皮肤的话题。"
        "判断标准：\n"
        "- 保留：娱乐、体育、明星、网红、搞笑事件、网络梗、萌宠、情感共鸣、有调侃空间、大众能会心一笑的话题；\n"
        "- 排除：严肃时政、灾难、司法、军事、政策、宏观经济等不适合娱乐化的新闻，以及没有梗、没有记忆点的无聊日常。\n"
        "- 允许保留对名人或网络人物的娱乐化调侃，只要足够有梗、足够娱乐化，不要因为涉及逝者就一律排除。\n"
        "同时为每个保留的话题起一个简洁自然的中文猫名，要求 2 到 6 个汉字、以“猫”结尾、贴合话题。\n"
        "只输出 JSON：{\"keep\": [{\"index\": 编号, \"cat_name\": \"猫名\"}, ...]}，不要任何解释文字。"
    )
    try:
        resp = _daily_report_module._deepseek_chat(
            key,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=2600,
        )
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(content)
        keep = parsed.get("keep") or []
        name_by_index: dict[int, str] = {}
        for item in keep:
            if isinstance(item, dict):
                try:
                    name_by_index[int(item.get("index"))] = str(item.get("cat_name") or "")
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, (int, str)) and str(item).isdigit():
                name_by_index[int(item)] = ""
        kept = []
        for index, topic in enumerate(topics):
            if index in name_by_index:
                topic = dict(topic)
                topic["cat_name"] = name_by_index[index]
                kept.append(topic)
        if not kept:
            print("  [cat-trends] AI filter kept 0 topics; keeping programmatic result", flush=True)
            return topics
        print(f"  [cat-trends] AI entertainment filter kept {len(kept)}/{len(topics)}", flush=True)
        return kept
    except Exception as exc:
        print(f"  [cat-trends] AI entertainment filter failed: {exc}", flush=True)
        return topics


auth = AuthManager()
key_manager = KeyManager()
manager = AppManager()
tracker = UsageTracker()
cat_concept_store = CatConceptStore(CAT_CONCEPT_SEEDS_PATH, CAT_TRENDS_PATH, topic_filter=_filter_entertaining_topics)
cat_skin_generator = CatSkinGenerator(
    CAT_SKINS_DIR,
    key_loader=lambda: _daily_report_module._load_deepseek_key(STATE_DIR),
    concept_store=cat_concept_store,
)
_ACTIVITY_BACKFILL_APPS = {"seedance", "nano-banana", "volcengine-portrait"}
_ACTIVITY_BACKFILL_TIMEOUT = 1.2
_ACTIVITY_BACKFILL_MISS_TTL = 300.0
_ACTIVITY_BACKFILL_MISSES: dict[str, float] = {}
_ACTIVITY_METADATA_FIELDS = {
    "aspect_ratio", "ratio", "duration", "resolution", "image_size",
    "count", "mode", "style", "negative_prompt", "seed", "generate_audio",
    "custom_model", "model_name", "model_version",
}


def _coerce_epoch(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def _activity_request_values(record: dict) -> dict:
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    parsed = request.get("parsed") if isinstance(request.get("parsed"), dict) else {}
    values = parsed.get("values") if isinstance(parsed.get("values"), dict) else None
    if not values and isinstance(request.get("values"), dict):
        values = request["values"]
    if not values:
        restore = record.get("restore") if isinstance(record.get("restore"), dict) else {}
        values = restore.get("values") if isinstance(restore.get("values"), dict) else {}
    return values if isinstance(values, dict) else {}


def _activity_history_metadata(record: dict) -> dict:
    values = _activity_request_values(record)
    title = str(record.get("title") or values.get("prompt") or "").strip()
    prompt = str(values.get("prompt") or record.get("prompt") or title).strip()
    model = str(record.get("model") or values.get("custom_model") or values.get("model") or "").strip()
    params: dict[str, Any] = {}
    for key in _ACTIVITY_METADATA_FIELDS:
        value = values.get(key)
        if value not in (None, ""):
            params[key] = value
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    raw_results = result.get("results") if isinstance(result.get("results"), list) else []
    results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("download_url") or item.get("file_url")
        if url:
            results.append({"url": str(url), "kind": item.get("kind") or item.get("type") or "video"})
    finished_at = _coerce_epoch(record.get("finished_at") or result.get("finished_at"))
    return {
        "activity_id": str(record.get("id") or record.get("activity_id") or "").strip(),
        "title": title[:200],
        "prompt": prompt[:2000],
        "model": model[:200],
        "params": params,
        "results": results,
        "thumb_url": results[0]["url"] if results else "",
        "error": str(record.get("error") or result.get("error") or "").strip()[:500],
        "completed_at": finished_at,
    }


def _fetch_activity_history_record(app: str, job_id: str, username: str = "") -> dict | None:
    if app not in _ACTIVITY_BACKFILL_APPS or not job_id:
        return None
    cache_key = f"{app}:{job_id}"
    now = time.time()
    miss_at = _ACTIVITY_BACKFILL_MISSES.get(cache_key, 0.0)
    if miss_at and now - miss_at < _ACTIVITY_BACKFILL_MISS_TTL:
        return None
    spec = SPEC_BY_NAME.get(app)
    if spec is None:
        return None
    conn = None
    try:
        ts = int(time.time())
        username_encoded = urllib.parse.quote(username or "", safe="")
        headers = {
            "X-Is-Admin": "1",
            "X-Username": username_encoded,
            "X-Portal-Ts": str(ts),
            "X-Portal-Sig": _sign_admin_header(username_encoded, True, ts),
        }
        conn = http.client.HTTPConnection("127.0.0.1", spec.port, timeout=_ACTIVITY_BACKFILL_TIMEOUT)
        conn.request("GET", "/api/activity/by-job/" + urllib.parse.quote(job_id, safe=""), headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            _ACTIVITY_BACKFILL_MISSES[cache_key] = now
            return None
        data = json.loads(body.decode("utf-8", errors="replace"))
        # A persisted activity record may legitimately contain a task-level
        # ``error`` field (for example an upstream provider failure). Only a
        # missing record / explicit API failure should be cached as a miss.
        if (not isinstance(data, dict)
                or data.get("ok") is False
                or not data.get("id")):
            _ACTIVITY_BACKFILL_MISSES[cache_key] = now
            return None
        _ACTIVITY_BACKFILL_MISSES.pop(cache_key, None)
        return data
    except Exception:
        _ACTIVITY_BACKFILL_MISSES[cache_key] = now
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _history_record_needs_activity_backfill(record: dict) -> bool:
    return (
        str(record.get("app") or "") in _ACTIVITY_BACKFILL_APPS
        and bool(record.get("job_id"))
        and (
            not str(record.get("prompt") or "").strip()
            or not str(record.get("model") or "").strip()
            or not isinstance(record.get("params"), dict)
            or not record.get("params")
            or not record.get("activity_id")
        )
    )


def _backfill_history_record(record: dict) -> bool:
    if not _history_record_needs_activity_backfill(record):
        return False
    activity = _fetch_activity_history_record(
        str(record.get("app") or ""),
        str(record.get("job_id") or ""),
        str(record.get("username") or ""),
    )
    if not activity:
        return False
    meta = _activity_history_metadata(activity)
    changed = False
    for key in ("activity_id", "model", "completed_at"):
        value = meta.get(key)
        if value not in (None, "", 0.0) and not record.get(key):
            record[key] = value
            changed = True
    if meta.get("prompt") and not str(record.get("prompt") or "").strip():
        record["prompt"] = meta["prompt"]
        changed = True
    if meta.get("title") and not str(record.get("title") or "").strip():
        record["title"] = meta["title"]
        changed = True
    if meta.get("params") and not record.get("params"):
        record["params"] = meta["params"]
        changed = True
    if meta.get("results") and not record.get("results"):
        record["results"] = meta["results"]
        record["thumb_url"] = meta.get("thumb_url") or ""
        changed = True
    if meta.get("error") and not str(record.get("error") or "").strip():
        record["error"] = meta["error"]
        changed = True
    return changed

def _fetch_feishu_history(user_id: str, username: str, limit: int = 200) -> list[dict]:
    """Read one Portal user's independently persisted Feishu Agent runs."""
    spec = SPEC_BY_NAME.get("feishu-generation-agent")
    if spec is None:
        return []
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", spec.port, timeout=5)
        headers = {
            "X-Portal-User-Id": str(user_id),
            "X-Username": urllib.parse.quote(username, safe=""),
        }
        conn.request(
            "GET", f"/api/portal/history?limit={max(1, min(int(limit), 200))}",
            headers=headers,
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read()) if resp.status == 200 else {}
        return [item for item in payload.get("items", []) if isinstance(item, dict)]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _feishu_daily_task_completed(user: dict, today: str) -> bool:
    # Feishu Agent owns its run history in a separate SQLite database, so it
    # cannot be inferred from the other generators' /api/jobs records.
    for record in _fetch_feishu_history(
        str(user["user_id"]), user.get("username", ""), limit=200,
    ):
        if record.get("status") != "done":
            continue
        try:
            completed = datetime.fromtimestamp(
                float(record.get("completed_at") or 0)
            ).astimezone()
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if completed.date().isoformat() == today:
            return True
    return False


cat_skin_manager = CatSkinManager(
    CAT_SKINS_PATH, CAT_CLASSIC_PATH, cat_skin_generator,
    task_completed_fn=_feishu_daily_task_completed,
)
cat_experiment_service = CatExperimentService(CAT_SKINS_DIR, cat_skin_generator)


# ─── HTTP Handler ──────────────────────────────────────────────────────────────

_AUTH_EXEMPT = {"/login", "/healthz", "/api/auth/login", "/api/auth/register"}
# 登录页在认证前就需要 UI Core。这里只公开无业务数据的共享样式目录；
# 其他 Portal 静态资源仍然经过认证，避免意外扩大匿名访问范围。
_AUTH_EXEMPT_PREFIXES = ("/ui/",)


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def setup(self):
        super().setup()
        # TLS 握手必须在每个连接自己的线程里完成并限时：监听 socket 以
        # do_handshake_on_connect=False 包装后，握手推迟到这里。此前握手
        # 在主线程 accept() 里同步进行且无超时——一个半开连接（连上后
        # 不发字节的扫描器/休眠的浏览器）就能把整个 Portal 卡死、所有
        # 客户端挂起（2026-09-02 断服实锤，main thread 100% 卡在
        # _ssl__SSLSocket_do_handshake_impl 的 sock_read）。
        conn = getattr(self, "connection", None)
        if conn is None or not hasattr(conn, "do_handshake"):
            return
        conn.settimeout(10)
        try:
            conn.do_handshake()
        except socket.timeout:
            # 半开连接：连上后 10s 不发握手字节（2026-09-02 断服元凶姿势）。
            # 记下来源 IP——下次再出现可以直接锁定是哪台机器/哪个扫描器。
            print(f"  [tls-timeout] handshake timeout from {self.client_address[0]}", flush=True)
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        except (ssl.SSLError, OSError, ValueError):
            # 扫描器/坏客户端：安静关掉，随后的 readline 读到空行，
            # handle_one_request 自行收尾，不产生 traceback 噪音。
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        finally:
            try:
                conn.settimeout(None)
            except OSError:
                pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _reject_oversized_upload(self) -> bool:
        raw = self.headers.get("Content-Length")
        if not raw:
            return False
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return False
        if n > MAX_UPLOAD_BYTES:
            body = json.dumps({
                "ok": False,
                "error": f"upload too large: {n} bytes (limit {MAX_UPLOAD_BYTES})",
            }).encode("utf-8")
            self.send_response(413)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return True
        return False

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError) as e:
            # The client hung up mid-exchange. Routine browser noise on GET,
            # but an aborted POST is exactly what silently ate an upload and
            # left no trace anywhere, so state-changing methods get a line.
            method = getattr(self, "command", "")
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                print(f"  [CONN RESET] {method} {getattr(self, 'path', '-')} "
                      f"ip={self.client_address[0]} {type(e).__name__}", flush=True)
        except ssl.SSLError as e:
            print(f"  [SSL ERROR] {e} (ip={self.client_address[0]} path={getattr(self, 'path', '-')})", flush=True)
        except OSError as e:
            print(f"  [OS ERROR] {e} (ip={self.client_address[0]} path={getattr(self, 'path', '-')})", flush=True)

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _current_user(self) -> dict | None:
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith("session="):
                user = auth.get_user(part[8:].strip())
                if user:
                    return user
        token = (self.headers.get("X-Session") or "").strip()
        if not token:
            authz = self.headers.get("Authorization") or ""
            if authz.lower().startswith("bearer "):
                token = authz[7:].strip()
        if token:
            return auth.get_user(token)
        return None

    def _is_https(self) -> bool:
        proto = self.headers.get("X-Forwarded-Proto", "")
        if proto == "https":
            return True
        try:
            return bool(self.connection.context)
        except Exception:
            return False

    def _set_cookie(self, token: str):
        cookie = f"session={token}; Path=/; Max-Age={SESSION_MAX_AGE}; HttpOnly; SameSite=Lax"
        if self._is_https():
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)

    def _clear_cookie(self):
        self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly")

    def _require_auth(self, path: str) -> dict | None:
        """Return user dict or send error/redirect and return None."""
        user = self._current_user()
        if user:
            return user
        # Every API surface answers JSON 401, never a redirect. Sub-app paths
        # such as /seedance/api/media/upload do not start with /api/, so the
        # old startswith("/api/") test sent them a 302 to the login page —
        # XHR/fetch callers follow it, receive HTML, and their JSON parse
        # fails, which surfaced as a bogus "server rejected the file" error.
        if path.startswith("/api/") or "/api/" in path:
            self._json(401, {"ok": False, "error": "unauthorized"})
        elif path == "/login":
            # Never chain /login -> /login?next=...: each hop would re-encode
            # the previous next value, growing it exponentially (observed as
            # an unbounded redirect loop on POST /login).
            self._redirect("/login")
        else:
            next_url = urllib.parse.quote(self.path, safe="")
            self._redirect(f"/login?next={next_url}")
        return None

    # ── Request dispatch ──────────────────────────────────────────────────────

    def _log_req(self, method: str):
        try:
            cookies = self.headers.get("Cookie") or ""
            token = ""
            for part in cookies.split(";"):
                part = part.strip()
                if part.startswith("session="):
                    token = part[8:].strip()
                    break
            valid = bool(auth.get_user(token)) if token else False
            x = (self.headers.get("X-Session") or "").strip()
            ua = (self.headers.get("User-Agent") or "")[:50]
            tok = (token[:8] + "..") if token else ""
            print(f"  [req] {method} {self.path} ip={self.client_address[0]} tok={tok} valid={valid} xsess={'Y' if x else 'N'} ua={ua}", flush=True)
        except Exception:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        self._log_req("GET")
        if path == "/healthz":
            self._json(200, {"ok": True, "service": "portal"})
            return
        if path == "/api/auth/first-run":
            self._json(200, {"ok": True, "first_run": auth.first_run(), "signup_enabled": auth.signup_enabled()})
            return
        if path in _AUTH_EXEMPT or path == "/login" or path.startswith(_AUTH_EXEMPT_PREFIXES):
            self._serve_portal(path if path != "/login" else "/login.html")
            return
        user = self._require_auth(path)
        if not user:
            return
        # 「中心」转发（2026-10-14 用户口径）：本机配了 center 就说明「统一存放点是另一台」，
        # 使用统计的所有查询直接转发过去 —— 两台机器看到同一份数据。
        # 只对管理员转发（普通用户看本机自己的记录，避免越权看到别人）；中心不可达则回退本机。
        if path.startswith("/api/platform/stats") or path == "/api/platform/remote-sources":
            if str(user.get("role") or "") == "admin" and self._forward_stats_to_center():
                return
        if path == "/api/platform/status":
            self._platform_status(user)
        elif path == "/api/platform/stats":
            self._platform_stats(user)
        elif path == "/api/platform/stats/history":
            self._platform_stats_history(user)
        elif path == "/api/platform/stats/day":
            self._platform_stats_day(user)
        elif path == "/api/platform/stats/range":
            self._platform_stats_range(user)
        elif path == "/api/platform/stats/export":
            self._platform_stats_export(user)
        elif path == "/api/platform/stats/generations":
            self._platform_stats_generations(user)
        elif path == "/api/platform/stats/generations/export":
            self._platform_stats_generations_export(user)
        elif path == "/api/platform/stats/generations/export.xlsx":
            self._platform_stats_generations_export_xlsx(user)
        elif path == "/api/platform/remote-sources":
            self._remote_sources_status(user)
        elif path == "/api/platform/stats/generations/summary.csv":
            self._platform_stats_generations_summary_export(user)
        elif path == "/api/platform/queue":
            self._platform_queue(user)
        elif path == "/api/platform/activity":
            self._platform_activity(user)
        # 注意顺序：history-users 必须先于 startswith("/api/platform/history")
        # 判断，否则被前缀匹配吞掉，前端用户下拉永远只有「全部用户」。
        elif path == "/api/platform/history-users":
            self._platform_history_users(user)
        elif path == "/api/platform/history/events":
            self._platform_history_events(user)
        elif path.startswith("/api/platform/history"):
            self._platform_history(user)
        elif path == "/api/video-check":
            self._platform_video_check(user)
        elif path == "/api/platform/me":
            self._json(200, {"ok": True, "username": user["username"], "role": user["role"]})
        elif path == "/api/platform/thumb":
            self._platform_thumb(user)
        elif self._try_company_key_route(path, "GET", user):
            pass
        elif path == "/api/feishu/config":
            self._feishu_config_get(user)
        elif path.startswith("/api/reports/daily/") and path.endswith(".csv"):
            date = path[len("/api/reports/daily/"):-len(".csv")]
            self._report_csv_download(user, date)
        elif path == "/api/auth/me":
            self._json(200, {"ok": True, "username": user["username"], "role": user["role"]})
        elif path == "/api/cat/wardrobe":
            self._json(200, cat_skin_manager.wardrobe(user))
        elif path == "/api/cat/experiment/config":
            self._cat_experiment_config(user)
        elif path == "/api/cat/concepts/status":
            self._cat_concepts_status(user)
        elif path == "/api/users":
            if not auth.has_permission(user, "manage_users"):
                self._json(403, {"ok": False, "error": "forbidden"})
            else:
                self._json(200, {"ok": True, "users": auth.list_users()})
        elif path.startswith("/api/keys/") and path.endswith("/reveal"):
            key_id = path[len("/api/keys/"):-len("/reveal")]
            value = key_manager.resolve(user["user_id"], key_id)
            if value is None:
                self._json(404, {"ok": False, "error": "key not found"})
            else:
                self._json(200, {"ok": True, "key": value})
        elif path.startswith("/api/keys"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            provider = qs.get("provider", [None])[0]
            self._json(200, {"ok": True, "keys": key_manager.list_keys(user["user_id"], provider)})
        elif path == "/api/apps":
            self._apps_meta()
        elif self._try_proxy(path, "GET", user):
            pass
        else:
            self._serve_portal(path)

    def _analytics_event(self, user: dict):
        body = self._read_json()
        if body is None:
            return
        event = str(body.get("event") or "").strip()
        if not event:
            self._json(400, {"ok": False, "error": "event is required"})
            return
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        entry = {
            "ts": int(time.time()),
            "username": user.get("username", ""),
            "user_id": user.get("user_id", ""),
            "event": event,
            "payload": payload,
        }
        _append_analytics_jsonl(entry)
        self._json(200, {"ok": True})

    def do_POST(self):
        if self._reject_oversized_upload():
            return
        path = urllib.parse.urlparse(self.path).path
        self._log_req("POST")
        if path == "/api/auth/login":
            self._auth_login()
            return
        if path == "/api/auth/register":
            self._auth_register()
            return
        if path == "/auth/login":
            self._auth_form_login()
            return
        if path == "/auth/register":
            self._auth_form_register()
            return
        if path == "/api/internal/jobs/finalize":
            self._internal_finalize_job()
            return
        user = self._require_auth(path)
        if not user:
            return
        if path == "/api/platform/remote-sources/sync":
            # 「立刻同步一次」是动作 → 只在 POST 上暴露（GET 上暴露会被 <img>/预取误触发）
            self._remote_sources_sync(user)
            return
        if path == "/api/auth/logout":
            token = ""
            for part in self.headers.get("Cookie", "").split(";"):
                p = part.strip()
                if p.startswith("session="):
                    token = p[8:].strip()
            auth.logout(token)
            self.send_response(200)
            self._clear_cookie()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "12")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if path == "/api/cat/open":
            self._cat_open(user)
            return
        if path == "/api/cat/experiment/generate":
            self._cat_experiment_generate(user)
            return
        if path == "/api/cat/concepts/refresh":
            self._cat_concepts_refresh(user)
            return
        if path == "/api/cat/equip":
            self._cat_equip(user)
            return
        if path == "/api/cat/release":
            self._cat_release(user)
            return
        if path == "/api/auth/create-user":
            if not auth.has_permission(user, "manage_users"):
                self._json(403, {"ok": False, "error": "forbidden"})
                return
            body = self._read_json()
            if body is None:
                return
            u = auth.create_user(body.get("username", ""), body.get("password", ""), body.get("role", "user"))
            if u:
                self._json(200, {"ok": True, "user": {"id": u["id"], "username": u["username"], "role": u["role"]}})
            else:
                self._json(409, {"ok": False, "error": "username already exists"})
            return
        if path == "/api/auth/signup-toggle":
            if not auth.has_permission(user, "manage_users"):
                self._json(403, {"ok": False, "error": "forbidden"})
                return
            body = self._read_json()
            if body is None:
                return
            auth.set_signup_enabled(bool(body.get("enabled", True)))
            self._json(200, {"ok": True, "signup_enabled": auth.signup_enabled()})
            return
        if path.startswith("/api/users/") and auth.has_permission(user, "manage_users"):
            self._update_user(path)
            return
        if path == "/api/keys":
            body = self._read_json()
            if body is None:
                return
            try:
                entry = key_manager.add_key(
                    user["user_id"], body.get("name", ""), body.get("provider", "other"),
                    body.get("key", ""), body.get("note", ""),
                )
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "key": entry})
            return
        if self._try_company_key_route(path, "POST", user):
            return
        if path == "/api/reports/send":
            self._report_send(user)
            return
        if path == "/api/reports/preview":
            self._report_preview(user)
            return
        if path == "/api/feishu/config":
            self._feishu_config_put(user)
            return
        if path == "/api/analytics/event":
            self._analytics_event(user)
            return
        if not self._try_proxy(path, "POST", user):
            self._json(404, {"ok": False, "error": "not found"})

    def do_PUT(self):
        if self._reject_oversized_upload():
            return
        path = urllib.parse.urlparse(self.path).path
        user = self._require_auth(path)
        if not user:
            return
        if not self._try_proxy(path, "PUT", user):
            self._json(404, {"ok": False, "error": "not found"})

    def do_PATCH(self):
        if self._reject_oversized_upload():
            return
        path = urllib.parse.urlparse(self.path).path
        user = self._require_auth(path)
        if not user:
            return
        if not self._try_proxy(path, "PATCH", user):
            self._json(404, {"ok": False, "error": "not found"})

    def do_DELETE(self):
        if self._reject_oversized_upload():
            return
        path = urllib.parse.urlparse(self.path).path
        user = self._require_auth(path)
        if not user:
            return
        if path.startswith("/api/keys/"):
            key_id = path[len("/api/keys/"):]
            if key_manager.delete_key(user["user_id"], key_id):
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._try_proxy(path, "DELETE", user):
            self._json(404, {"ok": False, "error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── Auth endpoint handlers ────────────────────────────────────────────────

    def _cat_open(self, user: dict):
        try:
            skin = cat_skin_manager.open_gift(user)
        except CatDailyTaskRequiredError as exc:
            self._json(409, {"ok": False, "code": "daily_task_required", "error": str(exc)})
            return
        except CatDailyLimitError as exc:
            self._json(409, {"ok": False, "code": "daily_limit", "error": str(exc)})
            return
        except CatGenerationBusyError as exc:
            self._json(409, {"ok": False, "code": "generating", "error": str(exc)})
            return
        except CatGenerationError as exc:
            print(f"  [cat-skin] generation failed for {user['username']}: {exc}", flush=True)
            self._json(502, {"ok": False, "code": "generation_failed", "error": "猫咪生成失败，本次机会未消耗，请稍后重试"})
            return
        except Exception as exc:
            print(f"  [cat-skin] unexpected error for {user['username']}: {exc}", flush=True)
            self._json(500, {"ok": False, "code": "generation_failed", "error": "猫咪生成失败，本次机会未消耗"})
            return
        wardrobe = cat_skin_manager.wardrobe(user)
        self._json(200, {
            "ok": True, "skin": skin, "can_open": wardrobe["can_open"],
            "equipped_skin_id": skin["id"],
            "opens_today": wardrobe.get("opens_today", 0),
            "daily_limit": wardrobe.get("daily_limit"),
            "daily_task": wardrobe.get("daily_task", {}),
        })

    def _cat_concepts_status(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "管理员才能查看猫咪热点状态"})
            return
        self._json(200, {"ok": True, **cat_concept_store.status()})

    def _cat_concepts_refresh(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "管理员才能刷新猫咪热点"})
            return
        result = cat_concept_store.refresh()
        self._json(200 if result.get("ok") else 502, result)

    def _cat_experiment_config(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "管理员才能使用猫咪生成实验台"})
            return
        self._json(200, cat_experiment_service.config())

    def _cat_experiment_generate(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "管理员才能使用猫咪生成实验台"})
            return
        body = self._read_json()
        if body is None:
            return
        rarity = str(body.get("rarity") or "common").strip()
        name = str(body.get("name") or "").strip()
        raw_seed = body.get("seed")
        try:
            seed = int(raw_seed) if raw_seed not in (None, "") else None
            result = cat_experiment_service.generate(rarity, name, seed)
        except KeyError as exc:
            self._json(400, {"ok": False, "error": str(exc).strip("'")})
            return
        except (TypeError, ValueError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        except RuntimeError as exc:
            self._json(503, {"ok": False, "error": str(exc)})
            return
        if not result["validation"]["passed"]:
            self._json(422, result)
            return
        self._json(200, result)

    def _cat_equip(self, user: dict):
        body = self._read_json()
        if body is None:
            return
        skin_id = str(body.get("skin_id") or "").strip()
        if not skin_id:
            self._json(400, {"ok": False, "error": "skin_id required"})
            return
        try:
            result = cat_skin_manager.equip(user, skin_id)
        except KeyError as exc:
            self._json(404, {"ok": False, "error": str(exc).strip("'")})
            return
        self._json(200, result)

    def _cat_release(self, user: dict):
        body = self._read_json()
        if body is None:
            return
        skin_id = str(body.get("skin_id") or "").strip()
        if not skin_id:
            self._json(400, {"ok": False, "error": "skin_id required"})
            return
        try:
            result = cat_skin_manager.release(user, skin_id)
        except PermissionError as exc:
            self._json(403, {"ok": False, "error": str(exc)})
            return
        except KeyError as exc:
            self._json(404, {"ok": False, "error": str(exc).strip("'")})
            return
        self._json(200, result)

    def _auth_login(self):
        body = self._read_json()
        if body is None:
            return
        token = auth.login(body.get("username", ""), body.get("password", ""))
        if not token:
            self._json(401, {"ok": False, "error": "invalid credentials"})
            return
        user = auth.get_user(token)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._set_cookie(token)
        self._cors_headers()
        raw = json.dumps({"ok": True, "username": user["username"], "role": user["role"], "token": token}).encode()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth_register(self):
        # First user always allowed (becomes admin); subsequent registrations
        # require signup_enabled flag (default true, admin can toggle off).
        first_run = auth.first_run()
        if not first_run and not auth.signup_enabled():
            self._json(403, {"ok": False, "error": "registration disabled"})
            return
        body = self._read_json()
        if body is None:
            return
        username = body.get("username", "").strip()
        pw = body.get("password", "")
        if not username or not pw or len(pw) < 6:
            self._json(400, {"ok": False, "error": "username required, password min 6 chars"})
            return
        role = "admin" if first_run else "user"
        user = auth.create_user(username, pw, role=role)
        if not user:
            self._json(409, {"ok": False, "error": "username already exists"})
            return
        token = auth.login(username, pw)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._set_cookie(token)
        self._cors_headers()
        raw = json.dumps({"ok": True, "username": username, "role": role, "token": token}).encode()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _safe_next(self, value: str) -> str:
        v = (value or "").strip()
        if not v or not v.startswith("/") or v.startswith("//"):
            return "/"
        return v

    def _read_form(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            pairs = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            return {k: v[0] for k, v in pairs.items()}
        except Exception:
            return None

    def _auth_form_login(self):
        body = self._read_form()
        if body is None:
            self._redirect("/login?error=bad_request")
            return
        username = (body.get("username") or "").strip()
        pw = body.get("password") or ""
        nxt = self._safe_next(body.get("next") or "/")
        token = auth.login(username, pw)
        if not token:
            self._redirect(f"/login?error=invalid_credentials&mode=login&next={urllib.parse.quote(nxt, safe='')}")
            return
        self._redirect(nxt, set_cookie_token=token)

    def _auth_form_register(self):
        body = self._read_form()
        if body is None:
            self._redirect("/login?error=bad_request")
            return
        username = (body.get("username") or "").strip()
        pw = body.get("password") or ""
        nxt = self._safe_next(body.get("next") or "/")
        first_run = auth.first_run()
        if not first_run and not auth.signup_enabled():
            self._redirect(f"/login?error=registration_disabled&mode=register&next={urllib.parse.quote(nxt, safe='')}")
            return
        if not username or not pw or len(pw) < 6:
            mode = "setup" if first_run else "register"
            self._redirect(f"/login?error=password_min_6&mode={mode}&next={urllib.parse.quote(nxt, safe='')}")
            return
        role = "admin" if first_run else "user"
        user = auth.create_user(username, pw, role=role)
        if not user:
            mode = "setup" if first_run else "register"
            self._redirect(f"/login?error=already_exists&mode={mode}&next={urllib.parse.quote(nxt, safe='')}")
            return
        token = auth.login(username, pw)
        self._redirect(nxt, set_cookie_token=token)

    def _update_user(self, path: str):
        # PATCH-like via POST /api/users/<id>
        user_id = path[len("/api/users/"):]
        body = self._read_json()
        if body is None:
            return
        if "password" in body:
            auth.reset_password(user_id, body["password"])
        updates = {k: body[k] for k in ("role", "enabled") if k in body}
        if updates:
            auth.update_user(user_id, **updates)
        self._json(200, {"ok": True})

    # ── Platform endpoints ────────────────────────────────────────────────────

    def _platform_status(self, user: dict):
        lan_ip = get_lan_ip()
        # 磁盘剩余空间（GB），供前端横幅预警；探测失败时为 None（前端忽略）。
        disk_free_gb = None
        try:
            disk_free_gb = round(shutil.disk_usage(str(STATE_DIR)).free / 1024**3, 1)
        except Exception:
            pass
        apps_info = []
        for name, config in APPS.items():
            info = {"name": name, "port": config["port"], **manager.status.get(name, {"status": "unknown"})}
            info["url"] = f"/{name}/"
            apps_info.append(info)
        self._json(200, {"ok": True, "lan_ip": lan_ip, "portal_port": PORTAL_PORT,
                         "disk_free_gb": disk_free_gb, "local_gateway": local_gateway.snapshot(timeout=0.8), "apps": apps_info})

    def _forward_stats_to_center(self) -> bool:
        """把当前这次「使用统计」查询转发到中心，成功返回 True（响应已写完）。

        - 本机就是中心（URL 指向自己）→ 返回 False，绝不自己转给自己（会死循环）；
        - 任何失败都返回 False → 调用方继续走本机数据（中心挂了不能让人什么都看不到）。
        """
        try:
            cfg = remote_sources.center_config()
        except Exception:  # noqa: BLE001
            return False
        if not cfg:
            return False
        host = (urllib.parse.urlsplit(str(cfg.get("base_url") or "")).hostname or "").lower()
        if not host:
            return False
        local = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
        try:
            local.add(str(get_lan_ip()).lower())
        except Exception:  # noqa: BLE001
            pass
        try:
            local.add(socket.gethostname().lower())
        except Exception:  # noqa: BLE001
            pass
        if host in local:
            return False                     # 中心就是本机 → 按本机数据处理
        parts = urllib.parse.urlsplit(self.path)
        target = parts.path + (("?" + parts.query) if parts.query else "")
        try:
            status, body, ctype = remote_sources.center_request(target)
        except Exception as exc:  # noqa: BLE001
            print(f"  [center] forward failed: {str(exc)[:160]}", flush=True)
            return False
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype or "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Stats-From", "center")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return True

    def _platform_stats(self, user: dict):
        stats = tracker.get_stats(username=user["username"], role=user["role"])
        self._json(200, {"ok": True, **stats})

    def _platform_stats_history(self, user: dict):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            days = int(qs.get("days", ["30"])[0])
        except (TypeError, ValueError):
            days = 30
        history = tracker.get_history(days=days, username=user["username"], role=user["role"])
        self._json(200, {"ok": True, **history})

    def _platform_stats_day(self, user: dict):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        date = (qs.get("date", [""])[0] or time.strftime("%Y-%m-%d")).strip()
        # Basic shape sanity (YYYY-MM-DD, 10 chars). Reject anything else.
        if len(date) != 10 or date[4] != "-" or date[7] != "-":
            self._json(400, {"ok": False, "error": "invalid date"})
            return
        day = tracker.get_day(date, username=user["username"], role=user["role"])
        self._json(200, {"ok": True, **day})

    def _parse_range_qs(self) -> tuple[str, str] | None:
        """Read & validate ?start=&end=. Defaults to last 30 days."""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        end = (qs.get("end", [""])[0] or time.strftime("%Y-%m-%d")).strip()
        default_start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 29 * 86400))
        start = (qs.get("start", [""])[0] or default_start).strip()
        if not UsageTracker._validate_date(start) or not UsageTracker._validate_date(end):
            self._json(400, {"ok": False, "error": "invalid date format (expect YYYY-MM-DD)"})
            return None
        return start, end

    def _platform_stats_range(self, user: dict):
        rng = self._parse_range_qs()
        if not rng:
            return
        start, end = rng
        data = tracker.get_range(start, end, username=user["username"], role=user["role"])
        self._json(200, {"ok": True, "start": start, "end": end, **data})

    def _platform_stats_export(self, user: dict):
        rng = self._parse_range_qs()
        if not rng:
            return
        start, end = rng
        # Wide-format: one row per user, columns per app + totals
        app_names = list(APPS.keys())
        rows = tracker.export_range(start, end, app_names, username=user["username"], role=user["role"])
        # Build CSV with UTF-8 BOM so Excel/WPS double-click renders Chinese correctly.
        buf = io.StringIO()
        # Header row order: username, <app>_images, <app>_seconds, <app>_tokens,
        # total_images, total_seconds, total_tokens
        fieldnames = ["username"]
        for a in app_names:
            fieldnames.append(f"{a}_images")
            fieldnames.append(f"{a}_seconds")
            fieldnames.append(f"{a}_tokens")
        # Tail: any extra apps that may have appeared but aren't in APPS list (defensive)
        extra_keys: list[str] = []
        for r in rows:
            for k in r.keys():
                if k in fieldnames or k in ("total_images", "total_seconds", "total_tokens"): continue
                if k not in extra_keys: extra_keys.append(k)
        for k in extra_keys:
            fieldnames.append(k)
        fieldnames.extend(["total_images", "total_seconds", "total_tokens"])
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        body = ("﻿" + buf.getvalue()).encode("utf-8")
        filename = f"usage-{start}-{end}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _generation_query(self) -> tuple[str, str] | None:
        """读 ?start=&end=（默认最近 7 天）。非法返回 None 并已回 400。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        end = (qs.get("end", [""])[0] or time.strftime("%Y-%m-%d")).strip()
        default_start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
        start = (qs.get("start", [""])[0] or default_start).strip()
        if not UsageTracker._validate_date(start) or not UsageTracker._validate_date(end):
            self._json(400, {"ok": False, "error": "invalid date format (expect YYYY-MM-DD)"})
            return None
        return start, end

    @staticmethod
    def _generation_summary(rows: list[dict]) -> dict:
        priced = [r for r in rows if isinstance(r.get("amount"), (int, float))]
        currency = str((priced[0].get("currency") if priced else "CNY") or "CNY")
        return {
            "count": len(rows),
            "tokens": sum(int(r.get("tokens") or 0) for r in rows),
            "amount": round(sum(float(r.get("amount") or 0) for r in priced), 4),
            "priced_count": len(priced),
            "unpriced_count": len(rows) - len(priced),
            "currency": currency,
            "users": len({str(r.get("username") or "") for r in rows if r.get("username")}),
            # 按应用计数：页面上一眼看出「飞书有没有被记上」——以前只有总数，
            # 飞书任务被 seedance 挤到后面几页时会以为没记录。
            "apps": dict(collections.Counter(
                str(r.get("app") or "") for r in rows if r.get("app"))),
        }
    def _feishu_generation_rows(self, start: str, end: str, is_admin: bool,
                                username: str) -> list[dict]:
        """把飞书任务作为「使用记录」的一部分（**查询时合并**，不落盘）。

        为什么不靠代理登记：实测 2026-10-12 飞书当天 35 个 run **没有一个经过 Portal 的
        HTTP 代理**（日志里只有 `POST /api/runs/<id>/decision` 这类子操作，没有
        `POST /api/runs`）—— 这些 run 由飞书多维表格/Agent 自己创建。只有向飞书 Agent
        拉取它的运行记录才看得见（Portal 的「创作记录」本来就是这么合并的）。

        代价：每个 Portal 用户一次本地调用（飞书侧是本地 SQLite 查询），管理员视角最多
        十几次；这条路径本来就在「创作记录」每次加载时跑，属于既有模式。
        """
        targets: list[tuple[str, str]] = []
        try:
            users = [u for u in auth.list_users() if u.get("enabled", True)]
        except Exception:  # noqa: BLE001
            users = []
        if is_admin:
            targets = [(str(u.get("id") or ""), str(u.get("username") or "")) for u in users]
        else:
            me = next((u for u in users if str(u.get("username")) == username), None)
            if me is not None:
                targets = [(str(me.get("id") or ""), username)]
        rows: list[dict] = []
        for uid, uname in targets:
            for rec in _fetch_feishu_history(uid, uname, limit=200):
                try:
                    ts = float(rec.get("submitted_at") or 0)
                except (TypeError, ValueError):
                    continue
                if ts <= 0:
                    continue
                day = time.strftime("%Y-%m-%d", time.localtime(ts))
                if day < start or day > end:
                    continue
                status = str(rec.get("status") or "").lower()
                results = rec.get("results") or []
                if status in _FEISHU_PRE_GENERATION:
                    continue      # 还没生成（等审批 / 排队 / 正在跑）
                if status in _FEISHU_FAILED_NO_OUTPUT and not results:
                    continue      # 没有产物的失败 = 没有发生有效生成
                rows.append({
                    "ts": ts, "date": day,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                    "username": uname, "app": "feishu-generation-agent",
                    "job_id": str(rec.get("job_id") or ""),
                    "task_ids": [], "kind": str(rec.get("kind") or "video"),
                    "model": str(rec.get("model") or ""),
                    "provider": "", "provider_label": "飞书 Agent（自带 key）",
                    "provider_detail": "飞书任务由飞书 Agent 自己的 key 发起（方舟 key）",
                    "title": str(rec.get("prompt") or "")[:200],
                    "resolution": "", "has_input_video": False, "media_fields": [],
                    "runs": 1, "images": 0, "seconds": 0,
                    "tokens": 0, "unit_price": None, "unit": "",
                    "amount": None, "currency": "CNY",
                    "price_source": "untracked", "price_family": "",
                    "status": feishu_usage_status(status, bool(results)),
                    "source_status": str(rec.get("status") or ""),   # 飞书原始工作流状态，仅用于排查
                    "tracked": False,
                    # 飞书 Agent 存着方舟任务号：拿它去方舟换真实 usage（token / 档位 / 金额）
                    "provider_task_ids": [str(t) for t in (rec.get("provider_task_ids") or [])][:40],
                    "results": rec.get("results") or [],
                    "thumb_url": str(rec.get("thumb_url") or ""),
                    "note": "飞书任务：取自飞书 Agent 自身的运行记录",
                })
        try:
            _apply_ark_usage(rows)
        except Exception as exc:  # noqa: BLE001
            print(f"  [usage] ark enrich failed: {exc}", flush=True)
        return rows

    def _history_generation_rows(self, start: str, end: str) -> list[dict]:
        """把「创作记录」（history.json）里、**明细里没有的**历史生成，也补进使用记录。

        为什么：明细文件（generations.jsonl）是 10-12 才有的，之前每一次生成都没有明细行，
        用户看到的是「很多生成没记录」。而 history.json 里恢复出了完整的时间/用户/应用/产物
        （9/11 起 2639 条），把它们并进来才叫「所有生成都有记录」。

        **准确优先**：这些历史记录当时**没有做用量统计**，所以 tokens/金额一律留空并写明原因，
        绝不按模型或时间去猜一个数字填进去。
        """
        rows: list[dict] = []
        try:
            history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return rows
        for rec in history.values():
            if not isinstance(rec, dict):
                continue
            try:
                ts = float(rec.get("submitted_at") or 0)
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            if day < start or day > end:
                continue
            app = str(rec.get("app") or "")
            model = str(rec.get("model") or "")
            provider = str(rec.get("provider") or "")
            rows.append({
                "ts": ts, "date": day,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                "username": str(rec.get("username") or ""), "app": app,
                "job_id": str(rec.get("job_id") or ""),
                "task_ids": list(rec.get("task_ids") or [])[:20],
                "kind": str(rec.get("kind") or "image"), "model": model,
                "provider": provider,
                "provider_label": provider_label(app, model, provider),
                "provider_detail": provider_detail(app, model, provider),
                "title": str(rec.get("title") or rec.get("prompt") or "")[:200],
                "resolution": str(rec.get("resolution") or ""),
                "has_input_video": bool(rec.get("has_input_video")), "media_fields": [],
                "runs": int(rec.get("runs") or 0), "images": int(rec.get("images") or 0),
                "seconds": int(rec.get("seconds") or 0),
                # 当时没有用量统计 → 留空（不是 0，0 会被当成「确实没用量」）
                "tokens": int(rec.get("tokens") or 0),
                "unit_price": None, "unit": "", "amount": None,
                "currency": "CNY", "price_source": "history_only", "price_family": "",
                "status": str(rec.get("status") or ""), "tracked": False,
                "results": rec.get("results") or [],
                "thumb_url": str(rec.get("thumb_url") or ""),
                "note": "历史记录（当时未做用量统计）：时间/用户/应用/产物准确，用量与金额留空",
                "_recovered_from": str(rec.get("_recovered_from") or "history"),
            })
        return rows

    def _generation_rows_merged(self, start: str, end: str, user: dict, is_admin: bool,
                                filters: dict) -> list[dict]:
        """我方明细 + 飞书任务（查询时合并），并套用页面的筛选条件。"""
        ours, _ = read_generation_records(
            start, end, username=user.get("username", ""), is_admin=is_admin,
            limit=20000, offset=0, **filters)
        have = {str(r.get("job_id") or "") for r in ours if r.get("job_id")}
        extra = [r for r in self._feishu_generation_rows(start, end, is_admin, user.get("username", ""))
                 if str(r.get("job_id") or "") not in have]
        # 另一台同款平台的记录（remote_sources 定时拉取，落 state/remote_generations.jsonl）。
        # 用 (来源 + 任务号) 去重：两边的 job_id 空间互不相干，绝不能只按 job_id 去重。
        try:
            remote_rows: list[dict] = []
            for row in remote_sources.all_remote_rows():
                day = str(row.get("date") or "")
                if day and (day < start or day > end):
                    continue
                key = f"{row.get('source')}:{row.get('job_id')}"
                if key in have:
                    continue
                have.add(key)
                remote_rows.append(row)
                extra.append(row)
            # 远端「本机应用」的 token 由对方自己算就行，但**飞书那条链路对方算不出来**：
            # 飞书 Agent 提交任务用的是**本机(服务器A)的方舟账号**（2026-10-14 实测：B 的飞书
            # 任务号 `cgt-…` 在本机账号里能查到、在 B 自己的账号里查不到）→ 所以 B 侧
            # provider_task_ids 拿得到、token 恒为 0。这里用本机账号给远端行补一次 usage 与
            # 金额（价格同走本机定价表，钱的去向一致）。查不到的行原样不动，不影响其它字段。
            if remote_rows:
                try:
                    _apply_ark_usage(remote_rows)
                except Exception as exc:  # noqa: BLE001
                    print(f"  [usage] remote ark enrich failed: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [usage] remote rows load failed: {exc}", flush=True)
        # 再把创作记录里「明细没有的」历史生成补进来 —— 目标是「所有生成都有记录」
        # （默认不加：它们没有用量数字，勾上「包含历史记录」才显示）
        if self._include_history_flag():
            for row in self._history_generation_rows(start, end):
                key = str(row.get("job_id") or "")
                if key and key in have:
                    continue
                if key:
                    have.add(key)
                extra.append(row)
        app_f = str(filters.get("app_filter") or "")
        user_f = str(filters.get("user_filter") or "")
        status_f = str(filters.get("status_filter") or "")
        needle = str(filters.get("query") or "").lower()
        merged = list(ours)
        for row in extra:
            if app_f and row.get("app") != app_f:
                continue
            if user_f and row.get("username") != user_f:
                continue
            if status_f and row.get("status") != status_f:
                continue
            if needle:
                hay = f"{row.get('title') or ''} {row.get('job_id') or ''}".lower()
                if needle not in hay:
                    continue
            merged.append(row)
        merged.sort(key=lambda r: (str(r.get("date") or ""), float(r.get("ts") or 0)), reverse=True)
        # 云代理（中转站）的真实花费：它们的生成响应里没有 usage，只能从用量日志补
        try:
            _apply_relay_usage(merged)
        except Exception as exc:  # noqa: BLE001
            print(f"  [usage] relay enrich failed: {exc}", flush=True)
        return merged

    def _include_history_flag(self) -> bool:
        """?include_history=1 时，才把「当时没做用量统计」的历史记录并进使用记录。

        默认不显示：这些行没有 token/金额，一屏全是「—」会淹没真正有数据的记录
        （2026-10-13 用户反馈）。勾上就全都有 —— 「所有生成都有记录」这条依然成立。
        """
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        vals = qs.get("include_history") or []
        return bool(vals) and str(vals[0]).strip() not in ("", "0", "false", "False")

    def _platform_stats_generations(self, user: dict):
        """按次生成明细（用量 + 金额）。管理员看全部，普通用户只看自己。

        支持筛选（区间 / 用户 / 应用 / 状态 / 关键词）与**翻页**；`summary` 与
        `bill_summary` 按**整个筛选区间**算（不是只算当前页），否则翻页时数字会跳。
        """
        rng = self._generation_query()
        if not rng:
            return
        start, end = rng
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def _first(key: str, default: str = "") -> str:
            vals = qs.get(key)
            return ((vals[0] if vals else "") or default).strip()

        try:
            limit = max(1, min(int(_first("limit", "15")), 500))
        except ValueError:
            limit = 15
        try:
            offset = max(0, int(_first("offset", "0")))
        except ValueError:
            offset = 0
        filters = {
            "user_filter": _first("user"),
            "app_filter": _first("app"),
            "status_filter": _first("status"),
            "query": _first("q"),
        }
        is_admin = auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"
        # 一次读全（内存切片）：total / summary 与筛选口径天然一致；飞书任务在查询时合并进来
        all_rows = self._generation_rows_merged(start, end, user, is_admin, filters)
        total = len(all_rows)
        page = all_rows[offset:offset + limit]
        self._json(200, {
            "ok": True, "start": start, "end": end,
            "rows": page, "total": total, "offset": offset, "limit": limit,
            "summary": self._generation_summary(all_rows),
            "bill_summary": self._bill_summary_rows(all_rows),
            "is_admin": is_admin,
            "price_note": "金额按 state/pricing.json 的方舟单价估算（原价，未计优惠）。"
                          "Chiyun / T8Star / 本地 ComfyUI 上游不返回 token，也没有单价，显示为「—」；"
                          "飞书任务等不上报进度的应用只记次数。",
        })

    def _generation_filters(self) -> dict:
        """页面上「使用记录」的筛选条件，导出时沿用同一套（否则导出的和看到的不是一回事）。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def _first(key: str, default: str = "") -> str:
            vals = qs.get(key)
            return ((vals[0] if vals else "") or default).strip()

        return {
            "user_filter": _first("user"),
            "app_filter": _first("app"),
            "status_filter": _first("status"),
            "query": _first("q"),
        }

    def _platform_stats_generations_export(self, user: dict):
        rng = self._generation_query()
        if not rng:
            return
        start, end = rng
        is_admin = auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"
        rows = self._generation_rows_merged(start, end, user, is_admin, self._generation_filters())
        buf = io.StringIO()
        # 默认全量 24 列；想要窄一点的行用 ?slim=1
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        want_slim = str(qs.get("slim", ["0"])[0]).lower() in ("1", "true", "yes")
        cols = GENERATION_EXPORT_COLUMNS_SLIM if want_slim else GENERATION_EXPORT_COLUMNS
        writer = csv.writer(buf)
        writer.writerow([label for _, label in cols])
        for row in rows:
            writer.writerow([export_cell(field, row.get(field)) for field, _ in cols])
        body = ("﻿" + buf.getvalue()).encode("utf-8")
        filename = f"生成明细-{start}_{end}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _platform_stats_generations_export_xlsx(self, user: dict):
        """同一份明细导出成 **xlsx**：列宽由我们定死（CSV 做不到），表头加粗冻结 + 自动筛选。

        用户 2026-10-13：「很多信息看起来不方便」——CSV 在 WPS/Excel 里要么窄列截断、
        要么一键最适合列宽把提示词列拉成一条。xlsx 打开即是排好的样子。
        """
        rng = self._generation_query()
        if not rng:
            return
        start, end = rng
        is_admin = auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"
        rows = self._generation_rows_merged(start, end, user, is_admin, self._generation_filters())
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        want_slim = str(qs.get("slim", ["0"])[0]).lower() in ("1", "true", "yes")
        cols = GENERATION_EXPORT_COLUMNS_SLIM if want_slim else GENERATION_EXPORT_COLUMNS
        headers = [label for _, label in cols]
        widths = [GENERATION_EXPORT_WIDTHS.get(field, 14) for field, _ in cols]
        data: list[list] = []
        for row in rows:
            line: list = []
            for field, _ in cols:
                value = row.get(field)
                if field in GENERATION_EXPORT_NUMERIC and isinstance(value, (int, float)) \
                        and not isinstance(value, bool):
                    line.append(value)          # 真数字：Excel 里能直接求和
                else:
                    line.append(export_cell(field, value))
            data.append(line)
        body = xlsx_export.build_xlsx(headers, data, widths, sheet_name="生成明细")
        filename = f"生成明细-{start}_{end}.xlsx"
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _remote_sources_status(self, user: dict):
        """远端来源（另一台同款平台）的同步状态。管理员看全部，普通用户也能看到「来源」是否存在。"""
        try:
            snap = remote_sources.status_snapshot()
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": str(exc), "sources": []})
            return
        self._json(200, {"ok": True, "local_label": remote_sources.local_label(),
                         "center": remote_sources.center_status(), **snap})

    def _remote_sources_sync(self, user: dict):
        """立刻同步一次（管理员）。用于「等不及 10 分钟」的场合。"""
        if not (auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"):
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        try:
            result = remote_sources.sync_all(force=True)
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": str(exc)})
            return
        ok = all(bool(v.get("ok")) for v in result.values()) if result else True
        self._json(200, {"ok": ok, "result": result, **remote_sources.status_snapshot()})

    @staticmethod
    def _bill_summary_rows(rows: list[dict]) -> list[dict]:
        """按「日期 + 计费项」汇总，字段口径对齐方舟账单（用量用千tokens，单价用元/千tokens）。"""
        groups: dict[tuple, dict] = {}
        for row in rows:
            family = str(row.get("price_family") or row.get("price_family_id") or row.get("model") or "")
            item = billing_item_label(family, str(row.get("resolution") or ""),
                                      str(row.get("image_size") or ""),
                                      bool(row.get("has_input_video")))
            day = str(row.get("date") or "")
            key = (day, item or "（未识别计费项）", family)
            bucket = groups.setdefault(key, {
                "账期": day[:7], "消费日期": day, "模型": family,
                "计费项": item or "（未识别计费项）", "次数": 0,
                "用量(tokens)": 0, "用量(千tokens)": 0.0, "单位": "千tokens",
                "_amount": 0.0, "_priced": 0, "币种": str(row.get("currency") or "CNY"),
            })
            tokens = int(row.get("tokens") or 0)
            bucket["次数"] += 1
            bucket["用量(tokens)"] += tokens
            bucket["用量(千tokens)"] = round(bucket["用量(千tokens)"] + tokens / 1000.0, 3)
            if isinstance(row.get("amount"), (int, float)):
                bucket["_amount"] += float(row["amount"])
                bucket["_priced"] += 1
        out = []
        for bucket in groups.values():
            tokens_k = bucket["用量(千tokens)"]
            priced = bucket["_priced"]
            bucket["单价(元/千tokens)"] = (
                round(bucket["_amount"] / tokens_k, 6) if tokens_k > 0 and priced else "")
            bucket["金额(元)"] = round(bucket["_amount"], 4) if priced else ""
            bucket["备注"] = "" if priced == bucket["次数"] else f"{bucket['次数'] - priced} 次无单价"
            bucket.pop("_amount", None)
            bucket.pop("_priced", None)
            out.append(bucket)
        out.sort(key=lambda b: (b["消费日期"], b["计费项"]))
        return out

    def _platform_stats_generations_summary_export(self, user: dict):
        """导出「按日期 × 计费项」的汇总表，口径对齐方舟控制台消费明细，便于对账。"""
        rng = self._generation_query()
        if not rng:
            return
        start, end = rng
        is_admin = auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"
        rows = self._generation_rows_merged(start, end, user, is_admin, self._generation_filters())
        summary = self._bill_summary_rows(rows)
        buf = io.StringIO()
        fieldnames = ["账期", "消费日期", "模型", "计费项", "次数", "用量(tokens)",
                      "用量(千tokens)", "单位", "单价(元/千tokens)", "金额(元)", "币种", "备注"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in summary:
            # 与明细导出同一口径：值里不留下换行/多余空白
            writer.writerow({k: export_cell(k, v) for k, v in row.items()})
        body = ("﻿" + buf.getvalue()).encode("utf-8")
        filename = f"用量汇总-{start}_{end}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _company_key_get(self, spec: AppSpec, user: dict):
        """Admin-only: returns whether the company-wide key for `spec` is set.
        Never returns the plaintext. Strict role check — do NOT go through
        spec.admin_permission (which would let manage_dreamina_accounts read
        another app's admin key)."""
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        try:
            conn = http.client.HTTPConnection("127.0.0.1", spec.port, timeout=5)
            conn.request("GET", "/api/config")
            resp = conn.getresponse()
            data = json.loads(resp.read()) if resp.status == 200 else {}
            conn.close()
        except Exception as exc:
            self._json(502, {"ok": False, "error": f"upstream error: {exc}"})
            return
        self._json(200, {
            "ok": True,
            "has_api_key": bool(data.get("has_api_key") or data.get("has_key")),
            "has_access_key": bool(data.get("has_access_key")),
            "has_secret_key": bool(data.get("has_secret_key")),
        })

    def _company_key_set(self, spec: AppSpec, user: dict):
        """Admin-only: write the company-wide key/AK/SK for `spec` via its
        /api/config. Empty fields silently ignored ('do not modify'). Strict
        role check (see _company_key_get)."""
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json()
        if body is None:
            return
        forwarded = {}
        for field in ("api_key", "access_key", "secret_key"):
            val = (body.get(field) or "").strip()
            if val:
                forwarded[field] = val
        if not forwarded:
            self._json(400, {"ok": False, "error": "no key field provided"})
            return
        try:
            conn = http.client.HTTPConnection("127.0.0.1", spec.port, timeout=5)
            payload = json.dumps(forwarded).encode("utf-8")
            username_encoded = urllib.parse.quote(user.get("username", ""), safe="")
            ts = int(time.time())
            conn.request("POST", "/api/config", body=payload, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "X-Is-Admin": "1",
                "X-Username": username_encoded,
                "X-Portal-Ts": str(ts),
                "X-Portal-Sig": _sign_admin_header(username_encoded, True, ts),
            })
            resp = conn.getresponse()
            data = json.loads(resp.read()) if resp.status == 200 else {}
            status = resp.status
            conn.close()
        except Exception as exc:
            self._json(502, {"ok": False, "error": f"upstream error: {exc}"})
            return
        if status != 200:
            self._json(status, data or {"ok": False, "error": "upstream failed"})
            return
        self._json(200, {
            "ok": True,
            "has_api_key": bool(data.get("has_api_key")),
            "has_access_key": bool(data.get("has_access_key")),
            "has_secret_key": bool(data.get("has_secret_key")),
        })

    def _apps_meta(self):
        """Return sub-app metadata that the frontend needs to render tabs
        and stats tables. Excludes backend-sensitive fields (credentials,
        admin permissions, extra headers) — those are enforced server-side."""
        payload = [
            {
                "name": s.name,
                "display_name": s.display_name,
                "mount": s.mount,
                "iframe_url": s.iframe_url,
                "component_factory": s.component_factory,
                "color": s.color,
                "metrics": list(s.metrics),
                "unit_label": s.unit_label,
                "stats_combine": s.stats_combine,
            }
            for s in SPECS
        ]
        self._json(200, {"ok": True, "apps": payload})

    def _try_company_key_route(self, path: str, method: str, user: dict) -> bool:
        """Dispatch /api/platform/<endpoint> if any spec declares that
        endpoint. Returns True if handled. `/api/platform/portrait-key`
        stays wired via the spec so old bookmarks/API clients keep working."""
        prefix = "/api/platform/"
        if not path.startswith(prefix):
            return False
        endpoint = path[len(prefix):]
        for spec in SPECS:
            if spec.company_key_endpoint == endpoint:
                if method == "GET":
                    self._company_key_get(spec, user)
                elif method == "POST":
                    self._company_key_set(spec, user)
                else:
                    self._json(405, {"ok": False, "error": "method not allowed"})
                return True
        return False

    def _valid_report_date(self, date: str) -> bool:
        """Accept YYYY-MM-DD strictly, reject future dates. Emits 400 on failure."""
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            self._json(400, {"ok": False, "error": "invalid date"})
            return False
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid date"})
            return False
        if d > datetime.now().date():
            self._json(400, {"ok": False, "error": "date in future"})
            return False
        return True

    def _report_csv_download(self, user: dict, date: str):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        if not self._valid_report_date(date):
            return
        state_dir = STATE_DIR
        csv_path = state_dir / "reports" / f"{date}.csv"
        if not csv_path.exists():
            # generate on demand。注意：daily_report 当前契约为
            # load_usage_data → write_csv(state_dir, date, by_user_raw)；
            # 早前这里调用过不存在的 load_events（半成品重构残留）→ AttributeError 500。
            by_user_raw, _ = _daily_report_module.load_usage_data(state_dir, date)
            csv_path = _daily_report_module.write_csv(state_dir, date, by_user_raw)
        data = csv_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="usage-{date}.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _report_send(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json() or {}
        date = str(body.get("date") or "").strip()
        if not self._valid_report_date(date):
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        key = _daily_report_module._load_deepseek_key(STATE_DIR)
        try:
            result = _daily_report_module.send_daily_report(STATE_DIR, date, cfg, deepseek_key=key, dry_run=False)
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"send failed: {exc}"})
            return
        self._json(200, {"ok": result["ok"], "info": result["feishu_info"], "source": result["source"]})

    def _report_preview(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json() or {}
        date = str(body.get("date") or "").strip()
        if not self._valid_report_date(date):
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        key = _daily_report_module._load_deepseek_key(STATE_DIR)
        try:
            result = _daily_report_module.send_daily_report(STATE_DIR, date, cfg, deepseek_key=key, dry_run=True)
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"preview failed: {exc}"})
            return
        self._json(200, {
            "ok": True,
            "source": result["source"],
            "card": result["card"],
        })

    def _feishu_config_get(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        # Secrets (webhook URL + sign secret) are never sent back to the browser
        # in plaintext to avoid accidental round-trip corruption if the admin
        # saves without retyping. Frontend shows a *_present flag instead.
        masked = dict(cfg)
        w = masked.get("webhook_url", "")
        masked["webhook_url_present"] = bool(w)
        if w:
            masked["webhook_url_hint"] = w[:35] + "..." if len(w) > 38 else w
        else:
            masked["webhook_url_hint"] = ""
        masked["webhook_url"] = ""
        s = masked.get("sign_secret", "")
        masked["sign_secret_present"] = bool(s)
        masked["sign_secret"] = ""
        self._json(200, {"ok": True, "config": masked})

    def _feishu_config_put(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json()
        if body is None:
            return
        updates: dict = {}
        for k in ("enabled", "webhook_url", "sign_secret", "schedule_time", "portal_base_url"):
            if k in body:
                updates[k] = body[k]
        cfg = _daily_report_module.save_config(STATE_DIR, updates)
        self._json(200, {"ok": True, "config": {k: cfg[k] for k in ("enabled", "schedule_time", "portal_base_url")}})

    def _platform_activity(self, user: dict):
        if not auth.has_permission(user, "view_stats_all"):
            self._json(200, {"ok": True, "activity": []})
            return
        merged = []
        for name, config in APPS.items():
            if not config["spec"].managed:
                continue
            try:
                conn = http.client.HTTPConnection("127.0.0.1", config["port"], timeout=5)
                conn.request("GET", "/api/activity")
                resp = conn.getresponse()
                if resp.status == 200:
                    data = json.loads(resp.read())
                    items = (data.get("records")
                             or data.get("items")
                             or data.get("history")
                             or [])
                    for item in items[:20]:
                        item["_app"] = name
                        if not item.get("username"):
                            jid = item.get("job_id") or ""
                            item["username"] = tracker.get_job_owner(name, jid) if jid else ""
                        merged.append(item)
                conn.close()
            except Exception:
                pass
        merged.sort(key=lambda x: x.get("created_at") or x.get("time") or "", reverse=True)
        self._json(200, {"ok": True, "activity": merged[:50]})

    def _platform_video_check(self, user: dict):
        """确定性视频体检：抽帧算画面指标，不调用任何模型。

        与 _platform_thumb() 的关键差别：这台机器没有 ffmpeg，不能把媒体 URL
        直接交给 ffmpeg 的 http demuxer，所以先把媒体取到本地临时文件，再交给
        cv2 解码。只在用户显式请求时跑，不阻塞生成流程。
        结论全部带数值证据，语义判读（多余肢体等）留给后续接模型。
        """
        if video_check is None:
            self._json(503, {"ok": False, "error": "视频体检不可用：cv2 未安装"})
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        app = (qs.get("app", [None])[0] or "").strip()
        url = (qs.get("url", [None])[0] or "").strip()
        if app not in APPS or not url or len(url) > 512:
            self._json(400, {"ok": False, "error": "bad request"})
            return
        if not re.fullmatch(r"/[A-Za-z0-9._/-]+", url) or ".." in url:
            self._json(400, {"ok": False, "error": "bad request"})
            return
        port = APPS[app].get("port")
        if not port:
            self._json(404, {"ok": False, "error": "unknown app"})
            return

        expected: dict[str, Any] = {}
        declared = (qs.get("duration", [None])[0] or "").strip()
        if declared:
            expected["duration"] = declared

        source = f"http://127.0.0.1:{port}{url}"
        tmp_path = None
        try:
            with _THUMB_SEM:
                with urllib.request.urlopen(source, timeout=60) as resp:
                    payload = resp.read(MAX_VIDEO_CHECK_BYTES + 1)
            if len(payload) > MAX_VIDEO_CHECK_BYTES:
                self._json(413, {"ok": False, "error": "视频过大，暂不支持体检"})
                return
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
                handle.write(payload)
                tmp_path = handle.name
            result = video_check.analyze(tmp_path, expected=expected or None)
            self._json(200 if result.get("ok") else 422, result)
        except Exception as exc:
            self._json(502, {"ok": False, "error": f"体检失败: {str(exc)[:200]}"})
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _platform_thumb(self, user: dict):
        """服务端视频缩略图（上游 77c4802+cfe65c3 方案 A）：把媒体 URL 直接喂给
        ffmpeg 抽帧。ffmpeg 的 http demuxer 支持 Range 寻址，能自己找到 MP4
        尾部的 moov atom，无需整片下载；抽出的 480 宽 JPEG 按 sha1(app+url)
        落盘缓存终身复用（原子写入）。失败返回 404，前端 <img onerror>
        回退到 <video preload=metadata>。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        app = (qs.get("app", [None])[0] or "").strip()
        url = (qs.get("url", [None])[0] or "").strip()
        if app not in APPS or not url or len(url) > 512:
            self._json(400, {"ok": False, "error": "bad request"})
            return
        # 只接受子应用媒体路径：以 / 开头、白名单字符、无路径穿越。
        if not re.fullmatch(r"/[A-Za-z0-9._/-]+", url) or ".." in url:
            self._json(400, {"ok": False, "error": "bad request"})
            return
        port = APPS[app].get("port")
        if not port:
            self._json(404, {"ok": False})
            return
        key = hashlib.sha1(f"{app}\0{url}".encode("utf-8")).hexdigest()
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        cached = THUMB_DIR / f"{key}.jpg"
        if not cached.is_file():
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                print("  [thumb] ffmpeg not found", flush=True)
                self._json(404, {"ok": False})
                return
            source = f"http://127.0.0.1:{port}{url}"
            tmp = THUMB_DIR / f".{key}.jpg.tmp"
            try:
                with _THUMB_SEM:
                    proc = subprocess.run(
                        [ffmpeg, "-hide_banner", "-loglevel", "error",
                         "-i", source, "-ss", "0.2", "-frames:v", "1",
                         "-vf", "scale=480:-2", "-f", "mjpeg", "pipe:1"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=90)
                if proc.returncode != 0 or len(proc.stdout) < 64:
                    print(f"  [thumb] ffmpeg failed for {app}{url}: {proc.stderr[:300]!r}", flush=True)
                    self._json(404, {"ok": False})
                    return
                tmp.write_bytes(proc.stdout)
                os.replace(tmp, cached)
                try:
                    os.chmod(cached, 0o600)
                except OSError:
                    pass
            except (subprocess.TimeoutExpired, OSError) as exc:
                tmp.unlink(missing_ok=True)
                print(f"  [thumb] ffmpeg failed for {app}{url}: {exc}", flush=True)
                self._json(404, {"ok": False})
                return
        try:
            content = cached.read_bytes()
        except OSError:
            self._json(404, {"ok": False})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _platform_queue(self, user: dict):
        is_admin = auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"
        self._json(200, {
            "ok": True,
            "items": tracker.queue_snapshot(user.get("username", ""), is_admin=is_admin),
        })

    def _platform_history(self, user: dict):
        """任务级历史记录查询：admin 全量（可按人筛），普通用户仅本人。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def _first(key: str, default: str) -> str:
            vals = qs.get(key)
            return vals[0] if vals else default

        is_admin = auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"
        try:
            limit = max(1, min(int(_first("limit", "60")), 200))
        except ValueError:
            limit = 60
        try:
            offset = max(0, int(_first("offset", "0")))
        except ValueError:
            offset = 0
        try:
            days = int(_first("days", "30"))
        except ValueError:
            days = 30
        kind = _first("kind", "all")
        status_filter = _first("status", "all")
        query = _first("q", "")
        user_filter = _first("user", "")
        app_filter = _first("app", "")
        # Fetch the local set before pagination, then merge the independently
        # persisted Feishu Agent runs and paginate the combined timeline.
        local_items, _ = tracker.query_history(
            username=user.get("username", ""),
            is_admin=is_admin,
            days=days, kind=kind, status=status_filter, q=query,
            user_filter=user_filter, app=app_filter, limit=HISTORY_CAP, offset=0,
        )
        merged = list(local_items)
        if is_admin:
            portal_users = [u for u in auth.list_users() if u.get("enabled", True)]
            if user_filter:
                portal_users = [u for u in portal_users if u.get("username") == user_filter]
        else:
            portal_users = [{"id": user["user_id"], "username": user.get("username", "")}]
        cutoff = time.time() - max(1, min(days, 365)) * 86400
        for portal_user in portal_users:
            for rec in _fetch_feishu_history(
                str(portal_user.get("id", "")), portal_user.get("username", ""), limit=200,
            ):
                try:
                    submitted_at = float(rec.get("submitted_at") or 0)
                except (TypeError, ValueError):
                    continue
                if submitted_at < cutoff:
                    continue
                if kind != "all" and rec.get("kind") != kind:
                    continue
                if status_filter != "all" and rec.get("status") != status_filter:
                    continue
                if query and query.lower() not in f"{rec.get('prompt', '')} {rec.get('job_id', '')}".lower():
                    continue
                if user_filter and rec.get("username") != user_filter:
                    continue
                if app_filter and rec.get("app") != app_filter:
                    continue
                merged.append(rec)
        merged.sort(key=lambda rec: float(rec.get("submitted_at") or 0), reverse=True)
        total = len(merged)
        items = merged[offset:offset + limit]
        # Repair older records on demand; cap network lookups so history stays responsive.
        for rec in items[:12]:
            if _history_record_needs_activity_backfill(rec) and _backfill_history_record(rec):
                tracker.history_upsert(rec)
        queue_map = {
            f"{q['app']}:{q['job_id']}": q
            for q in tracker.queue_snapshot(user.get("username", ""), is_admin=is_admin)
        }
        out = []
        for rec in items:
            spec = SPEC_BY_NAME.get(rec.get("app", ""))
            q = queue_map.get(f"{rec.get('app', '')}:{rec.get('job_id', '')}")
            out.append({**rec,
                        "display_name": spec.display_name if spec else rec.get("app", ""),
                        "queue_position": q.get("queue_position") if q else None,
                        "eta_minutes": q.get("eta_minutes") if q else None})
        self._json(200, {"ok": True, "total": total, "items": out})

    def _platform_history_events(self, user: dict):
        """Server-sent event stream for terminal history updates."""
        is_admin = auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"
        username = user.get("username", "")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()
        with _HISTORY_EVENT_COND:
            cursor = _HISTORY_EVENT_SEQ
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                with _HISTORY_EVENT_COND:
                    if _HISTORY_EVENT_SEQ <= cursor:
                        _HISTORY_EVENT_COND.wait(timeout=15)
                    pending = [
                        payload for seq, payload in _HISTORY_EVENTS
                        if seq > cursor
                    ]
                    cursor = _HISTORY_EVENT_SEQ
                if not pending:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                for payload in pending:
                    if not is_admin and payload.get("username") != username:
                        continue
                    raw = json.dumps(payload, ensure_ascii=False)
                    self.wfile.write(f"event: history\ndata: {raw}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ssl.SSLError):
            return

    def _platform_history_users(self, user: dict):
        if not (auth.has_permission(user, "view_stats_all") or user.get("role") == "admin"):
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        users = {r.get("username", "") for r in tracker.history_records().values()
                 if isinstance(r, dict) and r.get("username")}
        users.update(u.get("username", "") for u in auth.list_users()
                     if u.get("enabled", True) and u.get("username"))
        self._json(200, {"ok": True, "users": sorted(users)})

    # ── Proxy ─────────────────────────────────────────────────────────────────

    def _try_proxy(self, path: str, method: str, user: dict) -> bool:
        for app_name, config in APPS.items():
            prefix = f"/{app_name}"
            if path == prefix or path.startswith(prefix + "/"):
                target_path = path[len(prefix):] or "/"
                # 透传 querystring（do_GET/do_POST/do_DELETE 提取 path 时
                # 用 urlparse 把 query 丢了，这里补回去 — 否则子应用拿不到
                # 像 ?group_ids=xxx 这种过滤参数）
                parsed = urllib.parse.urlparse(self.path)
                if parsed.query:
                    target_path = target_path + "?" + parsed.query
                if not auth.has_permission(user, "use_apps"):
                    self._json(403, {"ok": False, "error": "forbidden"})
                    return True
                self._proxy(app_name, config["port"], method, target_path, user)
                return True
        return False

    def _proxy(self, app_name: str, port: int, method: str, target_path: str, user: dict):
        client_ip = self.client_address[0]
        is_job = tracker._is_job_request(method, target_path)
        # 兜底：能建任务但不上报 X-Job-Id 的应用（飞书 run 等）——过去完全不统计
        count_only = tracker._is_count_only_job_request(method, target_path)
        tracker.record(app_name, client_ip, method, target_path, username=user["username"])

        # Detect job type for usage stats. Video duration is read later from
        # /api/jobs/<id> directly, so we don't need to parse the request body.
        # See app_spec.classify_job_type() for the per-app rules.
        spec = SPEC_BY_NAME.get(app_name)
        job_type = "image"
        if is_job and spec is not None:
            job_type = classify_job_type(spec, target_path)

        conn = None
        try:
            body = None
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                length = int(self.headers.get("Content-Length") or "0")
                if length > 0:
                    body = self.rfile.read(length)

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
            headers: dict[str, str] = {}
            # Range must be forwarded so <video> scrubbing / resumable downloads
            # of 50-200MB media work: the browser sends Range, sub-apps' serve_file
            # already answers 206 + Content-Range, and the response side streams
            # headers through verbatim. Without forwarding the request Range, the
            # sub-app always returns 200 full-body and seeking re-downloads the clip.
            for key in ("Content-Type", "Content-Length", "Accept", "Accept-Encoding",
                        "Range", "If-Range",
                        "X-Workspace-Id", "X-Api-Key", "X-Access-Key", "X-Secret-Key"):
                val = self.headers.get(key)
                if val:
                    headers[key] = val

            # Resolve stored key by ID if client sent X-Key-Id.
            # Header wiring is driven by spec.credential_scheme:
            #   api_key -> X-Api-Key: <key>
            #   ak_sk   -> "<ak>:::<sk>" split into X-Access-Key + X-Secret-Key
            #   none    -> skip
            key_id = self.headers.get("X-Key-Id", "").strip()
            if key_id:
                key_val = key_manager.resolve(user["user_id"], key_id)
                if key_val:
                    scheme = spec.credential_scheme if spec else "api_key"
                    if scheme == "ak_sk" and ":::" in key_val:
                        ak, sk = key_val.split(":::", 1)
                        headers["X-Access-Key"] = ak
                        headers["X-Secret-Key"] = sk
                    elif scheme == "api_key":
                        headers["X-Api-Key"] = key_val
                    # scheme == "none": ignore any provided key

            headers["X-Forwarded-For"] = client_ip
            # Propagate public-facing host/proto so subapps can build absolute URLs
            # for callbacks. cloudflared sets these when terminating the tunnel;
            # for direct LAN/HTTPS access we synthesize from the Host header.
            fwd_host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").strip()
            if fwd_host:
                headers["X-Forwarded-Host"] = fwd_host
            fwd_proto = (self.headers.get("X-Forwarded-Proto") or "").strip()
            if not fwd_proto:
                fwd_proto = "https" if self._is_https() else "http"
            headers["X-Forwarded-Proto"] = fwd_proto
            # Propagate the logged-in username to every sub-app for per-user
            # job ownership / filtering. X-Is-Admin is now strictly tied to the
            # admin role; the dreamina-specific "everyone can manage accounts"
            # affordance moves to X-Dreamina-Manage so task views can be
            # per-user even when account management is open to all.
            # http.client headers must be latin-1 safe, so URL-percent-encode
            # the username — sub-apps urllib.parse.unquote it back. Empty/ASCII
            # names pass through unchanged.
            username_encoded = urllib.parse.quote(user.get("username", ""), safe="")
            headers["X-Username"] = username_encoded
            # User identity comes only from the authenticated Portal session.
            # Never accept a browser-supplied X-Portal-User-Id value.
            headers["X-Portal-User-Id"] = str(user["user_id"])
            # X-Is-Admin: raised for the admin role and also for any spec that
            # declares an `admin_permission` and the user has that permission
            # (dreamina lets `manage_dreamina_accounts` proxy admin so ops folks
            # can add accounts without full portal admin).
            is_admin = user.get("role") == "admin"
            if not is_admin and spec is not None and spec.admin_permission:
                is_admin = auth.has_permission(user, spec.admin_permission)
            if is_admin:
                headers["X-Is-Admin"] = "1"
            # Static extra headers with {perm:xxx} placeholders — e.g. dreamina
            # emits X-Dreamina-Manage=1 when the caller has use_apps.
            if spec is not None and spec.extra_headers:
                for hk, hv in resolve_extra_headers(spec, user, auth.has_permission).items():
                    headers[hk] = hv
            # HMAC-sign the (username, is_admin, ts) triple so a sub-app never
            # trusts a raw X-Is-Admin header from an unauthenticated caller.
            # See _sign_admin_header — sub-apps must verify with the shared
            # PORTAL_INTERNAL_TOKEN and a small time window against replay.
            ts = int(time.time())
            headers["X-Portal-Ts"] = str(ts)
            headers["X-Portal-Sig"] = _sign_admin_header(username_encoded, is_admin, ts)

            conn.request(method, target_path, body=body, headers=headers)
            resp = conn.getresponse()

            # job_id is surfaced by the sub-app via the X-Job-Id response header
            # so we can register usage stats without buffering the body. This is
            # the P0 fix for #15: long-running creation responses no longer hold
            # the proxy thread waiting for resp.read() to complete.
            if is_job and resp.status in (200, 201):
                jid_header = resp.getheader("X-Job-Id", "").strip()
                aid_header = resp.getheader("X-Activity-Id", "").strip()
                if jid_header:
                    metadata = {}
                    if method == "POST" and body:
                        metadata = extract_job_metadata(
                            self.headers.get("Content-Type", ""), body)
                    if aid_header:
                        metadata["activity_id"] = aid_header
                    tracker.register_job(app_name, jid_header, user["username"],
                                         job_type, metadata=metadata)
                    tracker.inc_daily_jobs(app_name)
            elif count_only and 200 <= resp.status < 300:
                # 该应用建了任务但不上报 X-Job-Id：只记次数与归属（详见 record_untracked_job）
                title = ""
                if method == "POST" and body:
                    title = str((extract_job_metadata(
                        self.headers.get("Content-Type", ""), body) or {}).get("title") or "")
                tracker.record_untracked_job(app_name, user["username"], job_type, title=title)

            content_type = resp.getheader("Content-Type", "")
            # Only buffer JSON bodies we still need to inspect (none for now —
            # X-Job-Id covers the historic reason). Keep `is_job` out of the
            # condition so creation responses stream like everything else.
            should_buffer = False

            if should_buffer:
                resp_body = resp.read()
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() in ("transfer-encoding", "connection", "server", "date", "content-length"):
                        continue
                    self.send_header(key, value)
                if _cache_path_of(target_path).endswith((".html", ".js", ".css", ".mjs")):
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self._cors_headers()
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
            else:
                # Stream everything (job creation, media, JSON status) without buffering
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() in ("transfer-encoding", "connection", "server", "date"):
                        continue
                    self.send_header(key, value)
                if _cache_path_of(target_path).endswith((".html", ".js", ".css", ".mjs")):
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self._cors_headers()
                self.end_headers()
                shutil.copyfileobj(resp, self.wfile, length=65536)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError) as exc:
            # The client or the TLS session went away before we could answer.
            # Swallowing this silently is why an aborted upload produced no log
            # line anywhere; record it, and still try to hand back a JSON error
            # (the write will simply fail again when the socket is truly gone).
            print(f"  [PROXY ABORT] {method} {target_path} ip={client_ip} "
                  f"{type(exc).__name__}: {exc}", flush=True)
            try:
                self._json(502, {"ok": False, "error": f"proxy aborted: {type(exc).__name__}"})
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                pass
        except Exception as exc:
            msg = str(exc)[:300]
            try:
                self._json(502, {"ok": False, "error": f"proxy error: {msg}"})
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                pass
        finally:
            # Always release the upstream connection. Previously conn.close() sat
            # on the normal path only, so a client disconnect mid-copyfileobj
            # (BrokenPipe) jumped to except and leaked the http.client socket to
            # the sub-app — fds/connections accumulated over repeated aborts.
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ── Static file serving ───────────────────────────────────────────────────

    def _serve_portal(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        file_path = STATIC_DIR / path.lstrip("/")
        if not file_path.exists() or not file_path.is_file():
            file_path = STATIC_DIR / "index.html"
        if not file_path.exists():
            self.send_error(404)
            return
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(content)

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _json(self, status: int, data: dict):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, url: str, set_cookie_token: str | None = None):
        self.send_response(302)
        self.send_header("Location", url)
        if set_cookie_token:
            self._set_cookie(set_cookie_token)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _internal_finalize_job(self):
        """Loopback-only endpoint sub-apps call to declare a job's terminal
        status. Failures roll back the +1 inc_daily_jobs applied at register
        time. Authenticated by a shared random token injected into the
        sub-app's env at portal startup."""
        token = self.headers.get("X-Internal-Token", "")
        client_ip = self.client_address[0]
        if token != INTERNAL_TOKEN or not client_ip.startswith("127."):
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        body = self._read_json()
        if body is None:
            return
        app = (body.get("app") or "").strip()
        job_id = (body.get("job_id") or "").strip()
        status = (body.get("status") or "").strip()
        if not app or not job_id or not status:
            self._json(400, {"ok": False, "error": "missing fields"})
            return
        rolled = tracker.finalize_job(app, job_id, status)
        tracker.refresh_history_from_child(app, job_id, status)
        self._json(200, {"ok": True, "rolled_back": rolled})

    def _cors_headers(self):
        global _ALLOWED_ORIGINS, _ALLOWED_ORIGINS_TS
        now = time.time()
        if _ALLOWED_ORIGINS is None or now - _ALLOWED_ORIGINS_TS > _CORS_REFRESH_SECONDS:
            _ALLOWED_ORIGINS = _default_allowed_origins()
            _ALLOWED_ORIGINS_TS = now
        origin = self.headers.get("Origin") or ""
        # Only echo Origin back if it is in the whitelist. Missing or foreign
        # origins get no ACAO header at all, so browsers block the response
        # from any cross-origin script that expected it.
        if origin and origin in _ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Workspace-Id, X-Api-Key, X-Access-Key, X-Secret-Key, X-Key-Id")

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw)
        except Exception:
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return None


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    lan_ip = get_lan_ip()
    # PORTAL_HTTP_ONLY=1 forces HTTP mode (TLS terminates at upstream proxy like Cloudflare Tunnel)
    if os.environ.get("PORTAL_HTTP_ONLY") == "1":
        certs = None
        print("  [INFO] PORTAL_HTTP_ONLY=1 — running HTTP-only (TLS handled upstream)")
    else:
        certs = ensure_certs(_DATA_BASE / "certs")

    # Bump listen backlog so bursts of concurrent requests during job submission
    # storms aren't dropped at the kernel layer (default is 5 — too tight for the
    # 10+ users we now have). Set the class attribute before instantiation so
    # server_activate() picks it up. 64 keeps memory cost negligible.
    ThreadingHTTPServer.request_queue_size = 64
    server = ThreadingHTTPServer(("0.0.0.0", PORTAL_PORT), Handler)
    redirect_server = None

    if certs:
        cert_file, key_file = certs
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        # do_handshake_on_connect=False：推迟握手到每个连接的 Handler.setup()
        # （连接自己的线程里，10s 超时）。默认 True 时握手在 SSLSocket.accept()
        # 里主线程同步完成且无超时，一个半开连接即可卡死整个 Portal。
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)

        class RedirectHandler(SimpleHTTPRequestHandler):
            def log_message(self, format, *args): pass
            def do_GET(self):
                host = self.headers.get("Host", "").split(":")[0] or lan_ip
                https_url = f"https://{host}:{PORTAL_PORT}{self.path}"
                page = (f'<script>var h=window.location.hostname;'
                        f'window.location.replace("https://"+h+":{PORTAL_PORT}"+window.location.pathname+window.location.search);</script>'
                        f'<a href="{https_url}">{https_url}</a>').encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(page)
            do_POST = do_GET
            do_OPTIONS = do_GET

        redirect_server = ThreadingHTTPServer(("0.0.0.0", REDIRECT_PORT), RedirectHandler)
        print(f"\n  RedCraft (HTTPS):")
        print(f"    Local:   https://127.0.0.1:{PORTAL_PORT}")
        print(f"    LAN:     https://{lan_ip}:{PORTAL_PORT}")
    else:
        print(f"\n  RedCraft (HTTP):")
        print(f"    Local:   http://127.0.0.1:{PORTAL_PORT}")
        print(f"    LAN:     http://{lan_ip}:{PORTAL_PORT}")

    print("Starting sub-applications...")
    manager.start_all()
    time.sleep(2)
    if redirect_server:
        threading.Thread(target=redirect_server.serve_forever, daemon=True).start()

    # Feishu daily report scheduler (daemon, tolerates all errors internally)
    threading.Thread(
        target=_daily_report_module.scheduler_loop,
        args=(STATE_DIR,),
        daemon=True,
        name="daily_report_scheduler",
    ).start()
    print("  [daily_report] scheduler thread started", flush=True)

    threading.Thread(
        target=cat_concept_store.scheduler_loop,
        daemon=True,
        name="cat_trend_scheduler",
    ).start()
    print("  [cat-trends] scheduler thread started", flush=True)

    if certs:
        threading.Thread(
            target=start_cert_ip_watcher,
            args=(_DATA_BASE / "certs",),
            daemon=True,
            name="cert_ip_watcher",
        ).start()
        print("  [cert-watch] LAN IP watcher started", flush=True)

    for name, config in APPS.items():
        print(f"    {name:20s} -> http://127.0.0.1:{config['port']}")

    if auth.first_run():
        print(f"\n  [SETUP] No users found — visit the portal to create the admin account")
    print(f"\n  Press Ctrl+C to stop\n")

    def shutdown_handler(sig, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, shutdown_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown_handler)
    elif hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Force a final synchronous flush of usage data before we tear down —
        # the debounced flusher may have pending dirty state not yet written.
        try:
            tracker.flush()
        except Exception:
            pass
        manager.shutdown()
        if redirect_server:
            redirect_server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
