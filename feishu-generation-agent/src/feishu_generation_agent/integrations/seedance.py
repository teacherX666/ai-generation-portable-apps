import base64
from hashlib import sha256
import ipaddress
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import stat
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
import unicodedata
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import SecretStr

from feishu_generation_agent.domain.artifact import (
    ProviderResult,
    ProviderSubmission,
)
from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import GenerationTask, ImageReference, SEEDANCE_PROMPT_SUBMIT_MAX_CHARS
from feishu_generation_agent.integrations.video_reference import (
    normalize_reference_video,
    reframe_reference_video,
)
from feishu_generation_agent.domain.video_models import (
    VIDEO_MODEL_BY_ID,
    VideoModelCapability,
    _supported_resolution,
)
from feishu_generation_agent.domain.reference_contract import (
    canonicalize_references,
    remap_prompt_references,
)
from feishu_generation_agent.integrations.public_media import (
    PublicMediaHost,
    PublicMediaUploadError,
)
from feishu_generation_agent.integrations.rework_prompt import (
    merge_negative_constraints,
)


_LOGGER = logging.getLogger(__name__)


def _border_median_color(image: Any) -> tuple[int, ...]:
    """取四边像素的**中位色**当补边底色（分镜表切片多是纯色底，视觉几乎无感）。"""
    try:
        width, height = image.size
        pixels: list[tuple[int, ...]] = []
        for x in range(width):
            pixels.append(tuple(image.getpixel((x, 0)))[:3])
            pixels.append(tuple(image.getpixel((x, height - 1)))[:3])
        for y in range(height):
            pixels.append(tuple(image.getpixel((0, y)))[:3])
            pixels.append(tuple(image.getpixel((width - 1, y)))[:3])
        channels = list(zip(*pixels))
        return tuple(
            sorted(channel)[len(channel) // 2] for channel in channels
        )
    except Exception:  # noqa: BLE001
        return (255, 255, 255)

#: 供应商错误消息里像密钥的片段要抹掉（错误文本会进日志与界面）。
_SECRET_LIKE = re.compile(r"\b(?:ark|sk)-[A-Za-z0-9._-]{8,}")


#: 上游对参考图的硬约束（Ark）。**三条并列**，缺一条就被整单拒：
#: 每边 300~6000px、宽高比 0.40~2.50、总像素 ≥90,000（仅 r2v）。
#:
#: 总像素这条是 2026-11-05 才暴露的（`doubao-seedance-2-5` 的 r2v）：
#:
#:     the parameter image pixel count specified in the request must be greater
#:     than or equal to 90000 for model doubao-seedance-2-5 in r2v
#:
#: ⚠️ 它与「每边 ≥300px」**互不蕴含**：`300x300` 正好 90,000 是边界，
#: 而 `190x147` 按面积放大到 90,000 后是 `342x264` —— 面积够了、短边仍只有 264。
#: 所以倍数必须取**两者较大者**。
_REFERENCE_MIN_SIDE = 300
_REFERENCE_MAX_SIDE = 6000
_REFERENCE_MIN_PIXELS = 90_000


def reference_min_scale(width: int, height: int) -> float:
    """把参考图放大到「每边 ≥300px **且** 总像素 ≥90,000」所需的倍数（已达标返回 1.0）。

    这是**唯一一份**这套数学 —— `integrations.seedance` 的提交保障与
    `graph.nodes` 的「执行前整理素材」都调它。2026-11-05 之前两边各写一套，
    结果只补了面积、漏了短边（`190x147` → `342x264`，短边仍 264）——
    同一个约束在两处各判一套，必然再次打架。
    """
    if width <= 0 or height <= 0:
        return 1.0
    side_scale = max(_REFERENCE_MIN_SIDE / width, _REFERENCE_MIN_SIDE / height)
    area_scale = (
        (_REFERENCE_MIN_PIXELS / (width * height)) ** 0.5
        if width * height < _REFERENCE_MIN_PIXELS
        else 1.0
    )
    return max(side_scale, area_scale, 1.0)


def reference_target_size(width: int, height: int) -> tuple[int, int]:
    """放大后的目标尺寸（等比，两条约束都满足；已达标则原样返回）。"""
    scale = reference_min_scale(width, height)
    if scale <= 1:
        return width, height
    target_width = max(_REFERENCE_MIN_SIDE, round(width * scale))
    target_height = max(_REFERENCE_MIN_SIDE, round(height * scale))
    # 放大后也不能超过上限：超过就整体缩回去（仍保持比例）。
    overflow = max(
        target_width / _REFERENCE_MAX_SIDE,
        target_height / _REFERENCE_MAX_SIDE,
        1,
    )
    return (
        max(_REFERENCE_MIN_SIDE, round(target_width / overflow)),
        max(_REFERENCE_MIN_SIDE, round(target_height / overflow)),
    )



def _redact_secret_like(text: str) -> str:
    return _SECRET_LIKE.sub("<已隐藏>", text)

#: 提交文本（正文 + 参考图映射 + 「必须避免」块）的总字符预算。
#:
#: 上游实测口径：合计 2007 字能过、2088 字被拒 `generation_prompt_too_long`；
#: 本地旧闸门是 2048 —— 比上游松，于是 2008~2048 这段灰区会放过去、由上游报错
#: （2026-09-17 用户正是撞在这个灰区）。取 1900 留出余量，只裁「跨轮反复改写的
#: 负向块」，正文与参考图映射永不裁剪。
SUBMIT_TOTAL_BUDGET_CHARS = 1900


_IMAGE_MIME_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
_IMAGE_FORMAT_MIME_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_REFERENCE_ROLES = frozenset(
    {
        "reference_image",
        "first_frame",
        "last_frame",
        "reference_video",
        "reference_audio",
    }
)
#: 用了这两个角色中的任意一个，输出比例就**由首帧图决定**，不能再指定具体比例。
_FRAME_ROLES = frozenset({"first_frame", "last_frame"})
_ASPECT_RATIOS = frozenset(
    {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21", "adaptive"}
)
_RESOLUTIONS = frozenset({"720p", "1080p"})
_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_DEFAULT_MAX_INPUT_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_TOTAL_INPUT_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_NONTERMINAL_STATUSES = frozenset(
    {"queued", "running", "pending", "submitted", "processing", "in_progress"}
)
_TERMINAL_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "cancelled", "canceled", "expired"}
)


class SeedanceVideoGenerator:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str | None,
        api_key: str | SecretStr | None,
        model: str | None,
        capability: VideoModelCapability | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_input_bytes: int = _DEFAULT_MAX_INPUT_BYTES,
        max_total_input_bytes: int = _DEFAULT_MAX_TOTAL_INPUT_BYTES,
        enforce_total_input_bytes: bool = True,
        public_media_host: PublicMediaHost | None = None,
        provider_name: str = "seedance",
        image_url_resolver: Callable[[GenerationTask, Any, MediaAsset, bytes], Awaitable[str]] | None = None,
    ) -> None:
        if not isinstance(base_url, str):
            raise self._configuration_error("base_url", "expected=https_origin_or_api_v3")
        try:
            parsed = urlsplit(base_url.strip())
            port = parsed.port
        except (TypeError, ValueError):
            raise self._configuration_error(
                "base_url", "expected=https_origin_or_api_v3"
            ) from None
        normalized_path = parsed.path.rstrip("/")
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or normalized_path not in {"", "/api/v3"}
            or self._is_obviously_local_host(parsed.hostname)
        ):
            raise self._configuration_error(
                "base_url", "expected=https_origin_or_api_v3"
            )

        secret = (
            api_key.get_secret_value()
            if isinstance(api_key, SecretStr)
            else api_key
        )
        if not isinstance(secret, str) or not secret.strip():
            raise self._configuration_error("api_key", "cause=empty")
        if not isinstance(model, str) or not model.strip():
            raise self._configuration_error("model", "cause=empty")

        limits = {
            "max_response_bytes": max_response_bytes,
            "max_input_bytes": max_input_bytes,
            "max_total_input_bytes": max_total_input_bytes,
        }
        for field_name, value in limits.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise self._configuration_error(field_name, "cause=not_positive")
        if max_total_input_bytes < max_input_bytes:
            raise self._configuration_error(
                "max_total_input_bytes", "cause=less_than_single_limit"
            )
        if not isinstance(enforce_total_input_bytes, bool):
            raise self._configuration_error(
                "enforce_total_input_bytes", "cause=not_bool"
            )

        self._http_client = http_client
        self._base_url = f"https://{parsed.netloc}/api/v3"
        self._api_key = SecretStr(secret.strip())
        self._model = model.strip()
        self._capability = capability or VIDEO_MODEL_BY_ID.get(self._model)
        if self._capability is not None and self._capability.model != self._model:
            raise self._configuration_error("model", "cause=capability_mismatch")
        self._max_response_bytes = max_response_bytes
        self._max_input_bytes = max_input_bytes
        self._max_total_input_bytes = max_total_input_bytes
        self._enforce_total_input_bytes = enforce_total_input_bytes
        self._timeout = httpx.Timeout(120, connect=10)
        self._public_media_host = public_media_host
        self._provider_name = provider_name
        self._image_url_resolver = image_url_resolver

    @property
    def supports_video_references(self) -> bool:
        """这条通道能不能收 `video_url + role=reference_video`。

        2026-10-08：把「视频能不能直传」这件事收敛成**一个判据**，供 `graph/nodes.py`
        的降级决策使用（以前有两处互相矛盾的逻辑：这里的
        `_strip_non_image_references` 会**静默摘掉**视频，`graph/nodes.py` 的
        `_replace_video_references_with_frames` 又会**默认把视频换成抽帧** ——
        两处都与上游能力冲突，而且都不留可见痕迹）。

        - 方舟多参考模式（`_image_url_resolver is None`）：**支持**，视频会被发成
          `{"type": "video_url", "role": "reference_video"}`；
        - 真人类通道（`_image_url_resolver` 只认 `asset://` 图片）：**不支持**，
          `submit` 会明确报错，由执行层降级成抽帧并写事件。
        """
        return self._image_url_resolver is None

    @property
    def reference_video_max_seconds(self) -> float | None:
        """这条通道能接受的**参考视频时长上限**（秒）；不知道就返回 None。

        上游对参考视频时长有硬上限，实测错误原文（2026-10-08，「刮太空沙」run
        `a632f45f`，参考视频 43.7s）：

            The parameter `content[2]` specified in the request is not valid: the
            parameter video duration (seconds) specified in the request must be
            less than or equal to 30.2 for model doubao-seedance-2-5 in r2

        该上限与模型的生成时长上限同量级（`duration_max`，2.5 为 30s / 2.0 为 15s），
        所以取能力表里的 `duration_max`；拿不到能力信息就返回 None（**不猜**），
        执行层会因此跳过「裁短」这一级、直接走抽帧兜底。
        """
        capability = self._capability
        if capability is None:
            return None
        return float(capability.duration_max)

    @property
    def reference_video_max_bytes(self) -> int | None:
        """这条通道能接受的**参考视频单文件体积上限**（字节）；不知道返回 None。

        ⚠️ 这个数**不是上游的限制**，是本机适配层自己的闸门
        （`_validate_submission` 里 `file_stat.st_size > self._max_input_bytes`
        → `cause=input_too_large`）。2026-10-10 生产实测（run
        `13268f86-1522-43f4-ada4-35cdfc72b9e6`，参考视频 35,710,093 B = 34.06 MiB）：

        - 用户看到的是 `参考素材无法用于生成（generation_input_too_large）`；
        - 复现脚本 `_diag_repro_input_too_large.py` 证明该错误**在上游请求之前**
          抛出（httpx transport 记录到 0 次请求），所以它量的是**单个参考素材文件
          的字节数**，与分辨率/码率/时长都无关（那个 run 的 operation 停在
          `failed` 且 `official_id` 为空、execute 节点 26ms 完成）；
        - 上游方舟文档（创建视频生成任务，video_url 字段）写的是
          「大小：单个视频不超过 200 MB」—— 比本机闸门**宽 6 倍**，所以 34 MB
          视频在上游本来是合法的，撞的是我们自己的闸门。

        本机闸门比上游严，是因为它同时还要守住 base64 内联（图片走 data URL）与
        `max_total_input_bytes` 那条 64 MiB 的请求体约束；**不放松闸门**，改成
        超限时自动压缩到闸门以内（`graph/nodes.py` 的修复梯子第一级）。
        返回的就是闸门本身，保证「压缩目标」与「实际拦截点」是同一个数，
        不会出现两处各写一个魔数、改了闸门却忘了压缩阈值的情况。
        """
        return self._max_input_bytes

    @property
    def reference_video_accepted_codecs(self) -> frozenset[str] | None:
        """这条通道能接受的**参考视频编码**（ffprobe `codec_name` 口径）。

        2026-10-10 生产实测：**上游只认 H.264**，HEVC/H.265 会以
        `input media detect failed: invalid_media` 被拒（`code=invalidparameter`，
        发生在 poll 阶段）。证据：

        - 两条 `invalid_media` 失败的参考视频都是 `hevc/Main`（2160x3840 4K，
          2.5s / 7.0s）；用户原话「怎么又失败了」；
        - 历史上所有成功的参考视频都是 `h264`；
        - 同一批 HEVC 视频里**时长超 30s 的两条反而成功** —— 既有的「裁短」档位
          （`trim_video`）本来就用 libx264 重编码，等于顺手把它们转成了 H.264；
          没超时长的那两条原样直传，才撞上 invalid_media；
        - 公开图床那条路**已排除**：生产用 TOS 预签名 URL，实测匿名 GET 200、
          `Content-Type: video/mp4`、字节数与源文件一致、`Range: bytes=0-1023` → 206
          （`_diag_public_url_probe.py`），所以不是"上游取不到有效字节"。

        因此这条通道声明只收 `h264`；`graph/nodes.py` 会在**提交前**把别的编码
        （HEVC/AV1/MPEG-4…）转成 H.264/mp4（保留分辨率、帧率、时长）。拿不到探测
        结果时（`codec is None`）**不判死**，交给上游如实报错。
        """
        if self._capability is None:
            return None
        return frozenset({"h264"})

    def _reject_video_references(
        self,
        task: GenerationTask,
    ) -> None:
        """只收图片的通道遇到视频引用时**明确拒绝**（不再静默摘掉）。

        以前这里直接 `task, assets = self._strip_non_image_references(...)`：用户挂了
        参考视频，适配层悄悄把它摘掉、还顺手重写提示词 token，事件里什么都没有 ——
        与「上游本来就支持视频」的能力冲突。现在改成报一个可判定的错误，执行层据此
        走唯一的降级路径（抽帧 + 可见事件）。

        音频是**例外**：音频没有「抽帧」这种等价替代，且真人类通道确实收不了，保持
        原来的摘除行为（见 `_strip_audio_references`，记 warning），否则会把本来能
        跑通的任务直接判死。
        """
        video_ids = [
            reference.asset_id
            for reference in task.reference_images
            if reference.role == "reference_video"
        ]
        if video_ids:
            raise self._validation_error(
                task.task_id,
                "该通道不支持参考视频（只接受图片素材）",
                "cause=video_reference_not_supported; "
                f"asset_ids={','.join(sorted(video_ids))}",
            )

    def _strip_audio_references(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
    ) -> tuple[GenerationTask, list[MediaAsset]]:
        """Image-only mode: drop **audio** references (portrait provider).

        音频语义已经被规划器折进 `task.prompt`，这里摘掉引用并重写 @音频N token，
        避免上游因为不支持的 `audio_url` 拒单（HTTP 400）。视频**不在这里摘**
        （见 `_reject_video_references`：会明确报错并让执行层走抽帧降级）。
        """
        references = task.reference_images
        audio_references = [
            reference
            for reference in references
            if reference.role == "reference_audio"
        ]
        if not audio_references:
            return task, assets
        _LOGGER.warning(
            "真人类通道不支持参考音频，已摘掉 task=%s asset_ids=%s",
            task.task_id,
            [reference.asset_id for reference in audio_references],
        )
        kept_references = [
            reference
            for reference in references
            if reference.role != "reference_audio"
        ]
        keep_ids = {reference.asset_id for reference in kept_references}
        kept_assets = [asset for asset in assets if asset.asset_id in keep_ids]
        mime_types = {asset.asset_id: asset.mime_type for asset in assets}
        canonical_references = canonicalize_references(kept_references)
        try:
            prompt = remap_prompt_references(
                task.prompt,
                references,
                canonical_references,
                mime_types,
            )
        except Exception:
            _LOGGER.warning(
                "Failed to rewrite prompt tokens while dropping portrait "
                "audio refs; keeping original prompt. task=%s",
                task.task_id,
                exc_info=True,
            )
            prompt = task.prompt
        updated = task.model_copy(
            update={
                "reference_images": canonical_references,
                "prompt": prompt,
            }
        )
        return updated, kept_assets

    def _apply_frame_ratio_constraint(
        self,
        task: GenerationTask,
        references: list[Any],
    ) -> GenerationTask:
        """首帧/首尾帧 **以及带参考视频** 的任务，ratio 只能是 `adaptive`，下发前就地改写。

        2026-10-09 生产事故（飞书 Agent 第一次真正走到「首尾帧」这条分支）：审批通过的
        任务带着 `reference_mode=first_last_frame` + `first_frame/last_frame`，而
        `aspect_ratio` 还是规划器给的 `9:16`，payload 原样下发 → 方舟前置校验直接 400：

            The parameter ratio specified in the request is not valid. For first-frame
            or first-last-frame generation, the output ratio follows the first-frame
            image. Request id: 021791525053115795aa3a4ba36fe8857
            (code: submit_invalidparameter.tasktypeconstraint)

        2026-10-15 追加（用户报 `Bad Request …（poll_invalidparameter）`，同一文档
        「12 秒纯图片」成功、「22 秒 + **参考视频**」连败三次）：**带 `reference_video`
        的任务同样中招** —— 方舟会**按提示词**把任务判成「视频延长 / 视频编辑」，而这类
        任务 `ratio` 同样只支持 `adaptive`。仓库里早有这条错误的原文留档：

            Seedance identified your task as video extension based on your prompt.
            For this task type, the output ratio follows the input video selected for
            extension. Issues: [0] `ratio` must be `adaptive`.

        原来这里只认 `_FRAME_ROLES`（首帧/首尾帧），`reference_video` 漏掉了 —— 于是
        带着参考视频的任务始终下发 `9:16`，必然 400。**判据放宽到「含参考视频」是安全的**：
        `adaptive` 本来就在能力表的允许集合里（`VIDEO_MODELS[].aspect_ratios`），
        多参考图与纯文生视频不受影响（没有参考视频时原值原样保留）。

        只改**下发用**的副本，不动审批通过的 task 本身（计划/审批记录保持用户确认的原样）。
        """
        needs_adaptive = any(
            reference.role in _FRAME_ROLES or reference.role == "reference_video"
            for reference in references
        )
        if not needs_adaptive:
            return task
        if task.aspect_ratio == "adaptive":
            return task
        _LOGGER.info(
            "Seedance frame/reference-video mode forces ratio=adaptive (was %s); task=%s",
            task.aspect_ratio,
            task.task_id,
        )
        return task.model_copy(update={"aspect_ratio": "adaptive"})

    async def _normalize_video_references(
        self,
        assets: list[MediaAsset],
        contents: list[bytes],
        *,
        aspect_ratio: str | None = None,
    ) -> tuple[list[MediaAsset], list[bytes]]:
        """把参考视频转成上游接受的规格（**H.264 + AAC-LC**）后再上传。

        2026-10-15「手工串珠没有成功过一次」的根因：参考视频音轨是 HE-AAC，上游拒收
        （6/6 失败）；本地转成 AAC 后同一条通道的 4 秒探针**一次成功**。转码是阻塞的
        ffmpeg 调用，放线程里跑，别卡事件循环；任何失败都按原样提交（不因为转码失败
        把本来能跑的任务判死 —— 上游若真拒绝，还有执行层的修复梯子兜底）。

        2026-10-16 追加（用户报「为什么西兰花那个视频不是 9.16」）：带参考视频的任务
        方舟强制 `ratio=adaptive`、**输出比例跟随输入的参考视频**，所以横屏参考视频
        会把成片带成横屏。这里在提交前把参考视频**裁成任务要的画幅**（cover）。
        """
        if not any(str(asset.mime_type).startswith("video/") for asset in assets):
            return assets, contents
        normalized_assets: list[MediaAsset] = []
        normalized_contents: list[bytes] = []
        for asset, content in zip(assets, contents, strict=True):
            if not str(asset.mime_type).startswith("video/"):
                normalized_assets.append(asset)
                normalized_contents.append(content)
                continue
            try:
                normalized = await asyncio.to_thread(
                    normalize_reference_video, Path(asset.local_path)
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "参考视频规范化失败，按原样提交 asset=%s", asset.asset_id, exc_info=True
                )
                normalized = None
            # 画幅跟随：把参考视频裁成任务要的比例（否则 adaptive 会把成片带成横屏）
            try:
                source = normalized or Path(asset.local_path)
                reframed = await asyncio.to_thread(
                    reframe_reference_video, source, aspect_ratio
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "参考视频画幅归一失败，按原样提交 asset=%s", asset.asset_id, exc_info=True
                )
                reframed = None
            if reframed is not None:
                _LOGGER.info(
                    "参考视频已裁成任务画幅 %s：asset=%s -> %s",
                    aspect_ratio,
                    asset.asset_id,
                    reframed.name,
                )
                normalized = reframed
            if normalized is None:
                normalized_assets.append(asset)
                normalized_contents.append(content)
                continue
            data = normalized.read_bytes()
            _LOGGER.info(
                "参考视频已规范化（H.264+AAC-LC）asset=%s -> %s",
                asset.asset_id,
                normalized.name,
            )
            normalized_assets.append(
                asset.model_copy(
                    update={
                        "local_path": normalized,
                        "size": len(data),
                        "sha256": sha256(data).hexdigest(),
                    }
                )
            )
            normalized_contents.append(data)
        return normalized_assets, normalized_contents

    def _clamp_task_to_capability(self, task: GenerationTask) -> GenerationTask:
        """提交前把 `resolution` / `duration` 夹进本通道能力表允许的范围。

        见 `submit` 里的注释：这是**最后一道关卡**（计划期归一覆盖不到「复用已批准计划」
        与审批页改参这两条路）。`duration` 超范围时上游同样会拒（2026-10-10 的
        `duration must be less than or equal to` 教训），所以一并夹。
        """
        capability = self._capability
        if capability is None:
            return task
        update: dict[str, object] = {}
        resolution = _supported_resolution(task.resolution, capability)
        if resolution != task.resolution:
            _LOGGER.warning(
                "分辨率按能力表夹取：%s → %s（task=%s 原值来自计划/复用/人工编辑）",
                task.resolution,
                resolution,
                task.task_id,
            )
            update["resolution"] = resolution
        duration = task.duration
        if isinstance(duration, int) and not isinstance(duration, bool):
            clamped = min(capability.duration_max, max(capability.duration_min, duration))
            if clamped != duration:
                _LOGGER.warning(
                    "时长按能力表夹取：%s → %s 秒（task=%s）",
                    duration,
                    clamped,
                    task.task_id,
                )
                update["duration"] = clamped
        return task.model_copy(update=update) if update else task

    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
        *,
        submission_id: str | None = None,
    ) -> ProviderSubmission:
        # This is a local crash-correlation token owned by the orchestration layer.
        # Ark does not support client-assigned task IDs, so it must not cross the API.
        del submission_id
        # 2026-10-15 根治「拯救香水 generation_invalid_resolution」：
        # 计划期的能力归一（`normalize_video_task_payload`）只覆盖「生成新计划 → 校验」
        # 这条路；走「复用上一版已批准计划」或审批页改过参数时**根本不经过它**，
        # 脏值（生产真值 `720*1280`）直达上游被判 `Input validation failed`。
        # 所以在**提交前**再夹一次 —— 这是最后一道关卡，无论值从哪来都成立。
        task = self._clamp_task_to_capability(task)
        if self._image_url_resolver is not None:
            # 单一判据：视频要么直传上游，要么由执行层降级成抽帧 —— 适配层不再
            # 静默改写引用（2026-10-08 合并两处互相矛盾的摘除逻辑）。
            self._reject_video_references(task)
            task, assets = self._strip_audio_references(task, assets)
        references, ordered_assets, contents = self._validate_submission(task, assets)
        # 2026-10-15（用户报「手工串珠任务没有成功过一次」）：**上传之前**把参考视频
        # 规范化成上游能接受的规格（H.264 + AAC-LC）。同一份参考视频实测：
        # 音轨 HE-AAC → 生产 6 次全败（invalid_media / Bad Request）；
        # 转成 AAC 后同一条通道的 4 秒探针一次成功。不转就必然被拒，转了才对得上。
        ordered_assets, contents = await self._normalize_video_references(
            ordered_assets,
            contents,
            aspect_ratio=task.aspect_ratio,
        )
        task = self._apply_frame_ratio_constraint(task, references)
        request_content: list[dict[str, Any]] = [
            {"type": "text", "text": self._prompt(task, references)}
        ]
        for reference, asset, content in zip(
            references, ordered_assets, contents, strict=True
        ):
            if asset.mime_type.startswith("image/"):
                # 2026-11-02（用户报「记忆碎片任务失败：expected the aspect ratio to be
                # between 0.39 and 2.50, but received image with aspect ratio: 2.76」）：
                # **参考图的长宽比也是上游硬约束** —— 文档里的分镜表切片经常是"细长条"
                # （那一批实测 411×1135=0.362、280×766=0.366、640×2004=0.319，长边/短边
                # 2.76/2.74/3.13），一提交就被拒。**这不该让用户去改图**，提交前补齐即可。
                content = self._fit_reference_aspect_ratio(asset, content)
                content = self._ensure_reference_pixel_floor(asset, content)
                if self._image_url_resolver is not None:
                    url = await self._image_url_resolver(task, reference, asset, content)
                    if not isinstance(url, str) or not url.startswith("asset://"):
                        raise self._validation_error(task.task_id, "真人资产引用无效", "cause=invalid_asset_url")
                elif self._public_media_host is not None:
                    try:
                        url = await self._public_media_host.upload(
                            content, asset.local_path.name, asset.mime_type
                        )
                    except PublicMediaUploadError as exc:
                        raise self._error(
                            ErrorCategory.TRANSIENT,
                            str(exc),
                            "operation=public_media_upload",
                            retryable=True,
                        ) from None
                else:
                    encoded = base64.b64encode(content).decode("ascii")
                    url = f"data:{asset.mime_type};base64,{encoded}"
                request_content.append({"type": "image_url", "image_url": {"url": url}, "role": reference.role})
                continue
            if self._public_media_host is None:
                raise self._validation_error(task.task_id, "未配置参考音视频临时托管", "cause=missing_public_media_host")
            try:
                url = await self._public_media_host.upload(content, asset.local_path.name, asset.mime_type)
            except PublicMediaUploadError as exc:
                raise self._error(
                    ErrorCategory.TRANSIENT,
                    str(exc),
                    "operation=public_media_upload",
                    retryable=True,
                ) from None
            request_content.append({
                "type": "video_url" if reference.role == "reference_video" else "audio_url",
                "video_url" if reference.role == "reference_video" else "audio_url": {"url": url},
                "role": reference.role,
            })
        payload = {
            "model": self._model,
            "content": request_content,
            "duration": task.duration,
            "ratio": task.aspect_ratio,
            "resolution": task.resolution,
            # 声音默认开启（用户 2026-09-21：「所有文档默认把声音打开」）：
            # 以前是 bool(task.generate_audio)，未填写(None) 会静默变成静音，
            # 于是「有的文档没有声音」。现在只有**显式 false**（文档要求静音，
            # 或用户在审批页手动关掉）才不出声。
            "generate_audio": task.generate_audio is not False,
            "watermark": False,
        }
        body = await self._request_json(
            "POST",
            f"{self._base_url}/contents/generations/tasks",
            json_body=payload,
            operation="submit",
        )
        provider_task_id = self._official_task_id(body.get("id"), "submit")
        status = self._status(body.get("status", "queued"), "submit")
        if status in _TERMINAL_FAILURE_STATUSES:
            raise self._terminal_status_error(
                "submit", status, provider_error=body.get("error")
            )
        result_items = (
            [self._video_result(body, operation="submit")]
            if status in {"success", "succeeded"}
            else []
        )
        return ProviderSubmission(
            provider=self._provider_name,
            provider_task_id=provider_task_id,
            status="succeeded" if status == "success" else status,
            result_items=result_items,
        )

    async def poll(
        self,
        submission: ProviderSubmission,
    ) -> ProviderSubmission:
        if submission.provider != self._provider_name:
            raise self._error(
                ErrorCategory.VALIDATION,
                "只能轮询 Seedance 提交",
                "operation=poll; cause=provider_mismatch",
            )
        try:
            provider_task_id = self._official_task_id(
                submission.provider_task_id, "poll"
            )
        except AgentError:
            raise self._error(
                ErrorCategory.VALIDATION,
                "Seedance 任务 ID 无效",
                "operation=poll; cause=invalid_provider_task_id",
            ) from None

        task_path = quote(provider_task_id, safe="")
        body = await self._request_json(
            "GET",
            f"{self._base_url}/contents/generations/tasks/{task_path}",
            json_body=None,
            operation="poll",
        )
        returned_id = body.get("id")
        if returned_id is not None:
            official_returned_id = self._official_task_id(returned_id, "poll")
            if official_returned_id != provider_task_id:
                raise self._provider_error(
                    "Seedance 返回了不匹配的任务 ID",
                    "operation=poll; cause=provider_task_id_mismatch",
                )
        status = self._status(body.get("status"), "poll")
        if status in _NONTERMINAL_STATUSES:
            return ProviderSubmission(
                provider=self._provider_name,
                provider_task_id=provider_task_id,
                status=status,
            )
        if status in _TERMINAL_FAILURE_STATUSES:
            # 任务失败时火山的 body 里带 `error.code/message`（例如
            # `InvalidParameter: input media detect failed: invalid_media`）。
            # 以前只报「视频任务未成功完成（poll_http_failed）」，用户根本不知道
            # 是参考素材无效 —— 现在把原因带出来（用户 2026-09-18）。
            raise self._terminal_status_error(
                "poll", status, provider_error=body.get("error")
            )
        if status not in {"succeeded", "success"}:
            raise self._provider_error(
                "Seedance 返回了未知任务状态",
                "operation=poll; cause=invalid_status",
            )

        result = self._video_result(body, operation="poll")
        return ProviderSubmission(
            provider=self._provider_name,
            provider_task_id=provider_task_id,
            status="succeeded",
            result_items=[result],
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None,
        operation: str,
    ) -> dict[str, Any]:
        try:
            async with self._http_client.stream(
                method,
                url,
                headers={
                    "Authorization": (
                        "Bearer " + self._api_key.get_secret_value()
                    )
                },
                json=json_body,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    provider_code, provider_message = (
                        await self._safe_provider_error_reason(response)
                    )
                    raise self._http_error(
                        operation,
                        response.status_code,
                        provider_code=provider_code,
                        provider_message=provider_message,
                    )
                declared_size = response.headers.get("content-length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > self._max_response_bytes:
                            raise self._provider_error(
                                "Seedance 响应超过大小限制",
                                f"operation={operation}; cause=response_too_large",
                            )
                    except ValueError:
                        pass
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > self._max_response_bytes:
                        raise self._provider_error(
                            "Seedance 响应超过大小限制",
                            f"operation={operation}; cause=response_too_large",
                        )
                    raw.extend(chunk)
        except AgentError:
            raise
        except httpx.TransportError as exc:
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.TRANSIENT,
                    message="连接 Seedance 服务失败，请稍后重试",
                    technical_detail=(
                        f"operation={operation}; cause={type(exc).__name__}"
                    ),
                    retryable=True,
                )
            ) from None

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise self._provider_error(
                "Seedance 返回了无法解析的响应",
                f"operation={operation}; cause=invalid_json",
            ) from None
        if not isinstance(payload, dict):
            raise self._provider_error(
                "Seedance 返回的数据结构无效",
                f"operation={operation}; cause=non_object_json",
            )
        return payload

    @staticmethod
    @staticmethod
    async def _safe_provider_error_code(response: httpx.Response) -> str | None:
        code, _message = await SeedanceVideoGenerator._safe_provider_error_reason(
            response
        )
        return code

    @staticmethod
    async def _safe_provider_error_reason(
        response: httpx.Response,
    ) -> tuple[str | None, str | None]:
        """从供应商的拒绝响应里取出 `(code, message)`。

        用户 2026-09-18：「Seedance 2.5 生成服务拒绝了请求（submit_http_400）」——
        以前只留了 HTTP 状态码，火山的错误码和消息都没落盘，排查只能猜。现在两个都取，
        消息会进 technical_detail（`provider_message=`）并附在用户可见的失败原因后面。
        """
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > 65_536:
                return None, None
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        error = payload.get("error")
        source = error if isinstance(error, dict) else payload
        code = source.get("code")
        if (
            not isinstance(code, str)
            or not code
            or len(code) > 80
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in code
            )
        ):
            code = None
        message = source.get("message")
        if isinstance(message, str):
            message = " ".join(message.split())[:200]
            message = _redact_secret_like(message)
        else:
            message = None
        return code, message or None

    def _video_result(
        self,
        body: dict[str, Any],
        *,
        operation: str,
    ) -> ProviderResult:
        candidates = self._collect_video_url_candidates(
            body,
            operation=operation,
        )
        video_urls: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate:
                raise self._invalid_result(operation, "invalid_video_url")
            self._validate_result_url(candidate, operation=operation)
            if candidate not in video_urls:
                video_urls.append(candidate)
        if not video_urls:
            raise self._invalid_result(operation, "missing_video_url")
        if len(video_urls) != 1:
            raise self._invalid_result(operation, "multiple_video_urls")
        return ProviderResult(
            url=video_urls[0],
            url_trust="untrusted",
            mime_type="video/mp4",
        )

    def _collect_video_url_candidates(
        self,
        body: dict[str, Any],
        *,
        operation: str,
        depth: int = 0,
    ) -> list[Any]:
        if depth > 16:
            raise self._invalid_result(operation, "nested_data_too_deep")
        candidates: list[Any] = []
        content = body.get("content")
        if isinstance(content, dict):
            mime_type = content.get("mime_type")
            if mime_type is not None and mime_type != "video/mp4":
                raise self._invalid_result(operation, "invalid_mime")
            self._append_mapping_values(
                candidates,
                content,
                ("video_url", "videoUrl"),
            )
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    self._append_mapping_values(
                        candidates,
                        item,
                        ("video_url", "videoUrl"),
                        unwrap_url_object=True,
                    )
                    if item.get("type") == "video_url":
                        self._append_mapping_values(
                            candidates,
                            item,
                            ("url",),
                        )

        nested = body.get("data")
        if isinstance(nested, dict):
            candidates.extend(
                self._collect_video_url_candidates(
                    nested,
                    operation=operation,
                    depth=depth + 1,
                )
            )
        self._append_mapping_values(
            candidates,
            body,
            ("video_url", "videoUrl"),
        )
        results = body.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    self._append_mapping_values(
                        candidates,
                        item,
                        ("url", "video_url"),
                    )
        return candidates

    @staticmethod
    def _append_mapping_values(
        candidates: list[Any],
        mapping: dict[str, Any],
        keys: tuple[str, ...],
        *,
        unwrap_url_object: bool = False,
    ) -> None:
        for key in keys:
            if key not in mapping:
                continue
            value = mapping[key]
            if unwrap_url_object and isinstance(value, dict):
                value = value.get("url")
            candidates.append(value)

    def _validate_result_url(self, value: str, *, operation: str) -> None:
        if value != value.strip() or len(value) > 8192:
            raise self._invalid_result(operation, "unsafe_video_url")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError):
            raise self._invalid_result(operation, "unsafe_video_url") from None
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme != "https"
            or not hostname
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or self._is_obviously_local_host(hostname)
        ):
            raise self._invalid_result(operation, "unsafe_video_url")

    @staticmethod
    def _is_obviously_local_host(value: str | None) -> bool:
        hostname = (value or "").rstrip(".").lower()
        if (
            not hostname
            or hostname == "localhost"
            or hostname.endswith(".localhost")
            or hostname.endswith(".local")
            or hostname.endswith(".internal")
        ):
            return True
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return not address.is_global

    def _official_task_id(self, value: Any, operation: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 512
            or any(
                unicodedata.category(character) == "Cc"
                for character in value
            )
        ):
            raise self._provider_error(
                "Seedance 返回的任务 ID 无效",
                f"operation={operation}; cause=invalid_provider_task_id",
            )
        return value

    def _status(self, value: Any, operation: str) -> str:
        if not isinstance(value, str) or value not in (
            _NONTERMINAL_STATUSES
            | _TERMINAL_FAILURE_STATUSES
            | {"succeeded", "success"}
        ):
            raise self._provider_error(
                "Seedance 返回了未知任务状态",
                f"operation={operation}; cause=invalid_status",
            )
        return value

    def _validate_submission(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
    ) -> tuple[list[Any], list[MediaAsset], list[bytes]]:
        self._validate_video_parameters(task)
        references = sorted(task.reference_images, key=lambda item: item.order)
        prompt_length = len(self._prompt(task, references))
        if prompt_length > SEEDANCE_PROMPT_SUBMIT_MAX_CHARS:
            raise self._validation_error(
                task.task_id,
                f"Seedance 提示词过长（{prompt_length} 字，上限 {SEEDANCE_PROMPT_SUBMIT_MAX_CHARS} 字），请精简后重试",
                "cause=prompt_too_long",
            )
        if not references and not assets:
            # Seedance 的文生视频模式：content 只保留 text，不传任何参考素材。
            return [], [], []
        if not references or not assets:
            raise self._validation_error(
                task.task_id,
                "图生视频任务至少需要一张参考图片",
                "cause=missing_reference",
            )

        reference_ids = [reference.asset_id for reference in references]
        reference_orders = [reference.order for reference in references]
        if len(reference_ids) != len(set(reference_ids)):
            raise self._validation_error(
                task.task_id,
                "任务重复引用了同一图片",
                "cause=duplicate_reference",
            )
        if reference_orders != list(range(1, len(references) + 1)):
            raise self._validation_error(
                task.task_id,
                "参考图片顺序必须从 1 连续递增",
                "cause=order_mismatch",
            )
        if any(reference.role not in _REFERENCE_ROLES for reference in references):
            raise self._validation_error(
                task.task_id,
                "参考图片角色不受支持",
                "cause=invalid_role",
            )
        self._validate_reference_roles(task, references)

        asset_ids = [asset.asset_id for asset in assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise self._validation_error(
                task.task_id,
                "传入素材包含重复图片",
                "cause=duplicate_input_asset",
            )
        if set(asset_ids) != set(reference_ids):
            raise self._validation_error(
                task.task_id,
                "传入素材与任务引用不一致",
                "cause=asset_mapping_mismatch",
            )
        by_id = {asset.asset_id: asset for asset in assets}
        ordered_assets = [by_id[asset_id] for asset_id in reference_ids]

        expected_stats: list[os.stat_result] = []
        total_size = 0
        for reference, asset in zip(references, ordered_assets, strict=True):
            valid_role = (
                (asset.mime_type in _IMAGE_MIME_TYPES and reference.role in {"reference_image", "first_frame", "last_frame"})
                or (asset.mime_type.startswith("video/") and reference.role == "reference_video")
                or (asset.mime_type.startswith("audio/") and reference.role == "reference_audio")
            )
            if not valid_role:
                raise self._validation_error(
                    task.task_id,
                    "参考素材类型与用途不匹配",
                    f"asset_id={asset.asset_id}; cause=invalid_mime_or_role",
                )
            if asset.download_error is not None:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片下载失败",
                    "cause=download_error",
                )
            if (
                asset.size <= 0
                or not isinstance(asset.sha256, str)
                or len(asset.sha256) != 64
            ):
                raise self._document_error(
                    asset.asset_id,
                    "参考图片元数据无效",
                    "cause=invalid_metadata",
                )
            try:
                file_stat = asset.local_path.lstat()
            except OSError as exc:
                raise self._document_error(
                    asset.asset_id,
                    "无法读取参考图片",
                    f"cause={type(exc).__name__}",
                ) from None
            if not stat.S_ISREG(file_stat.st_mode):
                raise self._document_error(
                    asset.asset_id,
                    "参考图片路径不安全",
                    "cause=unsafe_file",
                )
            if file_stat.st_size > self._max_input_bytes:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片超过大小限制",
                    "cause=input_too_large",
                )
            if file_stat.st_size != asset.size:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片完整性校验失败",
                    "cause=content_mismatch",
                )
            total_size += file_stat.st_size
            if (
                self._enforce_total_input_bytes
                and total_size > self._max_total_input_bytes
            ):
                raise self._document_error(
                    asset.asset_id,
                    "参考图片总量超过大小限制",
                    "cause=total_input_too_large",
                )
            expected_stats.append(file_stat)

        contents: list[bytes] = []
        for asset, expected_stat in zip(
            ordered_assets, expected_stats, strict=True
        ):
            content = self._read_verified_asset(asset, expected_stat)
            if asset.mime_type.startswith("image/"):
                self._validate_image_content(asset, content)
            contents.append(content)
        return references, ordered_assets, contents

    def _validate_video_parameters(self, task: GenerationTask) -> None:
        if task.task_type.value != "image_to_video":
            raise self._validation_error(
                task.task_id,
                "Seedance 只接受图生视频任务",
                "cause=unsupported_task_type",
            )
        duration_min = self._capability.duration_min if self._capability else 4
        duration_max = self._capability.duration_max if self._capability else 15
        aspect_ratios = (
            self._capability.aspect_ratios
            if self._capability
            else _ASPECT_RATIOS
        )
        resolutions = (
            self._capability.resolutions
            if self._capability
            else _RESOLUTIONS
        )
        if (
            not isinstance(task.duration, int)
            or isinstance(task.duration, bool)
            or not duration_min <= task.duration <= duration_max
        ):
            raise self._validation_error(
                task.task_id, "视频时长无效", "cause=invalid_duration"
            )
        if task.aspect_ratio not in aspect_ratios:
            raise self._validation_error(
                task.task_id, "视频比例无效", "cause=invalid_aspect_ratio"
            )
        if task.resolution not in resolutions:
            raise self._validation_error(
                task.task_id, "视频分辨率无效", "cause=invalid_resolution"
            )
        if task.generate_audio is not None and not isinstance(
            task.generate_audio, bool
        ):
            raise self._validation_error(
                task.task_id, "音频参数无效", "cause=invalid_generate_audio"
            )
        if task.output_count != 1:
            raise self._validation_error(
                task.task_id, "Seedance 每次只生成一个视频", "cause=output_count"
            )

    def _validate_reference_roles(
        self, task: GenerationTask, references: list[Any]
    ) -> None:
        roles = [reference.role for reference in references]
        if task.reference_mode == "first_last_frame":
            if roles != ["first_frame", "last_frame"]:
                raise self._validation_error(
                    task.task_id,
                    "首尾帧模式必须且只能按顺序指定一张首帧和一张尾帧",
                    "cause=invalid_first_last_frame_mode",
                )
            return
        if task.reference_mode == "multi_reference":
            if any(role in {"first_frame", "last_frame"} for role in roles):
                raise self._validation_error(
                    task.task_id,
                    "多参考模式不能使用首帧或尾帧",
                    "cause=invalid_multi_reference_mode",
                )
            return
        if "reference_image" in roles and any(
            role in {"first_frame", "last_frame"} for role in roles
        ):
            raise self._validation_error(
                task.task_id,
                "普通参考图不能与首尾帧混用",
                "cause=mixed_reference_roles",
            )
        if roles.count("first_frame") > 1 or roles.count("last_frame") > 1:
            raise self._validation_error(
                task.task_id,
                "首帧或尾帧只能各指定一张",
                "cause=duplicate_frame_role",
            )
        if "last_frame" in roles and "first_frame" not in roles:
            raise self._validation_error(
                task.task_id,
                "指定尾帧时必须同时指定首帧",
                "cause=last_frame_without_first",
            )
        if "first_frame" in roles and "last_frame" in roles:
            if roles.index("first_frame") > roles.index("last_frame"):
                raise self._validation_error(
                    task.task_id,
                    "首帧必须排在尾帧之前",
                    "cause=frame_order",
                )

    def _read_verified_asset(
        self,
        asset: MediaAsset,
        expected_stat: os.stat_result,
    ) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(asset.local_path, flags)
        except OSError as exc:
            raise self._document_error(
                asset.asset_id,
                "无法读取参考图片",
                f"cause={type(exc).__name__}",
            ) from None
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or self._file_identity(opened_stat)
                != self._file_identity(expected_stat)
            ):
                raise self._document_error(
                    asset.asset_id,
                    "参考图片路径不安全",
                    "cause=file_replaced",
                )
            content = bytearray()
            digest = sha256()
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                if len(content) + len(chunk) > self._max_input_bytes:
                    raise self._document_error(
                        asset.asset_id,
                        "参考图片超过大小限制",
                        "cause=input_too_large",
                    )
                content.extend(chunk)
                digest.update(chunk)
            final_stat = os.fstat(descriptor)
        except AgentError:
            raise
        except OSError as exc:
            raise self._document_error(
                asset.asset_id,
                "无法读取参考图片",
                f"cause={type(exc).__name__}",
            ) from None
        finally:
            os.close(descriptor)

        try:
            path_stat = asset.local_path.lstat()
        except OSError as exc:
            raise self._document_error(
                asset.asset_id,
                "参考图片路径在读取期间发生变化",
                f"cause={type(exc).__name__}",
            ) from None
        if (
            self._file_identity(final_stat) != self._file_identity(expected_stat)
            or self._file_identity(path_stat) != self._file_identity(expected_stat)
            or len(content) != asset.size
            or digest.hexdigest() != asset.sha256
        ):
            raise self._document_error(
                asset.asset_id,
                "参考图片完整性校验失败",
                "cause=content_mismatch",
            )
        return bytes(content)

    @staticmethod
    def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
        )

    #: 上游对**参考图长宽比**的硬约束（Ark 原文：`expected the aspect ratio to be between
    #: 0.39 and 2.50`）。这里留出更宽的余量（0.42 / 2.40）并向上取整 —— 边界值四舍五入
    #: 差一个像素就又会贴到上限上被拒（实测 306/766=0.3995 就落在 0.40 的刀口上）。
    _REFERENCE_ASPECT_MIN = 0.42
    _REFERENCE_ASPECT_MAX = 2.40

    #: 上游对**参考图总像素数**的硬下限（r2v）。Ark 原文（2026-11-05 生产实测）：
    #: `the parameter image pixel count specified in the request must be greater than
    #: or equal to 90000 for model doubao-seedance-2-5 in r2v`。
    #:
    #: ⚠️ 与「每边 ≥300px」是**并列的两条**约束，互不蕴含（300x300 正好 90,000 是边界；
    #: 340x260 = 88,400 两边都过 300px 却仍不达标）。`graph/nodes.py` 的
    #: `_REFERENCE_MIN_PIXELS` 是同一个数 —— 那处管"执行前整理素材"，这里管
    #: **提交那一刻真正发出去的字节**（提交路径上还有换去水印版等会换掉素材的环节，
    #: 只有这里能兜住所有情况）。两处都改时记得一起改。
    _REFERENCE_MIN_PIXELS = 90_000

    def _fit_reference_aspect_ratio(
        self, asset: MediaAsset, content: bytes
    ) -> bytes:
        """参考图长宽比超范围时**补边**到合法区间（内容一个像素都不裁）。

        为什么是补边不是裁剪：这些图是文档里的分镜表切片（细长条），裁掉两头会丢掉
        用户写在那里的分镜内容；补边只加背景色，内容完整保留。

        补边颜色取**四边像素的中位色**（分镜表通常是纯色底，视觉上几乎看不出补过）。
        任何异常都返回原字节 —— 这只是"让它能提交"，绝不能因为它把任务弄失败。
        """
        try:
            with Image.open(BytesIO(content)) as image:
                image.load()
                width, height = image.size
                if not width or not height:
                    return content
                ratio = width / height
                if (
                    self._REFERENCE_ASPECT_MIN
                    <= ratio
                    <= self._REFERENCE_ASPECT_MAX
                ):
                    return content
                if ratio < self._REFERENCE_ASPECT_MIN:
                    # +1：向上取整，保证补完**严格**落在区间内（浮点/取整都不贴边）
                    target_w = int(height * self._REFERENCE_ASPECT_MIN) + 1
                    target_h = height
                else:
                    target_w = width
                    target_h = int(width / self._REFERENCE_ASPECT_MAX) + 1
                canvas = Image.new(
                    image.mode if image.mode in {"RGB", "RGBA"} else "RGB",
                    (target_w, target_h),
                    _border_median_color(image),
                )
                canvas.paste(
                    image,
                    ((target_w - width) // 2, (target_h - height) // 2),
                )
                buffer = BytesIO()
                canvas.save(buffer, format="PNG")
        except Exception:  # noqa: BLE001 - 补边失败就按原图提交（不因为增量能力失败）
            _LOGGER.warning(
                "Seedance 参考图长宽比补齐失败，按原图提交 asset=%s",
                asset.asset_id,
                exc_info=True,
            )
            return content
        _LOGGER.warning(
            "Seedance 参考图长宽比超范围（%dx%d ratio=%.3f），已补边到 %dx%d 后提交 "
            "asset=%s",
            width,
            height,
            ratio,
            target_w,
            target_h,
            asset.asset_id,
        )
        return buffer.getvalue()

    def _ensure_reference_pixel_floor(
        self, asset: MediaAsset, content: bytes
    ) -> bytes:
        """提交前**最后一道**尺寸保障：总像素不足 90,000 就地等比放大。

        为什么必须在**适配层**再做一次（2026-11-05 生产事故，同一报错连修两次）：

            上游原文：The parameter `content[6]` specified in the request is not
            valid: the parameter image pixel count specified in the request must be
            greater than or equal to 90000 for model doubao-seedance-2-5 in r2v

        第一版只在 `graph/nodes.py` 的「执行前整理素材」里加了这条判据，但**提交路径
        上还有别的环节会替换素材**，被替换进来的文件没经过那道闸：

        - `nodes._clean_media_assets` 在提交前把素材换成台账里的**去水印版**
          （`_with_cleaned_file`）—— 清洗是**整张重绘**，实测清洗版可以比原图小得多
          （`193x261` = 50,373 像素、`190x147` = 27,930 像素，都远低于 90,000），
          而 `_upscale_reference` 已经因为"尺寸达标"跳过了；
        - 同族的还有台账复用（跨运行沿用旧结论）、比例补齐（`_fit_reference_aspect_ratio`
          会换一份新字节）。

        所以判据放在**真正要发出去的那份字节**上：上游认的、也是我们唯一能保证的，
        就是这里 `content` 的像素数。与既有的比例补齐同一个模式 —— 能就地修好的
        硬约束，不该丢给用户去改图（用户 2026-09-18 口径：「不要拦截，能不能帮我
        自动等比例放大」）。

        任何异常都返回原字节 —— 这只是"让它能提交"，绝不能因为它把任务弄失败。
        """
        try:
            with Image.open(BytesIO(content)) as image:
                image.load()
                width, height = image.size
                if not width or not height:
                    return content
                if reference_min_scale(width, height) <= 1:
                    return content
                target_width, target_height = reference_target_size(width, height)
                resized = image.convert("RGB").resize(
                    (target_width, target_height), Image.Resampling.LANCZOS
                )
                buffer = BytesIO()
                resized.save(buffer, format="PNG")
        except Exception:  # noqa: BLE001 - 放大失败就按原字节提交（不因增量能力失败）
            _LOGGER.warning(
                "Seedance 参考图像素补足失败，按原图提交 asset=%s",
                asset.asset_id,
                exc_info=True,
            )
            return content
        if reference_min_scale(target_width, target_height) > 1:
            # 取整/上限回缩后仍不达标（理论到不了，到了就不要装作修好了）。
            _LOGGER.warning(
                "Seedance 参考图放大后仍不达标（%dx%d），按原图提交 asset=%s",
                target_width,
                target_height,
                asset.asset_id,
            )
            return content
        _LOGGER.warning(
            "Seedance 参考图尺寸不足（%dx%d=%d 像素），已等比放大到 %dx%d 后提交 asset=%s",
            width,
            height,
            width * height,
            target_width,
            target_height,
            asset.asset_id,
        )
        # 放大可能让比例越过 0.42~2.40 的窗口（取整放大的边界效应），补一次比例。
        resized_bytes = buffer.getvalue()
        return self._fit_reference_aspect_ratio(asset, resized_bytes)

    def _validate_image_content(self, asset: MediaAsset, content: bytes) -> None:
        try:
            with Image.open(BytesIO(content)) as image:
                image_format = image.format
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            raise self._document_error(
                asset.asset_id,
                "参考图片内容无效",
                "cause=invalid_image_content",
            ) from None
        actual_mime = _IMAGE_FORMAT_MIME_TYPES.get(str(image_format).upper())
        if actual_mime != asset.mime_type:
            raise self._document_error(
                asset.asset_id,
                "参考图片格式与声明不一致",
                "cause=mime_mismatch",
            )

    @staticmethod
    def _prompt(task: GenerationTask, references: list[Any]) -> str:
        lines = [task.prompt]
        lines.append(
            "参考图映射："
            + "；".join(
                f"图片 {index}={reference.role}"
                for index, reference in enumerate(references, start=1)
            )
        )
        block = SeedanceVideoGenerator._negative_block(task, "\n\n".join(lines))
        if block:
            lines.append("必须避免：" + block)
        return "\n\n".join(lines)

    @staticmethod
    def _negative_block(task: GenerationTask, head: str) -> str:
        """按总预算裁剪「必须避免」整块，保留**最新**的条目。

        negative_constraints 是跨轮累积的：每轮返工的融合都会按全部历史要求重新
        派生一份 must_avoid 并进来（同一批要求的反复改写），于是它随返工轮数线性
        膨胀 —— 实测同一条记录：5 轮 40 条/686 字 → 6 轮 46 条/779 字 → 7 轮
        51 条/868 字。而正文本身已接近 1500，两者相加在 7 轮时顶穿上游上限，
        上游直接回 `generation_prompt_too_long`（真人通道实测：合计 2007 字过、
        2088 字被拒）。

        正文与参考图映射永不裁剪；只有这段反复改写的负向块会按预算收缩。
        `merge_requirements` 保序（旧→新），所以从尾部往前取、丢掉最旧的。

        2026-09-17：先做一次**语义去重**（`merge_negative_constraints`）再裁剪 ——
        存量的老任务里同一句约束会被攒成十几条换皮写法（墨滴任务实测 21 条里 10 条
        同义），既占掉 26% 的提交篇幅，又把「不要出现的东西」反复喂给模型。
        """
        entries = merge_negative_constraints(
            task.negative_constraints or [], []
        )
        if not entries:
            return ""
        budget = SUBMIT_TOTAL_BUDGET_CHARS - len(head) - len("\n\n必须避免：")
        kept: list[str] = []
        used = 0
        for entry in reversed(entries):
            cost = len(entry) + (1 if kept else 0)
            if kept and used + cost > budget:
                break
            kept.append(entry)
            used += cost
        kept.reverse()
        if len(kept) != len(entries):
            _LOGGER.warning(
                "提交文本超预算，「必须避免」条目已从 %d 条裁到 %d 条以塞进 %d 字上限",
                len(entries),
                len(kept),
                SUBMIT_TOTAL_BUDGET_CHARS,
            )
        return "；".join(kept)

    @staticmethod
    def _error(
        category: ErrorCategory,
        message: str,
        technical_detail: str,
        *,
        retryable: bool = False,
    ) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=category,
                message=message,
                technical_detail=technical_detail,
                retryable=retryable,
            )
        )

    @classmethod
    def _configuration_error(cls, field_name: str, cause: str) -> AgentError:
        messages = {
            "base_url": "Seedance 服务地址配置无效",
            "api_key": "Seedance API Key 未配置",
            "model": "Seedance 模型未配置",
        }
        return cls._error(
            ErrorCategory.CONFIGURATION,
            messages.get(field_name, "Seedance 大小限制配置无效"),
            f"field={field_name}; {cause}",
        )

    @classmethod
    def _validation_error(
        cls,
        task_id: str,
        message: str,
        detail: str,
    ) -> AgentError:
        return cls._error(
            ErrorCategory.VALIDATION,
            f"{message}（task_id={task_id}）",
            f"task_id={task_id}; {detail}",
        )

    @classmethod
    def _document_error(
        cls,
        asset_id: str,
        message: str,
        detail: str,
    ) -> AgentError:
        return cls._error(
            ErrorCategory.DOCUMENT,
            f"{message}（asset_id={asset_id}）",
            f"asset_id={asset_id}; {detail}",
        )

    @staticmethod
    def _http_error(
        operation: str,
        status_code: int,
        *,
        provider_code: str | None = None,
        provider_message: str | None = None,
    ) -> AgentError:
        if status_code in {401, 403}:
            category = ErrorCategory.PERMISSION
            message = "Seedance 凭证无效或没有模型权限"
            retryable = False
        elif status_code == 400:
            category = ErrorCategory.PROVIDER_TERMINAL
            message = "Seedance 拒绝了请求，请检查提示词长度、参考图数量和 duration/resolution 组合"
            retryable = False
        elif status_code == 429 or status_code >= 500:
            category = ErrorCategory.TRANSIENT
            message = "Seedance 服务暂时不可用，请稍后重试"
            retryable = True
        else:
            category = ErrorCategory.PROVIDER_TERMINAL
            message = "Seedance 拒绝了请求"
            retryable = False
        if provider_message:
            # 供应商自己的说明比我们的猜测有用得多（用户 2026-09-18 要求记录原因）。
            message = f"{message}：{provider_message}"
        return AgentError(
            ErrorDetail(
                category=category,
                message=message,
                technical_detail="; ".join(
                    part
                    for part in (
                        f"operation={operation}",
                        f"status={status_code}",
                        f"provider_code={provider_code}" if provider_code else None,
                        f"provider_message={provider_message}" if provider_message else None,
                    )
                    if part is not None
                ),
                retryable=retryable,
            )
        )

    @staticmethod
    def _provider_error(message: str, technical_detail: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=message,
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    def _terminal_status_error(
        self,
        operation: str,
        status: str,
        *,
        provider_error: object = None,
    ) -> AgentError:
        code = message = None
        if isinstance(provider_error, dict):
            raw_code = provider_error.get("code")
            raw_message = provider_error.get("message")
            code = raw_code if isinstance(raw_code, str) and raw_code else None
            if isinstance(raw_message, str) and raw_message:
                message = _redact_secret_like(" ".join(raw_message.split())[:200])
        text = "Seedance 视频任务未成功完成"
        if message:
            text = f"{text}：{message}"
        detail = "; ".join(
            part
            for part in (
                f"operation={operation}",
                f"status={status}",
                f"provider_code={code}" if code else None,
                f"provider_message={message}" if message else None,
            )
            if part is not None
        )
        return self._provider_error(text, detail)

    def _invalid_result(self, operation: str, cause: str) -> AgentError:
        return self._provider_error(
            "Seedance 视频结果无效",
            f"operation={operation}; cause={cause}",
        )
