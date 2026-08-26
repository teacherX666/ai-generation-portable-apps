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


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/config":
            json_response(self, 200, {"ok": True, **config_payload()})
            return
        super().do_GET()

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
