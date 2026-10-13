"""流水线执行器：串行跑阶段，逐阶段落盘，失败即停并如实记录。

大模型调用一律走 `shared/model_gateway.py`（既有网关，已处理火山方舟
DeepSeek v4 必须显式下发 thinking:disabled 的坑），本模块不新起 HTTP 客户端。

状态一致性：执行器**从不持有可写的 run 快照**。每一步都通过 `store.mark_stage`
/ `store.log_event` 做「锁内重读 → 改 → 写回」，因此与 HTTP 线程的人工保存、
重跑请求不会互相覆盖。
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parent
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared import model_gateway  # noqa: E402

from . import stages as stages_mod  # noqa: E402
from .store import RunStore, next_pending_stage, upstream_texts  # noqa: E402

# 密钥解析顺序：环境变量 → 本模块 state/deepseek.key → 兄弟应用（director/seedance）
# 已有的 state/deepseek.key。最后一条是为了开箱可用 —— 同一台机器上同一个方舟 Key
# 已经在两个应用里落盘，这里复用而不是再复制一份密钥文件。来源会出现在 /api/config。
KEY_ENV_NAMES = ("DEEPSEEK_API_KEY", "ARK_LLM_API_KEY")
SIBLING_KEY_FILES = (
    REPO_ROOT / "director" / "state" / "deepseek.key",
    REPO_ROOT / "seedance" / "state" / "deepseek.key",
)
LOCAL_KEY_FILE = APP_ROOT / "state" / "deepseek.key"


def load_llm_key() -> tuple[str, str]:
    """返回 (key, 来源描述)。"""
    import os

    for name in KEY_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, f"env:{name}"
    if LOCAL_KEY_FILE.exists():
        try:
            value = LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
            if value:
                return value, "state/deepseek.key"
        except Exception:
            pass
    for path in SIBLING_KEY_FILES:
        try:
            if path.exists():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value, str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        except Exception:
            continue
    return "", ""


class PipelineRunner:
    def __init__(self, store: RunStore, *, provider: str = "deepseek", local_first: bool = False):
        self.store = store
        self.provider = provider
        self.local_first = local_first
        self._queue: "queue.Queue[tuple[str, list[str] | None]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running_run: str = ""
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="drama-pipeline-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def busy_run(self) -> str:
        return self._running_run

    def submit(self, run_id: str, stage_ids: list[str] | None = None) -> None:
        self._queue.put((run_id, stage_ids))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_id, stage_ids = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.execute(run_id, stage_ids)
            except Exception as exc:  # 兜底：worker 绝不能因单次运行崩掉
                self.store.log_event(
                    run_id, f"运行异常终止：{exc}", status="failed", current_stage=""
                )
            finally:
                with self._lock:
                    self._running_run = ""
                self._queue.task_done()

    # ---------- 执行 ----------

    def execute(self, run_id: str, stage_ids: list[str] | None = None) -> dict[str, Any]:
        run = self.store.load(run_id)
        if not run:
            return {"ok": False, "error": "运行不存在"}

        all_ids = list(run.get("stage_ids") or [])
        if stage_ids is not None:
            todo = list(stage_ids)
        elif run.get("status") in ("failed", "interrupted", "partial", "pending"):
            # 续跑：从第一个未完成阶段接着走
            nxt = next_pending_stage(run)
            todo = all_ids[all_ids.index(nxt):] if nxt else all_ids
        else:
            todo = all_ids

        with self._lock:
            self._running_run = run_id
        self.store.set_fields(run_id, status="running")

        failed = False
        completed: list[str] = []
        for sid in todo:
            stage = stages_mod.STAGE_BY_ID.get(sid)
            if stage is None:
                continue
            self.store.mark_stage(
                run_id,
                sid,
                status="running",
                started_at=time.time(),
                finished_at=0,
                chars=0,
                error="",
            )
            self.store.log_event(run_id, f"开始阶段 {stage.order:02d} {stage.title}", current_stage=sid)

            result = self._run_stage(run_id, stage)

            if result["ok"]:
                self.store.write_artifact(run_id, stage.artifact, result["content"])
                self.store.mark_stage(
                    run_id,
                    sid,
                    status="done",
                    chars=len(result["content"]),
                    provider=result.get("provider", ""),
                    model=result.get("model", ""),
                    finished_at=time.time(),
                    error="",
                )
                self.store.log_event(
                    run_id,
                    f"完成阶段 {stage.order:02d} {stage.title}（{len(result['content'])} 字，"
                    f"{result.get('provider', '?')}）",
                )
                completed.append(sid)
            else:
                if stage.optional:
                    self.store.mark_stage(
                        run_id, sid, status="skipped", finished_at=time.time(), error=result["error"]
                    )
                    self.store.log_event(
                        run_id, f"跳过可选阶段 {stage.order:02d} {stage.title}：{result['error']}"
                    )
                    completed.append(sid)
                    continue
                self.store.mark_stage(
                    run_id, sid, status="failed", finished_at=time.time(), error=result["error"]
                )
                self.store.log_event(run_id, f"阶段 {stage.order:02d} {stage.title} 失败：{result['error']}")
                failed = True
                break

        fresh = self.store.load(run_id) or {}
        finalized = all(
            (fresh.get("stages", {}).get(s) or {}).get("status") in ("done", "skipped")
            for s in all_ids
        )
        status = "succeeded" if finalized else ("failed" if failed else "partial")
        self.store.log_event(
            run_id,
            f"运行结束：{status}（本次完成 {len(completed)} 个阶段）",
            status=status,
            current_stage="",
        )
        return {"ok": True, "run_id": run_id, "status": status}

    def _run_stage(self, run_id: str, stage: stages_mod.Stage) -> dict[str, Any]:
        run = self.store.load(run_id) or {}
        upstream = upstream_texts(self.store, run_id, stage.inputs)
        messages = stages_mod.stage_prompt(
            stage,
            topic=run.get("topic", ""),
            mode=run.get("mode", ""),
            upstream=upstream,
        )
        api_key, _source = load_llm_key()
        try:
            result = model_gateway.call_llm(
                messages,
                provider=self.provider,
                api_key=api_key or None,
                local_first=self.local_first,
                enable_thinking=False,
                temperature=stage.temperature,
                max_tokens=stage.max_tokens,
                timeout=600,
            )
        except Exception as exc:
            return {"ok": False, "error": f"大模型调用异常：{exc}"}

        if not result.get("ok"):
            return {"ok": False, "error": str(result.get("error") or "大模型调用失败")}
        content = str(result.get("content") or "").strip()
        if not content:
            return {"ok": False, "error": "大模型返回空内容"}
        return {
            "ok": True,
            "content": content,
            "provider": result.get("provider", ""),
            "model": result.get("model", ""),
        }

    # ---------- 单阶段重跑 ----------

    def rerun_stage(self, run_id: str, stage_id: str) -> tuple[bool, str]:
        """把某阶段及其之后重置为 pending 并投回队列。"""
        run = self.store.load(run_id)
        if not run:
            return False, "运行不存在"
        if stage_id not in (run.get("stage_ids") or []):
            return False, "该阶段不在本次运行里"
        ids = list(run["stage_ids"])
        idx = ids.index(stage_id)
        tail = ids[idx:]
        for sid in tail:
            self.store.mark_stage(
                run_id, sid, status="pending", error="", chars=0, started_at=0, finished_at=0
            )
        self.store.log_event(
            run_id,
            f"从阶段 {stages_mod.STAGE_BY_ID[stage_id].title} 起重跑",
            status="pending",
            current_stage=stage_id,
        )
        self.submit(run_id, tail)
        return True, "已重新排队"
