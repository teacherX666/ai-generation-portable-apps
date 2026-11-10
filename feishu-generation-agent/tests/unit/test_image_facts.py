"""方案 C 的「专门读图」部分：一次任务单纯的 DeepSeek 读图调用。

⚠️ 方案 C 里原本还有一条 `_check_reference_facts_fidelity`（读图事实 vs 提示词【素材说明】
的保真校验，会把不一致写进 `blocking_issues`）。**2026-10-28 已按用户要求整块删除**：
它的判据太粗（正则匹配「一颗/三颗/十颗」这类词），把「数十颗」这种本来就表示"多"的
写法也判成小数量，阻塞了正常审批 —— 用户原话「把这个阻塞删了，碍事」。
保留的是读图本身：它产出的结构化事实会随 `visions` 注入规划器，且失败会写进
`vision_issues`（不阻断、可见）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_generation_agent.domain.document import MediaAsset, VisionDescription
from feishu_generation_agent.integrations.image_facts import (
    DeepSeekImageFacts,
    _extract_json,
)


def _asset(asset_id: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        source_block_id=f"block-{asset_id}",
        origin="document",
        local_path=Path(f"/tmp/{asset_id}.png"),
        mime_type="image/png",
        size=1024,
        sha256=f"sha-{asset_id}",
    )


def _part(asset_id: str) -> dict:
    return {"type": "image_url", "image_url": {"url": f"https://x/{asset_id}.png"}}


class _StubModel:
    """假的 DeepSeek 多模态模型。"""

    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.calls = 0
        self.last_messages: list | None = None

    async def ainvoke(self, messages, config=None):  # noqa: ANN001, ANN201
        self.calls += 1
        self.last_messages = messages
        if isinstance(self.reply, Exception):
            raise self.reply
        return type("_Resp", (), {"content": self.reply})()


_WAX_REPLY = json.dumps(
    {
        "images": [
            {
                "index": 1,
                "regions": [
                    "左上：深红色，约占画面 35%，由大量彼此独立、大小不一的小滴组成，数量级上百",
                    "左下：蓝色，约占 25%，同样为大量彼此独立的小滴，排列较疏",
                    "右侧：浅绿色，约占 30%，连成一整片连续的片状",
                ],
                "surface": "浅色原木木纹桌面，哑光",
                "camera": "正俯拍，全景深",
                "light": "从左上方打来，色温中性偏冷（约 5500K）",
                "visible_text": [],
                "subjects": ["红色蜡滴区", "蓝色蜡滴区", "浅绿色蜡片区"],
                "colors": ["深红", "蓝", "浅绿"],
                "uncertainties": [],
            }
        ]
    },
    ensure_ascii=False,
)


@pytest.mark.asyncio
async def test_read_maps_facts_into_vision_description() -> None:
    model = _StubModel(_WAX_REPLY)
    facts, issues = await DeepSeekImageFacts(model).read(
        [_asset("image-1")], [_part("image-1")]
    )

    assert issues == []
    assert len(facts) == 1
    fact = facts[0]
    assert fact.asset_id == "image-1"
    # 可校验事实必须落进字段（规划器读它们）
    assert len(fact.regions) == 3
    assert "大量" in fact.regions[0]
    assert "连成一整片" in fact.regions[2]
    assert fact.surface.startswith("浅色原木")
    assert "俯拍" in fact.camera
    assert "中性偏冷" in fact.light
    # 读图只喂图 + 读图提示词：不应带上规划契约
    assert model.calls == 1
    system = model.last_messages[0]["content"]
    assert "事实转录" in system
    assert "Seedance" not in system


@pytest.mark.asyncio
async def test_read_failure_is_reported_not_swallowed() -> None:
    """读图失败绝不静默（2026-10-27 之前整条链路 0 日志、排查白绕）。"""
    model = _StubModel(RuntimeError("boom"))
    facts, issues = await DeepSeekImageFacts(model).read(
        [_asset("image-1")], [_part("image-1")]
    )
    assert facts == []
    assert len(issues) == 1
    assert "读图调用失败" in issues[0]


@pytest.mark.asyncio
async def test_read_rejects_non_json_and_length_mismatch() -> None:
    facts, issues = await DeepSeekImageFacts(_StubModel("我看不懂")).read(
        [_asset("image-1")], [_part("image-1")]
    )
    assert facts == [] and len(issues) == 1

    facts, issues = await DeepSeekImageFacts(_StubModel(_WAX_REPLY)).read(
        [_asset("image-1"), _asset("image-2")], [_part("image-1")]
    )
    assert facts == [] and "不一致" in issues[0]


def test_extract_json_tolerates_markdown_fence() -> None:
    fenced = "```json\n" + _WAX_REPLY + "\n```"
    assert _extract_json(fenced) is not None
    assert _extract_json("前缀废话 " + _WAX_REPLY + " 尾巴") is not None
    assert _extract_json("完全没有 JSON") is None


def test_facts_model_disables_thinking_and_asks_for_json() -> None:
    """方舟的 DeepSeek v4 不显式关思考会返回空 content（全烧在 reasoning_content）。

    判别力自检：把 `thinking` 从 disabled 改回不设置，这条必须红。
    """

    class _Copyable:
        def __init__(self) -> None:
            self.updates: list[dict] = []
            self.binds: list[dict] = []

        def model_copy(self, *, update=None):  # noqa: ANN001, ANN201
            self.updates.append(dict(update or {}))
            return self

        def bind(self, **kwargs):  # noqa: ANN003, ANN201
            self.binds.append(kwargs)
            return self

        async def ainvoke(self, messages, config=None):  # noqa: ANN001, ANN201
            return type("_R", (), {"content": "{}"})()

    model = _Copyable()
    DeepSeekImageFacts(model)
    assert model.updates, "必须用 model_copy 下发（bind 的 extra_body 不会被转发）"
    assert model.updates[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert model.binds and model.binds[0]["response_format"] == {"type": "json_object"}


def test_read_only_feeds_images_not_the_planning_contract() -> None:
    """顺手钉一条：读图事实里的 regions 会原样进 VisionDescription（规划器据此写素材说明）。"""
    fact = VisionDescription(
        asset_id="image-1",
        subjects=[],
        scene="桌面",
        style="冷光",
        composition="俯拍",
        characters=[],
        actions=[],
        visible_text=[],
        colors=["红", "蓝", "浅绿"],
        probable_role="reference_image",
        uncertainties=[],
        regions=["左上：深红，约 35%，大量独立小滴，数量级上百"],
        surface="浅色木纹桌面",
        camera="正俯拍",
        light="左上，中性偏冷",
    )
    assert "大量独立小滴" in fact.regions[0]
    assert fact.surface and fact.camera and fact.light
