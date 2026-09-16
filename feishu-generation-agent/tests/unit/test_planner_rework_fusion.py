"""返工提示词的 AI 融合：Planner 侧的行为契约。"""

from types import SimpleNamespace
from typing import Any

from feishu_generation_agent.integrations.planner import DeepSeekPlanner


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


async def test_fusion_returns_stripped_content() -> None:
    model = FakeFuseModel("  @图片1 中的猫缓慢走开  ")
    planner = DeepSeekPlanner(model)

    fused = await planner.fuse_rework_prompt("@图片1 中的猫在跑", ["动作慢一点"])

    assert fused == "@图片1 中的猫缓慢走开"


async def test_fusion_returns_none_when_model_raises() -> None:
    model = FakeFuseModel(RuntimeError("上游 500"))
    planner = DeepSeekPlanner(model)

    assert await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"]) is None


async def test_fusion_returns_none_when_content_blank() -> None:
    model = FakeFuseModel("   ")
    planner = DeepSeekPlanner(model)

    assert await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"]) is None


async def test_fusion_returns_none_without_requirements() -> None:
    model = FakeFuseModel("不该被调用")
    planner = DeepSeekPlanner(model)

    assert await planner.fuse_rework_prompt("原始提示词", []) is None
    assert model.requests == []


async def test_fusion_request_carries_base_prompt_and_every_requirement() -> None:
    model = FakeFuseModel("融合结果")
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


async def test_fusion_does_not_add_bind_calls_to_constructor() -> None:
    """融合模型按需派生，构造期仍然只有 plan/audit 两次 bind。"""
    model = FakeFuseModel("融合结果")
    DeepSeekPlanner(model)

    assert len(model.bind_calls) == 2