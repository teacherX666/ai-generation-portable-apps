"""出片编排：把第 8 阶段的镜头交给**本机** MiniMax H3，并把成片搬回运行目录。

职责边界
--------
- generator 只认「本地」。所有提交都经 `local_h3`，而 `local_h3` 里 provider 写死
  `comfyui_local`。这条链路上不存在任何会产生云费用的分支。
- 成片**必须落盘到 state/runs/<id>/shots/`**：页面关掉、浏览器刷新、Portal 重启，
  产物都还在，重开就能看。所以除了 HTTP 触发的同步，还有一个后台线程兜底同步。
"""
from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from typing import Any

from . import local_h3, shots as shots_mod
from .store import RunStore

VIDEO_STAGE = "video_prompts"
ACTIVE = ("submitted", "pending", "running", "querying")
_DATA_URL = re.compile(r"^data:(?P<mime>[^;,]+)?(?:;charset=[^;,]+)?;base64,(?P<payload>.*)$", re.DOTALL)


def _decode_data_url(data_url: str) -> tuple[str, bytes] | None:
    match = _DATA_URL.match(str(data_url or "").strip())
    if not match:
        return None
    try:
        blob = base64.b64decode(match.group("payload"), validate=False)
    except Exception:
        return None
    if not blob:
        return None
    return (match.group("mime") or "application/octet-stream"), blob


def _encode_data_url(mime: str, blob: bytes) -> str:
    return f"data:{mime or 'application/octet-stream'};base64,{base64.b64encode(blob).decode('ascii')}"


class LocalGenerator:
    def __init__(self, store: RunStore, *, interval: float = 8.0):
        self.store = store
        self.interval = max(3.0, float(interval))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sync_lock = threading.Lock()   # 串行化同步，避免 HTTP 与后台线程撞同一任务
        self.last_tick: float = 0.0
        self.last_error: str = ""

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="drama-local-h3-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for run_id in self.store.runs_with_shots_in_flight():
                    self.sync_run(run_id)
                self.last_tick = time.time()
            except Exception as exc:  # 后台线程绝不因单次异常退出
                self.last_error = str(exc)
            self._stop.wait(self.interval)

    # ---------- 视图 ----------

    def parse_stage_shots(self, run_id: str) -> tuple[list[shots_mod.Shot], bool, str]:
        run = self.store.load(run_id) or {}
        stage = (run.get("stages") or {}).get(VIDEO_STAGE) or {}
        filename = stage.get("artifact") or "08-video-prompts.md"
        text = self.store.read_artifact(run_id, filename)
        parsed, ok = shots_mod.parse_shots(text)
        return parsed, ok, (filename if text else "")

    def shots_view(self, run_id: str) -> dict[str, Any]:
        parsed, parsed_ok, source = self.parse_stage_shots(run_id)
        stored = self.store.read_shots(run_id)
        items: list[dict[str, Any]] = []
        for shot in parsed:
            entry = dict(stored.get(str(shot.index)) or {})
            payload = shot.to_payload()
            started = float(entry.get("started_at") or 0)
            status = str(entry.get("status") or "idle")
            elapsed = float(entry.get("elapsed") or 0)
            if status in ACTIVE and started:
                elapsed = max(elapsed, time.time() - started)
            video = self.store.shot_video_path(run_id, shot.index)
            payload.update(
                {
                    "status": status,
                    "job_id": entry.get("job_id", ""),
                    "mode": entry.get("mode", ""),
                    "mode_label": local_h3.MODE_LABELS.get(entry.get("mode", ""), ""),
                    "error": entry.get("error", ""),
                    "elapsed": round(elapsed, 1),
                    "seed": entry.get("seed", ""),
                    "images": len(entry.get("images") or []),
                    "has_video": video.exists() and video.stat().st_size > 0,
                    "video_bytes": video.stat().st_size if video.exists() else 0,
                    "prompt_override": entry.get("prompt", ""),
                    "params": entry.get("params") or {},
                }
            )
            items.append(payload)
        return {
            "ok": True,
            "run_id": run_id,
            "parsed": parsed_ok,
            "source": source,
            "shots": items,
        }

    # ---------- 参考图持久化 ----------

    def _persist_images(self, run_id: str, index: int, images: list[dict[str, str]]) -> list[dict[str, str]]:
        """把上传的参考图落盘，返回元数据（下次重跑不用再传一次）。"""
        meta: list[dict[str, str]] = []
        for slot, item in enumerate(images[: local_h3.MAX_REF_IMAGES]):
            decoded = _decode_data_url(str((item or {}).get("data_url") or ""))
            if not decoded:
                continue
            mime, blob = decoded
            path = self.store.shot_image_path(run_id, index, slot)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            meta.append(
                {
                    "slot": str(slot),
                    "mime": mime,
                    "filename": str((item or {}).get("filename") or f"ref{slot}"),
                    "bytes": str(len(blob)),
                    "sha256": hashlib.sha256(blob).hexdigest()[:16],
                }
            )
        return meta

    def _load_images(self, run_id: str, index: int, meta: list[dict[str, str]]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in meta or []:
            try:
                slot = int(item.get("slot") or 0)
            except (TypeError, ValueError):
                continue
            path = self.store.shot_image_path(run_id, index, slot)
            if not path.exists():
                continue
            out.append(
                {
                    "data_url": _encode_data_url(str(item.get("mime") or ""), path.read_bytes()),
                    "filename": str(item.get("filename") or f"ref{slot}"),
                }
            )
        return out

    # ---------- 提交 ----------

    def submit(self, run_id: str, index: int, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        run = self.store.load(run_id)
        if not run:
            raise local_h3.LocalH3Error("运行不存在")

        parsed, _ok, _src = self.parse_stage_shots(run_id)
        shot = next((s for s in parsed if s.index == int(index)), None)
        existing = self.store.read_shots(run_id).get(str(int(index))) or {}

        prompt = str(options.get("prompt") or "").strip()
        if not prompt:
            prompt = str(existing.get("prompt") or "").strip()
        if not prompt and shot is not None:
            prompt = shot.prompt
        if not prompt:
            raise local_h3.LocalH3Error(f"镜 {index} 没有可用提示词，请手动填写")

        incoming = options.get("images")
        if incoming:
            meta = self._persist_images(run_id, index, list(incoming))
        else:
            meta = list(existing.get("images") or [])
        images = self._load_images(run_id, index, meta)

        mode = local_h3.normalize_mode(str(options.get("mode") or ""), len(images))
        duration_id = str(options.get("duration_id") or "")
        if not duration_id and shot is not None:
            duration_id = self._nearest_duration(shot.seconds)
        ratio = str(options.get("ratio") or "")
        megapixels = options.get("megapixels")
        try:
            megapixels = float(megapixels) if megapixels not in (None, "") else None
        except (TypeError, ValueError):
            megapixels = None
        second_stage = options.get("second_stage")
        seed = options.get("seed")

        fingerprint = hashlib.sha256(
            "|".join(
                [
                    prompt,
                    mode,
                    str(duration_id),
                    str(ratio),
                    str(megapixels),
                    str(second_stage),
                    *(m.get("sha256", "") for m in meta),
                ]
            ).encode("utf-8")
        ).hexdigest()[:16]

        try:
            result = local_h3.submit(
                prompt=prompt,
                mode=mode,
                images=images,
                request_id=local_h3.request_key(run_id, int(index), fingerprint),
                duration_id=duration_id,
                ratio=ratio,
                megapixels=megapixels,
                second_stage=bool(second_stage) if second_stage is not None else None,
                seed=int(seed) if str(seed or "").strip().lstrip("-").isdigit() else None,
            )
        except local_h3.LocalH3Error as exc:
            self.store.update_shot(
                run_id, index, status="failed", error=str(exc), finished_at=time.time()
            )
            raise

        # 重新提交时先把上一次的成片标记为待覆盖（不删文件，失败还能看旧的）
        self.store.update_shot(
            run_id,
            index,
            status="submitted",
            job_id=result["job_id"],
            deduplicated=result["deduplicated"],
            mode=mode,
            prompt=prompt,
            duration_id=duration_id,
            ratio=ratio,
            images=meta,
            params=result["values"],
            error="",
            seed="",
            elapsed=0,
            started_at=time.time(),
            finished_at=0,
        )
        return {
            "ok": True,
            "run_id": run_id,
            "index": int(index),
            "job_id": result["job_id"],
            "deduplicated": result["deduplicated"],
            "mode": mode,
            "mode_label": local_h3.MODE_LABELS.get(mode, mode),
            "images": len(images),
        }

    @staticmethod
    def _nearest_duration(seconds: float) -> str:
        best = local_h3.DURATION_PRESETS[0]
        best_gap = abs(float(best["num_frames"]) / 24.0 - float(seconds or 4))
        for preset in local_h3.DURATION_PRESETS:
            gap = abs(float(preset["num_frames"]) / 24.0 - float(seconds or 4))
            if gap < best_gap:
                best, best_gap = preset, gap
        return str(best["id"])

    # ---------- 取消 / 同步 ----------

    def cancel(self, run_id: str, index: int) -> dict[str, Any]:
        entry = self.store.read_shots(run_id).get(str(int(index))) or {}
        job_id = str(entry.get("job_id") or "")
        if not job_id or str(entry.get("status") or "") not in ACTIVE:
            return {"ok": False, "error": "该镜头没有在跑的任务"}
        try:
            local_h3.cancel(job_id)
        except local_h3.LocalH3Error as exc:
            return {"ok": False, "error": str(exc)}
        self.store.update_shot(run_id, index, status="cancelled", finished_at=time.time())
        return {"ok": True}

    def sync_run(self, run_id: str) -> dict[str, Any]:
        """把在飞的镜头同步一次；成功的把成片抓回落盘。"""
        with self._sync_lock:
            changed = 0
            in_flight = self.store.shots_in_flight(run_id)
            for index in in_flight:
                entry = self.store.read_shots(run_id).get(str(index)) or {}
                job_id = str(entry.get("job_id") or "")
                if not job_id:
                    continue
                try:
                    job = local_h3.job_status(job_id)
                except local_h3.LocalH3Error as exc:
                    self.store.update_shot(run_id, index, error=str(exc))
                    continue

                started = float(entry.get("started_at") or 0)
                elapsed = round(max(0.0, time.time() - started), 1) if started else 0.0

                if not job["done"]:
                    status = job["status"] or "running"
                    self.store.update_shot(run_id, index, status=status, elapsed=elapsed)
                    changed += 1
                    continue

                if not job["ok"]:
                    self.store.update_shot(
                        run_id,
                        index,
                        status="failed",
                        error=job.get("error") or "本地出片失败",
                        elapsed=elapsed,
                        finished_at=time.time(),
                    )
                    changed += 1
                    continue

                output = local_h3.output_of(job["raw"] if "raw" in job else {})
                if not output and job.get("results"):
                    output = local_h3.output_of({"results": job["results"]})
                dest = self.store.shot_video_path(run_id, index)
                try:
                    if output.get("download_url"):
                        size = local_h3.download_to(output["download_url"], dest)
                    else:
                        raise local_h3.LocalH3Error("任务成功但没有返回成片地址")
                except local_h3.LocalH3Error as exc:
                    self.store.update_shot(
                        run_id,
                        index,
                        status="failed",
                        error=str(exc),
                        elapsed=elapsed,
                        finished_at=time.time(),
                    )
                    changed += 1
                    continue

                self.store.update_shot(
                    run_id,
                    index,
                    status="succeeded",
                    elapsed=elapsed,
                    finished_at=time.time(),
                    error="",
                    seed=output.get("seed", ""),
                    filename=output.get("filename", ""),
                    bytes=size,
                    upstream_path=output.get("local_path", ""),
                )
                changed += 1
            return {"ok": True, "run_id": run_id, "synced": len(in_flight), "changed": changed}
