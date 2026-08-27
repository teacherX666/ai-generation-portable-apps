"""KB 混合检索：语义相似度 + KB 关键词匹配。

单独依赖向量相似度时，用户的一段长描述很容易被“看起来沾边”的条目
误召回。这里先用向量召回候选，再对活动 KB 的全部条目做一次很便宜的
关键词重排：错误码/错误短语优先，最后只保留达到阈值的结果。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chromadb.errors import NotFoundError
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)

# 这些词在几乎所有错误条目里都会出现，不能单独算作关键词命中。
_ZH_STOPWORDS = {
    "一个", "一些", "这个", "那个", "用户", "请求", "接口", "服务", "任务",
    "错误", "报错", "问题", "失败", "提交", "生成", "视频", "图片", "内容",
    "参考", "素材", "出现", "返回", "导致", "需要", "可以", "如果", "然后",
    "以及", "或者", "进行", "检查", "平台", "系统", "情况", "相关", "信息",
}
# 英文自然语言词不能当作错误码，否则 IMAGE_SAFETY 会因为 image 出现在
# “not an image”里而误命中；not/valid 等也不能用子串匹配。
_ASCII_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "this", "to", "with", "not",
    "error", "failed", "failure", "image", "images", "video", "videos", "task",
    "request", "response", "result", "found", "valid", "invalid",
}
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.:/-]{1,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}")
_TERM_RE_TEMPLATE = r"(?<![a-z0-9]){term}(?![a-z0-9])"
_PHRASE_PATTERNS = (
    re.compile(r"\bis\s+not\s+an\s+image\b", re.IGNORECASE),
    re.compile(r"\bis\s+not\s+found\b", re.IGNORECASE),
    re.compile(r"\bduration\s+not\s+valid\b", re.IGNORECASE),
    re.compile(r"\bcopyright\s+violation\b", re.IGNORECASE),
    re.compile(r"\bcontent\s+policy\b", re.IGNORECASE),
    re.compile(r"\bconnection\s+failed\b", re.IGNORECASE),
)
_KEYWORDS_RE = re.compile(r"^关键词[：:][ \t]*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class RetrievalHit:
    document: Document
    vector_score: float
    keyword_score: float
    hybrid_score: float
    exact_keyword: bool


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _query_terms(text: str) -> set[str]:
    """提取英文错误码/短语、数字和中文片段。"""
    normalized = (text or "").casefold()
    terms: set[str] = {
        match.group(0).casefold()
        for pattern in _PHRASE_PATTERNS
        for match in pattern.finditer(normalized)
    }
    for token in _TOKEN_RE.findall(normalized):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            # 中文没有空格分词：保留完整连续片段，同时补 2~6 字短语，
            # 这样“图片下载失败”可以匹配“下载失败”，但不会把整句当关键词。
            terms.add(token)
            for size in range(2, min(6, len(token)) + 1):
                terms.update(token[i : i + size] for i in range(len(token) - size + 1))
        else:
            if token not in _ASCII_STOPWORDS:
                terms.add(token)
    return {
        t for t in terms
        if t not in _ZH_STOPWORDS and len(t) >= 2
    }


def _keyword_text(document: Document) -> str:
    """取标题+关键词；兼容重建索引前的旧 metadata。"""
    metadata = document.metadata or {}
    explicit = str(metadata.get("kb_keywords", ""))
    if not explicit:
        match = _KEYWORDS_RE.search(document.page_content or "")
        explicit = match.group(1).strip() if match else ""
    return f"{metadata.get('error_title', '')}\n{explicit}".casefold()


def _doc_text(document: Document) -> str:
    return f"{_keyword_text(document)}\n{document.page_content}".casefold()


def _contains_term(text: str, term: str) -> bool:
    """英文按完整 token 匹配，中文/数字仍按包含匹配。"""
    if re.search(r"[a-z]", term):
        return re.search(_TERM_RE_TEMPLATE.format(term=re.escape(term)), text) is not None
    return term in text


def _keyword_score(
    query: str,
    document: Document,
    common_terms: set[str] | frozenset[str] = frozenset(),
) -> tuple[float, bool]:
    """返回 (关键词分, 是否有明确关键词命中)。

    英文错误码命中标题/关键词时是强命中；中文必须命中至少一个三字以上
    的 KB 短语，或命中两个不同的中文短语，避免“视频/任务”式误召回。
    """
    q_terms = _query_terms(query)
    if not q_terms:
        return 0.0, False

    keyword_text = _keyword_text(document)
    full_text = _doc_text(document)
    matched_keyword = {term for term in q_terms if _contains_term(keyword_text, term)}
    matched_full = {term for term in q_terms if _contains_term(full_text, term)}
    # InvalidParameter 这类在多个 KB 条目重复出现的通用码只能辅助排序，
    # 不能单独制造“强命中”；真正的错误短语（如 is not an image）才是区分依据。
    specific_keyword = matched_keyword - set(common_terms)
    specific_full = matched_full - set(common_terms)
    if not specific_full:
        return 0.0, False

    # 只按“最长的若干匹配”计分，避免中文 n-gram 互相重复把分数虚高。
    ranked = sorted(specific_full, key=lambda term: (len(term), term), reverse=True)
    selected: list[str] = []
    for term in ranked:
        if any(term in chosen or chosen in term for chosen in selected):
            continue
        selected.append(term)
        if len(selected) >= 3:
            break

    title_ratio = len(specific_keyword) / max(1, min(3, len(q_terms)))
    full_ratio = len(selected) / max(1, min(3, len(q_terms)))
    score = _clamp(0.72 * min(1.0, title_ratio) + 0.28 * min(1.0, full_ratio))

    exact_ascii = any(
        re.search(r"[a-z]", term) and _contains_term(keyword_text, term)
        for term in specific_keyword
    )
    exact_zh = any(
        len(term) >= 3 and term not in _ZH_STOPWORDS and _contains_term(keyword_text, term)
        for term in selected
    ) or sum(
        1 for term in selected
        if re.fullmatch(r"[\u4e00-\u9fff]+", term) and term not in _ZH_STOPWORDS
    ) >= 2
    return score, bool(exact_ascii or exact_zh)


def _with_scores(hit: RetrievalHit) -> Document:
    metadata = dict(hit.document.metadata or {})
    metadata.update(
        {
            "retrieval_vector_score": round(hit.vector_score, 4),
            "retrieval_keyword_score": round(hit.keyword_score, 4),
            "retrieval_hybrid_score": round(hit.hybrid_score, 4),
            "retrieval_exact_keyword": hit.exact_keyword,
        }
    )
    return Document(page_content=hit.document.page_content, metadata=metadata)


class KbRetriever:
    def __init__(
        self,
        chroma_dir: Path,
        status_path: Path,
        embeddings: Embeddings,
        top_k: int = 5,
        candidate_k: int = 20,
        min_similarity: float = 0.52,
        min_hybrid_score: float = 0.38,
        vector_weight: float = 0.55,
        keyword_weight: float = 0.45,
    ) -> None:
        self._chroma_dir = chroma_dir
        self._status_path = status_path
        self._embeddings = embeddings
        self._top_k = max(1, top_k)
        self._candidate_k = max(self._top_k, candidate_k)
        self._min_similarity = _clamp(min_similarity)
        self._min_hybrid_score = _clamp(min_hybrid_score)
        total = max(0.001, vector_weight + keyword_weight)
        self._vector_weight = vector_weight / total
        self._keyword_weight = keyword_weight / total

    def _active_collection(self) -> str:
        if not self._status_path.exists():
            raise RuntimeError(
                f"sync_status.json 不存在:{self._status_path};"
                "请先运行 `uv run python -m rag_agent.sync` 建库"
            )
        data = json.loads(self._status_path.read_text(encoding="utf-8"))
        active = data.get("active_collection")
        if not active:
            raise RuntimeError("sync_status.json 中缺 active_collection 字段")
        return active

    @staticmethod
    def _vector_score(raw_score: Any) -> float:
        """把 Chroma relevance score 规整到 0~1。"""
        return _clamp(raw_score)

    def _load_all_documents(self, vs: Chroma) -> list[Document]:
        """读取活动 collection 的文本和 metadata，供关键词补召回。"""
        try:
            data = vs._collection.get(include=["documents", "metadatas"])
        except Exception:
            logger.exception("failed to load all KB documents for keyword recall")
            return []
        docs: list[Document] = []
        metadatas = data.get("metadatas") or []
        documents = data.get("documents") or []
        for content, metadata in zip(documents, metadatas):
            if content:
                docs.append(Document(page_content=content, metadata=metadata or {}))
        return docs

    @staticmethod
    def _common_query_terms(query: str, documents: list[Document]) -> set[str]:
        """找出在多个 KB 条目里重复出现的查询词，降低通用错误码权重。"""
        terms = _query_terms(query)
        counts = {
            term: sum(1 for doc in documents if _contains_term(_keyword_text(doc), term))
            for term in terms
        }
        return {term for term, count in counts.items() if count >= 2}

    def _do_retrieve_hits(self, query: str) -> list[RetrievalHit]:
        collection = self._active_collection()
        vs = Chroma(
            collection_name=collection,
            persist_directory=str(self._chroma_dir),
            embedding_function=self._embeddings,
        )

        vector_results = vs.similarity_search_with_relevance_scores(
            query, k=self._candidate_k
        )
        vector_by_content: dict[str, float] = {
            doc.page_content: self._vector_score(score)
            for doc, score in vector_results
        }
        all_docs = self._load_all_documents(vs)
        if not all_docs:
            all_docs = [doc for doc, _ in vector_results]

        # 关键词命中可以把没有进入向量 top-k 的精确错误码补回来。
        merged: dict[str, Document] = {doc.page_content: doc for doc in all_docs}
        merged.update({doc.page_content: doc for doc, _ in vector_results})
        common_terms = self._common_query_terms(query, list(merged.values()))
        hits: list[RetrievalHit] = []
        for content, doc in merged.items():
            vector_score = vector_by_content.get(content, 0.0)
            keyword_score, exact_keyword = _keyword_score(query, doc, common_terms)
            # 混合检索必须有“共同证据”：关键词命中，或足够高的语义分。
            # 纯向量结果仍可保留，但使用更高门槛，避免把沾边条目直接交给模型。
            semantic_only = keyword_score <= 0.0
            if semantic_only and vector_score < self._min_similarity:
                continue
            hybrid_score = self._vector_weight * vector_score + self._keyword_weight * keyword_score
            if not exact_keyword and hybrid_score < self._min_hybrid_score:
                continue
            hits.append(
                RetrievalHit(
                    document=doc,
                    vector_score=vector_score,
                    keyword_score=keyword_score,
                    hybrid_score=hybrid_score,
                    exact_keyword=exact_keyword,
                )
            )

        hits.sort(key=lambda h: (h.hybrid_score, h.exact_keyword, h.keyword_score), reverse=True)
        return hits[: self._top_k]

    def retrieve_with_scores(self, query: str) -> list[Document]:
        """返回混合检索结果，并把三种分数写入 metadata 便于日志/调参。"""
        return [_with_scores(hit) for hit in self._do_retrieve_hits(query)]

    def retrieve(self, query: str) -> list[Document]:
        """混合检索 top-K；低于阈值的“沾边”条目会被过滤。"""
        try:
            return self.retrieve_with_scores(query)
        except NotFoundError:
            logger.warning(
                "collection not found during retrieval, retrying with fresh status"
            )
            return self.retrieve_with_scores(query)
