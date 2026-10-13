"""重启前「空闲看门人」的判据测试。

为什么值得一条测试：重启 Portal 会带走所有子应用与运行中的任务，而恢复是
「重新入队重跑」。这台机器的历史上，看门人两次误判空闲、打断了同事的任务，根因都是
**判据恒真**：
  - 「近 N 分钟无提交」固定读最后 400 行，浏览器轮询很快把提交行刷出窗口，
    于是「扫不到提交」被当成「无限久没提交」（2026-10-12 真实发生，quiet=1e9）；
  - 「无在跑任务」查了带 ws 过滤的接口，看不到别人工作区里的任务。

所以这里对每条判据都验证它**真的能报忙**，而不只是验证「空闲时报空闲」。
"""

import datetime as dt
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHER = ROOT / "_diag" / "watch_until_idle.py"


def load_watcher(root: Path):
    spec = importlib.util.spec_from_file_location("watch_until_idle_test", WATCHER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["watch_until_idle_test"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.ROOT = root
    module.LOG_DIR = root / "portal" / "state" / "logs"
    module.TICKET_PATH = Path(root.name) / "idle_ticket.json"
    return module


class IdleWatchdogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "portal" / "state" / "logs").mkdir(parents=True)
        self.mod = load_watcher(self.root)
        self.mod.TICKET_PATH = self.root / "idle_ticket.json"
        # 观察起点回溯一小时：否则「无时间戳日志」这条判据天然不满 QUIET 秒
        self.mod._WATCH_STARTED_AT = time.time() - 3600
        self.mod._UNTIMED_SUBMIT_AT = 0.0
        self.mod._TAIL_STATE.clear()

    def _activity(self, app: str, records: list[dict]) -> None:
        path = self.root / app / "state"
        path.mkdir(parents=True, exist_ok=True)
        (path / "activity_log.json").write_text(json.dumps(records, ensure_ascii=False),
                                                encoding="utf-8")

    def _access_log(self, name: str, lines) -> None:
        # 注意：lines 必须是 list —— 传字符串会被 join 按字符拆行，测试会"蒙对"
        assert isinstance(lines, list), "lines 必须是 list"
        (self.root / "portal" / "state" / "logs" / name).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def _stamp(self, seconds_ago: float) -> str:
        when = dt.datetime.now() - dt.timedelta(seconds=seconds_ago)
        return when.strftime("%d/%b/%Y %H:%M:%S")

    @staticmethod
    def _iso(seconds_ago: float) -> str:
        when = dt.datetime.now() - dt.timedelta(seconds=seconds_ago)
        return when.strftime("%Y-%m-%d %H:%M:%S")

    # ── 判据 1：非终态任务 ────────────────────────────────────────────────
    def test_running_job_counts_as_busy(self):
        self._activity("seedance", [{"job_id": "j1", "status": "running",
                                     "created_at": "2026-10-12 13:00:00"}])
        live, _ = self.mod.live_jobs()
        self.assertEqual(live, 1, "在跑的任务必须被数出来")

    def test_terminal_jobs_are_not_busy(self):
        self._activity("seedance", [{"job_id": "j1", "status": "succeeded"},
                                    {"job_id": "j2", "status": "failed"},
                                    {"job_id": "j3", "status": "cancelled"},
                                    {"job_id": "j4", "status": "interrupted"}])
        self.assertEqual(self.mod.live_jobs()[0], 0)

    def test_pending_and_queued_also_count(self):
        self._activity("nano-banana", [{"job_id": "a", "status": "pending"},
                                       {"job_id": "b", "status": "queued"},
                                       {"job_id": "c", "status": "processing"}])
        self.assertEqual(self.mod.live_jobs()[0], 3)

    # ── 判据 2：有时间戳日志里的任务 POST ────────────────────────────────
    def test_recent_submission_is_busy(self):
        self._access_log("seedance.log", [
            '127.0.0.1 - - [%s] "GET /api/jobs HTTP/1.1" 200 -' % self._stamp(600),
            '127.0.0.1 - - [%s] "POST /api/jobs HTTP/1.1" 200 -' % self._stamp(10),
        ])
        ts, detail, timed = self.mod._scan_last_job_post(
            self.root / "portal" / "state" / "logs" / "seedance.log")
        self.assertTrue(timed)
        self.assertLess(dt.datetime.now().timestamp() - ts, 120)
        self.assertIn("/api/jobs", detail)

    def test_get_requests_are_not_submissions(self):
        self._access_log("nano-banana.log", [
            '127.0.0.1 - - [%s] "GET /api/jobs?ws=x HTTP/1.1" 200 -' % self._stamp(5),
            '127.0.0.1 - - [%s] "POST /api/jobs HTTP/1.1" 200 -' % self._stamp(900),
        ])
        ts, _, _ = self.mod._scan_last_job_post(
            self.root / "portal" / "state" / "logs" / "nano-banana.log")
        self.assertGreater(dt.datetime.now().timestamp() - ts, 180)

    def test_cancel_does_not_count_as_submission(self):
        self._access_log("seedance.log", [
            '127.0.0.1 - - [%s] "POST /api/jobs/abc/cancel HTTP/1.1" 200 -' % self._stamp(5),
            '127.0.0.1 - - [%s] "POST /api/jobs HTTP/1.1" 200 -' % self._stamp(900),
        ])
        ts, _, _ = self.mod._scan_last_job_post(
            self.root / "portal" / "state" / "logs" / "seedance.log")
        self.assertGreater(dt.datetime.now().timestamp() - ts, 180)

    def test_other_creation_endpoints_are_recognised(self):
        for path in ("/api/text2image", "/api/virtual/jobs", "/api/v1/jobs?ws=w"):
            with self.subTest(path=path):
                self._access_log("x.log", ['127.0.0.1 - - [%s] "POST %s HTTP/1.1" 200 -'
                                           % (self._stamp(5), path)])
                ts, _, _ = self.mod._scan_last_job_post(
                    self.root / "portal" / "state" / "logs" / "x.log")
                self.assertLess(dt.datetime.now().timestamp() - ts, 120)

    def test_sub_actions_count_as_activity(self):
        """子操作（刷新分镜等）也算「有人在动」—— 保守口径只推迟重启，不放行危险窗口。"""
        self.assertIsNotNone(self.mod._is_job_post(
            '[drama-pipeline] "POST /api/jobs/20261008-x/shots/refresh HTTP/1.1" 200 -'))

    def test_timestamp_shaped_but_invalid_fails_closed(self):
        """有时间戳形状却解析不了 → 当「刚刚提交」，绝不静默跳过。"""
        self._access_log("seedance.log",
                         ['127.0.0.1 - - [32/Foo/2026 99:99:99] "POST /api/jobs HTTP/1.1" 200 -'])
        ts, _, timed = self.mod._scan_last_job_post(
            self.root / "portal" / "state" / "logs" / "seedance.log")
        self.assertTrue(timed)
        self.assertLess(dt.datetime.now().timestamp() - ts, 5)

    # ── 真实踩过的坑：固定行数窗口会让判据「真空成立」 ─────────────────────
    def test_submission_survives_thousands_of_polling_lines(self):
        """提交行被浏览器轮询刷到 2000 行之前 —— 仍然必须找得到。"""
        self._access_log("seedance.log", [
            '127.0.0.1 - - [%s] "POST /api/jobs HTTP/1.1" 200 -' % self._stamp(300),
        ] + [
            '127.0.0.1 - - [%s] "GET /api/jobs?ws=x HTTP/1.1" 200 -' % self._stamp(299 - i * 0.01)
            for i in range(2000)
        ])
        ts, _, _ = self.mod._scan_last_job_post(
            self.root / "portal" / "state" / "logs" / "seedance.log")
        self.assertGreater(ts, 0, "2000 行轮询不该让提交行消失")
        quiet = dt.datetime.now().timestamp() - ts
        self.assertGreater(quiet, 250)
        self.assertLess(quiet, 100000, "不能退化成「无限久没提交」")

    def test_submission_survives_large_log_tail(self):
        """尾部超过第一个扫描窗口（256KB）时，靠扩大窗口仍要扫得到。"""
        filler = ('127.0.0.1 - - [%s] "GET /api/jobs?ws=zz HTTP/1.1" 200 -' % self._stamp(100))
        self._access_log("nano-banana.log",
                         ['127.0.0.1 - - [%s] "POST /api/jobs HTTP/1.1" 200 -' % self._stamp(400)]
                         + [filler] * 12000)
        ts, _, _ = self.mod._scan_last_job_post(
            self.root / "portal" / "state" / "logs" / "nano-banana.log")
        self.assertGreater(ts, 0, "扩大窗口后必须仍能找到提交")

    # ── 判据 4：无时间戳日志用字节增量 ───────────────────────────────────
    def test_untimed_log_is_classified_untimed(self):
        self._access_log("drama-pipeline.log",
                         ['[drama-pipeline] "POST /api/jobs HTTP/1.1" 200 -'])
        ts, _, timed = self.mod._scan_last_job_post(
            self.root / "portal" / "state" / "logs" / "drama-pipeline.log")
        self.assertFalse(timed, "行内没有时间戳 → 应判定为无时间戳日志")

    def test_new_untimed_job_post_blocks_idle(self):
        """基线建立后新增的任务 POST 必须被字节增量发现（不依赖时间戳）。"""
        log = self.root / "portal" / "state" / "logs" / "drama-pipeline.log"
        log.write_text('[drama-pipeline] "GET / HTTP/1.1" 200 -\n', encoding="utf-8")
        self.mod.scan_untimed_growth()  # 建立基线
        with log.open("a", encoding="utf-8") as fh:
            fh.write('[drama-pipeline] "POST /api/jobs HTTP/1.1" 200 -\n')
        ts, detail = self.mod.scan_untimed_growth()
        self.assertLess(time.time() - ts, 5, "新增的提交必须立刻把判据打成「忙」")
        self.assertIn("/api/jobs", detail)

    def test_untimed_growth_without_job_post_stays_quiet(self):
        log = self.root / "portal" / "state" / "logs" / "drama-pipeline.log"
        log.write_text('[drama-pipeline] "GET / HTTP/1.1" 200 -\n', encoding="utf-8")
        self.mod.scan_untimed_growth()
        with log.open("a", encoding="utf-8") as fh:
            fh.write('[drama-pipeline] "GET /api/jobs HTTP/1.1" 200 -\n')
        ts, _ = self.mod.scan_untimed_growth()
        self.assertGreater(time.time() - ts, 3000, "普通读取不该推迟静默期")

    def test_watch_start_floor_prevents_instant_idle(self):
        """观察不满 QUIET 秒时，无时间戳这条判据不能放行。"""
        self.mod._WATCH_STARTED_AT = time.time() - 10
        ts, _ = self.mod.scan_untimed_growth()
        self.assertLess(time.time() - ts, 60)

    # ── 组合判据 idle_status ────────────────────────────────────────────
    def test_idle_requires_no_live_and_quiet(self):
        self._activity("seedance", [{"status": "running", "created_at": self._iso(0)}])
        self.assertFalse(self.mod.idle_status()["idle"], "有任务在跑就不是空闲")

    def test_recent_activity_alone_blocks_idle(self):
        self._activity("seedance", [{"status": "succeeded", "updated_at": self._iso(5)}])
        status = self.mod.idle_status()
        self.assertFalse(status["idle"], "刚有任务活动就不能算空闲")
        self.assertLess(status["quiet"], 60)

    def test_fully_quiet_reports_idle(self):
        self._activity("seedance", [{"status": "succeeded", "updated_at": self._iso(1800)}])
        self._access_log("seedance.log",
                         ['127.0.0.1 - - [%s] "POST /api/jobs HTTP/1.1" 200 -' % self._stamp(1800)])
        self.assertTrue(self.mod.idle_status()["idle"])

    def test_quiet_is_the_most_conservative_of_all_signals(self):
        """quiet 取各信号里最小的，不能被宽的那个放行。"""
        self._activity("seedance", [{"status": "succeeded", "updated_at": self._iso(5)}])
        self._access_log("seedance.log",
                         ['127.0.0.1 - - [%s] "POST /api/jobs HTTP/1.1" 200 -' % self._stamp(1800)])
        status = self.mod.idle_status()
        self.assertGreater(status["quiet_submit"], 1700)
        self.assertLess(status["quiet_activity"], 60)
        self.assertLess(status["quiet"], 60)

    # ── 门票 ───────────────────────────────────────────────────────────
    def test_ticket_carries_expiry(self):
        status = {"checked_at": 1000.0, "quiet": 500.0, "live": 0,
                  "quiet_untimed": 900.0, "quiet_submit": 500.0, "quiet_activity": 900.0}
        self.mod.write_ticket(status)
        ticket = json.loads(self.mod.TICKET_PATH.read_text(encoding="utf-8"))
        self.assertEqual(ticket["expires_at"], 1000.0 + self.mod.TICKET_TTL_SECONDS)
        self.assertEqual(ticket["live"], 0)


if __name__ == "__main__":
    unittest.main()
