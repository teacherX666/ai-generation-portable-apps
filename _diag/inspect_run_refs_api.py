"""只读：拉 run 31770465 的参考素材 order/role，并核对 inputs 里那个 mp4 是不是孤儿。"""
import json
import sqlite3
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\123\Documents\LTX\ai-generation-portable-apps\feishu-generation-agent")
DATA = ROOT / "data"
RUN = "31770465-b994-4e7e-98f5-187481a00d7e"

c = sqlite3.connect(f"file:{DATA / 'agent.sqlite3'}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
row = c.execute("select * from runs where run_id=?", (RUN,)).fetchone()
print("run:", dict(row) if row else None)
owner = row["owner_user_id"] if row else "prime-local"
c.close()

req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/runs/{RUN}",
    headers={"X-Portal-User-Id": owner},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    payload = json.load(resp)

plan = payload.get("plan") or payload.get("draft_plan") or payload.get("view") or {}
if not isinstance(plan, dict) or not plan.get("tasks"):
    approval = payload.get("approval")
    if isinstance(approval, dict):
        print("approval 顶层键:", list(approval.keys()))
        plan = approval.get("plan") or approval.get("draft_plan") or approval
tasks = plan.get("tasks") if isinstance(plan, dict) else None
if tasks is None:
    print("顶层键:", list(payload.keys()))
    print(json.dumps(payload.get("approval"), ensure_ascii=False)[:1500])
    raise SystemExit(0)

for task in tasks:
    refs = task.get("reference_images") or []
    print(f"\n任务 {task.get('task_id')}  类型={task.get('task_type')}  模式={task.get('reference_mode')}")
    for r in refs:
        print("    asset_id=%s role=%s order=%s" % (r.get("asset_id"), r.get("role"), r.get("order")))

print("\ninputs 目录里的文件:")
for f in sorted((DATA / "runs" / RUN).rglob("*")):
    if f.is_file():
        print("   ", f.name, f.stat().st_size)
