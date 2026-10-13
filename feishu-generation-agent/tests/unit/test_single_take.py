"""文档要求「一镜到底」时，规划器不许把视频拆成多个镜头。

用户 2026-09-22：「明明我的剧本规定了一镜到底啥的，只要一个镜头就能做完的视频
他还是会分成几个镜头」—— 契约里「按 duration 分 2-4 个镜头」的节奏建议是无条件
写的，直接压过了文档要求，所以这里补一道硬校验兜住。
"""

from __future__ import annotations

from types import SimpleNamespace

from feishu_generation_agent.integrations.planner import (
    _document_requests_single_take,
    validate_plan,
)


def _document(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text_view=text,
        blocks=[],
        media_assets=[],
        document_id="doc-1",
        title="测试",
        revision=1,
        requirements=[],
    )


def _plan(prompt: str) -> dict:
    return {
        "document_summary": "测试文档摘要",
        "tasks": [
            {
                "task_id": "task-1",
                "task_type": "image_to_video",
                "title": "测试",
                "source_block_ids": ["block-1"],
                "user_intent": "生成视频",
                "prompt": prompt,
                "reference_images": [],
                "aspect_ratio": "9:16",
                "duration": 5,
                "resolution": "720p",
            }
        ],
    }


_ONE_SHOT = (
    "镜头 1（0-5s）：镜头全程跟随主体推进，一镜到底不切。\n"
    "风格与约束：画面稳定、无水印、不要 logo。"
)
_THREE_SHOTS = (
    "镜头 1（0-2s）：A。\n镜头 2（2-4s）：B。\n镜头 3（4-5s）：C。\n"
    "风格与约束：画面稳定、无水印、不要 logo。"
)


def test_document_single_take_keywords() -> None:
    """文档里「一镜到底」的各种说法都要认出来。"""
    for text in (
        "本片一镜到底，全程不切。",
        "要求长镜头跟拍。",
        "单镜头完成，不要切镜头。",
        "连续拍摄，无剪辑。",
        "不要切换镜头，全程一个镜头拍完。",
        "镜头不切换，一直跟拍。",
        "不需要剪辑，跟拍到底。",
    ):
        assert _document_requests_single_take(
            SimpleNamespace(text_view=text)
        ), text
    for text in (
        "按分镜表拍三个镜头。",
        "第一镜近景、第二镜全景。",
        "视频第9秒切换到下一个镜头。",
    ):
        assert not _document_requests_single_take(
            SimpleNamespace(text_view=text)
        ), text


def _single_take_issues(prompt: str, document_text: str) -> list[str]:
    return [
        issue
        for issue in validate_plan(
            _plan(prompt),
            _document(document_text),
            4,
            enforce_seedance_prompt_contract=True,
        )
        if "一镜到底" in issue
    ]


def test_validator_rejects_splitting_a_single_take_document() -> None:
    take = "本片要求一镜到底，全程跟拍不切镜头。"

    assert _single_take_issues(_ONE_SHOT, take) == []
    # 被拆成 3 个镜头 → 判不合格（就是用户报的那个行为）
    assert _single_take_issues(_THREE_SHOTS, take)


def test_multi_shot_is_allowed_when_the_document_does_not_ask_for_one_take() -> None:
    assert _single_take_issues(_THREE_SHOTS, "按分镜表拍摄，共三个镜头。") == []
