import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location(
    "portal_app_api", ROOT / "portal" / "app.py"
)
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_api"] = portal
_spec.loader.exec_module(portal)


def _make_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(portal, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(portal, "HISTORY_PATH", tmp_path / "history.json")
    return portal.UsageTracker()


def _seed(tracker):
    now = time.time()
    tracker.history_upsert({"app": "nano-banana", "job_id": "j1", "username": "alice",
                            "kind": "image", "prompt": "一只猫", "model": "m",
                            "params": {}, "status": "done",
                            "submitted_at": now, "completed_at": now + 60,
                            "duration": 0, "results": [], "error": ""})
    tracker.history_upsert({"app": "seedance", "job_id": "j2", "username": "bob",
                            "kind": "video", "prompt": "挥手", "model": "m",
                            "params": {}, "status": "failed",
                            "submitted_at": now + 1, "completed_at": now + 61,
                            "duration": 10, "results": [], "error": "boom"})
    tracker.history_upsert({"app": "seedance", "job_id": "j-old", "username": "bob",
                            "kind": "video", "prompt": "旧任务", "model": "m",
                            "params": {}, "status": "done",
                            "submitted_at": now - 40 * 86400,
                            "completed_at": now - 40 * 86400 + 60,
                            "duration": 5, "results": [], "error": ""})


def test_query_history_permission_and_filters(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    _seed(tracker)
    # 普通用户只看到自己的
    items, total = tracker.query_history(
        username="alice", is_admin=False, days=30, kind="all", status="all",
        q="", user_filter="", limit=50, offset=0)
    assert total == 1 and items[0]["job_id"] == "j1"
    # admin 全量 + 状态过滤
    items, total = tracker.query_history(
        username="admin", is_admin=True, days=30, kind="all", status="failed",
        q="", user_filter="", limit=50, offset=0)
    assert total == 1 and items[0]["job_id"] == "j2"
    # kind 过滤
    items, total = tracker.query_history(
        username="admin", is_admin=True, days=30, kind="video", status="all",
        q="", user_filter="", limit=50, offset=0)
    assert total == 1 and items[0]["job_id"] == "j2"
    # 搜索提示词
    items, total = tracker.query_history(
        username="admin", is_admin=True, days=30, kind="all", status="all",
        q="猫", user_filter="", limit=50, offset=0)
    assert total == 1 and items[0]["job_id"] == "j1"
    # 40 天前记录不进 30 天窗口
    items, total = tracker.query_history(
        username="admin", is_admin=True, days=30, kind="all", status="all",
        q="", user_filter="", limit=50, offset=0)
    assert total == 2
    # admin 用户过滤
    items, total = tracker.query_history(
        username="admin", is_admin=True, days=30, kind="all", status="all",
        q="", user_filter="bob", limit=50, offset=0)
    assert total == 1 and items[0]["job_id"] == "j2"


def test_query_history_pagination(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    now = time.time()
    for i in range(5):
        tracker.history_upsert({"app": "a", "job_id": f"j{i}", "username": "u",
                                "kind": "image", "prompt": f"p{i}", "model": "",
                                "params": {}, "status": "done",
                                "submitted_at": now + i, "completed_at": now + i,
                                "duration": 0, "results": [], "error": ""})
    items, total = tracker.query_history(
        username="u", is_admin=False, days=30, kind="all", status="all",
        q="", user_filter="", limit=2, offset=0)
    assert total == 5 and len(items) == 2 and items[0]["job_id"] == "j4"
    items2, _ = tracker.query_history(
        username="u", is_admin=False, days=30, kind="all", status="all",
        q="", user_filter="", limit=2, offset=2)
    assert items2[0]["job_id"] == "j2"
