"""「模型审片」看片失败（HTTP 400）的回归测试：公开托管必须优先火山 TOS。

**生产事故**（2026-10-21，飞书任务「女巫划过」run `77f0a961`）：用户点「让模型看一遍」，
界面报 `看片失败：RuntimeError`。真因是审片链路把 21.2MB 成片传到 **uguu.se**
（境外免费图床），方舟侧下载超时：

    HTTP 400 InvalidParameter: Timeout occurred while processing video for content[1].
    Please verify that the video size is within acceptable limits and the URL is
    reachable. (param=video_url)

实测同一段成片、同一 payload：**uguu 链接 → 400；TOS 预签名链接 → 200**（模型正常给出
审片结论）。本机 TOS 凭据（`VOLCENGINE_ACCESS_KEY` / `SECRET_KEY` / `TOS_BUCKET`）
本来就配好了，只有审片/参考视频这几处硬编码成了 uguu。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from feishu_generation_agent import bootstrap as bootstrap_module
from feishu_generation_agent.config import Settings
from feishu_generation_agent.integrations.public_media import (
    TosPublicMediaHost,
    UguuPublicMediaHost,
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "data_dir": tmp_path / "data",
        "outputs_dir": tmp_path / "outputs",
        "business_db_path": tmp_path / "business.sqlite3",
        "ark_api_key": "ark",
        "deepseek_api_key": "deepseek",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_tos_is_preferred_when_credentials_are_configured(tmp_path: Path) -> None:
    """配了 TOS 凭据就必须走 TOS —— 否则方舟拉 uguu 链接会超时（本次事故）。"""
    settings = _settings(
        tmp_path,
        volcengine_access_key="ak-test",
        volcengine_secret_key="sk-test",
        tos_bucket="seedance-sd",
        tos_region="cn-beijing",
    )
    host = bootstrap_module.build_public_media_host(settings, httpx.AsyncClient())
    assert isinstance(host, TosPublicMediaHost)
    assert host._bucket == "seedance-sd"
    assert host._region == "cn-beijing"


def test_falls_back_to_uguu_without_tos_credentials(tmp_path: Path) -> None:
    """没配 TOS 才退回 uguu（保证开箱可用，不至于因为没 TOS 就完全不能审片）。"""
    settings = _settings(tmp_path)
    host = bootstrap_module.build_public_media_host(settings, httpx.AsyncClient())
    assert isinstance(host, UguuPublicMediaHost)


@pytest.mark.parametrize(
    "partial",
    [
        {"volcengine_access_key": "ak-test", "volcengine_secret_key": "sk-test"},
        {"tos_bucket": "seedance-sd"},
        {"volcengine_access_key": "ak-test", "tos_bucket": "seedance-sd"},
    ],
)
def test_partial_config_falls_back_instead_of_crashing(
    tmp_path: Path, partial: dict[str, object]
) -> None:
    """半套配置（缺 bucket / 缺 SK）不能抛异常，安静退回 uguu。"""
    settings = _settings(tmp_path, **partial)
    host = bootstrap_module.build_public_media_host(settings, httpx.AsyncClient())
    assert isinstance(host, UguuPublicMediaHost)


def test_no_call_site_hardcodes_uguu() -> None:
    """护栏：bootstrap 里**不许**再出现行内的 `public_media_host=UguuPublicMediaHost(`。

    审片、参考视频、人像、参考音视频四条链路都吃这个参数；任何一处写回 uguu
    就等于把「方舟拉取超时」重新引回来（而且只在生产上表现为一句
    「看片失败：RuntimeError」，很难查）。
    """
    source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")
    assert "public_media_host=UguuPublicMediaHost(" not in source, (
        "bootstrap 里又出现了行内 uguu 托管；请改用 build_public_media_host(...)"
    )
