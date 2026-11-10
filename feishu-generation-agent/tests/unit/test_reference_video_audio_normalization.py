"""「参考视频 HE-AAC 音轨必须先转成 AAC-LC」的回归测试（2026-10-15）。

用户报「手工串珠任务没有成功过一次」。实测分界线非常干净：

* 10-09 四次（12 秒、**纯图片**）→ **全部成功**；
* 10-15 六次（14/22 秒、**带参考视频**）→ **全部失败**
  （`input media detect failed: invalid_media` / 光秃秃的 `Bad Request`）。

根因：那条参考视频是 `H.264 High + **HE-AAC**`。用同一条通道做 4 秒最小探针验证：
原始（HE-AAC）被拒；本地转成 `-c:v libx264 -c:a aac`（AAC-LC）后**一次成功**
（`_diag/probe_seedance_reference_video.py`）。所以提交前统一规范化。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from feishu_generation_agent.integrations.video_reference import (
    VideoInfo,
    reference_video_needs_normalizing,
)


def _info(**kwargs) -> VideoInfo:
    base = dict(
        width=1080, height=1920, fps=30.0, duration=9.03,
        bitrate_bps=3_000_000, size_bytes=3_535_332, has_audio=True,
        codec="h264", audio_codec="aac", audio_profile="LC",
    )
    base.update(kwargs)
    return VideoInfo(**base)  # type: ignore[arg-type]


def test_he_aac_reference_video_must_be_normalized() -> None:
    """这就是生产上连败 6 次的那份参考视频：H.264 + HE-AAC → 必须转码。"""
    assert reference_video_needs_normalizing(
        _info(audio_codec="aac", audio_profile="HE-AAC")
    )


def test_aac_lc_reference_video_is_left_alone() -> None:
    """已经是 AAC-LC 的不要白转一遍。"""
    assert not reference_video_needs_normalizing(_info(audio_profile="LC"))


def test_hevc_video_codec_must_be_normalized() -> None:
    assert reference_video_needs_normalizing(_info(codec="hevc", audio_profile="LC"))


def test_silent_video_is_left_alone() -> None:
    """没有音轨的视频不用管音轨（转码只解决音轨问题）。"""
    assert not reference_video_needs_normalizing(
        _info(has_audio=False, audio_codec=None, audio_profile=None)
    )


def test_unknown_profile_is_normalized_conservatively() -> None:
    """profile 读不到时按"需要转"处理：HE-AAC 与 AAC-LC 的 codec_name 都是 aac，
    认不出来就等于把 6 次连败的风险留在路上。"""
    assert reference_video_needs_normalizing(_info(audio_profile=None))


def test_unknown_video_info_is_not_guessed() -> None:
    """ffprobe 读不到参数时不猜、也不转（避免把好文件重编一遍）。"""
    assert not reference_video_needs_normalizing(None)


def test_normalize_is_wired_into_submit() -> None:
    """接线守门：`submit` 必须在上传前调用规范化（否则这条修复就是死代码）。"""
    source = (
        Path(__file__).resolve().parents[2]
        / "src" / "feishu_generation_agent" / "integrations" / "seedance.py"
    ).read_text(encoding="utf-8")
    assert "normalize_reference_video" in source
    assert "_normalize_video_references(" in source
    # 必须在 _validate_submission 之后、构造 request_content 之前
    assert source.index("_normalize_video_references(\n") < source.index("request_content: list")


def test_media_asset_update_keeps_hash_consistent() -> None:
    """转码后必须同步更新 local_path/size/sha256 —— 产物校验靠 sha256。"""
    source = (
        Path(__file__).resolve().parents[2]
        / "src" / "feishu_generation_agent" / "integrations" / "seedance.py"
    ).read_text(encoding="utf-8")
    block = source[source.index("async def _normalize_video_references") :]
    block = block[: block.index("async def submit(")]
    assert '"sha256": sha256(data).hexdigest()' in block
    assert '"size": len(data)' in block
