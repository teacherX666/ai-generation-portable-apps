# -*- coding: utf-8 -*-
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEEDANCE = ROOT / "seedance"


def load_seedance_isolated(tmp_dir):
    spec = importlib.util.spec_from_file_location(
        "seedance_retry_activity_test", SEEDANCE / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["seedance_retry_activity_test"] = module
    assert spec.loader is not None
    # Isolate every write the app does (state/, outputs/, media/) into tmp_dir,
    # and satisfy the import-time secrets check with a stub key.
    state_dir = Path(tmp_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "secrets.json").write_text(
        json.dumps({"volcengine_api_key": "test-key"}), encoding="utf-8"
    )
    old = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = tmp_dir
    try:
        spec.loader.exec_module(module)
    finally:
        if old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = old
    return module


class RetryActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.mod = load_seedance_isolated(cls.tmp.name)
        # Never actually run generation inside these tests.
        cls.mod.run_job = lambda *a, **k: None

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def make_activity(self, job_id="job-1", status="failed"):
        ws_id = "localhost"
        activity_id = self.mod.uuid.uuid4().hex
        self.mod.record_activity({
            "id": activity_id,
            "job_id": job_id,
            "source": "page",
            "request_kind": "multipart",
            "status": status,
            "title": "retry-test",
            "request": {},
            "response": {"job_id": job_id},
            "workspace_id": ws_id,
            "username": "",
            "started_at": 1.0,
            "finished_at": 2.0,
            "restore": {
                "values": {"prompt": "a test video", "duration": 12},
                "media": {},
            },
        }, ws_id)
        return activity_id

    def test_retry_keeps_single_top_level_activity(self):
        activity_id = self.make_activity()
        before = len(self.mod.read_activity_log())
        new_job_id = self.mod.retry_job("job-1")
        after = self.mod.read_activity_log()

        self.assertEqual(len(after), before,
                         "retry must not create a new top-level activity record")

        root = next(r for r in after if r.get("id") == activity_id)
        self.assertEqual(root["job_id"], new_job_id)
        self.assertEqual(root["retry_of"], "job-1")
        self.assertEqual(root["status"], "running")
        attempts = root.get("attempts") or []
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["job_id"], "job-1")

        listing = self.mod.activity_list(ws_id="localhost")
        self.assertEqual(listing["counts"]["total"], before)
        record = next(r for r in listing["records"] if r["id"] == activity_id)
        self.assertEqual(record["job_id"], new_job_id)
        self.assertEqual(record["attempt_count"], 1)

    def test_retry_accumulates_attempts(self):
        activity_id = self.make_activity("job-1")
        self.mod.retry_job("job-1")
        self.mod.retry_job(self.mod.read_activity_log()[0]["job_id"])

        items = self.mod.read_activity_log()
        roots = [r for r in items if r.get("id") == activity_id]
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(roots[0].get("attempts") or []), 2)
        self.assertEqual(
            [a["job_id"] for a in roots[0]["attempts"]],
            ["job-1", roots[0]["attempts"][1]["job_id"]],
        )

    def test_retry_missing_restore_raises(self):
        self.mod.record_activity({
            "id": self.mod.uuid.uuid4().hex,
            "job_id": "job-norestore",
            "source": "page",
            "status": "failed",
            "title": "no restore",
        }, "localhost")
        with self.assertRaises(ValueError):
            self.mod.retry_job("job-norestore")


if __name__ == "__main__":
    unittest.main()
