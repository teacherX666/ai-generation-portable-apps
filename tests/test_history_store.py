import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location(
    "portal_app_history_store", ROOT / "portal" / "app.py"
)
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_history_store"] = portal
_spec.loader.exec_module(portal)


def _make_tracker(tmp_path, monkeypatch, cap=10):
    monkeypatch.setattr(portal, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(portal, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(portal, "HISTORY_CAP", cap)
    return portal.UsageTracker()


def _record(**overrides):
    base = {"app": "nano-banana", "job_id": "j1", "username": "alice",
            "kind": "image", "prompt": "一只猫", "params": {},
            "status": "pending", "submitted_at": time.time(),
            "completed_at": None, "duration": 0, "results": [], "error": ""}
    base.update(overrides)
    return base


def test_history_upsert_and_prune(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    tracker.history_upsert(_record())
    # 40 天前的记录应被同次写入剪掉
    tracker.history_upsert(_record(job_id="j-old",
                                   submitted_at=time.time() - 40 * 86400))
    data = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert "nano-banana:j1" in data
    assert "nano-banana:j-old" not in data


def test_history_cap(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch, cap=5)
    now = time.time()
    for i in range(8):
        tracker.history_upsert(_record(app="a", job_id=f"j{i}",
                                       submitted_at=now + i))
    data = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert len(data) == 5
    # 保留的是最新 5 条（j3..j7）
    assert "a:j2" not in data and "a:j3" in data and "a:j7" in data


def test_history_upsert_survives_corrupt_file(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    (tmp_path / "history.json").write_text("{corrupt", encoding="utf-8")
    tracker.history_upsert(_record())
    data = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert "nano-banana:j1" in data
