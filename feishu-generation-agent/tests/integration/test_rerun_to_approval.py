"""「重新运行」/「重跑选中任务」→ 复用已批准计划 +（有返工要求就融合改写）→ **停在待审批**。

用户口径 2026-09-24（原话）：

    我说的是我的返工要求重跑选中任务应应该是优化我的提示词然后让我确认啊，
    重新运行按钮逻辑跟这个一模一样，然后如果返工要求里没内容则是直接不改提示词
    原封不动让我重新审核

对应状态机：

    ① 复用上一版**已批准的计划**（不重新规划）
    ② 返工要求有内容 → 调 rework 融合改写提示词；没内容 → **一个字都不改**
    ③ 进入「待审批」，用户能看到改后的提示词/参数
    ④ 点「批准」后才真正提交生成

这里用**真实图** + 可观测的假融合器，把 ②③④ 三条钉死；④ 之前那条「复用计划后
直接进执行」的老路径在 `clone_run_for_approval` 里已经不存在，但以前**不带勾选**
（即 `/rerun`，底部「重新运行」）时返工要求会被整体忽略，所以也在这里回归。
"""
from dataclasses import replace
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import GraphRuntime

SOURCE_URL = "https://fiction.feishu.cn/docx/doc-graph"


class _RecordingFuser:
    """把 fake planner 包一层，只额外提供 fuse_rework_prompt 并记录调用。"""

    def __init__(self, inner: Any, fused: object) -> None:
        self._inner = inner
        self._fused = fused
        self.fuse_calls: list[tuple[str, list[str]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def fuse_rework_prompt(
        self,
        original_prompt: str,
        requirements: list[str],
    ) -> str | None:
        self.fuse_calls.append((original_prompt, list(requirements)))
        if isinstance(self._fused, Exception):
            raise self._fused
        if callable(self._fused):
            return self._fused(original_prompt, requirements)
        return self._fused


async def _runtime_at_approval(
    services: GraphServices,
    *,
    fused: object,
    run_id: str,
    thread_id: str,
) -> tuple[GraphRuntime, _RecordingFuser]:
    planner = _RecordingFuser(services.planner, fused)
    services = replace(services, planner=planner)
    graph = build_graph(services, InMemorySaver())
    runtime = GraphRuntime(
        graph=graph,
        repository=services.repository,
        file_store=services.file_store,
        settings=services.settings,
        delivery_writer=services.delivery_writer,
        rework_fuser=planner.fuse_rework_prompt,
    )
    await services.repository.create_run(run_id, thread_id, SOURCE_URL)
    await graph.ainvoke(
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "source_url": SOURCE_URL,
            "source_revision": 7,
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    await services.repository.update_run_status(run_id, "waiting_approval")
    # 「已批准过的运行」：生产里这是审批节点在用户点批准时写进 checkpoint 的
    # `approved_plan` / `approved_tasks`。重跑要复用的正是它 —— 不写就退化成
    # 「重新规划」（那条路径不是本次要验的状态机）。
    config = runtime._config(thread_id)
    snapshot = await graph.aget_state(config)
    state = dict(snapshot.values or {})
    plan = state.get("draft_plan") or state.get("task_plan")
    state["approved_plan"] = plan
    state["approved_tasks"] = list((plan or {}).get("tasks") or [])
    await runtime._graph_aupdate_state(config, state, as_node="validate_plan")
    return runtime, planner


def _clone_events(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in view.get("events", [])
        if event.get("node") == "clone_approved_plan"
    ]


async def test_rerun_with_feedback_rewrites_prompt_and_stops_at_approval(
    fake_services: GraphServices,
) -> None:
    """有返工要求：融合改写提示词，然后**停在待审批**（不自动放行）。"""
    runtime, planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: f"{base}（已按「{reqs[-1]}」改写）",
        run_id="run-rerun-fused",
        thread_id="thread-rerun-fused",
    )
    try:
        before_view = await runtime.get_run_view("run-rerun-fused")
        before = before_view["approval"]["tasks"][0]["prompt"]

        # 不带 task_ids —— 这正是底部「重新运行」的调用形状。
        cloned_run_id = await runtime.clone_run_for_approval(
            "run-rerun-fused",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-rerun-fused-clone",
            thread_id="thread-rerun-fused-clone",
            feedback="动作再慢一点，结尾保持稳定",
        )

        after = await runtime.get_run_view(cloned_run_id)

        # ③ 新运行停在待审批，用户还能改提示词/参数。
        assert after["status"] == "waiting_approval"
        # ④ 没有批准就没有任何执行痕迹。
        assert after["artifacts"] == []
        assert after["execution_records"] == []
        assert after["operations"] == []

        # ② 提示词确实被融合改写了，并留下「改前 / 历次要求」给审批页对比。
        task = after["approval"]["tasks"][0]
        assert task["prompt"] == f"{before}（已按「动作再慢一点，结尾保持稳定」改写）"
        assert task["rework_requirements"] == ["动作再慢一点，结尾保持稳定"]
        assert task["rework_previous_prompt"] == before
        assert task["rework_base_prompt"] == before
        assert planner.fuse_calls == [
            (before, ["动作再慢一点，结尾保持稳定"])
        ]

        # 事件里如实写清「这次改写过了」与「停在待审批」。
        summaries = [event["summary"] for event in _clone_events(after)]
        assert any("按本次返工要求改写提示词" in text for text in summaries)
        assert any("已进入待审批" in text for text in summaries)
    finally:
        await runtime.close()


async def test_rerun_without_feedback_keeps_prompt_byte_identical(
    fake_services: GraphServices,
) -> None:
    """没有返工要求：**一个字都不改**，也不该调融合模型（不产生费用）。"""
    runtime, planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: f"{base}（不该被调用）",
        run_id="run-rerun-verbatim",
        thread_id="thread-rerun-verbatim",
    )
    try:
        before = (await runtime.get_run_view("run-rerun-verbatim"))["approval"]["tasks"][0]

        cloned_run_id = await runtime.clone_run_for_approval(
            "run-rerun-verbatim",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-rerun-verbatim-clone",
            thread_id="thread-rerun-verbatim-clone",
            feedback="   ",  # 只有空白 = 等同于没填
        )

        after = await runtime.get_run_view(cloned_run_id)

        assert after["status"] == "waiting_approval"
        task = after["approval"]["tasks"][0]
        # 逐字节相同。
        assert task["prompt"] == before["prompt"]
        assert task["rework_requirements"] == list(
            before.get("rework_requirements") or []
        )
        # 没改写就不该有「改前」对比（视图里未设置是 None）。
        assert not task["rework_previous_prompt"]
        # 融合器一次都没被调用。
        assert planner.fuse_calls == []

        summaries = [event["summary"] for event in _clone_events(after)]
        assert any("提示词原样保留" in text for text in summaries)
        assert any("已进入待审批" in text for text in summaries)
    finally:
        await runtime.close()


async def test_rerun_falls_back_to_verbatim_when_fusion_unavailable(
    fake_services: GraphServices,
) -> None:
    """融合上游挂了也不能让重跑失败：正文原样，但要求必须被记下、仍进待审批。"""
    runtime, planner = await _runtime_at_approval(
        fake_services,
        fused=RuntimeError("上游 500"),
        run_id="run-rerun-fusion-down",
        thread_id="thread-rerun-fusion-down",
    )
    try:
        before = (await runtime.get_run_view("run-rerun-fusion-down"))["approval"]["tasks"][0]

        cloned_run_id = await runtime.clone_run_for_approval(
            "run-rerun-fusion-down",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-rerun-fusion-down-clone",
            thread_id="thread-rerun-fusion-down-clone",
            feedback="动作再慢一点",
        )

        after = await runtime.get_run_view(cloned_run_id)

        assert after["status"] == "waiting_approval"
        task = after["approval"]["tasks"][0]
        assert task["prompt"] == before["prompt"]
        assert task["rework_requirements"] == ["动作再慢一点"]
        assert planner.fuse_calls
    finally:
        await runtime.close()


async def test_rerun_event_says_honestly_when_the_prompt_did_not_change(
    fake_services: GraphServices,
) -> None:
    """正文原样保留时，事件不能再说「已按本次返工要求改写提示词」。

    2026-10-10「铲」任务实测（run `cf40359d`）：事件里写的是「已复用上一版已批准计划，
    并按本次返工要求改写提示词」，而提示词的**视觉段一个字没动** —— 融合只往
    【全局声音与字幕约束】里塞了 4 个字（`蜡片卷曲` → `蜡片脆性断裂卷曲`）。用户因此
    以为「提示词改了、效果却没变」，实际上改的那 4 个字根本不在画面上。

    新契约（`audio_only_landing_reason`）会把这种落点判不合格 → 重试用尽后正文原样保留，
    此时事件必须如实说是「没有改变提示词正文」。要求本身仍要记下来，不能丢。
    """
    runtime, planner = await _runtime_at_approval(
        fake_services,
        # 原样退回 → 契约拒绝（与原文一致）→ 重试用尽 → 回退「正文原样保留」。
        fused=lambda base, reqs: base,
        run_id="run-rerun-unchanged",
        thread_id="thread-rerun-unchanged",
    )
    try:
        before = (await runtime.get_run_view("run-rerun-unchanged"))["approval"]["tasks"][0]

        cloned_run_id = await runtime.clone_run_for_approval(
            "run-rerun-unchanged",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-rerun-unchanged-clone",
            thread_id="thread-rerun-unchanged-clone",
            feedback="把蜡改成明显硬化的",
        )

        after = await runtime.get_run_view(cloned_run_id)

        assert after["status"] == "waiting_approval"
        assert after["approval"]["tasks"][0]["prompt"] == before["prompt"]
        summaries = [event["summary"] for event in _clone_events(after)]
        assert any("没有改变提示词正文" in text for text in summaries), summaries
        assert not any(
            "按本次返工要求改写提示词" in text for text in summaries
        ), summaries
        # 要求仍要记下来（绝不能因为「没改成」就丢）。
        assert after["approval"]["tasks"][0]["rework_requirements"] == [
            "把蜡改成明显硬化的"
        ]
    finally:
        await runtime.close()


# --- ②-b（2026-10-10，P0）：复用提示词的守卫 ---------------------------------
#
# 实测根因（REPORT_YARN §4.1 / §6.2 P0-b）：「点返工重做但不填返工要求」走
# 「一个字都不改」的快路径，会把**修复前**的旧提示词逐字节搬回来 —— 包括与本次时长
# 对不上的时间轴、以及已被摘掉素材留下的 `@视频N` 空洞。这里把三条判据钉死。


async def _rewrite_reused_plan(
    runtime: GraphRuntime,
    thread_id: str,
    *,
    prompt: str,
    duration: int,
) -> None:
    """把 checkpoint 里的草稿/已批准计划改写成指定的 提示词+时长。"""
    config = runtime._config(thread_id)
    snapshot = await runtime.graph.aget_state(config)
    state = dict(snapshot.values or {})
    plan = dict(state.get("draft_plan") or state.get("task_plan") or {})
    tasks = []
    for task in plan.get("tasks") or []:
        updated = dict(task)
        updated["prompt"] = prompt
        updated["duration"] = duration
        tasks.append(updated)
    plan["tasks"] = tasks
    state["draft_plan"] = plan
    state["task_plan"] = plan
    state["approved_plan"] = plan
    state["approved_tasks"] = tasks
    await runtime._graph_aupdate_state(config, state, as_node="validate_plan")


async def test_rerun_with_new_duration_does_not_preserve_prompt_verbatim(
    fake_services: GraphServices,
) -> None:
    """本次时长 ≠ 被克隆计划的时长 → 不许原样保留（置 blocking_issues 拦住）。"""
    runtime, _planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: f"{base}（不该被调用）",
        run_id="run-duration-shift",
        thread_id="thread-duration-shift",
    )
    try:
        # 上一版：5 秒 + 正文「预计5秒」（自洽）。
        await _rewrite_reused_plan(
            runtime,
            "thread-duration-shift",
            prompt="正面近景固定机位，单镜头一镜到底，预计5秒。",
            duration=5,
        )

        cloned_run_id = await runtime.clone_run_for_approval(
            "run-duration-shift",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-duration-shift-clone",
            thread_id="thread-duration-shift-clone",
            feedback="",          # 不填返工要求 = 走「原样保留」快路径
            duration=8,           # 本次时长 8 ≠ 上一版 5
        )

        after = await runtime.get_run_view(cloned_run_id)
        assert after["status"] == "waiting_approval"
        task = after["approval"]["tasks"][0]
        # 正文确实还是旧的（我们没替用户瞎改），但**被标了阻塞**：
        # 审批页会把 blocking 文案显示出来，且 approved_subset 会拒绝批准。
        assert "预计5秒" in task["prompt"]
        assert task["blocking_issues"], "时长变了却原样保留提示词，必须被拦住"
        assert any("8 秒" in text and "5 秒" in text for text in task["blocking_issues"])
        # 事件里如实说明被拦了（否则用户看不出红色阻塞是本轮加进去的）
        summaries = [event["summary"] for event in _clone_events(after)]
        assert any("被拦下" in text for text in summaries)
    finally:
        await runtime.close()


async def test_patching_duration_clears_stale_blocking_issue(
    fake_services: GraphServices,
) -> None:
    """把时长改回与正文一致之后，旧阻断项必须消失、审批必须能过（2026-10-13 小猫钓鱼 422）。

    **生产事故**：run `788ab355` 停在审批页，用户点审批一直 422
    （日志实锤 `POST /api/runs/788ab355.../decision → 422`）。根因是
    `TaskPlan.approve()` 只要 `blocking_issues` 非空就抛错，而那条是**旧时长下写的存量
    字符串**：「正文自述时长（12 秒）与任务时长（18 秒）不一致」；用户改时长走的
    `patch_task` 只落字段、**从不重算**它，于是两边已经一致了也永远批不了，
    界面上只显示「审批无效」。

    判别力：去掉 `patch_task` / `_validate_decision` 里的重算，本条必红。
    """
    from feishu_generation_agent.domain.plan import ApprovalDecision

    runtime, _planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: base,
        run_id="run-stale-block",
        thread_id="thread-stale-block",
    )
    try:
        # 上一版自己就带着缺陷：任务时长 8、正文却写「预计5秒」（复刻生产那条 run 的形态：
        # 正文 12 秒 / 时长 18 秒）。不传 duration → 走守卫判据 2，写下「正文自述时长…不一致」。
        await _rewrite_reused_plan(
            runtime,
            "thread-stale-block",
            prompt="正面近景固定机位，单镜头一镜到底，预计5秒。",
            duration=8,
        )
        cloned_run_id = await runtime.clone_run_for_approval(
            "run-stale-block",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-stale-block-clone",
            thread_id="thread-stale-block-clone",
            feedback="",
        )
        before = await runtime.get_run_view(cloned_run_id)
        task = before["approval"]["tasks"][0]
        assert task["blocking_issues"], "正文自述时长与任务时长不一致，必须被拦住"
        assert any("自述时长" in text for text in task["blocking_issues"]), task["blocking_issues"]

        # 用户在审批页把时长改成正文写的 5 —— 现在两边**一致**了。
        await runtime.patch_task(
            cloned_run_id,
            task_id=task["task_id"],
            patch={"duration": 5},
        )

        after = await runtime.get_run_view(cloned_run_id)
        updated = after["approval"]["tasks"][0]
        assert updated["duration"] == 5
        assert updated["blocking_issues"] == [], (
            "时长已经改成一致，旧阻断项还留着 → 审批会永久 422（这就是用户看到的「审批无效」）"
        )

        # 而且**真的能批**（approve 会因 blocking_issues 非空抛错）。
        _run, state = await runtime._waiting_state(cloned_run_id)
        runtime._validate_decision(
            state,
            ApprovalDecision(action="approve", selected_task_ids=[task["task_id"]]),
        )
    finally:
        await runtime.close()


async def test_already_wedged_run_becomes_approvable_once_values_agree(
    fake_services: GraphServices,
) -> None:
    """**存量**卡住的 run 也要能救：状态里还留着旧阻断项，但两边已经一致。

    这是生产里真正发生的那一步 —— 用户改时长发生在修复上线**之前**，所以 checkpoint 里
    还躺着旧字符串；修复后必须做到：① 审批页不再显示那条假「阻塞」；② 直接点批准能过。
    不加这两处重算（视图 + 审批校验）时本条必红。
    """
    from feishu_generation_agent.domain.plan import ApprovalDecision

    runtime, _planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: base,
        run_id="run-wedged",
        thread_id="thread-wedged",
    )
    try:
        await _rewrite_reused_plan(
            runtime,
            "thread-wedged",
            prompt="正面近景固定机位，单镜头一镜到底，预计5秒。",
            duration=8,
        )
        cloned_run_id = await runtime.clone_run_for_approval(
            "run-wedged",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-wedged-clone",
            thread_id="thread-wedged-clone",
            feedback="",
        )
        task_id = (await runtime.get_run_view(cloned_run_id))["approval"]["tasks"][0][
            "task_id"
        ]
        # 模拟「修复前就把时长改一致了」：状态里时长已改、旧阻断项没人清。
        await _rewrite_reused_plan(
            runtime,
            "thread-wedged-clone",
            prompt="正面近景固定机位，单镜头一镜到底，预计5秒。",
            duration=5,
        )
        state = (await runtime.graph.aget_state(runtime._config("thread-wedged-clone"))).values
        stored = state["draft_plan"]["tasks"][0]["blocking_issues"]
        assert stored, "前置条件：状态里必须还留着旧阻断项（否则这条测试没意义）"

        view = await runtime.get_run_view(cloned_run_id)
        assert view["approval"]["tasks"][0]["blocking_issues"] == [], (
            "审批页还在显示「阻塞：时长不一致」—— 其实两边已经一致了"
        )

        _run, waiting_state = await runtime._waiting_state(cloned_run_id)
        runtime._validate_decision(
            waiting_state,
            ApprovalDecision(action="approve", selected_task_ids=[task_id]),
        )
    finally:
        await runtime.close()


async def test_rework_feedback_is_remembered_as_long_term_rule(
    fake_services: GraphServices,
) -> None:
    """用户在返工里说的话要**自动沉淀**成长期口径（2026-10-13：「没有记忆功能吗」）。

    判别力：去掉 `clone_run_for_approval` 里的 `_remember_requirements` 调用，本条必红。
    """
    from pathlib import Path

    from feishu_generation_agent.integrations.user_rules import (
        SHARED_OWNER,
        UserRuleStore,
    )

    runtime, _planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: base,
        run_id="run-memory",
        thread_id="thread-memory",
    )
    try:
        await runtime.clone_run_for_approval(
            "run-memory",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-memory-clone",
            thread_id="thread-memory-clone",
            feedback="钱袋子底部的 UI 横条必须复现。结尾要回到游戏画面",
        )
        store = UserRuleStore(Path(fake_services.settings.data_dir) / "user_rules.json")
        # 共享桶里本来就有产品级种子口径，这里只看**这次采集到的**。
        remembered = [
            rule.text
            for rule in store.rules(SHARED_OWNER)
            if rule.source == "rework"
        ]
        assert remembered == [
            "钱袋子底部的 UI 横条必须复现",
            "结尾要回到游戏画面",
        ], remembered
    finally:
        await runtime.close()


async def test_rerun_blocks_reused_prompt_with_stale_stated_duration(
    fake_services: GraphServices,
) -> None:
    """上一版自己就带着「时长 8 / 正文预计5秒」的缺陷 → 不许把它往前传。"""
    runtime, _planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: base,
        run_id="run-stale-duration",
        thread_id="thread-stale-duration",
    )
    try:
        await _rewrite_reused_plan(
            runtime,
            "thread-stale-duration",
            prompt="正面近景固定机位，单镜头一镜到底，预计5秒。",
            duration=8,
        )

        cloned_run_id = await runtime.clone_run_for_approval(
            "run-stale-duration",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-stale-duration-clone",
            thread_id="thread-stale-duration-clone",
        )

        after = await runtime.get_run_view(cloned_run_id)
        task = after["approval"]["tasks"][0]
        assert task["blocking_issues"]
        assert any("预计" in text or "不一致" in text for text in task["blocking_issues"])
    finally:
        await runtime.close()


async def test_rerun_blocks_reused_prompt_with_dangling_video_token(
    fake_services: GraphServices,
) -> None:
    """`@视频N` 空洞：提示词引用了不存在于本次计划的参考视频 → 拦住并给出出口。

    这正是用户记过的「点返工重做但不填返工要求会把修复前的旧提示词原样搬回来」
    （上一版摘掉参考视频后，正文里还留着 `@视频1`）。
    """
    runtime, _planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: base,
        run_id="run-dangling-video",
        thread_id="thread-dangling-video",
    )
    try:
        await _rewrite_reused_plan(
            runtime,
            "thread-dangling-video",
            # 计划里只有 image-1（asset-1）→ @视频1 没有对应素材
            prompt="按 @视频1 中匀速顺滑的运镜节奏推进，主体以 @图片1 为唯一基准。",
            duration=10,
        )

        cloned_run_id = await runtime.clone_run_for_approval(
            "run-dangling-video",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-dangling-video-clone",
            thread_id="thread-dangling-video-clone",
        )

        after = await runtime.get_run_view(cloned_run_id)
        task = after["approval"]["tasks"][0]
        assert task["blocking_issues"]
        assert any("@视频1" in text for text in task["blocking_issues"])
    finally:
        await runtime.close()


async def test_rerun_without_new_duration_stays_unblocked(
    fake_services: GraphServices,
) -> None:
    """守卫的误伤防护：计划自洽 + 锚点齐全时，**不许**加任何阻塞。"""
    runtime, _planner = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: base,
        run_id="run-guard-clean",
        thread_id="thread-guard-clean",
    )
    try:
        await _rewrite_reused_plan(
            runtime,
            "thread-guard-clean",
            prompt="正面近景固定机位，单镜头一镜到底，预计10秒；主体以 @图片1 为基准。",
            duration=10,
        )

        cloned_run_id = await runtime.clone_run_for_approval(
            "run-guard-clean",
            RequirementRequest(source_url=SOURCE_URL),
            run_id="run-guard-clean-clone",
            thread_id="thread-guard-clean-clone",
        )

        after = await runtime.get_run_view(cloned_run_id)
        task = after["approval"]["tasks"][0]
        assert task["blocking_issues"] == []
        summaries = [event["summary"] for event in _clone_events(after)]
        assert not any("被拦下" in text for text in summaries)
    finally:
        await runtime.close()
