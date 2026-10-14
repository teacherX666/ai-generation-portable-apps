"""重启续跑测试（2026-10-14 口径：token 优先）。

背景：上游（方舟）任务是异步的，我们进程死了它照样跑、照样计费；旧恢复逻辑
却对 backlog 里每一条都重新 run_job → 同一件事付两次钱。

本文件守三条不变量：
1. 已拿到 task_id 的 run 重启后只「接回轮询」，**绝不重新 POST**；
2. 已拿到结果的 run 重启后直接回放（0 网络调用，tokens 照原样计）；
3. 状态未确认（submitting）的 run **不自动重投** —— 宁可让用户点一次重试。

对应实现见 docs/restart-resume-design.md。
"""
import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_seedance():
    return _load("seedance_resume_test", "seedance/app.py")


def _load_nano():
    return _load("nano_resume_test", "nano-banana/app.py")


def _isolate(mod, tmp_path, monkeypatch, backlog_attr="BACKLOG_PATH"):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "ACTIVITY_PATH", state / "activity_log.json")
    monkeypatch.setattr(mod, backlog_attr, state / "jobs_backlog.json")
    if hasattr(mod, "FILES_MAP_PATH"):
        monkeypatch.setattr(mod, "FILES_MAP_PATH", state / "download_files.json")
    if hasattr(mod, "OUTPUT_DIR"):
        monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path / "outputs")


def _activity(mod, activity_id, job_id, values, ws_id="ws1"):
    items = mod.read_activity_log()
    items.append({
        "id": activity_id, "job_id": job_id, "status": "running",
        "workspace_id": ws_id, "username": "tester",
        "restore": {"values": values, "media": {}},
    })
    mod.write_activity_log(items)


def _seed_job(mod, job_id, values, ws_id="ws1"):
    """run_job 假定 JOBS 条目已由 create_job / recover_backlog 建好。"""
    lock = getattr(mod, "JOBS_LOCK", None) or mod.LOCK
    with lock:
        mod.JOBS[job_id] = {
            "id": job_id, "status": "queued", "events": [], "results": [], "errors": [],
            "done": 0, "total": 0, "duration": 0, "username": "tester",
            "workspace_id": ws_id, "title": "t", "prompt": str(values.get("prompt") or ""),
            "model": "", "provider": "", "submitted_at": time.time(),
            "started_at": None, "finished_at": None,
        }


VALUES_1X1 = {"prompt": "p", "duration": "5", "repeat_count": "1", "concurrency": "1"}
ARK_STATUS_URL = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/cgt-1"


# ── 1. 分类矩阵 ────────────────────────────────────────────────────────────

def test_submitted_run_is_resumed_never_resubmitted(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    plan = mod._resume_plan("job", VALUES_1X1, {
        "1": {"phase": "submitted", "task_id": "cgt-1", "status_url": ARK_STATUS_URL},
    })
    assert plan["resume"] == {1: {"task_id": "cgt-1", "status_url": ARK_STATUS_URL}}
    assert plan["done"] == {} and plan["unknown"] == {} and plan["resubmit"] == []


def test_done_run_is_replayed_not_resubmitted(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    result = {"index": 1, "task_id": "cgt-1", "status": "succeeded", "tokens": 540900}
    plan = mod._resume_plan("job", VALUES_1X1, {
        "1": {"phase": "done", "task_id": "cgt-1", "status_url": ARK_STATUS_URL, "result": result},
    })
    assert plan["done"] == {1: result}
    assert plan["resume"] == {} and plan["resubmit"] == []


def test_submitting_run_is_not_auto_resubmitted(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    plan = mod._resume_plan("job", VALUES_1X1, {"1": {"phase": "submitting"}})
    assert plan["resubmit"] == [] and plan["resume"] == {} and plan["done"] == {}
    assert 1 in plan["unknown"]
    assert "未自动重投" in plan["unknown"][1]


def test_untracked_runs_are_resubmitted(tmp_path, monkeypatch):
    """没写记录 = 线程还没发起付费请求（本地模型永远停在这里）→ 重投不花钱。"""
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    values = {"prompt": "p", "duration": "5", "repeat_count": "3", "concurrency": "1"}
    plan = mod._resume_plan("job", values, {
        "1": {"phase": "submitted", "task_id": "cgt-1", "status_url": ARK_STATUS_URL},
    })
    assert plan["resume"] == {1: {"task_id": "cgt-1", "status_url": ARK_STATUS_URL}}
    assert sorted(plan["resubmit"]) == [2, 3]


def test_legacy_started_without_runs_is_indeterminate(tmp_path, monkeypatch):
    """存量条目（本功能上线前写的）无法判断是否已计费 → 不许自动重投。"""
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    assert mod._resume_plan("job", VALUES_1X1, {}) is None


# ── 2. recover_backlog 的分流 ───────────────────────────────────────────────

def test_recover_backlog_hands_resume_plan_to_run_job(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    mod.JOBS.clear()
    _activity(mod, "act-r", "job-resume", {"prompt": "p", "duration": "5",
                                           "repeat_count": "2", "concurrency": "1"})
    done_result = {"index": 2, "task_id": "cgt-2", "status": "succeeded", "tokens": 42}
    with mod.JOBS_LOCK:
        mod._backlog_set_locked("job-resume", stage="started", activity_id="act-r",
                                ws_id="ws1", username="tester")
        mod._backlog_set_run_locked("job-resume", 1, phase="submitted",
                                    task_id="cgt-1", status_url=ARK_STATUS_URL)
        mod._backlog_set_run_locked("job-resume", 2, phase="done",
                                    task_id="cgt-2", result=done_result)

    captured = {}
    monkeypatch.setattr(mod, "run_job", lambda *a, **k: captured.update({"args": a, "kwargs": k}))
    recovered, interrupted = mod.recover_backlog()

    assert recovered == 1 and interrupted == 0
    plan = captured["kwargs"]["recovery"]
    assert plan["resume"] == {1: {"task_id": "cgt-1", "status_url": ARK_STATUS_URL}}
    assert plan["done"] == {2: done_result}
    assert plan["resubmit"] == [] and plan["unknown"] == {}
    # 事件里必须看得见「续跑」这件事，不能静默
    assert any("续跑" in e["message"] for e in mod.JOBS["job-resume"]["events"])


def test_recover_backlog_keeps_queued_requeue_behaviour(tmp_path, monkeypatch):
    """queued（从未开跑）→ 照旧自动重新入队，且不带恢复计划。"""
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    mod.JOBS.clear()
    _activity(mod, "act-q", "job-queued", {"prompt": "p", "duration": "5"})
    with mod.JOBS_LOCK:
        mod._backlog_set_locked("job-queued", stage="queued", activity_id="act-q",
                                ws_id="ws1", username="tester")

    captured = {}
    monkeypatch.setattr(mod, "run_job", lambda *a, **k: captured.update({"kwargs": k}))
    recovered, _ = mod.recover_backlog()

    assert recovered == 1
    assert captured["kwargs"]["recovery"] is None


# ── 3. run_one 真的不 POST ─────────────────────────────────────────────────

def test_run_one_resume_polls_status_without_creating_task(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    job_id = "job-resume-run"
    with mod.JOBS_LOCK:
        mod.JOBS[job_id] = {
            "id": job_id, "status": "running", "events": [], "results": [], "errors": [],
            "done": 0, "total": 1, "username": "tester", "workspace_id": "ws1",
        }
    calls = []
    status_payload = {
        "status": "succeeded",
        "content": {"video_url": "https://cdn.example/out.mp4"},
        "duration": 5,
        "usage": {"total_tokens": 540900},
    }

    def fake_request_json(method, url, api_key, body=None, **kw):
        calls.append((method, url))
        return dict(status_payload)

    monkeypatch.setattr(mod, "request_json", fake_request_json)
    monkeypatch.setattr(mod, "download_video", lambda url, out_path: (out_path.parent.mkdir(parents=True, exist_ok=True),
                                                                     out_path.write_bytes(b"VIDEO")))
    monkeypatch.setattr(mod, "report_final_to_portal", lambda *a, **k: None)
    values = {
        "api_key": "ark-test", "provider": "volcengine",
        "model": "doubao-seedance-2-5-260628", "prompt": "p", "duration": "5",
        "poll_interval": "1", "output_dir": str(tmp_path / "outputs"),
    }

    result = mod.run_one(job_id, 1, values, {}, "ws1",
                         resume={"task_id": "cgt-1", "status_url": ARK_STATUS_URL})

    assert [c[0] for c in calls] == ["GET"], f"续跑路径不许出现 POST：{calls}"
    assert calls[0][1] == ARK_STATUS_URL
    assert result["task_id"] == "cgt-1"
    assert result["tokens"] == 540900
    assert any("续跑上游任务" in e["message"] for e in mod.JOBS[job_id]["events"])


# ── 4. run_job 回放已完成的 run、只对未提交的派 worker ──────────────────────

def test_run_job_replays_done_runs_and_only_dispatches_rest(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "report_final_to_portal", lambda *a, **k: None)
    dispatched = []

    def fake_run_one_with_fallback(*args, **kwargs):
        dispatched.append(args)
        return {"index": args[1], "task_id": "cgt-2", "status": "succeeded",
                "tokens": 7, "filename": "b.mp4"}

    monkeypatch.setattr(mod, "run_one_with_fallback", fake_run_one_with_fallback)
    job_id = "job-replay"
    values = {"api_key": "k", "provider": "volcengine", "prompt": "p",
              "duration": "5", "repeat_count": "2", "concurrency": "1"}
    _seed_job(mod, job_id, values)
    _activity(mod, "act-replay", job_id, values)
    replayed = {"index": 1, "task_id": "cgt-1", "status": "succeeded",
                "tokens": 540900, "filename": "a.mp4"}
    mod.run_job(job_id, values, {}, activity_id="act-replay", ws_id="ws1",
                recovery={"done": {1: replayed},
                          "resume": {2: {"task_id": "cgt-2", "status_url": ARK_STATUS_URL}},
                          "unknown": {}, "resubmit": []})

    job = mod.JOBS[job_id]
    assert job["status"] == "succeeded"
    assert job["done"] == 2
    # 回放的 run 的 tokens 必须照原样计入（统计口径不能被重启改小/改大）
    assert job["tokens"] == 540900 + 7
    # index 1 不派 worker；index 2 带着 resume 派出去（位置参数第 6 个）
    assert [a[1] for a in dispatched] == [2]
    assert dispatched[0][5] == {"task_id": "cgt-2", "status_url": ARK_STATUS_URL}
    # 终态收尾要清 backlog，否则下次重启还会恢复一遍
    assert job_id not in mod._backlog_load()
    assert any("回放" in e["message"] for e in job["events"])


def test_run_job_unknown_run_fails_without_resubmitting(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "report_final_to_portal", lambda *a, **k: None)
    dispatched = []
    monkeypatch.setattr(mod, "run_one_with_fallback",
                        lambda *a, **k: dispatched.append(a) or {"index": a[1], "tokens": 0})
    job_id = "job-unknown"
    values = {"api_key": "k", "provider": "volcengine", "prompt": "p",
              "duration": "5", "repeat_count": "2", "concurrency": "1"}
    _seed_job(mod, job_id, values)
    _activity(mod, "act-unknown", job_id, values)
    mod.run_job(job_id, values, {}, activity_id="act-unknown", ws_id="ws1",
                recovery={"done": {}, "resume": {},
                          "unknown": {1: "Run 1: 状态未确认，未自动重投"}, "resubmit": []})

    job = mod.JOBS[job_id]
    assert dispatched == [] or [a[1] for a in dispatched] == [2]
    assert job["status"] == "failed"
    assert any("未自动重投" in e for e in job["errors"])
    # 提示文案让用户点「重试」→ 按钮必须点亮（前端只看 retryable）
    assert job.get("retryable") is True


# ── 6. 正常路径不许被这次改动破坏（提交链路照旧 POST + 落 submitted）────────

def test_seedance_normal_path_still_posts_and_records_submitted(tmp_path, monkeypatch):
    mod = _load_seedance()
    _isolate(mod, tmp_path, monkeypatch)
    job_id = "job-normal"
    values = {"api_key": "ark-test", "provider": "volcengine",
              "model": "doubao-seedance-2-5-260628", "prompt": "p", "duration": "5",
              "poll_interval": "1", "output_dir": str(tmp_path / "outputs")}
    _seed_job(mod, job_id, values)
    calls = []
    payloads = {
        "POST": {"id": "cgt-normal"},
        "GET": {"status": "succeeded", "content": {"video_url": "https://cdn.example/n.mp4"},
                "duration": 5, "usage": {"total_tokens": 111}},
    }

    def fake_request_json(method, url, api_key, body=None, **kw):
        calls.append(method)
        # 提交那一刻，backlog 里必须先有 submitting 兜底标记
        runs = mod._backlog_runs(mod._backlog_load().get(job_id))
        if method == "POST":
            assert (runs.get("1") or {}).get("phase") == mod.RUN_PHASE_SUBMITTING, runs
        return dict(payloads[method])

    monkeypatch.setattr(mod, "request_json", fake_request_json)
    monkeypatch.setattr(mod, "download_video", lambda url, out_path: (out_path.parent.mkdir(parents=True, exist_ok=True),
                                                                     out_path.write_bytes(b"V")))
    result = mod.run_one(job_id, 1, values, {}, "ws1")

    assert calls[0] == "POST" and calls.count("POST") == 1     # 正常路径照旧提交一次
    assert result["tokens"] == 111
    runs = mod._backlog_runs(mod._backlog_load().get(job_id))
    assert runs["1"]["phase"] == mod.RUN_PHASE_SUBMITTED
    assert runs["1"]["task_id"] == "cgt-normal"


def test_nano_normal_path_completes_and_clears_backlog(tmp_path, monkeypatch):
    """同步通道正常跑完：submitting → done → 终态清 backlog（重启时不会残留）。"""
    mod = _load_nano()
    _isolate(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "report_final_to_portal", lambda *a, **k: None)
    job_id = "job-nb-normal"
    values = {"api_key": "k", "provider": "t8star", "prompt": "p", "mode": "text2img",
              "repeat_count": "1", "concurrency": "1",
              "output_dir": str(tmp_path / "outputs")}
    _seed_job(mod, job_id, values)
    _activity(mod, "act-nb-normal", job_id, values)
    monkeypatch.setattr(mod, "request_json",
                        lambda method, url, api_key, body=None, **kw:
                        {"data": [{"url": "https://cdn.example/a.png"}]})
    monkeypatch.setattr(mod, "save_image_item",
                        lambda item, out_dir, prefix, i: (str(item.get("url")),
                                                          str(out_dir / f"{prefix}_{i}.png")))
    mod.run_job(job_id, values, {}, activity_id="act-nb-normal", ws_id="ws1")

    job = mod.JOBS[job_id]
    assert job["status"] == "succeeded", job.get("errors")
    assert job["done"] == 1
    # 终态收尾必须清 backlog，否则下次重启还会按 run 状态恢复一遍
    assert job_id not in mod._backlog_load()


NANO_ASYNC_URL = "https://ai.t8star.org/v1/images/tasks/t8-1"


def test_nano_submitted_run_resumes_without_post(tmp_path, monkeypatch):
    mod = _load_nano()
    _isolate(mod, tmp_path, monkeypatch)
    job_id = "job-nb-resume"
    values = {"api_key": "k", "provider": "t8star", "prompt": "p", "mode": "text2img",
              "repeat_count": "1", "concurrency": "1", "poll_interval": "1",
              "output_dir": str(tmp_path / "outputs")}
    _seed_job(mod, job_id, values)
    calls = []

    def fake_request_json(method, url, api_key, body=None, **kw):
        calls.append((method, url))
        return {"data": {"status": "succeeded", "data": [{"url": "https://cdn.example/a.png"}]}}

    monkeypatch.setattr(mod, "request_json", fake_request_json)
    monkeypatch.setattr(mod, "save_image_item",
                        lambda item, out_dir, prefix, i: (str(item.get("url")),
                                                          str(out_dir / f"{prefix}_{i}.png")))

    result = mod.run_one(job_id, 1, values, {}, "ws1",
                         resume={"task_id": "t8-1", "status_url": NANO_ASYNC_URL})

    assert [c[0] for c in calls] == ["GET"], f"续跑路径不许出现 POST：{calls}"
    assert calls[0][1] == NANO_ASYNC_URL
    assert result["task_id"] == "t8-1"
    assert result["status"] == "succeeded"
    assert any("续跑上游任务" in e["message"] for e in mod.JOBS[job_id]["events"])


def test_nano_marks_submitting_before_paid_call(tmp_path, monkeypatch):
    """同步通道没有断点可续 —— 必须靠提交前的 submitting 标记把「状态未确认」记下来。"""
    mod = _load_nano()
    _isolate(mod, tmp_path, monkeypatch)
    job_id = "job-nb-submitting"
    values = {"api_key": "k", "provider": "t8star", "prompt": "p", "mode": "text2img",
              "repeat_count": "1", "concurrency": "1"}
    _seed_job(mod, job_id, values)
    seen = {}

    def fake_request_json(method, url, api_key, body=None, **kw):
        runs = mod._backlog_runs(mod._backlog_load().get(job_id))
        seen["phase"] = (runs.get("1") or {}).get("phase")
        raise RuntimeError("stop before the paid response")

    monkeypatch.setattr(mod, "request_json", fake_request_json)
    try:
        mod.run_one(job_id, 1, values, {}, "ws1")
    except Exception:
        pass
    assert seen["phase"] == mod.RUN_PHASE_SUBMITTING


def test_nano_local_run_writes_no_run_record(tmp_path, monkeypatch):
    """本地 ComfyUI 免费：不写记录，恢复时照旧重跑（这比「让用户点重试」更合预期）。"""
    mod = _load_nano()
    _isolate(mod, tmp_path, monkeypatch)
    job_id = "job-nb-local"
    values = {"api_key": "", "provider": "comfyui_local", "prompt": "p", "mode": "img2img",
              "repeat_count": "1", "concurrency": "1"}
    _seed_job(mod, job_id, values)
    monkeypatch.setattr(mod, "local_gateway_available", lambda *a, **k: False)
    try:
        mod.run_one(job_id, 1, values, {}, "ws1")
    except Exception:
        pass
    assert mod._backlog_runs(mod._backlog_load().get(job_id)) == {}


def test_nano_recover_backlog_resumes_and_replays(tmp_path, monkeypatch):
    mod = _load_nano()
    _isolate(mod, tmp_path, monkeypatch)
    mod.JOBS.clear()
    _activity(mod, "act-nb", "job-nb-recover",
              {"prompt": "p", "repeat_count": "2", "concurrency": "1"})
    done_result = {"index": 2, "task_id": "t8-2", "status": "succeeded", "images": []}
    with mod.LOCK:
        mod._backlog_set_locked("job-nb-recover", stage="started", activity_id="act-nb",
                                ws_id="ws1", username="tester")
        mod._backlog_set_run_locked("job-nb-recover", 1, phase="submitted",
                                    task_id="t8-1", status_url=NANO_ASYNC_URL)
        mod._backlog_set_run_locked("job-nb-recover", 2, phase="done",
                                    task_id="t8-2", result=done_result)

    captured = {}
    monkeypatch.setattr(mod, "run_job", lambda *a, **k: captured.update({"kwargs": k}))
    recovered, interrupted = mod.recover_backlog()

    assert recovered == 1 and interrupted == 0
    plan = captured["kwargs"]["recovery"]
    assert plan["resume"] == {1: {"task_id": "t8-1", "status_url": NANO_ASYNC_URL}}
    assert plan["done"] == {2: done_result}
    assert plan["resubmit"] == [] and plan["unknown"] == {}
    assert any("续跑" in e["message"] for e in mod.JOBS["job-nb-recover"]["events"])

