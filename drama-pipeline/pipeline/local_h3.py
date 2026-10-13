"""本地 MiniMax H3 出片客户端（直连本机 AI Port 的 video_local 模块）。

设计原则
--------
- **只用本地**：provider 固定 `comfyui_local`，本模块不存在任何云 provider 分支。
  出片这一步的云费用恒为 0。
- **参数取本机实测值**，不从文档抄。默认值来自本机 AI Port 的 provider 默认表 +
  一次真实跑成功过的 t2v 记录（96 帧 @0.4MP、8 步、cfg 1.0、sageattn → 143 秒）。
- **出站走直连 opener**（`shared.net.UPSTREAM`）：这台机器的系统代理是 SOCKS，
  stdlib urllib 读注册表后会把本地 127.0.0.1 请求也送去代理并失败。
- 调用契约（已用源码核对，**不要照抄 schema 里那个 example，它是过时的**）：
    POST {base}/api/video_local/jobs/json
        body = {"values": {...全部字符串...}, "files": {...}, "request_id": "..."}
      —— 顶层的 values/files 两段式；扁平写法会被 values_files_from_json 丢掉。
    GET  {base}/api/video_local/jobs/{job_id}
    GET  {base}/api/download/{token}[?inline=1]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parent
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared.net import UPSTREAM  # noqa: E402

MODULE_ID = "video_local"
# 唯一允许的 provider —— 本地 ComfyUI。没有云端分支。
LOCAL_PROVIDER = "comfyui_local"
H3_MODEL_KIND = "minimax_h3_all_reference"

# 本机 AI Port 的 H3 加速参数。来源：8801 /api/video_local/schema 的 provider 默认值
# + 2026-09-20 11:51 一条真实成功记录（t2v，96 帧，耗时 143 秒）。
H3_DEFAULTS: dict[str, Any] = {
    "provider": LOCAL_PROVIDER,
    "model_kind": H3_MODEL_KIND,
    "fps": 24,
    "num_frames": 96,            # 96/24fps = 4 秒
    "steps": 8,                  # LightX2V 8 步加速
    "cfg": 1.0,
    "h3_aspect_ratio": "9:16 (Portrait Widescreen)",
    "h3_megapixels": 0.4,        # 一采 0.4MP
    "h3_ref_image_size": "max",
    "h3_multiple": 32,
    "h3_second_stage_enabled": True,   # 二采放大
    "h3_second_stage_first_megapixels": 0.4,
    "h3_second_stage_megapixels": 0.9,
    "h3_second_stage_steps": 4,
    "h3_second_stage_denoise": 0.2,
    "attention_mode": "sageattn",
    "blocks_to_swap": 25,
    "h3_prompt_enhancer_enabled": False,   # 提示词由本模块第 8 阶段产出，不再让 H3 二次改写
    "repeat_count": 1,
    "concurrency": 1,
    "poll_interval": 5,
    "timeout": 3600,
    "vary_seed": False,
}

# 上下文化名 —— 用户可读的时长/画幅预设
DURATION_PRESETS: tuple[dict[str, Any], ...] = (
    {"id": "4s", "label": "4 秒（96 帧，最省时）", "num_frames": 96},
    {"id": "6s", "label": "6 秒（144 帧）", "num_frames": 144},
    {"id": "8s", "label": "8 秒（192 帧）", "num_frames": 192},
    {"id": "12s", "label": "12 秒（288 帧，整段直出）", "num_frames": 288},
)
RATIO_PRESETS: tuple[dict[str, str], ...] = (
    {"id": "9:16 (Portrait Widescreen)", "label": "竖屏 9:16（短剧默认）"},
    {"id": "16:9 (Widescreen)", "label": "横屏 16:9"},
    {"id": "1:1 (Square)", "label": "方形 1:1"},
)
# 参考图数量 → H3 任务模式（已核对 MINIMAX_H3_TASK_MODES = {t2v,i2v,flf2v,ref2v}）
MODE_BY_IMAGE_COUNT = {0: "t2v", 1: "i2v", 2: "flf2v"}
MODE_LABELS = {
    "t2v": "文生视频",
    "i2v": "首帧图生视频",
    "flf2v": "首尾帧视频",
    "ref2v": "全参考模式（多图/多视频/多音频）",
}
MAX_REF_IMAGES = 9
MAX_REF_VIDEOS = 3
MAX_REF_AUDIOS = 3


class LocalH3Error(RuntimeError):
    """本地出片失败，message 已是可直接展示给用户的中文。"""


# ───────────────────────── 错误人话化 ─────────────────────────

_PROXY_HINT = (
    "本机 ComfyUI 被系统代理掐断了：这台机器开着 SOCKS 代理（Jamjams 之类），"
    "Python 的 urllib 会把 127.0.0.1 的请求也送去代理，于是报 "
    "`unknown url type: socks4`。"
    "处理办法：关掉系统代理/加速器后重试（本地出片本来就不需要走代理）。"
)


def humanize_error(text: str) -> str:
    """把上游一长串 traceback 压成用户能直接照做的一句话。"""
    raw = str(text or "").strip()
    low = raw.lower()
    if "socks4" in low or "unknown url type: socks" in low:
        return _PROXY_HINT
    if "comfyui 未启动" in low or "comfyui 未" in low or "connection refused" in low:
        return ("本机 ComfyUI(8188) 没起来。先启动 ComfyUI 再重试；"
                "若已在运行，检查是否被系统代理拦了。")
    if "任务队列已满" in raw:
        return raw
    if "invalid_request" in low:
        return "本地网关拒绝了这次请求（参数不合法）：" + raw[:300]
    if "out of memory" in low or "cuda oom" in low:
        return ("显存不够。把「一采分辨率」降到 0.3MP、或把时长从 12 秒降到 4 秒，"
                "或先关掉其它占用显存的程序。")
    if "not found" in low or "404" in low:
        return "本地网关接口返回 404：AI Port(8801) 可能没起来，或 video_local 模块缺失。"
    # 去掉 traceback 噪音，只留最后一行异常
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    tail = lines[-1] if lines else raw
    return tail[:400]


# ───────────────────────── 网关地址 ─────────────────────────

def _base_candidates() -> list[str]:
    out: list[str] = []
    env = (os.environ.get("DRAMA_LOCAL_GATEWAY") or "").strip()
    if env:
        out.append(env.rstrip("/"))
    out.append("http://127.0.0.1:8801")
    try:
        from shared import local_gateway  # 仓库现成的配置解析（config/local_ai.env）

        resolved = local_gateway.resolved_url()
        if resolved and resolved not in out:
            out.append(resolved)
    except Exception:
        pass
    return out


_CACHED_BASE: str = ""
_CACHED_AT: float = 0.0
_BASE_TTL = 30.0


def gateway_base(force: bool = False) -> str:
    """第一个能应答 /api/modules 的候选地址；带 30 秒缓存。"""
    global _CACHED_BASE, _CACHED_AT
    now = time.time()
    if not force and _CACHED_BASE and now - _CACHED_AT < _BASE_TTL:
        return _CACHED_BASE
    for base in _base_candidates():
        try:
            _request_json("GET", f"{base}/api/modules", timeout=3)
            _CACHED_BASE = base
            _CACHED_AT = now
            return base
        except Exception:
            continue
    _CACHED_BASE = _base_candidates()[0]
    _CACHED_AT = now
    return _CACHED_BASE


# ───────────────────────── HTTP ─────────────────────────

def _request_json(
    method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 30
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with UPSTREAM.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = ""
        try:
            detail = json.loads(raw).get("error") or json.loads(raw).get("message") or ""
        except Exception:
            detail = raw[:200]
        raise LocalH3Error(humanize_error(detail or f"HTTP {exc.code}")) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, str):
            reason = humanize_error(reason)
        raise LocalH3Error(f"连不上本地网关：{reason}") from exc
    except (TimeoutError, ConnectionResetError, OSError) as exc:
        raise LocalH3Error(f"本地网关连接中断：{exc}") from exc


def health(timeout: float = 4) -> dict[str, Any]:
    """给 UI 用的本机出片能力快照。任何失败都不抛，只如实报告。"""
    base = gateway_base(force=True)
    info: dict[str, Any] = {
        "base_url": base,
        "provider": LOCAL_PROVIDER,
        "model_kind": H3_MODEL_KIND,
        "cloud_used": False,
        "ready": False,
        "error": "",
        "modules": [],
        "queue": None,
    }
    try:
        data = _request_json("GET", f"{base}/api/modules", timeout=timeout)
        info["modules"] = [m.get("id") for m in (data.get("modules") or []) if isinstance(m, dict)]
        info["ready"] = MODULE_ID in info["modules"]
        if not info["ready"]:
            info["error"] = f"AI Port 里没有 {MODULE_ID} 模块"
    except LocalH3Error as exc:
        info["error"] = str(exc)
        return info
    try:
        q = _request_json("GET", f"{base}/api/queue", timeout=timeout)
        info["queue"] = q.get("count")
    except Exception:
        pass
    return info


def health_snapshot(timeout: float = 4) -> dict[str, Any]:
    """health() 的对外包装：补上默认参数与预设，供 /api/config 使用。"""
    info = health(timeout)
    info["defaults"] = dict(H3_DEFAULTS)
    info["durations"] = [dict(d) for d in DURATION_PRESETS]
    info["ratios"] = [dict(r) for r in RATIO_PRESETS]
    info["mode_labels"] = dict(MODE_LABELS)
    info["limits"] = {
        "images": MAX_REF_IMAGES,
        "videos": MAX_REF_VIDEOS,
        "audios": MAX_REF_AUDIOS,
    }
    return info


# ───────────────────────── 参数推导 ─────────────────────────

def normalize_mode(requested: str, image_count: int) -> str:
    """定 h3_task_mode。用户显式给了就尊重（非法值忽略），否则按参考图数量推。"""
    want = str(requested or "").strip().lower()
    if want in MODE_LABELS:
        if want == "i2v" and image_count < 1:
            return "t2v"
        if want == "flf2v" and image_count < 2:
            return "i2v" if image_count == 1 else "t2v"
        return want
    if image_count >= 3:
        return "ref2v"
    return MODE_BY_IMAGE_COUNT.get(image_count, "ref2v")


def build_values(
    *,
    prompt: str,
    mode: str,
    duration_id: str = "",
    ratio: str = "",
    megapixels: float | None = None,
    second_stage: bool | None = None,
    seed: int | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, str]:
    """组装 AI Port 要的 values（**全部转成字符串**，它那边会 str() 一遍）。"""
    values: dict[str, Any] = dict(H3_DEFAULTS)
    values["prompt"] = str(prompt or "").strip()
    values["h3_task_mode"] = mode
    if not values["prompt"]:
        raise LocalH3Error("提示词为空，无法出片")

    for preset in DURATION_PRESETS:
        if preset["id"] == duration_id:
            values["num_frames"] = int(preset["num_frames"])
            break
    for preset in RATIO_PRESETS:
        if preset["id"] == ratio:
            values["h3_aspect_ratio"] = preset["id"]
            break
    if megapixels is not None:
        values["h3_megapixels"] = max(0.2, min(2.0, float(megapixels)))
    if second_stage is not None:
        values["h3_second_stage_enabled"] = bool(second_stage)
    if seed is not None:
        values["seed"] = int(seed)

    for key, value in (overrides or {}).items():
        if key in ("prompt", "provider", "model_kind"):
            continue  # 这三个不允许被覆盖：provider 必须锁死本地
        if key in H3_DEFAULTS or key in ("seed", "negative_prompt"):
            values[key] = value

    # 布尔统一成 AI Port 认的写法，其余一律 str()
    out: dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = "" if value is None else str(value)
    return out


def build_files(images: list[dict[str, str]] | None) -> dict[str, dict[str, str]]:
    """参考图 → h3_image_0..8。AI Port 只认 data_url（源码 media_item_to_file 已核对，
    schema 里写的 url 字段其实没实现），所以这里只接受 data URL。"""
    files: dict[str, dict[str, str]] = {}
    for index, item in enumerate((images or [])[:MAX_REF_IMAGES]):
        data_url = str((item or {}).get("data_url") or "").strip()
        if not data_url.startswith("data:"):
            continue
        entry: dict[str, str] = {"data_url": data_url}
        name = str((item or {}).get("filename") or "").strip()
        if name:
            entry["filename"] = name
        files[f"h3_image_{index}"] = entry
    return files


# ───────────────────────── 提交 / 查询 / 下载 ─────────────────────────

def submit(
    *,
    prompt: str,
    mode: str,
    images: list[dict[str, str]] | None = None,
    request_id: str = "",
    duration_id: str = "",
    ratio: str = "",
    megapixels: float | None = None,
    second_stage: bool | None = None,
    seed: int | None = None,
    overrides: dict[str, Any] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    """向本机 AI Port 提交一次 H3 出片。返回 {job_id, deduplicated, values}。"""
    values = build_values(
        prompt=prompt,
        mode=mode,
        duration_id=duration_id,
        ratio=ratio,
        megapixels=megapixels,
        second_stage=second_stage,
        seed=seed,
        overrides=overrides,
    )
    files = build_files(images)
    payload: dict[str, Any] = {"values": values, "files": files}
    if request_id:
        payload["request_id"] = request_id

    base = gateway_base()
    data = _request_json("POST", f"{base}/api/{MODULE_ID}/jobs/json", payload, timeout=timeout)
    job_id = str(data.get("job_id") or "")
    if not job_id:
        raise LocalH3Error("本地网关没有返回 job_id：" + json.dumps(data, ensure_ascii=False)[:200])
    return {
        "job_id": job_id,
        "deduplicated": bool(data.get("deduplicated")),
        "values": values,
        "images": len(files),
    }


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "canceled", "error"}


def job_status(job_id: str, timeout: float = 15) -> dict[str, Any]:
    """查一次任务。返回 {status, done, ok, results, events, error}。"""
    base = gateway_base()
    data = _request_json("GET", f"{base}/api/{MODULE_ID}/jobs/{urllib.parse.quote(job_id)}", timeout=timeout)
    status = str(data.get("status") or "").lower()
    results = data.get("results") or []
    first = results[0] if results and isinstance(results[0], dict) else {}
    error = ""
    if status in TERMINAL_STATUSES and status != "succeeded":
        error = humanize_error(str(first.get("error") or data.get("error") or status))
    elif first and first.get("ok") is False:
        status = "failed"
        error = humanize_error(str(first.get("error") or ""))
    return {
        "status": status,
        "done": status in TERMINAL_STATUSES,
        "ok": status == "succeeded",
        "results": results,
        "events": [e.get("message") for e in (data.get("events") or []) if isinstance(e, dict)][-12:],
        "error": error,
        "raw": data,
    }


def output_of(job: dict[str, Any]) -> dict[str, str]:
    """从任务里取出成片地址（download_url 是 AI Port 的相对路径）。"""
    results = job.get("results") or []
    if not results or not isinstance(results[0], dict):
        return {}
    first = results[0]
    if not first.get("ok"):
        return {}
    download_url = str(first.get("download_url") or "")
    return {
        "download_url": download_url,
        "absolute_url": f"{gateway_base()}{download_url}" if download_url else "",
        "filename": str(first.get("filename") or ""),
        "local_path": str(first.get("local_path") or ""),
        "seed": str(first.get("seed") or ""),
    }


def cancel(job_id: str, timeout: float = 15) -> dict[str, Any]:
    base = gateway_base()
    return _request_json(
        "POST", f"{base}/api/{MODULE_ID}/jobs/{urllib.parse.quote(job_id)}/cancel", {}, timeout=timeout
    )


def download_to(download_url: str, dest: Path, timeout: float = 600) -> int:
    """把成片抓到本地。download_url 形如 /api/download/<token>。"""
    if not download_url:
        raise LocalH3Error("任务没有返回下载地址")
    url = download_url if download_url.startswith("http") else f"{gateway_base()}{download_url}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, method="GET")
    try:
        with UPSTREAM.open(req, timeout=timeout) as resp, tmp.open("wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise LocalH3Error(f"下载成片失败：HTTP {exc.code}") from exc
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise LocalH3Error(f"下载成片失败：{exc}") from exc
    os.replace(tmp, dest)
    return dest.stat().st_size


def request_key(run_id: str, shot_index: int, payload_hash: str = "") -> str:
    """幂等键：同一个 run 的同一个镜头，重复提交会被 AI Port 归并而不是重复排队。"""
    parts = f"drama:{run_id}:{shot_index}:{payload_hash}"
    import hashlib

    return f"drama-{hashlib.sha256(parts.encode('utf-8')).hexdigest()[:32]}"
