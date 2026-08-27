# -*- coding: utf-8 -*-
"""报错问答助手 KB 后语义闸门的无网络回归测试。"""
from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAG_DIR = ROOT / "rag-assistant"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


semantic_gate = _load_module(
    "rag_assistant_semantic_gate_test",
    RAG_DIR / "rag_agent" / "query" / "semantic_gate.py",
)
preprocessor = _load_module(
    "rag_assistant_preprocessor_test",
    RAG_DIR / "rag_agent" / "query" / "preprocessor.py",
)


class FakeEmbeddings:
    def __init__(self):
        self.document_calls = 0
        self.query_calls = 0
        self.fail_query = False

    def embed_documents(self, texts):
        self.document_calls += 1
        error_markers = (
            "Traceback", "Error", "报错", "失败", "异常", "超时", "500",
            "崩", "卡住", "没有反应", "白屏", "不能生成", "余额不足",
            "不合法", "不支持", "TaskTypeConstraint", "ModelNotOpen",
            "CreditPreDeductNotEnough", "duration not valid",
        )
        return [
            [1.0, 0.0] if any(marker in text for marker in error_markers) else [0.0, 1.0]
            for text in texts
        ]

    def embed_query(self, text):
        self.query_calls += 1
        if self.fail_query:
            raise RuntimeError("embedding unavailable")
        error_markers = (
            "Traceback", "NameError", "Error", "报错", "失败", "异常", "超时",
            "500", "卡住", "没有反应", "白屏", "不能生成", "TaskTypeConstraint",
        )
        if any(marker in text for marker in error_markers):
            return [1.0, 0.0]
        return [0.0, 1.0]


class SemanticGateTests(unittest.TestCase):
    def test_error_route_is_the_only_route_that_allows_scan(self):
        gate = semantic_gate.SemanticGate(FakeEmbeddings(), margin=0.08, top_k=3)
        for text in (
            "NameError: name 'x' is not defined",
            "HTTP 500",
            "TaskTypeConstraint",
            "点击提交后没有反应，任务一直卡住",
        ):
            decision = gate.decide(text)
            self.assertEqual(decision.label, "error_report", text)
            self.assertTrue(decision.allow_scan, text)

    def test_unrelated_route_blocks_scan(self):
        gate = semantic_gate.SemanticGate(FakeEmbeddings(), margin=0.08, top_k=3)
        for text in ("你好", "1+1", "今天天气怎么样", "帮我写一首诗"):
            decision = gate.decide(text)
            self.assertEqual(decision.label, "unrelated", text)
            self.assertFalse(decision.allow_scan, text)

    def test_observed_one_plus_one_scores_are_classified_as_unrelated(self):
        """回归线上真实边界分数，避免 1+1 再落入 uncertain。"""
        class ObservedScoreEmbeddings:
            def embed_documents(self, texts):
                error_score = 0.5720777504543549
                unrelated_score = 0.6336104414158139
                error = [error_score, math.sqrt(1.0 - error_score**2)]
                unrelated = [unrelated_score, math.sqrt(1.0 - unrelated_score**2)]
                split = len(semantic_gate.ERROR_REPORT_UTTERANCES)
                return [error] * split + [unrelated] * (len(texts) - split)

            def embed_query(self, text):
                return [1.0, 0.0]

        gate = semantic_gate.SemanticGate(
            ObservedScoreEmbeddings(),
            margin=0.08,
            unrelated_margin=0.05,
            top_k=3,
        )
        decision = gate.decide("1+1")
        self.assertEqual(decision.label, "unrelated")
        self.assertFalse(decision.allow_scan)
        self.assertAlmostEqual(decision.error_score, 0.5720777504543549)
        self.assertAlmostEqual(decision.unrelated_score, 0.6336104414158139)

    def test_unrelated_margin_does_not_relax_error_scan_margin(self):
        class BorderlineErrorEmbeddings:
            def embed_documents(self, texts):
                error_score = 0.65
                unrelated_score = 0.58
                error = [error_score, math.sqrt(1.0 - error_score**2)]
                unrelated = [unrelated_score, math.sqrt(1.0 - unrelated_score**2)]
                split = len(semantic_gate.ERROR_REPORT_UTTERANCES)
                return [error] * split + [unrelated] * (len(texts) - split)

            def embed_query(self, text):
                return [1.0, 0.0]

        gate = semantic_gate.SemanticGate(
            BorderlineErrorEmbeddings(),
            margin=0.08,
            unrelated_margin=0.05,
            top_k=1,
        )
        decision = gate.decide("疑似报错")
        self.assertEqual(decision.label, "uncertain")
        self.assertFalse(decision.allow_scan)
        self.assertAlmostEqual(decision.margin_score, 0.07)

    def test_example_embeddings_are_cached(self):
        embeddings = FakeEmbeddings()
        gate = semantic_gate.SemanticGate(embeddings, margin=0.08, top_k=3)
        gate.decide("NameError: boom")
        gate.decide("今天天气怎么样")
        self.assertEqual(embeddings.document_calls, 1)
        self.assertEqual(embeddings.query_calls, 2)

    def test_minimum_absolute_score_prevents_low_confidence_margin_match(self):
        class LowConfidenceEmbeddings:
            def embed_documents(self, texts):
                error = [0.5, math.sqrt(0.75)]
                unrelated = [0.3, math.sqrt(0.91)]
                split = len(semantic_gate.ERROR_REPORT_UTTERANCES)
                return [error] * split + [unrelated] * (len(texts) - split)

            def embed_query(self, text):
                return [1.0, 0.0]

        gate = semantic_gate.SemanticGate(
            LowConfidenceEmbeddings(), margin=0.08, top_k=1, min_error_score=0.55
        )
        decision = gate.decide("有个不确定的问题")
        self.assertEqual(decision.label, "uncertain")
        self.assertFalse(decision.allow_scan)
        self.assertLess(decision.error_score, 0.55)
        self.assertGreater(decision.margin_score, 0.08)

    def test_close_route_scores_are_uncertain_and_block_scan(self):
        class CloseEmbeddings:
            def embed_documents(self, texts):
                split = len(semantic_gate.ERROR_REPORT_UTTERANCES)
                return [[0.70, 0.714142]] * split + [[0.68, 0.733212]] * (len(texts) - split)

            def embed_query(self, text):
                return [1.0, 0.0]

        gate = semantic_gate.SemanticGate(CloseEmbeddings(), margin=0.08, top_k=1)
        decision = gate.decide("帮我看看这个")
        self.assertEqual(decision.label, "uncertain")
        self.assertFalse(decision.allow_scan)

    def test_invalid_embedding_fails_closed(self):
        class InvalidEmbeddings:
            def embed_documents(self, texts):
                return [[1.0, 0.0] for _ in texts]

            def embed_query(self, text):
                return []

        gate = semantic_gate.SemanticGate(InvalidEmbeddings(), margin=0.08)
        decision = gate.decide("我的脚本好像有点问题")
        self.assertEqual(decision.label, "gate_error")
        self.assertFalse(decision.allow_scan)

    def test_gate_failure_cooldown_avoids_repeated_embedding_calls(self):
        class FailingEmbeddings:
            def __init__(self):
                self.document_calls = 0
                self.query_calls = 0

            def embed_documents(self, texts):
                self.document_calls += 1
                raise RuntimeError("service down")

            def embed_query(self, text):
                self.query_calls += 1
                raise RuntimeError("service down")

        embeddings = FailingEmbeddings()
        gate = semantic_gate.SemanticGate(
            embeddings, margin=0.08, failure_cooldown_seconds=60
        )
        first = gate.decide("我的脚本好像有点问题")
        second = gate.decide("另一个问题")
        self.assertEqual(first.reason, "gate_exception")
        self.assertEqual(second.reason, "gate_recent_failure")
        self.assertFalse(first.allow_scan)
        self.assertFalse(second.allow_scan)
        self.assertEqual(embeddings.document_calls, 1)
        self.assertEqual(embeddings.query_calls, 0)


class QueryPreprocessorTests(unittest.TestCase):
    def test_text_and_image_summary_reaches_retrieval_and_generation(self):
        prepared = preprocessor.prepare_query(
            "帮我看看",
            ["data:image/png;base64,abc"],
            lambda _urls: "截图显示接口返回 500",
        )
        self.assertIn("帮我看看", prepared.query_for_retrieval)
        self.assertIn("接口返回 500", prepared.query_for_retrieval)
        self.assertIn("接口返回 500", prepared.context_text_for_generation)

    def test_text_only_does_not_call_vision_summarizer(self):
        calls = []
        prepared = preprocessor.prepare_query(
            "接口返回 500",
            [],
            lambda urls: calls.append(urls) or "不应调用",
        )
        self.assertEqual(calls, [])
        self.assertEqual(prepared.context_text_for_generation, "接口返回 500")


class AskPipelineOrderingTests(unittest.TestCase):
    def test_kb_runs_before_the_only_semantic_gate(self):
        source = (RAG_DIR / "app_fastapi.py").read_text(encoding="utf-8")
        ask_source = source[source.index("def _answer_question") :]
        retrieve_pos = ask_source.index("chunks = retriever.retrieve")
        chat_pos = ask_source.index("raw_answer = chat")
        coverage_pos = ask_source.index('if coverage in ("完全命中", "部分命中")')
        gate_pos = ask_source.index("gate_decision = semantic_gate.decide")
        scan_pos = ask_source.index("analysis = scan_and_analyze")
        self.assertLess(retrieve_pos, chat_pos)
        self.assertLess(chat_pos, coverage_pos)
        self.assertLess(coverage_pos, gate_pos)
        self.assertLess(gate_pos, scan_pos)
        self.assertIn("if not gate_decision.allow_scan", ask_source)

    def test_old_rule_and_agent_gates_are_removed(self):
        source = (RAG_DIR / "app_fastapi.py").read_text(encoding="utf-8")
        gate_source = (RAG_DIR / "rag_agent" / "query" / "semantic_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("prescreen_error", source)
        self.assertNotIn("decide_scan_after_kb", source)
        self.assertNotIn("allow_retrieval", source)
        self.assertNotIn("prescreen_error", gate_source)
        self.assertFalse((RAG_DIR / "rag_agent" / "query" / "scan_gate.py").exists())

    def test_gate_is_skipped_when_kb_hits(self):
        source = (RAG_DIR / "app_fastapi.py").read_text(encoding="utf-8")
        ask_source = source[source.index("def _answer_question") :]
        hit_branch = ask_source[
            ask_source.index('if coverage in ("完全命中", "部分命中")') :
            ask_source.index("gate_decision = semantic_gate.decide")
        ]
        self.assertIn("user_visible = raw_answer", hit_branch)
        self.assertIn("else:", hit_branch)

    def test_image_summary_empty_never_reaches_source_scan(self):
        source = (RAG_DIR / "app_fastapi.py").read_text(encoding="utf-8")
        self.assertIn("not prep.image_summary.strip()", source)
        self.assertIn("截图里暂时没有识别到错误文字", source)


if __name__ == "__main__":
    unittest.main()
