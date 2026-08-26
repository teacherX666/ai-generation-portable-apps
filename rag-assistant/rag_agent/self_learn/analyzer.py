"""扫码分析器:parse_scan_response + AnalysisResult。

scan_and_analyze 在 S-Task 8 补,format_scan_answer 也在 S-Task 8 补。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from rag_agent.config import Settings
from rag_agent.llm.deepseek import chat
from rag_agent.self_learn.pack_repo import pack_repository
from rag_agent.self_learn.prompts import SCAN_SYSTEM_PROMPT, USER_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)


ConfidenceValue = Literal["高", "中", "低"]


@dataclass
class AnalysisResult:
    user_facing_answer: str = ""       # 简短版,给用户看
    kb_candidate_section: str = ""     # 完整 KB 格式段(BEGIN/END 之间的内容),写候选池用
    confidence: ConfidenceValue = "低"
    cited_sources: list[str] = field(default_factory=list)

    # 向后兼容属性(万一有旧代码引用)
    @property
    def answer(self) -> str:
        """兼容旧代码 —— 返回 kb_candidate_section。"""
        return self.kb_candidate_section


def parse_scan_response(text: str) -> AnalysisResult:
    """拆解 Claude 的两段式回复。

    - 用户段:'## 面向用户' 到 '---BEGIN KB CANDIDATE---' 之间
    - KB 候选段:'---BEGIN KB CANDIDATE---' 到 '---END KB CANDIDATE---' 之间
    - confidence:【推理置信度】tag(缺则默认 "低")
    - cited_sources:从 KB 候选段的 **参考代码** 段提取
    """
    conf_match = re.search(r"【推理置信度】\s*(高|中|低)", text)
    confidence: ConfidenceValue = conf_match.group(1) if conf_match else "低"

    # 拆两段
    user_match = re.search(
        r"##\s*面向用户\s*\n(.*?)(?=\n---BEGIN KB CANDIDATE---|\Z)",
        text,
        re.DOTALL,
    )
    user_facing = user_match.group(1).strip() if user_match else ""

    kb_match = re.search(
        r"---BEGIN KB CANDIDATE---\s*\n(.*?)---END KB CANDIDATE---",
        text,
        re.DOTALL,
    )
    kb_candidate = kb_match.group(1).strip() if kb_match else ""

    # 参考代码只从 kb_candidate 段抽
    cited_sources: list[str] = []
    ref_section = re.search(
        r"\*\*参考代码\*\*\s*\n(.*?)(?=\n\*\*关键词\*\*|\Z)",
        kb_candidate,
        re.DOTALL,
    )
    if ref_section:
        cited_sources = re.findall(r"[\w/.-]+\.[a-zA-Z]+:\d+", ref_section.group(1))

    return AnalysisResult(
        user_facing_answer=user_facing,
        kb_candidate_section=kb_candidate,
        confidence=confidence,
        cited_sources=cited_sources,
    )


def scan_and_analyze(
    query_text: str,
    top_kb_titles: list[str],
    settings: Settings,
) -> AnalysisResult:
    """打包源码 + DeepSeek 现场分析 + 解析结果。

    - 任何异常(网络 / API / 解析)都 catch → 返回 AnalysisResult(answer="", confidence="低", ...)
    - 上层看到 confidence="低" 走"请联系管理员"分支
    """
    try:
        repo_xml = pack_repository(
            root=settings.code_scan_root,
            max_tokens=settings.code_scan_max_tokens,
        )
        user_text = USER_PROMPT_TEMPLATE.format(
            repo_xml=repo_xml,
            query_text=query_text or "(无文本)",
            top_kb_titles="\n".join(f"- {t}" for t in top_kb_titles) or "(无)",
        )

        raw = chat(
            settings,
            [
                {"role": "system", "content": SCAN_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            max_tokens=2048,
        )
        logger.info("scan raw response length: %d chars", len(raw))
        logger.debug("scan raw response: %s", raw)
        result = parse_scan_response(raw)
        logger.info("scan parsed: confidence=%s user_facing_len=%d kb_candidate_len=%d sources=%d",
                    result.confidence, len(result.user_facing_answer),
                    len(result.kb_candidate_section), len(result.cited_sources))
        return result
    except Exception:
        logger.exception("scan_and_analyze failed")
        return AnalysisResult(
            user_facing_answer="",
            kb_candidate_section="",
            confidence="低",
            cited_sources=[],
        )


def format_scan_answer(analysis: AnalysisResult, show_kb_candidate: bool = False) -> str:
    """把 AnalysisResult 转成面向用户的最终答复。

    show_kb_candidate=True 时(调试)在简短版下方追加完整 KB 候选段,供开发时观察。
    生产环境该参数保持 False。
    """
    if not analysis.user_facing_answer:
        return "我暂时无法从知识库和源码里定位这个问题，请联系管理员。"
    if analysis.confidence == "低":
        # 低置信度：优先展示模型的说明（如「这不是报错问题」），不加免责后缀
        return analysis.user_facing_answer

    body = analysis.user_facing_answer

    if show_kb_candidate and analysis.kb_candidate_section:
        body += "\n\n---\n【调试:KB 候选段】\n" + analysis.kb_candidate_section

    if analysis.confidence == "高":
        suffix = "\n\n⚠️ 以上是 AI 现场读代码分析的结果,未经维护者审核;若有偏差请联系管理员。"
    else:
        suffix = "\n\n⚠️ AI 现场推测,可信度中等,建议向维护者确认后再采纳方案。"
    return body + suffix
