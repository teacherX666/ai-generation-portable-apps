from dataclasses import dataclass
from pathlib import Path
import json
import math
import shutil
import subprocess
import os


@dataclass(frozen=True, slots=True)
class ExtractedVideoFrame:
    index: int
    path: Path
    mime_type: str = "image/jpeg"


@dataclass(frozen=True, slots=True)
class FrameChoice:
    """抽帧兜底时选中的那一帧（1-based）与选择理由（要写进事件给用户看）。"""

    index: int
    reason: str


def _binary(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    program_data = os.environ.get("ProgramData", "C:/ProgramData")
    candidates = (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"{program_data}/chocolatey/bin/{name}.exe",
        f"{program_data}/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/{name}.exe",
        f"C:/ffmpeg/bin/{name}.exe",
        "C:/Program Files/ffmpeg/bin/{name}.exe",
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return str(path)
    raise RuntimeError(f"{name} is not available")

def _video_duration(video_path: Path) -> float:
    result = subprocess.run(
        [
            _binary("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    value = result.stdout.strip()
    try:
        duration = float(value)
    except ValueError as exc:
        raise RuntimeError("ffprobe returned an invalid duration") from exc
    if duration <= 0:
        raise RuntimeError("ffprobe returned a non-positive duration")
    return duration


def extract_video_frames(
    video_path: Path,
    frame_count: int,
    output_dir: Path,
) -> list[Path]:
    """抽取视频中间均匀分布的 N 帧，返回 JPEG 文件路径。

    帧采样点取 duration * (i + 1) / (frame_count + 1)，避免抽到黑帧/片头片尾。
    任何一帧失败都整体失败：视频参考要么可读，要么明确走失败分支，避免拿
    半截帧列表继续规划。
    """
    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = _video_duration(video_path)
    paths: list[Path] = []
    ffmpeg = _binary("ffmpeg")
    for index in range(frame_count):
        timestamp = duration * (index + 1) / (frame_count + 1)
        output_path = output_dir / f"frame-{index + 1:02d}.jpg"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-y",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced no frame")
        paths.append(output_path)
    return paths


#: 抽帧兜底时**排除**的首尾比例。采样点本身已经是 `duration*(i+1)/(n+1)`（避开片头
#: 片尾），这里再排掉首尾 20%，避免账号卡 / 黑场 / 平台结尾页。
_FRAME_EDGE_RATIO = 0.2


def _corner_overlay_penalty(path: Path) -> float:
    """左下 + 右下角块的「边缘密度」，用来就近判断角部有没有叠加水印。

    平台水印（小红书「一只小瓜」/「小红书」、即梦AI、抖音号…）都是**后期叠加**在画面
    下角的紧凑高对比文字块，边缘锐利。同一段视频各帧的水印位置固定，没有水印的帧角部
    明显更"平"，所以角部边缘密度低的帧更可能是干净帧。这是启发式，不做 OCR：只在
    抽帧兜底路径上决定"抽哪一帧"，读图失败一律返回 0.0（不因判定失败挡住兜底）。
    """
    try:
        from PIL import Image, ImageFilter, ImageStat

        with Image.open(path) as image:
            grey = image.convert("L")
            width, height = grey.size
            if width < 40 or height < 40:
                return 0.0
            box_width = max(8, width // 4)
            box_height = max(8, height // 6)
            boxes = (
                (0, height - box_height, box_width, height),
                (width - box_width, height - box_height, width, height),
            )
            return max(
                float(
                    ImageStat.Stat(
                        grey.crop(box).filter(ImageFilter.FIND_EDGES)
                    ).mean[0]
                )
                for box in boxes
            )
    except Exception:  # noqa: BLE001 —— 判定失败不该挡住兜底
        return 0.0


def probe_video_duration(video_path: Path) -> float | None:
    """读视频时长（秒）；读不到返回 None（不猜）。"""
    try:
        return _video_duration(Path(video_path))
    except Exception:  # noqa: BLE001 —— 探测失败不该挡住其它路径
        return None


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """参考视频的实际参数（压缩决策与「压缩前后具体数字」事件都靠它）。"""

    width: int
    height: int
    fps: float | None
    duration: float
    bitrate_bps: int | None
    size_bytes: int
    has_audio: bool
    #: 视频编码（ffprobe `codec_name`，例如 `h264` / `hevc`）。默认 None 是为了
    #: 兼容既有调用方（测试桩）—— 拿不到就当"不知道"，不猜。
    codec: str | None = None


def _parse_fps(value: object) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    numerator, _, denominator = value.partition("/")
    try:
        top = float(numerator)
        bottom = float(denominator)
    except ValueError:
        return None
    if bottom == 0 or top <= 0:
        return None
    return top / bottom


def probe_video_info(video_path: Path) -> VideoInfo | None:
    """一次 ffprobe 读全参考视频参数；读不到返回 None（不猜、不挡路径）。

    为什么需要它：上游与本机对参考视频的约束不止「时长」一个维度，体积超限时
    要给出**具体数字**（原分辨率/码率/帧率）才知道怎么压、压了多少。旧实现只有
    `_video_duration`，压缩决策会退化成瞎猜。
    """
    path = Path(video_path)
    try:
        result = subprocess.run(
            [
                _binary("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,avg_frame_rate",
                "-show_entries",
                "format=duration,bit_rate,size",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        payload = json.loads(result.stdout)
    except Exception:  # noqa: BLE001
        return None
    streams = payload.get("streams")
    fmt = payload.get("format")
    if not isinstance(streams, list) or not isinstance(fmt, dict):
        return None
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    if video is None:
        return None
    try:
        width = int(video["width"])
        height = int(video["height"])
        duration = float(fmt["duration"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or duration <= 0:
        return None
    bitrate: int | None = None
    try:
        bitrate = int(fmt["bit_rate"])
    except (KeyError, TypeError, ValueError):
        bitrate = None
    if bitrate is not None and bitrate <= 0:
        bitrate = None
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return None
    return VideoInfo(
        width=width,
        height=height,
        fps=_parse_fps(video.get("avg_frame_rate")),
        duration=duration,
        bitrate_bps=bitrate,
        size_bytes=size_bytes,
        has_audio=any(
            isinstance(stream, dict) and stream.get("codec_type") == "audio"
            for stream in streams
        ),
        codec=(
            str(video["codec_name"])
            if isinstance(video.get("codec_name"), str)
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class CompressedVideo:
    """一次压缩的**实际结果**（事件里要写的具体数字就取这里）。"""

    path: Path
    info: VideoInfo
    video_kbps: int
    max_long_side: int | None


#: 压缩时给容器/封装开销预留的比例（目标字节预算 × 该系数当作码率预算）。
_COMPRESS_CONTAINER_OVERHEAD = 0.96


def _even(value: int) -> int:
    """libx264 + yuv420p 要求宽高都是偶数。"""
    return max(2, value - (value % 2))


def _scaled_size(
    width: int, height: int, max_long_side: int
) -> tuple[int, int]:
    """等比缩放到长边 = `max_long_side`，宽高都取偶数（libx264 + yuv420p 要求）。"""
    if width >= height:
        new_width = _even(max_long_side)
        new_height = _even(round(height * max_long_side / width))
    else:
        new_height = _even(max_long_side)
        new_width = _even(round(width * max_long_side / height))
    return new_width, new_height


def _scale_filter(
    width: int, height: int, max_long_side: int | None
) -> tuple[str | None, int | None]:
    """按长边上限算等比缩放；不需要缩就返回 (None, None)。"""
    if max_long_side is None or max(width, height) <= max_long_side:
        return None, None
    new_width, new_height = _scaled_size(width, height, max_long_side)
    if new_width <= 0 or new_height <= 0:
        return None, None
    return f"scale={new_width}:{new_height}", max(new_width, new_height)


def _is_mp4_container(path: Path) -> bool:
    """文件头是不是 ISO-BMFF（`....ftyp`）—— 上游只收 mp4/mov。"""
    try:
        head = path.open("rb").read(16)
    except OSError:
        return False
    return len(head) >= 12 and head[4:8] == b"ftyp"


def count_decoded_frames(path: Path) -> int | None:
    """**完整解码一遍视频流**并数出真实帧数；失败返回 None。

    为什么不用容器里的 `nb_frames`：2026-10-10 实测那条 HEVC 4K 参考视频
    `nb_frames=305`，但 `-count_frames` 真解码只有 **149** 帧（同样的时间基/
    时长），即容器的帧数字段**根本不可信**。用 `nb_frames` 判"有没有抽帧"会得出
    完全相反的结论（我第一次就是这么误判的：以为转码丢了 51% 的帧，实际一帧没丢）。

    这个调用同时充当「能解码」的自检 —— `-count_frames` 会把视频流解到底。
    """
    try:
        result = subprocess.run(
            [
                _binary("ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip().splitlines()
    if not value:
        return None
    try:
        frames = int(value[0].strip())
    except ValueError:
        return None
    return frames if frames > 0 else None


def verify_reference_video(
    path: Path,
    expected: "CompressedVideoCheck",
) -> bool:
    """自检压缩/转码产物：**不通过就绝不交给上游**。

    2026-10-10：上游 `invalid_media` 是"拿到素材但解不了"，所以这里逐条验：

    1. 容器是 mp4（头 4..8 字节 `ftyp`）；
    2. ffprobe 读得出参数；
    3. **编码是我们下发的 H.264**（上游对参考视频实际只认 H.264 —— 见
       `compress_video` 的注释）；
    4. **能完整解码**（`-count_frames` 会把视频流解到底，顺带数出真实帧数）；
    5. 时长没被改短/改长（容差 0.3s 与 2% 取大）；
    6. **解码帧数没被抽掉**（与源比对，容差 2% 且至少 1 帧）—— 这是"不抽帧"的
       唯一可靠判据（容器 `nb_frames` 不可信）；
    7. 帧率没被大改（相对容差 5%：VFR→CFR 会让平均帧率从 59.26 变成 60.0，
       帧数其实一帧没少，所以这里不能卡绝对值）；
    8. 分辨率等于计划值（等比缩放后的偶数尺寸；没缩就与源一致）。
    """
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if not _is_mp4_container(path):
        return False
    info = probe_video_info(path)
    if info is None:
        return False
    if expected.codec is not None and info.codec != expected.codec:
        return False
    if expected.width is not None and info.width != expected.width:
        return False
    if expected.height is not None and info.height != expected.height:
        return False
    tolerance = max(0.3, expected.duration * 0.02)
    if abs(info.duration - expected.duration) > tolerance:
        return False
    if expected.fps is not None and info.fps is not None:
        if abs(info.fps - expected.fps) > max(0.5, expected.fps * 0.05):
            return False
    frames = count_decoded_frames(path)
    if frames is None:
        return False  # 解不完 = 上游也会 invalid_media
    if expected.frames is not None:
        if abs(frames - expected.frames) > max(1, expected.frames * 0.02):
            return False
    return True


@dataclass(frozen=True, slots=True)
class CompressedVideoCheck:
    """自检期望值（由 `compress_video` 按计划填好）。"""

    duration: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = "h264"
    #: 源视频**解码后**的真实帧数（不是容器里的 `nb_frames`，那个字段不可信）。
    frames: int | None = None


def compress_video(
    video_path: Path,
    max_bytes: int | None,
    output_path: Path,
    *,
    max_long_side: int | None = None,
    budget_ratio: float = 0.92,
    crf: int | None = None,
    audio_kbps: int = 128,
    min_video_kbps: int = 250,
    expected_frames: int | None = None,
) -> CompressedVideo | None:
    """把参考视频压到 `max_bytes` 以内、并输出**上游认的 H.264/mp4**（保时长与帧率）。

    为什么需要它 —— 2026-10-10 两个不同的生产错误：

    1. `generation_input_too_large`：参考视频 34.06MiB 撞上本机闸门
       `SeedanceVideoGenerator(max_input_bytes=32MiB)`（`cause=input_too_large`）。
       上游方舟文档允许「单个视频不超过 200 MB」，真凶是**本机闸门**；用户要的是
       「自动缩小」，不是"把闸门调大"。
    2. `invalid_media`（`input media detect failed`）：参考视频是 **HEVC/H.265**
       （2160x3840 4K），上游拿到字节后**解不了**。三条证据：
       - 两条 `invalid_media` 失败的参考视频都是 `hevc/Main`；
       - 历史上所有成功的参考视频都是 `h264`；
       - 同一批 HEVC 视频里，**时长超 30s 的那两条反而成功了** —— 因为既有的
         「裁短」档位（`trim_video`）本来就用 libx264 重编码，等于顺手把它们转成了
         H.264；而 2.5s / 7.0s 这两条没超时长、**原样直传**，于是撞上 invalid_media。
       - 公开图床 URL（生产用 TOS 预签名）**实测健康**（匿名 GET 200 / CT video/mp4 /
       字节数一致 / Range 206），所以不是"上游取不到字节"那一类。

    参数：
    - `max_bytes`：体积上限；`None` 表示**不压体积**（纯转码，用 `crf` 画质优先）；
    - `crf`：给定就走画质优先的 CRF 编码（转码归一用），否则按 `max_bytes` 反推码率；
    - 输出一律 `libx264 / yuv420p / +faststart / mp4`，**不传 `-r`**（不抽帧、不降帧率）；
    - 产出**必须过 `verify_reference_video` 自检**，不过就返回 None（绝不上传脏产物）。

    码率按 `目标字节 × budget_ratio × 8 / 时长` 反推，音频码率先从预算里扣掉；
    算出来低于 `min_video_kbps` 就取该下限（画面优先，体积若仍超由调用方再降一档）。
    """
    if max_bytes is not None and max_bytes <= 0:
        return None
    if max_bytes is None and crf is None:
        return None
    source = Path(video_path)
    info = probe_video_info(source)
    if info is None or info.duration <= 0:
        return None
    video_kbps = 0
    if crf is None:
        assert max_bytes is not None
        budget_bytes = int(max_bytes * budget_ratio)
        if budget_bytes <= 0:
            return None
        target_total_kbps = budget_bytes * 8 / info.duration / 1000
        usable_kbps = target_total_kbps * _COMPRESS_CONTAINER_OVERHEAD
        audio_kbps_used = audio_kbps if info.has_audio else 0
        video_kbps = int(usable_kbps - audio_kbps_used)
        if video_kbps < min_video_kbps:
            video_kbps = min_video_kbps
    scale, applied_long_side = _scale_filter(
        info.width, info.height, max_long_side
    )
    command = [
        _binary("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
    ]
    if scale is not None:
        command += ["-vf", scale]
    command += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
    ]
    if crf is not None:
        command += ["-crf", str(crf)]
    else:
        command += [
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{int(video_kbps * 1.5)}k",
            "-bufsize",
            f"{video_kbps * 3}k",
        ]
    command += [
        "-pix_fmt",
        "yuv420p",
    ]
    if info.has_audio:
        command += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", "-y", str(output_path)]
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
            check=True,
        )
    except Exception:  # noqa: BLE001 —— 压缩失败不能拖挂整单
        return None
    if not output_path.is_file() or output_path.stat().st_size == 0:
        return None
    if crf is None and max_bytes is not None:
        if output_path.stat().st_size > max_bytes:
            return None
    if applied_long_side is None:
        expected_width, expected_height = info.width, info.height
    else:
        expected_width, expected_height = _scaled_size(
            info.width, info.height, max_long_side
        )
    source_frames = (
        expected_frames
        if isinstance(expected_frames, int) and expected_frames > 0
        else count_decoded_frames(source)
    )
    if not verify_reference_video(
        output_path,
        CompressedVideoCheck(
            duration=info.duration,
            width=expected_width,
            height=expected_height,
            fps=info.fps,
            codec="h264",
            frames=source_frames,
        ),
    ):
        return None
    compressed_info = probe_video_info(output_path)
    if compressed_info is None:
        return None
    return CompressedVideo(
        path=output_path,
        info=compressed_info,
        video_kbps=video_kbps,
        max_long_side=applied_long_side,
    )



def trim_video(
    video_path: Path,
    max_seconds: float,
    output_path: Path,
) -> Path | None:
    """把参考视频裁到 `max_seconds` 秒以内（保留运动参考，不抽帧）。

    为什么需要它：上游对**参考视频**的时长有硬上限，实测原文 ——

        The parameter `content[2]` specified in the request is not valid: the
        parameter video duration (seconds) specified in the request must be less
        than or equal to 30.2 for model doubao-seedance-2-5 in r2

    而「刮太空沙」那条需求的参考视频是 43.7s。**抽帧会丢掉用户要的运动信息**
    （这正是 P0 要修的那件事），所以超出上限时优先**裁剪**，裁剪失败才退到抽帧。
    重新编码（而不是 `-c copy`）是为了让输出时长精确可控 —— 关键帧对齐会让
    `-c copy` 切出比目标更长的时间。
    """
    if max_seconds <= 0:
        return None
    try:
        ffmpeg = _binary("ffmpeg")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-t",
                f"{max_seconds:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-y",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=True,
        )
    except Exception:  # noqa: BLE001
        return None
    if not output_path.is_file() or output_path.stat().st_size == 0:
        return None
    duration = probe_video_duration(output_path)
    if duration is not None and duration > max_seconds + 0.05:
        # 编码器给出的时长还是超了 → 不要把它当成"裁好了"（否则上游再拒一次）。
        return None
    return output_path


def choose_reference_frame(paths: list[Path]) -> FrameChoice:
    """从抽好的帧里挑**一张**当参考图（抽帧兜底专用）。

    2026-10-08 生产实测（刮太空沙 run 4154013f / a1ce7928 / 266501b7，三次字节完全
    相同的 `4f7f16d3….jpg`）：兜底时按语义分析给的 `representative_frame_index: 1`
    取了**抽出的第 1 帧**，那一帧带小红书水印、主体还是用户明确不要参考的紫海绵块，
    成片结尾因此被换成了同一个紫海绵。所以这里改成：

    1. **避开首尾各 20%**（账号卡 / 黑场 / 结尾页）；
    2. **跳过角部叠加水印的帧**（`_corner_overlay_penalty` 最低者优先）；
    3. 打平时取**最靠近中间**的那一帧（比起点更可能代表整段的运动）。
    """
    count = len(paths)
    if count <= 0:
        raise ValueError("paths must not be empty")
    if count == 1:
        return FrameChoice(1, "只抽到一帧")
    edge = math.ceil(count * _FRAME_EDGE_RATIO)
    low = 1 + edge
    high = count - edge
    if high < low:  # 帧数太少，排除首尾会把候选清空 → 退回全部候选
        low, high = 1, count
    middle = (count + 1) / 2
    scored: list[tuple[float, float, int]] = []
    for index in range(low, high + 1):
        scored.append(
            (
                _corner_overlay_penalty(paths[index - 1]),
                abs(index - middle),
                index,
            )
        )
    scored.sort()
    penalty, distance, chosen = scored[0]
    avoided_watermark = penalty < scored[-1][0]
    parts = [f"共抽 {count} 帧，已避开首尾各 {edge} 帧"]
    if avoided_watermark:
        parts.append("优先选了角部没有平台水印的帧")
    parts.append("取最靠近中段的一帧" if distance else "正中间")
    return FrameChoice(chosen, "，".join(parts))
