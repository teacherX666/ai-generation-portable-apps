"""自由创作前端的**离线可视化**校验台（不连 Portal、不产生任何费用）。

为什么需要它：Portal 是 HTTPS + 登录态，而本机对 9090 截图会超时（CLAUDE.local 记过）。
要确认「用这条样片出成片」这个新按钮的**排布**（`.fc-preview-actions` 从两个按钮变三个、
容器改成了 flex + gap），只能把前端原样端到一个普通 HTTP 端口上截图看。

做法：把 `portal/static/` 当静态根，同时把前端要的几个接口打成桩（模型 schema、历史、
鉴权、jobs 轮询）。**只读代码、不写任何 state**，跑完直接 Ctrl+C。

用法：python _diag/free_creation_preview_harness.py [port]
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "portal" / "static"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799

SCHEMAS = {
    "/seedance/api/schema": {
        "ok": True,
        "providers": {
            "comfyui_local": {
                "label": "本地 ComfyUI（免费）",
                "models": [{"id": "minimax_h3_all_reference", "label": "MiniMax H3 / 海螺 H3 全能参考视频（免费）",
                            "duration_range": [4, 12], "resolutions": ["480p", "720p"]}],
                "defaults": {},
            },
            "volcengine": {
                "label": "豆包官方 / 火山方舟",
                "models": [
                    {"id": "doubao-seedance-2-0-260128", "label": "Seedance 2.0（最高 4k）",
                     "duration_range": [4, 15], "resolutions": ["480p", "720p", "1080p", "4k"],
                     "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"]},
                    {"id": "doubao-seedance-2-5-260628", "label": "Seedance 2.5（最长 30s / 50 素材）",
                     "duration_range": [4, 30], "resolutions": ["480p", "720p"],
                     "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"],
                     "capabilities": {"draft": True}, "defaults": {"duration": 10}},
                ],
                "defaults": {},
            },
        },
    },
    "/nano-banana/api/schema": {
        "ok": True,
        "providers": {"comfyui_local": {"label": "本地 ComfyUI（免费）",
                                        "models": [{"id": "auto", "label": "自动"}], "defaults": {}}},
    },
    "/volcengine-portrait/api/config": {"ok": True, "local_gateway": {"modules": []}},
    "/api/platform/history": {"ok": True, "items": []},
    "/api/auth/me": {"ok": True, "username": "harness", "role": "admin"},
    "/seedance/api/jobs": {"ok": True, "jobs": []},
    "/nano-banana/api/jobs": {"ok": True, "jobs": []},
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音访问日志
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in SCHEMAS:
            self._json(200, SCHEMAS[path])
            return
        if path == "/" or path == "":
            self._redirect("/free-creation/index.html")
            return
        relative = path.lstrip("/")
        candidate = (STATIC / relative).resolve()
        if STATIC in candidate.parents or candidate == STATIC:
            if candidate.is_file():
                mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(candidate.suffix, "application/octet-stream")
                self._send(200, candidate.read_bytes(), mime)
                return
        self._json(404, {"ok": False, "error": f"harness 没有这个桩：{path}"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._json(200, {"ok": True, "job_id": "harness-job"})

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    print(f"free-creation 校验台: http://127.0.0.1:{PORT}/free-creation/index.html")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
