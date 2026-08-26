import http.client
import importlib.util
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "director"))

# 用唯一模块名加载，避免与套件里其它 `import app` 的测试撞 sys.modules
_spec = importlib.util.spec_from_file_location(
    "director_app_jobs", ROOT / "director" / "app.py"
)
director = importlib.util.module_from_spec(_spec)
sys.modules["director_app_jobs"] = director
_spec.loader.exec_module(director)


def _fake_ark_json(method, url, api_key, body=None, timeout=None):
    if "images/generations" in url:
        return {"data": [{"url": "https://fake.example/img1.png"}]}
    return {}


def test_run_text2image_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLCENGINE_ARK_API_KEY", "sk-ark-test")
    monkeypatch.setattr(director, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(director, "request_json", _fake_ark_json)
    monkeypatch.setattr(director, "_download_image", lambda url, dest: dest.write_bytes(b"PNG"))

    job_id = "job-1"
    director.JOBS[job_id] = {"id": job_id, "status": "pending", "prompt": "一只猫",
                             "aspect_ratio": "1:1", "count": 1, "results": [], "error": ""}
    director._run_text2image(job_id, "一只猫", "1:1", 1, "2K")
    job = director.JOBS[job_id]
    assert job["status"] == "done"
    assert len(job["results"]) == 1
    assert (tmp_path / "outputs" / "job-1-0.png").exists()


def test_jobs_post_returns_x_job_id_header(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLCENGINE_ARK_API_KEY", "sk-ark-test")
    monkeypatch.setattr(director, "OUTPUT_DIR", tmp_path / "outputs")  # 隔离，避免写真实 outputs
    monkeypatch.setattr(director, "request_json", _fake_ark_json)
    monkeypatch.setattr(director, "_download_image", lambda url, dest: dest.write_bytes(b"PNG"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), director.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({"prompt": "一只猫", "aspect_ratio": "1:1", "count": 1})
        conn.request("POST", "/api/jobs", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("X-Job-Id")
        payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
