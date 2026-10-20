"""「claim 建运行失败必须回滚绑定」的回归测试（2026-10-15）。

用户报：「多维表格运行不存在，我重试怎么还失败，出bug了？」

实测指纹（生产数据）：
* 绑定 `production_tasks` 里 `run_id = fe698799…`、`status = 失败`、
  `last_error = 本地运行记录不存在`、`active = 0`；
* `runs` 表里**没有** `fe698799`；而同一秒真正的 run 是 `37757a6c`；
* 该记录 `recvy6wEqBnwbx`（拯救香水）从此**无法重试**：点任何按钮都走
  `RunNotFound("多维表格运行不存在")`。

根因在 `ProductionBitableService.claim`：**先把绑定落库**（claim 里生成 run_id），
**再** `start_run` 建运行 —— 后者一抛异常（服务重启 / 上游失败 / RunConflict），
绑定就留在库里、run 从未创建。修法：建运行失败时把绑定**释放**（`release` → active=0），
让记录回到「可领取」，用户能再点「开始分析」。
"""

from __future__ import annotations

import pytest

from feishu_generation_agent.bitable import production_service as module
from feishu_generation_agent.bitable.production_service import (
    ProductionBitableService,
)


class _StubStore:
    def __init__(self) -> None:
        self.released: list[dict] = []

    async def claim(self, location, task, *, run_id, thread_id, owner_user_id):
        return type(
            "Binding",
            (),
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "source_url": "https://example.feishu.cn/record/x",
                "owner_user_id": owner_user_id,
            },
        )()

    async def release(self, run_id, *, status, last_error=None, owner_user_id=None):
        self.released.append(
            {
                "run_id": run_id,
                "status": status,
                "last_error": last_error,
                "owner_user_id": owner_user_id,
            }
        )


class _StubRuntime:
    def __init__(self, *, boom: bool) -> None:
        self.boom = boom

    async def start_run(self, request, *, run_id=None, thread_id=None):
        if self.boom:
            raise RuntimeError("start_run 失败（模拟重启/上游异常）")
        return run_id


class _StubTask:
    record_id = "recvy6wEqBnwbx"
    task_type = "动画类"
    claimed_run_id = None
    display_text = "拯救香水"


def _service(*, boom: bool) -> tuple[ProductionBitableService, _StubStore]:
    service = ProductionBitableService.__new__(ProductionBitableService)
    store = _StubStore()
    service._store = store                      # type: ignore[attr-defined]
    service._runtime = _StubRuntime(boom=boom)  # type: ignore[attr-defined]
    service._enabled_task_types = {"动画类"}    # type: ignore[attr-defined]
    service._runtime_owner_scope = lambda owner: _NullScope()  # type: ignore[attr-defined]

    async def prepared_source(category="animation"):
        return type("Src", (), {"location": object(), "matches_task_type": lambda self, t: True})()

    async def scan(category="animation"):
        return [_StubTask()]

    async def auto_archive_best_effort(owner_user_id, record_id):
        return None

    service._prepared_source = prepared_source          # type: ignore[attr-defined]
    service.scan = scan                                  # type: ignore[attr-defined]
    service._auto_archive_best_effort = auto_archive_best_effort  # type: ignore[attr-defined]
    return service, store


class _NullScope:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_claim_releases_binding_when_start_run_fails() -> None:
    """建运行失败 → 绑定必须被释放（否则用户永远重试不了）。"""
    service, store = _service(boom=True)
    with pytest.raises(RuntimeError):
        await service.claim("recvy6wEqBnwbx", "animation")
    assert len(store.released) == 1, "失败时必须释放绑定"
    released = store.released[0]
    assert "已释放" in str(released["last_error"])
    assert released["status"].value == "失败"


@pytest.mark.asyncio
async def test_claim_does_not_release_on_success() -> None:
    """成功路径不许误释放（否则记录会立刻回到可领取，出现重复领取）。"""
    service, store = _service(boom=False)
    run_id = await service.claim("recvy6wEqBnwbx", "animation")
    assert run_id
    assert store.released == []


def test_rollback_is_wired_into_claim() -> None:
    """接线守门：`claim` 里必须能看到回滚分支与可见的错误文案。"""
    import inspect

    source = inspect.getsource(module.ProductionBitableService.claim)
    assert "本地运行未能创建" in source
    assert "_store.release" in source
    assert source.index("start_run") < source.index("_store.release")
