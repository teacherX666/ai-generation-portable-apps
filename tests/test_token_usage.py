"""Token 消耗统计（火山方舟按 token 计费）贯穿链路的回归测试。

链路（2026-10-12 实现，方案 A）：
  子应用在解析上游响应时把 usage 累加到 job["tokens"]
    → job JSON 带出 tokens
    → Portal 轮询终态时读 tokens 并写入 by_user[日期][用户][应用]["tokens"]
    → /api/platform/stats/range|export 返回；管理员看全部、普通用户只看自己

这里的重点是**Portal 侧**：poller 真的驱动一次，验证归属与累加；以及聚合/导出
是否把 tokens 带出去。子应用侧只做「字段没被剥掉」+「接线还在」的守卫——
run_one() 会真打方舟并轮询，端到端不可测（与 test_seedance_task_error_surfaced.py
的判断一致）。

口径边界（有意为之，测试里固定住）：
- 只有走火山方舟通道的任务产生 token；Chiyun/t8star/本地 ComfyUI/Dreamina 恒 0。
- 与 images/seconds 同口径：done == 0（没有产出）时一个字段都不写。
"""

import importlib.util
import io
import json
import re
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.portal_isolation import load_isolated

ROOT = Path(__file__).resolve().parents[1]
PORTAL_APP = ROOT / "portal" / "app.py"


def load_portal_module():
    # 必须隔离数据目录：这个文件会驱动真实轮询循环，而轮询终态除写入 by_user 统计外
    # 还会写「按次生成明细」（模块级 GENERATIONS_PATH）。不隔离就会把测试夹具写进
    # 生产 state —— 2026-10-12 真实发生过（生产 generations.jsonl 里出现 alice/bob）。
    return load_isolated("portal_app_token_usage", PORTAL_APP)


def load_app(name: str, relative: str):
    """按路径加载子应用模块；缺本机前置（secrets.json 等）时跳过。

    只吞「缺前置」这一类异常：`NameError`/`SyntaxError`/`AttributeError` 这类**编程错误
    必须让测试红**。宽泛的 except 会把 NameError 变成 skip —— 2026-10-12 真踩过：
    少写一个 import，三个子应用快照测试就静默跳过了，看起来还是 OK。
    """
    path = ROOT / relative
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    except (RuntimeError, ImportError, OSError) as exc:
        raise unittest.SkipTest(f"{relative} 缺本机前置，跳过：{type(exc).__name__}: {exc}")


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


class TokenPollingTests(unittest.TestCase):
    """驱动真实的轮询循环体，验证 token 落到 by_user 的正确的人、正确的应用上。"""

    @classmethod
    def setUpClass(cls):
        cls.module = load_portal_module()

    def _tracker(self):
        tracker = self.module.UsageTracker.__new__(self.module.UsageTracker)
        tracker._lock = threading.Lock()
        tracker._data = self.module.UsageTracker._empty_data()
        tracker._pending_jobs = []
        tracker._dirty = False
        tracker._flush_wake = threading.Event()
        tracker._save = lambda: None
        return tracker

    def _drain_poll_once(self, tracker, payload: dict, app: str = "seedance"):
        conn = FakeConnection(payload)
        sleeps = {"n": 0}

        def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] > 1:
                raise StopIteration

        fake_apps = {app: {"port": 18888}}
        with mock.patch.object(self.module.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(self.module.http.client, "HTTPConnection", return_value=conn), \
             mock.patch.object(self.module, "APPS", fake_apps):
            with self.assertRaises(StopIteration):
                tracker._job_poll_loop()

    def _only_day(self, section: dict) -> dict:
        self.assertEqual(len(section), 1, f"expected exactly one date bucket, got {section!r}")
        return next(iter(section.values()))

    def _poll(self, tracker, payload, app="seedance", username="alice",
              job_id="job-1", job_type="video", duration_per_item=5):
        tracker.register_job(app, job_id, username, job_type=job_type,
                             duration_per_item=duration_per_item)
        tracker.inc_daily_jobs(app)
        self._drain_poll_once(tracker, payload, app=app)

    def test_video_job_records_completion_tokens(self):
        """真机实测：15s/720p 的 Seedance 任务 usage.completion_tokens = 540900。"""
        tracker = self._tracker()
        self._poll(tracker, {"status": "succeeded", "done": 1, "total": 1,
                             "duration": 15, "tokens": 540900})
        day = self._only_day(tracker._data["by_user"])
        self.assertEqual(day["alice"]["seedance"]["tokens"], 540900)
        self.assertEqual(day["alice"]["seedance"]["seconds"], 15, "原有秒数不能被打断")

    def test_image_job_records_total_tokens(self):
        """方舟图片：usage.total_tokens（一次请求生成 N 张，token 覆盖整次请求）。"""
        tracker = self._tracker()
        self._poll(tracker, {"status": "succeeded", "done": 2, "total": 2, "tokens": 23107},
                   app="nano-banana", job_type="image", duration_per_item=0)
        day = self._only_day(tracker._data["by_user"])
        self.assertEqual(day["alice"]["nano-banana"]["tokens"], 23107)
        self.assertEqual(day["alice"]["nano-banana"]["images"], 2)

    def test_tokens_accumulate_across_jobs(self):
        tracker = self._tracker()
        self._poll(tracker, {"status": "succeeded", "done": 1, "total": 1, "tokens": 100},
                   job_id="job-a")
        self._poll(tracker, {"status": "succeeded", "done": 1, "total": 1, "tokens": 250},
                   job_id="job-b")
        day = self._only_day(tracker._data["by_user"])
        self.assertEqual(day["alice"]["seedance"]["tokens"], 350)

    def test_tokens_are_attributed_per_user(self):
        tracker = self._tracker()
        self._poll(tracker, {"status": "succeeded", "done": 1, "total": 1, "tokens": 100},
                   username="alice", job_id="job-a")
        self._poll(tracker, {"status": "succeeded", "done": 1, "total": 1, "tokens": 700},
                   username="bob", job_id="job-b")
        day = self._only_day(tracker._data["by_user"])
        self.assertEqual(day["alice"]["seedance"]["tokens"], 100)
        self.assertEqual(day["bob"]["seedance"]["tokens"], 700)

    def test_non_ark_provider_records_zero_tokens(self):
        """Chiyun(gemini)/t8star/本地通道响应里没有 usage → 不写 token，但图照记。"""
        tracker = self._tracker()
        self._poll(tracker, {"status": "succeeded", "done": 3, "total": 3},
                   app="nano-banana", job_type="image", duration_per_item=0)
        day = self._only_day(tracker._data["by_user"])
        self.assertEqual(day["alice"]["nano-banana"]["images"], 3)
        self.assertEqual(day["alice"]["nano-banana"].get("tokens", 0), 0)

    def test_nested_job_payload_tokens_are_read(self):
        """dreamina 之类把字段嵌在 "job" 里；nested 也要能取到 token。"""
        tracker = self._tracker()
        self._poll(tracker, {"job": {"status": "completed", "done": 1, "tokens": 999},
                             "status": "completed", "done": 1},
                   app="dreamina", job_type="image", duration_per_item=0)
        day = self._only_day(tracker._data["by_user"])
        self.assertEqual(day["alice"]["dreamina"]["tokens"], 999)

    def test_no_output_writes_no_token_row(self):
        """纯失败（done=0）即使带上 tokens 也不落账 —— 与 images/seconds 同口径。"""
        tracker = self._tracker()
        self._poll(tracker, {"status": "failed", "done": 0, "total": 2, "tokens": 123456})
        self.assertEqual(tracker._data.get("by_user"), {})

    def test_legacy_entries_without_tokens_are_tolerated(self):
        """老 usage.json 里的 {images, seconds} 没有 tokens 键，必须按 0 兼容。

        轮询写的是「今天」这个桶，所以断言要看今天的桶；同时确认老桶没被改写。
        """
        tracker = self._tracker()
        legacy_day = {"alice": {"seedance": {"images": 0, "seconds": 12}}}
        tracker._data["by_user"] = {"2026-10-01": json.loads(json.dumps(legacy_day))}
        self._poll(tracker, {"status": "succeeded", "done": 1, "total": 1, "tokens": 5},
                   job_id="job-new")
        today = self.module.time.strftime("%Y-%m-%d")
        self.assertNotEqual(today, "2026-10-01", "本测试假设今天不是 2026-10-01")
        self.assertEqual(tracker._data["by_user"][today]["alice"]["seedance"]["tokens"], 5)
        self.assertEqual(tracker._data["by_user"][today]["alice"]["seedance"]["seconds"], 5)
        self.assertEqual(tracker._data["by_user"]["2026-10-01"], legacy_day,
                         "老数据桶不能被改写")


class TokenAggregationTests(unittest.TestCase):
    """区间/导出必须把 tokens 带出去，并且继承现有的可见性分流。"""

    @classmethod
    def setUpClass(cls):
        cls.module = load_portal_module()

    def _tracker(self):
        tracker = self.module.UsageTracker.__new__(self.module.UsageTracker)
        tracker._lock = threading.Lock()
        tracker._data = self.module.UsageTracker._empty_data()
        tracker._pending_jobs = []
        tracker._dirty = False
        tracker._flush_wake = threading.Event()
        tracker._save = lambda: None
        return tracker

    def _seed(self, tracker):
        tracker._data["by_user"] = {
            "2026-10-12": {
                "alice": {"seedance": {"images": 0, "seconds": 30, "tokens": 540900}},
                "bob": {"seedance": {"images": 0, "seconds": 12, "tokens": 260100}},
            }
        }
        return tracker

    def test_history_exposes_token_series(self):
        tracker = self._seed(self._tracker())
        out = tracker.get_history(days=3, username="alice", role="admin")
        self.assertIn("tokens", out["users"]["alice"]["seedance"])
        self.assertEqual(sum(out["users"]["alice"]["seedance"]["tokens"]), 540900)

    def test_history_zero_fills_tokens(self):
        tracker = self._seed(self._tracker())
        out = tracker.get_history(days=3, username="alice", role="admin")
        self.assertEqual(len(out["users"]["alice"]["seedance"]["tokens"]), 3)
        self.assertEqual(out["users"]["alice"]["seedance"]["tokens"].count(0), 2)

    def test_range_exposes_token_series(self):
        tracker = self._seed(self._tracker())
        out = tracker.get_range("2026-10-12", "2026-10-12", username="alice", role="admin")
        self.assertEqual(out["users"]["bob"]["seedance"]["tokens"], [260100])

    def test_non_admin_range_only_sees_self_tokens(self):
        tracker = self._seed(self._tracker())
        out = tracker.get_range("2026-10-12", "2026-10-12", username="alice", role="user")
        self.assertEqual(sorted(out["users"]), ["alice"])
        self.assertEqual(out["users"]["alice"]["seedance"]["tokens"], [540900])

    def test_export_range_has_per_app_and_grand_token_totals(self):
        tracker = self._seed(self._tracker())
        rows = tracker.export_range("2026-10-12", "2026-10-12", ["seedance", "nano-banana"],
                                    username="admin", role="admin")
        by_user = {r["username"]: r for r in rows}
        self.assertEqual(by_user["alice"]["seedance_tokens"], 540900)
        self.assertEqual(by_user["alice"]["total_tokens"], 540900)
        self.assertEqual(by_user["bob"]["total_tokens"], 260100)

    def test_export_range_non_admin_sees_only_self(self):
        tracker = self._seed(self._tracker())
        rows = tracker.export_range("2026-10-12", "2026-10-12", ["seedance"],
                                    username="bob", role="user")
        self.assertEqual([r["username"] for r in rows], ["bob"])

    def test_legacy_rows_without_tokens_export_zero(self):
        tracker = self._tracker()
        tracker._data["by_user"] = {
            "2026-10-01": {"alice": {"seedance": {"images": 0, "seconds": 9}}}
        }
        rows = tracker.export_range("2026-10-01", "2026-10-01", ["seedance"],
                                    username="admin", role="admin")
        self.assertEqual(rows[0]["seedance_tokens"], 0)
        self.assertEqual(rows[0]["total_tokens"], 0)


class FakeCsvHandler:
    """只提供 _platform_stats_export 需要的那几件东西。"""

    def __init__(self, path: str):
        self.path = path
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, str(value)))

    def end_headers(self):
        return None

    def _cors_headers(self):
        return None

    def _json(self, code, payload):
        self.status = code
        self.json_payload = payload


class CsvExportTests(unittest.TestCase):
    """CSV 是交给 Excel 的产物，表头是手写的 —— 单独钉住 tokens 列。"""

    @classmethod
    def setUpClass(cls):
        cls.module = load_portal_module()

    def _csv(self, role: str = "admin", username: str = "admin") -> str:
        tracker = self.module.UsageTracker.__new__(self.module.UsageTracker)
        tracker._lock = threading.Lock()
        tracker._data = self.module.UsageTracker._empty_data()
        tracker._data["by_user"] = {
            "2026-10-12": {"alice": {"seedance": {"images": 0, "seconds": 30, "tokens": 540900}}}
        }
        handler = FakeCsvHandler("/api/platform/stats/export?start=2026-10-12&end=2026-10-12")
        # 复用真实 Handler 的参数解析，避免在假对象里复制一份日期校验逻辑。
        handler._parse_range_qs = lambda: self.module.Handler._parse_range_qs(handler)
        with mock.patch.object(self.module, "tracker", tracker):
            self.module.Handler._platform_stats_export(handler, {"username": username, "role": role})
        self.assertEqual(handler.status, 200)
        return handler.wfile.getvalue().decode("utf-8-sig")

    def test_header_contains_token_columns(self):
        text = self._csv()
        header = text.splitlines()[0]
        self.assertIn("seedance_tokens", header)
        self.assertIn("total_tokens", header)

    def test_row_carries_token_value(self):
        text = self._csv()
        lines = text.splitlines()
        header = lines[0].split(",")
        row = lines[1].split(",")
        cells = dict(zip(header, row))
        self.assertEqual(cells["username"], "alice")
        self.assertEqual(cells["seedance_tokens"], "540900")
        self.assertEqual(cells["total_tokens"], "540900")


class SubAppSnapshotTests(unittest.TestCase):
    """子应用的 job 序列化必须把 tokens 透出去，否则 Portal 永远读到 0。"""

    def test_seedance_snapshot_keeps_tokens(self):
        mod = load_app("seedance_token_snapshot", "seedance/app.py")
        snap = mod.public_job_snapshot({"id": "j", "status": "succeeded", "tokens": 540900,
                                        "api_key": "ark-secret"})
        self.assertEqual(snap["tokens"], 540900)
        self.assertNotIn("api_key", snap)

    def test_nano_snapshot_keeps_tokens(self):
        mod = load_app("nano_token_snapshot", "nano-banana/app.py")
        snap = mod.public_job_snapshot({"id": "j", "status": "succeeded", "tokens": 23107})
        self.assertEqual(snap["tokens"], 23107)

    def test_portrait_public_keeps_tokens(self):
        mod = load_app("portrait_token_public", "volcengine-portrait/app.py")
        pub = mod._public({"job_id": "j", "tokens": 123456, "api_key": "ark-secret"})
        self.assertEqual(pub["tokens"], 123456)
        self.assertNotIn("api_key", pub)


class SubAppWiringTests(unittest.TestCase):
    """接线守卫：三个读方舟响应的子应用都得真的调用 extract_tokens。

    这是防「静默归零」的——上游解析被改掉之后，功能全正常、token 永远是 0，
    没有断言就会一直没人发现。run_one() 无法端到端跑，所以在这里钉住接线。
    """

    CASES = [
        ("seedance", "seedance/app.py"),
        ("nano-banana", "nano-banana/app.py"),
        ("volcengine-portrait", "volcengine-portrait/app.py"),
    ]

    def test_each_ark_reader_calls_extract_tokens(self):
        for label, relative in self.CASES:
            with self.subTest(app=label):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("from shared.ark_usage import extract_tokens", source,
                              f"{label} 必须共用统一口径的 extract_tokens")
                calls = re.findall(r"extract_tokens\(", source)
                self.assertGreaterEqual(len(calls), 1,
                                        f"{label} 必须在成功分支真的调用 extract_tokens")

    def test_each_ark_reader_accumulates_into_job_tokens(self):
        """累加而不是覆盖 —— 并发/多跑时每个 run 是独立的一次方舟任务。"""
        for label, relative in self.CASES:
            with self.subTest(app=label):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn('"tokens"', source, f"{label} 的 job 里应有 tokens 字段")


if __name__ == "__main__":
    unittest.main()
