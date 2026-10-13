"""②-a / ③ 回归语料收集（只读）。

从 data/checkpoints.sqlite3 按 runs.thread_id（走 PK 索引）精确取每个 run 的最新
checkpoint，抽出所有 image_to_video 任务的 (prompt, duration, 任务来源)。

只读：sqlite 全部用 `file:...?mode=ro` URI 打开；不写回任何库。

输出 JSON 到仓库外的 _quality_analysis/yarn-fix/corpus.json。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

REPO = Path(__file__).resolve().parent
CKPT = REPO / "data" / "checkpoints.sqlite3"
AGENT = REPO / "data" / "agent.sqlite3"
OUT = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\yarn-fix\corpus.json")

MAX_RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 500


def recent_threads() -> list[dict]:
    con = sqlite3.connect(f"file:{AGENT}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "select run_id, thread_id, status, created_at, source_url "
            "from runs order by created_at desc limit ?",
            (MAX_RUNS,),
        )
        return [
            {
                "run_id": row[0],
                "thread_id": row[1],
                "status": row[2],
                "created_at": row[3],
                "source_url": row[4],
            }
            for row in cur.fetchall()
        ]
    finally:
        con.close()


def pick_plan(values: dict) -> dict:
    for key in ("approved_plan", "draft_plan", "task_plan"):
        candidate = values.get(key)
        if isinstance(candidate, dict) and candidate.get("tasks"):
            return {"key": key, "plan": candidate}
    return {}


async def main() -> None:
    threads = recent_threads()
    print(f"runs considered: {len(threads)}")
    serializer = JsonPlusSerializer(
        pickle_fallback=False, allowed_msgpack_modules=None
    )
    rows: list[dict] = []
    async with aiosqlite.connect(f"file:{CKPT}?mode=ro", uri=True) as connection:
        saver = AsyncSqliteSaver(connection, serde=serializer)
        for item in threads:
            try:
                tup = await saver.aget_tuple(
                    {"configurable": {"thread_id": item["thread_id"]}}
                )
            except Exception as exc:  # noqa: BLE001
                print("skip", item["run_id"], type(exc).__name__, exc)
                continue
            if tup is None:
                continue
            values = dict(
                tup.checkpoint.get("channel_values")
                or tup.checkpoint.get("values")
                or {}
            )
            found = pick_plan(values)
            if not found:
                continue
            for task in found["plan"]["tasks"]:
                if task.get("task_type") != "image_to_video":
                    continue
                rows.append(
                    {
                        "run_id": item["run_id"],
                        "run_status": item["status"],
                        "created_at": item["created_at"],
                        "source_url": item["source_url"],
                        "plan_key": found["key"],
                        "task_id": task.get("task_id"),
                        "duration": task.get("duration"),
                        "prompt": task.get("prompt") or "",
                        "user_intent": task.get("user_intent") or "",
                        "warnings": task.get("warnings") or [],
                        "blocking_issues": task.get("blocking_issues") or [],
                    }
                )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"image_to_video tasks: {len(rows)} -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
