"""只读：runs 非终态（P0 修复期间用，跑完可删）。"""
import sqlite3

con = sqlite3.connect("file:data/agent.sqlite3?mode=ro", uri=True)
cur = con.cursor()
rows = list(
    cur.execute(
        "select run_id, status, created_at, updated_at from runs "
        "where status in ('running','resuming')"
    )
)
print("NONTERMINAL COUNT:", len(rows))
for r in rows:
    print("  ", r)
print("RECENT:")
for r in cur.execute(
    "select run_id, status, created_at, updated_at from runs "
    "order by rowid desc limit 12"
):
    print("  ", r)
