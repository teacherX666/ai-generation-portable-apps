"""运行（run）持久化：每次全流程一个目录，逐阶段落盘，断链可续。

目录布局：
    state/runs/<run_id>/run.json          运行元数据 + 各阶段状态
    state/runs/<run_id>/01-concept.md     阶段产物（文件名 = Stage.artifact）

并发模型（2026-09-30 重写，起因是一次真实故障）：
    现象：UI 轮询期间 `os.replace(run.json.tmp -> run.json)` 抛
          `PermissionError(13, 拒绝访问)`，整次运行中断。
    复现：同目录下一个线程持续 `read_text`、另一个线程持续 `os.replace`，
          400 次里稳定失败数百次 —— 这台机器上读者持有目标文件句柄时
          MoveFileEx 会被拒。
    根因有两条，都在这层：
      1. 读没加锁（HTTP 线程读，runner 线程写）；
      2. 写入方持有自己那份 dict 快照写回，会覆盖别处的更新（丢更新）。
    修法：所有状态读写一律走同一把锁，且**写入一律 "重新读盘 → 改 → 写回"**，
    调用方不再持有可写快照；`os.replace` 再加有限重试兜住杀软瞬时占用。
"""
from __future__ import annotations

import json
import os
import random
import re
import string
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .stages import STAGE_BY_ID, STAGE_ORDER

RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]{6,64}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")

TERMINAL = {"succeeded", "failed", "cancelled", "partial", "interrupted"}
ACTIVE = {"pending", "running"}

_REPLACE_ATTEMPTS = 8


def new_run_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    return f"{stamp}-{suffix}"


def _atomic_write(path: Path, text: str) -> None:
    """原子写 + Windows 瞬时限用重试。

    newline="\\n" 显式关闭 Windows 的 \\n → \\r\\n 转换：产物要与 run.json 里的
    chars 计数一致，也避免同一个文件在不同工具里行尾不一致。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    last: Exception | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # 杀软 / 索引器瞬时占用
            last = exc
            time.sleep(0.02 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise last if last else OSError(f"无法写入 {path}")


class RunStore:
    """线程安全的 run 存储。runner 单线程跑任务，HTTP 线程只读写状态。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---------- 路径 ----------

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id or ""):
            raise ValueError("非法的 run id")
        return self.root / run_id

    def artifact_path(self, run_id: str, filename: str) -> Path:
        if not SAFE_NAME_RE.match(filename or ""):
            raise ValueError("非法的产物文件名")
        return self.run_dir(run_id) / filename

    # ---------- 内部（调用方必须已持锁） ----------

    def _read(self, run_id: str) -> dict[str, Any] | None:
        path = self.run_dir(run_id) / "run.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write(self, run: dict[str, Any]) -> None:
        run["updated_at"] = time.time()
        _atomic_write(self.run_dir(run["id"]) / "run.json", json.dumps(run, ensure_ascii=False, indent=2))

    # ---------- 读写 ----------

    def create(self, *, topic: str, mode: str, stage_ids: list[str]) -> dict[str, Any]:
        with self._lock:
            run_id = new_run_id()
            self.run_dir(run_id).mkdir(parents=True, exist_ok=False)
            now = time.time()
            run: dict[str, Any] = {
                "id": run_id,
                "created_at": now,
                "updated_at": now,
                "topic": topic.strip(),
                "mode": mode,
                "stage_ids": list(stage_ids),
                "status": "pending",
                "current_stage": stage_ids[0] if stage_ids else "",
                "stages": {
                    sid: {
                        "status": "pending",
                        "artifact": STAGE_BY_ID[sid].artifact,
                        "provider": "",
                        "model": "",
                        "chars": 0,
                        "started_at": 0,
                        "finished_at": 0,
                        "error": "",
                    }
                    for sid in stage_ids
                },
                "events": [f"{time.strftime('%H:%M:%S')} 运行创建（{len(stage_ids)} 个阶段）"],
            }
            self._write(run)
            return run

    def load(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(run_id)

    def mutate(self, run_id: str, fn: Callable[[dict[str, Any]], Any]) -> dict[str, Any] | None:
        """在锁内「重新读盘 → 应用 fn → 写回」，避免丢更新。

        fn 直接改 run 字典；返回值被忽略（调用方拿返回的 run）。
        """
        with self._lock:
            run = self._read(run_id)
            if run is None:
                return None
            fn(run)
            self._write(run)
            return run

    def mark_stage(self, run_id: str, stage_id: str, **fields: Any) -> dict[str, Any] | None:
        def apply(run: dict[str, Any]) -> None:
            entry = run.setdefault("stages", {}).setdefault(stage_id, {})
            entry.update(fields)

        return self.mutate(run_id, apply)

    def set_fields(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        def apply(run: dict[str, Any]) -> None:
            run.update(fields)

        return self.mutate(run_id, apply)

    def log_event(self, run_id: str, message: str, **fields: Any) -> dict[str, Any] | None:
        def apply(run: dict[str, Any]) -> None:
            run.update(fields)
            events = run.setdefault("events", [])
            events.append(f"{time.strftime('%H:%M:%S')} {message}")
            run["events"] = events[-200:]

        return self.mutate(run_id, apply)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for d in sorted(self.root.iterdir()) if self.root.exists() else []:
                if not d.is_dir():
                    continue
                run = self._read(d.name)
                if not run:
                    continue
                total = len(run.get("stage_ids") or [])
                done = sum(
                    1 for s in (run.get("stages") or {}).values() if s.get("status") == "done"
                )
                out.append(
                    {
                        "id": run.get("id", d.name),
                        "topic": run.get("topic", ""),
                        "mode": run.get("mode", ""),
                        "status": run.get("status", ""),
                        "created_at": run.get("created_at", 0),
                        "done": done,
                        "total": total,
                    }
                )
            out.sort(key=lambda r: r["created_at"], reverse=True)
            return out[:limit]

    # ---------- 产物 ----------

    def write_artifact(self, run_id: str, filename: str, text: str) -> int:
        path = self.artifact_path(run_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            _atomic_write(path, text)
        return len(text)

    def read_artifact(self, run_id: str, filename: str) -> str:
        path = self.artifact_path(run_id, filename)
        if not path.exists():
            return ""
        with self._lock:
            return path.read_text(encoding="utf-8", errors="replace")

    # ---------- 出片（本地 MiniMax H3）镜头状态 ----------

    def shots_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "shots.json"

    def shot_video_path(self, run_id: str, index: int) -> Path:
        number = self._shot_number(index)
        return self.run_dir(run_id) / "shots" / f"shot-{number:02d}.mp4"

    def shot_image_path(self, run_id: str, index: int, slot: int) -> Path:
        number = self._shot_number(index)
        return self.run_dir(run_id) / "shots" / f"shot-{number:02d}-ref{slot}.bin"

    @staticmethod
    def _shot_number(index: Any) -> int:
        try:
            number = int(index)
        except (TypeError, ValueError):
            raise ValueError("非法的镜头号") from None
        if not 1 <= number <= 999:
            raise ValueError("非法的镜头号")
        return number

    def read_shots(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            path = self.shots_path(run_id)
            if not path.exists():
                return {}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}

    def update_shot(self, run_id: str, index: int, **fields: Any) -> dict[str, Any]:
        """锁内「重读 → 改 → 写回」；与 run.json 同样的并发纪律。"""
        number = self._shot_number(index)
        with self._lock:
            path = self.shots_path(run_id)
            if not path.exists():
                self.run_dir(run_id).mkdir(parents=True, exist_ok=True)
                data: dict[str, Any] = {}
            else:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
            entry = data.setdefault(str(number), {})
            entry.update(fields)
            entry["updated_at"] = time.time()
            _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
            return entry

    def shots_in_flight(self, run_id: str) -> list[int]:
        out: list[int] = []
        for key, entry in (self.read_shots(run_id) or {}).items():
            status = str((entry or {}).get("status") or "").lower()
            if status in ("submitted", "pending", "running", "querying") and (entry or {}).get("job_id"):
                try:
                    out.append(int(key))
                except (TypeError, ValueError):
                    continue
        return sorted(out)

    def runs_with_shots_in_flight(self) -> list[str]:
        out: list[str] = []
        with self._lock:
            names = [d.name for d in sorted(self.root.iterdir())] if self.root.exists() else []
        for name in names:
            if not RUN_ID_RE.match(name or ""):
                continue
            try:
                if self.shots_in_flight(name):
                    out.append(name)
            except Exception:
                continue
        return out


    def delete(self, run_id: str) -> bool:
        with self._lock:
            d = self.run_dir(run_id)
            if not d.exists():
                return False
            for child in sorted(d.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                else:
                    child.rmdir()
            d.rmdir()
            return True

    # ---------- 启动对账 ----------

    def reconcile_on_start(self) -> int:
        """把上一次进程死掉时留在 running/pending 的运行标成 interrupted。

        子应用被 Portal 看门狗重启、或进程崩溃时，内存里的执行线程随之消失，
        但磁盘上仍写着 running —— 不清理的话 UI 会永远显示「运行中」，
        续跑按钮也不会出现。
        """
        fixed = 0
        with self._lock:
            for d in sorted(self.root.iterdir()) if self.root.exists() else []:
                if not d.is_dir():
                    continue
                run = self._read(d.name)
                if not run or run.get("status") not in ACTIVE:
                    continue
                for sid, entry in (run.get("stages") or {}).items():
                    if entry.get("status") == "running":
                        entry["status"] = "failed"
                        entry["error"] = entry.get("error") or "进程重启中断（可点「从失败处续跑」）"
                        entry["finished_at"] = entry.get("finished_at") or time.time()
                run["status"] = "interrupted"
                run["current_stage"] = ""
                events = run.setdefault("events", [])
                events.append(f"{time.strftime('%H:%M:%S')} 检测到上次进程中断，已标记为可续跑")
                run["events"] = events[-200:]
                self._write(run)
                fixed += 1
        return fixed


def resolve_stage_ids(requested: list[str] | None) -> list[str]:
    """把请求里的阶段列表规范化成 STAGE_ORDER 的顺序子集。

    缺省 = 全部 8 个阶段（可选阶段也包含，由执行方按 optional 决定跳过与否）。
    非法/未知 id 直接忽略，而不是报错 —— 前端传参不该让整次运行失败。
    """
    if not requested:
        return list(STAGE_ORDER)
    wanted = {str(s).strip() for s in requested if str(s).strip()}
    return [sid for sid in STAGE_ORDER if sid in wanted]


def next_pending_stage(run: dict[str, Any]) -> str:
    """第一个未完成的阶段 id（用于续跑）。"""
    for sid in run.get("stage_ids") or []:
        if (run.get("stages", {}).get(sid) or {}).get("status") != "done":
            return sid
    return ""


def upstream_texts(store: RunStore, run_id: str, stage_inputs: tuple[str, ...]) -> dict[str, str]:
    """取上游阶段的产物正文。阶段产物文件名以**磁盘现状**为准（可能是人工编辑过的）。"""
    run = store.load(run_id) or {}
    out: dict[str, str] = {}
    for sid in stage_inputs:
        entry = (run.get("stages") or {}).get(sid) or {}
        filename = entry.get("artifact") or (STAGE_BY_ID[sid].artifact if sid in STAGE_BY_ID else "")
        if not filename:
            continue
        text = store.read_artifact(run_id, filename)
        if text:
            out[sid] = text
    return out
