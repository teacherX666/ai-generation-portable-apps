"""按次生成明细（用量 + 价格）的回归测试。

这是「像中转站那样一条一条记录用量和价格」的地基，三块都要钉住：
  1. 单价查表：模型族归一（接入点 id / mini / fast / 2.5 不能串档）、分辨率档位、
     有无输入视频两档、认不出就**不给金额**（宁缺勿猜）。
  2. 明细写入：轮询终态时真的往 generations.jsonl 追加一条，且金额算对。
  3. 权限与读取：管理员看全部、普通用户只看自己；CSV 导出列齐全。

真机依据（2026-10-12）：实测一条 Seedance 2.0 mini、4 秒、480p、无输入视频的
任务 usage.completion_tokens = 40594，按 23 元/百万 token 应为 0.9337 元。
"""

import csv
import zipfile
import io
import json
import sys
import tempfile
import threading
import time
from datetime import datetime
import unittest
from pathlib import Path
from unittest import mock

from tests.portal_isolation import load_isolated

ROOT = Path(__file__).resolve().parents[1]
PORTAL_APP = ROOT / "portal" / "app.py"


def load_portal_module():
    # 隔离数据目录：本文件也会驱动轮询循环（终态除 by_user 外还写按次明细）
    return load_isolated("portal_app_generations", PORTAL_APP)


class FakeResponse:
    def __init__(self, payload: dict):
        self.status = 200
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload


class FakeConnection:
    def __init__(self, payload: dict):
        self._payload = payload

    def request(self, *args, **kwargs):
        return None

    def getresponse(self) -> FakeResponse:
        return FakeResponse(self._payload)

    def close(self):
        return None


class PricingQuoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_portal_module()

    def setUp(self):
        # 用内置表：把 PRICING_PATH 指到不存在的文件，并**同时清掉缓存数据**——
        # mtime 相同就不会重载，只改 mtime 会让上一个用例的表泄漏进来。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mod.PRICING_PATH = Path(self._tmp.name) / "pricing.json"
        self.mod._PRICING_CACHE["mtime"] = -12345.0
        self.mod._PRICING_CACHE["data"] = self.mod._DEFAULT_PRICING

    def quote(self, **kw):
        return self.mod.quote_generation(**kw)

    def test_real_measured_job(self):
        """真机实测：seedance 2.0 mini / 480p / 无输入视频 / 40594 token。"""
        q = self.quote(model="ep-20260912121738-vtd78", resolution="480p",
                       has_input_video=False, tokens=40594)
        self.assertEqual(q["unit_price"], 23.0)
        self.assertEqual(q["family_id"], "seedance-2.0-mini")
        self.assertAlmostEqual(q["amount"], 0.9337, places=4)
        self.assertEqual(q["source"], "pricing_table")

    def test_input_video_tier_is_cheaper(self):
        q = self.quote(model="ep-20260912121738-vtd78", resolution="480p",
                       has_input_video=True, tokens=1_000_000)
        self.assertEqual(q["unit_price"], 14.0)
        self.assertAlmostEqual(q["amount"], 14.0, places=4)

    def test_family_ordering_does_not_cross_tiers(self):
        """mini / fast / 2.5 必须各自命中，不能被通用的 2.0 吃掉。"""
        cases = {
            "doubao-seedance-2-0-mini-260615": 23.0,
            "doubao-seedance-2-0-fast-260128": 37.0,
            "doubao-seedance-2-5-260628": 67.17,
            "doubao-seedance-2-0-260128": 44.88,
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                q = self.quote(model=model, resolution="720p",
                               has_input_video=False, tokens=1_000_000)
                self.assertEqual(q["unit_price"], expected, msg=q)

    def test_resolution_buckets(self):
        base = dict(model="doubao-seedance-2-0-260128", has_input_video=False, tokens=1_000_000)
        self.assertEqual(self.quote(resolution="1080p", **base)["unit_price"], 50.5)
        self.assertEqual(self.quote(resolution="4k", **base)["unit_price"], 26.0)
        self.assertEqual(self.quote(resolution="480p", **base)["unit_price"], 44.88)
        self.assertEqual(self.quote(resolution="720p", **base)["unit_price"], 44.88)

    def test_unknown_resolution_falls_back_to_default_tier(self):
        q = self.quote(model="doubao-seedance-2-0-260128", resolution="8k",
                       has_input_video=False, tokens=1_000_000)
        self.assertEqual(q["unit_price"], 44.88)

    def test_unknown_model_gets_no_amount(self):
        """认不出的模型绝不给一个看起来合理的数字。"""
        q = self.quote(model="some-unknown-model", resolution="720p", tokens=123456)
        self.assertIsNone(q["amount"])
        self.assertIsNone(q["unit_price"])
        self.assertEqual(q["source"], "unknown")

    def test_empty_model_gets_no_amount(self):
        q = self.quote(model="", resolution="720p", tokens=999)
        self.assertIsNone(q["amount"])

    def test_image_family_prices_per_image(self):
        q = self.quote(model="doubao-seedream-5-0-pro-260628", image_size="2K", images=2)
        self.assertEqual(q["unit"], "CNY/张")
        self.assertAlmostEqual(q["amount"], 0.84, places=4)

    def test_image_small_bucket(self):
        q = self.quote(model="doubao-seedream-5-0-pro-260628", image_size="1.5K", images=1)
        self.assertAlmostEqual(q["amount"], 0.27, places=4)

    def test_zero_tokens_is_zero_amount_not_unknown(self):
        q = self.quote(model="ep-20260912121738-vtd78", resolution="480p", tokens=0)
        self.assertEqual(q["amount"], 0.0)
        self.assertEqual(q["source"], "pricing_table")

    def test_model_match_is_case_insensitive(self):
        q = self.quote(model="  Doubao-Seedance-2.0-Mini-260615  ", resolution="720p",
                       tokens=1_000_000)
        self.assertEqual(q["unit_price"], 23.0)

    def test_editable_pricing_file_overrides_builtin(self):
        """运维改 pricing.json 立即生效（不重启 Portal）。"""
        data = {
            "schema_version": 1, "currency": "CNY",
            "endpoint_aliases": {"ep-x": "my-family"},
            "families": [{
                "id": "my-family", "label": "My", "unit": "video",
                "match": ["doubao-seedance-2-0-mini"],
                "resolution_buckets": {},
                "prices": {"default": {"no_input_video": 99.0, "input_video": 1.0}},
            }],
        }
        self.mod.PRICING_PATH.write_text(json.dumps(data), encoding="utf-8")
        self.mod._PRICING_CACHE["mtime"] = -1.0
        q = self.quote(model="ep-x", resolution="480p", tokens=1_000_000)
        self.assertEqual(q["unit_price"], 99.0)
        self.assertEqual(q["label"], "My")

    def test_broken_pricing_file_falls_back_to_builtin(self):
        self.mod.PRICING_PATH.write_text("{not json", encoding="utf-8")
        self.mod._PRICING_CACHE["mtime"] = -1.0
        q = self.quote(model="ep-20260912121738-vtd78", resolution="480p", tokens=1_000_000)
        self.assertEqual(q["unit_price"], 23.0, "损坏时应沿用内置表而不是崩溃")


class JobMetadataTests(unittest.TestCase):
    """有没有输入视频决定查哪一档单价，靠的是 multipart 里的**文件名**。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_portal_module()

    def _multipart(self, text_fields: dict, file_fields: dict) -> tuple[str, bytes]:
        boundary = "----testboundary"
        parts = []
        for k, v in text_fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
        for k, fname in file_fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                         f'Content-Type: video/mp4\r\n\r\n<bytes>\r\n')
        body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
        return f"multipart/form-data; boundary={boundary}", body

    def test_detects_reference_video(self):
        ctype, body = self._multipart(
            {"prompt": "一只猫", "model": "doubao-seedance-2-0-mini", "resolution": "480p"},
            {"ref_video_1": "a.mp4"},
        )
        meta = self.mod.extract_job_metadata(ctype, body)
        self.assertTrue(meta["has_input_video"])
        self.assertIn("ref_video_1", meta["media_fields"])
        self.assertEqual(meta["model"], "doubao-seedance-2-0-mini")
        self.assertEqual(meta["params"]["resolution"], "480p")

    def test_first_frame_is_not_input_video(self):
        """首帧/尾帧是图，不算「有输入视频」档。"""
        ctype, body = self._multipart(
            {"prompt": "一只猫"}, {"first_frame": "a.png", "last_frame": "b.png"},
        )
        meta = self.mod.extract_job_metadata(ctype, body)
        self.assertFalse(meta["has_input_video"])
        self.assertEqual(sorted(meta["media_fields"]), ["first_frame", "last_frame"])

    def test_no_media_is_no_input_video(self):
        ctype, body = self._multipart({"prompt": "一只猫"}, {})
        meta = self.mod.extract_job_metadata(ctype, body)
        self.assertFalse(meta["has_input_video"])
        self.assertEqual(meta["media_fields"], [])

    def test_file_contents_are_never_captured(self):
        ctype, body = self._multipart({"prompt": "x"}, {"ref_video_1": "a.mp4"})
        meta = self.mod.extract_job_metadata(ctype, body)
        self.assertNotIn("ref_video_1", meta["params"], "文件字段绝不能进 params")

    # ── 供应商：请求体里本来就带 provider，取到就不必靠模型名反推 ──────────────
    def test_captures_provider_from_body(self):
        ctype, body = self._multipart(
            {"prompt": "一只猫", "model": "doubao-seedance-2-0-mini", "provider": "volcengine"}, {})
        meta = self.mod.extract_job_metadata(ctype, body)
        self.assertEqual(meta["provider"], "volcengine")

    def test_provider_defaults_to_empty_when_absent(self):
        ctype, body = self._multipart({"prompt": "一只猫"}, {})
        self.assertEqual(self.mod.extract_job_metadata(ctype, body)["provider"], "")

    def test_provider_label_prefers_captured_value(self):
        """通道 id 要翻成**提供 key 的一方**：nano-banana 的 gemini 通道 key 是 Chiyun 提供的。"""
        self.assertEqual(self.mod.provider_label("seedance", "", "volcengine"), "火山方舟")
        self.assertEqual(self.mod.provider_label("nano-banana", "", "t8star"), "T8Star")
        self.assertEqual(self.mod.provider_label("nano-banana", "", "gemini"), "Chiyun")
        self.assertEqual(self.mod.provider_label("nano-banana", "", "comfyui_local"),
                         "本地（不消耗外部 key）")
        # 认不出的通道名原样显示（别丢信息），并说明是未识别通道
        self.assertEqual(self.mod.provider_label("seedance", "", "some-new-gateway"),
                         "some-new-gateway")
        self.assertIn("未识别", self.mod.provider_detail("seedance", "", "some-new-gateway"))

    def test_provider_label_derives_from_model_for_old_rows(self):
        """老记录没存 provider，只能按模型名去各通道的 base_url 反查供应商。"""
        cases = {
            ("seedance", "ep-20260912121738-vtd78"): "火山方舟",
            ("seedance", "doubao-seedance-2-5-260628"): "火山方舟",
            ("nano-banana", "flux2_klein_allinone"): "本地（不消耗外部 key）",
            # banana2-ssvip 走的是通道 gemini，其 base_url 是 chiyun.work → 供应商是 Chiyun
            ("nano-banana", "banana2-ssvip"): "Chiyun",
            # gemini-3-pro-image 属于 t8star 通道 → 供应商是 T8Star（不是 Google）
            ("nano-banana", "gemini-3-pro-image"): "T8Star",
            ("feishu-generation-agent", "anything"): "飞书 Agent（自带 key）",
            ("dreamina", "jimeng-3.0"): "即梦 Dreamina",
        }
        for (app, model), expected in cases.items():
            with self.subTest(app=app, model=model):
                self.assertEqual(self.mod.provider_label(app, model, ""), expected)

    def test_provider_label_is_empty_when_unknown(self):
        self.assertEqual(self.mod.provider_label("mystery-app", "mystery-model", ""), "",
                         "认不出就留空，别编一个供应商")


class GenerationRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_portal_module()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "generations.jsonl"
        self.mod.GENERATIONS_PATH = self.path
        self.mod.PRICING_PATH = Path(self._tmp.name) / "nope.json"
        self.mod._PRICING_CACHE["mtime"] = -12345.0
        self.mod._PRICING_CACHE["data"] = self.mod._DEFAULT_PRICING
        self.mod._GENERATIONS_LAST_PRUNE = 0.0

    def _tracker(self):
        tracker = self.mod.UsageTracker.__new__(self.mod.UsageTracker)
        tracker._lock = threading.Lock()
        tracker._data = self.mod.UsageTracker._empty_data()
        tracker._pending_jobs = []
        tracker._dirty = False
        tracker._flush_wake = threading.Event()
        tracker._save = lambda: None
        return tracker

    def _drain_poll_once(self, tracker, payload, app="seedance"):
        conn = FakeConnection(payload)
        sleeps = {"n": 0}

        def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] > 1:
                raise StopIteration

        with mock.patch.object(self.mod.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(self.mod.http.client, "HTTPConnection", return_value=conn), \
             mock.patch.object(self.mod, "APPS", {app: {"port": 18888}}):
            with self.assertRaises(StopIteration):
                tracker._job_poll_loop()

    def _lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_poller_writes_one_record_with_amount(self):
        """真机口径复现：40594 token x 23 元/百万 = 0.9337 元。"""
        tracker = self._tracker()
        tracker.register_job(
            "seedance", "job-1", "alice", job_type="video",
            metadata={"model": "ep-20260912121738-vtd78", "params": {"resolution": "480p"},
                      "has_input_video": False},
        )
        tracker.inc_daily_jobs("seedance")
        self._drain_poll_once(tracker, {
            "status": "succeeded", "done": 1, "total": 1, "duration": 4, "tokens": 40594,
            "provider_tasks": [{"task_id": "cgt-1"}, {"task_id": "cgt-2"}],
        })

        rows = self._lines()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["username"], "alice")
        self.assertEqual(row["app"], "seedance")
        self.assertEqual(row["model"], "ep-20260912121738-vtd78")
        self.assertEqual(row["tokens"], 40594)
        self.assertEqual(row["unit_price"], 23.0)
        self.assertAlmostEqual(row["amount"], 0.9337, places=4)
        self.assertEqual(row["price_source"], "pricing_table")
        self.assertEqual(row["task_ids"], ["cgt-1", "cgt-2"])
        self.assertEqual(row["runs"], 1)
        self.assertEqual(row["seconds"], 4)

    def test_input_video_flag_changes_price(self):
        tracker = self._tracker()
        tracker.register_job(
            "seedance", "job-2", "bob", job_type="video",
            metadata={"model": "ep-20260912121738-vtd78", "params": {"resolution": "480p"},
                      "has_input_video": True, "media_fields": ["ref_video_1"]},
        )
        tracker.inc_daily_jobs("seedance")
        self._drain_poll_once(tracker, {"status": "succeeded", "done": 1, "total": 1,
                                        "duration": 4, "tokens": 1_000_000})
        row = self._lines()[0]
        self.assertEqual(row["unit_price"], 14.0)
        self.assertAlmostEqual(row["amount"], 14.0, places=4)
        self.assertTrue(row["has_input_video"])
        self.assertEqual(row["media_fields"], ["ref_video_1"])

    def test_non_ark_job_records_zero_but_no_amount(self):
        """本地/Chiyun 通道：没有 token 也没有单价 —— 记录保留、金额留空。"""
        tracker = self._tracker()
        tracker.register_job("nano-banana", "job-3", "carol", job_type="image",
                             metadata={"model": "banana2-ssvip"})
        tracker.inc_daily_jobs("nano-banana")
        self._drain_poll_once(tracker, {"status": "succeeded", "done": 2, "total": 2},
                              app="nano-banana")
        row = self._lines()[0]
        self.assertEqual(row["images"], 2)
        self.assertEqual(row["tokens"], 0)
        self.assertIsNone(row["amount"])
        self.assertEqual(row["price_source"], "unknown")

    def test_no_output_writes_no_record(self):
        tracker = self._tracker()
        tracker.register_job("seedance", "job-4", "dave", job_type="video",
                             metadata={"model": "ep-20260912121738-vtd78"})
        tracker.inc_daily_jobs("seedance")
        self._drain_poll_once(tracker, {"status": "failed", "done": 0, "total": 1,
                                        "tokens": 999999})
        self.assertEqual(self._lines(), [], "没有产出就不该有明细行")

    def test_statistics_still_written_alongside_record(self):
        """明细是新加的，不能把原来的 by_user 统计顶掉。"""
        tracker = self._tracker()
        tracker.register_job("seedance", "job-5", "erin", job_type="video",
                             duration_per_item=4,
                             metadata={"model": "ep-20260912121738-vtd78"})
        tracker.inc_daily_jobs("seedance")
        self._drain_poll_once(tracker, {"status": "succeeded", "done": 1, "total": 1,
                                        "duration": 4, "tokens": 40594})
        day = next(iter(tracker._data["by_user"].values()))
        self.assertEqual(day["erin"]["seedance"]["tokens"], 40594)
        self.assertEqual(day["erin"]["seedance"]["seconds"], 4)
        self.assertEqual(len(self._lines()), 1)

    def test_read_filters_by_date_and_owner(self):
        self.path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
            {"date": "2026-10-11", "ts": 1, "username": "alice", "tokens": 10, "amount": 1.0},
            {"date": "2026-10-12", "ts": 2, "username": "alice", "tokens": 20, "amount": 2.0},
            {"date": "2026-10-12", "ts": 3, "username": "bob", "tokens": 30, "amount": 3.0},
        ]) + "\n", encoding="utf-8")

        admin_rows, admin_total = self.mod.read_generation_records("2026-10-12", "2026-10-12", is_admin=True)
        self.assertEqual([r["username"] for r in admin_rows], ["bob", "alice"], "最新在前")
        self.assertEqual(admin_total, 2)

        own_rows, own_total = self.mod.read_generation_records("2026-10-12", "2026-10-12",
                                                              username="alice", is_admin=False)
        self.assertEqual([r["username"] for r in own_rows], ["alice"])
        self.assertEqual(own_total, 1)

        ranged, _ = self.mod.read_generation_records("2026-10-11", "2026-10-11", is_admin=True)
        self.assertEqual(len(ranged), 1)

    def test_corrupt_line_is_skipped(self):
        self.path.write_text('{"date": "2026-10-12", "ts": 1, "username": "a"}\nnot json\n',
                             encoding="utf-8")
        rows, _total = self.mod.read_generation_records("2026-10-12", "2026-10-12", is_admin=True)
        self.assertEqual(len(rows), 1, "坏行要跳过而不是让整次读取失败")

    # ── 筛选与翻页（2026-10-12 合并页面时新增）─────────────────────────────
    def test_filters_by_app_user_status_and_query(self):
        self.path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
            {"date": "2026-10-12", "ts": 1, "username": "alice", "app": "seedance",
             "status": "succeeded", "title": "橘猫看雨", "model": "ep-x", "tokens": 10},
            {"date": "2026-10-12", "ts": 2, "username": "bob", "app": "nano-banana",
             "status": "failed", "title": "海报", "model": "banana", "tokens": 0},
        ]) + "\n", encoding="utf-8")

        def read(**kw):
            return self.mod.read_generation_records("2026-10-12", "2026-10-12", is_admin=True, **kw)

        self.assertEqual(read(app_filter="seedance")[1], 1)
        self.assertEqual(read(user_filter="bob")[1], 1)
        self.assertEqual(read(status_filter="failed")[1], 1)
        self.assertEqual(read(query="橘猫")[1], 1)
        self.assertEqual(read(query="橘猫", app_filter="nano-banana")[1], 0, "筛选是与关系")
        self.assertEqual(read(query="banana")[1], 1, "关键词也要能命中模型名")

    def test_pagination_slices_and_reports_total(self):
        rows = [{"date": "2026-10-12", "ts": i, "username": "alice", "app": "seedance",
                 "tokens": i} for i in range(1, 8)]
        self.path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        page1, total = self.mod.read_generation_records("2026-10-12", "2026-10-12",
                                                       is_admin=True, limit=3, offset=0)
        self.assertEqual(total, 7, "total 是命中总数，不是本页条数")
        self.assertEqual([r["ts"] for r in page1], [7, 6, 5], "最新在前")
        page3, _ = self.mod.read_generation_records("2026-10-12", "2026-10-12",
                                                   is_admin=True, limit=3, offset=6)
        self.assertEqual([r["ts"] for r in page3], [1], "最后一页只有 1 条")

    # ── 兜底登记：不上报 X-Job-Id 的应用（飞书等）过去完全不统计 ──────────────
    def test_count_only_path_matching_is_exact(self):
        """只认「建任务」的精确路径 —— 子操作（/decision、/cancel）不能算新建任务。"""
        is_count_only = self.mod.UsageTracker._is_count_only_job_request
        self.assertTrue(is_count_only(None, "POST", "/api/runs"))
        self.assertTrue(is_count_only(None, "POST", "/api/runs?foo=1"))
        self.assertFalse(is_count_only(None, "POST", "/api/runs/abc/decision"))
        self.assertFalse(is_count_only(None, "POST", "/api/runs/abc/cancel"))
        self.assertFalse(is_count_only(None, "GET", "/api/runs"))

    def test_untracked_job_is_counted_but_never_fabricates_numbers(self):
        """兜底登记只记次数与归属：token/金额留空，且**不动 by_user**（不知道就别写 0）。"""
        tracker = self._tracker()
        tracker.record_untracked_job("feishu-generation-agent", "alice", "video",
                                     title="飞书文档出片")
        self.assertEqual(tracker._pending_jobs, [], "没有 job id，不能进轮询队列")
        self.assertEqual(tracker._data.get("by_user"), {}, "不知道张数/秒数/token 就不能写")
        day = next(iter(tracker._data["daily"].values()))
        self.assertEqual(day["feishu-generation-agent"]["jobs"], 1, "任务数要 +1")
        row = self._lines()[0]
        self.assertFalse(row["tracked"])
        self.assertEqual(row["tokens"], 0)
        self.assertIsNone(row["amount"])
        self.assertEqual(row["price_source"], "untracked")
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["username"], "alice")
        self.assertEqual(row["app"], "feishu-generation-agent")

    # ── 重复计账（2026-10-12 生产事故）────────────────────────────────────
    # 同一次生成被记了 5~6 次：by_user 的 token/秒数被放大几倍，按次明细也跟着翻倍。
    # 下面三条分别钉住「注册去重」「恢复去重」「计账一次性」。
    def test_duplicate_registration_is_ignored(self):
        tracker = self._tracker()
        meta = {"model": "ep-20260912121738-vtd78", "params": {"resolution": "480p"}}
        for _ in range(5):
            tracker.register_job("seedance", "job-dup", "alice", job_type="video", metadata=meta)
        self.assertEqual(len(tracker._pending_jobs), 1, "同一 (app, job_id) 只能有一个 pending 条目")

    def test_duplicate_registration_counts_once(self):
        """重复注册 5 次，最终 token 只能计一次。"""
        tracker = self._tracker()
        meta = {"model": "ep-20260912121738-vtd78", "params": {"resolution": "480p"}}
        for _ in range(5):
            tracker.register_job("seedance", "job-dup", "alice", job_type="video", metadata=meta)
        tracker.inc_daily_jobs("seedance")
        self._drain_poll_once(tracker, {"status": "succeeded", "done": 1, "total": 1,
                                        "duration": 4, "tokens": 40594})
        self.assertEqual(len(self._lines()), 1)
        day = next(iter(tracker._data["by_user"].values()))
        self.assertEqual(day["alice"]["seedance"]["tokens"], 40594, "不能是 5 倍")
        self.assertEqual(day["alice"]["seedance"]["seconds"], 4)

    def test_restore_deduplicates_same_job(self):
        """历史里同一 job 有多条记录（不同 key）时，恢复只能产生一个条目。"""
        now = time.time()
        history = {
            "seedance:job-1": {"app": "seedance", "job_id": "job-1", "status": "running", "submitted_at": now},
            "job-1": {"app": "seedance", "job_id": "job-1", "status": "running", "submitted_at": now},
            "seedance:job-1:again": {"app": "seedance", "job_id": "job-1", "status": "pending", "submitted_at": now},
            "seedance:job-2": {"app": "seedance", "job_id": "job-2", "status": "running", "submitted_at": now},
        }
        tracker = self._tracker()
        with mock.patch.object(tracker, "_load_history", return_value=history):
            restored = tracker._restore_pending_jobs()
        self.assertEqual(sorted(j["job_id"] for j in restored), ["job-1", "job-2"])

    def test_restored_job_keeps_pricing_dimensions(self):
        """恢复的条目必须带模型/分辨率，否则这些任务算不出金额（会变成「未识别计费项」）。"""
        now = time.time()
        history = {
            "seedance:job-p": {
                "app": "seedance", "job_id": "job-p", "status": "running",
                "submitted_at": now, "kind": "video",
                "model": "ep-20260912121738-vtd78",
                "params": {"resolution": "480p", "duration": "8"},
            },
        }
        tracker = self._tracker()
        with mock.patch.object(tracker, "_load_history", return_value=history):
            restored = tracker._restore_pending_jobs()
        self.assertEqual(len(restored), 1)
        entry = restored[0]
        self.assertEqual(entry["model"], "ep-20260912121738-vtd78")
        self.assertEqual(entry["resolution"], "480p")
        self.assertEqual(entry["duration_per_item"], 8)
        # 能查到价 → 明细里不会变成「未识别计费项」
        quote = self.mod.quote_generation(model=entry["model"], resolution=entry["resolution"],
                                          tokens=100_000)
        self.assertEqual(quote["unit_price"], 23.0)
        self.assertIsNotNone(quote["amount"])

    def test_tally_is_one_shot_even_if_history_update_raises(self):
        """计账之后若历史更新抛异常（Windows 上 replace 会被读者句柄顶掉），条目会留在
        pending 里被再次轮询 —— 第二次绝不能再计一次账。"""
        tracker = self._tracker()
        tracker.register_job("seedance", "job-err", "alice", job_type="video", duration_per_item=4,
                             metadata={"model": "ep-20260912121738-vtd78"})
        tracker.inc_daily_jobs("seedance")
        payload = {"status": "succeeded", "done": 1, "total": 1, "duration": 4, "tokens": 40594}
        with mock.patch.object(tracker, "history_update_terminal", side_effect=RuntimeError("boom")):
            self._drain_poll_once(tracker, payload)
        self._drain_poll_once(tracker, payload)
        self.assertEqual(len(self._lines()), 1, "不能写出第二条明细")
        day = next(iter(tracker._data["by_user"].values()))
        self.assertEqual(day["alice"]["seedance"]["tokens"], 40594, "不能计两次")


class BillingItemTests(unittest.TestCase):
    """导出必须能和方舟账单的「计费项」对齐 —— 这是用户拿去对账的关键字段。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_portal_module()

    def label(self, *args, **kwargs):
        return self.mod.billing_item_label(*args, **kwargs)

    def test_seedance_25_uses_combined_resolution_bucket(self):
        self.assertEqual(
            self.label("Seedance 2.5", "720p", has_input_video=False),
            "Doubao-Seedance-2.5在线推理480P/720P（无输入视频）")
        self.assertEqual(
            self.label("Seedance 2.5", "480p", has_input_video=True),
            "Doubao-Seedance-2.5在线推理480P/720P（有输入视频）")

    def test_seedance_20_resolution_tiers(self):
        self.assertEqual(self.label("Seedance 2.0", "1080p"),
                         "Doubao-Seedance-2.0在线推理1080P（无输入视频）")
        self.assertEqual(self.label("Seedance 2.0", "4k"),
                         "Doubao-Seedance-2.0在线推理4k（无输入视频）")
        # 账单里 480P/720P 档不带分辨率后缀
        self.assertEqual(self.label("Seedance 2.0", "720p"),
                         "Doubao-Seedance-2.0在线推理（无输入视频）")

    def test_mini_and_fast_have_own_items(self):
        self.assertEqual(self.label("Seedance 2.0 mini", "480p"),
                         "Doubao-Seedance-2.0-mini在线推理（无输入视频）")
        self.assertEqual(self.label("Seedance 2.0 fast", "720p", has_input_video=True),
                         "Doubao-Seedance-2.0-fast在线推理（有输入视频）")

    def test_seedream_items_by_output_size(self):
        self.assertEqual(self.label("Seedream 5.0 Pro", image_size="1.5K"),
                         "Doubao-Seedream-5.0-pro-不超过1.5K输出图")
        self.assertEqual(self.label("Seedream 5.0 Pro", image_size="2K"),
                         "Doubao-Seedream-5.0-pro-超过1.5K不超过2K输出图")

    def test_unknown_family_returns_empty_not_a_guess(self):
        self.assertEqual(self.label("banana2-ssvip", "16:9"), "")
        self.assertEqual(self.label("", "720p"), "")


class BillSummaryExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_portal_module()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mod.GENERATIONS_PATH = Path(self._tmp.name) / "generations.jsonl"
        self.mod.remote_sources.use_state_dir(self._tmp.name)   # 别读到真实的远端来源数据
        rows = [
            {"date": "2026-10-12", "ts": 1, "username": "MM", "app": "seedance",
             "model": "ep-x", "price_family": "Seedance 2.0 mini", "resolution": "480p",
             "has_input_video": False, "tokens": 100000, "amount": 2.3, "currency": "CNY"},
            {"date": "2026-10-12", "ts": 2, "username": "MM", "app": "seedance",
             "model": "ep-x", "price_family": "Seedance 2.0 mini", "resolution": "480p",
             "has_input_video": False, "tokens": 50000, "amount": 1.15, "currency": "CNY"},
            {"date": "2026-10-12", "ts": 3, "username": "MM", "app": "seedance",
             "model": "doubao-seedance-2-5-260628", "price_family": "Seedance 2.5",
             "resolution": "720p", "has_input_video": True, "tokens": 200000,
             "amount": 8.058, "currency": "CNY"},
            {"date": "2026-10-12", "ts": 4, "username": "MM", "app": "nano-banana",
             "model": "banana2-ssvip", "price_family": "", "tokens": 0, "amount": None},
        ]
        self.mod.GENERATIONS_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    def _csv(self, role="admin", username="admin") -> tuple[list[str], list[dict]]:
        handler = FakeHandler("/api/platform/stats/generations/summary.csv"
                              "?start=2026-10-12&end=2026-10-12")
        handler._generation_query = lambda: self.mod.Handler._generation_query(handler)
        handler._generation_filters = lambda: self.mod.Handler._generation_filters(handler)
        handler._bill_summary_rows = self.mod.Handler._bill_summary_rows
        handler._generation_rows_merged = (
            lambda *a, **k: self.mod.Handler._generation_rows_merged(handler, *a, **k))
        handler._feishu_generation_rows = lambda *a, **k: []   # 测试不碰真实飞书
        handler._history_generation_rows = lambda *a, **k: []  # 历史记录单独测
        handler._include_history_flag = (
            lambda: self.mod.Handler._include_history_flag(handler))
        self.mod.Handler._platform_stats_generations_summary_export(
            handler, {"username": username, "role": role})
        self.assertEqual(handler.status, 200)
        text = handler.wfile.getvalue().decode("utf-8-sig")
        lines = [l for l in text.splitlines() if l.strip()]
        header = lines[0].split(",")
        return header, [dict(zip(header, l.split(","))) for l in lines[1:]]

    def test_columns_match_bill_vocabulary(self):
        header, _ = self._csv()
        for col in ("账期", "消费日期", "模型", "计费项", "用量(千tokens)", "单位", "金额(元)", "币种"):
            self.assertIn(col, header)

    def test_groups_by_day_and_billing_item(self):
        header, rows = self._csv()
        self.assertEqual(len(rows), 3, f"应分成 3 组（mini×2 合并、2.5、未识别）: {rows}")
        by_item = {r["计费项"]: r for r in rows}
        mini = by_item["Doubao-Seedance-2.0-mini在线推理（无输入视频）"]
        self.assertEqual(mini["次数"], "2")
        self.assertEqual(mini["用量(千tokens)"], "150.0")
        self.assertEqual(mini["用量(tokens)"], "150000")
        self.assertEqual(mini["金额(元)"], "3.45")
        self.assertEqual(mini["单价(元/千tokens)"], "0.023")
        self.assertEqual(mini["账期"], "2026-10")

    def test_unpriced_group_is_marked_not_counted_as_zero(self):
        _, rows = self._csv()
        unpriced = [r for r in rows if "未识别" in r["计费项"]]
        self.assertEqual(len(unpriced), 1)
        self.assertEqual(unpriced[0]["金额(元)"], "", "没单价就不能给 0")
        self.assertEqual(unpriced[0]["单价(元/千tokens)"], "")
        self.assertIn("无单价", unpriced[0]["备注"])

    def test_non_admin_sees_only_own_rows(self):
        with self.mod.GENERATIONS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"date": "2026-10-12", "ts": 9, "username": "bob",
                                 "app": "seedance", "price_family": "Seedance 2.0 mini",
                                 "resolution": "480p", "tokens": 1000, "amount": 0.023},
                                ensure_ascii=False) + "\n")
        with mock.patch.object(self.mod.auth, "has_permission", return_value=False):
            _, rows = self._csv(role="user", username="bob")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["次数"], "1")


class FakeHandler:
    def __init__(self, path: str):
        self.path = path
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        return None

    def end_headers(self):
        return None

    def _cors_headers(self):
        return None

    def _json(self, code, payload):
        self.status = code
        self.json_payload = payload


class GenerationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_portal_module()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mod.GENERATIONS_PATH = Path(self._tmp.name) / "generations.jsonl"
        self.mod.remote_sources.use_state_dir(self._tmp.name)   # 别读到真实的远端来源数据
        self.feishu_rows: list[dict] = []   # 默认没有飞书任务；需要时在测试里赋值
        self.history_rows: list[dict] = []  # 历史记录（创作记录）合并进来的行
        rows = [
            {"date": "2026-10-12", "ts": 2, "time": "2026-10-12 13:11:55", "username": "alice",
             "app": "seedance", "model": "ep-20260912121738-vtd78", "tokens": 40594,
             "unit_price": 23.0, "unit": "CNY/百万token", "amount": 0.9337, "currency": "CNY",
             "price_source": "pricing_table", "price_family": "Seedance 2.0 mini",
             "task_ids": ["cgt-1"], "status": "succeeded", "images": 0, "seconds": 4, "runs": 1},
            {"date": "2026-10-12", "ts": 3, "time": "2026-10-12 13:20:00", "username": "bob",
             "app": "seedance", "model": "ep-20260912121738-vtd78", "tokens": 1000,
             "unit_price": 23.0, "unit": "CNY/百万token", "amount": 0.023, "currency": "CNY",
             "price_source": "pricing_table", "task_ids": [], "status": "succeeded"},
        ]
        self.mod.GENERATIONS_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    def _handler(self, path: str) -> FakeHandler:
        handler = FakeHandler(path)
        # 复用真实 Handler 的 querystring 解析、筛选、日期校验与汇总，避免在假对象里重复实现
        handler._generation_query = lambda: self.mod.Handler._generation_query(handler)
        handler._generation_filters = lambda: self.mod.Handler._generation_filters(handler)
        handler._generation_summary = self.mod.Handler._generation_summary
        handler._bill_summary_rows = self.mod.Handler._bill_summary_rows
        handler._generation_rows_merged = (
            lambda *a, **k: self.mod.Handler._generation_rows_merged(handler, *a, **k))
        # 飞书任务来自飞书 Agent 自己的运行记录（本地 HTTP 拉取）—— 测试里换成桩，
        # 既能断言合并逻辑，又不让测试依赖真实飞书进程。
        handler._feishu_generation_rows = lambda *a, **k: list(self.feishu_rows)
        handler._history_generation_rows = lambda *a, **k: list(self.history_rows)
        handler._include_history_flag = (
            lambda: self.mod.Handler._include_history_flag(handler))
        return handler

    def _qs(self, start="2026-10-12", end="2026-10-12"):
        return f"/api/platform/stats/generations?start={start}&end={end}"

    def test_admin_sees_everyone(self):
        h = self._handler(self._qs())
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.json_payload["summary"]["count"], 2)
        self.assertEqual(h.json_payload["summary"]["tokens"], 41594)
        self.assertAlmostEqual(h.json_payload["summary"]["amount"], 0.9567, places=4)
        self.assertTrue(h.json_payload["is_admin"])
        # 按应用计数：页面用它说明「飞书到底有没有记上」
        self.assertEqual(h.json_payload["summary"]["apps"], {"seedance": 2})

    def test_normal_user_sees_only_self(self):
        h = self._handler(self._qs())
        with mock.patch.object(self.mod.auth, "has_permission", return_value=False):
            self.mod.Handler._platform_stats_generations(h, {"username": "alice", "role": "user"})
        self.assertEqual(h.json_payload["summary"]["count"], 1)
        self.assertEqual([r["username"] for r in h.json_payload["rows"]], ["alice"])
        self.assertFalse(h.json_payload["is_admin"])

    def test_remote_source_rows_merge_with_source_label(self):
        """另一台同款平台的记录要能合并进同一张表，并带「来源」；去重键是 (来源 + 任务号)。

        2026-10-13 需求：https://192.168.30.56:9090 上的记录也要进本机的使用统计。
        两边的 job_id 空间互不相干 —— **绝不能只按 job_id 去重**，否则会互相吞记录。
        """
        rs = self.mod.remote_sources
        rows = [
            {"source": "服务器B(30.56)", "source_host": "https://192.168.30.56:9090",
             "date": "2026-10-12", "ts": 99, "time": "2026-10-12 09:00:00", "username": "bob",
             "app": "seedance", "model": "ep-x", "status": "succeeded", "tokens": 1000,
             "amount": 0.5, "job_id": "remote-job-1", "title": "远端任务"},
            # 与本机某行同 job_id：两边 id 空间不同，必须都保留
            {"source": "服务器B(30.56)", "date": "2026-10-12", "ts": 98,
             "time": "2026-10-12 08:00:00", "username": "bob", "app": "seedance",
             "model": "ep-x", "status": "succeeded", "tokens": 2000, "amount": 1.0,
             "job_id": "", "title": "没有任务号的远端记录"},
        ]
        rs.ROWS_PATH.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                encoding="utf-8")
        rs._ROWS_CACHE["mtime"] = None

        h = self._handler(self._qs())
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.status, 200)
        payload = h.json_payload
        got = [r for r in payload["rows"] if r.get("source")]
        self.assertEqual(len(got), 2)
        self.assertEqual({r["source"] for r in got}, {"服务器B(30.56)"})
        # 本机那 2 条仍在（没被远端吞掉）
        self.assertEqual(len([r for r in payload["rows"] if not r.get("source")]), 2)
        # 远端金额计入汇总
        self.assertAlmostEqual(payload["summary"]["amount"], 0.9567 + 1.5, places=3)
        # 按「用户」筛选也要能筛到远端行（本机 fixture 里也有一个 bob，所以是 1 本机 + 2 远端）
        h2 = self._handler(self._qs() + "&user=bob")
        self.mod.Handler._platform_stats_generations(h2, {"username": "admin", "role": "admin"})
        self.assertEqual(len(h2.json_payload["rows"]), 3)
        self.assertEqual(len([r for r in h2.json_payload["rows"] if r.get("source")]), 2)

    def test_remote_sync_route_is_post_only(self):
        """「立即同步」是**动作**，只挂在 POST 上。

        2026-10-13 踩到：第一版把这条路由加进了 do_GET 的链子里，而前端发的是 POST →
        点了按钮没反应（日志里连请求都没有）。挂在 GET 上还会被 <img>/预取误触发。
        """
        import inspect
        self.assertNotIn("remote-sources/sync", inspect.getsource(self.mod.Handler.do_GET))
        self.assertIn("remote-sources/sync", inspect.getsource(self.mod.Handler.do_POST))

    def test_remote_source_sync_status_endpoint(self):
        """同步状态接口：没配来源时也要 200（界面上那几件东西按 sources 是否为空来显隐）。"""
        h = self._handler("/api/platform/remote-sources")
        self.mod.Handler._remote_sources_status(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.status, 200)
        self.assertTrue(h.json_payload["ok"])
        self.assertEqual(h.json_payload["sources"], [])

    def test_unpriced_rows_are_counted_separately(self):
        with self.mod.GENERATIONS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"date": "2026-10-12", "ts": 4, "username": "carol",
                                 "app": "nano-banana", "tokens": 0, "amount": None,
                                 "price_source": "unknown"}, ensure_ascii=False) + "\n")
        h = self._handler(self._qs())
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        summary = h.json_payload["summary"]
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["priced_count"], 2)
        self.assertEqual(summary["unpriced_count"], 1)
        self.assertAlmostEqual(summary["amount"], 0.9567, places=4, msg="没价的行不能算进金额")

    def test_bad_date_returns_400(self):
        h = self._handler("/api/platform/stats/generations?start=nope&end=2026-10-12")
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.status, 400)

    # ── 云代理真实花费：中转站（new-api）的按请求用量日志 ──────────────────────
    def test_relay_amount_converts_quota_with_configured_rate(self):
        """quota → 人民币的口径写在定价表里，不散落在代码里。"""
        self.assertEqual(self.mod.relay_amount(500000), 7.2)
        self.assertEqual(self.mod.relay_amount(189000), 2.7216)
        self.assertIsNone(self.mod.relay_amount(0), "0 额度不成金额")

    def test_relay_log_fills_amount_by_model_and_time_window(self):
        rows = [
            {"ts": 1000.0, "model": "banana2-ssvip", "images": 1, "amount": None,
             "price_source": "unknown", "note": ""},
            {"ts": 9000.0, "model": "banana2-ssvip", "images": 1, "amount": None,
             "price_source": "unknown", "note": ""},
        ]
        entries = {"chiyun": [
            {"created_at": 1005.0, "model": "banana2-ssvip", "quota": 189000,
             "prompt_tokens": 516, "completion_tokens": 26174},
            {"created_at": 3000.0, "model": "其他模型", "quota": 999, "prompt_tokens": 0,
             "completion_tokens": 0},
        ]}
        with mock.patch.object(self.mod, "relay_usage_entries", return_value=entries):
            filled = self.mod._apply_relay_usage(rows)
        self.assertEqual(filled, 1, "只有同一个模型、且在时间窗内的那一条能对上")
        self.assertAlmostEqual(rows[0]["amount"], 2.7216, places=4)
        self.assertEqual(rows[0]["price_source"], "relay_log")
        self.assertEqual(rows[0]["unit_price"], 2.7216)
        self.assertEqual(rows[0]["tokens"], 26174, "中转站日志里的 completion_tokens 就是用量")
        self.assertIn("quota=189000", rows[0]["note"])
        self.assertIsNone(rows[1]["amount"], "对不上的宁可留空")

    def test_relay_log_never_reuses_one_entry_twice(self):
        rows = [
            {"ts": 1000.0, "model": "m", "images": 1, "amount": None, "price_source": "unknown"},
            {"ts": 1001.0, "model": "m", "images": 1, "amount": None, "price_source": "unknown"},
        ]
        entries = {"chiyun": [{"created_at": 1000.5, "model": "m", "quota": 500000,
                              "prompt_tokens": 0, "completion_tokens": 0}]}
        with mock.patch.object(self.mod, "relay_usage_entries", return_value=entries):
            filled = self.mod._apply_relay_usage(rows)
        self.assertEqual(filled, 1, "一条日志只能抵一条记录（否则会把花费算两遍）")
        self.assertIsNone(rows[1]["amount"])

    def test_relay_log_does_not_touch_already_priced_rows(self):
        rows = [{"ts": 1000.0, "model": "doubao-seedance-2-5", "tokens": 100, "amount": 1.23,
                 "price_source": "pricing_table"}]
        entries = {"chiyun": [{"created_at": 1000.0, "model": "doubao-seedance-2-5",
                              "quota": 500000, "prompt_tokens": 0, "completion_tokens": 0}]}
        with mock.patch.object(self.mod, "relay_usage_entries", return_value=entries):
            filled = self.mod._apply_relay_usage(rows)
        self.assertEqual(filled, 0)
        self.assertAlmostEqual(rows[0]["amount"], 1.23, places=4)

    # ── 飞书任务真实 token：方舟任务号 → 方舟 usage ─────────────────────────────
    def test_ark_task_index_sums_tokens_and_picks_right_tier(self):
        index = {
            "cgt-a": {"tokens": 100000, "model": "doubao-seedance-2-5-260628",
                      "resolution": "720p", "has_input_video": False, "duration": 4,
                      "status": "succeeded"},
            "cgt-b": {"tokens": 50000, "model": "doubao-seedance-2-5-260628",
                      "resolution": "720p", "has_input_video": False, "duration": 4,
                      "status": "succeeded"},
        }
        rows = [{"app": "feishu-generation-agent", "job_id": "run-1", "tokens": 0,
                 "amount": None, "price_source": "untracked", "note": "",
                 "provider_task_ids": ["cgt-a", "cgt-b", "cgt-没这条"]}]
        with mock.patch.object(self.mod, "ark_task_index", return_value=index):
            filled = self.mod._apply_ark_usage(rows)
        self.assertEqual(filled, 1)
        self.assertEqual(rows[0]["tokens"], 150000, "多个任务要累加")
        self.assertEqual(rows[0]["has_input_video"], False)
        # 无输入视频档 = 67.17 元/百万 token
        self.assertAlmostEqual(rows[0]["amount"], 150000 / 1_000_000 * 67.17, places=4)
        self.assertEqual(rows[0]["price_source"], "ark_usage")
        self.assertIn("逐任务精确对齐", rows[0]["note"])

    def test_ark_usage_uses_input_video_tier_when_any_task_had_reference(self):
        index = {"cgt-a": {"tokens": 100000, "model": "doubao-seedance-2-5-260628",
                           "resolution": "720p", "has_input_video": True, "duration": 4,
                           "status": "succeeded"}}
        rows = [{"app": "feishu-generation-agent", "tokens": 0, "amount": None,
                 "price_source": "untracked", "provider_task_ids": ["cgt-a"]}]
        with mock.patch.object(self.mod, "ark_task_index", return_value=index):
            self.mod._apply_ark_usage(rows)
        self.assertAlmostEqual(rows[0]["amount"], 100000 / 1_000_000 * 40.29, places=4,
                               msg="有参考视频的任务必须走有输入视频档（差 1.6 倍）")

    def test_ark_usage_leaves_rows_without_task_ids_alone(self):
        rows = [{"app": "feishu-generation-agent", "tokens": 0, "amount": None,
                 "price_source": "untracked", "provider_task_ids": []}]
        with mock.patch.object(self.mod, "ark_task_index", return_value={"cgt-x": {}}):
            self.assertEqual(self.mod._apply_ark_usage(rows), 0)
        self.assertIsNone(rows[0]["amount"])

    # ── 所有生成都要有记录：把创作记录里缺的历史生成并进使用记录 ────────────────
    def _history_row(self, job_id="recovered-1", app="seedance", username="alice"):
        return {"date": "2026-10-11", "ts": 100.0, "time": "2026-10-11 09:00:00",
                "username": username, "app": app, "job_id": job_id, "kind": "video",
                "title": "历史提示词", "model": "", "status": "done", "tokens": 0,
                "amount": None, "price_source": "history_only", "tracked": False}

    def test_history_records_are_merged_into_usage_records(self):
        """勾上「包含历史记录」时，创作记录里的历史生成也要出现在使用记录里。"""
        self.history_rows = [self._history_row()]
        h = self._handler("/api/platform/stats/generations?start=2026-10-11&end=2026-10-11&include_history=1")
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.json_payload["total"], 1)
        row = h.json_payload["rows"][0]
        self.assertEqual(row["job_id"], "recovered-1")
        self.assertEqual(row["price_source"], "history_only")
        self.assertEqual(row["tokens"], 0)
        self.assertIsNone(row["amount"], "当时没统计过 → 金额必须留空，不能猜")

    def test_history_records_hidden_by_default(self):
        """默认**不显示**历史记录：它们没有金额，一屏都是「—」会淹没真正有数据的记录。"""
        self.history_rows = [self._history_row()]
        h = self._handler("/api/platform/stats/generations?start=2026-10-11&end=2026-10-11")
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.json_payload["total"], 0, "没勾选时不该出现")
        listed = self._handler("/api/platform/stats/generations?start=2026-10-11&end=2026-10-11&include_history=0")
        self.mod.Handler._platform_stats_generations(listed, {"username": "admin", "role": "admin"})
        self.assertEqual(listed.json_payload["total"], 0, "include_history=0 同样不显示")

    def test_history_merge_deduplicates_by_job_id(self):
        """明细里已有的作业，不能再从创作记录补一条（否则同一次生成算两次）。"""
        self.mod.GENERATIONS_PATH.write_text(json.dumps(
            {"date": "2026-10-12", "ts": 5, "username": "alice", "app": "seedance",
             "job_id": "job-live", "tokens": 1000, "amount": 0.023,
             "price_source": "pricing_table"}, ensure_ascii=False) + "\n", encoding="utf-8")
        self.history_rows = [self._history_row(job_id="job-live"),
                             self._history_row(job_id="recovered-2")]
        h = self._handler("/api/platform/stats/generations?start=2026-10-12&end=2026-10-12&include_history=1")
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        ids = sorted(r["job_id"] for r in h.json_payload["rows"])
        self.assertEqual(ids, ["job-live", "recovered-2"], "同一 job_id 只能出现一次")

    def test_history_rows_respect_filters_and_are_marked_unpriced(self):
        self.history_rows = [self._history_row(app="seedance"), self._history_row(job_id="r2", app="nano-banana")]
        h = self._handler("/api/platform/stats/generations?start=2026-10-11&end=2026-10-11"
                          "&app=nano-banana&include_history=1")
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.json_payload["total"], 1)
        self.assertEqual(h.json_payload["rows"][0]["app"], "nano-banana")
        # 它们没有单价 → 计入 unpriced，不能混进金额
        self.assertEqual(h.json_payload["summary"]["amount"], 0)
        self.assertEqual(h.json_payload["summary"]["unpriced_count"], 1)
        self.assertEqual(h.json_payload["summary"]["tokens"], 0)

    # ── 飞书任务：靠代理登记看不见，必须从飞书 Agent 自己的运行记录合并 ─────────
    # 实测 2026-10-12：飞书当天 35 个 run 没有一个经过 Portal 的 HTTP 代理
    # （日志里只有 /api/runs/<id>/decision 这类子操作），所以代理登记恒为 0。
    def _feishu_row(self, job_id="run-1", status="waiting_review", username="alice"):
        return {"date": "2026-10-12", "ts": 9, "time": "2026-10-12 17:10:33",
                "username": username, "app": "feishu-generation-agent", "job_id": job_id,
                "kind": "video", "title": "飞书文档出片需求", "status": status,
                "tokens": 0, "amount": None, "price_source": "untracked", "tracked": False,
                "results": [{"url": f"/api/runs/{job_id}/artifacts/a/content", "kind": "video"}]}

    def test_feishu_runs_are_merged_into_usage_records(self):
        self.feishu_rows = [self._feishu_row()]
        h = self._handler(self._qs())
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        apps = [r["app"] for r in h.json_payload["rows"]]
        self.assertIn("feishu-generation-agent", apps, "飞书任务必须出现在使用记录里")
        self.assertEqual(h.json_payload["total"], 3)
        self.assertEqual(h.json_payload["summary"]["count"], 3)
        feishu = next(r for r in h.json_payload["rows"] if r["app"] == "feishu-generation-agent")
        self.assertEqual(feishu["tokens"], 0, "上游不按 token 计费 → 0，不能编")
        self.assertIsNone(feishu["amount"], "没有单价 → 金额必须留空")
        self.assertEqual(feishu["status"], "waiting_review", "状态要如实透出，不能硬写成成功")
        self.assertAlmostEqual(h.json_payload["summary"]["amount"], 0.9567, places=4,
                               msg="飞书任务不能污染金额合计")

    def test_feishu_merge_deduplicates_and_respects_filters(self):
        """同一 job_id 已有我方记录时不能算两次；页面筛选对飞书行同样生效。"""
        self.feishu_rows = [self._feishu_row(job_id="run-1", username="bob")]
        h = self._handler(self._qs() + "&app=seedance")
        self.mod.Handler._platform_stats_generations(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.json_payload["total"], 2, "app=seedance 时飞书行要被筛掉")
        self.assertTrue(all(r["app"] == "seedance" for r in h.json_payload["rows"]))

        h2 = self._handler(self._qs() + "&status=waiting_review")
        self.mod.Handler._platform_stats_generations(h2, {"username": "admin", "role": "admin"})
        self.assertEqual(h2.json_payload["total"], 1, "按状态筛选要能选中飞书任务")
        self.assertEqual(h2.json_payload["rows"][0]["job_id"], "run-1")

    def test_feishu_rows_skip_runs_without_generation(self):
        """必须**真的生成过**才计数：等审批 / 排队 / 正在跑都不算，没产物的失败也不算。

        用户 2026-10-12 的明确要求：「必须完成一次生成任务后才会有记录」——
        当时列表里出现了一条 waiting_approval（只是规划完、还没开始生成）。
        等确认（waiting_review）意味着成片已经出来了，算完成。
        """
        ts = datetime(2026, 10, 12, 17, 38).timestamp()
        runs = [
            {"job_id": "approval", "status": "waiting_approval", "submitted_at": ts, "results": []},
            {"job_id": "running", "status": "running", "submitted_at": ts, "results": []},
            {"job_id": "review", "status": "waiting_review", "submitted_at": ts,
             "results": [{"url": "/api/runs/review/artifacts/a/content", "kind": "video"}]},
            {"job_id": "failed-empty", "status": "failed", "submitted_at": ts, "results": []},
            {"job_id": "failed-partial", "status": "failed", "submitted_at": ts,
             "results": [{"url": "/api/runs/failed-partial/artifacts/b/content", "kind": "video"}]},
        ]
        handler = FakeHandler("/api/platform/stats/generations")
        with mock.patch.object(self.mod, "_fetch_feishu_history", return_value=runs), \
                mock.patch.object(self.mod.auth, "list_users",
                                  return_value=[{"id": "u1", "username": "alice", "enabled": True}]):
            rows = self.mod.Handler._feishu_generation_rows(
                handler, "2026-10-12", "2026-10-12", True, "admin")
        self.assertEqual([r["job_id"] for r in rows], ["review", "failed-partial"])

    def test_local_models_are_priced_zero_not_unknown(self):
        """本地通道（AI Port / ComfyUI / MiniMax H3）的 API 花费确实是 0 —— 标 0，不留「—」。"""
        local_img = self.mod.quote_generation(model="flux2_klein_allinone", images=2)
        self.assertEqual(local_img["unit_price"], 0)
        self.assertEqual(local_img["amount"], 0)
        self.assertEqual(local_img["source"], "pricing_table")
        local_vid = self.mod.quote_generation(model="minimax_h3_all_reference", tokens=0)
        self.assertEqual(local_vid["amount"], 0)
        # 短剧出片固定走本机 H3，任务里不带模型名 → 按应用兜底
        self.assertEqual(self.mod.quote_generation(model="", app="drama-pipeline")["amount"], 0)
        # 但云代理没有账单来源，仍然必须留空（不能顺手写成 0）
        self.assertIsNone(self.mod.quote_generation(model="banana2-ssvip", images=2)["amount"])
        # 真方舟模型不能被本地族抢走匹配
        ark = self.mod.quote_generation(model="doubao-seedance-2-0-mini", tokens=100_000)
        self.assertEqual(ark["unit_price"], 23.0)
        self.assertAlmostEqual(ark["amount"], 2.3, places=4)

    def test_csv_export_has_usage_and_price_columns(self):
        h = self._handler("/api/platform/stats/generations/export?start=2026-10-12&end=2026-10-12")
        self.mod.Handler._platform_stats_generations_export(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.status, 200)
        text = h.wfile.getvalue().decode("utf-8-sig")
        header = text.splitlines()[0].split(",")
        # 2026-10-13 起导出表头改中文（用户反馈表头是英文看不懂）
        for col in ("用户", "模型", "用量(tokens)", "单价", "金额", "上游任务号", "提示词"):
            self.assertIn(col, header)
        self.assertNotIn("username", header)
        body = dict(zip(header, text.splitlines()[1].split(",")))
        self.assertEqual(body["用户"], "bob")
        self.assertEqual(body["用量(tokens)"], "1000")
        self.assertEqual(body["金额"], "0.023")

    def test_export_cell_collapses_newlines_and_translates(self):
        """导出单元格：换行折成空格 + 状态/类型/有无输入视频走中文口径。"""
        self.assertEqual(self.mod.export_cell("title", "第一行\n第二行   第三行"), "第一行 第二行 第三行")
        self.assertEqual(self.mod.export_cell("title", None), "")
        self.assertEqual(self.mod.export_cell("status", "succeeded"), "成功")
        self.assertEqual(self.mod.export_cell("status", "waiting_review"), "待确认")
        self.assertEqual(self.mod.export_cell("kind", "video"), "视频")
        self.assertEqual(self.mod.export_cell("has_input_video", True), "是")
        self.assertEqual(self.mod.export_cell("has_input_video", None), "")
        self.assertEqual(self.mod.export_cell("task_ids", ["cgt-1", "cgt-2"]), "cgt-1|cgt-2")

    def test_csv_export_default_keeps_all_columns_and_slim_is_opt_in(self):
        """默认必须是全量列（用户：不要减少我的数据）；窄版只能通过 ?slim=1 主动选。"""
        h = self._handler("/api/platform/stats/generations/export?start=2026-10-12&end=2026-10-12")
        self.mod.Handler._platform_stats_generations_export(h, {"username": "admin", "role": "admin"})
        default_cols = h.wfile.getvalue().decode("utf-8-sig").splitlines()[0].split(",")
        for col in ("时间" if False else "生成时间", "来源", "日期", "计费族", "价格来源", "供应商代码",
                    "任务编号", "上游任务号", "提示词"):
            self.assertIn(col, default_cols)
        self.assertGreaterEqual(len(default_cols), 24)
        # 常用列在前、又长又少用的在后（提示词在最后，避免打开就要横向滚）
        self.assertEqual(default_cols[-1], "提示词")

        h2 = self._handler("/api/platform/stats/generations/export?start=2026-10-12&end=2026-10-12&slim=1")
        self.mod.Handler._platform_stats_generations_export(h2, {"username": "admin", "role": "admin"})
        slim = h2.wfile.getvalue().decode("utf-8-sig").splitlines()[0].split(",")
        self.assertLess(len(slim), len(default_cols))
        self.assertNotIn("计费族", slim)
        self.assertIn("来源", slim)          # 窄版也要能看出记录来自哪台机器

    def test_feishu_workflow_status_is_not_used_as_usage_status(self):
        """飞书的「等工作流确认」不是用量语义：waiting_review 必须归一成「成功」。

        2026-10-13 用户反馈：所有飞书任务都被误标成「待确认」（因为直接把飞书的
        run status 当成了用量记录的状态）。
        """
        f = self.mod.feishu_usage_status
        self.assertEqual(f("waiting_review"), "succeeded")
        self.assertEqual(f("done"), "succeeded")
        self.assertEqual(f("failed"), "failed")
        self.assertEqual(f("partial"), "partial")
        # 从未知状态 + 有产物 → 成功（有片子就是生成成功了）
        self.assertEqual(f("weird_state", True), "succeeded")
        # 任何输入都不能吐出「待确认」这类工作流字样
        for raw in ("waiting_review", "done", "failed", "partial", "weird_state"):
            self.assertNotIn("waiting_review", f(raw))

    def test_xlsx_export_is_valid_workbook_with_widths(self):
        """xlsx 导出：合法 zip 结构 + 我们自己定的列宽 + 冻结表头 + 自动筛选 + 数字列是真数字。

        为什么要有 xlsx：CSV 没有列宽信息，WPS/Excel 打开后要么窄列截断、要么一键
        「最适合列宽」把提示词列拉成一条 —— 用户 2026-10-13 反馈的「间距太宽」就是这个。
        """
        h = self._handler("/api/platform/stats/generations/export.xlsx?start=2026-10-12&end=2026-10-12")
        self.mod.Handler._platform_stats_generations_export_xlsx(h, {"username": "admin", "role": "admin"})
        self.assertEqual(h.status, 200)
        raw = h.wfile.getvalue()
        self.assertTrue(raw.startswith(b"PK"), "xlsx 必须是 zip 容器")
        z = zipfile.ZipFile(io.BytesIO(raw))
        for need in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                     "xl/_rels/workbook.xml.rels", "xl/styles.xml", "xl/worksheets/sheet1.xml"):
            self.assertIn(need, z.namelist())
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn('customWidth="1"', sheet)      # 列宽由我们写死
        self.assertIn("autoFilter", sheet)           # 自动筛选
        self.assertIn("frozen", sheet)               # 表头冻结
        self.assertIn("生成时间", sheet)              # 中文表头
        self.assertIn("<v>1000</v>", sheet)          # 用量是真数字，Excel 里能直接求和
        # 提示词列宽度必须是收敛的（60 字），否则又会变成一条拉宽的大列。
        # 列号不写死：列集合会随需求变化（2026-10-13 加了「来源」列），按表头动态定位。
        prompt_idx = self.mod.GENERATION_EXPORT_COLUMNS.index(("title", "提示词")) + 1
        self.assertIn(f'<col min="{prompt_idx}" max="{prompt_idx}" width="60" customWidth="1"/>', sheet)
        # 「来源」列也要有宽度（不然远端记录那一列会挤在一起）
        src_idx = self.mod.GENERATION_EXPORT_COLUMNS.index(("source", "来源")) + 1
        self.assertIn(f'<col min="{src_idx}" max="{src_idx}" width=', sheet)

    def test_csv_export_has_exactly_one_line_per_record(self):
        """一条记录恰好一行：提示词里的换行必须已折成空格，否则 Excel 里会错位。"""
        h = self._handler("/api/platform/stats/generations/export?start=2026-10-12&end=2026-10-12")
        self.mod.Handler._platform_stats_generations_export(h, {"username": "admin", "role": "admin"})
        text = h.wfile.getvalue().decode("utf-8-sig")
        physical = [ln for ln in text.splitlines() if ln.strip()]
        records = list(csv.reader(io.StringIO(text)))
        self.assertGreater(len(records), 1)
        self.assertEqual(len(physical), len(records))
        header = records[0]
        for row in records[1:]:
            self.assertEqual(len(row), len(header))

    def test_csv_export_uses_chinese_status(self):
        h = self._handler("/api/platform/stats/generations/export?start=2026-10-12&end=2026-10-12")
        self.mod.Handler._platform_stats_generations_export(h, {"username": "admin", "role": "admin"})
        text = h.wfile.getvalue().decode("utf-8-sig")
        header, row = text.splitlines()[0].split(","), text.splitlines()[1].split(",")
        body = dict(zip(header, row))
        self.assertIn(body["状态"], ("成功", "失败", "待确认", "待审批", "生成中", "已提交", "排队中"))


if __name__ == "__main__":
    unittest.main()
