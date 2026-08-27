"""轻量语义路由：提前拦截明确无关输入，并控制是否允许扫描源码。

实现遵循 aurelio-labs/semantic-router 的核心模式：为每条 Route 配置示例话术，
用同一个 embedding 模型编码示例和查询，再按余弦相似度、top-k 聚合与阈值选路。
这里复用项目已有的 OpenAI-compatible embedding 客户端，避免为两个静态路由引入
semantic-router 完整包及其 LiteLLM、Boto3 等额外依赖。
"""
from __future__ import annotations

import logging
import math
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
    "点击提交后没有反应",
    "任务一直卡住没有结果",
    "上传后生成失败",
    "页面打开后一直白屏",
    "突然不能生成了",
    "账号余额不足，任务提交失败",
    "参数不合法，模型不支持这个请求",
    "TaskTypeConstraint",
    "ModelNotOpen",
    "CreditPreDeductNotEnough",
    "duration not valid",
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
    "生成一张短发女人的图片",
    "帮我优化一段视频提示词",
    "怎么使用这个网站",
    "有哪些模型可以选择",
    "生成图片时不要出现错误文字",
]


class EmbeddingsLike(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class GateDecision:
    label: str
    error_score: float
    unrelated_score: float
    allow_scan: bool
    reason: str

    @property
    def margin_score(self) -> float:
        return self.error_score - self.unrelated_score


def _as_valid_vector(value: object, name: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{name} contains a non-number")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError(f"{name} contains a non-finite number")
        vector.append(number)
    if math.sqrt(sum(v * v for v in vector)) == 0:
        raise ValueError(f"{name} has zero norm")
    return vector


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("embedding dimensions do not match")
    denom = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    if denom == 0:
        raise ValueError("embedding has zero norm")
    return sum(x * y for x, y in zip(a, b)) / denom


def _route_score(query: list[float], examples: list[list[float]], top_k: int) -> float:
    """按 SemanticRouter 的 top-k + mean 聚合方式计算一条路由的分数。"""
    scores = sorted((_cosine(query, example) for example in examples), reverse=True)
    selected = scores[: max(1, min(top_k, len(scores)))]
    return sum(selected) / len(selected) if selected else 0.0


class SemanticGate:
    """两个静态 Route：``error_report`` 与 ``unrelated``。

    只有报错路由超过绝对阈值，并且相对无关路由领先至少 ``margin``，才允许
    扫描源码。无匹配、两类接近、embedding 异常都禁止扫描（fail closed）。
    """

    def __init__(
        self,
        embeddings: EmbeddingsLike,
        margin: float = 0.08,
        unrelated_margin: float = 0.05,
        top_k: int = 3,
        min_error_score: float = 0.55,
        min_unrelated_score: float = 0.60,
        failure_cooldown_seconds: float = 30.0,
    ) -> None:
        self._embeddings = embeddings
        self.margin = margin
        # 拒绝无关输入可以使用稍低的独立分差；这不会放宽允许源码扫描的门槛。
        self.unrelated_margin = unrelated_margin
        self.top_k = max(1, top_k)
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
            # 两条 Route 的 utterances 合并成一次请求，减少首次路由的网络往返。
            all_examples = ERROR_REPORT_UTTERANCES + UNRELATED_UTTERANCES
            merged = self._validate_embedding_batch(
                self._embeddings.embed_documents(all_examples),
                expected_count=len(all_examples),
            )
            split = len(ERROR_REPORT_UTTERANCES)
            self._error_embs = merged[:split]
            self._unrelated_embs = merged[split:]

    @staticmethod
    def _failure_decision(reason: str) -> GateDecision:
        return GateDecision(
            label="gate_error",
            error_score=0.0,
            unrelated_score=0.0,
            allow_scan=False,
            reason=reason,
        )

    def decide(self, text: str) -> GateDecision:
        if time.monotonic() < self._retry_after_monotonic:
            return self._failure_decision("gate_recent_failure")
        try:
            self._ensure_loaded()
            query_emb = _as_valid_vector(self._embeddings.embed_query(text), "query embedding")
            error_embs = self._error_embs or []
            unrelated_embs = self._unrelated_embs or []
            if error_embs and len(query_emb) != len(error_embs[0]):
                raise ValueError("query embedding dimension does not match example embeddings")

            error_score = _route_score(query_emb, error_embs, self.top_k)
            unrelated_score = _route_score(query_emb, unrelated_embs, self.top_k)
            margin_score = error_score - unrelated_score

            if error_score >= self.min_error_score and margin_score >= self.margin:
                return GateDecision(
                    label="error_report",
                    error_score=error_score,
                    unrelated_score=unrelated_score,
                    allow_scan=True,
                    reason="error_route_matched",
                )
            if (
                unrelated_score >= self.min_unrelated_score
                and margin_score <= -self.unrelated_margin
            ):
                return GateDecision(
                    label="unrelated",
                    error_score=error_score,
                    unrelated_score=unrelated_score,
                    allow_scan=False,
                    reason="unrelated_route_matched",
                )
            return GateDecision(
                label="uncertain",
                error_score=error_score,
                unrelated_score=unrelated_score,
                allow_scan=False,
                reason="no_route_above_confidence_margin",
            )
        except Exception:
            self._retry_after_monotonic = time.monotonic() + self.failure_cooldown_seconds
            logger.exception("semantic route failed; continue KB but block source scan")
            return self._failure_decision("gate_exception")
