"""确定性视频体检：只用 OpenCV 算可复现的画面指标，不调用任何模型。

为什么单独成模块：这些指标是纯函数（输入一段视频文件，输出数值与结论），
可以完全用合成视频做单测，不需要任何真实生成结果、不花一分钱。

实测约束（2026-09-15 在本机确认）：
- **这台机器没有 ffmpeg**（`shutil.which("ffmpeg")` 返回 None），所以抽帧必须走
  cv2 自带的视频后端，且输入得是本地文件 —— 不能像 `_platform_thumb()` 那样把
  HTTP URL 直接喂给 ffmpeg。
- `cv2` 4.11.0 能本地读写 MP4（mp4v），单测因此可以合成已知缺陷的视频。

语义判读（多余肢体、与提示词不符等）不在本模块范围内，那是后续接模型的事；
这里只产出**带数值证据**的确定性结论，因此不会误报。
"""

from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

DEFAULT_FRAME_COUNT = 8
MAX_DECODED_FRAMES = 1200

# 判定阈值。合成视频单测会把缺陷做得比这些阈值极端得多，
# 所以阈值在这里是"保守"的：宁可漏报，也不要对正常视频误报。
BLUR_WARN = 60.0          # 清晰度：Laplacian 方差低于此值判为偏糊
FREEZE_DIFF = 1.5         # 相邻帧平均绝对差低于此值视为"没有变化"
FREEZE_MIN_RUN = 3        # 连续这么多帧没变化才算一段静止
LETTERBOX_MEAN = 14.0     # 边缘行的平均亮度低于此值
LETTERBOX_STD = 9.0       # 且这些行的亮度波动很小
LETTERBOX_MIN_PX = 2      # 至少这么多像素宽/高才算黑边
FLICKER_DELTA = 30.0      # 相邻帧平均亮度跳变超过此值算一次闪烁
DURATION_TOLERANCE = 0.35  # 时长相对误差容差


def _sample_count(total: int, wanted: int) -> int:
    return max(1, min(wanted, total))


def _edge_bar(gray: np.ndarray, axis: int) -> int:
    """返回 axis 方向上从头开始连续有多少行/列是纯黑边。

    axis=1 看行（上下黑边），axis=0 看列（左右黑边）。
    """
    profile = gray.mean(axis=axis)
    std = gray.std(axis=axis)
    count = 0
    for mean_value, std_value in zip(profile, std):
        if mean_value <= LETTERBOX_MEAN and std_value <= LETTERBOX_STD:
            count += 1
        else:
            break
    return count


def _letterbox(gray: np.ndarray) -> dict[str, int]:
    height = gray.shape[0]
    width = gray.shape[1]
    top = _edge_bar(gray, 1)
    bottom = _edge_bar(gray[::-1, :], 1)
    left = _edge_bar(gray, 0)
    right = _edge_bar(gray[:, ::-1], 0)
    # 黑边不能把整帧吃掉（纯黑视频不是"黑边"，是另一种问题）
    if top + bottom >= height - LETTERBOX_MIN_PX:
        top = bottom = 0
    if left + right >= width - LETTERBOX_MIN_PX:
        left = right = 0
    return {"top": top, "bottom": bottom, "left": left, "right": right}


def _blur_score(gray: np.ndarray) -> float:
    if gray.size == 0:
        return 0.0
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def analyze(path: str, expected: dict[str, Any] | None = None,
            frame_count: int = DEFAULT_FRAME_COUNT) -> dict[str, Any]:
    """对本地视频文件做确定性体检。

    expected 可选，用来比对提交时声明的参数：{"duration": 8, "resolution": "720p"}。
    """
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        return {"ok": False, "error": "无法解码该视频文件"}

    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        reported_total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        blur_scores: list[float] = []
        brightness: list[float] = []
        frame_diffs: list[float] = []
        letterbox_seen: list[dict[str, int]] = []
        previous_gray: np.ndarray | None = None
        decoded = 0
        sampled = 0

        # 需要逐帧解码才能做静止段/闪烁检测，所以这里按全帧率读，
        # 只是给一个上限防止超长视频把请求拖死。
        while decoded < MAX_DECODED_FRAMES:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            decoded += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness.append(float(gray.mean()))
            if previous_gray is not None:
                frame_diffs.append(float(cv2.absdiff(gray, previous_gray).mean()))
            previous_gray = gray
            if decoded % max(1, math.ceil((reported_total or 1) / frame_count)) == 0:
                blur_scores.append(_blur_score(gray))
                sampled += 1

        if previous_gray is not None:
            blur_scores.append(_blur_score(previous_gray))
            sampled += 1
            # 黑边只需看一帧末尾帧即可（多数生成视频黑边是全程稳定的）
            letterbox_seen.append(_letterbox(previous_gray))
    finally:
        capture.release()

    if decoded == 0:
        return {"ok": False, "error": "视频里没有可解码的帧"}

    duration = (decoded / fps) if fps > 0 else 0.0
    blur_min = min(blur_scores) if blur_scores else 0.0
    letterbox = letterbox_seen[0] if letterbox_seen else {"top": 0, "bottom": 0, "left": 0, "right": 0}

    # 静止段：连续 FREEZE_MIN_RUN 帧以上几乎没有变化
    freeze_runs = 0
    run = 0
    for diff in frame_diffs:
        if diff <= FREEZE_DIFF:
            run += 1
        else:
            if run >= FREEZE_MIN_RUN:
                freeze_runs += 1
            run = 0
    if run >= FREEZE_MIN_RUN:
        freeze_runs += 1

    flicker_events = sum(
        1 for a, b in zip(brightness, brightness[1:]) if abs(b - a) >= FLICKER_DELTA
    )

    metrics = {
        "fps": round(fps, 3),
        "width": width,
        "height": height,
        "decoded_frames": decoded,
        "sampled_frames": sampled,
        "duration_seconds": round(duration, 3),
        "blur_laplacian_min": round(blur_min, 2),
        "freeze_runs": freeze_runs,
        "flicker_events": flicker_events,
        "letterbox_px": letterbox,
    }

    findings: list[dict[str, Any]] = []

    if letterbox["top"] >= LETTERBOX_MIN_PX or letterbox["bottom"] >= LETTERBOX_MIN_PX \
            or letterbox["left"] >= LETTERBOX_MIN_PX or letterbox["right"] >= LETTERBOX_MIN_PX:
        findings.append({
            "level": "warn",
            "kind": "letterbox",
            "message": "画面四周有黑边，内容没有铺满整帧",
            "evidence": {"letterbox_px": letterbox, "frame": {"width": width, "height": height}},
        })

    if blur_min < BLUR_WARN:
        findings.append({
            "level": "warn",
            "kind": "blur",
            "message": "存在偏糊的帧，画面细节不足",
            "evidence": {"blur_laplacian_min": round(blur_min, 2), "threshold": BLUR_WARN},
        })

    if freeze_runs:
        findings.append({
            "level": "warn",
            "kind": "freeze",
            "message": f"检测到 {freeze_runs} 段画面完全静止，运动不连续",
            "evidence": {"freeze_runs": freeze_runs, "frames_per_run_min": FREEZE_MIN_RUN,
                         "diff_threshold": FREEZE_DIFF},
        })

    if flicker_events:
        findings.append({
            "level": "warn",
            "kind": "flicker",
            "message": f"检测到 {flicker_events} 次亮度突变，画面在闪",
            "evidence": {"flicker_events": flicker_events, "delta_threshold": FLICKER_DELTA},
        })

    if expected:
        expected_duration = expected.get("duration")
        try:
            expected_duration = float(expected_duration)
        except (TypeError, ValueError):
            expected_duration = None
        if expected_duration and expected_duration > 0 and duration > 0:
            relative = abs(duration - expected_duration) / expected_duration
            if relative > DURATION_TOLERANCE:
                findings.append({
                    "level": "warn",
                    "kind": "duration_mismatch",
                    "message": f"实际时长 {duration:.2f}s 与提交参数 {expected_duration:g}s 不符",
                    "evidence": {"actual": round(duration, 3), "expected": expected_duration,
                                 "relative_error": round(relative, 4)},
                })

    return {
        "ok": True,
        "metrics": metrics,
        "findings": findings,
        "clean": not findings,
    }
