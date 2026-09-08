"""任务队列持久化测试：重启恢复（queued 重入队 / started 标记中断）+ 重试端点重放。"""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_seedance():
    mod_path = ROOT / "seedance" / "app.py"
    spec = importlib.util.spec_from_file_location("seedance_backlog_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seedance_backlog_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _isolate_state(mod, tmp_path, monkeypatch):
    """把 STATE_DIR/ACTIVITY_PATH/BACKLOG_PATH 全部指向临时目录。"""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "ACTIVITY_PATH", state / "activity_log.json")
    monkeypatch.setattr(mod, "BACKLOG_PATH", state / "jobs_backlog.json")


def _write_activity(mod, activity_id, job_id, values, ws_id="ws1"):
    items = mod.read_activity_log()
    items.append({
        "id": activity_id, "job_id": job_id, "status": "running",
        "workspace_id": ws_id, "username": "tester",
        "restore": {"values": values, "media": {}},
    })
    mod.write_activity_log(items)


def test_queued_job_requeued_on_recovery(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate_state(mod, tmp_path, monkeypatch)
    mod.JOBS.clear()
    _write_activity(mod, "act-1", "job-queued", {"prompt": "hello", "duration": 10})
    with mod.JOBS_LOCK:
        mod._backlog_set_locked("job-queued", stage="queued", activity_id="act-1",
                                ws_id="ws1", username="tester")

    started = []
    monkeypatch.setattr(mod, "run_job", lambda *a, **k: started.append(a))
    recovered, interrupted = mod.recover_backlog()

    assert recovered == 1 and interrupted == 0
    assert "job-queued" in mod.JOBS
    assert mod.JOBS["job-queued"]["status"] == "queued"
    assert started and started[0][0] == "job-queued"
    # 恢复后 backlog 保留 queued 阶段（终态时由 run_job 清除）
    assert mod._backlog_load()["job-queued"]["stage"] == "queued"


def test_started_job_marked_interrupted(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate_state(mod, tmp_path, monkeypatch)
    mod.JOBS.clear()
    _write_activity(mod, "act-2", "job-running", {"prompt": "hi", "duration": 12})
    with mod.JOBS_LOCK:
        mod._backlog_set_locked("job-running", stage="started", activity_id="act-2",
                                ws_id="ws1", username="tester")

    monkeypatch.setattr(mod, "run_job", lambda *a, **k: None)
    recovered, interrupted = mod.recover_backlog()

    assert recovered == 0 and interrupted == 1
    job = mod.JOBS["job-running"]
    assert job["status"] == "failed"
    assert job["retryable"] is True
    assert any("服务更新重启" in e for e in job["errors"])
    assert "job-running" not in mod._backlog_load()
    # 活动记录同步为 failed
    act = [a for a in mod.read_activity_log() if a["id"] == "act-2"][0]
    assert act["status"] == "failed"


def test_started_job_files_rebuilt_from_restore(tmp_path, monkeypatch):
    """素材从落盘文件重建：restore.media 里的 stored 文件被读回 files。"""
    mod = _load_seedance()
    _isolate_state(mod, tmp_path, monkeypatch)
    # 直接构造 copy_files_to_restore 的产物：写一个素材文件到 workspace media 目录
    media_dir = mod._ws_media_dir("ws1")
    media_dir.mkdir(parents=True, exist_ok=True)
    stored = "act-3_abc_image_1.png"
    (media_dir / stored).write_bytes(b"PNGDATA")
    _write_activity(mod, "act-3", "job-files", {"prompt": "p"})
    items = mod.read_activity_log()
    items[-1]["restore"]["media"] = {"ref_image_1": {"filename": "ref.png", "stored": stored, "mime": "image/png"}}
    mod.write_activity_log(items)

    files = mod._files_from_restore(items[-1]["restore"], "ws1")
    assert files.get("ref_image_1") == ("ref.png", b"PNGDATA")


def test_retry_job_rebuilds_and_resubmits(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate_state(mod, tmp_path, monkeypatch)
    _write_activity(mod, "act-4", "job-old", {"prompt": "retry me", "duration": 8, "ratio": "16:9"})
    items = mod.read_activity_log()
    items[-1]["restore"]["media"] = {}
    mod.write_activity_log(items)

    calls = {}
    monkeypatch.setattr(mod, "create_job",
                        lambda values, files, source, request_kind, request_data, ws_id, username:
                        calls.update({"values": values, "source": source, "kind": request_kind,
                                      "ws": ws_id, "user": username, "files": files}) or "new-id-1")
    new_id = mod.retry_job("job-old")
    assert new_id == "new-id-1"
    assert calls["values"]["prompt"] == "retry me"
    assert calls["values"]["ratio"] == "16:9"
    assert calls["source"] == "retry"
    assert calls["ws"] == "ws1" and calls["user"] == "tester"


def test_backlog_cleared_on_terminal(tmp_path, monkeypatch):
    """终态收尾要清 backlog，否则下次重启会重复恢复已完成任务。"""
    mod = _load_seedance()
    _isolate_state(mod, tmp_path, monkeypatch)
    mod.JOBS.clear()
    with mod.JOBS_LOCK:
        mod._backlog_set_locked("job-done", stage="started", activity_id="act-5", ws_id="ws1")
    with mod.JOBS_LOCK:
        mod._backlog_remove_locked("job-done")
    assert mod._backlog_load() == {}
