import importlib.util
import sys
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


def test_history_write_failure_never_breaks_stats(tmp_path, monkeypatch):
    """历史落库失败（如路径不可写）时，统计登记照常成功。"""
    tracker = _make_tracker(tmp_path, monkeypatch)
    monkeypatch.setattr(portal, "HISTORY_PATH", tmp_path / "no" / "way" / "history.json")
    tracker.register_job("nano-banana", "j4", "alice", "image",
                         metadata={"prompt": "猫"})
    # 统计侧 job_owners 正常登记
    owner = tracker.get_job_owner("nano-banana", "j4")
    assert owner == "alice"
