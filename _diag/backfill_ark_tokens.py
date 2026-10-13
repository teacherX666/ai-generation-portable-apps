#!/usr/bin/env python3
"""回填 10-08 ~ 10-12 的逐任务 token（方舟任务列表 → 使用记录明细）。

为什么只能补这一段：方舟「查询视频生成任务列表」**只回溯 5~6 天**（实测 1212 条，最早
10-08 09:19）。这正是历史 token 唯一的原始来源 —— 更早的日期任何来源都没有了。

归属口径（**推断，必须校验**）：
  1. **飞书任务精确**：飞书库 `operations.official_id` 就是方舟任务号，一对一。
  2. **自有任务按时间对齐**：把剩下的方舟任务按 `created_at` 挂到「提交时间不晚于它」的
     最近一条历史记录（history.json 里有 时间/用户/应用）。同一秒的多个任务（一次提交多 run）
     自然落到同一条记录上。
  3. **校验**：按天统计「自有任务 token 合计」vs `usage.json.by_user` 的当日 token 合计；
     差额如实打印。对不上的任务**不写记录**，只报数。

用法：
    python _diag/backfill_ark_tokens.py            # dry-run（零写入）
    python _diag/backfill_ark_tokens.py --apply    # 写入 generations.jsonl（先备份）
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "portal"))

import urllib.request  # noqa: E402

from shared.net import UPSTREAM  # noqa: E402
import app as portal_app  # noqa: E402  （import 不会起服务；用它拿路径与用户表）

APPS_VIDEO = {"seedance", "volcengine-portrait"}
MATCH_WINDOW = 1800.0          # 任务创建时间允许比提交时间晚多久（秒）
FEISHU_DB = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"


def fmt(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def ark_tasks() -> dict[str, dict]:
    key = json.loads((ROOT / "seedance" / "state" / "secrets.json").read_text("utf-8")).get(
        "volcengine_api_key", "").strip()
    out: dict[str, dict] = {}
    for page in range(1, 30):
        url = ("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
               f"?page_size=100&page_num={page}")
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with UPSTREAM.open(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            print(f"  第 {page} 页失败：{type(exc).__name__}")
            break
        items = payload.get("items") or []
        if not items:
            break
        for item in items:
            tid = str(item.get("id") or "")
            if not tid:
                continue
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            parts = content.get("content") if isinstance(content.get("content"), list) else []
            out[tid] = {
                "created_at": int(item.get("created_at") or 0),
                "model": str(item.get("model") or ""),
                "resolution": str(item.get("resolution") or ""),
                "duration": int(item.get("duration") or 0),
                "status": str(item.get("status") or ""),
                "tokens": int(usage.get("total_tokens") or usage.get("completion_tokens") or 0),
                "has_input_video": any(str(p.get("type") or "") == "video_url"
                                       for p in parts if isinstance(p, dict)),
            }
        if len(items) < 100:
            break
    return out


def feishu_task_owner() -> dict[str, tuple[str, float]]:
    """方舟任务号 → (Portal 用户名, 提交时间)。飞书库里任务号是精确的。"""
    con = sqlite3.connect(f"file:{FEISHU_DB}?mode=ro", uri=True)
    users = {str(u.get("id") or ""): str(u.get("username") or "")
             for u in portal_app.auth.list_users()}
    out: dict[str, tuple[str, float]] = {}
    q = ("select o.official_id, o.run_id, r.owner_user_id, r.created_at from operations o "
         "join runs r on r.run_id = o.run_id where o.official_id like 'cgt-%'")
    for official_id, _run_id, owner, created in con.execute(q):
        try:
            ts = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        out[str(official_id)] = (users.get(str(owner), ""), ts)
    con.close()
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    print("拉取方舟任务列表…")
    tasks = ark_tasks()
    print(f"  共 {len(tasks)} 条")
    succeeded = {k: v for k, v in tasks.items() if v["tokens"] > 0}
    print(f"  其中成功且有 usage 的 {len(succeeded)} 条，token 合计 {sum(v['tokens'] for v in succeeded.values()):,}")

    owner_map = feishu_task_owner()
    print(f"飞书库里有方舟任务号的 {len(owner_map)} 个（精确归属）")

    hist = json.loads(portal_app.HISTORY_PATH.read_text("utf-8"))
    by_key: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for rec in hist.values():
        by_key[(str(rec.get("app") or ""), str(rec.get("username") or ""))].append(rec)
    for recs in by_key.values():
        recs.sort(key=lambda r: float(r.get("submitted_at") or 0))
    existing_jobs = set()
    if portal_app.GENERATIONS_PATH.exists():
        for line in portal_app.GENERATIONS_PATH.read_text("utf-8").splitlines():
            if line.strip():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (str(row.get("app") or ""), str(row.get("job_id") or ""))
                if row.get("job_id"):
                    existing_jobs.add(key)

    # 归属
    assigned: dict[tuple[str, str], dict] = {}      # (app, job_id) → 汇总行
    feishu_assigned = 0
    unmatched: list[dict] = []
    for tid, info in sorted(succeeded.items(), key=lambda kv: kv[1]["created_at"]):
        ts = float(info["created_at"])
        if tid in owner_map:
            feishu_assigned += 1
            continue
        row = None
        for app in APPS_VIDEO:
            for cand in by_key.get((app, ""), []) or []:
                pass
        # 在所有视频类历史记录里找「提交时间不晚于任务创建时间、且最近」的一条
        best = None
        for (app, _u), recs in by_key.items():
            if app not in APPS_VIDEO:
                continue
            for rec in recs:
                sub = float(rec.get("submitted_at") or 0)
                if sub <= ts and ts - sub <= MATCH_WINDOW:
                    if best is None or sub > best[0]:
                        best = (sub, app, rec)
        if best is None:
            unmatched.append({"id": tid, **info})
            continue
        _sub, app, rec = best
        key = (app, str(rec.get("job_id") or ""))
        bucket = assigned.setdefault(key, {
            "app": app, "job_id": str(rec.get("job_id") or ""),
            "username": str(rec.get("username") or ""), "submitted_at": _sub,
            "tokens": 0, "tasks": [], "models": set(), "resolution": "",
            "has_input_video": False, "duration": 0,
        })
        bucket["tokens"] += info["tokens"]
        bucket["tasks"].append(tid)
        bucket["models"].add(info["model"])
        bucket["resolution"] = bucket["resolution"] or info["resolution"]
        bucket["has_input_video"] = bucket["has_input_video"] or info["has_input_video"]
        bucket["duration"] = max(bucket["duration"], info["duration"])

    print(f"\n归属结果：飞书精确 {feishu_assigned} 条；自有任务落到 {len(assigned)} 个作业上；"
          f"对不上的任务 {len(unmatched)} 条（token {sum(u['tokens'] for u in unmatched):,}）")

    # 校验：按天 vs by_user
    usage = json.loads(portal_app.USAGE_PATH.read_text("utf-8"))
    by_user = usage.get("by_user") or {}
    per_day_assigned = collections.Counter()
    per_day_user = collections.Counter()
    for (app, _job), bucket in assigned.items():
        day = time.strftime("%Y-%m-%d", time.localtime(bucket["submitted_at"]))
        per_day_assigned[day] += bucket["tokens"]
        per_day_user[(day, bucket["username"])] += bucket["tokens"]
    per_day_by_user = collections.Counter()
    per_day_user_by_user = collections.Counter()
    for day, users in by_user.items():
        for uname, apps in (users or {}).items():
            for app, stat in (apps or {}).items():
                tok = int((stat or {}).get("tokens") or 0)
                if tok:
                    per_day_by_user[day] += tok
                    per_day_user_by_user[(day, uname)] += tok

    print("\n校验（按天）：归属出的 token vs by_user 里的 token")
    for day in sorted(set(list(per_day_assigned) + list(per_day_by_user))):
        a = per_day_assigned.get(day, 0)
        b = per_day_by_user.get(day, 0)
        print(f"  {day}  归属={a:>12,}   by_user={b:>12,}   差={a - b:>+12,}")
    print("\n校验（按人，只列有 by_user 的）：")
    for (day, uname), b in sorted(per_day_user_by_user.items()):
        a = per_day_user.get((day, uname), 0)
        print(f"  {day} {uname:<8} 归属={a:>12,}  by_user={b:>12,}  差={a - b:>+12,}")

    if not apply:
        print("\nDRY-RUN：零写入。加 --apply 才写。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(portal_app.GENERATIONS_PATH,
                    portal_app.GENERATIONS_PATH.with_name(
                        f"{portal_app.GENERATIONS_PATH.name}.pre-backfill.{stamp}.bak"))
    written = 0
    with portal_app.GENERATIONS_PATH.open("a", encoding="utf-8") as fh:
        for (app, job_id), bucket in sorted(assigned.items(),
                                            key=lambda kv: kv[1]["submitted_at"]):
            if job_id and (app, job_id) in existing_jobs:
                continue
            model = sorted(bucket["models"])[0] if bucket["models"] else ""
            quote = portal_app.quote_generation(
                model=model, resolution=bucket["resolution"],
                has_input_video=bucket["has_input_video"], tokens=bucket["tokens"], app=app)
            ts = bucket["submitted_at"]
            row = {
                "ts": ts, "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
                "time": fmt(ts), "username": bucket["username"], "app": app,
                "job_id": job_id, "task_ids": bucket["tasks"][:20], "kind": "video",
                "model": model, "provider": "volcengine", "provider_label": "火山方舟",
                "title": "", "resolution": bucket["resolution"],
                "has_input_video": bucket["has_input_video"], "media_fields": [],
                "runs": len(bucket["tasks"]), "images": 0,
                "seconds": bucket["duration"] * len(bucket["tasks"]),
                "tokens": bucket["tokens"],
                "unit_price": quote.get("unit_price"), "unit": quote.get("unit"),
                "amount": quote.get("amount"), "currency": quote.get("currency") or "CNY",
                "price_source": "ark_backfill", "price_family": quote.get("label") or "",
                "status": "succeeded",
                "_recovered_from": "ark_task_list",
                "note": (f"历史回填：token 取自方舟任务记录（{len(bucket['tasks'])} 个任务，"
                         f"按提交时间对齐到该作业）；方舟任务列表只回溯 5~6 天，所以仅补到 10-08 起"),
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    print(f"\n已写入 {written} 条（备份 generations.jsonl.pre-backfill.{stamp}.bak）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
