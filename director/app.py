#!/usr/bin/env python3
"""导演台子应用：提示词优化/扩写（DeepSeek）+ 文生图（火山方舟 Seedream）。

与 nano-banana 同款 stdlib 骨架；由 portal 按 apps.json 拉起
（env: PORT / HOST / CORS / VOLCENGINE_ARK_API_KEY）。
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
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

# 导演台 v2 词库资产（来源见 tools/extract_director_assets.py 与各 JSON 的 source 字段）
ASSETS_DIR = ROOT / "assets"
ASSETS_VERSION = "2026-08-28-v2"
_ASSET_FILES = {
    "gpt_image_templates": "gpt_image_templates.json",
    "nano_banana_styles": "nano_banana_styles.json",
    "shortcut_inspirations": "shortcut_inspirations.json",
    "negative_tags": "negative_tags.json",
}


def assets_payload() -> dict[str, Any]:
    out: dict[str, Any] = {"version": ASSETS_VERSION}
    for key, fname in _ASSET_FILES.items():
        path = ASSETS_DIR / fname
        try:
            out[key] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            out[key] = {}
    return out

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


def _download_image(url: str, dest: Path) -> None:
    """下载生成结果到本地文件；失败抛 APIError。"""
    req = urllib.request.Request(url, headers={"User-Agent": "director/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
    except Exception as exc:
        raise APIError(502, "生成结果下载失败", str(exc)) from exc


def _run_text2image(job_id: str, prompt: str, aspect_ratio: str,
                    count: int, resolution: str) -> None:
    """在线程中执行：逐张调方舟 Seedream（同步接口）并落盘。"""
    job = JOBS[job_id]
    ark = PROVIDERS.get("ark", {})
    base_url = ark.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
    model = ark.get("model", "doubao-seedream-5-0-pro-260628")
    api_key = _ark_key()
    try:
        size = seedream_size(resolution, aspect_ratio)
    except ValueError as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        body = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "response_format": "url",
            "output_format": "png",
            "watermark": False,
        }
        try:
            result = request_json(
                "POST", f"{base_url}/images/generations", api_key, body, timeout=180,
            )
            items = result.get("data") or []
            if not items:
                raise APIError(502, "方舟未返回图片结果", json.dumps(result, ensure_ascii=False)[:300])
            dest = OUTPUT_DIR / f"{job_id}-{index}.png"
            _download_image(items[0].get("url") or "", dest)
            job["results"].append({"index": index, "url": f"/outputs/{dest.name}"})
        except APIError as exc:
            job["status"] = "failed"
            job["error"] = exc.message
            return
    job["status"] = "done"


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
    if mode == "langgpt":
        mode_text = (
            "把上面的内容改写成 LangGPT 结构化提示词：\n"
            "# Role（角色定义，一句话）\n## Profile（专业背景/能力）\n"
            "## Rules（规则：输出格式、禁止事项）\n"
            "## Workflow（步骤）\n## Initialization（开场白）\n"
            "保持用户原意，直接输出结构化结果，不要解释。"
        )
    else:
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
            # 推理模型长指令延迟可达 2-3 分钟（上游 c5d1c10 同款放宽 30s→180s）
            "POST", f"{DEEPSEEK_BASE}/chat/completions", api_key, body, timeout=180,
        )
        content = (result.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content.strip():
            return {"ok": False, "error": "DeepSeek 返回了空结果，请重试"}
        return {"ok": True, "prompt": content.strip()}
    except APIError as exc:
        return {"ok": False, "error": exc.message}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                json_response(self, 404, {"ok": False, "error": "任务不存在"})
                return
            json_response(self, 200, {"ok": True, **job})
            return
        if self.path.startswith("/outputs/"):
            self._serve_output()
            return
        if self.path == "/api/assets":
            json_response(self, 200, {"ok": True, **assets_payload()})
            return
        if self.path == "/api/config":
            json_response(self, 200, {"ok": True, **config_payload()})
            return
        super().do_GET()

    def _serve_output(self) -> None:
        name = Path(urllib.parse.unquote(self.path.rsplit("/", 1)[-1])).name
        path = OUTPUT_DIR / name
        if not path.exists() or not path.is_file():
            json_response(self, 404, {"ok": False, "error": "文件不存在"})
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/jobs":
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                json_response(self, 400, {"ok": False, "error": "请求格式异常"})
                return
            prompt = str(data.get("prompt", "")).strip()
            if not prompt:
                json_response(self, 400, {"ok": False, "error": "请先输入提示词"})
                return
            aspect_ratio = str(data.get("aspect_ratio", "1:1")).strip()
            if aspect_ratio not in ASPECT_RATIOS:
                json_response(self, 400, {"ok": False, "error": f"不支持的比例：{aspect_ratio}"})
                return
            try:
                count = max(1, min(int(data.get("count", 1)), 4))
            except (TypeError, ValueError):
                count = 1
            resolution = str(data.get("resolution", "2K")).strip()
            if not _ark_key():
                json_response(self, 503, {"ok": False, "error": "方舟 Ark key 未配置，请联系管理员"})
                return
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "status": "pending", "prompt": prompt,
                   "aspect_ratio": aspect_ratio, "count": count,
                   "resolution": resolution, "results": [], "error": ""}
            with JOBS_LOCK:
                JOBS[job_id] = job
            threading.Thread(
                target=_run_text2image,
                args=(job_id, prompt, aspect_ratio, count, resolution),
                daemon=True,
            ).start()
            # X-Job-Id 让 portal 按「张」登记用量（统计红线）
            json_response(self, 200, {"ok": True, "job_id": job_id},
                          extra_headers={"X-Job-Id": job_id})
            return
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
