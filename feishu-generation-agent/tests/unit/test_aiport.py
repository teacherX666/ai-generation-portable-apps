import base64
import json
from pathlib import Path

import httpx
import pytest

from feishu_generation_agent.domain.artifact import ProviderSubmission
from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import AgentError
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.integrations.aiport import AiPortImageGenerator


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _asset(tmp_path: Path) -> MediaAsset:
    path = tmp_path / "ref.png"
    path.write_bytes(PNG_1X1)
    return MediaAsset(
        asset_id="asset-1",
        source_block_id="block-1",
        origin="feishu",
        local_path=path,
        mime_type="image/png",
        size=len(PNG_1X1),
        sha256="a" * 64,
    )


def _task(**updates: object) -> GenerationTask:
    payload: dict[str, object] = {
        "task_id": "task-1",
        "task_type": "image_to_image",
        "title": "本地出图",
        "source_block_ids": ["block-1"],
        "user_intent": "把参考图改成 CG 风格",
        "prompt": "@图片1 里的主体改成 CG 风格",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "image_provider": "aiport",
    }
    payload.update(updates)
    return GenerationTask.model_validate(payload)


def _generator(handler, **updates: object) -> AiPortImageGenerator:
    kwargs: dict[str, object] = {
        "base_url": "http://127.0.0.1:8801",
        "model_kind": "qwen2511",
    }
    kwargs.update(updates)
    return AiPortImageGenerator(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs
    )


async def test_submit_posts_jobs_json_and_returns_job_id(tmp_path: Path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "job_id": "job-123",
                "status_url": "/api/image_local/jobs/job-123",
            },
        )

    submission = await _generator(handler).submit(_task(), [_asset(tmp_path)])

    assert submission.provider == "aiport"
    assert submission.provider_task_id == "job-123"
    assert submission.status == "submitted"
    assert str(captured["url"]).endswith("/api/image_local/jobs/json")
    body = captured["body"]
    assert isinstance(body, dict)
    values = body["values"]
    files = body["files"]
    assert values["model_kind"] == "qwen2511"
    assert values["repeat_count"] == 1
    assert values["width"] == 2048 and values["height"] == 2048
    assert files["image_1"]["data_url"].startswith("data:image/png;base64,")


async def test_poll_maps_pending_to_running(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "job-123", "status": "running", "results": []}
        )

    result = await _generator(handler).poll(
        ProviderSubmission(provider="aiport", provider_task_id="job-123", status="submitted")
    )
    assert result.status == "running"


async def test_poll_downloads_succeeded_result(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/image_local/jobs/job-123":
            return httpx.Response(
                200,
                json={
                    "id": "job-123",
                    "status": "succeeded",
                    "results": [
                        {
                            "index": 0,
                            "ok": True,
                            "status": "succeeded",
                            "filename": "out.png",
                            "download_url": "/api/download/tok-1",
                        }
                    ],
                },
            )
        if request.url.path == "/api/download/tok-1":
            return httpx.Response(
                200, content=PNG_1X1, headers={"content-type": "image/png"}
            )
        return httpx.Response(404)

    result = await _generator(handler).poll(
        ProviderSubmission(provider="aiport", provider_task_id="job-123", status="submitted")
    )
    assert result.status == "succeeded"
    assert len(result.result_items) == 1
    item = result.result_items[0]
    assert item.mime_type == "image/png"
    assert item.base64_data == base64.b64encode(PNG_1X1).decode("ascii")


async def test_poll_maps_failed(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "job-123", "status": "failed", "error": "boom"}
        )

    result = await _generator(handler).poll(
        ProviderSubmission(provider="aiport", provider_task_id="job-123", status="submitted")
    )
    assert result.status == "failed"
    assert result.error_message == "boom"


async def test_poll_rejects_wrong_provider(tmp_path: Path):
    gen = _generator(lambda request: httpx.Response(500))
    with pytest.raises(AgentError):
        await gen.poll(
            ProviderSubmission(provider="seedream", provider_task_id="job-123", status="submitted")
        )


async def test_submit_rejects_video_task(tmp_path: Path):
    gen = _generator(lambda request: httpx.Response(500))
    video = _task(
        task_type="image_to_video",
        image_provider=None,
        image_size=None,
        duration=3,
        resolution="720p",
        reference_images=[],
    )
    with pytest.raises(AgentError):
        await gen.submit(video, [])