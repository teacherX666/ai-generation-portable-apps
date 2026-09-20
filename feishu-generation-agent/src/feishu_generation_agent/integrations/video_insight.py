"""用 ds4.1 的多模态能力**直接看视频**（不抽帧）。

用户要求（2026-09-17）：「不要抽帧，直接上传视频」+「视频基本上都没有能作为参考图的，
只能作为分镜参考」。

实测（火山方舟 `deepseek-v4-1-flash-260910`，chat/completions）：
  - `{"type":"image_url"}` 放 PNG → 200，答对颜色 ✓
  - `{"type":"video_url"}` 放 MP4 → 200，答对颜色 ✓（真的看了视频）
  - 视频塞进 `image_url` → 400 InvalidParameter（必须用 `video_url`）

所以文档里的参考视频整段送进模型，产出**分镜参考**用的文字描述，而不是被抽成一帧
冒充参考图。视频本体不内联进请求（成片动辄十几 MB），统一先传到公开图床拿 https
链接 —— 与人像通道上传素材用的是同一套 `PublicMediaHost`。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from feishu_generation_agent.domain.document import (
    MediaAsset,
    VideoReferenceAnalysis,
    VideoReferenceKind,
)
from feishu_generation_agent.integrations.public_media import (
    PublicMediaHost,
    PublicMediaUploadError,
)

_LOGGER = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你在看一段**参考视频**（文档里的素材，不是成片）。请判断它在给创作提供什么"
    "参考，只输出 JSON：\n"
    '{"kind":"character|camera_movement|editing_style|scene_style|other",'
    '"summary":"中文描述","uncertainties":["不确定的点"]}\n'
    "summary 要具体到能用来写分镜：画面里有什么主体、动作怎么发生（因果顺序）、"
    "镜头怎么运动、剪辑节奏、场景与光线、画风。不要复述这份说明。"
)

_USER_PROMPT = "这是参考视频，请按要求输出 JSON。"

#: 「审片模式」：看的是**自己生成的成片**，要顺带把可疑的穿帮挑出来。
#:
#: 用户要求（2026-09-18）：「现在重做他会不会自动看视频分析穿帮镜头啊，还是必须要我
#: 自己找问题」—— 以前只让它"描述画面"，它不会主动报问题。现在明确要求列 problems。
#:
#: 2026-09-18 追加：用户反馈「这个看的效果跟我想的不一样，没有抓住关键点，比如我的提示词
#: 明确了第 1 根枝桠三只绿色小鸟…结果最后效果不一样」—— 光列通用穿帮不够，必须**对照
#: 提示词逐条核对**（数量/颜色/位置/镜头/动作），把"要求了但没做到"的单独标出来。
_TAKE_SYSTEM_PROMPT = (
    "你在看一段 **AI 生成的成片**，同时会给你**当初写的生成提示词**。"
    "请像审片一样输出 JSON：\n"
    '{"summary":"画面实际是什么（主体/动作顺序/镜头/节奏/光线）",'
    '"problems":[{"at":"0:03","issue":"问题是什么","why":"为什么算问题",'
    '"kind":"违背要求|穿帮"}],'
    '"uncertainties":["看不清或不确定的点"]}\n'
    "**先逐条核对提示词**：主体数量、颜色、各自位置（哪根枝桠 / 画面哪个方位）、"
    "镜头运动、动作顺序 —— 凡是提示词**明确要求**而画面没做到、做错或做反的，"
    "每条列进 problems 并把 kind 写成「违背要求」，issue 里写清"
    "「要求 X，实际 Y」。\n"
    "然后列画面本身的穿帮（肢体穿模、人物悬浮、多余肢体或手指异常、动作跳变或瞬移、"
    "口型与台词对不上、道具或服装突变、画面闪烁变形、文字或屏幕内容乱码、"
    "主体一致性漂移），kind 写成「穿帮」。\n"
    "没有就返回空数组，不要硬凑；每条都要给出大致时间点（分:秒）。"
)

_TAKE_USER_PROMPT = "这是成片，请按要求输出 JSON。"


def _clean_problems(value: Any) -> list[dict[str, str]]:
    """把模型返回的 problems 洗成 [{at, issue, why}]（丢掉空项与非法项）。"""
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue") or "").strip()
        if not issue:
            continue
        cleaned.append(
            {
                "at": str(item.get("at") or "").strip(),
                "issue": issue[:200],
                "why": str(item.get("why") or "").strip()[:200],
                "kind": "违背要求" if str(item.get("kind") or "").strip() == "违背要求" else "穿帮",
            }
        )
    return cleaned


async def describe_output_videos(
    analyzer: Any,
    artifacts: list[Any] | None,
    *,
    limit: int = 1,
) -> str:
    """把**上一版成片**交给能看视频的模型，返回「实际画面 + 疑似穿帮」描述。

    用户要求（2026-09-17）：返工时把上一版成片**直接上传视频**（不抽帧）让模型看到
    实际画成了什么。以前返工只把「你打的字」喂给融合模型，它没见过成片，所以
    「不要参考人物形象」这类要求反复不生效 —— 现在把画面描述一起给它。

    2026-09-18 起改成**审片模式**：顺带列出可疑的穿帮（穿模/悬浮/跳变/口型…），
    融合时一并带上，模型才知道"上一版到底哪里不对"。

    没有分析器 / 没有视频产物 / 分析失败 → 返回空串（返工照常进行，不因为没有画面
    上下文而失败）。
    """
    if analyzer is None or not artifacts:
        return ""
    videos = [
        artifact
        for artifact in artifacts
        if str(_field(artifact, "kind") or "") == "video"
    ]
    lines: list[str] = []
    for artifact in videos[:limit]:
        local_path = _field(artifact, "local_path")
        if not local_path or not Path(str(local_path)).is_file():
            continue
        asset = MediaAsset(
            asset_id=str(_field(artifact, "artifact_id") or "artifact"),
            source_block_id=str(_field(artifact, "task_id") or ""),
            origin="generated",
            local_path=Path(str(local_path)),
            mime_type=str(_field(artifact, "mime_type") or "video/mp4"),
            size=int(_field(artifact, "size") or 0),
            sha256=str(_field(artifact, "sha256") or ""),
        )
        try:
            findings = await analyze_take(analyzer, asset)
        except Exception:
            _LOGGER.warning(
                "上一版成片分析失败，本次返工不带画面上下文 artifact=%s",
                asset.asset_id,
                exc_info=True,
            )
            continue
        summary = str(findings.get("summary") or "").strip()
        if not summary:
            continue
        suffix = (
            "（不确定：" + "；".join(findings["uncertainties"]) + "）"
            if findings.get("uncertainties")
            else ""
        )
        lines.append(f"【{asset.asset_id}】{summary}{suffix}")
        problems = findings.get("problems") or []
        if problems:
            lines.append(
                "疑似穿帮："
                + "；".join(
                    " ".join(
                        part
                        for part in (item.get("at"), item.get("issue"))
                        if part
                    )
                    for item in problems
                )
            )
    return "\n".join(lines)


#: 看片结果缓存：`sha256 -> findings`。
#:
#: 看一整段成片 ≈ 40k token（实测），而**审片**和**返工融合**看的是同一段视频 ——
#: 以前各看一遍，用户点完审片再重跑就白烧 40k（2026-09-18：「减少规划频率，提高规划
#: 质量保证」）。按文件 sha256 缓存，同一段成片只让模型看一次。
_TAKE_CACHE: dict[str, dict[str, Any]] = {}
_TAKE_CACHE_LIMIT = 24


async def analyze_take(
    analyzer: Any,
    asset: MediaAsset,
    prompt: str = "",
) -> dict[str, Any]:
    """像审片一样看**成片**：`{summary, problems, uncertainties}`。

    给了 `prompt`（当初的生成提示词）就要求它**逐条核对**要求做到了没有 ——
    用户 2026-09-18：「没有抓住关键点，比如我的提示词明确了第 1 根枝桠三只绿色小鸟…
    结果最后效果不一样」。

    结果按 `asset.sha256` 缓存：同一段成片只让模型看一次（省 ~40k token）。
    带 prompt 的核对结果**不缓存**（它跟具体提示词绑定，换了提示词结论会变）。

    分析器不支持审片模式（老实现 / 测试替身）时退回普通描述，`problems` 为空。
    """
    cache_key = ""
    if not (prompt or "").strip() and asset.sha256:
        cache_key = asset.sha256
        cached = _TAKE_CACHE.get(cache_key)
        if cached is not None:
            _LOGGER.info("看片命中缓存，跳过模型调用 asset=%s", asset.asset_id)
            return cached
    if hasattr(analyzer, "analyze_take"):
        try:
            findings = await analyzer.analyze_take(asset, prompt)
        except TypeError:
            findings = await analyzer.analyze_take(asset)
    else:
        insight = await analyzer.analyze_video(asset, [])
        findings = {
            "summary": getattr(insight, "summary", "") or "",
            "problems": [],
            "uncertainties": list(getattr(insight, "uncertainties", []) or []),
        }
    if cache_key:
        if len(_TAKE_CACHE) >= _TAKE_CACHE_LIMIT:
            _TAKE_CACHE.clear()
        _TAKE_CACHE[cache_key] = findings
    return findings


async def analyze_artifacts(
    analyzer: Any,
    artifacts: list[Any] | None,
    *,
    limit: int = 1,
    prompt: str = "",
) -> dict[str, Any]:
    """审一遍成片，返回给界面用的 `{available, summary, problems, uncertainties}`。

    用户要求（2026-09-18）：「现在重做他会不会自动看视频分析穿帮镜头啊，还是必须要我
    自己找问题」—— 这个函数就是「自动找问题」的入口：审批/成片确认页点一下就能拿到
    疑似穿帮清单，再一键采纳成返工反馈。
    """
    if analyzer is None:
        return {
            "available": False,
            "reason": "没有配置能看视频的模型",
            "summary": "",
            "problems": [],
            "uncertainties": [],
        }
    videos = [
        artifact
        for artifact in (artifacts or [])
        if str(_field(artifact, "kind") or "") == "video"
    ]
    for artifact in videos[:limit]:
        local_path = _field(artifact, "local_path")
        if not local_path or not Path(str(local_path)).is_file():
            continue
        asset = MediaAsset(
            asset_id=str(_field(artifact, "artifact_id") or "artifact"),
            source_block_id=str(_field(artifact, "task_id") or ""),
            origin="generated",
            local_path=Path(str(local_path)),
            mime_type=str(_field(artifact, "mime_type") or "video/mp4"),
            size=int(_field(artifact, "size") or 0),
            sha256=str(_field(artifact, "sha256") or ""),
        )
        try:
            findings = await analyze_take(analyzer, asset, prompt)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("审片失败 artifact=%s", asset.asset_id, exc_info=True)
            return {
                "available": False,
                "reason": f"看片失败：{type(exc).__name__}",
                "summary": "",
                "problems": [],
                "uncertainties": [],
            }
        return {
            "available": True,
            "artifact_id": asset.asset_id,
            "summary": str(findings.get("summary") or ""),
            "problems": findings.get("problems") or [],
            "uncertainties": findings.get("uncertainties") or [],
        }
    return {
        "available": False,
        "reason": "这一版没有可分析的成片文件",
        "summary": "",
        "problems": [],
        "uncertainties": [],
    }


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


class DeepSeekVideoInsight:
    """把参考视频整段交给 ds4.1 分析，返回分镜参考用的文字描述。"""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: Any,
        model: str,
        public_media_host: PublicMediaHost,
        timeout: float = 300.0,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._api_key = (
            api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key
        )
        self._model = model
        self._public_media_host = public_media_host
        self._timeout = timeout

    async def analyze_take(
        self, asset: MediaAsset, prompt: str = ""
    ) -> dict[str, Any]:
        """审片模式：看**成片**，返回 `{summary, problems, uncertainties}`。

        给了 `prompt`（当初的生成提示词）就要求它**逐条核对**要求有没有做到 ——
        否则它只会报通用穿帮，抓不到"提示词写了但没做到"这类最关键的问题。
        """
        user_prompt = _TAKE_USER_PROMPT
        requirement = (prompt or "").strip()
        if requirement:
            user_prompt = (
                "这是成片。**当初写的生成提示词**如下：\n"
                f"{requirement[:4000]}\n\n"
                "请按要求输出 JSON，重点核对提示词里明确写了的"
                "数量、颜色、各自位置、镜头运动与动作顺序。"
            )
        return await self._analyze(asset, _TAKE_SYSTEM_PROMPT, user_prompt)

    async def analyze_video(
        self,
        asset: MediaAsset,
        frames: list[Any] | None = None,
    ) -> VideoReferenceAnalysis:
        """`frames` 参数仅为兼容旧调用点保留 —— 本实现**不抽帧**。"""
        del frames
        data = await self._analyze(asset, _SYSTEM_PROMPT, _USER_PROMPT)
        try:
            kind = VideoReferenceKind(str(data.get("kind") or "other").strip())
        except ValueError:
            kind = VideoReferenceKind.OTHER
        summary = data.get("summary")
        uncertainties = data.get("uncertainties")
        return VideoReferenceAnalysis(
            asset_id=asset.asset_id,
            kind=kind,
            summary=summary.strip() if isinstance(summary, str) else "",
            uncertainties=[
                item.strip()
                for item in (uncertainties if isinstance(uncertainties, list) else [])
                if isinstance(item, str) and item.strip()
            ],
        )

    async def _analyze(
        self,
        asset: MediaAsset,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """上传整段视频 → 交给模型 → 返回解析后的 JSON 对象（两种模式共用）。"""
        if asset.download_error is not None:
            raise RuntimeError(f"视频素材读取失败：{asset.asset_id}")
        try:
            content = Path(asset.local_path).read_bytes()
        except OSError as exc:
            raise RuntimeError(f"视频素材不可读：{asset.asset_id}") from exc
        if not content:
            raise RuntimeError(f"视频素材为空：{asset.asset_id}")

        try:
            url = await self._public_media_host.upload(
                content,
                f"{asset.asset_id}{Path(str(asset.local_path)).suffix or '.mp4'}",
                asset.mime_type or "video/mp4",
            )
        except PublicMediaUploadError as exc:
            raise RuntimeError(f"参考视频上传失败：{asset.asset_id}") from exc

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "video_url", "video_url": {"url": url}},
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        try:
            response = await self._http.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"参考视频分析请求失败：{asset.asset_id}") from exc
        except ValueError as exc:
            raise RuntimeError(f"参考视频分析返回非 JSON：{asset.asset_id}") from exc

        raw = (
            ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
        )
        return self._parse(asset.asset_id, raw)

    @staticmethod
    def _parse(asset_id: str, raw: Any) -> dict[str, Any]:
        """解析模型返回的 JSON：`{summary, problems, uncertainties, kind?}`。"""
        if not isinstance(raw, str):
            raise RuntimeError(f"参考视频分析返回为空：{asset_id}")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"参考视频分析返回不是 JSON：{asset_id}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"参考视频分析返回不是对象：{asset_id}")
        summary = data.get("summary")
        uncertainties = data.get("uncertainties")
        return {
            "kind": data.get("kind"),
            "summary": summary.strip() if isinstance(summary, str) else "",
            "problems": _clean_problems(data.get("problems")),
            "uncertainties": [
                item.strip()
                for item in (uncertainties if isinstance(uncertainties, list) else [])
                if isinstance(item, str) and item.strip()
            ],
        }
