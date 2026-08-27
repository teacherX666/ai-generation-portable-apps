"""KB 检索后的源码扫描闸门。

源码扫描会打包整个项目并再次调用模型，成本和耗时都明显高于 KB 检索。
因此只有在 KB 没有足够答案时，才用一个短小的分类请求确认“这确实是故障排查问题”。
分类器允许返回 uncertain；不确定时宁可不扫源码，也不把普通输入误当成报错。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Literal

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

ScanGateLabel = Literal["error_report", "unrelated", "uncertain", "gate_error"]

_SCAN_GATE_SYSTEM = """你是一个非常保守的源码扫描准入判断器，不负责解决问题。
你的唯一任务：判断用户输入是否明确是在排查 AI Generation Portable Apps 的真实故障。

分类规则：
- error_report：用户描述了实际失败、异常、卡住、白屏、无结果、无法提交/上传/启动/生成等故障，哪怕没有出现“错误/error”字样；截图摘要中若同时有创作提示词和故障症状，以故障为主。
- unrelated：明确是闲聊、数学题、翻译、写作、天气、普通创作提示词、产品玩法咨询等，不是在反馈已经发生的故障。
- uncertain：信息太少、只有模糊的“帮看看/有问题吗”，或无法可靠区分。不要为了凑分类而猜测。

只允许输出一个 JSON 对象，不要输出 Markdown、解释或代码块：
{"label":"error_report|unrelated|uncertain","reason":"不超过30字的中文原因"}
"""


@dataclass(frozen=True)
class ScanGateDecision:
    label: ScanGateLabel
    reason: str = ""
    raw: str = ""

    @property
    def allow_scan(self) -> bool:
        return self.label == "error_report"


def _context_for_judge(query_text: str, chunks: list[Document], coverage: str) -> str:
    titles = []
    for chunk in chunks[:5]:
        metadata = chunk.metadata or {}
        title = str(metadata.get("error_title") or "").strip()
        if title:
            titles.append(title)
    return (
        f"用户输入（可能包含视觉摘要）：\n{query_text or '(无文本)'}\n\n"
        f"KB 覆盖度：{coverage}\n"
        f"KB 候选标题：\n" + ("\n".join(f"- {title}" for title in titles) or "(无)")
    )


def _parse_decision(raw: str) -> ScanGateDecision:
    text = (raw or "").strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.insert(0, fenced.group(1))
    object_match = re.search(r"\{\s*\"label\"\s*:\s*\"[^\"]+\".*?\}", text, re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        label = data.get("label") if isinstance(data, dict) else None
        if label in {"error_report", "unrelated", "uncertain"}:
            return ScanGateDecision(label=label, reason=str(data.get("reason") or ""), raw=text)
    raise ValueError("scan gate returned invalid JSON label")


def decide_scan_after_kb(
    query_text: str,
    chunks: list[Document],
    coverage: str,
    chat_fn: Callable[..., str],
    settings: object,
) -> ScanGateDecision:
    """在 KB 未充分覆盖时判断是否允许���码扫描。

    失败时返回 gate_error（禁止扫描），避免分类服务故障反而打开昂贵路径。
    ``chat_fn`` 注入是为了让单元测试不依赖网络。
    """
    try:
        raw = chat_fn(
            settings,
            [
                {"role": "system", "content": _SCAN_GATE_SYSTEM},
                {
                    "role": "user",
                    "content": _context_for_judge(query_text, chunks, coverage),
                },
            ],
            max_tokens=160,
        )
        return _parse_decision(raw)
    except Exception:
        logger.exception("post-KB scan gate failed; block source scan")
        return ScanGateDecision(label="gate_error", reason="扫描准入判断失败")
