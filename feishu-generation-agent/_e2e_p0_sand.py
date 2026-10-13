"""P0 真实端到端：「刮太空沙」需求，带参考视频跑 1 条生成。

分阶段（避免一次烧掉额度，也方便逐步核验）：
  A: 起 run → 等审批 → 落盘审批态（引用 / 提示词）
  B: （可选）改提示词 + 挂视频 → 批准 → 等成片 → 下载成片 → 落盘事件与产物
  C: 删除测试 run（清理）

用法：.venv\\Scripts\\python.exe _e2e_p0_sand.py A|B|C
状态文件：_e2e_p0_state.json
数据目录：_quality_analysis/sand/p0_verify/
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee"}
SOURCE_URL = "https://redcqchina.feishu.cn/wiki/AZQhws4jyicD8ekZWI7celKPn2f"
STATE = Path("_e2e_p0_state.json")
OUT = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\sand\p0_verify")
VIDEO_FALLBACK = Path(
    r"data\runs\4154013f-7d69-4825-86a3-8f234609c713\inputs"
    r"\367ea9fb04383a4ef1328a0b559104d454516777e9ef4b5ef195f8b379131bbc.mp4"
)

APPROVAL_STATUSES = {"waiting_approval", "waiting_review"}
TERMINAL = {
    "completed",
    "failed",
    "cancelled",
    "completed_with_errors",
    "verification_pending",
    "waiting_review",
}


def _state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def _save(data: dict) -> None:
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _dump(name: str, payload) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("wrote", path)
    return path


def _wait(client: httpx.Client, run_id: str, want, timeout: float = 900.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        view = client.get(f"{BASE}/api/runs/{run_id}").json()
        status = view.get("status")
        if status != last:
            print(f"  [{time.strftime('%H:%M:%S')}] status={status}")
            last = status
        if want(status, view):
            return view
        time.sleep(5)
    raise SystemExit(f"timeout waiting for run {run_id} (last status {last})")


def stage_a() -> None:
    with httpx.Client(headers=HEADERS, timeout=60) as client:
        response = client.post(
            f"{BASE}/api/runs",
            json={"source_url": SOURCE_URL, "planning_mode": "video"},
        )
        print("POST /api/runs ->", response.status_code, response.text[:400])
        response.raise_for_status()
        run_id = response.json()["run_id"]
        print("run_id =", run_id)
        _save({"run_id": run_id})
        view = _wait(
            client,
            run_id,
            lambda status, _v: status in APPROVAL_STATUSES
            or status in {"failed", "cancelled"},
        )
        _dump("A_approval_view.json", view)


def stage_b(patch_prompt: bool) -> None:
    data = _state()
    run_id = data["run_id"]
    with httpx.Client(headers=HEADERS, timeout=180) as client:
        view = client.get(f"{BASE}/api/runs/{run_id}").json()
        tasks = (view.get("approval") or {}).get("tasks") or []
        if not tasks:
            raise SystemExit(f"no approval tasks in view: {list(view)}")
        task = next(
            (item for item in tasks if item.get("task_type") == "image_to_video"),
            tasks[0],
        )
        task_id = task["task_id"]
        print("task_id =", task_id)
        print("references =")
        for reference in task.get("reference_images") or []:
            print("   ", reference)
        print("prompt (first 600) =")
        print((task.get("prompt") or "")[:600])

        video_ids = [
            asset.get("asset_id")
            for asset in (view.get("media_assets") or view.get("assets") or [])
            if str(asset.get("mime_type", "")).startswith("video/")
        ]
        print("video assets in view:", video_ids)

        if patch_prompt:
            prompt = (OUT / "B_prompt_patched.txt").read_text(encoding="utf-8")
            response = client.patch(
                f"{BASE}/api/runs/{run_id}/tasks/{task_id}",
                json={"patch": {"prompt": prompt}},
            )
            print("PATCH task ->", response.status_code, response.text[:300])

        response = client.post(
            f"{BASE}/api/runs/{run_id}/decision",
            json={"action": "approve", "selected_task_ids": [task_id]},
        )
        print("POST decision ->", response.status_code, response.text[:600])
        response.raise_for_status()
        _save({**data, "task_id": task_id})

        view = _wait(
            client,
            run_id,
            lambda status, v: bool(v.get("artifacts")) or status in {"failed", "cancelled"},
            timeout=1800,
        )
        _dump("B_final_view.json", view)
        artifacts = view.get("artifacts") or []
        print("artifacts:", [(a.get("artifact_id"), a.get("kind")) for a in artifacts])
        for artifact in artifacts:
            artifact_id = artifact.get("artifact_id")
            content = client.get(
                f"{BASE}/api/runs/{run_id}/artifacts/{artifact_id}/content"
            )
            print("artifact", artifact_id, "->", content.status_code, len(content.content))
            if content.status_code == 200 and content.content:
                suffix = ".mp4" if artifact.get("kind") != "image" else ".png"
                path = OUT / f"OUTPUT_{artifact_id[:12]}{suffix}"
                path.write_bytes(content.content)
                print("saved", path, path.stat().st_size)


def stage_c() -> None:
    data = _state()
    run_id = data["run_id"]
    with httpx.Client(headers=HEADERS, timeout=60) as client:
        response = client.delete(f"{BASE}/api/runs/{run_id}")
        print("DELETE ->", response.status_code, response.text[:300])


if __name__ == "__main__":
    stage = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    if stage == "A":
        stage_a()
    elif stage == "B":
        stage_b(patch_prompt="--patch" in sys.argv)
    elif stage == "C":
        stage_c()
    else:
        raise SystemExit("unknown stage")
