"""排查 ③ 误伤：为什么这些「明明多镜头」的文档被数成 1 行分镜。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from feishu_generation_agent.domain.document import NormalizedDocument
from feishu_generation_agent.integrations.planner import (
    _STORYBOARD_HEADER,
    _STORYBOARD_ROW_MARKER,
    _STORYBOARD_ROW_NUMBER,
    _storyboard_shot_count,
)

REPO = Path(__file__).resolve().parent
CKPT = REPO / "data" / "checkpoints.sqlite3"
AGENT = REPO / "data" / "agent.sqlite3"
OUT = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\yarn-fix\rule3_probe.txt")

TARGET_RUNS = [
    "1ee2ab1d", "878049d8", "d5fba896", "5f5320e5", "ad625a16",
    "cd8f6e63", "862e0e95", "f9b8c7f0", "04d95082", "e1ede2ae",
    "afd140f3", "3a86ed24", "51cce401", "dbea6b9f",
]


async def main() -> None:
    con = sqlite3.connect(f"file:{AGENT}?mode=ro", uri=True)
    rows = con.execute(
        "select run_id, thread_id, status from runs"
    ).fetchall()
    con.close()
    wanted = {
        r[0][:8]: (r[0], r[1], r[2]) for r in rows if r[0][:8] in TARGET_RUNS
    }
    out: list[str] = []
    add = out.append
    ser = JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)
    async with aiosqlite.connect(f"file:{CKPT}?mode=ro", uri=True) as conn:
        saver = AsyncSqliteSaver(conn, serde=ser)
        for short, (run_id, thread_id, status) in sorted(wanted.items()):
            tup = await saver.aget_tuple(
                {"configurable": {"thread_id": thread_id}}
            )
            if tup is None:
                add(f"{short}: no checkpoint")
                continue
            values = dict(tup.checkpoint.get("channel_values") or {})
            payload = values.get("normalized_document")
            if not isinstance(payload, dict):
                add(f"{short}: no document")
                continue
            doc = NormalizedDocument.model_validate(payload)
            count = _storyboard_shot_count(doc)
            add("=" * 90)
            add(f"{short} status={status} storyboard_shot_count={count} title={doc.title!r}")
            tables = [b for b in doc.blocks if b.block_type == "table"]
            add(f"  tables={len(tables)}")
            for table in tables:
                cells = {
                    b.block_id: b
                    for b in doc.blocks
                    if b.block_type == "table_cell" and b.parent_id == table.block_id
                    and isinstance(b.table_row, int)
                }
                by_row: dict[int, list[tuple[int, str, str]]] = {}
                for cell in cells.values():
                    texts = [
                        b.text
                        for b in doc.blocks
                        if b.block_type == "text" and cell.block_id in b.path
                    ]
                    by_row.setdefault(cell.table_row, []).append(
                        (
                            cell.table_column if cell.table_column is not None else -1,
                            cell.block_id[:8],
                            " | ".join(t[:40] for t in texts if t),
                        )
                    )
                add(f"  --- table {table.block_id[:8]}: {len(by_row)} rows ---")
                for row in sorted(by_row):
                    for col, cid, text in sorted(by_row[row]):
                        flags = []
                        if _STORYBOARD_HEADER.fullmatch(text or ""):
                            flags.append("HEADER")
                        if _STORYBOARD_ROW_MARKER.match(text or ""):
                            flags.append("MARKER")
                        if _STORYBOARD_ROW_NUMBER.fullmatch(text or ""):
                            flags.append("NUMBER")
                        add(
                            f"    row={row} col={col} cell={cid} {'/'.join(flags):12s} {text!r}"
                        )
            add("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print("written", OUT, len(out))


if __name__ == "__main__":
    asyncio.run(main())
