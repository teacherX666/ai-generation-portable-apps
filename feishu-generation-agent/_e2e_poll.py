"""稳健轮询一条 run 到终态：每次请求用**新连接**（避免 keep-alive 被服务端断开）。

用法：python _e2e_poll.py <run_id> [case] [timeout_seconds]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee"}
OUT_ROOT = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\reference_repair")

run_id = sys.argv[1]
case = sys.argv[2] if len(sys.argv) > 2 else "size"
timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 2400.0
out = OUT_ROOT / case
out.mkdir(parents=True, exist_ok=True)


def _get(path: str) -> httpx.Response:
    with httpx.Client(
        base_url=BASE, headers=HEADERS, timeout=120, trust_env=False
    ) as client:
        return client.get(path)


def view_of() -> dict:
    return _get(f"/api/runs/{run_id}").json()


def main() -> None:
    deadline = time.monotonic() + timeout
    last = None
    view: dict = {}
    while time.monotonic() < deadline:
        try:
            view = view_of()
        except Exception as exc:  # noqa: BLE001 —— 连接抖动不该终止轮询
            print(f"  [{time.strftime('%H:%M:%S')}] poll error: {type(exc).__name__}")
            time.sleep(10)
            continue
        status = view.get("status")
        if status != last:
            print(f"  [{time.strftime('%H:%M:%S')}] status={status}")
            last = status
        if view.get("artifacts") or status in {
            "failed",
            "cancelled",
            "waiting_review",
            "verification_pending",
        }:
            break
        time.sleep(8)

    (out / "final_view.json").write_text(
        json.dumps(view, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print("status:", view.get("status"))
    print("=== 修复相关事件 ===")
    for event in view.get("events") or []:
        summary = str(event.get("summary") if isinstance(event, dict) else event)
        if any(
            key in summary
            for key in ("压缩", "转码", "重编码", "裁剪", "抽帧", "体积", "编码", "生成失败", "上传")
        ):
            print("  -", summary)
    print("=== execution_records ===")
    print(json.dumps(view.get("execution_records"), ensure_ascii=False)[:2500])
    print("=== operations ===")
    print(json.dumps(view.get("operations"), ensure_ascii=False)[:1200])
    artifacts = view.get("artifacts") or []
    print(
        "artifacts:",
        [(a.get("artifact_id"), a.get("kind"), a.get("size"), a.get("status")) for a in artifacts],
    )
    for artifact in artifacts:
        artifact_id = artifact.get("artifact_id")
        try:
            content = _get(f"/api/runs/{run_id}/artifacts/{artifact_id}/content")
        except Exception as exc:  # noqa: BLE001
            print("download failed:", type(exc).__name__)
            continue
        print("download", artifact_id, "->", content.status_code, len(content.content))
        if content.status_code == 200 and content.content:
            suffix = ".mp4" if artifact.get("kind") != "image" else ".png"
            path = out / f"OUTPUT_{artifact_id[:12]}{suffix}"
            path.write_bytes(content.content)
            print("saved", path, path.stat().st_size)
    print("last_error:", json.dumps(view.get("last_error"), ensure_ascii=False)[:600])


if __name__ == "__main__":
    main()
    sys.stdout.flush()
