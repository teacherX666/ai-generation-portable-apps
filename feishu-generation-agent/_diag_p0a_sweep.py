"""用**真实实现**扫一遍现网语料：②-a（规则1/规则2）与 ③（单镜头禁止拆分）的命中清单。

输出 UTF-8 文本到仓库外，供人工判读误伤。只读 DB（mode=ro）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from feishu_generation_agent.domain.document import NormalizedDocument
from feishu_generation_agent.domain.reference_contract import (
    duration_consistency_issues,
    single_whole_clip_timeline_hint,
)
from feishu_generation_agent.integrations.planner import (
    _shot_split_directives,
    _single_shot_basis,
    _storyboard_shot_count,
)

REPO = Path(__file__).resolve().parent
OUT = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\yarn-fix\sweep.txt")
AGENT = REPO / "data" / "agent.sqlite3"
CKPT = REPO / "data" / "checkpoints.sqlite3"
MAX_RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 500


def recent_threads() -> list[tuple[str, str, str, str]]:
    con = sqlite3.connect(f"file:{AGENT}?mode=ro", uri=True)
    try:
        return con.execute(
            "select run_id, thread_id, status, created_at from runs "
            "order by created_at desc limit ?",
            (MAX_RUNS,),
        ).fetchall()
    finally:
        con.close()


async def main() -> None:
    rows: list[dict] = []
    ser = JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)
    async with aiosqlite.connect(f"file:{CKPT}?mode=ro", uri=True) as conn:
        saver = AsyncSqliteSaver(conn, serde=ser)
        for run_id, thread_id, status, created_at in recent_threads():
            try:
                tup = await saver.aget_tuple(
                    {"configurable": {"thread_id": thread_id}}
                )
            except Exception:  # noqa: BLE001
                continue
            if tup is None:
                continue
            values = dict(tup.checkpoint.get("channel_values") or {})
            doc_payload = values.get("normalized_document")
            doc = None
            if isinstance(doc_payload, dict) and doc_payload.get("blocks"):
                try:
                    doc = NormalizedDocument.model_validate(doc_payload)
                except Exception:  # noqa: BLE001
                    doc = None
            plan = None
            for key in ("approved_plan", "draft_plan", "task_plan"):
                candidate = values.get(key)
                if isinstance(candidate, dict) and candidate.get("tasks"):
                    plan = candidate
                    break
            if plan is None:
                continue
            for task in plan["tasks"]:
                if task.get("task_type") != "image_to_video":
                    continue
                rows.append(
                    {
                        "run_id": run_id,
                        "status": status,
                        "created_at": created_at,
                        "task_id": task.get("task_id"),
                        "duration": task.get("duration"),
                        "prompt": task.get("prompt") or "",
                        "doc": doc,
                    }
                )

    out: list[str] = []
    add = out.append
    add(f"corpus: {len(rows)} image_to_video tasks (from {MAX_RUNS} recent runs)")
    add("")

    # ---- ②-a 规则 1（阻塞） ----
    rule1 = [
        (r, duration_consistency_issues(r["prompt"], r["duration"])) for r in rows
    ]
    rule1 = [(r, i) for r, i in rule1 if i]
    add(f"=== RULE1 (blocking) hits: {len(rule1)} / {len(rows)} ===")
    for row, issues in rule1:
        add(
            f"- run={row['run_id'][:8]} status={row['status']} "
            f"dur={row['duration']} created={row['created_at'][:16]}"
        )
        for m in __import__("re").finditer(
            r"(?:预计|共|总计|总时长|全片时长|成片时长)\s*[:：为]?\s*"
            r"(\d+(?:\.\d+)?)\s*(?:秒|[sS]\b)",
            row["prompt"],
        ):
            start = max(0, m.start() - 30)
            add(f"      ctx: ...{row['prompt'][start:m.end() + 30]}...".replace("\n", " / "))
    add("")

    # ---- ②-a 规则 2（警告） ----
    rule2 = [
        (r, single_whole_clip_timeline_hint(r["prompt"], r["duration"]))
        for r in rows
    ]
    rule2 = [(r, h) for r, h in rule2 if h]
    ge6 = [r for r in rows if isinstance(r["duration"], int) and r["duration"] >= 6]
    add(f"=== RULE2 (warning) hits: {len(rule2)} / {len(ge6)} tasks with dur>=6 ===")
    add(f"    durations: {Counter(r['duration'] for r, _ in rule2).most_common()}")
    add("")

    # ---- ③ 单镜头禁止拆分 ----
    doc_counts = Counter()
    for row in rows:
        doc_counts[
            _storyboard_shot_count(row["doc"]) if row["doc"] is not None else -1
        ] += 1
    add(f"storyboard_shot_count distribution (-1 = 无文档): {sorted(doc_counts.items())}")
    add("")
    rule3 = []
    for row in rows:
        directives = _shot_split_directives(row["prompt"])
        if not directives:
            continue
        basis = (
            _single_shot_basis(row["doc"], row["prompt"])
            if row["doc"] is not None
            else None
        )
        if basis is not None:
            rule3.append((row, directives, basis))
    add(f"=== RULE3 (blocking at planning) hits: {len(rule3)} / {len(rows)} ===")
    for row, directives, basis in rule3:
        add(
            f"- run={row['run_id'][:8]} status={row['status']} dur={row['duration']} "
            f"basis={basis} directives={directives}"
        )
        add(f"      {row['prompt'][:180]}".replace("\n", " / "))
    add("")
    # 有意放行的样本：有分镜指令但依据不成立（多分镜文档）
    allowed = [
        (r, _shot_split_directives(r["prompt"]))
        for r in rows
        if _shot_split_directives(r["prompt"])
        and (
            r["doc"] is None or _single_shot_basis(r["doc"], r["prompt"]) is None
        )
    ]
    add(f"=== RULE3 NOT triggered (multi-shot docs) : {len(allowed)} ===")
    add(f"    directives seen: {Counter(d for _, ds in allowed for d in ds).most_common(20)}")
    add("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out[:14]))
    print("...")
    print(f"rule1={len(rule1)} rule2={len(rule2)} rule3={len(rule3)} -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
