import base64
import json
from pathlib import Path

import httpx
import pytest

from feishu_generation_agent.domain.artifact import ProviderSubmission
from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import AgentError
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.integrations.aiport_video import AiPortVideoGenerator
from feishu_generation_agent.integrations.video_router import VideoGeneratorRouter


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
MP4_STUB = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


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
        "task_id": "task-v",
        "task_type": "image_to_video",
        "title": "本地视频",
        "source_block_ids": ["block-1"],
        "user_intent": "把参考图做成视频",
        "prompt": "@图片1 里的主体动起来",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "duration": 5,
        "resolution": "720p",
        "video_provider": "aiport",
    }
    payload.update(updates)
    return GenerationTask.model_validate(payload)


def _generator(handler, **updates: object) -> AiPortVideoGenerator:
    kwargs: dict[str, object] = {
        "base_url": "http://127.0.0.1:8801",
        "model_kind": "minimax_h3_all_reference",
    }
    kwargs.update(updates)
    return AiPortVideoGenerator(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs
    )


async def test_submit_posts_video_jobs_json(tmp_path: Path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "job_id": "job-v",
                "status_url": "/api/video_local/jobs/job-v",
            },
        )

    submission = await _generator(handler).submit(_task(), [_asset(tmp_path)])
    assert submission.provider == "aiport"
    assert submission.provider_task_id == "job-v"
    assert submission.status == "submitted"
    assert str(captured["url"]).endswith("/api/video_local/jobs/json")

    body = captured["body"]
    assert isinstance(body, dict)
    values = body["values"]
    files = body["files"]
    assert values["model_kind"] == "minimax_h3_all_reference"
    assert values["h3_task_mode"] == "i2v"
    assert values["num_frames"] == 5 * 24
    assert values["h3_aspect_ratio"] == "9:16 (Portrait Widescreen)"
    assert files["h3_image_0"]["data_url"].startswith("data:image/png;base64,")


async def test_poll_maps_pending_to_running(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "job-v", "status": "running", "results": []}
        )

    result = await _generator(handler).poll(
        ProviderSubmission(provider="aiport", provider_task_id="job-v", status="submitted")
    )
    assert result.status == "running"


async def test_poll_downloads_succeeded_mp4(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/video_local/jobs/job-v":
            return httpx.Response(
                200,
                json={
                    "id": "job-v",
                    "status": "succeeded",
                    "results": [
                        {
                            "index": 0,
                            "ok": True,
                            "status": "succeeded",
                            "filename": "out.mp4",
                            "download_url": "/api/download/tok-v",
                        }
                    ],
                },
            )
        if request.url.path == "/api/download/tok-v":
            return httpx.Response(
                200, content=MP4_STUB, headers={"content-type": "video/mp4"}
            )
        return httpx.Response(404)

    result = await _generator(handler).poll(
        ProviderSubmission(provider="aiport", provider_task_id="job-v", status="submitted")
    )
    assert result.status == "succeeded"
    assert len(result.result_items) == 1
    item = result.result_items[0]
    assert item.mime_type == "video/mp4"
    assert item.base64_data == base64.b64encode(MP4_STUB).decode("ascii")


async def test_poll_maps_failed(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "job-v", "status": "failed", "error": "boom"}
        )

    result = await _generator(handler).poll(
        ProviderSubmission(provider="aiport", provider_task_id="job-v", status="submitted")
    )
    assert result.status == "failed"
    assert result.error_message == "boom"


async def test_submit_rejects_image_task(tmp_path: Path):
    gen = _generator(lambda request: httpx.Response(500))
    image = _task(
        task_type="image_to_image",
        image_size="2K",
        image_provider="aiport",
        duration=None,
        resolution=None,
        video_provider=None,
    )
    with pytest.raises(AgentError):
        await gen.submit(image, [])


class _FakeGenerator:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.submit_calls = 0
        self.poll_calls = 0

    async def submit(self, task, assets, *, submission_id=None):
        self.submit_calls += 1
        return ProviderSubmission(
            provider=self.provider, provider_task_id=f"{self.provider}-job", status="submitted"
        )

    async def poll(self, submission):
        self.poll_calls += 1
        return ProviderSubmission(
            provider=submission.provider,
            provider_task_id=submission.provider_task_id,
            status="succeeded",
        )


async def test_router_dispatches_by_video_provider(tmp_path: Path):
    seedance = _FakeGenerator("seedance")
    aiport = _FakeGenerator("aiport")
    router = VideoGeneratorRouter(seedance, aiport)

    local = await router.submit(_task(), [_asset(tmp_path)])
    assert local.provider == "aiport"
    assert aiport.submit_calls == 1
    assert seedance.submit_calls == 0

    cloud = await router.submit(
        _task(video_provider=None), [_asset(tmp_path)]
    )
    assert cloud.provider == "seedance"
    assert seedance.submit_calls == 1

    polled = await router.poll(
        ProviderSubmission(provider="aiport", provider_task_id="aiport-job", status="submitted")
    )
    assert polled.provider == "aiport"
    assert aiport.poll_calls == 1