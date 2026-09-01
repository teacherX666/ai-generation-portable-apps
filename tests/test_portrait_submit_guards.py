"""提交前拦截测试：duration 范围 + 素材 GetAsset 可用性（历史失败高频原因）。"""
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load_portrait_app():
    mod_path = ROOT / "volcengine-portrait" / "app.py"
    spec = importlib.util.spec_from_file_location("portrait_app_guards_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["portrait_app_guards_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeReader(io.BytesIO):
    def readline(self, size=-1):
        return b""


class FakeHandler:
    def __init__(self, body):
        raw = json.dumps(body).encode("utf-8")
        self.headers = {"Content-Type": "application/json",
                        "Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.path = "/api/virtual/jobs"
        self.status_code = None
        self.response_body = None

    def send_response(self, code):
        self.status_code = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    @property
    def wfile(self):
        class W:
            def write(self_, b):
                self.response_body = b
        return W()


def _submit(mod, monkeypatch, body, openapi_side_effect=None):
    handler = FakeHandler(body)
    monkeypatch.setattr(mod, "json_response",
                        lambda h, code, data: (setattr(h, "status_code", code),
                                               setattr(h, "response_body", data)))
    if openapi_side_effect is not None:
        monkeypatch.setattr(mod, "openapi_call", openapi_side_effect)
    monkeypatch.setattr(mod, "_decode_username", lambda h: "tester")
    monkeypatch.setattr(mod, "_user_day_subdir", lambda *a: "/tmp/out")
    monkeypatch.setattr(mod, "_prune_jobs_locked", lambda: None)
    monkeypatch.setattr(mod, "record_activity", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_executor", mock.MagicMock())
    mod.handle_virtual_jobs_post(handler)
    return handler


def test_duration_out_of_range_blocked(tmp_path, monkeypatch):
    mod = _load_portrait_app()
    # 默认模型是 2.0（上限 15 秒）
    for bad in (3, 18, 20, 0, 16, 100):
        handler = _submit(mod, monkeypatch,
                          {"asset_id": "asset-1", "prompt": "p", "duration": bad})
        assert handler.status_code == 400, f"duration={bad} 应被拦截"
        assert "4~15" in handler.response_body["error"]


def test_duration_range_follows_model(tmp_path, monkeypatch):
    mod = _load_portrait_app()
    monkeypatch.setattr(mod, "JOBS", {})

    def fake_openapi(action, body, **kw):
        return {"Result": {"Id": body["Id"], "Status": "Active"}}

    # 2.0 模型：16~30 秒全部拦截
    for bad in (16, 20, 30):
        handler = _submit(mod, monkeypatch,
                          {"asset_id": "asset-1", "prompt": "p", "duration": bad,
                           "model": "doubao-seedance-2-0-260128"},
                          openapi_side_effect=fake_openapi)
        assert handler.status_code == 400, f"2.0 duration={bad} 应被拦截"
        assert "4~15" in handler.response_body["error"]
    # 2.5 模型：16~30 秒合法放行
    for ok in (16, 20, 30):
        handler = _submit(mod, monkeypatch,
                          {"asset_id": "asset-1", "prompt": "p", "duration": ok,
                           "model": "doubao-seedance-2-5-260628"},
                          openapi_side_effect=fake_openapi)
        assert handler.status_code == 201, f"2.5 duration={ok} 应放行"
    # 2.5 模型：31+ 与 <4 仍拦截
    for bad in (3, 31, 60):
        handler = _submit(mod, monkeypatch,
                          {"asset_id": "asset-1", "prompt": "p", "duration": bad,
                           "model": "doubao-seedance-2-5-260628"},
                          openapi_side_effect=fake_openapi)
        assert handler.status_code == 400, f"2.5 duration={bad} 应被拦截"
        assert "4~30" in handler.response_body["error"]


def test_duration_valid_passes_guard_and_checks_asset(tmp_path, monkeypatch):
    mod = _load_portrait_app()
    calls = []

    def fake_openapi(action, body, **kw):
        calls.append((action, body))
        return {"Result": {"Id": body["Id"], "Status": "Active"}}

    monkeypatch.setattr(mod, "JOBS", {})
    handler = _submit(mod, monkeypatch,
                      {"asset_id": "asset-ok", "prompt": "p", "duration": 12},
                      openapi_side_effect=fake_openapi)
    assert handler.status_code == 201
    assert any(a == "GetAsset" for a, _ in calls)


def test_missing_asset_blocked_before_job_creation(tmp_path, monkeypatch):
    mod = _load_portrait_app()

    def fake_openapi(action, body, **kw):
        return {"error": "Asset not found"}

    handler = _submit(mod, monkeypatch,
                      {"asset_id": "asset-gone", "prompt": "p", "duration": 12},
                      openapi_side_effect=fake_openapi)
    assert handler.status_code == 400
    assert "已不存在" in handler.response_body["error"]
    assert mod.JOBS == {}


def test_processing_asset_blocked(tmp_path, monkeypatch):
    mod = _load_portrait_app()

    def fake_openapi(action, body, **kw):
        return {"Result": {"Id": body["Id"], "Status": "Processing"}}

    handler = _submit(mod, monkeypatch,
                      {"asset_id": "asset-new", "prompt": "p", "duration": 12},
                      openapi_side_effect=fake_openapi)
    assert handler.status_code == 400
    assert "审核处理中" in handler.response_body["error"]


def test_check_failure_does_not_block_submission(tmp_path, monkeypatch):
    """校验通道本身故障（网络等）不阻断提交，上游会再报真实错误。"""
    mod = _load_portrait_app()
    monkeypatch.setattr(mod, "JOBS", {})

    def fake_openapi(action, body, **kw):
        raise OSError("network down")

    handler = _submit(mod, monkeypatch,
                      {"asset_id": "asset-x", "prompt": "p", "duration": 12},
                      openapi_side_effect=fake_openapi)
    assert handler.status_code == 201
