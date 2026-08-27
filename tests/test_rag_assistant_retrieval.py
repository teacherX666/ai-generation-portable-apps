# -*- coding: utf-8 -*-
"""KB 分块和混合检索的无网络回归测试。"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RAG_DIR = ROOT / "rag-assistant"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


indexer = _load_module("rag_assistant_indexer_test", RAG_DIR / "rag_agent/sync/indexer.py")
retriever_module = _load_module("rag_assistant_retriever_test", RAG_DIR / "rag_agent/query/retriever.py")


class ChunkingTests(unittest.TestCase):
    def test_split_keeps_each_kb_section_intact_and_extracts_keywords(self):
        markdown = """# KB

前言

## 【参数错误】:时长参数不合法 duration not valid

症状：时长错误

关键词：duration not valid、时长不合法

## 【网络】:网络连接失败

症状：网络断开

关键词：网络连接、connection failed
"""
        docs = indexer.split_markdown(markdown)
        self.assertEqual(len(docs), 2)
        self.assertIn("症状：时长错误", docs[0].page_content)
        self.assertIn("关键词：duration not valid", docs[0].page_content)
        self.assertEqual(docs[0].metadata["chunk_strategy"], "markdown_section")
        self.assertIn("duration not valid", docs[0].metadata["kb_keywords"])
        self.assertEqual(docs[1].metadata["section_index"], 1)

    def test_split_requires_markdown_sections(self):
        with self.assertRaises(ValueError):
            indexer.split_markdown("只有普通段落，没有章节标题")


class FakeVectorStore:
    def __init__(self, docs):
        self.docs = docs
        self._collection = self

    def similarity_search_with_relevance_scores(self, query, k):
        # 模拟向量只把泛化的“内容审核”放在首位，验证关键词能补回精确条目。
        score = 0.20 if query == "天气怎么样" else 0.48
        return [(self.docs[1], score), (self.docs[0], 0.24)]

    def get(self, include):
        return {
            "documents": [d.page_content for d in self.docs],
            "metadatas": [d.metadata for d in self.docs],
        }


class HybridRetrievalTests(unittest.TestCase):
    def test_exact_keyword_can_recall_below_vector_threshold(self):
        docs = [
            indexer.Document(
                page_content="## duration\n关键词：duration not valid",
                metadata={"error_title": "【参数错误】:时长参数不合法 duration not valid", "kb_keywords": "duration not valid、时长不合法"},
            ),
            indexer.Document(
                page_content="## content\n关键词：copyright violation",
                metadata={"error_title": "【内容审核】:供应商返回版权/真人/NSFW违规", "kb_keywords": "copyright violation、NSFW"},
            ),
        ]
        fake = FakeVectorStore(docs)
        with patch.object(retriever_module, "Chroma", return_value=fake):
            r = retriever_module.KbRetriever(
                chroma_dir=Path("/tmp/chroma"),
                status_path=Path("/tmp/status.json"),
                embeddings=object(),
                top_k=2,
                candidate_k=2,
                min_similarity=0.50,
                min_hybrid_score=0.30,
            )
            with patch.object(r, "_active_collection", return_value="kb_v_test"):
                hits = r.retrieve("接口返回 duration not valid，任务创建失败")
        self.assertEqual(len(hits), 1)
        self.assertIn("duration not valid", hits[0].metadata["error_title"])
        self.assertTrue(hits[0].metadata["retrieval_exact_keyword"])
        self.assertGreater(hits[0].metadata["retrieval_keyword_score"], 0)

    def test_unrelated_query_returns_no_hybrid_hits(self):
        docs = [
            indexer.Document(
                page_content="## duration\n关键词：duration not valid",
                metadata={"error_title": "【参数错误】:时长参数不合法 duration not valid", "kb_keywords": "duration not valid、时长不合法"},
            ),
            indexer.Document(
                page_content="## content\n关键词：copyright violation",
                metadata={"error_title": "【内容审核】:供应商返回版权/真人/NSFW违规", "kb_keywords": "copyright violation、NSFW"},
            ),
        ]
        fake = FakeVectorStore(docs)
        with patch.object(retriever_module, "Chroma", return_value=fake):
            r = retriever_module.KbRetriever(
                chroma_dir=Path("/tmp/chroma"),
                status_path=Path("/tmp/status.json"),
                embeddings=object(),
                top_k=2,
                candidate_k=2,
                min_similarity=0.50,
                min_hybrid_score=0.30,
            )
            with patch.object(r, "_active_collection", return_value="kb_v_test"):
                hits = r.retrieve("天气怎么样")
        self.assertEqual(hits, [])

    def test_common_english_words_do_not_create_false_keyword_hits(self):
        doc = indexer.Document(
            page_content="## content\n关键词：InputImageSensitiveContentDetected",
            metadata={
                "error_title": "【内容审核】:输入图片审核未通过 InputImageSensitiveContentDetected",
                "kb_keywords": "InputImageSensitiveContentDetected、输入审核、敏感内容",
            },
        )
        self.assertEqual(retriever_module._keyword_score("IMAGE_SAFETY", doc), (0.0, False))
        self.assertEqual(retriever_module._keyword_score("接口返回 duration not valid", doc), (0.0, False))


if __name__ == "__main__":
    unittest.main()
