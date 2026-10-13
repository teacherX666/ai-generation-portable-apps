"""重启前判据：列出 `runs` 表里 status in ('running','resuming') 的运行（只读）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
con = sqlite3.connect(ROOT / "data" / "agent.sqlite3")
con.row_factory = sqlite3.Row
rows = con.execute(
    "select run_id, status, source_url, created_at, updated_at from runs "
    "where status in ('running', 'resuming')"
).fetchall()
print(f"non-terminal runs: {len(rows)}")
for row in rows:
    print(dict(row))
sys.stdout.flush()
