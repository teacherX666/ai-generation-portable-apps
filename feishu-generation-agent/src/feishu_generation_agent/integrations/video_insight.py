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

import hashlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from feishu_generation_agent.domain.document import (
    MediaAsset,
    VideoReferenceAnalysis,
    VideoReferenceKind,
)
from feishu_generation_agent.integrations.planning_media import (
    build_planning_media_parts,
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
    '"summary":"中文描述","camera_movement":"具体运镜","editing_style":"剪辑节奏",'
    '"uncertainties":["不确定的点"]}\n'
    "summary 要具体到能用来写分镜：画面里有什么主体、动作怎么发生（因果顺序）、"
    "镜头怎么运动、剪辑节奏、场景与光线、画风。不要复述这份说明。\n"
    "**camera_movement 与 editing_style 无论 kind 判成哪一类都必须写满、不得留空**："
    "camera_movement 写具体运镜（如「缓慢推近」「从左向右摇镜」；机位不动时写"
    "「固定机位、全片无位移」）；editing_style 写剪辑节奏与转场（如「4 秒处一次硬切」"
    "「全片无剪辑、单镜头连续」）。单镜头任务没有分镜可依据，这两项是唯一能约束"
    "成片节奏的语义。"
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
    prompts: Mapping[str, str] | None = None,
    limit: int = 1,
) -> str:
    """把**上一版成片**交给能看视频的模型，返回「模型发现的问题 + 实际画面」。

    用户要求（2026-09-17）：返工时把上一版成片**直接上传视频**（不抽帧）让模型看到
    实际画成了什么。以前返工只把「你打的字」喂给融合模型，它没见过成片，所以
    「不要参考人物形象」这类要求反复不生效 —— 现在把画面描述一起给它。

    2026-09-18 起改成**审片模式**：顺带列出可疑的穿帮（穿模/悬浮/跳变/口型…），
    融合时一并带上，模型才知道"上一版到底哪里不对"。

    2026-09-28：**必须把上一版正文也交给它**（`prompts`：task_id → 当初的提示词）。
    系统提示词 `_TAKE_SYSTEM_PROMPT` 要求它「先逐条核对提示词」「写成『要求 X，
    实际 Y』」，但这条链路以前一个字都没传 —— 生产上三条记录的看片模型都自己写了
    「缺少原始提示词，无法逐条核对」。于是它只能描述画面，融合模型拿不到差距，
    只好自己发明问题（重注旧要求 / 把画面里的杂物写成禁令 / 干脆什么都不改）。

    问题清单排在画面描述**前面**：这条路的结果写进运行事件时会被截断
    （`text[:300]`），排在后面的问题会被整段截掉，用户就没法核对它识别对没有。

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
        task_prompt = str(
            (prompts or {}).get(str(_field(artifact, "task_id") or "")) or ""
        )
        try:
            findings = await analyze_take(analyzer, asset, task_prompt)
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
        problems = findings.get("problems") or []
        if problems:
            lines.append(
                "模型发现的问题（优先看这里）："
                + "；".join(
                    " ".join(
                        part
                        for part in (item.get("at"), item.get("issue"))
                        if part
                    )
                    for item in problems
                )
            )
        suffix = (
            "（不确定：" + "；".join(findings["uncertainties"]) + "）"
            if findings.get("uncertainties")
            else ""
        )
        lines.append(f"【{asset.asset_id}】{summary}{suffix}")
    return "\n".join(lines)


#: 看片结果缓存：`资产指纹[|提示词指纹] -> findings`。
#:
#: 看一整段成片 ≈ 40k token（实测），而**审片**和**返工融合**看的是同一段视频 ——
#: 以前各看一遍，用户点完审片再重跑就白烧 40k（2026-09-18：「减少规划频率，提高规划
#: 质量保证」）。同一段成片配同一版提示词只让模型看一次。
_TAKE_CACHE: dict[str, dict[str, Any]] = {}
_TAKE_CACHE_LIMIT = 24


def take_asset(artifact: Any) -> MediaAsset | None:
    """把一条**成片产物**转成 MediaAsset；不是可读视频就返回 None。

    运行视图里的产物**没有 sha256**（只有 artifact_id），而上传缓存按 sha256 建键 ——
    所以缺 sha256 时用 artifact_id 顶上（与看片缓存同一套口径，见 `_take_cache_key`）。
    """
    if str(_field(artifact, "kind") or "") != "video":
        return None
    local_path = _field(artifact, "local_path")
    if not local_path or not Path(str(local_path)).is_file():
        return None
    artifact_id = str(_field(artifact, "artifact_id") or "artifact")
    return MediaAsset(
        asset_id=artifact_id,
        source_block_id=str(_field(artifact, "task_id") or ""),
        origin="generated",
        local_path=Path(str(local_path)),
        mime_type=str(_field(artifact, "mime_type") or "video/mp4"),
        size=int(_field(artifact, "size") or 0),
        sha256=str(_field(artifact, "sha256") or artifact_id),
    )


async def build_take_media_parts(
    artifacts: list[Any] | None,
    *,
    public_media_host: Any,
    cache: Any,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """把上一版成片变成多模态 content parts（`video_url`），供**一次**融合调用直读。

    2026-09-28 用户要求：「把调用改成一次多模态调用就行了，不要纯文本」。

    融合模型与看片模型本来就是同一个 ds4.1 端点（`bootstrap` 里两者都用
    `settings.deepseek_base_url` / `deepseek_model` / `deepseek_api_key`），所以成片
    可以跟原始提示词、返工要求一起进**同一次**请求 —— 不再需要"先看片转成一段文字、
    再让融合去读那段转述"。

    上传失败 / 没有可用成片 → 返回空列表，调用方回落到文字看片（绝不因此失败）。
    """
    if public_media_host is None:
        return []
    size = max(1, limit)
    assets = [
        asset
        for artifact in (artifacts or [])[:size]
        if (asset := take_asset(artifact)) is not None
    ]
    if not assets:
        return []
    try:
        return await build_planning_media_parts(
            assets,
            public_media_host=public_media_host,
            cache=cache,
            max_items=size,
        )
    except Exception:  # noqa: BLE001 —— 成片准备失败不能挡住返工本身
        _LOGGER.warning("上一版成片上传失败，本次融合回落到文字看片", exc_info=True)
        return []


def _take_cache_key(asset: MediaAsset, prompt: str) -> str:
    """看片缓存的键：`资产指纹` 或 `资产指纹|提示词指纹`。

    资产指纹优先用内容 sha256；**运行视图里的产物没有 sha256**（只有 artifact_id），
    退回用 asset_id —— 它同样能保证「同一段成片只分析一次」。2026-09-18 实测：
    只认 sha256 时键恒为空 → 缓存从未命中 → 每次重跑都重新看片 40k → 撞 429 →
    融合失败 → 提示词连续两轮不变（用户：「为什么连续两次没有修改我的提示词了」）。

    2026-09-28：返工也带上当初的提示词了，所以键里必须**含提示词指纹** ——
    否则「同一段成片 + 换了提示词」会命中旧结论。含指纹后仍然缓存：同一段成片
    配同一版提示词（先点审片、隔一会儿再返工）只让模型看一次。
    """
    fingerprint = (asset.sha256 or asset.asset_id or "").strip()
    if not fingerprint:
        return ""
    requirement = (prompt or "").strip()
    if not requirement:
        return fingerprint
    digest = hashlib.sha256(requirement.encode("utf-8")).hexdigest()[:12]
    return f"{fingerprint}|{digest}"


async def analyze_take(
    analyzer: Any,
    asset: MediaAsset,
    prompt: str = "",
) -> dict[str, Any]:
    """像审片一样看**成片**：`{summary, problems, uncertainties}`。

    给了 `prompt`（当初的生成提示词）就要求它**逐条核对**要求做到了没有 ——
    用户 2026-09-18：「没有抓住关键点，比如我的提示词明确了第 1 根枝桠三只绿色小鸟…
    结果最后效果不一样」。

    结果按缓存键复用：同一段成片配**同一版提示词**只让模型看一次（省 ~40k token）。
    键含提示词指纹（见 `_take_cache_key`）—— 换了提示词结论会变，必须重新看。

    分析器不支持审片模式（老实现 / 测试替身）时退回普通描述，`problems` 为空。
    """
    cache_key = _take_cache_key(asset, prompt)
    if cache_key:
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
        camera_movement = data.get("camera_movement")
        editing_style = data.get("editing_style")
        return VideoReferenceAnalysis(
            asset_id=asset.asset_id,
            kind=kind,
            summary=summary.strip() if isinstance(summary, str) else "",
            # 2026-10-10（P1）：「运镜 / 剪辑节奏」改成无条件输出字段。这里对旧格式
            # 输出（没有这两个键）保持兼容：确实缺失时回落成显式的「未提供」，
            # 而不是留空字符串，免得下游再靠「空 = 未知」猜。
            camera_movement=(
                camera_movement.strip()
                if isinstance(camera_movement, str) and camera_movement.strip()
                else "未提供"
            ),
            editing_style=(
                editing_style.strip()
                if isinstance(editing_style, str) and editing_style.strip()
                else "未提供"
            ),
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
