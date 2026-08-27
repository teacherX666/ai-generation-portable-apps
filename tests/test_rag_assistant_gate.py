# -*- coding: utf-8 -*-
"""报错问答助手前置语义闸门的无网络回归测试。"""
from __future__ import annotations

import importlib.util
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
        self.fail_query = False

    def embed_documents(self, texts):
        self.document_calls += 1
        markers = ("Traceback", "Error", "报错", "失败", "异常", "超时", "500", "部署", "程序崩了", "接口请求")
        return [
            [1.0, 0.0] if any(m in t for m in markers) else [0.0, 1.0]
            for t in texts
        ]

    def embed_query(self, text):
        if self.fail_query:
            raise RuntimeError("embedding unavailable")
        markers = ("NameError", "Error", "报错", "失败", "异常", "超时", "500")
        if any(m in text for m in markers):
            return [1.0, 0.0]
        return [0.0, 1.0]


class SemanticGateTests(unittest.TestCase):
    def test_error_input_allows_retrieval_and_scan(self):
        gate = semantic_gate.SemanticGate(FakeEmbeddings(), margin=0.08, top_k=3)
        decision = gate.decide("NameError: name 'x' is not defined")
        self.assertEqual(decision.label, "error_report")
        self.assertTrue(decision.allow_retrieval)
        self.assertTrue(decision.allow_scan)

    def test_unrelated_input_short_circuits_before_retrieval(self):
        gate = semantic_gate.SemanticGate(FakeEmbeddings(), margin=0.08, top_k=3)
        decision = gate.decide("今天天气怎么样")
        self.assertEqual(decision.label, "unrelated")
        self.assertFalse(decision.allow_retrieval)
        self.assertFalse(decision.allow_scan)

    def test_gate_failure_allows_kb_but_blocks_expensive_source_scan(self):
        embeddings = FakeEmbeddings()
        embeddings.fail_query = True
        gate = semantic_gate.SemanticGate(embeddings, margin=0.08, top_k=3)
        decision = gate.decide("服务启动失败")
        self.assertEqual(decision.label, "gate_error")
        self.assertTrue(decision.allow_retrieval)
        self.assertFalse(decision.allow_scan)
        self.assertEqual(decision.reason, "gate_exception")

    def test_example_embeddings_are_cached(self):
        embeddings = FakeEmbeddings()
        gate = semantic_gate.SemanticGate(embeddings, margin=0.08, top_k=3)
        gate.decide("NameError: boom")
        gate.decide("今天天气怎么样")
        self.assertEqual(embeddings.document_calls, 1)

    def test_prescreen_unrelated_short_circuits_without_embedding(self):
        self.assertEqual(semantic_gate.prescreen_error("1+1"), "unrelated")
        self.assertEqual(semantic_gate.prescreen_error("2的三次方"), "unrelated")
        self.assertEqual(semantic_gate.prescreen_error("你好"), "unrelated")

    def test_prescreen_error_short_circuits_without_embedding(self):
        self.assertEqual(semantic_gate.prescreen_error("接口返回 500"), "error")
        self.assertEqual(semantic_gate.prescreen_error("Traceback (most recent call last)"), "error")

    def test_prescreen_unknown_falls_back_to_semantic_gate(self):
        self.assertIsNone(semantic_gate.prescreen_error("我的脚本好像有点问题"))

    def test_generation_prompt_is_not_an_error(self):
        # 不再因为“生成视频/参考图/台词”等业务词提前拦截，交给成熟的语义闸门。
        self.assertIsNone(
            semantic_gate.prescreen_error("生成视频禁止出现台词和文字，参考图一转写镜头固定")
        )

    def test_generation_prompt_with_error_still_counts_as_error(self):
        self.assertEqual(
            semantic_gate.prescreen_error(
                "生成视频：短发女人，禁止台词和文字；但提交后返回 duration not valid"
            ),
            "error",
        )

    def test_generation_failure_still_counts_as_error(self):
        self.assertEqual(semantic_gate.prescreen_error("生成视频失败，返回 500"), "error")


class QueryPreprocessorTests(unittest.TestCase):
    def test_text_and_image_summary_reaches_retrieval_and_generation(self):
        prepared = preprocessor.prepare_query(
            text="任务失败",
            image_data_urls=["data:image/png;base64,abc"],
            summarizer=lambda _urls: "InvalidParameter: duration not valid",
        )
        self.assertIn("InvalidParameter", prepared.query_for_retrieval)
        self.assertIn("InvalidParameter", prepared.context_text_for_generation)
        self.assertEqual(prepared.image_summary, "InvalidParameter: duration not valid")

    def test_text_only_does_not_call_vision_summarizer(self):
        def should_not_run(_urls):
            raise AssertionError("纯文本不应调用视觉模型")

        prepared = preprocessor.prepare_query("接口返回 500", [], should_not_run)
        self.assertEqual(prepared.query_for_retrieval, "接口返回 500")
        self.assertEqual(prepared.context_text_for_generation, "接口返回 500")


class AskPipelineOrderingTests(unittest.TestCase):
    def test_gate_runs_before_kb_retrieval(self):
        source = (RAG_DIR / "app_fastapi.py").read_text(encoding="utf-8")
        ask_source = source[source.index("async def ask") :]
        gate_pos = ask_source.index("semantic_gate.decide")
        retrieve_pos = ask_source.index("retriever.retrieve")
        chat_pos = ask_source.index("raw_answer = chat")
        self.assertLess(gate_pos, retrieve_pos)
        self.assertLess(gate_pos, chat_pos)
        self.assertIn("if not gate_decision.allow_retrieval", ask_source)


if __name__ == "__main__":
    unittest.main()
