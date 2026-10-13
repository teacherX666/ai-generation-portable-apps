"""参考视频 → 抽帧产物：免费活体验证（不调用任何生成供应商，不花钱）。

用法（在 feishu-generation-agent 目录下）：

    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    .\.venv\Scripts\python.exe repro_video_reference_frame_fix.py [source_url] [video_path]

做三件事：

1. `POST /api/runs` 建一条**独立运行**，等它到 `waiting_approval`；
2. 用真实接口 `POST /api/runs/{id}/references` 把参考视频挂到任务上（用户就是这么传的），
   打印视图里的 `reference_images`（**改之前的现场**：视频直接当引用）；
3. 用这条真实运行的真实任务 + 真实视频跑一遍**执行前流水线**
   （`_replace_video_references_with_frames` → `_without_unusable_references` →
   `_unusable_reference_message`），打印引用是否已落到抽帧产物上（**改之后**）。

**不批准**（批准会真的花一次视频生成的钱）。脚本最后会 `DELETE /api/runs/{id}` 清理。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8765"
PORTAL_USER = "6b34d9e8-3058-41b6-89b2-4cedac2362ee"
DEFAULT_SOURCE = "https://redcqchina.feishu.cn/wiki/UOCjwTJ9siENnUkMHNCclmTcnDb"
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR / "src"))

from feishu_generation_agent.domain.document import (  # noqa: E402
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
)
from feishu_generation_agent.domain.plan import GenerationTask  # noqa: E402
from feishu_generation_agent.graph import nodes as nodes_module  # noqa: E402
from feishu_generation_agent.storage.files import FileStore  # noqa: E402


class _Repository:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def append_event(self, run_id, node, status, summary) -> None:
        self.events.append(f"[{node}/{status}] {summary}")


def _local_path_for(run_id: str, asset_id: str, mime: str) -> Path:
    inputs = APP_DIR / "data" / "runs" / run_id / "inputs"
    suffix = ".mp4" if mime.startswith("video/") else ".png"
    candidates = sorted(inputs.glob(f"*{suffix}"))
    for candidate in candidates:
        if mime.startswith("video/") and asset_id in candidate.name:
            return candidate
    if mime.startswith("video/"):
        for candidate in candidates:
            return candidate
    return candidates[0] if candidates else inputs / f"{asset_id}{suffix}"


def _wait_for_plan(client: httpx.Client, run_id: str) -> dict:
    """等运行进入「等审批」；规划可能要好几分钟（LLM 慢时），最多等 10 分钟。"""
    import time

    view: dict = {}
    for _ in range(120):
        view = client.get(f"/api/runs/{run_id}").json()
        tasks = (view.get("approval") or {}).get("tasks") or []
        if view.get("status") in {"failed", "succeeded", "cancelled"}:
            return view
        if view.get("status") in {"waiting_approval", "waiting_review"} and tasks:
            return view
        time.sleep(5)
    return view


async def _pre_execution_pipeline(run_id: str, view: dict) -> None:
    task_json = view["approval"]["tasks"][0]
    assets = [
        MediaAsset(
            asset_id=item["asset_id"],
            source_block_id=f"live:{item['asset_id']}",
            origin=item.get("origin") or "unknown",
            local_path=_local_path_for(run_id, item["asset_id"], item["mime_type"]),
            mime_type=item["mime_type"],
            size=item.get("size") or 1,
            sha256="live",
            width=item.get("width"),
            height=item.get("height"),
        )
        for item in view["approval"]["media_assets"]
    ]
    document = NormalizedDocument(
        document_id=view["approval"].get("document_id") or "live-doc",
        title=view["approval"].get("document_title") or "live",
        revision=1,
        source_type=SourceType.DOCX,
        source_token="live",
        blocks=[
            DocumentBlock(
                block_id="b", parent_id="p", block_type="text", order=0,
                path=["p", "b"], text="live",
            )
        ],
        text_view="live",
        media_assets=assets,
    )
    task = GenerationTask.model_validate(task_json)
    services = type("S", (), {})()
    services.repository = _Repository()
    services.file_store = FileStore(
        APP_DIR / "data", APP_DIR / "outputs", max_bytes=200 * 1024 * 1024
    )

    had_references = bool(task.reference_images)
    rewritten, frame_assets = await nodes_module._replace_video_references_with_frames(
        task, document, [], run_id, services, {}
    )
    resolved = await nodes_module._without_unusable_references(
        nodes_module._task_assets(rewritten, document, frame_assets), run_id, services
    )
    reason = nodes_module._unusable_reference_message(
        rewritten, resolved, had_references=had_references
    )

    print("=== 改之后：执行前流水线 ===")
    print("events:", json.dumps(services.repository.events, ensure_ascii=False))
    print("reference_images:", json.dumps(
        [r.model_dump(mode="json") for r in rewritten.reference_images],
        ensure_ascii=False,
    ))
    print("orders:", [r.order for r in rewritten.reference_images])
    print("assets handed to provider:", [(a.asset_id, a.mime_type) for a in resolved])
    print("references == assets:",
          {a.asset_id for a in resolved} == {r.asset_id for r in rewritten.reference_images})
    print("zero-reference failure reason:", reason)
    for asset in frame_assets:
        print("frame asset:", asset.asset_id, asset.mime_type, asset.size,
              asset.local_path, "exists:", asset.local_path.is_file())


def main() -> None:
    source_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOURCE
    video_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    client = httpx.Client(
        base_url=BASE_URL,
        headers={"X-Portal-User-Id": PORTAL_USER},
        timeout=180.0,
        trust_env=False,
    )
    if video_path is None:
        candidates = sorted((APP_DIR / "data" / "runs").glob("*/inputs/*.mp4"))
        if not candidates:
            raise SystemExit("没有可用的参考视频，请把 video_path 作为第二个参数传进来")
        video_path = candidates[-1]
    print("参考视频:", video_path)

    response = client.post("/api/runs", json={"source_url": source_url})
    response.raise_for_status()
    run_id = response.json()["run_id"]
    print("run_id:", run_id)
    try:
        view = _wait_for_plan(client, run_id)
        print("=== 上传参考视频之前 ===")
        print("status:", view.get("status"))
        print("reference_images:", json.dumps(
            view["approval"]["tasks"][0]["reference_images"], ensure_ascii=False))

        task = view["approval"]["tasks"][0]
        order = max(
            [int(ref.get("order") or 0) for ref in (task.get("reference_images") or [])]
            or [0]
        ) + 1
        upload = client.post(
            f"/api/runs/{run_id}/references",
            files={"file": (video_path.name, video_path.read_bytes(), "video/mp4")},
            data={"task_id": task["task_id"], "role": "reference_video",
                  "order": str(order)},
        )
        print("POST references ->", upload.status_code, upload.text[:200])

        view = client.get(f"/api/runs/{run_id}").json()
        print("=== 上传参考视频之后（改之前的现场）===")
        print("reference_images:", json.dumps(
            view["approval"]["tasks"][0]["reference_images"], ensure_ascii=False))

        asyncio.run(_pre_execution_pipeline(run_id, view))
    finally:
        print("DELETE run:", client.delete(f"/api/runs/{run_id}").text)


if __name__ == "__main__":
    main()
