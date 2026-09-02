"""Dispatch video tasks between Seedance (cloud) and the local AI Port provider.

The execution graph passes a single ``video_generator``. This router keeps that
shape while allowing ``GenerationTask.video_provider`` to select the local path.
"""

from __future__ import annotations

from typing import Any

from feishu_generation_agent.domain.artifact import ProviderSubmission
from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import GenerationTask


class VideoGeneratorRouter:
    def __init__(self, seedance: Any, aiport: Any | None = None) -> None:
        self._seedance = seedance
        self._aiport = aiport

    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
        *,
        submission_id: str | None = None,
    ) -> ProviderSubmission:
        if task.resolved_video_provider == "aiport":
            if self._aiport is None:
                raise self._configuration_error(
                    "AI Port 视频 provider 未配置",
                    "operation=generate; provider=aiport",
                )
            return await self._aiport.submit(
                task, assets, submission_id=submission_id
            )
        return await self._seedance.submit(
            task, assets, submission_id=submission_id
        )

    async def poll(self, submission: ProviderSubmission) -> ProviderSubmission:
        if submission.provider == "aiport":
            if self._aiport is None:
                raise self._configuration_error(
                    "AI Port 视频 provider 未配置",
                    "operation=poll; provider=aiport",
                )
            return await self._aiport.poll(submission)
        return await self._seedance.poll(submission)

    @staticmethod
    def _configuration_error(message: str, technical_detail: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.CONFIGURATION,
                message=message,
                technical_detail=technical_detail,
                retryable=False,
            )
        )