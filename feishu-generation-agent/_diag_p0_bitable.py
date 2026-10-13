"""只读：production-bitable 快照（用于证明测试 run 没有污染真实用户行）。"""
import hashlib
import json
import sqlite3

con = sqlite3.connect("file:data/production-bitable.sqlite3?mode=ro", uri=True)
cur = con.cursor()
tables = [r[0] for r in cur.execute(
    "select name from sqlite_master where type='table' order by name"
)]
snapshot = {}
for name in tables:
    cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{name}")')]
    rows = [dict(zip(cols, row)) for row in cur.execute(f'select * from "{name}"')]
    snapshot[name] = rows
    print(name, len(rows), cols[:8])
blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
print("SNAPSHOT_SHA256", hashlib.sha256(blob.encode("utf-8")).hexdigest())
with open("_e2e_p0_bitable_snapshot.json", "w", encoding="utf-8") as handle:
    handle.write(blob)
print("--- sand rows in production_tasks ---")
for row in snapshot.get("production_tasks", []):
    if "太空沙" in str(row.get("display_text", "")):
        print(json.dumps(row, ensure_ascii=False)[:600])
