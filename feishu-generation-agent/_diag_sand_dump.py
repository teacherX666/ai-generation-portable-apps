"""刮太空沙（③ 的真实案例）：导出各 run 的分镜表 + 生成的提示词，UTF-8 落盘。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

REPO = Path(__file__).resolve().parent
CKPT = REPO / "data" / "checkpoints.sqlite3"
AGENT = REPO / "data" / "agent.sqlite3"
PB = REPO / "data" / "production-bitable.sqlite3"
OUT = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\yarn-fix\sand_evidence.txt")

RECORD = "recvwGSDh9ewHm"


def threads() -> list[tuple[str, str, str, str, str]]:
    con = sqlite3.connect(f"file:{PB}?mode=ro", uri=True)
    rows = con.execute(
        "select run_id, thread_id, status, created_at, display_text from "
        "production_task_history where source_record_id=? order by created_at",
        (RECORD,),
    ).fetchall()
    rows += con.execute(
        "select run_id, thread_id, status, created_at, display_text from "
        "production_tasks where source_record_id=? order by created_at",
        (RECORD,),
    ).fetchall()
    con.close()
    return rows


async def main() -> None:
    out: list[str] = []
    add = out.append
    ser = JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)
    async with aiosqlite.connect(f"file:{CKPT}?mode=ro", uri=True) as conn:
        saver = AsyncSqliteSaver(conn, serde=ser)
        for run_id, thread_id, status, created_at, _text in threads():
            add("=" * 100)
            add(f"run={run_id} status={status} created={created_at}")
            tup = await saver.aget_tuple(
                {"configurable": {"thread_id": thread_id}}
            )
            if tup is None:
                add("  <no checkpoint>")
                continue
            values = dict(tup.checkpoint.get("channel_values") or {})
            doc = values.get("normalized_document") or {}
            add(f"  document_title={doc.get('title')!r} rev={doc.get('revision')}")
            add("  --- normalized_document.text_view ---")
            add("  " + (doc.get("text_view") or "").replace("\n", "\n  ")[:4000])
            add("  --- blocks (table/table_cell/text in tables) ---")
            for block in doc.get("blocks") or []:
                if block.get("block_type") in {"table", "table_cell"}:
                    add(
                        f"    [{block.get('block_type')}] row={block.get('table_row')} "
                        f"col={block.get('table_column')} text={block.get('text')!r}"
                    )
            for key in ("approved_plan", "draft_plan", "task_plan"):
                plan = values.get(key)
                if not isinstance(plan, dict):
                    continue
                for task in plan.get("tasks") or []:
                    if task.get("task_type") != "image_to_video":
                        continue
                    add(f"  --- {key} task={task.get('task_id')} dur={task.get('duration')} ---")
                    add("  " + (task.get("prompt") or "").replace("\n", "\n  "))
                    add(f"    warnings={task.get('warnings')}")
            add("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print("written", OUT, len(out), "lines")


if __name__ == "__main__":
    asyncio.run(main())
