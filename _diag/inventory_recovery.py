#!/usr/bin/env python3
"""盘点：那 2382 条「请求日志恢复」的记录，正文/产物还能不能找回来。

分三类看：
  A. 产物（文件）—— history.json 里存的是下载链接；文件本身若还在磁盘，就能挂回去。
  B. 正文（提示词/模型）—— Portal 从来不落盘请求体，所以只有**应用自己有存储**的才可能有：
     飞书 Agent（sqlite + checkpoint）、AI 短剧（state/runs/<id>/）、各子应用的
     workspaces/*/preset.json（只存「最后一次表单」，不是每次任务）。
  C. 更早的 history 备份（9/2、9/8）—— 里面是**带正文的完整记录**，可作为 ≤9/8 的富恢复源。
"""
from __future__ import annotations

import collections
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
OUT = Path(__file__).resolve().parent / "recovery_inventory.txt"
buf: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def day_of(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


hist = json.loads((STATE / "history.json").read_text(encoding="utf-8"))
by_src = collections.Counter(str(v.get("_recovered_from") or "live") for v in hist.values())
w("== 当前 history.json ==")
w(f"总条数 {len(hist)}；来源分布 {dict(by_src)}")
thin = [v for v in hist.values() if str(v.get("_recovered_from")) == "request_log"]
w(f"其中「请求日志恢复」（只有时间/用户/应用）{len(thin)} 条，覆盖 "
  f"{min(day_of(float(v['submitted_at'])) for v in thin)} .. {max(day_of(float(v['submitted_at'])) for v in thin)}")

# A. 产物
w("")
w("== A. 产物（文件）是否还在 ==")
for app in ["seedance", "nano-banana", "volcengine-portrait", "dreamina"]:
    base = ROOT / app / "outputs"
    if not base.exists():
        w(f"{app:<20} 无 outputs 目录")
        continue
    per_day = collections.Counter()
    per_user = collections.Counter()
    for f in base.rglob("*"):
        if not f.is_file():
            continue
        per_day[f.parent.name] += 1
        per_user[f.parent.parent.name] += 1
    days = sorted(per_day)
    if not days:
        w(f"{app:<20} outputs 目录为空")
        continue
    w(f"{app:<20} {sum(per_day.values()):6d} 个文件，{len(days)} 天（{days[0]} .. {days[-1]}）")
    recent = ", ".join(f"{d}:{per_day[d]}" for d in days[-6:])
    w(f"{'':20} 最近：{recent}")

# download_files.json（token → 路径）
w("")
w("== A2. 下载 token 映射 ==")
for app in ["seedance", "nano-banana", "volcengine-portrait", "dreamina"]:
    p = ROOT / app / "state" / "download_files.json"
    if not p.exists():
        w(f"{app:<20} 无 download_files.json")
        continue
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        w(f"{app:<20} 解析失败")
        continue
    days = collections.Counter()
    for path in data.values():
        parts = Path(str(path)).parts
        for part in parts:
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                days[part] += 1
    ds = sorted(days)
    w(f"{app:<20} {len(data):6d} 条 token；日期覆盖 {ds[0] if ds else '-'} .. {ds[-1] if ds else '-'}")

# 逐条：恢复的记录能不能在产物里找到对应（按 应用+用户+日期）
w("")
w("== A3. 恢复条目 ↔ 产物 的可关联比例（按 应用+用户+日期 归并）==")
artifact: dict[tuple[str, str, str], int] = collections.Counter()
for app in ["seedance", "nano-banana", "volcengine-portrait"]:
    base = ROOT / app / "outputs"
    if not base.exists():
        continue
    for user_dir in base.iterdir():
        if not user_dir.is_dir():
            continue
        for day_dir in user_dir.iterdir():
            if day_dir.is_dir():
                artifact[(app, user_dir.name, day_dir.name)] = sum(
                    1 for x in day_dir.rglob("*") if x.is_file())
linked = 0
for rec in thin:
    key = (str(rec.get("app")), str(rec.get("username")), day_of(float(rec["submitted_at"])))
    if artifact.get(key):
        linked += 1
w(f"{linked} / {len(thin)} 条能按「应用+用户+日期」找到产物目录（{linked * 100 // max(1, len(thin))}%）")

# B. 各应用自己的存储
w("")
w("== B. 有自己持久化存储的应用（正文可能还在）==")
runs = ROOT / "drama-pipeline" / "state" / "runs"
if runs.exists():
    n = sum(1 for p in runs.iterdir() if p.is_dir())
    w(f"AI 短剧 runs: {n} 个（每个含分阶段正文，在 state/runs/<id>/）")
feishu = ROOT / "feishu-generation-agent" / "data"
for name in ("agent.sqlite3", "checkpoints.sqlite3"):
    p = feishu / name
    if p.exists():
        w(f"飞书 Agent {name}: {p.stat().st_size // 1024} KB（run/提示词在库内）")
ws = list(ROOT.glob("*/state/workspaces/u_*"))
w(f"各子应用 workspace preset: {len(ws)} 个（只存「最后一次表单」，不是每次任务）")
src = sorted(ROOT.glob("seedance/state/workspaces/u_*"))
for p in src[:5]:
    f = p / "preset.json"
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        vals = (d.get("values") or {})
        w(f"    例 {p.name}：model={vals.get('model','')} prompt={(str(vals.get('prompt',''))[:24])!r}")

# C. 旧 history 备份
w("")
w("== C. 更早的 history 备份（带正文的完整记录）==")
for p in [STATE / "history.json.20260908-300min-stall.bak",
          ROOT / "_backup_aigapps_20260902_172817" / "portal" / "state" / "history.json"]:
    if not p.exists():
        continue
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        w(f"{p.name}: 解析失败 {exc}")
        continue
    days = sorted(day_of(float(v.get("submitted_at") or 0)) for v in d.values() if isinstance(v, dict))
    with_prompt = sum(1 for v in d.values() if isinstance(v, dict) and str(v.get("prompt") or "").strip())
    w(f"{p.name}: {len(d)} 条，{days[0] if days else '-'} .. {days[-1] if days else '-'}，其中有正文 {with_prompt} 条")

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
