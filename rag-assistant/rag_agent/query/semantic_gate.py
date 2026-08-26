"""轻量语义闸门：用现有 embedding 判断输入是否为报错内容。

思路对齐 aurelio-labs/semantic-router 的「示例话术 + 余弦相似度」，
但复用 rag-assistant 已经存在的 OpenAIEmbeddings，不额外引入依赖。

- error_report：真实报错特征示例
- unrelated：与报错无关的闲聊 / 数学 / 其他
- 对查询与两类示例分别算 top-k 余弦均值，分高者胜出；
  只有 error_report 领先幅度 >= margin 才放行扫码（用相对差距比绝对阈值更稳）。
"""
from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

ERROR_REPORT_UTTERANCES = [
    "Traceback (most recent call last):",
    "NameError: name 'x' is not defined",
    "KeyError: 'token'",
    "AttributeError: 'NoneType' object has no attribute 'foo'",
    "TypeError: unsupported operand type(s)",
    "ImportError: No module named 'xxx'",
    "FileNotFoundError: [Errno 2] No such file or directory",
    "报错：连接超时",
    "服务启动失败，日志如下",
    "接口请求返回 500 错误",
    "程序崩了，帮我看看报错",
    "部署失败，报错信息如下",
    "运行时异常，请帮忙定位",
]

UNRELATED_UTTERANCES = [
    "1+1 等于几",
    "2 的三次方",
    "今天天气怎么样",
    "帮我写一首诗",
    "讲个笑话",
    "中午吃什么",
    "你好",
    "你是谁",
    "给我推荐一部电影",
    "写一段 python 冒泡排序",
    "把这句话翻译成英文",
]


# 规则预筛：明显报错直接放行、明显无关直接拦截，两者都不用调 Embedding，
# 只有规则判断不了才走 SemanticGate。正则保持保守，宁可漏给闸门也不误伤。
_ERROR_RE = re.compile(
    r"traceback|exception|\berror\b|错误|报错|异常|失败|超时|timeout|failed|"
    r"invalid|not\s+found|denied|refused|connection|连接|"
    r"keyerror|nameerror|typeerror|importerror|attributeerror|valueerror|"
    r"filenotfounderror|\b(4\d\d|5\d\d)\b",
    re.IGNORECASE,
)
_ARITHMETIC_RE = re.compile(r"^[\d\s+\-*/()%=^.,，。]+$")
_CHINESE_ARITHMETIC_RE = re.compile(r"等于几|的三次方|的平方|的立方|算一下|多少$|几加几|几乘几")
_GREETING_RE = re.compile(r"^(你好|您好|hi|hello|hey|在吗|在不在|在么)\s*[!！?？。.]*$", re.IGNORECASE)


def prescreen_error(text: str) -> str | None:
    """零成本的规则预筛，返回 "error" / "unrelated" / None(不确定)。

    - "error"：明显是报错，可跳过 SemanticGate 直接放行；
    - "unrelated"：明显无关，可直接拒答；
    - None：规则判断不了，需要走 SemanticGate。
    """
    t = (text or "").strip()
    if not t:
        return "unrelated"
    if len(t) <= 40:
        if _GREETING_RE.match(t):
            return "unrelated"
        if _ARITHMETIC_RE.match(t) and re.search(r"\d", t):
            # 纯数字/算术表达式，但排除 404 / 500 这类状态码。
            if not _ERROR_RE.search(t):
                return "unrelated"
        if _CHINESE_ARITHMETIC_RE.search(t) and re.search(r"\d", t) and not _ERROR_RE.search(t):
            return "unrelated"
    if _ERROR_RE.search(t):
        return "error"
    return None


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass
class GateDecision:
    label: str
    error_score: float
    unrelated_score: float
    allow_retrieval: bool
    allow_scan: bool
    reason: str = ""

    @property
    def margin_score(self) -> float:
        return self.error_score - self.unrelated_score


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _class_score(query_emb: list[float], class_embs: list[list[float]], top_k: int = 3) -> float:
    sims = sorted((_cosine(query_emb, e) for e in class_embs), reverse=True)
    top = sims[:top_k]
    return sum(top) / len(top)


class SemanticGate:
    """首次 decide 时才拉取示例 embedding，避免启动时网络依赖。"""

    def __init__(self, embeddings: Embedder, margin: float, top_k: int = 3, min_error_score: float = 0.0):
        self._embeddings = embeddings
        self.margin = margin
        self.top_k = top_k
        self.min_error_score = min_error_score
        self._load_lock = threading.Lock()
        self._error_embs: list[list[float]] | None = None
        self._unrelated_embs: list[list[float]] | None = None

    def _ensure_loaded(self) -> None:
        if self._error_embs is not None and self._unrelated_embs is not None:
            return
        with self._load_lock:
            if self._error_embs is not None and self._unrelated_embs is not None:
                return
            # 合并成一次 embedding 请求，减少首答的网络往返。
            merged = self._embeddings.embed_documents(
                ERROR_REPORT_UTTERANCES + UNRELATED_UTTERANCES
            )
            split = len(ERROR_REPORT_UTTERANCES)
            self._error_embs = merged[:split]
            self._unrelated_embs = merged[split:]

    def decide(self, text: str) -> GateDecision:
        """判断输入是否值得进入 KB 检索和高成本源码分析。

        正常报错允许两条链路；无关输入直接短路；闸门自身失败时允许
        低成本 KB 检索以避免误挡真实报错，但禁止源码扫描。
        """
        try:
            self._ensure_loaded()
            query_emb = self._embeddings.embed_query(text)
            error_score = _class_score(query_emb, self._error_embs or [], self.top_k)
            unrelated_score = _class_score(query_emb, self._unrelated_embs or [], self.top_k)
            is_error = (
                error_score >= self.min_error_score
                and error_score - unrelated_score >= self.margin
            )
            if is_error:
                return GateDecision(
                    label="error_report",
                    error_score=error_score,
                    unrelated_score=unrelated_score,
                    allow_retrieval=True,
                    allow_scan=True,
                    reason="error_semantic_match",
                )
            return GateDecision(
                label="unrelated",
                error_score=error_score,
                unrelated_score=unrelated_score,
                allow_retrieval=False,
                allow_scan=False,
                reason="unrelated_semantic_match",
            )
        except Exception:
            logger.exception("semantic gate failed; allow KB retrieval but block source scan")
            return GateDecision(
                label="gate_error",
                error_score=0.0,
                unrelated_score=0.0,
                allow_retrieval=True,
                allow_scan=False,
                reason="gate_exception",
            )
