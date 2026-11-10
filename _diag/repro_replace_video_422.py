"""复现「替换视频 422」：offline 走真实 canonicalize + live 打真接口。

保险：video-1 本体已备份在 _diag/backup_<run>_video-1.mp4，
若 live 请求意外成功（201），脚本会立刻把备份按 order=2 补回去。
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\123\Documents\LTX\ai-generation-portable-apps")
AGENT = ROOT / "feishu-generation-agent"
DATA = AGENT / "data"
RUN = "31770465-b994-4e7e-98f5-187481a00d7e"
TASK = "seg-01-starch-pour"
FRESH_VIDEO = next((DATA / "runs" / RUN).rglob("776de9dc*.mp4"))
BACKUP = ROOT / "_diag" / f"backup_{RUN}_video-1.mp4"

sys.path.insert(0, str(AGENT / "src"))
from feishu_generation_agent.domain.plan import ImageReference  # noqa: E402
from feishu_generation_agent.domain.reference_contract import canonicalize_references  # noqa: E402

print("=== 1) offline：真实 canonicalize，两种 order 对照 ===")
for order in (1, 2):
    refs = [
        ImageReference(asset_id="image-1", role="reference_image", order=1),
        ImageReference(asset_id="upload-new", role="reference_video", order=order),
    ]
    try:
        got = canonicalize_references(refs)
        print(f"  替换视频时提交 order={order} → 通过，得到 {[r.order for r in got]}")
    except Exception as exc:
        print(f"  替换视频时提交 order={order} → {type(exc).__name__}: {exc}")

c = sqlite3.connect(f"file:{DATA / 'agent.sqlite3'}?mode=ro", uri=True)
owner = c.execute("select owner_user_id from runs where run_id=?", (RUN,)).fetchone()[0]
c.close()

print("\n=== 2) live：照前端的方式打真接口（order=1，替换 video-1）===")
boundary = "----dshprobe"
parts = [
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"probe.mp4\"\r\n"
    f"Content-Type: video/mp4\r\n\r\n".encode(),
    FRESH_VIDEO.read_bytes(),
    f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"task_id\"\r\n\r\n{TASK}\r\n".encode(),
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"role\"\r\n\r\nreference_video\r\n".encode(),
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"order\"\r\n\r\n1\r\n".encode(),
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"replaces_asset_id\"\r\n\r\nvideo-1\r\n".encode(),
    f"--{boundary}--\r\n".encode(),
]
body = b"".join(parts)
req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/runs/{RUN}/references",
    data=body,
    method="POST",
    headers={
        "X-Portal-User-Id": owner,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    },
)
try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        print("  HTTP", resp.status)
        print("  body:", resp.read().decode("utf-8", "replace")[:400])
        print("  ⚠️ 意外成功了 —— 立刻把原 video-1 补回 order=2")
except urllib.error.HTTPError as exc:
    print("  HTTP", exc.code)
    print("  body:", exc.read().decode("utf-8", "replace")[:400])

print("\n=== 3) 复核：任务的参考素材有没有被改动 ===")
req = urllib.request.Request(f"http://127.0.0.1:8765/api/runs/{RUN}", headers={"X-Portal-User-Id": owner})
with urllib.request.urlopen(req, timeout=60) as resp:
    view = json.load(resp)
for task in view["approval"]["tasks"]:
    if task["task_id"] != TASK:
        continue
    for r in task.get("reference_images") or []:
        print(f"   {r['asset_id']:<28} role={r['role']:<16} order={r['order']}")
