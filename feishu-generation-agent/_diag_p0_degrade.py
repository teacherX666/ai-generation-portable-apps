import asyncio, sys, tempfile, traceback
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, "src")

from feishu_generation_agent.domain.errors import AgentError, ErrorCategory, ErrorDetail
from feishu_generation_agent.graph import nodes as nodes_module
from feishu_generation_agent.graph.nodes import _video_rejection_reason

err = AgentError(
    ErrorDetail(
        category=ErrorCategory.VALIDATION,
        message="上游拒绝了参考视频 video_url（task_id=task-01）",
        technical_detail="cause=video_reference_not_supported",
    )
)
print("category:", err.detail.category)
print("reason:", _video_rejection_reason(err, None, [SimpleNamespace(mime_type="video/mp4")]))
print("reason(no video):", _video_rejection_reason(err, None, [SimpleNamespace(mime_type="image/png")]))
print("MARKERS", nodes_module._VIDEO_REJECTION_MARKERS)
