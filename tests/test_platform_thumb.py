import http.client
import importlib.util
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location(
    "portal_app_thumb", ROOT / "portal" / "app.py"
)
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_thumb"] = portal
_spec.loader.exec_module(portal)

FAKE_JPEG = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"


def _serve(monkeypatch, tmp_path):
    monkeypatch.setattr(portal, "THUMB_DIR", tmp_path)
    monkeypatch.setattr(portal, "APPS", {"seedance": {"port": 8787}})
    monkeypatch.setattr(portal.shutil, "which", lambda _name: "/fake/ffmpeg")

    class TestHandler(portal.Handler):
        def _current_user(self):
            return {"username": "alice", "role": "user"}

    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_thumb_rejects_bad_app_and_bad_url(tmp_path, monkeypatch):
    server, port = _serve(monkeypatch, tmp_path)
    try:
        resp, _ = _get(port, "/api/platform/thumb?app=nope&url=/x.mp4")
        assert resp.status == 400
        resp, _ = _get(port, "/api/platform/thumb?app=seedance&url=/../etc/passwd")
        assert resp.status == 400
        resp, _ = _get(port, "/api/platform/thumb?app=seedance&url=")
        assert resp.status == 400
    finally:
        server.shutdown()
        server.server_close()


def test_thumb_extracts_caches_and_serves_jpeg(tmp_path, monkeypatch):
    server, port = _serve(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        portal.subprocess, "run",
        lambda *a, **k: calls.append((a, k)) or _FakeProc(0, FAKE_JPEG, b""),
    )
    try:
        resp, body = _get(port, "/api/platform/thumb?app=seedance&url=/api/download/abc123")
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "image/jpeg"
        assert resp.getheader("Cache-Control") == "public, max-age=86400"
        assert body == FAKE_JPEG
        # 缓存落盘且原子命名（无 .tmp 残留）
        assert any(p.suffix == ".jpg" for p in tmp_path.rglob("*"))
        assert not any(p.name.endswith(".tmp") for p in tmp_path.rglob("*"))
        # ffmpeg 收到的是子应用内网地址
        args = calls[0][0][0]
        assert "http://127.0.0.1:8787/api/download/abc123" in args
        assert "-ss" in args and "scale=480:-2" in args
        # 第二次请求走缓存，不再起 ffmpeg
        before = len(calls)
        resp, _ = _get(port, "/api/platform/thumb?app=seedance&url=/api/download/abc123")
        assert resp.status == 200
        assert len(calls) == before
    finally:
        server.shutdown()
        server.server_close()


def test_thumb_returns_404_when_ffmpeg_fails(tmp_path, monkeypatch):
    server, port = _serve(monkeypatch, tmp_path)

    class _FakeProc:
        def __init__(self, code, out, err):
            self.returncode, self.stdout, self.stderr = code, out, err

    monkeypatch.setattr(
        portal.subprocess, "run",
        lambda *a, **k: _FakeProc(1, b"", b"moov atom not found"),
    )
    try:
        resp, _ = _get(port, "/api/platform/thumb?app=seedance&url=/api/download/bad")
        assert resp.status == 404
        assert not any(p.suffix == ".jpg" for p in tmp_path.rglob("*"))
    finally:
        server.shutdown()
        server.server_close()


class _FakeProc:
    def __init__(self, code, out, err):
        self.returncode, self.stdout, self.stderr = code, out, err
