"""零成本复现：`generation_input_too_large` 到底在量什么。

结论（先写在这里，脚本会逐条验证）：
它是一个**本机闸门**，不是上游错误 —— 量的是**单个参考素材文件的字节数**，
上限 `SeedanceVideoGenerator(max_input_bytes)`，bootstrap 用默认值 32 MiB。
判据：`_validate_submission` 里 `file_stat.st_size > self._max_input_bytes` →
`cause=input_too_large`；执行层 `_execution_error` 把它渲染成
`参考素材无法用于生成` + `code=generation_input_too_large`。

脚本用**永远不会被调用**的 httpx transport（一发起请求就抛）来证明
「这个错误发生在任何网络请求之前」→ 不产生上游费用。

只读，不写任何业务数据。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from feishu_generation_agent.domain.document import MediaAsset  # noqa: E402
from feishu_generation_agent.domain.errors import AgentError  # noqa: E402
from feishu_generation_agent.domain.plan import GenerationTask  # noqa: E402
from feishu_generation_agent.domain.video_models import (  # noqa: E402
    VIDEO_MODEL_BY_KEY,
)
from feishu_generation_agent.graph import nodes as nodes_module  # noqa: E402
from feishu_generation_agent.integrations.seedance import (  # noqa: E402
    SeedanceVideoGenerator,
)
from feishu_generation_agent.integrations.video_reference import (  # noqa: E402
    probe_video_info,
)

RUN = "13268f86-1522-43f4-ada4-35cdfc72b9e6"
VIDEO = (
    ROOT
    / "data"
    / "runs"
    / RUN
    / "inputs"
    / "752f4f4197fa196dd669a68f24f3cf1b1ffbf0aff295c02e46a8f59ea41a7bfb.mp4"
)
LIMIT = 32 * 1024 * 1024


class _ForbiddenTransport(httpx.AsyncBaseTransport):
    """任何出站请求都直接失败 —— 用来证明错误发生在提交之前。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_async_request(self, request):  # noqa: ANN001, ANN201
        self.calls.append(f"{request.method} {request.url}")
        raise AssertionError("本机闸门竟然发起了上游请求（不该发生）")


def _asset(path: Path) -> MediaAsset:
    import hashlib

    data = path.read_bytes()
    return MediaAsset(
        asset_id="upload-video-1",
        source_block_id="local-upload",
        origin="local_upload",
        file_token=None,
        local_path=path,
        mime_type="video/mp4",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _task() -> GenerationTask:
    return GenerationTask.model_validate(
        {
            "task_id": "task-1",
            "task_type": "image_to_video",
            "title": "复现 input_too_large",
            "source_block_ids": ["block-1"],
            "user_intent": "复现",
            "prompt": "参考 @视频1 的运镜。",
            "reference_images": [
                {"asset_id": "upload-video-1", "role": "reference_video", "order": 1}
            ],
            "reference_mode": "multi_reference",
            "aspect_ratio": "9:16",
            "duration": 5,
            "resolution": "720p",
        }
    )


async def main() -> None:
    info = probe_video_info(VIDEO)
    print("--- 被拒的参考视频（真实素材）---")
    print(
        json.dumps(
            {
                "path": str(VIDEO),
                "size_bytes": VIDEO.stat().st_size,
                "size_MiB": round(VIDEO.stat().st_size / 1024 / 1024, 3),
                "limit_bytes": LIMIT,
                "over_by_bytes": VIDEO.stat().st_size - LIMIT,
                **(
                    {
                        "width": info.width,
                        "height": info.height,
                        "fps": info.fps,
                        "duration_s": round(info.duration, 3),
                        "bitrate_mbps": round((info.bitrate_bps or 0) / 1e6, 3),
                    }
                    if info
                    else {}
                ),
            },
            ensure_ascii=False,
            indent=1,
        )
    )

    transport = _ForbiddenTransport()
    capability = VIDEO_MODEL_BY_KEY["seedance2.5"]
    async with httpx.AsyncClient(transport=transport) as client:
        generator = SeedanceVideoGenerator(
            client,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key="ark-0000000000000000000000000000000000000000",
            model=capability.model,
            capability=capability,
        )
        print("--- 本机闸门配置（bootstrap 未覆盖 → 用默认值）---")
        print(
            json.dumps(
                {
                    "max_input_bytes": generator._max_input_bytes,
                    "max_total_input_bytes": generator._max_total_input_bytes,
                    "max_input_bytes_MiB": round(
                        generator._max_input_bytes / 1024 / 1024, 3
                    ),
                    "reference_video_max_seconds": generator.reference_video_max_seconds,
                },
                ensure_ascii=False,
            )
        )

        asset = _asset(VIDEO)
        task = _task()
        try:
            await generator.submit(task, [asset])
        except AgentError as exc:
            detail = exc.detail
            print("--- 复现到的错误（本地抛出）---")
            print(
                json.dumps(
                    {
                        "category": detail.category.value,
                        "message": detail.message,
                        "technical_detail": detail.technical_detail,
                        "retryable": detail.retryable,
                    },
                    ensure_ascii=False,
                    indent=1,
                )
            )
            rendered = nodes_module._execution_error(exc)
            print("--- 执行层渲染给用户看到的样子 ---")
            print(json.dumps(rendered, ensure_ascii=False, indent=1))
            print("--- 上游请求次数 ---", len(transport.calls), transport.calls)
            print(
                "code 是否为 generation_input_too_large:",
                rendered.get("code") == "generation_input_too_large",
            )


if __name__ == "__main__":
    asyncio.run(main())
    sys.stdout.flush()
