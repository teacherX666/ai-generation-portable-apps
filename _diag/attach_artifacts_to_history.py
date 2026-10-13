#!/usr/bin/env python3
"""把磁盘上还存在的产物，挂回「请求日志恢复」出来的历史记录。

背景：history.json 被毁后，2382 条记录只能从请求日志恢复出「时间/用户/应用」（正文与产物
链接都没了）。但**产物文件本身还在**（seedance 1327 / 图像 1759 / 人像 50），而子应用的
`state/download_files.json` 里还留着「下载 token → 文件路径」的映射。本脚本据此把产物挂回去，
让这些记录能预览/下载。

归属算法（必须说清，因为它是**推断**不是原始事实）：同一 (应用, 用户, 日期) 下的文件，按
文件名里的 `YYYYMMDD_HHMMSS`（没有则用文件 mtime）落在「**最近一条提交时间不晚于它的记录**」
上 —— 即按时间就近归属。原始的任务↔产物对应关系没有留存，所以记录上会标
`results_recovered`，`note` 里也会写明这一点。

**可在 Portal 运行中执行**：history.json 是「读盘→改→写回」，不常驻内存。

用法：
    python _diag/attach_artifacts_to_history.py            # 只报告
    python _diag/attach_artifacts_to_history.py --apply    # 写回（先备份）
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
HISTORY = STATE / "history.json"

APPS = ["seedance", "nano-banana", "volcengine-portrait"]
NAME_TS = re.compile(r"(\d{8})_(\d{6})")
KIND_BY_APP = {"seedance": "video", "volcengine-portrait": "video", "nano-banana": "image"}
MAX_RESULTS = 4


def file_ts(name: str, fallback: float) -> float:
    m = NAME_TS.search(name)
    if not m:
        return fallback
    try:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return fallback


def token_for_path(app: str) -> dict[str, str]:
    """路径（小写、正斜杠归一）→ 下载 token。"""
    p = ROOT / app / "state" / "download_files.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out = {}
    for token, path in data.items():
        out[str(Path(str(path))).lower().replace("\\", "/")] = str(token)
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    history = json.loads(HISTORY.read_text(encoding="utf-8"))

    # 1) 每个 (app,user,day) 的产物（带 token）
    inventory: dict[tuple[str, str, str], list[tuple[float, str]]] = collections.defaultdict(list)
    for app in APPS:
        base = ROOT / app / "outputs"
        if not base.exists():
            continue
        tokens = token_for_path(app)
        for user_dir in base.iterdir():
            if not user_dir.is_dir():
                continue
            for day_dir in user_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                for f in day_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    token = tokens.get(str(f).lower().replace("\\", "/"))
                    if not token:
                        continue
                    inventory[(app, user_dir.name, day_dir.name)].append(
                        (file_ts(f.name, f.stat().st_mtime), token))

    # 2) 待补的记录，按 (app,user,day) 分组并排序
    groups: dict[tuple[str, str, str], list[dict]] = collections.defaultdict(list)
    for rec in history.values():
        if str(rec.get("_recovered_from")) != "request_log":
            continue
        app = str(rec.get("app") or "")
        if app not in APPS:
            continue
        day = dt.datetime.fromtimestamp(float(rec.get("submitted_at") or 0)).strftime("%Y-%m-%d")
        groups[(app, str(rec.get("username") or ""), day)].append(rec)

    linked_records = 0
    linked_files = 0
    orphan_files = 0
    for key, records in groups.items():
        files = sorted(inventory.get(key) or [])
        if not files:
            continue
        records.sort(key=lambda r: float(r.get("submitted_at") or 0))
        buckets: list[list[tuple[float, str]]] = [[] for _ in records]
        for ts, token in files:
            # 最近一条「提交时间不晚于该文件」的记录；早于第一条的都归第一条
            idx = 0
            for i, rec in enumerate(records):
                if float(rec.get("submitted_at") or 0) <= ts:
                    idx = i
                else:
                    break
            buckets[idx].append((ts, token))
        for rec, bucket in zip(records, buckets):
            if not bucket:
                continue
            kind = KIND_BY_APP.get(key[0], "image")
            urls = [{"url": f"/api/download/{token}", "kind": kind} for _, token in bucket[:MAX_RESULTS]]
            rec["results"] = urls
            rec["thumb_url"] = urls[0]["url"]
            rec["results_recovered"] = "by_nearest_preceding_job"
            note = str(rec.get("note") or "")
            extra = "产物已挂回（按时间就近归属，非原始任务对应关系）"
            rec["note"] = (note + "；" + extra) if note else extra
            linked_records += 1
            linked_files += len(bucket)

    total_files = sum(len(v) for v in inventory.values())
    orphan_files = total_files - linked_files
    print(f"产物清单：{total_files} 个带下载 token 的文件，覆盖 {len(inventory)} 个 (应用,用户,日期) 组合")
    print(f"待补记录：{sum(len(v) for v in groups.values())} 条，分布在 {len(groups)} 个组合")
    print(f"可挂回：{linked_records} 条记录（用到 {linked_files} 个文件）；"
          f"未被归属的文件 {orphan_files} 个（多为对不到记录的旧产物）")
    if not apply:
        print("\nDRY-RUN：零写入。加 --apply 才写回。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(HISTORY, HISTORY.with_name(f"{HISTORY.name}.pre-artifacts.{stamp}.bak"))
    tmp = HISTORY.with_name(f"{HISTORY.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(tmp, HISTORY)
            break
        except OSError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
    print(f"\n已写回。备份 history.json.pre-artifacts.{stamp}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
