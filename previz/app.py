#!/usr/bin/env python3
"""分镜布局子应用：项目/镜头存档 + 渲染图存取 + zip 导出。

与 director 同款 stdlib 骨架；由 portal 按 apps.json 拉起
（env: PORT / HOST / CORS / DATA_DIR）。无模型调用、无 API key、
不发 X-Job-Id（Portal 统计白名单不含本应用路径，计数不受影响）。
"""
from __future__ import annotations

import cgi
import functools
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
_DATA_BASE = Path(os.environ.get("DATA_DIR", str(ROOT)))
STATE_DIR = _DATA_BASE / "state"
PROJECTS_DIR = STATE_DIR / "projects"
STATIC_DIR = ROOT / "static"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", "8896"))
HOST = os.environ.get("HOST", "127.0.0.1")
CORS = os.environ.get("CORS") == "1"

_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FILE_RE = re.compile(r"^s_[A-Za-z0-9_-]{1,64}_(render|thumb)\.png$")
MAX_RENDER_BYTES = 20 * 1024 * 1024


def _zip_entry_name(name: str) -> str:
    """zip 归档名消毒：去路径分隔/前导点，非字符合并，防 zip-slip。

    注意 lstrip 必须连 `_` 一起剥：`[\\/]→_` 先执行时 `../../evil` 会先变
    `.._.._evil`，只 lstrip(".") 后首字符仍是下划线，遍历残留点没除净。
    """
    clean = re.sub(r"[\\/]", "_", str(name)).lstrip("._")
    clean = re.sub(r"[^\w一-鿿-]+", "_", clean)[:60]
    return clean or "shot"

DEFAULT_CAMERA = {"position": [0, 3.2, 12], "target": [0, 1.0, 0], "fov": 50,
                  "shot_size": "中景", "azimuth": 0, "elevation": 15}


def valid_id(s: str) -> bool:
    return bool(s) and bool(_ID_RE.fullmatch(s))


def new_project(name: str, creator_ip: str = "") -> dict[str, Any]:
    return {
        "id": "p_" + uuid.uuid4().hex[:10],
        "name": (name or "未命名项目").strip()[:60],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_by_ip": creator_ip or "",
        "shots": [],
    }


def _project_path(pid: str) -> Path:
    return PROJECTS_DIR / pid / "project.json"


def validate_project(data: Any) -> dict[str, Any] | None:
    """最小校验 + 字段补默认。返回规范化后的 dict；非法返回 None。"""
    if not isinstance(data, dict):
        return None
    if not valid_id(str(data.get("id", ""))) or not isinstance(data.get("name"), str):
        return None
    if not isinstance(data.get("shots"), list):
        return None
    shots = []
    for shot in data["shots"]:
        if not isinstance(shot, dict) or not isinstance(shot.get("id"), str):
            return None
        camera = dict(DEFAULT_CAMERA)
        if isinstance(shot.get("camera"), dict):
            camera.update(shot["camera"])
        try:
            order = int(shot.get("order") or 0)
        except (TypeError, ValueError):
            order = 0  # 非数字 order 回退 0，避免 PUT 崩 500
        shots.append({
            "id": shot["id"],
            "name": str(shot.get("name") or shot["id"])[:60],
            "order": order,
            "aspect": str(shot.get("aspect") or "16:9"),
            "camera": camera,
            "characters": shot.get("characters") if isinstance(shot.get("characters"), list) else [],
            "props": shot.get("props") if isinstance(shot.get("props"), list) else [],
            "thumbnail": str(shot.get("thumbnail") or "") if _FILE_RE.fullmatch(str(shot.get("thumbnail") or "")) else "",
            "render": str(shot.get("render") or "") if _FILE_RE.fullmatch(str(shot.get("render") or "")) else "",
            "notes": str(shot.get("notes") or ""),
        })
    return {
        "id": str(data["id"]),
        "name": data["name"][:60],
        "created_at": str(data.get("created_at") or ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_by_ip": str(data.get("created_by_ip") or ""),
        "shots": shots,
    }


def save_project(project: dict[str, Any]) -> bool:
    pid = str(project.get("id", ""))
    if not valid_id(pid):
        return False
    d = PROJECTS_DIR / pid
    with _LOCK:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "project.json.tmp"
        tmp.write_text(json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(d / "project.json")
    return True


def load_project(pid: str) -> dict[str, Any] | None:
    if not valid_id(pid):
        return None
    p = _project_path(pid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 损坏兜底：备份重命名，返回 None（前端重建空项目并 toast）
        backup = p.with_name("project.json.broken-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            p.rename(backup)
        except OSError:
            pass
        return None


def list_projects() -> list[dict[str, Any]]:
    out = []
    if not PROJECTS_DIR.exists():
        return out
    with _LOCK:
        for d in sorted(PROJECTS_DIR.iterdir()):
            if not d.is_dir():
                continue
            p = load_project(d.name)
            if p is None:
                continue
            out.append({
                "id": p["id"], "name": p["name"], "updated_at": p.get("updated_at", ""),
                "shot_count": len(p["shots"]),
                "thumbnail": (p["shots"][0].get("thumbnail") or "") if p["shots"] else "",
            })
    return out


def count_broken_projects() -> int:
    """统计存在 project.json.broken-* 备份的目录数（load_project 损坏兜底产物）。"""
    if not PROJECTS_DIR.exists():
        return 0
    with _LOCK:
        return sum(1 for d in PROJECTS_DIR.iterdir()
                   if d.is_dir() and any(d.glob("project.json.broken-*")))


def delete_project(pid: str) -> bool:
    if not valid_id(pid):
        return False
    d = PROJECTS_DIR / pid
    if not d.exists():
        return False
    with _LOCK:
        shutil.rmtree(d)
    return True


def json_response(handler: SimpleHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: SimpleHTTPRequestHandler) -> Any:
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0 or n > 10 * 1024 * 1024:
        return None
    try:
        return json.loads(handler.rfile.read(n).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _client_ip(handler: SimpleHTTPRequestHandler) -> str:
    return (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip() \
        or handler.client_address[0]


def _serve_file(handler: SimpleHTTPRequestHandler, path: Path, content_type: str,
                attachment: str = "") -> None:
    # 渲染图读写可能并发（_handle_render 正在写同一文件），exists+read 一起上锁
    with _LOCK:
        if not path.exists() or not path.is_file():
            json_response(handler, 404, {"error": "文件不存在"})
            return
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            # exists 通过后文件仍可能被 _handle_render 的并发写/清理删掉，
            # 与 export 循环的 except continue 对称容错
            json_response(handler, 404, {"error": "文件不存在"})
            return
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    if attachment:
        handler.send_header("Content-Disposition",
                            f'attachment; filename="{attachment}"')
    handler.end_headers()
    handler.wfile.write(body)


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        # CORS 头统一在状态行之后注入（send_header 会追加到 _headers_buffer，
        # 在 super().do_GET() 之前调用会把头排到状态行前面，产出畸形 HTTP）
        if CORS:
            self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/projects":
            json_response(self, 200, {"projects": list_projects(),
                                      "broken": count_broken_projects()})
            return
        if path.startswith("/api/projects/"):
            pid = path[len("/api/projects/"):]
            if pid.endswith("/export.zip"):
                self._handle_export(pid[: -len("/export.zip")])
                return
            p = load_project(pid)
            if p is None:
                json_response(self, 404, {"error": "项目不存在"})
            else:
                json_response(self, 200, p)
            return
        if path.startswith("/api/files/"):
            rest = path[len("/api/files/"):].split("/", 1)
            if len(rest) == 2 and valid_id(rest[0]) and _FILE_RE.fullmatch(rest[1]):
                self._handle_files(rest[0], rest[1])
            else:
                json_response(self, 404, {"error": "文件不存在"})
            return
        super().do_GET()

    def _handle_files(self, pid: str, filename: str) -> None:
        _serve_file(self, PROJECTS_DIR / pid / filename, "image/png")

    def _handle_export(self, pid: str) -> None:
        p = load_project(pid)
        if p is None:
            json_response(self, 404, {"error": "项目不存在"})
            return
        buf = io.BytesIO()
        with _LOCK:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for shot in p["shots"]:
                    f = PROJECTS_DIR / pid / str(shot.get("render") or "")
                    # f.parent 防线：Path 保留 .. 组件，只查 f.name 拦不住
                    # render="../s_evil_render.png"（可把 state 根目录文件打包装 zip）
                    if _FILE_RE.fullmatch(f.name) and f.parent == (PROJECTS_DIR / pid) \
                            and f.exists():
                        try:
                            order = int(shot.get("order") or 0)
                        except (TypeError, ValueError):
                            order = 0  # 非整数 order 回退 0，避免 :02d 抛 500
                        try:
                            zf.write(f, arcname=f"{order:02d}-{_zip_entry_name(shot['name'])}.png")
                        except FileNotFoundError:
                            continue  # render 与 export 之间文件被删，跳过该镜头
        body = buf.getvalue()
        # 中文文件名不能直接进 Content-Disposition（latin-1 头编码会炸）：
        # ASCII 回退名 + RFC 5987 filename*（percent-encoded）双保险。
        fname = f"{p['name']}-分镜快照.zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"storyboard-{p['id']}.zip\"; "
                         f"filename*=UTF-8''{quote(fname, safe='')}")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/projects":
            body = _read_json_body(self)
            name = (body or {}).get("name") if isinstance(body, dict) else None
            if not name:
                json_response(self, 400, {"error": "缺少项目名"})
                return
            p = new_project(str(name), _client_ip(self))
            save_project(p)
            json_response(self, 201, p)
            return
        if path.startswith("/api/projects/") and path.endswith("/render"):
            form = cgi.FieldStorage(
                fp=self.rfile, headers=self.headers,
                environ={"REQUEST_METHOD": "POST",
                         "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                         "CONTENT_LENGTH": self.headers.get("Content-Length", "")})
            self._handle_render(path[len("/api/projects/"): -len("/render")], form)
            return
        json_response(self, 404, {"error": "接口不存在"})

    def _handle_render(self, pid: str, form: cgi.FieldStorage) -> None:
        p = load_project(pid)
        if p is None:
            json_response(self, 404, {"error": "项目不存在"})
            return
        shot_id = str(form.getvalue("shot_id", "") or "")
        # shot_id 会拼进文件名（s_{shot_id}_render.png），必须过 id 白名单防路径穿越
        if not shot_id or not valid_id(shot_id) \
                or not any(s["id"] == shot_id for s in p["shots"]):
            json_response(self, 400, {"error": "镜头不存在"})
            return
        render = form["render"] if "render" in form else None
        thumb = form["thumb"] if "thumb" in form else None
        if render is None or not render.file:
            json_response(self, 400, {"error": "缺少渲染图"})
            return
        render_bytes = render.file.read(MAX_RENDER_BYTES + 1)
        if not render_bytes or len(render_bytes) > MAX_RENDER_BYTES:
            json_response(self, 413, {"error": "渲染图为空或超过 20MB"})
            return
        thumb_bytes = None
        if thumb is not None and thumb.file:
            thumb_bytes = thumb.file.read(MAX_RENDER_BYTES + 1)
            if len(thumb_bytes) > MAX_RENDER_BYTES:
                thumb_bytes = None
        with _LOCK:
            (PROJECTS_DIR / pid / f"s_{shot_id}_render.png").write_bytes(render_bytes)
            if thumb_bytes:
                (PROJECTS_DIR / pid / f"s_{shot_id}_thumb.png").write_bytes(thumb_bytes)
        for shot in p["shots"]:
            if shot["id"] == shot_id:
                shot["render"] = f"s_{shot_id}_render.png"
                if thumb_bytes:
                    shot["thumbnail"] = f"s_{shot_id}_thumb.png"
        save_project(p)
        json_response(self, 200, {
            "render_url": f"/api/files/{pid}/s_{shot_id}_render.png",
            "thumbnail": f"s_{shot_id}_thumb.png" if thumb_bytes else "",
        })

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/projects/"):
            json_response(self, 404, {"error": "接口不存在"})
            return
        pid = path[len("/api/projects/"):]
        if not valid_id(pid):
            json_response(self, 400, {"error": "项目 id 非法"})
            return
        body = _read_json_body(self)
        project = validate_project(body) if body is not None else None
        if project is None or project["id"] != pid:
            json_response(self, 400, {"error": "项目数据非法"})
            return
        save_project(project)
        json_response(self, 200, {"ok": True})

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/projects/"):
            json_response(self, 404, {"error": "接口不存在"})
            return
        pid = path[len("/api/projects/"):]
        if delete_project(pid):
            json_response(self, 200, {"ok": True})
        else:
            json_response(self, 404, {"error": "项目不存在"})

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        if not CORS:
            super().log_message(fmt, *args)


if __name__ == "__main__":
    # SimpleHTTPRequestHandler.__init__ 只认构造参数 directory（默认 os.getcwd()），
    # 类属性 Handler.directory 是死代码（Python 3.7+）——用 partial 显式传入，
    # 保证静态目录与启动 CWD 无关（portal 以 cwd=previz/ 拉起）
    Handler = functools.partial(Handler, directory=str(STATIC_DIR))  # noqa: N806
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"previz serving {STATIC_DIR} on {HOST}:{PORT} (CORS={CORS})", flush=True)
    server.serve_forever()
