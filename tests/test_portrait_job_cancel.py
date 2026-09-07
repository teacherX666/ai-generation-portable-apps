"""Tests for handle_job_cancel (stdlib Handler 与 fastapi 桥接共用的取消入口).

覆盖：不存在 404 / 排队中取消 200 / 幂等 / 终态 409；
并断言所有响应都不带 X-Job-Id（统计红线：取消不触发计数）。
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_portrait_app():
    mod_path = ROOT / "volcengine-portrait" / "app.py"
    spec = importlib.util.spec_from_file_location("portrait_app_for_cancel_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["portrait_app_for_cancel_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeWriter:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b


class _FakeHandler:
    def __init__(self):
        self.headers = {}
        self.wfile = _FakeWriter()
        self.status_code = None
        self.sent_headers = {}
        self.path = "/api/virtual/jobs/x/cancel"

    def send_response(self, code):
        self.status_code = code

    def send_header(self, k, v):
        self.sent_headers[k] = v

    def end_headers(self):
        pass


class JobCancelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_portrait_app()
        # 隔离副作用：不写 backlog 文件、不回调 portal
        cls.mod._backlog_remove_locked = lambda jid: None
        cls.mod.report_final_to_portal = lambda jid, st: None

    def setUp(self):
        with self.mod.JOBS_LOCK:
            self.mod.JOBS.clear()

    def _cancel(self, job_id):
        handler = _FakeHandler()
        self.mod.handle_job_cancel(handler, job_id)
        return handler

    def test_missing_job_returns_404(self):
        h = self._cancel("nope")
        self.assertEqual(h.status_code, 404)
        self.assertIn("任务不存在", json.loads(h.wfile.buf)["error"])
        self.assertNotIn("X-Job-Id", h.sent_headers)

    def test_queued_job_cancels(self):
        with self.mod.JOBS_LOCK:
            self.mod.JOBS["job1"] = {"job_id": "job1", "status": "queued",
                                     "errors": [], "events": []}
        h = self._cancel("job1")
        self.assertEqual(h.status_code, 200)
        body = json.loads(h.wfile.buf)
        self.assertEqual(body["status"], "cancelled")
        with self.mod.JOBS_LOCK:
            job = self.mod.JOBS["job1"]
        self.assertTrue(job["cancel_requested"])
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["finished_at"])
        self.assertNotIn("X-Job-Id", h.sent_headers)

    def test_already_cancelled_is_idempotent(self):
        with self.mod.JOBS_LOCK:
            self.mod.JOBS["job2"] = {"job_id": "job2", "status": "cancelled",
                                     "cancel_requested": True,
                                     "errors": ["任务已取消。"], "events": []}
        h = self._cancel("job2")
        self.assertEqual(h.status_code, 200)

    def test_terminal_job_rejected(self):
        with self.mod.JOBS_LOCK:
            self.mod.JOBS["job3"] = {"job_id": "job3", "status": "succeeded",
                                     "errors": [], "events": []}
        h = self._cancel("job3")
        self.assertEqual(h.status_code, 409)
        self.assertNotIn("X-Job-Id", h.sent_headers)


if __name__ == "__main__":
    unittest.main()
