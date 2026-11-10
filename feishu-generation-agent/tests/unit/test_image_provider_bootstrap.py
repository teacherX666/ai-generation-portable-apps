import httpx
import pytest

from feishu_generation_agent.bootstrap import build_image_providers
from feishu_generation_agent.config import Settings


class _StubDownloader:
    async def download(self, url: str, *, expected_mime_type: str) -> bytes:
        del url, expected_mime_type
        return b"stub"


DOWNLOADER = _StubDownloader()


def _settings(**updates: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "chiyun_api_key": "fictional-chiyun-key",
        "chiyun_base_url": "https://chiyun.work",
        "chiyun_model": "banana2-ssvip",
        "ark_api_key": "fictional-ark-key",
    }
    base.update(updates)
    return Settings(**base)


@pytest.fixture
def http_client():
    # trust_env=False：本机系统代理是 socks4://127.0.0.1:1080，httpx 不认这个
    # scheme，构造时就会抛 "Unknown scheme for proxy URL"。仓库其它地方
    # （bootstrap.py、test_seedance.py）都显式关掉了 env 代理。
    client = httpx.AsyncClient(trust_env=False)
    yield client


def test_banana_and_gpt_image2_use_distinct_models(http_client, tmp_path):
    providers = build_image_providers(
        _settings(),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert providers["banana"]._model == "banana2-ssvip"
    assert providers["gpt-image2"]._model == "gpt-image-2"


def test_custom_models_are_honoured(http_client, tmp_path):
    providers = build_image_providers(
        _settings(banana_model="nano-banana2[2K]-base", gpt_image_model="gpt-image-3"),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert providers["banana"]._model == "nano-banana2[2K]-base"
    assert providers["gpt-image2"]._model == "gpt-image-3"


def test_builds_all_three_image_providers(http_client, tmp_path):
    providers = build_image_providers(
        _settings(),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert set(providers) == {"banana", "gpt-image2", "seedream"}


def test_seedream_uses_ark_model_and_endpoint(http_client, tmp_path):
    settings = _settings()
    providers = build_image_providers(
        settings,
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    seedream = providers["seedream"]
    # 模型名来自配置（2026-09-14 起是推理接入点 ep-…），断言绑定配置而不是写死某个 id
    assert seedream._model == settings.seedream_model
    assert seedream._base_url.startswith("https://ark.cn-beijing.volces.com")


def test_missing_ark_key_drops_only_seedream(http_client, tmp_path):
    providers = build_image_providers(
        _settings(ark_api_key=None),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert set(providers) == {"banana", "gpt-image2"}


def test_missing_chiyun_key_keeps_seedream(http_client, tmp_path):
    providers = build_image_providers(
        _settings(chiyun_api_key=None),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert set(providers) == {"seedream"}


def test_no_keys_yields_empty_registry(http_client, tmp_path):
    providers = build_image_providers(
        _settings(chiyun_api_key=None, ark_api_key=None),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert providers == {}
