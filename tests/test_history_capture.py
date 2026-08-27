import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location(
    "portal_app_capture", ROOT / "portal" / "app.py"
)
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_capture"] = portal
_spec.loader.exec_module(portal)


def _make_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(portal, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(portal, "HISTORY_PATH", tmp_path / "history.json")
    return portal.UsageTracker()


def test_register_job_writes_pending_history(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    tracker.register_job("nano-banana", "j1", "alice", "image",
                         metadata={"prompt": "一只猫", "model": "m1",
                                   "params": {"aspect_ratio": "1:1"}})
    rec = tracker.history_records()["nano-banana:j1"]
    assert rec["status"] == "pending"
    assert rec["prompt"] == "一只猫"
    assert rec["kind"] == "image"
    assert rec["model"] == "m1"


def test_register_job_video_kind(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    tracker.register_job("seedance", "j2", "alice", "video")
    rec = tracker.history_records()["seedance:j2"]
    assert rec["kind"] == "video"


def test_register_job_keeps_old_signature_working(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    tracker.register_job("seedance", "j3", "alice", "video")  # 无 metadata 也能跑
    assert tracker.history_records()["seedance:j3"]["status"] == "pending"


def test_normalize_status():
    assert portal.normalize_history_status("succeeded") == "done"
    assert portal.normalize_history_status("FAILED") == "failed"
    assert portal.normalize_history_status("processing") == "running"
    assert portal.normalize_history_status("queued") == "queued"
    assert portal.normalize_history_status("") == "queued"


def test_result_items_extraction():
    data = {"results": [{"url": "/outputs/a.png"}, {"download_url": "/outputs/b.mp4"}]}
    items = portal.history_result_items(data, "image")
    assert items == [{"url": "/outputs/a.png", "kind": "image"},
                     {"url": "/outputs/b.mp4", "kind": "image"}]
    nested = {"job": {"results": [{"url": "/outputs/c.mp4"}]}}
    assert portal.history_result_items(nested, "video") == [
        {"url": "/outputs/c.mp4", "kind": "video"}]


def test_history_update_terminal_sets_thumb_and_error(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    tracker.history_upsert({"app": "seedance", "job_id": "j1", "username": "u",
                            "kind": "video", "prompt": "p", "model": "m",
                            "params": {}, "status": "pending",
                            "submitted_at": time.time(), "completed_at": None,
                            "duration": 0, "thumb_url": "", "results": [],
                            "error": ""})
    data = {"status": "succeeded", "done": 1, "duration": 17,
            "results": [{"download_url": "/api/download/tok"}],
            "errors": [{"message": "boom"}]}
    tracker.history_update_terminal("seedance", "j1", "succeeded", data, "video")
    rec = tracker.history_records()["seedance:j1"]
    assert rec["status"] == "done"
    assert rec["thumb_url"] == "/api/download/tok"
    assert rec["results"] == [{"url": "/api/download/tok", "kind": "video"}]
    assert rec["duration"] == 17


def test_history_update_terminal_empty_results_clears_thumb(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    tracker.history_upsert({"app": "seedance", "job_id": "j2", "username": "u",
                            "kind": "video", "prompt": "p", "model": "m",
                            "params": {}, "status": "pending",
                            "submitted_at": time.time(), "completed_at": None,
                            "duration": 0, "thumb_url": "", "results": [],
                            "error": ""})
    tracker.history_update_terminal("seedance", "j2", "failed",
                                    {"status": "failed", "done": 0,
                                     "error": "上游炸了"}, "video")
    rec = tracker.history_records()["seedance:j2"]
    assert rec["status"] == "failed"
    assert rec["thumb_url"] == ""
    assert rec["error"] == "上游炸了"


def test_history_write_failure_never_breaks_stats(tmp_path, monkeypatch):
    """历史落库失败（如路径不可写）时，统计登记照常成功。"""
    tracker = _make_tracker(tmp_path, monkeypatch)
    monkeypatch.setattr(portal, "HISTORY_PATH", tmp_path / "no" / "way" / "history.json")
    tracker.register_job("nano-banana", "j4", "alice", "image",
                         metadata={"prompt": "猫"})
    # 统计侧 job_owners 正常登记
    owner = tracker.get_job_owner("nano-banana", "j4")
    assert owner == "alice"
