#!/usr/bin/env python3
"""导演台子应用：提示词优化/扩写（DeepSeek）+ 文生图（火山方舟 Seedream）。

与 nano-banana 同款 stdlib 骨架；由 portal 按 apps.json 拉起
（env: PORT / HOST / CORS / VOLCENGINE_ARK_API_KEY）。
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
_DATA_BASE = Path(os.environ.get("DATA_DIR", str(ROOT)))
OUTPUT_DIR = _DATA_BASE / "outputs"
STATE_DIR = _DATA_BASE / "state"
PROVIDERS_PATH = ROOT / "providers.json"
SKILL_PATH = ROOT / "SKILL.md"
DEEPSEEK_KEY_PATH = STATE_DIR / "deepseek.key"

PORT = int(os.environ.get("PORT", "8895"))
HOST = os.environ.get("HOST", "127.0.0.1")
CORS = os.environ.get("CORS") == "1"

ASPECT_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9", "9:21")

# 来自 nano-banana 子应用实测约束，勿凭空推导。
_SEEDREAM_5_PRO_SIZES: dict[str, dict[str, str]] = {
    "1K": {
        "1:1": "1024x1024", "4:3": "1152x864", "3:4": "864x1152",
        "16:9": "1424x800", "9:16": "800x1424", "3:2": "1248x832",
        "2:3": "832x1248", "21:9": "1568x672", "9:21": "672x1568",
    },
    "1.5K": {
        "1:1": "1536x1536", "4:3": "1792x1344", "3:4": "1344x1792",
        "16:9": "2048x1152", "9:16": "1152x2048", "3:2": "1872x1248",
        "2:3": "1248x1872", "21:9": "2352x1008", "9:21": "1008x2352",
    },
    "2K": {
        "1:1": "2048x2048", "4:3": "2368x1776", "3:4": "1776x2368",
        "16:9": "2816x1584", "9:16": "1584x2816", "3:2": "2496x1664",
        "2:3": "1664x2496", "21:9": "3136x1344", "9:21": "1344x3136",
    },
}


def _load_providers() -> dict[str, Any]:
    if PROVIDERS_PATH.exists():
        return json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    return {}


PROVIDERS = _load_providers()


def seedream_size(resolution: str, aspect_ratio: str) -> str:
    resolution = str(resolution or "2K").strip()
    aspect_ratio = str(aspect_ratio or "auto").strip()
    mapping = _SEEDREAM_5_PRO_SIZES.get(resolution)
    if mapping is None:
        raise ValueError("Seedream 5.0 Pro 尺寸只支持 1K、1.5K、2K")
    if aspect_ratio == "auto":
        return resolution
    if aspect_ratio not in mapping:
        raise ValueError(f"Seedream 5.0 Pro 不支持比例 {aspect_ratio}")
    return mapping[aspect_ratio]


def _ark_key() -> str:
    return (os.environ.get("VOLCENGINE_ARK_API_KEY") or "").strip()


def _load_deepseek_key() -> str:
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    if DEEPSEEK_KEY_PATH.exists():
        return DEEPSEEK_KEY_PATH.read_text(encoding="utf-8").strip()
    return ""


def config_payload() -> dict[str, Any]:
    ark = PROVIDERS.get("ark", {})
    deepseek = PROVIDERS.get("deepseek", {})
    return {
        "model": ark.get("model", "doubao-seedream-5-0-pro-260628"),
        "aspect_ratios": list(ASPECT_RATIOS),
        "resolutions": ["1K", "1.5K", "2K"],
        "default_resolution": ark.get("default_resolution", "2K"),
        "default_aspect_ratio": ark.get("default_aspect_ratio", "1:1"),
        "default_count": int(ark.get("default_count", 1)),
        "ark_ready": bool(_ark_key()),
        "deepseek_ready": bool(_load_deepseek_key() or SKILL_PATH.exists()),
        "deepseek_model": deepseek.get("model", "deepseek-chat"),
    }


def json_response(handler: SimpleHTTPRequestHandler, status: int, payload: Any,
                  extra_headers: dict[str, str] | None = None) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    if CORS:
        handler.send_header("Access-Control-Allow-Origin", "*")
    for key, value in (extra_headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


DEEPSEEK_MODEL = PROVIDERS.get("deepseek", {}).get("model", "deepseek-chat")
DEEPSEEK_BASE = PROVIDERS.get("deepseek", {}).get("base_url", "https://api.deepseek.com/v1")

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


class APIError(Exception):
    def __init__(self, status_code: int, message: str, raw_response: str = ""):
        self.status_code = status_code
        self.message = message
        self.raw_response = raw_response
        super().__init__(message)


def _load_skill() -> str:
    if SKILL_PATH.exists():
        return SKILL_PATH.read_text(encoding="utf-8").strip()
    return ""


def request_json(method: str, url: str, api_key: str, body: dict | None = None,
                 timeout: float = 120) -> dict:
    """urllib JSON 请求；非 2xx 抛 APIError（message 为中文翻译）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = ""
        try:
            detail = json.loads(raw).get("error", {}).get("message", "")
        except Exception:
            detail = raw[:200]
        if exc.code == 401:
            raise APIError(401, "密钥无效或已过期（InvalidApiKey）", raw)
        if exc.code == 429:
            raise APIError(429, "请求过于频繁，稍后重试", raw)
        raise APIError(exc.code, f"上游服务错误：{detail or exc.reason}", raw)
    except urllib.error.URLError as exc:
        raise APIError(502, f"网络错误：{exc.reason}", "") from exc


def optimize_prompt(text: str, mode: str) -> dict[str, Any]:
    skill = _load_skill()
    if not skill:
        return {"ok": False, "error": "SKILL.md 未找到或为空，无法进行优化"}
    if not (text or "").strip():
        return {"ok": False, "error": "请先输入提示词"}
    api_key = _load_deepseek_key()
    if not api_key:
        return {"ok": False, "error": (
            "提示词优化未配置 DeepSeek API Key，"
            "请联系管理员把 sk-... 写入 director/state/deepseek.key"
        )}
    mode_text = "按「优化 refine」规则改写" if mode == "refine" else "按「扩写 expand」规则改写"
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": skill},
            {"role": "user", "content": f"{mode_text}：\n{text.strip()}"},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    try:
        result = request_json(
            "POST", f"{DEEPSEEK_BASE}/chat/completions", api_key, body, timeout=120,
        )
        content = (result.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content.strip():
            return {"ok": False, "error": "DeepSeek 返回了空结果，请重试"}
        return {"ok": True, "prompt": content.strip()}
    except APIError as exc:
        return {"ok": False, "error": exc.message}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/config":
            json_response(self, 200, {"ok": True, **config_payload()})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/optimize-prompt":
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                json_response(self, 400, {"ok": False, "error": "请求格式异常"})
                return
            json_response(self, 200, optimize_prompt(
                str(data.get("text", "")), str(data.get("mode", "refine"))
            ))
            return
        json_response(self, 404, {"ok": False, "error": "未知接口"})

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        if not CORS:
            super().log_message(fmt, *args)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[director] listening on http://{HOST}:{PORT} (cors={CORS})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
