"""僵尸作业回归测试（2026-09-15）。

背景（真实故障）：`create_job` 旧顺序是
    JOBS[job_id] = {...} → record_activity(...) → Thread(run_job).start()
中间任何一步抛异常（典型是 copy_files_to_restore 往 workspace 素材目录写盘），
作业已经进了 JOBS 但 worker 线程从未启动 —— 前端提交只看到一次 400，队列里却
永久挂着一个 status=queued / started_at=None / total=0 的僵尸任务，点「取消」
也无效（没有线程可收）。seedance 与 nano-banana 同源缺陷，两处都已修。

本文件锁死三条不变量：
1) 准备工作失败时，作业不得留在 JOBS（也不能留 backlog 条目）；
2) 已经残留的僵尸，会被 /api/jobs 的自愈扫描标成 failed（可重试），不再永久排队；
3) 重试路径复用「先预留 job_id → 先落盘 restore → 再入库」的顺序，且
   create_job 用同一个 job_id 落盘，不能用别的 id 写素材。
"""
import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(rel_path: str, name: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _isolate_state(mod, tmp_path, monkeypatch, lock_name="JOBS_LOCK"):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "ACTIVITY_PATH", state / "activity_log.json")
    monkeypatch.setattr(mod, "BACKLOG_PATH", state / "jobs_backlog.json")
    mod.JOBS.clear()


def _stub_heavy_deps(mod, monkeypatch):
    """把与队列无关的重活换成空实现，只保留 create_job 的队列语义。"""
    monkeypatch.setattr(mod, "validate_model_capabilities", lambda values, files: None)
    monkeypatch.setattr(mod, "job_id_response", lambda job_id: {"job_id": job_id})
    started = []
    monkeypatch.setattr(mod, "run_job", lambda *a, **k: started.append(a[0] if a else None))
    import threading as _threading

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            self._target, self._args = target, args

        def start(self):
            if self._target:
                self._target(*self._args)

    monkeypatch.setattr(mod.threading, "Thread", _FakeThread)
    return started


# ─────────────────────────── seedance ───────────────────────────

def test_seedance_prepare_failure_leaves_no_zombie(tmp_path, monkeypatch):
    mod = _load("seedance/app.py", "seedance_zombie_test")
    _isolate_state(mod, tmp_path, monkeypatch)
    started = _stub_heavy_deps(mod, monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full / ACL denied")

    monkeypatch.setattr(mod, "copy_files_to_restore", boom)

    raised = False
    try:
        mod.create_job({"prompt": "x", "duration": 5}, {}, "page", "multipart",
                       {"raw": {}}, ws_id="ws1", username="tester")
    except OSError:
        raised = True

    assert raised, "准备工作失败必须把异常抛给调用方（HTTP 400）"
    assert mod.JOBS == {}, "失败后绝不能留下 queued 僵尸作业"
    assert mod._backlog_load() == {}, "失败后不能留下 backlog 条目"
    assert started == [], "绝不能启动 worker 线程"


def test_seedance_post_insert_failure_rolls_back(tmp_path, monkeypatch):
    """入库之后、线程起来之前失败（例如活动记录写盘失败）也必须回滚。"""
    mod = _load("seedance/app.py", "seedance_zombie_test2")
    _isolate_state(mod, tmp_path, monkeypatch)
    _stub_heavy_deps(mod, monkeypatch)
    monkeypatch.setattr(mod, "copy_files_to_restore", lambda *a, **k: {"values": {}, "media": {}})

    def boom(*a, **k):
        raise RuntimeError("activity log write failed")

    monkeypatch.setattr(mod, "record_activity", boom)

    raised = False
    try:
        mod.create_job({"prompt": "x", "duration": 5}, {}, "page", "multipart",
                       {"raw": {}}, ws_id="ws1", username="tester")
    except RuntimeError:
        raised = True

    assert raised
    assert mod.JOBS == {}, "record_activity 失败后作业必须被摘掉"
    assert mod._backlog_load() == {}


def test_seedance_sweep_marks_stale_queue_only(tmp_path, monkeypatch):
    """自愈扫描只清「从未启动」的陈旧排队任务，不碰正常的 queued/running。"""
    mod = _load("seedance/app.py", "seedance_zombie_test3")
    _isolate_state(mod, tmp_path, monkeypatch)
    now = time.time()
    with mod.JOBS_LOCK:
        mod.JOBS["zombie"] = {
            "id": "zombie", "status": "queued", "events": [], "results": [], "errors": [],
            "done": 0, "total": 0, "submitted_at": now - 600, "started_at": None,
            "workspace_id": "ws1", "username": "u",
        }
        mod.JOBS["fresh"] = {
            "id": "fresh", "status": "queued", "events": [], "results": [], "errors": [],
            "done": 0, "total": 0, "submitted_at": now - 5, "started_at": None,
            "workspace_id": "ws1", "username": "u",
        }
        mod.JOBS["running"] = {
            "id": "running", "status": "running", "events": [], "results": [], "errors": [],
            "done": 0, "total": 1, "submitted_at": now - 600, "started_at": now - 590,
            "workspace_id": "ws1", "username": "u",
        }

    swept = mod._sweep_zombie_jobs()

    assert swept == 1
    assert mod.JOBS["zombie"]["status"] == "failed"
    assert mod.JOBS["zombie"].get("finished_at")
    assert mod.JOBS["fresh"]["status"] == "queued", "刚提交的正常排队任务不能被误杀"
    assert mod.JOBS["running"]["status"] == "running", "运行中的任务不能被误杀"


def test_seedance_retry_reserves_job_id_before_insert(tmp_path, monkeypatch):
    """重试：先落盘 restore（用最终 job_id），再入库，且用同一个 id 写素材。"""
    mod = _load("seedance/app.py", "seedance_zombie_test4")
    _isolate_state(mod, tmp_path, monkeypatch)
    _stub_heavy_deps(mod, monkeypatch)

    seen = {}

    def fake_copy(values, files, prefix, ws_id="localhost"):
        seen["copy_prefix"] = prefix
        return {"values": {"prompt": "p"}, "media": {}}

    def fake_create(values, files, source, request_kind, request_data,
                    ws_id="localhost", username="", activity_id=None, record=True,
                    media_scope=None, job_id=None, prepared_restore=None, activity_patch=None):
        seen["create_job_id"] = job_id
        seen["prepared_restore"] = prepared_restore
        seen["activity_patch"] = activity_patch
        return job_id

    monkeypatch.setattr(mod, "copy_files_to_restore", fake_copy)
    monkeypatch.setattr(mod, "create_job", fake_create)

    items = [{
        "id": "act-root", "job_id": "job-old", "status": "failed", "username": "tester",
        "workspace_id": "ws1", "title": "t", "restore": {"values": {"prompt": "p", "duration": 5}, "media": {}},
    }]
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "read_activity_log", lambda: items)

    new_id = mod.retry_job("job-old")

    assert new_id == seen["create_job_id"], "落盘前缀与最终 job_id 必须一致"
    assert seen["copy_prefix"] == new_id
    assert seen["prepared_restore"] is not None
    patch = seen["activity_patch"] or {}
    assert patch.get("job_id") == new_id
    assert patch.get("status") == "running"
    assert patch.get("retry_of") == "job-old"
    assert patch.get("attempts"), "旧尝试要折进同一条活动记录"


# ───────────────────────── nano-banana ──────────────────────────

def test_nano_prepare_failure_leaves_no_zombie(tmp_path, monkeypatch):
    mod = _load("nano-banana/app.py", "nano_zombie_test")
    _isolate_state(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "validate_model_capabilities", lambda values, files: None)
    monkeypatch.setattr(mod, "job_id_response", lambda job_id: {"job_id": job_id})
    monkeypatch.setattr(mod, "copy_files_to_restore", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

    raised = False
    try:
        mod.create_job({"prompt": "x"}, {}, "page", "multipart", {"raw": {}},
                       ws_id="ws1", username="tester")
    except OSError:
        raised = True

    assert raised
    assert mod.JOBS == {}, "nano-banana 同样不能留下 queued 僵尸"
    assert mod._backlog_load() == {}


def test_nano_sweep_marks_stale_queue(tmp_path, monkeypatch):
    mod = _load("nano-banana/app.py", "nano_zombie_test2")
    _isolate_state(mod, tmp_path, monkeypatch)
    now = time.time()
    with mod.LOCK:
        mod.JOBS["zombie"] = {
            "id": "zombie", "status": "queued", "events": [], "results": [], "errors": [],
            "done": 0, "total": 0, "submitted_at": now - 600, "started_at": None,
            "workspace_id": "ws1", "username": "u",
        }
    assert mod._sweep_zombie_jobs() == 1
    assert mod.JOBS["zombie"]["status"] == "failed"
