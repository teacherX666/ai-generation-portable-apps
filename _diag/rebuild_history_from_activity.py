#!/usr/bin/env python3
"""从子应用活动日志重建 Portal 的「创作记录」（history.json）。

背景（2026-10-12 事故）：`history.json` 被非原子写毁掉（固定 tmp 名 + 并发写 → 文件变成
「合法 JSON + 多余 }」），随后 `_load_history()` 解析失败返回 {}，下一次 upsert 就把整个
文件重写成 1~2 条 —— 2554 条历史记录丢了。磁盘上没有任何 2.5MB 的副本。

可恢复源：各子应用的 `state/activity_log.json`（每个保留最近 100 条，字段齐全：
job_id / username / title / prompt / model / 参数 / 状态 / 起止时间 / 产物）。
恢复量：seedance 100 + nano-banana 100 + volcengine-portrait 57 = 257 条。

**可以在 Portal 运行中执行**：history.json 不像 usage.json 那样常驻内存，它是每次
「读盘 → 改 → 写回」，所以写进去的内容不会被内存快照覆盖。

用法：
    python _diag/rebuild_history_from_activity.py            # 只报告
    python _diag/rebuild_history_from_activity.py --apply    # 合并写回（先备份）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
HISTORY = STATE / "history.json"
LOGS = STATE / "logs"

APPS = ["seedance", "nano-banana", "volcengine-portrait", "dreamina", "director", "previz"]
KIND_BY_APP = {"seedance": "video", "volcengine-portrait": "video", "director": "video",
               "nano-banana": "image", "dreamina": "image", "previz": "image"}
STATUS_DONE = {"succeeded", "done", "completed", "success"}
STATUS_FAILED = {"failed", "cancelled", "canceled", "error"}
STATUS_RUNNING = {"running", "processing", "generating", "uploading"}
PARAM_WHITELIST = {"duration", "resolution", "ratio", "aspect_ratio", "model", "custom_model",
                   "seed", "generate_audio", "image_size", "mode", "count", "watermark",
                   "repeat_count", "concurrency", "task_mode", "output_name"}


def norm_status(raw: str) -> str:
    s = (raw or "").lower()
    if s in STATUS_DONE:
        return "done"
    if s in STATUS_FAILED:
        return "failed"
    if s in STATUS_RUNNING:
        return "running"
    return "queued"


def epoch(text: str) -> float:
    try:
        return time.mktime(time.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return time.time()


def kind_for(app: str, result: dict) -> str:
    for run in (result.get("results") or []):
        name = str(run.get("filename") or run.get("download_url") or "")
        if name.lower().endswith((".mp4", ".mov", ".webm")):
            return "video"
    return KIND_BY_APP.get(app, "image")


def results_for(result: dict, kind: str) -> list[dict]:
    items = []
    for run in (result.get("results") or [])[:4]:
        url = run.get("download_url") or run.get("url") or run.get("image_url")
        if not url:
            images = run.get("images")
            if isinstance(images, list) and images and isinstance(images[0], dict):
                url = images[0].get("download_url") or images[0].get("image_url")
        if url:
            items.append({"url": str(url), "kind": kind})
    return items


def from_activity(app: str) -> list[dict]:
    path = ROOT / app / "state" / "activity_log.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else (data.get("records") or [])
    out = []
    for rec in records:
        job_id = str(rec.get("job_id") or "")
        if not job_id:
            continue
        result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
        values = ((rec.get("request") or {}).get("values") or {}) if isinstance(rec.get("request"), dict) else {}
        kind = kind_for(app, result)
        params = {}
        for key, val in values.items():
            if str(key).lower() in PARAM_WHITELIST and isinstance(val, (str, int, float, bool)):
                text = str(val)
                if len(text) <= 80:  # 防止把长串塞进 params
                    params[str(key).lower()] = val
        finished = rec.get("finished_at")
        out.append({
            "app": app,
            "job_id": job_id,
            "username": str(rec.get("username") or ""),
            "kind": kind,
            "activity_id": str(rec.get("id") or ""),
            "title": str(rec.get("title") or "")[:200],
            "prompt": str(values.get("prompt") or rec.get("title") or "")[:2000],
            "model": str(rec.get("model") or "")[:200],
            "params": params,
            "status": norm_status(str(rec.get("status") or "")),
            "submitted_at": epoch(str(rec.get("created_at") or "")),
            "completed_at": float(finished) if isinstance(finished, (int, float)) else None,
            "duration": int(result.get("duration") or 0),
            "thumb_url": "",
            "results": results_for(result, kind),
            "error": str(rec.get("error") or "")[:500],
            "_recovered_from": "activity_log",
        })
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    existing = {}
    if HISTORY.exists():
        try:
            existing = json.loads(HISTORY.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"当前 history.json 解析失败（{exc}），将只写入恢复集")
            existing = {}

    recovered: dict[str, dict] = {}
    for app in APPS:
        for record in from_activity(app):
            recovered[f"{record['app']}:{record['job_id']}"] = record

    print(f"当前 history.json: {len(existing)} 条")
    print(f"活动日志可恢复: {len(recovered)} 条")
    only_new = [k for k in recovered if k not in existing]
    print(f"其中 {len(only_new)} 条是当前文件里没有的（会补齐）")
    by_app: dict[str, int] = {}
    for key in recovered:
        by_app[key.split(":", 1)[0]] = by_app.get(key.split(":", 1)[0], 0) + 1
    print("按应用:", by_app)

    merged = dict(existing)
    for key, record in recovered.items():
        if key not in merged:          # 已有的（在线采集的）记录优先，绝不覆盖
            merged[key] = record

    print(f"合并后: {len(merged)} 条")
    if not apply:
        print("\nDRY-RUN：零写入。加 --apply 才写回。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if HISTORY.exists():
        shutil.copyfile(HISTORY, HISTORY.with_name(f"{HISTORY.name}.pre-rebuild.{stamp}.bak"))
    tmp = HISTORY.with_name(f"{HISTORY.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(tmp, HISTORY)
            break
        except OSError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
    print(f"\n已写回 {len(merged)} 条。备份 history.json.pre-rebuild.{stamp}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
