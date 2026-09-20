import pytest
from pydantic import ValidationError

from feishu_generation_agent.web.schemas import ArtifactReviewRequest


def test_adjust_accepts_selected_task_feedback() -> None:
    request = ArtifactReviewRequest.model_validate(
        {
            "action": "adjust",
            "feedback": "动作再慢一点",
            "task_ids": ["task-video"],
        }
    )

    assert request.to_domain().task_ids == ["task-video"]


def test_confirm_rejects_retry_task_ids() -> None:
    with pytest.raises(ValidationError, match="不能携带调整意见或任务"):
        ArtifactReviewRequest.model_validate(
            {
                "action": "confirm",
                "task_ids": ["task-video"],
            }
        )
