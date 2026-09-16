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
    retryStarted,
    retrySucceeded,
    retryFailed,
    recentSucceeded,
    resetRunContext,
    runStage,
    runElapsedMs,
    runElapsedBreakdown,
  };
});
