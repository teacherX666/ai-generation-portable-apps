"""返工提示词的 AI 融合：Planner 侧的行为契约（输出 JSON，含必避清单）。"""

import json
from types import SimpleNamespace
from typing import Any

from feishu_generation_agent.integrations.planner import DeepSeekPlanner

_FUSED = json.dumps(
    {"prompt": "@图片1 中的猫缓缓走开", "must_avoid": ["画面不得出现文字"]},
    ensure_ascii=False,
)


class FakeFuseModel:
    """只够 DeepSeekPlanner 构造与一次 ainvoke 用的假模型。"""

    def __init__(self, response: object) -> None:
        self.response = response
        self.bind_calls: list[dict[str, Any]] = []
        self.requests: list[list[dict[str, Any]]] = []

    def bind(self, **kwargs: Any) -> "FakeFuseModel":
        self.bind_calls.append(kwargs)
        return self

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> object:
        self.requests.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return SimpleNamespace(content=self.response)


async def test_fusion_returns_prompt_and_must_avoid() -> None:
    planner = DeepSeekPlanner(FakeFuseModel(_FUSED))

    result = await planner.fuse_rework_prompt("@图片1 中的猫在跑", ["动作慢一点"])

    assert result == {
        "prompt": "@图片1 中的猫缓缓走开",
        "must_avoid": ["画面不得出现文字"],
    }


async def test_fusion_includes_previous_take_visual_context() -> None:
    """融合输入里必须带上「上一版成片的实际画面」—— 用户要求模型先看成片再改。"""
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt(
        "@图片1 中的猫在跑",
        ["不要参考人物形象"],
        visual_context="画面里出现了一个人物形象，占画面中心。",
    )

    user_content = model.requests[0][1]["content"]
    assert "上一版成片的实际画面" in user_content
    assert "画面里出现了一个人物形象" in user_content


async def test_fusion_omits_visual_section_without_context() -> None:
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt("@图片1 中的猫在跑", ["动作慢一点"])

    assert "上一版成片的实际画面" not in model.requests[0][1]["content"]


async def test_fusion_returns_none_when_content_is_not_json() -> None:
    planner = DeepSeekPlanner(FakeFuseModel("这就是一段普通文本"))
    assert await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"]) is None


async def test_fusion_returns_none_when_prompt_missing() -> None:
    planner = DeepSeekPlanner(
        FakeFuseModel(json.dumps({"must_avoid": ["不要发光"]}))
    )
    assert await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"]) is None


async def test_fusion_returns_none_when_model_raises() -> None:
    planner = DeepSeekPlanner(FakeFuseModel(RuntimeError("上游 500")))
    assert await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"]) is None


async def test_fusion_returns_none_when_content_blank() -> None:
    planner = DeepSeekPlanner(FakeFuseModel("   "))
    assert await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"]) is None


async def test_fusion_returns_none_without_requirements() -> None:
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    assert await planner.fuse_rework_prompt("原始提示词", []) is None
    assert model.requests == []


async def test_fusion_request_carries_base_prompt_and_every_requirement() -> None:
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt(
        "@图片1 中的人物站在大厅",
        ["手不要僵", "背景太暗"],
    )

    payload = "\n".join(
        str(message.get("content") or "") for message in model.requests[0]
    )
    assert "@图片1 中的人物站在大厅" in payload
    assert "手不要僵" in payload
    assert "背景太暗" in payload


async def test_fusion_prompt_asks_for_executable_rewrite_and_must_avoid() -> None:
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt("原始提示词", ["不要让红衣服老头跑出去"])

    system = model.requests[0][0]["content"]
    assert "must_avoid" in system
    assert "可判定" in system or "可执行" in system


async def test_fusion_does_not_add_bind_calls_to_constructor() -> None:
    """融合模型按需派生，构造期仍然只有 plan/audit 两次 bind。"""
    model = FakeFuseModel(_FUSED)
    DeepSeekPlanner(model)

    assert len(model.bind_calls) == 2