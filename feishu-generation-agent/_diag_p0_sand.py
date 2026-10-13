"""只读：取「刮太空沙」需求的信息 + planner 提示词快照状态（P0 修复期间用）。"""
import glob
import sqlite3

print("=== sqlite files ===")
for path in glob.glob("data/**/*.sqlite3", recursive=True) + glob.glob("data/**/*.db", recursive=True):
    print("  ", path)

con = sqlite3.connect("file:data/agent.sqlite3?mode=ro", uri=True)
cur = con.cursor()
print("=== planner_prompt_profiles ===")
cols = [r[1] for r in cur.execute("PRAGMA table_info(planner_prompt_profiles)")]
print("cols:", cols)
for row in cur.execute("select * from planner_prompt_profiles"):
    d = dict(zip(cols, row))
    text = d.pop("prompt_text", None) or d.pop("prompt", None)
    print("  ", d, "len(prompt)=", len(text) if isinstance(text, str) else None)

print("=== runs with sand source ===")
rcols = [r[1] for r in cur.execute("PRAGMA table_info(runs)")]
for row in cur.execute("select * from runs"):
    d = dict(zip(rcols, row))
    if "AZQhws4jyicD8ekZWI7celKPn2f" in (d.get("source_url") or ""):
        print("  ", {k: d.get(k) for k in ("run_id", "status", "source_url", "owner_user_id", "created_at")})
