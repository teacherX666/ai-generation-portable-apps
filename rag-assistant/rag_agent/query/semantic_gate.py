"""轻量语义闸门：判断输入是否像一个需要排查的报错。

思路对齐 aurelio-labs/semantic-router 的「示例话术 + 余弦相似度」，
但复用 rag-assistant 已有的 embedding，不增加新依赖：

1. 明确的报错码、日志格式和失败短语先快速放行；
2. 明确的问候、数学题先快速拦截；
3. 其余输入才由 embedding 进行二分类。

规则只识别*错误上下文*，不会因为“生成视频”“短发女人”“参考图”等
业务词而拦截。这样截图摘要中混有生成提示词和报错时，真正报错不会漏掉。
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
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


# 规则预筛必须比普通“包含某个词”更严格。比如“不要生成错误文字”或
# “连接飞书机器人”不是报错，不能因为含有“错误/连接”就触发 KB 和源码扫描。
# 真正模糊的句子留给 SemanticGate，不在这里武断拒绝。
_ERROR_CODE_RE = re.compile(
    r"\b(?:"
    r"(?:[A-Z][A-Za-z0-9_.-]+(?:Error|Exception))|"
    r"InvalidParameter|BadRequest|TaskTypeConstraint|ModelNotOpen|"
    r"CreditPreDeductNotEnough|InvalidEndpointOrModel(?:\.NotFound)?|"
    r"InputImageSensitiveContentDetected|OutputVideoSensitiveContentDetected|"
    r"CERTIFICATE_VERIFY_FAILED"
    r")\b",
    re.IGNORECASE,
)
_ERROR_LOG_RE = re.compile(
    r"traceback|\b(?:key|name|type|import|attribute|value|file)error\b|"
    r"\[errno\s*\d+\]|\brequest\s+id\s*[:：]|"
    r"\b(?:bad\s+request|internal\s+server\s+error|service\s+unavailable|"
    r"error\s+(?:while|when|occurred|downloading|uploading|processing|submitting)|"
    r"failed\s+to\s+|could\s+not\s+be\s+completed)\b",
    re.IGNORECASE,
)
_ERROR_PHRASE_RE = re.compile(
    r"(?:"
    # 英文错误短语 / 状态码。单独的 invalid、connection、error 不足以说明是报错。
    r"\b(?:duration\s+not\s+valid|must\s+be\s*<=?\s*\d+|"
    r"quota\s+exceeded|rate\s+limit|insufficient\s+balance|"
    r"invalid\s+(?:token|parameter|request|endpoint|model)|"
    r"connection\s+(?:refused|failed|timed\s*out|timeout|error)|"
    r"(?:is\s+)?not\s+(?:found|an\s+image|valid)|"
    r"copyright\s+violation|content\s+policy|real\s+person\s+detected)\b|"
    r"(?:https?\s*)?(?:状态码|status\s*code|返回|code)\s*[:：=]?\s*(?:4\d\d|5\d\d)|"
    # 明确的中文失败状态和业务错误。保留上下文，避免“错误文字”类提示词误放行。
    r"(?:账户|账号)欠费|余额不足|配额超限|请求过于频繁|参数不合法|参数错误|模型未开通|模型不支持|"
    r"(?:任务|请求|接口|服务|程序|提交|上传|运行|启动|调用|部署)"
    r".{0,18}?(?:失败|超时|拒绝|不可用|无法(?:完成|连接|提交|上传|启动)?|"
    r"不支持|不合法|不正确)|"
    r"(?:失败|超时|拒绝|无法(?:完成|连接|提交|上传|启动)?|不支持|不合法|不正确)"
    r".{0,18}?(?:任务|请求|接口|服务|程序|提交|上传|运行|启动|调用|部署)|"
    r"(?:生成|任务)\s*(?:任务)?\s*(?:失败|超时|被拒绝)|"
    r"(?:返回|报|状态码|code)\s*[:：=]?\s*(?:4\d\d|5\d\d)"
    r")",
    re.IGNORECASE,
)
_STATUS_ONLY_RE = re.compile(r"^(?:https?\s*)?[45]\d\d$", re.IGNORECASE)
_SHORT_ERROR_RE = re.compile(
    r"^(?:报错|错误|异常|失败|出错|超时|崩了)(?:了|怎么办|怎么解决|帮我看)?[!！。.!！?？\s]*$",
    re.IGNORECASE,
)
_ARITHMETIC_RE = re.compile(r"^[\d\s+\-*/()%=^.,，。]+$")
_CHINESE_ARITHMETIC_RE = re.compile(r"等于几|的三次方|的平方|的立方|算一下|多少$|几加几|几乘几")
_GREETING_RE = re.compile(r"^(你好|您好|hi|hello|hey|哈喽|哈囉|在吗|在不在|在么)\s*[!！?？。.]*$", re.IGNORECASE)


def prescreen_error(text: str) -> str | None:
    """零成本的规则预筛，返回 ``error`` / ``unrelated`` / ``None``。

    ``error`` 只用于高置信度的真实错误信号；普通创作需求中出现“错误”
    等词时返回 ``None``，继续由语义闸门判断，而不是误触发昂贵链路。
    """
    t = (text or "").strip()
    if not t:
        return "unrelated"
    if len(t) <= 40:
        if _GREETING_RE.match(t):
            return "unrelated"
        if _SHORT_ERROR_RE.match(t):
            return "error"
        if _STATUS_ONLY_RE.match(t):
            return "error"
        if _ARITHMETIC_RE.match(t) and re.search(r"\d", t):
            # 纯数字/算术表达式；HTTP 状态码已在上一条单独处理。
            return "unrelated"
        if _CHINESE_ARITHMETIC_RE.search(t) and re.search(r"\d", t):
            return "unrelated"
    if _ERROR_CODE_RE.search(t) or _ERROR_LOG_RE.search(t) or _ERROR_PHRASE_RE.search(t):
        return "error"
    # 其他输入交给原有 SemanticGate 判断。不要根据“生成视频、参考图、
    # 短发、台词”等业务词提前拦截：截图摘要可能同时包含真正的报错。
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


def _as_valid_vector(vector: object, name: str) -> list[float]:
    """把 embedding 校验为同维、有限的 float 列表，拒绝空值和 NaN。"""
    if not isinstance(vector, (list, tuple)) or not vector:
        raise ValueError(f"{name} is empty or not a vector")
    try:
        result = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} contains a non-numeric value") from exc
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} contains NaN or infinity")
    return result


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"embedding dimension mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        raise ValueError("zero-norm embedding")
    return dot / (na * nb)


def _class_score(query_emb: list[float], class_embs: list[list[float]], top_k: int = 3) -> float:
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if not class_embs:
        raise ValueError("class embeddings are empty")
    sims = sorted((_cosine(query_emb, e) for e in class_embs), reverse=True)
    top = sims[:top_k]
    if not top:
        raise ValueError("no class similarities available")
    return sum(top) / len(top)


class SemanticGate:
    """首次 decide 时才拉取示例 embedding，避免启动时网络依赖。"""

    def __init__(
        self,
        embeddings: Embedder,
        margin: float,
        top_k: int = 3,
        min_error_score: float = 0.55,
        min_unrelated_score: float = 0.60,
        failure_cooldown_seconds: float = 30.0,
    ):
        if top_k < 1:
            raise ValueError("semantic gate top_k must be at least 1")
        if not all(math.isfinite(value) for value in (margin, min_error_score, min_unrelated_score)):
            raise ValueError("semantic gate thresholds must be finite numbers")
        self._embeddings = embeddings
        self.margin = margin
        self.top_k = top_k
        self.min_error_score = min_error_score
        self.min_unrelated_score = min_unrelated_score
        self.failure_cooldown_seconds = max(0.0, failure_cooldown_seconds)
        self._load_lock = threading.Lock()
        self._error_embs: list[list[float]] | None = None
        self._unrelated_embs: list[list[float]] | None = None
        self._retry_after_monotonic = 0.0

    @staticmethod
    def _validate_embedding_batch(vectors: object, expected_count: int) -> list[list[float]]:
        if not isinstance(vectors, list) or len(vectors) != expected_count:
            actual = len(vectors) if isinstance(vectors, list) else "non-list"
            raise ValueError(f"embedding count mismatch: expected {expected_count}, got {actual}")
        checked = [_as_valid_vector(vector, f"embedding[{i}]") for i, vector in enumerate(vectors)]
        dimensions = {len(vector) for vector in checked}
        if len(dimensions) != 1:
            raise ValueError("embedding batch has inconsistent dimensions")
        return checked

    def _ensure_loaded(self) -> None:
        if self._error_embs is not None and self._unrelated_embs is not None:
            return
        with self._load_lock:
            if self._error_embs is not None and self._unrelated_embs is not None:
                return
            # 合并成一次 embedding 请求，减少首答的网络往返。
            all_examples = ERROR_REPORT_UTTERANCES + UNRELATED_UTTERANCES
            merged = self._validate_embedding_batch(
                self._embeddings.embed_documents(all_examples),
                expected_count=len(all_examples),
            )
            split = len(ERROR_REPORT_UTTERANCES)
            self._error_embs = merged[:split]
            self._unrelated_embs = merged[split:]

    def _failure_decision(self, reason: str) -> GateDecision:
        return GateDecision(
            label="gate_error",
            error_score=0.0,
            unrelated_score=0.0,
            allow_retrieval=True,
            allow_scan=False,
            reason=reason,
        )

    def decide(self, text: str) -> GateDecision:
        """判断输入是否值得进入 KB 检索和高成本源码分析。

        明确报错允许两条链路；明确无关输入直接短路；边界输入允许
        低成本 KB 检索但禁止源码扫描，交给 KB 后的二次判定。闸门自身失败时允许
        低成本 KB 检索以避免误挡真实报错，但禁止源码扫描。失败后的短暂
        冷却期不重复调用 embedding 服务，避免服务故障时雪上加霜。
        """
        if time.monotonic() < self._retry_after_monotonic:
            return self._failure_decision("gate_recent_failure")
        try:
            self._ensure_loaded()
            query_emb = _as_valid_vector(self._embeddings.embed_query(text), "query embedding")
            error_embs = self._error_embs or []
            unrelated_embs = self._unrelated_embs or []
            if error_embs and len(query_emb) != len(error_embs[0]):
                raise ValueError("query embedding dimension does not match example embeddings")
            error_score = _class_score(query_emb, error_embs, self.top_k)
            unrelated_score = _class_score(query_emb, unrelated_embs, self.top_k)
            margin_score = error_score - unrelated_score
            if (
                error_score >= self.min_error_score
                and margin_score >= self.margin
            ):
                return GateDecision(
                    label="error_report",
                    error_score=error_score,
                    unrelated_score=unrelated_score,
                    allow_retrieval=True,
                    allow_scan=True,
                    reason="error_semantic_match",
                )
            if (
                unrelated_score >= self.min_unrelated_score
                and margin_score <= -self.margin
            ):
                return GateDecision(
                    label="unrelated",
                    error_score=error_score,
                    unrelated_score=unrelated_score,
                    allow_retrieval=False,
                    allow_scan=False,
                    reason="unrelated_semantic_match",
                )
            # 两类都不够高，或分数接近：不能武断地说“无关”。允许低成本
            # KB 检索，但把源码扫描留给 KB 后的二次判定。
            return GateDecision(
                label="uncertain",
                error_score=error_score,
                unrelated_score=unrelated_score,
                allow_retrieval=True,
                allow_scan=False,
                reason="semantic_match_uncertain",
            )
        except Exception:
            self._retry_after_monotonic = time.monotonic() + self.failure_cooldown_seconds
            logger.exception("semantic gate failed; allow KB retrieval but block source scan")
            return self._failure_decision("gate_exception")
