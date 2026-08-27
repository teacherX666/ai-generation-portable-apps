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
scan_gate = _load_module(
    "rag_assistant_scan_gate_test",
    RAG_DIR / "rag_agent" / "query" / "scan_gate.py",
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

    def test_unknown_failure_symptoms_are_not_preclassified_as_unrelated(self):
        for text in (
            "点击提交后没有反应",
            "页面一直白屏",
            "任务卡住了",
            "上传后没有结果",
            "突然不能生成了",
            "我的脚本好像有点问题",
        ):
            self.assertIsNone(semantic_gate.prescreen_error(text), text)

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

    def test_image_summary_empty_is_not_silently_classified_as_unrelated(self):
        source = (RAG_DIR / "app_fastapi.py").read_text(encoding="utf-8")
        self.assertIn("not prep.image_summary.strip()", source)
        self.assertIn("截图里暂时没有识别到错误文字", source)


class GateHardeningTests(unittest.TestCase):
    def test_common_words_do_not_directly_trigger_error_prescreen(self):
        for text in (
            "生成500张图片",
            "生成一张图片，不要出现错误文字",
            "请不要返回错误信息",
            "连接飞书机器人",
            "invalid prompt 这个词只是提示词内容",
            "不要提到 exception 或 error 这两个单词",
            "不要提到 failed 这个词",
            "生成视频不支持出现台词",
            "生成图片不要出现错误文字",
        ):
            self.assertIsNone(semantic_gate.prescreen_error(text), text)

    def test_domain_error_phrases_are_fast_path_errors(self):
        for text in (
            "500",
            "HTTP 500",
            "TaskTypeConstraint",
            "ModelNotOpen",
            "quota exceeded",
            "rate limit",
            "账号欠费",
            "余额不足",
            "duration not valid",
            "must be <=15",
            "invalid token",
            "Error while downloading image",
            "The workflow node could not be completed",
            "失败怎么办",
            "参数不合法",
            "模型不支持",
            "生成任务失败",
        ):
            self.assertEqual(semantic_gate.prescreen_error(text), "error", text)

    def test_minimum_absolute_score_prevents_low_confidence_margin_match(self):
        class LowConfidenceEmbeddings:
            def embed_documents(self, texts):
                # 查询与报错示例相似度 0.50，与无关示例相似度 0.30；
                # 虽然分差 0.20 大于 margin，但绝对置信度不够。
                import math

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
        self.assertTrue(decision.allow_retrieval)
        self.assertFalse(decision.allow_scan)
        self.assertLess(decision.error_score, 0.55)
        self.assertGreater(decision.margin_score, 0.08)

    def test_invalid_embedding_is_safe_and_blocks_source_scan(self):
        class InvalidEmbeddings:
            def embed_documents(self, texts):
                return [[1.0, 0.0] for _ in texts]

            def embed_query(self, text):
                return []

        gate = semantic_gate.SemanticGate(InvalidEmbeddings(), margin=0.08)
        decision = gate.decide("我的脚本好像有点问题")
        self.assertEqual(decision.label, "gate_error")
        self.assertTrue(decision.allow_retrieval)
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
        self.assertEqual(embeddings.document_calls, 1)
        self.assertEqual(embeddings.query_calls, 0)


class PostKbScanGateTests(unittest.TestCase):
    @staticmethod
    def _chunk(title: str = "测试条目"):
        from langchain_core.documents import Document

        return Document(page_content="测试 KB 内容", metadata={"error_title": title})

    def test_only_explicit_error_allows_source_scan(self):
        calls = []

        def fake_chat(_settings, messages, **kwargs):
            calls.append((messages, kwargs["max_tokens"]))
            return '{"label":"error_report","reason":"用户明确描述提交失败"}'

        decision = scan_gate.decide_scan_after_kb(
            "点击提交后没有反应，任务一直卡住",
            [self._chunk()],
            "未命中",
            fake_chat,
            object(),
        )
        self.assertEqual(decision.label, "error_report")
        self.assertTrue(decision.allow_scan)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 160)

    def test_unrelated_is_stopped_after_kb(self):
        def fake_chat(_settings, _messages, **_kwargs):
            return '{"label":"unrelated","reason":"普通创作请求"}'

        decision = scan_gate.decide_scan_after_kb(
            "帮我写一首关于春天的诗", [], "未命中", fake_chat, object()
        )
        self.assertEqual(decision.label, "unrelated")
        self.assertFalse(decision.allow_scan)

    def test_uncertain_is_stopped_after_kb(self):
        def fake_chat(_settings, _messages, **_kwargs):
            return '{"label":"uncertain","reason":"信息不足"}'

        decision = scan_gate.decide_scan_after_kb(
            "帮我看看这个", [], "未命中", fake_chat, object()
        )
        self.assertEqual(decision.label, "uncertain")
        self.assertFalse(decision.allow_scan)

    def test_invalid_judge_response_fails_closed(self):
        def fake_chat(_settings, _messages, **_kwargs):
            return "我觉得应该扫描源码"

        decision = scan_gate.decide_scan_after_kb(
            "提交后没结果", [], "未命中", fake_chat, object()
        )
        self.assertEqual(decision.label, "gate_error")
        self.assertFalse(decision.allow_scan)

    def test_scan_gate_accepts_fenced_json_but_not_free_text(self):
        def fake_chat(_settings, _messages, **_kwargs):
            return '```json\n{"label":"error_report","reason":"任务失败"}\n```'

        decision = scan_gate.decide_scan_after_kb(
            "任务失败但没有错误码", [], "未命中", fake_chat, object()
        )
        self.assertTrue(decision.allow_scan)


class PipelineScanPermissionTests(unittest.TestCase):
    def test_source_scan_is_after_post_kb_gate_and_not_directly_after_chat(self):
        source = (RAG_DIR / "app_fastapi.py").read_text(encoding="utf-8")
        ask_source = source[source.index("def _answer_question") :]
        # 函数定义处的导入/声明不计入流程顺序，取实际调用位置。
        post_gate_call_pos = ask_source.index("scan_gate = decide_scan_after_kb(")
        scan_pos = ask_source.index("            analysis = scan_and_analyze(")
        self.assertLess(post_gate_call_pos, scan_pos)
        self.assertIn("if not scan_gate.allow_scan", ask_source)
        self.assertIn("scan_gate.allow_scan", ask_source)
        self.assertIn('gate_decision.label == "gate_error"', ask_source)


if __name__ == "__main__":
    unittest.main()
