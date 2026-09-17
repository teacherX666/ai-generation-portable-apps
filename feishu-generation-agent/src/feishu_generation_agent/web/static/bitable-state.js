(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.BitableState = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const CATEGORY_NAMES = new Set(["animation", "portrait", "image"]);
  const TERMINAL_RUN_STATUSES = new Set([
    "succeeded",
    "completed_with_errors",
    "failed",
    "cancelled",
    "delivery_failed",
  ]);

  function createCategoryState() {
    return {
      tasks: [],
      scan: { phase: "idle", error: "" },
    };
  }

  function categoryState(state, category) {
    if (!CATEGORY_NAMES.has(category)) throw new Error("未知任务类别");
    return state.categories[category];
  }

  function withCategory(state, category, nextCategoryState) {
    categoryState(state, category);
    return {
      ...state,
      categories: {
        ...state.categories,
        [category]: nextCategoryState,
      },
    };
  }

  function createState(activeCategory = "animation") {
    const selected = CATEGORY_NAMES.has(activeCategory) ? activeCategory : "animation";
    return {
      activeCategory: selected,
      categories: {
        animation: createCategoryState(),
        portrait: createCategoryState(),
        image: createCategoryState(),
      },
      claim: {
        phase: "idle",
        recordId: null,
        runId: null,
        category: null,
        error: "",
      },
      deliveryRetry: { phase: "idle", runId: null, error: "" },
      recentRuns: [],
    };
  }

  function selectCategory(state, category) {
    categoryState(state, category);
    return { ...state, activeCategory: category };
  }

  function activeCategoryState(state) {
    return categoryState(state, state.activeCategory);
  }

  function scanStarted(state, category) {
    const current = categoryState(state, category);
    return withCategory(state, category, {
      ...current,
      scan: { phase: "loading", error: "" },
    });
  }

  function scanSucceeded(state, category, tasks) {
    const current = categoryState(state, category);
    const incoming = Array.isArray(tasks) ? JSON.parse(JSON.stringify(tasks)) : [];
    const incomingIds = new Set(incoming.map((task) => task.record_id));
    const claimed = current.tasks.filter(
      (task) => task.claimed_run_id && !incomingIds.has(task.record_id),
    );
    return withCategory(state, category, {
      ...current,
      tasks: [...incoming, ...claimed],
      scan: { phase: "ready", error: "" },
    });
  }

  function scanFailed(state, category, error) {
    const current = categoryState(state, category);
    return withCategory(state, category, {
      ...current,
      scan: { phase: "error", error: String(error || "扫描失败") },
    });
  }

  function claimStarted(state, recordId, category) {
    categoryState(state, category);
    return {
      ...state,
      claim: { phase: "loading", recordId, runId: null, category, error: "" },
    };
  }

  function claimSucceeded(state, runId) {
    const recordId = state.claim.recordId;
    const category = state.claim.category;
    const nextState = withCategory(state, category, {
      ...categoryState(state, category),
      tasks: categoryState(state, category).tasks.map((task) => (
        task.record_id === recordId
          ? { ...task, claimed_run_id: runId, claim_status: "processing" }
          : task
      )),
    });
    return {
      ...nextState,
      claim: { ...state.claim, phase: "ready", recordId, runId, error: "" },
    };
  }

  function claimConflict(state, error) {
    return {
      ...state,
      claim: {
        ...state.claim,
        phase: "conflict",
        error: String(error || "该任务已被领取"),
      },
    };
  }

  /**
   * 已领取任务的徽章文案与配色。
   *
   * 后端 `claim_status` 是 `TableTaskStatus` 的中文值（处理中/待审批/生成中…），
   * 而刚点完「开始分析」时本地乐观状态写的是 `"processing"`，两种都要认。
   * 返回 null 表示这条记录还没被领取。
   */
  const CLAIM_STATUS_META = {
    processing: { label: "分析中", tone: "busy" },
    待处理: { label: "待处理", tone: "busy" },
    处理中: { label: "处理中", tone: "busy" },
    生成中: { label: "生成中", tone: "busy" },
    回写中: { label: "回写中", tone: "busy" },
    待审批: { label: "待审批", tone: "attention" },
    待确认成片: { label: "待确认成片", tone: "attention" },
    已完成: { label: "已完成", tone: "done" },
    失败: { label: "失败", tone: "danger" },
    回写失败: { label: "回写失败", tone: "danger" },
  };

  function claimBadge(task) {
    if (!task || !task.claimed_run_id) return null;
    const raw = task.claim_status;
    const meta = CLAIM_STATUS_META[raw];
    if (meta) return { ...meta };
    const text = typeof raw === "string" && raw ? raw : "分析中";
    return { label: text, tone: "busy" };
  }

  //: 运行状态文案（app.js 的 RUN_STATUS_UI）的 tone → 徽章 tone。
  const BADGE_TONE_BY_RUN_TONE = {
    running: "busy",
    attention: "attention",
    success: "done",
    warning: "attention",
    danger: "danger",
    muted: "busy",
  };

  /**
   * 徽章优先用「刚轮询到的运行状态」，拿不到才退回任务自带的 claim_status。
   *
   * `claim_status` 是扫描那一刻的快照，扫描要读整张飞书表，做不到高频；
   * 而任务记录列表每隔几秒就会拉一次运行状态。两者取新鲜的那个，徽章才不会
   * 一直停在「处理中」——这正是走查时看到的「状态不实时更新」。
   *
   * `runUi` 是 `{label, tone}`（由 app.js 的 statusUi 给出），传 null 表示
   * 当前没有这个运行的新鲜状态。
   */
  function liveClaimBadge(task, claimedRunId, runUi) {
    if (!claimedRunId) return null;
    if (runUi && runUi.label) {
      return {
        label: runUi.label,
        tone: BADGE_TONE_BY_RUN_TONE[runUi.tone] || "busy",
      };
    }
    return claimBadge({ ...task, claimed_run_id: claimedRunId });
  }

  function retryStarted(state, runId) {
    return {
      ...state,
      deliveryRetry: { phase: "loading", runId, error: "" },
    };
  }

  function retrySucceeded(state) {
    return {
      ...state,
      deliveryRetry: { ...state.deliveryRetry, phase: "ready", error: "" },
    };
  }

  function retryFailed(state, error) {
    return {
      ...state,
      deliveryRetry: {
        ...state.deliveryRetry,
        phase: "error",
        error: String(error || "交付重试失败"),
      },
    };
  }

  function recentSucceeded(state, recentRuns) {
    return {
      ...state,
      recentRuns: Array.isArray(recentRuns) ? JSON.parse(JSON.stringify(recentRuns)) : [],
    };
  }

  /**
   * 任务记录**一条记录一行**：同一条需求的历次尝试只占一行（重跑不再多出一行）。
   *
   * 展示的是「正在跑的那条」优先，否则最新的一条；历次版本不在这里展开 ——
   * 它们是去成片预览的横向滑条里看/切的（用户明确要的是这个分工）。
   * 没有 `record_id` 的（直连运行、老数据）各自成行，不猜。
   */
  function latestRunsByRecord(runs) {
    const order = [];
    const byKey = new Map();
    (Array.isArray(runs) ? runs : []).forEach((run) => {
      if (!run || !run.run_id) return;
      const key = run.record_id || `run:${run.run_id}`;
      if (!byKey.has(key)) {
        byKey.set(key, run);
        order.push(key);
        return;
      }
      // 同一个 key 之后又出现：进行中的那条优先当代表。
      if (run.active && !byKey.get(key).active) byKey.set(key, run);
    });
    return order.map((key) => byKey.get(key));
  }

  /**
   * 同一条多维表格记录的**其它尝试**（不含当前这条）。
   *
   * 重跑会在同一个 record_id 下留下多个 run。成片预览靠它把「往次生成的片子」
   * 摆出来一起看；而**任务记录仍然一版一行** —— 用户要的是「预览里能看到历史
   * 生成的」，不是把历次尝试合并成一条记录。
   *
   * `record_id` 缺失（老数据、测试替身）时返回空：没有归组依据就不猜。
   */
  function siblingRuns(runs, currentRunId) {
    const list = Array.isArray(runs) ? runs : [];
    const current = list.find((run) => run && run.run_id === currentRunId);
    if (!current || !current.record_id) return [];
    return list.filter(
      (run) =>
        run
        && run.record_id === current.record_id
        && run.run_id !== currentRunId,
    );
  }

  function resetRunContext(state) {
    return {
      ...state,
      claim: {
        phase: "idle",
        recordId: null,
        runId: null,
        category: null,
        error: "",
      },
      deliveryRetry: { phase: "idle", runId: null, error: "" },
    };
  }

  function runStage(view) {
    if (!view || typeof view !== "object") return null;
    if (view.status === "delivering") return "正在写入结果表";
    const operations = Array.isArray(view.operations) ? view.operations : [];
    const latest = operations.at(-1);
    if (
      view.status === "waiting_provider"
      || latest?.phase === "submitted"
      || latest?.provider_task_id
    ) {
      return "Seedance 正在生成";
    }
    if (
      ["running", "resuming"].includes(view.status)
      && ["intent_created", "submission_uncertain"].includes(latest?.phase)
      && !latest?.provider_task_id
    ) {
      return "正在准备参考素材并提交";
    }
    return null;
  }

  function runElapsedMs(view, now = Date.now()) {
    if (!view || typeof view !== "object") return null;
    const started = Date.parse(view.created_at);
    const finished = Date.parse(view.updated_at);
    if (!Number.isFinite(started)) return null;
    const end = TERMINAL_RUN_STATUSES.has(view.status) && Number.isFinite(finished)
      ? finished
      : now;
    return Math.max(0, end - started);
  }

  /**
   * 把墙钟总耗时拆成「系统耗时」与「人工耗时」。
   *
   * `runElapsedMs` 是 created_at→updated_at 的墙钟时间，会把用户在审批页/审片页
   * 停留的时间也算进去（实测某单 16分37秒 里有 2分15秒是用户在读计划），
   * 于是数字虚高、看不出真正的瓶颈。这里按事件流把人工停留扣出来：
   *  - `human_approval/started` 与前一个事件之间的间隔 = 计划审批停留
   *  - `verify_and_download_artifacts/completed` 之后的第一个事件之前的间隔
   *    = 成片审核停留
   */
  function runElapsedBreakdown(view, now = Date.now()) {
    const totalMs = runElapsedMs(view, now);
    if (totalMs === null) {
      return { totalMs: null, humanMs: 0, systemMs: null };
    }
    const events = Array.isArray(view.events) ? view.events : [];
    let humanMs = 0;
    let awaitingReview = false;
    for (let index = 0; index < events.length; index += 1) {
      const current = events[index] || {};
      const previous = index > 0 ? events[index - 1] || {} : null;
      const currentMs = Date.parse(current.created_at);
      const previousMs = previous ? Date.parse(previous.created_at) : NaN;
      const gap =
        Number.isFinite(currentMs) && Number.isFinite(previousMs)
          ? Math.max(0, currentMs - previousMs)
          : 0;
      if (
        current.node === "human_approval" &&
        current.status === "started"
      ) {
        humanMs += gap;
      } else if (awaitingReview) {
        humanMs += gap;
      }
      awaitingReview =
        current.node === "verify_and_download_artifacts" &&
        current.status === "completed";
    }
    const human = Math.min(humanMs, totalMs);
    return { totalMs, humanMs: human, systemMs: Math.max(0, totalMs - human) };
  }

  return {
    createState,
    selectCategory,
    activeCategoryState,
    scanStarted,
    scanSucceeded,
    scanFailed,
    claimStarted,
    claimSucceeded,
    claimConflict,
    claimBadge,
    liveClaimBadge,
    retryStarted,
    retrySucceeded,
    retryFailed,
    recentSucceeded,
    latestRunsByRecord,
    siblingRuns,
    resetRunContext,
    runStage,
    runElapsedMs,
    runElapsedBreakdown,
  };
});
