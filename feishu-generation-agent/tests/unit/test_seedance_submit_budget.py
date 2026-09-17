"""提交文本必须永远塞得进上游上限。

用户报（2026-09-17）：「生成失败：真人视频：生成参数不符合供应商要求
（generation_prompt_too_long）」。

实测同一条记录逐版演进（提交文本 = 正文 + 「必须避免：」整块）：
  5 轮返工 → 40 条负向 / 686 字 / 合计 1908 字 → 成功
  6 轮返工 → 46 条 / 779 字 / 合计 2007 字 → 成功
  7 轮返工 → 51 条 / 868 字 / 合计 2096 字 → 上游 generation_prompt_too_long

根因不是「提示词写太长」，而是 negative_constraints **跨轮无界累积**：每轮返工的
融合都按全部历史要求重新派生一份 must_avoid 再并进来（同一批要求的反复改写），
而正文本身已经接近 1500。两者相加迟早顶穿。
"""

import pytest

from feishu_generation_agent.domain.plan import (
    SEEDANCE_PROMPT_SUBMIT_MAX_CHARS,
    GenerationTask,
    ImageReference,
)
from feishu_generation_agent.integrations.seedance import (
    SUBMIT_TOTAL_BUDGET_CHARS,
    SeedanceVideoGenerator,
)


def _task(prompt: str, negatives: list[str], refs: int = 0) -> GenerationTask:
    return GenerationTask(
        task_id="t1",
        task_type="image_to_video",
        title="任务",
        source_block_ids=[],
        user_intent="意图",
        prompt=prompt,
        aspect_ratio="9:16",
        duration=15,
        resolution="720p",
        negative_constraints=negatives,
        reference_images=[
            ImageReference(asset_id=f"a{i}", role="reference_image", order=i)
            for i in range(1, refs + 1)
        ],
    )


def test_submit_text_stays_within_budget_when_negatives_explode() -> None:
    """51 条累积负向（复现用户那次）也必须压回预算内。"""
    negatives = [f"约束条目 {i}：某处不得出现某种东西" for i in range(51)]
    task = _task("正" * 1213, negatives)

    text = SeedanceVideoGenerator._prompt(task, [])

    assert len(text) <= SUBMIT_TOTAL_BUDGET_CHARS
    assert len(text) <= SEEDANCE_PROMPT_SUBMIT_MAX_CHARS
    # 预算内要尽量保留，不能一刀切全丢。
    assert "必须避免：" in text


def test_trimming_keeps_the_newest_negative_entries() -> None:
    """merge_requirements 保序（旧→新），所以要丢就丢最旧的。"""
    # 每条都写得够长，确保真的触发裁剪（短条目 60 条也塞得下）。
    negatives = [f"约束-{i:03d}：此处不得出现任何不该出现的东西" for i in range(1, 61)]
    task = _task("正" * 1213, negatives)

    text = SeedanceVideoGenerator._prompt(task, [])

    assert len(text) <= SUBMIT_TOTAL_BUDGET_CHARS
    assert "约束-060" in text        # 最新的一定留
    assert "约束-001" not in text     # 最旧的先丢


def test_mapping_and_prompt_are_never_dropped() -> None:
    """正文和参考图映射不参与裁剪 —— 裁的只有负向块。"""
    task = _task("正" * 1400, [f"约束{i}" for i in range(40)], refs=3)

    text = SeedanceVideoGenerator._prompt(task, task.reference_images)

    assert text.startswith("正" * 1400)
    assert "参考图映射：" in text
    assert len(text) <= SUBMIT_TOTAL_BUDGET_CHARS


def test_no_negatives_means_no_block() -> None:
    task = _task("正文", [])
    text = SeedanceVideoGenerator._prompt(task, [])
    assert "必须避免：" not in text
    # 参考图映射那一行是既有行为（无参考图时也留空行），此处不改动它。
    assert text == "正文\n\n参考图映射："


def test_short_negative_list_is_untouched() -> None:
    """没超预算就别动它。"""
    task = _task("正文", ["不得出现水印", "不得穿模"])
    text = SeedanceVideoGenerator._prompt(task, [])
    assert text == "正文\n\n参考图映射：\n\n必须避免：不得出现水印；不得穿模"