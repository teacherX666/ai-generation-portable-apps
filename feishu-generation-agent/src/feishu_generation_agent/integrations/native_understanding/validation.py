"""证据确定性校验：不调用任何模型。

存在的理由是一次实测教训：真实成功的视频理解输出末镜头 end=18.5，
而视频实际时长 18.08s。模型对片尾的时间估算天然有零点几秒溢出，
若按严格 [0, duration] 校验，会把完全正确的证据判为不合格并降级。
因此引入尾部容差：容差内夹紧，容差外才判定时间轴不可信。
"""

import math

from feishu_generation_agent.domain.document import (
    TranscriptLine,
    VideoEvidence,
    VideoShot,
)

TAIL_TOLERANCE_SECONDS = 1.0
DURATION_TOLERANCE_SECONDS = 0.5


class EvidenceValidationError(RuntimeError):
    """证据时间轴不可信，必须降级到抽帧。"""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = list(issues)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_video_evidence(
    evidence: VideoEvidence,
    *,
    ffprobe_duration: float,
) -> tuple[VideoEvidence, list[str]]:
    issues: list[str] = []

    if not _finite(ffprobe_duration) or ffprobe_duration <= 0:
        raise EvidenceValidationError(["ffprobe duration 非法，无法校验证据时间轴"])

    if abs(evidence.duration - ffprobe_duration) > DURATION_TOLERANCE_SECONDS:
        raise EvidenceValidationError(
            [
                "duration 与 ffprobe 不一致："
                f"evidence={evidence.duration} ffprobe={ffprobe_duration}"
            ]
        )

    kept_shots: list[VideoShot] = []
    for shot in evidence.shots:
        if not _finite(shot.start) or not _finite(shot.end) or shot.start > shot.end:
            issues.append(
                f"剔除时间非法镜头：start={shot.start} end={shot.end} action={shot.action}"
            )
            continue
        if shot.end > ffprobe_duration:
            overshoot = shot.end - ffprobe_duration
            if overshoot > TAIL_TOLERANCE_SECONDS:
                issues.append(
                    f"剔除越界镜头：end={shot.end} 超出时长 {overshoot:.2f}s "
                    f"> 容差 {TAIL_TOLERANCE_SECONDS}s action={shot.action}"
                )
                continue
            shot = shot.model_copy(update={"end": ffprobe_duration})
            issues.append(
                f"片尾溢出 {overshoot:.2f}s 已在容差内夹紧至 {ffprobe_duration}s"
            )
        if shot.start > ffprobe_duration:
            issues.append(f"剔除起点越界镜头：start={shot.start} action={shot.action}")
            continue
        kept_shots.append(shot)

    if not kept_shots:
        raise EvidenceValidationError(
            issues + ["shots 全部被剔除，时间轴不可信，必须降级抽帧"]
        )

    kept_transcript: list[TranscriptLine] = []
    for line in evidence.transcript:
        if not _finite(line.t) or not isinstance(line.text, str):
            issues.append("剔除时间或文本非法的台词记录")
            continue
        if line.t > ffprobe_duration + TAIL_TOLERANCE_SECONDS:
            issues.append(f"剔除越界台词：t={line.t} 超出容差范围")
            continue
        if line.t > ffprobe_duration:
            line = line.model_copy(update={"t": ffprobe_duration})
            issues.append(f"台词时间点 {line.text!r} 夹紧至片尾 {ffprobe_duration}s")
        kept_transcript.append(line)

    timestamp = evidence.representative_timestamp
    if not _finite(timestamp) or not any(
        shot.start <= timestamp <= shot.end for shot in kept_shots
    ):
        nearest = min(
            kept_shots,
            key=lambda shot: min(
                abs(shot.start - (timestamp if _finite(timestamp) else 0.0)),
                abs(shot.end - (timestamp if _finite(timestamp) else 0.0)),
            ),
        )
        clamped = min(
            max(timestamp if _finite(timestamp) else nearest.start, nearest.start),
            nearest.end,
        )
        issues.append(
            f"代表帧时间点 {timestamp} 不在任何镜头内，已夹紧至 {clamped}"
        )
        timestamp = clamped

    validated = evidence.model_copy(
        update={
            "shots": kept_shots,
            "transcript": kept_transcript,
            "representative_timestamp": timestamp,
            "uncertainties": list(evidence.uncertainties) + issues,
        }
    )
    return validated, issues
