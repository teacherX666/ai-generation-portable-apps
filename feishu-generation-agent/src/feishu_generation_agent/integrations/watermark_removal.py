"""成品视频「一键去水印」（2026-10-14，用户口径：「成品加一个按钮，按下才去水印」）。

**为什么必须在像素上处理**：实测提示词里连写三遍「不参考右下角豆包ai的水印」，
成片右下角照样清晰复现「豆包AI生成」—— 视频模型是照着参考图画，**不执行"忽略某块"的否定指令**。
所以要么生成前把参考图上的水印抹掉（`_diag/scrub_watermark.py`），要么成片后处理（本模块）。

**判据（怎么知道水印在哪）**：水印是**每帧同一位置的静态叠加层** —— 画面在动、它不动。
所以用「时间维方差极低」就能定位它。实测花卉浮雕那条（run `52991c28`）：
    720x1280 下 x 614..716 / y 1226..1274，去水印后该区域静态叠加层像素 194 → 0。
豆包/即梦的水印都贴在右下角、尺寸随分辨率等比缩放，所以这里用**相对框**。

**边界（老实说清）**：
* 只做 `delogo`（从框四周插值）。如果水印压在有复杂纹理的画面上，插值会留下轻微涂抹感；
  本次实测那条水印处是平整亮面，看不出痕迹。
* 只在**显式按下按钮**时才跑（用户要求），不做成每次自动。
* 产物写成 `<原名>-nowatermark.mp4` **另存**，绝不覆盖原产物 ——
  原产物文件名就是它的 sha256（`{sha256}.ext`），改了会破坏产物校验。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from feishu_generation_agent.integrations.video_reference import _binary

#: 水印相对框 (x0, y0, w, h)，相对画面宽高 —— 实测自 720x1280（x584..718 / y1230..1274）
#:
#: ⚠️ **第一版框取窄了 20px，用户当场发现"没去干净"**：水印最左边那个字压在**会动的画面**上，
#: 而"时间维方差极低"只能抓到不动的部分（当时只量到 x640..703），于是最左边一个字被留下。
#: 教训：**定位静态叠加层要吃余量** —— 时间维判据给核心位置，框再往外扩一圈，
#: 并且验收要看**字符图**（有没有残留文字结构），不能只看"静态像素数=0"。
STANDARD_BOX_REL = (584 / 720, 1230 / 1280, 134 / 720, 44 / 1280)
#: 转码给足时间（10 秒 720p 实测约 20 秒；上限防卡死）
REMOVE_TIMEOUT_SECONDS = 900
#: 另存后缀
OUTPUT_SUFFIX = "-nowatermark"


@dataclass(slots=True)
class WatermarkRemoval:
    source: Path
    output: Path
    box: tuple[int, int, int, int]
    reused: bool
    elapsed_seconds: float

    def as_dict(self) -> dict:
        return {
            "ok": True,
            "output": str(self.output),
            "box": {"x": self.box[0], "y": self.box[1], "w": self.box[2], "h": self.box[3]},
            "reused": self.reused,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


def output_path_for(source: Path) -> Path:
    """去水印产物路径：与源同目录、`<stem>-nowatermark<ext>`。"""
    return source.with_name(f"{source.stem}{OUTPUT_SUFFIX}{source.suffix}")


def watermark_box(width: int, height: int, box_rel: tuple[float, ...] = STANDARD_BOX_REL) -> tuple[int, int, int, int]:
    """把相对框换算成像素框，并保证**严格在画面内**（delogo 要求留边）。"""
    x = max(1, int(box_rel[0] * width))
    y = max(1, int(box_rel[1] * height))
    w = max(1, int(box_rel[2] * width))
    h = max(1, int(box_rel[3] * height))
    # ffmpeg delogo：x+w 必须 ≤ width-1（否则报 "Logo area is outside of the frame"）
    w = min(w, width - 2 - x)
    h = min(h, height - 2 - y)
    return x, y, max(1, w), max(1, h)


def _probe_size(video: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            _binary("ffprobe"),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    width, _, height = result.stdout.strip().partition(",")
    return int(width), int(height)


def remove_video_watermark(
    source: Path,
    *,
    output: Path | None = None,
    box_rel: tuple[float, ...] = STANDARD_BOX_REL,
    force: bool = False,
) -> WatermarkRemoval:
    """去掉 `source` 右下角的静态水印，另存为 `-nowatermark.mp4`。

    * 已有更新的产物且 `force=False` → 直接复用（重复点按钮不重复转码）；
    * ffmpeg 不可用 / 转码失败 → 抛 RuntimeError（调用方转成 4xx/5xx，界面给出明确原因）。
    """
    started = time.monotonic()
    source = Path(source)
    if not source.is_file():
        raise RuntimeError(f"源文件不存在：{source}")
    target = Path(output) if output is not None else output_path_for(source)
    width, height = _probe_size(source)
    box = watermark_box(width, height, box_rel)
    if target.exists() and not force and target.stat().st_mtime >= source.stat().st_mtime:
        # 复用已有的去水印版本（重复点按钮不重复转码）。
        return WatermarkRemoval(source, target, box, True, time.monotonic() - started)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.part{target.suffix}")
    command = [
        _binary("ffmpeg"),
        "-v", "error", "-y",
        "-i", str(source),
        "-vf", f"delogo=x={box[0]}:y={box[1]}:w={box[2]}:h={box[3]}:show=0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart",
        str(temporary),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=REMOVE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - 依赖外部进程
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"去水印超时（>{REMOVE_TIMEOUT_SECONDS}s）") from exc
    if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"去水印失败：{(result.stderr or '').strip()[:200] or 'ffmpeg 未产出文件'}")
    os.replace(temporary, target)
    return WatermarkRemoval(source, target, box, False, time.monotonic() - started)
