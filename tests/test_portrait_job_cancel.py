"""Tests for handle_job_cancel / handle_job_retry（stdlib Handler 与 fastapi 桥接共用的取消/重试入口）.

取消覆盖：不存在 404 / 排队中取消 200 / 幂等 / 终态 409；
并断言所有响应都不带 X-Job-Id（统计红线：取消不触发计数）。
重试覆盖：失败任务重提 201 / 新任务带 X-Job-Id（统计红线：重试按新任务计费一次）/
参数丢失 400。
"""
import importlib.util
import json
import sys
import types
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
        cls.mod._backlog_set_locked = lambda jid, **fields: None
        cls.mod.record_activity = lambda item: None
        cls.mod._executor = types.SimpleNamespace(submit=lambda fn, *a, **k: None)
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


class JobRetryTests(unittest.TestCase):
    """handle_job_retry：失败任务原样重提 → 新 job_id，且统计按新任务登记。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_portrait_app()
        cls.mod._backlog_remove_locked = lambda jid: None
        cls.mod._backlog_set_locked = lambda jid, **fields: None
        cls.mod.record_activity = lambda item: None
        cls.mod._executor = types.SimpleNamespace(submit=lambda fn, *a, **k: None)
        cls.mod.report_final_to_portal = lambda jid, st: None

    def setUp(self):
        with self.mod.JOBS_LOCK:
            self.mod.JOBS.clear()

    def _retry(self, job_id):
        handler = _FakeHandler()
        self.mod.handle_job_retry(handler, job_id)
        return handler

    def _failed_job(self, jid="job1"):
        return {
            "job_id": jid, "task_type": "virtual", "status": "failed",
            "total": 1, "done": 1, "results": [], "errors": ["Run 0: 内容审核未通过"],
            "events": [], "username": "u1", "asset_id": "asset-1",
            "extra_asset_ids": [], "prompt": "测试提示词",
            "model": "doubao-seedance-2-0-260128", "duration": 12,
            "requested_duration": 12, "resolution": "720p", "ratio": "16:9",
            "api_key": "sk-should-not-copy", "output_dir": "",
            "extra_image_urls": [], "provider": "",
        }

    def test_failed_job_retries_with_new_id(self):
        with self.mod.JOBS_LOCK:
            self.mod.JOBS["job1"] = self._failed_job()
        h = self._retry("job1")
        self.assertEqual(h.status_code, 201)
        body = json.loads(h.wfile.buf)
        self.assertTrue(body["ok"])
        new_id = body["job_id"]
        self.assertNotEqual(new_id, "job1")
        # 统计红线：重试是新任务，必须带 X-Job-Id 让 portal 登记一次
        self.assertEqual(h.sent_headers.get("X-Job-Id"), new_id)
        with self.mod.JOBS_LOCK:
            job = self.mod.JOBS[new_id]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["prompt"], "测试提示词")
        self.assertEqual(job["asset_id"], "asset-1")
        # 用户 key 不落盘到新任务
        self.assertIsNone(job.get("api_key"))
        # 旧任务仍在（历史可查）
        with self.mod.JOBS_LOCK:
            self.assertIn("job1", self.mod.JOBS)

    def test_missing_job_returns_400(self):
        h = self._retry("nope")
        self.assertEqual(h.status_code, 400)
        self.assertIn("无法找回", json.loads(h.wfile.buf)["error"])
        self.assertNotIn("X-Job-Id", h.sent_headers)


if __name__ == "__main__":
    unittest.main()
