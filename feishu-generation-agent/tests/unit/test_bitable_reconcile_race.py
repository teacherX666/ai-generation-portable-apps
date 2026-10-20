# -*- coding: utf-8 -*-
"""「运行还在创建中，绑定就被列表轮询判死」的回归测试（2026-10-20）。

用户报：「为啥我飞书模块两次重跑都失败了」「重跑任务一直在处理中」。

**生产实锤**（`data/production-bitable.sqlite3` 的 `last_error` + `data/agent.sqlite3`
的 `runs.created_at`，UTC）：

| 记录 | 绑定 run | 绑定被判死 | 该 run 真正落库 | 倒挂 |
|---|---|---|---|---|
| 水晶泡泡 | `5c643f09` | 07:35:27 | 07:52:26 | 17 分钟 |
| 水晶泡泡 | `77e8b76b` | 07:42:51 | 07:55:43 | 13 分钟 |
| 女巫划过 | `cdc8386e` | 07:49:01 | 07:55:43 | 6 分钟 |
| 女巫划过 | `c7da921b` | 07:49:41 | 07:55:43 | 6 分钟 |

**根因**：`claim()` / `rerun()` 都是**先写绑定、再建运行**；而前端看到「处理中」会
每 5 秒盯守一次（`app.js` 的 `RUN_WATCH_INTERVAL_MS = 5000`），每次盯守都走
`active_runs()` / `scan()` → `_reconcile_bindings()` → `sync_once()` →
`get_run_view()` → 运行还没落库 → `RunNotFound` → 绑定被 `release` 成
「失败：本地运行记录不存在」。

**比"显示失败"更贵的后果**：`release` 会顺手把 `active` 置 0，等于**把记录解锁** ——
用户可以立刻再领一次，于是同一条记录在几分钟内堆出好几个 run（重复计费的生成）。
2026-10-20 当天 水晶泡泡 6 个 run、女巫划过 6 个 run，就是这个形状。

**修法**：运行还不存在**不等于**这条绑定已经死了。
① 正在创建的那几个 run_id 记在内存里（`_mark_creating` / `_unmark_creating`），
   轮询期间绝不判死；
② 跨重启（内存标记丢失）留一段宽限期（`_RECONCILE_GRACE_SECONDS`）—— 重启会走
   `resume_incomplete()` 重建运行，宽限期内不许判死；
③ 宽限期过了、运行仍然不存在，才释放，而且文案必须写成**可重新领取**，
   不再冒充「运行丢了」。
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta, timezone

import pytest

from feishu_generation_agent.bitable import production_service as module
from feishu_generation_agent.bitable.production_service import (
    BindingNotFound,
    ProductionBitableService,
    _RECONCILE_GRACE_SECONDS,
)
from feishu_generation_agent.domain.bitable import TableTaskStatus
from feishu_generation_agent.graph.runtime import RunNotFound


def _stamp(seconds_ago: float) -> str:
    """SQLite `CURRENT_TIMESTAMP` 的样子：UTC、无时区、秒级。"""
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


class _Binding:
    def __init__(
        self,
        run_id: str,
        *,
        age_seconds: float = 0.0,
        status: TableTaskStatus = TableTaskStatus.PROCESSING,
        owner_user_id: str = "owner-1",
        active: bool = True,
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.owner_user_id = owner_user_id
        self.active = active
        self.created_at = _stamp(age_seconds)
        self.updated_at = _stamp(age_seconds)


class _StubStore:
    def __init__(self) -> None:
        self.released: list[dict] = []
        self.statuses: list[dict] = []

    async def release(self, run_id, *, status, last_error=None, owner_user_id=None):
        self.released.append(
            {
                "run_id": run_id,
                "status": status,
                "last_error": last_error,
                "owner_user_id": owner_user_id,
            }
        )

    async def set_status(self, run_id, status, *, owner_user_id=None):
        self.statuses.append({"run_id": run_id, "status": status})
        # 真实 store 会返回刷新后的绑定；这里只需要「运行存在」这一路继续跑通。


def _service(
    *, sync_error: BaseException | None
) -> tuple[ProductionBitableService, _StubStore]:
    service = ProductionBitableService.__new__(ProductionBitableService)
    store = _StubStore()
    service._store = store  # type: ignore[attr-defined]

    async def sync_once(run_id, *, owner_user_id=None):
        if sync_error is not None:
            raise sync_error
        return None

    service.sync_once = sync_once  # type: ignore[attr-defined]
    return service, store


# --------------------------------------------------------------------------- #
# ① 核心回归：运行还在创建中，轮询不许把绑定判死
# --------------------------------------------------------------------------- #
async def test_fresh_binding_is_not_killed_while_its_run_is_still_creating() -> None:
    service, store = _service(sync_error=RunNotFound("运行不存在"))
    binding = _Binding("run-fresh", age_seconds=0.0)

    await service._reconcile_bindings([binding])

    assert store.released == [], (
        "运行还没建出来，绑定不许被判死 —— release 会把 active 置 0 解锁记录，"
        "用户立刻能再领一次，于是同一记录堆出多个重复计费的生成"
    )


async def test_binding_with_a_live_creation_marker_is_never_killed() -> None:
    """克隆可能跑十几分钟（返工融合调用超时重试），期间内存标记必须一直生效。"""
    service, store = _service(sync_error=RunNotFound("运行不存在"))
    binding = _Binding("run-slow", age_seconds=_RECONCILE_GRACE_SECONDS * 4)
    service._mark_creating(binding.run_id)

    await service._reconcile_bindings([binding])

    assert store.released == [], "正在创建中的运行，年龄再大也不许判死"


async def test_ancient_binding_whose_run_never_appeared_is_released_as_retryable() -> None:
    """兜底：跨重启且运行始终没建出来 —— 释放，但文案必须是「可重新领取」。"""
    service, store = _service(sync_error=RunNotFound("运行不存在"))
    binding = _Binding("run-dead", age_seconds=_RECONCILE_GRACE_SECONDS * 3)

    await service._reconcile_bindings([binding])

    assert len(store.released) == 1
    released = store.released[0]
    assert released["status"] is TableTaskStatus.FAILED
    assert "可重新领取" in str(released["last_error"])
    assert released["owner_user_id"] == "owner-1"


async def test_binding_that_vanished_is_not_reported_as_a_missing_run() -> None:
    """`sync_once` 找不到**绑定**≠ 找不到**运行**：没有可释放的东西，也不该改状态。"""
    service, store = _service(sync_error=BindingNotFound("多维表格绑定不存在"))
    binding = _Binding("run-gone", age_seconds=_RECONCILE_GRACE_SECONDS * 3)

    await service._reconcile_bindings([binding])

    assert store.released == []


async def test_transient_error_is_logged_and_keeps_the_last_status(caplog) -> None:
    """瞬时错误（服务重启中 / 飞书抖动）不许判死，但必须留痕 —— 以前是静默 continue。"""
    service, store = _service(sync_error=RuntimeError("飞书抖动"))
    binding = _Binding("run-flaky", age_seconds=_RECONCILE_GRACE_SECONDS * 3)

    with caplog.at_level(logging.WARNING, logger="feishu_generation_agent.bitable.production_service"):
        await service._reconcile_bindings([binding])

    assert store.released == []
    assert any("run-flaky" in record.getMessage() for record in caplog.records), (
        "徽章会停在「处理中」，日志里必须能查到原因"
    )


async def test_without_the_grace_a_fresh_binding_would_be_killed(monkeypatch) -> None:
    """**判别力自检**：把宽限期压成 0，修复前的行为必须能被复现出来。

    没有这条，上面那些 `store.released == []` 有可能只是因为桩根本没走到
    release 分支（假绿）。
    """
    service, store = _service(sync_error=RunNotFound("运行不存在"))
    monkeypatch.setattr(module, "_RECONCILE_GRACE_SECONDS", 0.0)
    binding = _Binding("run-fresh", age_seconds=1.0)

    await service._reconcile_bindings([binding])

    assert len(store.released) == 1, (
        "宽限期一旦失效就必须判死 —— 否则说明保护没接在这条路径上"
    )
    assert store.released[0]["last_error"] == "本地运行记录不存在，绑定已释放，可重新领取"


# --------------------------------------------------------------------------- #
# ② 创建标记的接线与生命周期
# --------------------------------------------------------------------------- #
def test_reconcile_is_wired_to_the_grace_and_the_marker() -> None:
    source = inspect.getsource(ProductionBitableService._reconcile_bindings)
    assert "_creation_in_flight" in source
    assert "_binding_age_seconds" in source
    assert "BindingNotFound" in source
    assert "_RECONCILE_GRACE_SECONDS" in source  # 宽限期不能靠魔数藏在别处


def test_creation_marker_is_wired_into_claim_and_rerun() -> None:
    for name in ("claim", "rerun"):
        source = inspect.getsource(getattr(ProductionBitableService, name))
        assert "_mark_creating" in source, name
        assert "_unmark_creating" in source, name
        assert source.index("_mark_creating") < source.index("_store.claim"), (
            f"{name}: 标记必须在**绑定落库之前**就打上，否则轮询仍能挤进那条缝"
        )


class _StubClaimStore:
    def __init__(self, run_id: str) -> None:
        self.released: list[dict] = []
        self._run_id = run_id

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
        self.released.append({"run_id": run_id, "last_error": last_error})


class _BoomRuntime:
    async def start_run(self, request, *, run_id=None, thread_id=None):
        raise RuntimeError("start_run 失败（模拟重启/上游异常）")


class _NullScope:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def test_creation_marker_is_cleared_when_creation_fails() -> None:
    """建运行失败 → 标记必须一起清掉，否则那个 run_id 会永远占着「正在创建」。"""
    service = ProductionBitableService.__new__(ProductionBitableService)
    store = _StubClaimStore("run-x")
    service._store = store  # type: ignore[attr-defined]
    service._runtime = _BoomRuntime()  # type: ignore[attr-defined]
    service._enabled_task_types = {"动画类"}  # type: ignore[attr-defined]
    service._runtime_owner_scope = lambda owner: _NullScope()  # type: ignore[attr-defined]

    async def prepared_source(category="animation"):
        return type(
            "Src", (), {"location": object(), "matches_task_type": lambda self, t: True}
        )()

    async def scan(category="animation"):
        return [
            type(
                "Task",
                (),
                {
                    "record_id": "rec-x",
                    "task_type": "动画类",
                    "claimed_run_id": None,
                    "display_text": "x",
                },
            )()
        ]

    async def auto_archive_best_effort(owner_user_id, record_id):
        return None

    service._prepared_source = prepared_source  # type: ignore[attr-defined]
    service.scan = scan  # type: ignore[attr-defined]
    service._auto_archive_best_effort = auto_archive_best_effort  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError):
        await service.claim("rec-x", "animation")

    assert service._pending_creations() == set(), "创建标记泄漏会让该 run_id 永不被判死"
