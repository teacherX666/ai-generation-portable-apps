"""真机回放：证明「重启续跑」真的能接回已经提交过的方舟任务（0 元、不重投）。

做法：拿一个**真实存在**的方舟 task_id，在**临时 state 目录**里伪造「重启前正在跑」
的现场（activity + backlog[stage=started] + runs[1]={phase:submitted}），然后调用
进程内真正的 `recover_backlog()`，观察：

  1. 恢复走的是「续跑上游任务 <id>」而不是 `creating task`；
  2. 全程**没有任何 POST**（重投就是重复计费）；
  3. 视频真的被下载下来（已付费的产物能接回来）；
  4. job 终态 succeeded、tokens 正确。

用法（在 ai-generation-portable-apps 目录下）：
    python _diag/live_resume_replay.py <ark_task_id> [username]
"""
import importlib.util
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = sys.argv[3] if len(sys.argv) > 3 else "seedance"
APP_DIR = ROOT / APP_NAME

spec = importlib.util.spec_from_file_location(f"{APP_NAME}_live_resume", APP_DIR / "app.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

TASK_ID = sys.argv[1] if len(sys.argv) > 1 else ""
USERNAME = sys.argv[2] if len(sys.argv) > 2 else "tester"
if not TASK_ID:
    print("usage: live_resume_replay.py <ark_task_id> [username] [seedance|nano-banana]")
    raise SystemExit(2)

if APP_NAME == "seedance":
    STATUS_URL = f"https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{TASK_ID}"
    VALUES = {
        "prompt": "live resume replay", "provider": "volcengine",
        "model": "doubao-seedance-2-5-260628", "duration": "5",
        "repeat_count": "1", "concurrency": "1", "poll_interval": "2",
    }
else:
    STATUS_URL = f"https://ai.t8star.org/v1/images/tasks/{TASK_ID}"
    VALUES = {
        "prompt": "live resume replay", "provider": "t8star",
        "mode": "text2img", "repeat_count": "1", "concurrency": "1",
        "poll_interval": "2",
    }

tmp = Path(tempfile.mkdtemp(prefix="resume_replay_"))
state = tmp / "state"
state.mkdir(parents=True, exist_ok=True)
mod.STATE_DIR = state
mod.ACTIVITY_PATH = state / "activity_log.json"
mod.BACKLOG_PATH = state / "jobs_backlog.json"
if hasattr(mod, "FILES_MAP_PATH"):
    mod.FILES_MAP_PATH = state / "download_files.json"
mod.OUTPUT_DIR = tmp / "outputs"

JOB_ID = "job-live-resume"
ACT_ID = "act-live-resume"
mod.write_activity_log([{
    "id": ACT_ID, "job_id": JOB_ID, "status": "running",
    "workspace_id": "localhost", "username": USERNAME,
    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "restore": {"values": VALUES, "media": {}},
}])
lock = getattr(mod, "JOBS_LOCK", None) or mod.LOCK
with lock:
    mod._backlog_set_locked(JOB_ID, stage="started", activity_id=ACT_ID,
                            ws_id="localhost", username=USERNAME)
    mod._backlog_set_run_locked(JOB_ID, 1, phase="submitted",
                                task_id=TASK_ID, status_url=STATUS_URL)

# 任何非 GET 的上游调用都算失败（重投 = 重复计费）
calls: list[tuple[str, str]] = []
_orig_request_json = mod.request_json


def spy(method, url, api_key, body=None, **kw):
    calls.append((method, url))
    return _orig_request_json(method, url, api_key, body=body, **kw)


mod.request_json = spy

print(f"[1] 伪造现场：backlog stage=started + runs[1]={{phase:submitted, task_id={TASK_ID}}}")
print(f"[2] 调用 recover_backlog() —— 这就是子应用启动时做的事")
recovered, interrupted = mod.recover_backlog()
print(f"    recovered={recovered} interrupted={interrupted}")

deadline = time.time() + 300
snap = {}
while time.time() < deadline:
    snap = mod.public_job_snapshot(mod.JOBS.get(JOB_ID)) or {}
    if snap.get("status") in {"succeeded", "failed", "cancelled"}:
        break
    time.sleep(2)

print("[3] 事件流：")
for event in (snap.get("events") or [])[:14]:
    print("   ", event.get("message"))
print(f"[4] 终态={snap.get('status')} tokens={snap.get('tokens')} done={snap.get('done')}")
result = (snap.get("results") or [{}])[0]
local_path = Path(str(result.get("local_path") or ""))
size = local_path.stat().st_size if local_path.exists() else 0
print(f"[5] 产物：{local_path} exists={local_path.exists()} bytes={size}")
print(f"[6] 上游调用序列：{calls}")
non_get = [c for c in calls if c[0] != "GET"]
verdict = (
    snap.get("status") == "succeeded"
    and not non_get
    and local_path.exists() and size > 0
)
print(f"[7] 判定：{'PASS —— 重启接回了已付费的产物，全程 0 次重新提交' if verdict else 'FAIL'}")
print(f"    临时目录：{tmp}")
raise SystemExit(0 if verdict else 1)
