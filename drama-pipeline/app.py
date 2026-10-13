#!/usr/bin/env python3
"""AI 短剧自动化全流程（drama-pipeline）—— Portal 子应用。

一个贯穿「概念锚定 → 角色圣经 → 世界观 → 分场大纲 → 剧本 → 角色资产 →
表演设计 → 视频提示词」8 阶段的全流程工作台。每个阶段的 system prompt
直接来自 `skills/` 里的技能正文（AI随风弹性工作流 / 工业级分镜脚本 V3 /
H3 系列 / 打斗特效 / 官方 H3 技能包），阶段产物逐盘落盘、可编辑、可重跑。

端口：PORT 环境变量，默认 8903。Portal 以 iframe 挂载到 /drama-pipeline/。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipeline import local_h3  # noqa: E402
from pipeline import stages as stages_mod  # noqa: E402
from pipeline.generator import LocalGenerator  # noqa: E402
from pipeline.runner import PipelineRunner, load_llm_key  # noqa: E402
from pipeline.store import RunStore, resolve_stage_ids  # noqa: E402

STATE_DIR = APP_ROOT / "state"
STATIC_DIR = APP_ROOT / "static"
RUNS_DIR = STATE_DIR / "runs"

APP_NAME = "AI 短剧自动化全流程"
DEFAULT_PORT = int(os.environ.get("PORT", "8903"))
DEFAULT_HOST = os.environ.get("HOST", "127.0.0.1")
CORS_ENABLED = os.environ.get("CORS", "") == "1"

STORE = RunStore(RUNS_DIR)
RUNNER = PipelineRunner(STORE)
GENERATOR = LocalGenerator(STORE)

MAX_JSON_BODY = 40 * 1024 * 1024   # 参考图走 base64，给足空间（约 30MB 原图）
MAX_ARTIFACT_SAVE = 1 * 1024 * 1024
CHUNK = 256 * 1024


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(SimpleHTTPRequestHandler):
    server_version = "DramaPipeline/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    # ---------- 基础设施 ----------

    def log_message(self, fmt, *args):  # 保持 stdout 简洁，Portal 会把日志收进 logs/
        sys.stdout.write("[drama-pipeline] " + (fmt % args) + "\n")
        sys.stdout.flush()

    def _send_json(self, payload: dict, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if CORS_ENABLED:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_JSON_BODY:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Workspace-Id, X-Requested-With")
        self.end_headers()

    # ---------- 路由 ----------

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            return self._send_json({"ok": True, "service": "drama-pipeline"})
        if path == "/api/config":
            return self._handle_config()
        if path == "/api/skills":
            return self._handle_skills()
        if path == "/api/local":
            return self._send_json({"ok": True, "local_h3": local_h3.health_snapshot(timeout=4)})
        if path in ("/api/runs", "/api/jobs"):
            return self._send_json({"ok": True, "runs": STORE.list_runs()})
        if path.startswith("/api/jobs/"):
            return self._handle_job_get(path)
        if path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/jobs":
            return self._handle_job_create()
        if path.startswith("/api/jobs/"):
            return self._handle_job_action(path)
        return self._send_json({"ok": False, "error": "未知接口"}, status=404)

    def do_PUT(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/jobs/"):
            return self._handle_artifact_save(path)
        return self._send_json({"ok": False, "error": "未知接口"}, status=404)

    # ---------- /api/config ----------

    def _handle_config(self) -> None:
        key, source = load_llm_key()
        skills = stages_mod.load_skills()
        skills_payload = [
            {
                "id": s.id,
                "title": s.title,
                "role": s.role,
                "file": s.rel_path,
                "stages": list(s.stages),
                "exists": s.path.exists(),
                "chars": s.path.stat().st_size if s.path.exists() else 0,
            }
            for s in skills
        ]
        return self._send_json(
            {
                "ok": True,
                "app": APP_NAME,
                "port": self.server.server_address[1],
                "stages": stages_mod.stages_payload(),
                "modes": list(stages_mod.MODES),
                "skills": skills_payload,
                "llm": {
                    "provider": RUNNER.provider,
                    "local_first": RUNNER.local_first,
                    "has_key": bool(key),
                    "key_source": source,
                },
                "local_h3": {
                    "provider": local_h3.LOCAL_PROVIDER,
                    "model_kind": local_h3.H3_MODEL_KIND,
                    "cloud_used": False,
                    "defaults": dict(local_h3.H3_DEFAULTS),
                    "durations": [dict(d) for d in local_h3.DURATION_PRESETS],
                    "ratios": [dict(r) for r in local_h3.RATIO_PRESETS],
                    "mode_labels": dict(local_h3.MODE_LABELS),
                    "limits": {
                        "images": local_h3.MAX_REF_IMAGES,
                        "videos": local_h3.MAX_REF_VIDEOS,
                        "audios": local_h3.MAX_REF_AUDIOS,
                    },
                },
                "busy_run": RUNNER.busy_run,
            }
        )

    def _handle_skills(self) -> None:
        return self._send_json(
            {
                "ok": True,
                "skills": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "role": s.role,
                        "file": s.rel_path,
                        "stages": list(s.stages),
                        "exists": s.path.exists(),
                    }
                    for s in stages_mod.load_skills()
                ],
            }
        )

    # ---------- 运行创建（唯一上报用量的入口） ----------

    def _handle_job_create(self) -> None:
        data = self._read_json()
        topic = str(data.get("topic") or "").strip()
        if not topic:
            return self._send_json({"ok": False, "error": "请先填写项目主题"}, status=400)
        mode = str(data.get("mode") or "short_drama")
        if mode not in stages_mod.MODE_BY_ID:
            mode = "short_drama"
        stage_ids = resolve_stage_ids(data.get("stages"))
        if not stage_ids:
            return self._send_json({"ok": False, "error": "至少要选择一个阶段"}, status=400)

        run = STORE.create(topic=topic[:2000], mode=mode, stage_ids=stage_ids)
        RUNNER.submit(run["id"])
        # X-Job-Id 是 Portal 用量统计的唯一凭据。只有「创建运行」返回它；
        # 续跑/重跑/取消/保存产物一律不发，否则会重复计数。
        return self._send_json(
            {"ok": True, "run_id": run["id"], "run": run},
            headers={"X-Job-Id": run["id"]},
        )

    # ---------- 运行读取 ----------

    def _split_job_path(self, path: str) -> tuple[str, list[str]]:
        rest = path[len("/api/jobs/"):].strip("/")
        parts = [urllib.parse.unquote(p) for p in rest.split("/") if p]
        return (parts[0] if parts else ""), parts[1:]

    def _handle_job_get(self, path: str) -> None:
        run_id, tail = self._split_job_path(path)
        if tail and tail[0] == "artifacts":
            return self._serve_artifact(run_id, "/".join(tail[1:]))
        if tail and tail[0] == "shots":
            if len(tail) == 1:
                if self._load_run(run_id) is None:
                    return
                return self._send_json(GENERATOR.shots_view(run_id))
            if len(tail) == 3 and tail[2] == "video":
                return self._serve_shot_video(run_id, tail[1])
            return self._send_json({"ok": False, "error": "未知接口"}, status=404)

        run = self._load_run(run_id)
        if run is None:
            return
        return self._send_json({"ok": True, "run": run})

    # ---------- 出片：成片回放（带 Range，<video> 才画得出首帧、拖得动进度） ----------

    def _serve_shot_video(self, run_id: str, index: str) -> None:
        try:
            path = STORE.shot_video_path(run_id, index)
        except ValueError as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=400)
        if not path.exists() or path.stat().st_size <= 0:
            return self._send_json({"ok": False, "error": "该镜头还没有成片"}, status=404)

        size = path.stat().st_size
        start, end = 0, size - 1
        partial = False
        header = (self.headers.get("Range") or "").strip()
        if header.lower().startswith("bytes="):
            spec = header[6:].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = max(0, int(first))
                    end = int(last) if last else size - 1
                elif last:                      # bytes=-N 取末尾 N 字节
                    start = max(0, size - int(last))
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True
            except ValueError:
                start, end, partial = 0, size - 1, False

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if CORS_ENABLED:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            return
        remaining = length
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return          # 播放器拖动进度时会主动断开，不是错误
                remaining -= len(chunk)

    def _load_run(self, run_id: str) -> dict | None:
        """读一个 run；run_id 非法或不存在时已回好错误响应，返回 None。"""
        try:
            run = STORE.load(run_id)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return None
        if not run:
            self._send_json({"ok": False, "error": "运行不存在"}, status=404)
            return None
        return run

    def _serve_artifact(self, run_id: str, filename: str) -> None:
        try:
            path = STORE.artifact_path(run_id, filename)
        except ValueError as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=400)
        if not path.exists():
            return self._send_json({"ok": False, "error": "产物不存在"}, status=404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if CORS_ENABLED:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 运行动作（一律不发 X-Job-Id） ----------

    def _handle_job_action(self, path: str) -> None:
        run_id, tail = self._split_job_path(path)
        if not run_id or not tail:
            return self._send_json({"ok": False, "error": "未知接口"}, status=404)
        run = self._load_run(run_id)
        if run is None:
            return

        action = tail[0]
        if action == "resume" and len(tail) == 1:
            if run.get("status") not in ("failed", "partial", "pending", "interrupted"):
                return self._send_json({"ok": False, "error": "该运行无需续跑"}, status=400)
            RUNNER.submit(run_id)
            return self._send_json({"ok": True, "run_id": run_id})

        if action == "cancel" and len(tail) == 1:
            if run.get("status") in ("succeeded", "failed", "cancelled", "partial", "interrupted"):
                return self._send_json({"ok": True, "run_id": run_id, "status": run["status"]})
            STORE.log_event(
                run_id, "已请求取消（当前阶段跑完后停止）", status="cancelled", current_stage=""
            )
            return self._send_json({"ok": True, "run_id": run_id, "status": "cancelled"})

        if action == "stages" and len(tail) == 3 and tail[2] == "rerun":
            ok, message = RUNNER.rerun_stage(run_id, tail[1])
            return self._send_json({"ok": ok, "message": message}, status=200 if ok else 400)

        # ---------- 出片（本地 MiniMax H3） ----------
        # 这几个接口**一律不发 X-Job-Id**：出片是运行内部的子任务，
        # 用量在「创建运行」时已经计过一次，重复上报会让统计虚高。
        if action == "shots" and len(tail) >= 2:
            index = tail[1]
            # 整轮同步：POST /api/jobs/<id>/shots/refresh
            if len(tail) == 2 and tail[1] == "refresh":
                sync = GENERATOR.sync_run(run_id)
                view = GENERATOR.shots_view(run_id)
                return self._send_json({"ok": True, "sync": sync, "shots": view["shots"],
                                        "parsed": view["parsed"]})
            if len(tail) == 3 and tail[2] == "generate":
                data = self._read_json()
                try:
                    result = GENERATOR.submit(run_id, index, data)
                except local_h3.LocalH3Error as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, status=400)
                except ValueError as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, status=400)
                return self._send_json(result)
            if len(tail) == 3 and tail[2] == "cancel":
                try:
                    result = GENERATOR.cancel(run_id, index)
                except ValueError as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, status=400)
                return self._send_json(result, status=200 if result.get("ok") else 400)
            if len(tail) == 3 and tail[2] == "refresh":
                try:
                    sync = GENERATOR.sync_run(run_id)
                except ValueError as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, status=400)
                view = GENERATOR.shots_view(run_id)
                return self._send_json({"ok": True, "sync": sync, "shots": view["shots"],
                                        "parsed": view["parsed"]})
            return self._send_json({"ok": False, "error": "未知接口"}, status=404)

        return self._send_json({"ok": False, "error": "未知接口"}, status=404)

    def _handle_artifact_save(self, path: str) -> None:
        run_id, tail = self._split_job_path(path)
        if not run_id or len(tail) != 3 or tail[0] != "stages" or tail[2] != "artifact":
            return self._send_json({"ok": False, "error": "未知接口"}, status=404)
        stage_id = tail[1]
        run = self._load_run(run_id)
        if run is None:
            return
        stage = stages_mod.STAGE_BY_ID.get(stage_id)
        if stage is None or stage_id not in (run.get("stage_ids") or []):
            return self._send_json({"ok": False, "error": "该阶段不在本次运行里"}, status=400)
        data = self._read_json()
        content = str(data.get("content") or "")
        if len(content) > MAX_ARTIFACT_SAVE:
            return self._send_json({"ok": False, "error": "产物过大（上限 1MB）"}, status=400)
        chars = STORE.write_artifact(run_id, stage.artifact, content)
        STORE.mark_stage(run_id, stage_id, status="done", chars=chars, provider="manual", model="manual")
        # 人工改过产物后，下游阶段的旧产物已过期 —— 提示但不自动重跑（用户可能只想改一处）。
        downstream = [s for s in (run.get("stage_ids") or [])[((run.get("stage_ids") or []).index(stage_id) + 1):]
                      if (run.get("stages", {}).get(s) or {}).get("status") == "done"]
        suffix = f"；下游 {len(downstream)} 个阶段产物已过期，建议重跑" if downstream else ""
        STORE.log_event(run_id, f"手动保存阶段 {stage.order:02d} {stage.title}（{chars} 字）{suffix}")
        return self._send_json({"ok": True, "chars": chars, "stale_downstream": downstream})


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    interrupted = STORE.reconcile_on_start()
    RUNNER.start()
    GENERATOR.start()

    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), Handler)
    print(f"{APP_NAME} at http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    skills = stages_mod.load_skills()
    missing = [s.id for s in skills if not s.path.exists()]
    print(f"  stages: {len(stages_mod.STAGES)}  skills: {len(skills)}"
          + (f"  missing: {', '.join(missing)}" if missing else ""))
    if interrupted:
        print(f"  reconciled interrupted runs: {interrupted}")
    local = local_h3.health(timeout=3)
    print("  local H3: " + ("ready" if local["ready"] else f"NOT READY ({local['error']})")
          + f"  {local['base_url']}  provider={local['provider']}  cloud_used=False")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        RUNNER.stop()
        GENERATOR.stop()


if __name__ == "__main__":
    main()
