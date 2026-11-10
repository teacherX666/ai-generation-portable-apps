"""只读核对：inputs 里那个 mp4 是 video-1 的本体，还是「替换失败留下的孤儿」。

判据：/api/runs/<id>/references/video-1/content 的 sha256
   == inputs/*.mp4 的 sha256  → 它就是 video-1，失败发生在保存之前
   !=                        → 它是替换时新存进来、却没能挂到任务上的孤儿
                               （save_input 在 _task_with_references 之前跑）
"""
import hashlib
import sqlite3
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\123\Documents\LTX\ai-generation-portable-apps\feishu-generation-agent")
DATA = ROOT / "data"
OUT = Path(r"C:\Users\123\Documents\LTX\ai-generation-portable-apps\_diag")
RUN = "31770465-b994-4e7e-98f5-187481a00d7e"

c = sqlite3.connect(f"file:{DATA / 'agent.sqlite3'}?mode=ro", uri=True)
owner = c.execute("select owner_user_id from runs where run_id=?", (RUN,)).fetchone()[0]
c.close()

req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/runs/{RUN}/references/video-1/content",
    headers={"X-Portal-User-Id": owner},
)
with urllib.request.urlopen(req, timeout=120) as resp:
    video_bytes = resp.read()
    ctype = resp.headers.get("Content-Type")

backup = OUT / f"backup_{RUN}_video-1.mp4"
backup.write_bytes(video_bytes)
video_sha = hashlib.sha256(video_bytes).hexdigest()

orphan = next((DATA / "runs" / RUN).rglob("*.mp4"))
orphan_bytes = orphan.read_bytes()
orphan_sha = hashlib.sha256(orphan_bytes).hexdigest()

print(f"video-1 本体      : {len(video_bytes):>9} B  sha256={video_sha[:24]}  ({ctype})")
print(f"inputs 里的 mp4   : {len(orphan_bytes):>9} B  sha256={orphan_sha[:24]}")
print(f"同名文件          : {orphan.name}")
print()
if video_sha == orphan_sha:
    print("结论：inputs 里那个就是 video-1 本体 → 替换失败发生在「保存文件」之前")
else:
    print("结论：inputs 里那个 ≠ video-1 本体 → 它是替换时被保存下来、但没挂上任务的**孤儿**")
    print("      ⇒ 请求已经过了 FastAPI 字段校验 与 file_store.validate，")
    print("        失败发生在之后的 canonicalize/_persist_draft 阶段")
print(f"\n已把 video-1 本体备份到: {backup}")
