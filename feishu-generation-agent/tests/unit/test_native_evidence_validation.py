import math

import pytest

from feishu_generation_agent.domain.document import (
    TranscriptLine,
    VideoEvidence,
    VideoShot,
)
from feishu_generation_agent.integrations.native_understanding.validation import (
    EvidenceValidationError,
    TAIL_TOLERANCE_SECONDS,
    validate_video_evidence,
)


def _evidence(
    *,
    duration: float = 18.08,
    shots: list[VideoShot] | None = None,
    transcript: list[TranscriptLine] | None = None,
    representative_timestamp: float = 2.8,
) -> VideoEvidence:
    return VideoEvidence(
        asset_id="asset-video-1",
        engine_id="gemini_native",
        schema_version="native_v1",
        duration=duration,
        shots=shots
        if shots is not None
        else [
            VideoShot(start=0.0, end=2.8, shot_size="中景", action="哭喊", camera="固定"),
            VideoShot(start=15.5, end=18.5, shot_size="特写", action="递出手机", camera="固定"),
        ],
        transcript=transcript
        if transcript is not None
        else [TranscriptLine(t=11.2, text="Oh, Grandma?")],
        audio=["背景音乐"],
        on_screen_text=[],
        representative_timestamp=representative_timestamp,
        summary="哭丧后棺盖被打开",
        uncertainties=[],
    )


def test_real_18s_output_tail_overshoot_is_clamped_not_rejected():
    """实测回归：真实成功输出末镜头 end=18.5，视频实际 18.08s。

    这条证据完全正确，绝不能被判不合格。
    """
    evidence, issues = validate_video_evidence(_evidence(), ffprobe_duration=18.08)
    last = evidence.shots[-1]
    assert last.end == pytest.approx(18.08)
    assert last.start == pytest.approx(15.5)
    assert any("夹紧" in issue for issue in issues)


def test_tail_overshoot_beyond_tolerance_drops_shot():
    evidence, issues = validate_video_evidence(
        _evidence(
            shots=[
                VideoShot(start=0.0, end=2.8, action="哭喊"),
                VideoShot(start=15.5, end=25.0, action="越界镜头"),
            ]
        ),
        ffprobe_duration=18.08,
    )
    assert [shot.action for shot in evidence.shots] == ["哭喊"]
    assert any("剔除" in issue for issue in issues)


def test_duration_mismatch_raises():
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_video_evidence(_evidence(duration=18.08), ffprobe_duration=30.0)
    assert any("duration" in issue for issue in excinfo.value.issues)


def test_all_shots_dropped_raises():
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_video_evidence(
            _evidence(shots=[VideoShot(start=5.0, end=1.0, action="时间倒挂")]),
            ffprobe_duration=18.08,
        )
    assert any("shots" in issue for issue in excinfo.value.issues)


def test_non_finite_timestamps_are_dropped():
    evidence, issues = validate_video_evidence(
        _evidence(
            shots=[
                VideoShot(start=0.0, end=2.8, action="正常"),
                VideoShot(start=math.nan, end=5.0, action="NaN"),
            ]
        ),
        ffprobe_duration=18.08,
    )
    assert [shot.action for shot in evidence.shots] == ["正常"]
    assert issues


def test_representative_timestamp_is_clamped_into_a_shot():
    evidence, _ = validate_video_evidence(
        _evidence(representative_timestamp=99.0),
        ffprobe_duration=18.08,
    )
    assert any(
        shot.start <= evidence.representative_timestamp <= shot.end
        for shot in evidence.shots
    )


def test_representative_timestamp_inside_shot_is_untouched():
    evidence, _ = validate_video_evidence(
        _evidence(representative_timestamp=1.5),
        ffprobe_duration=18.08,
    )
    assert evidence.representative_timestamp == pytest.approx(1.5)


def test_transcript_beyond_tolerance_is_dropped():
    evidence, issues = validate_video_evidence(
        _evidence(
            transcript=[
                TranscriptLine(t=11.2, text="保留"),
                TranscriptLine(t=99.0, text="越界"),
            ]
        ),
        ffprobe_duration=18.08,
    )
    assert [line.text for line in evidence.transcript] == ["保留"]
    assert issues


def test_tail_tolerance_constant_is_one_second():
    assert TAIL_TOLERANCE_SECONDS == 1.0
