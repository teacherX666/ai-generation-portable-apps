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


# --- 2026-09-28：把「画面信息」从背景资料升级成诊断依据 + 最小改动 ---


async def test_fusion_system_prompt_limits_the_edit_to_the_requested_gap() -> None:
    """用户 2026-09-28：「它会改一些我没有说的问题，即使本来是对的，它也要加一些词」。

    旧提示词通篇只讲"你是改写器""不得删减"，没有一句约束**改动范围** ——
    于是它把看片描述里的杂物写成禁令、把旧要求重新注入正文。
    """
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"])

    system = model.requests[0][0]["content"]
    assert "只改" in system
    assert "没提出" in system or "没说过的" in system


async def test_fusion_system_prompt_forbids_picture_derived_constraints() -> None:
    """画面描述只能用来定位差距，不许当成"新增禁止项"的来源。"""
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"])

    system = model.requests[0][0]["content"]
    assert "画面描述" in system
    assert "核对结论" in system


async def test_fusion_request_uses_the_comparison_conclusion() -> None:
    """带提示词的看片会产出「要求 X 而画面是 Y」—— 融合要认出这是核对结论。"""
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt(
        "原始提示词",
        ["汽车是刚体"],
        visual_context="要刚体不变形，实际车顶被压塌。",
    )

    user_content = model.requests[0][1]["content"]
    assert "核对结论" in user_content
    assert "实际车顶被压塌" in user_content
    # 老测试依赖这个抬头，不能改掉
    assert "上一版成片的实际画面" in user_content


async def test_fusion_retry_feedback_reaches_the_model() -> None:
    """重试时把不合格原因带给模型（否则 4 次都是同样的请求）。"""
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt(
        "原始提示词", ["动作慢一点"], retry_feedback="上一次输出与原文一致"
    )

    assert "上一次输出与原文一致" in model.requests[0][1]["content"]


# --- 2026-09-28 用户要求：「把调用改成一次多模态调用，不要纯文本」 ---


async def test_fusion_sends_the_previous_take_as_a_video_part() -> None:
    """上一版成片直接进**同一次**融合请求（video_url content part）。

    融合模型与看片模型是同一个 ds4.1 端点（`bootstrap` 里两者都用
    `settings.deepseek_base_url` / `deepseek_model` / `deepseek_api_key`），所以不需要
    先跑一次文本看片再让融合去读转述 —— 一次多模态调用即可。
    """
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)
    part = {
        "type": "video_url",
        "video_url": {"url": "https://example.invalid/take.mp4"},
    }

    await planner.fuse_rework_prompt(
        "@图片1 中的猫在跑", ["动作慢一点"], media_parts=[part]
    )

    content = model.requests[0][1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "@图片1 中的猫在跑" in content[0]["text"]
    assert "动作慢一点" in content[0]["text"]
    assert content[1:] == [part]
    # 系统提示词仍然只有一条
    assert model.requests[0][0]["role"] == "system"


async def test_fusion_without_media_parts_stays_plain_text() -> None:
    """没传媒体时消息体仍是纯字符串（老调用点/测试替身不受影响）。"""
    model = FakeFuseModel(_FUSED)
    planner = DeepSeekPlanner(model)

    await planner.fuse_rework_prompt("原始提示词", ["动作慢一点"])

    assert isinstance(model.requests[0][1]["content"], str)
