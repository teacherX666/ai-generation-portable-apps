"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");

const BitableState = require(
  "../../src/feishu_generation_agent/web/static/bitable-state.js"
);

test("production-only page has no legacy document form", () => {
  const html = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/index.html"),
    "utf8",
  );

  assert.equal(html.includes('id="run-form"'), false);
  assert.equal(html.includes('id="source-url"'), false);
  assert.equal(html.includes('id="scan-bitable-button"'), true);
  assert.equal(html.includes('id="animation-category-tab"'), true);
  assert.equal(html.includes('id="portrait-category-tab"'), true);
  assert.equal(html.includes(">刷新任务<"), true);
});

const tasks = [
  {
    record_id: "rec-1",
    display_text: "雨中纸船",
    source_url: "https://tenant.feishu.cn/docx/doc1",
    executor_open_ids: ["ou_alice"],
  },
];

test("app persists and restores the selected bitable category", () => {
  const app = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8",
  );

  assert.match(app, /feishu-agent\.active-category/);
  assert.match(app, /BitableState\.createState\(initialBitableCategory\(\)\)/);
  assert.match(app, /persistBitableCategory\(category\)/);
});
test("scan start, success and failure preserve explicit UI phases", () => {
  let state = BitableState.createState();
  state = BitableState.scanStarted(state, "animation");
  assert.equal(state.categories.animation.scan.phase, "loading");
  assert.equal(state.categories.animation.scan.error, "");

  state = BitableState.scanSucceeded(state, "animation", tasks);
  assert.equal(state.categories.animation.scan.phase, "ready");
  assert.deepEqual(state.categories.animation.tasks, tasks);

  state = BitableState.scanFailed(state, "animation", "读取失败");
  assert.equal(state.categories.animation.scan.phase, "error");
  assert.equal(state.categories.animation.scan.error, "读取失败");
  assert.deepEqual(state.categories.animation.tasks, tasks);
});

test("claim success keeps the task marked as processing", () => {
  let state = BitableState.scanSucceeded(BitableState.createState(), "animation", tasks);
  state = BitableState.claimStarted(state, "rec-1", "animation");
  assert.deepEqual(state.claim, {
    phase: "loading",
    recordId: "rec-1",
    runId: null,
    category: "animation",
    error: "",
  });

  const conflicted = BitableState.claimConflict(state, "已被领取");
  assert.equal(conflicted.claim.phase, "conflict");
  assert.equal(conflicted.claim.error, "已被领取");
  assert.equal(conflicted.categories.animation.tasks.length, 1);

  state = BitableState.claimSucceeded(state, "run-1");
  assert.equal(state.claim.phase, "ready");
  assert.equal(state.claim.runId, "run-1");
  assert.equal(state.categories.animation.tasks.length, 1);
  assert.equal(state.categories.animation.tasks[0].claimed_run_id, "run-1");
  assert.equal(state.categories.animation.tasks[0].claim_status, "processing");
});

test("rescan keeps a claimed task when the backend excludes active runs", () => {
  let state = BitableState.scanSucceeded(BitableState.createState(), "portrait", tasks);
  state = BitableState.claimStarted(state, "rec-1", "portrait");
  state = BitableState.claimSucceeded(state, "run-portrait");
  state = BitableState.scanSucceeded(state, "portrait", []);

  assert.equal(state.categories.portrait.tasks.length, 1);
  assert.equal(state.categories.portrait.tasks[0].record_id, "rec-1");
  assert.equal(state.categories.portrait.tasks[0].claimed_run_id, "run-portrait");
});

test("createState accepts a persisted category", () => {
  assert.equal(BitableState.createState("portrait").activeCategory, "portrait");
  assert.equal(BitableState.createState("invalid").activeCategory, "animation");
});

test("claim badge labels follow the persisted claim status", () => {
  assert.equal(BitableState.claimBadge({ record_id: "rec-1" }), null);
  assert.equal(BitableState.claimBadge(null), null);
  assert.deepEqual(
    BitableState.claimBadge({ claimed_run_id: "run-1", claim_status: "processing" }),
    { label: "分析中", tone: "busy" },
  );
  assert.deepEqual(
    BitableState.claimBadge({ claimed_run_id: "run-1", claim_status: "处理中" }),
    { label: "处理中", tone: "busy" },
  );
  assert.deepEqual(
    BitableState.claimBadge({ claimed_run_id: "run-1", claim_status: "待审批" }),
    { label: "待审批", tone: "attention" },
  );
  assert.deepEqual(
    BitableState.claimBadge({ claimed_run_id: "run-1", claim_status: "待确认成片" }),
    { label: "待确认成片", tone: "attention" },
  );
  assert.deepEqual(
    BitableState.claimBadge({ claimed_run_id: "run-1", claim_status: "回写失败" }),
    { label: "回写失败", tone: "danger" },
  );
  assert.deepEqual(
    BitableState.claimBadge({ claimed_run_id: "run-1" }),
    { label: "分析中", tone: "busy" },
  );
});

test("rescan adopts the claim fields the backend now returns", () => {
  let state = BitableState.scanSucceeded(
    BitableState.createState(),
    "animation",
    tasks,
  );
  state = BitableState.claimStarted(state, "rec-1", "animation");
  state = BitableState.claimSucceeded(state, "run-local");
  // 后端现在会把已领取的记录一起带回来（带真实状态），服务端数据优先。
  state = BitableState.scanSucceeded(state, "animation", [
    { ...tasks[0], claimed_run_id: "run-local", claim_status: "待审批" },
  ]);

  assert.equal(state.categories.animation.tasks.length, 1);
  assert.equal(state.categories.animation.tasks[0].claimed_run_id, "run-local");
  assert.equal(state.categories.animation.tasks[0].claim_status, "待审批");
});

test("任务记录按需求分组：同一条记录的历史版本叠在同一任务下", () => {
  const runs = [
    { run_id: "run-3", record_id: "rec-a", display_text: "拿着吧你", status: "running", active: true },
    { run_id: "run-2", record_id: "rec-a", display_text: "拿着吧你", status: "succeeded" },
    { run_id: "run-1", record_id: "rec-a", display_text: "拿着吧你", status: "failed" },
    { run_id: "run-b", record_id: "rec-b", display_text: "脱毛", status: "waiting_review" },
  ];

  const groups = BitableState.groupRecentRuns(runs);

  // 重跑不再新开任务记录：rec-a 的三次尝试归到同一条任务下。
  assert.equal(groups.length, 2);
  assert.equal(groups[0].record_id, "rec-a");
  assert.equal(groups[0].display_text, "拿着吧你");
  assert.equal(groups[0].versions.length, 3);
  // 当前版＝正在跑的那条；其余是历史版本（预览历史用）。
  assert.equal(groups[0].current.run_id, "run-3");
  assert.deepEqual(
    groups[0].history.map((run) => run.run_id),
    ["run-2", "run-1"],
  );
  assert.equal(groups[1].current.run_id, "run-b");
  assert.deepEqual(groups[1].history, []);
});

test("没有 record_id 时按 run_id 各自成组（老数据不串台）", () => {
  const groups = BitableState.groupRecentRuns([
    { run_id: "run-1", display_text: "甲" },
    { run_id: "run-2", display_text: "乙" },
  ]);

  assert.equal(groups.length, 2);
  assert.equal(groups[0].record_id, null);
  assert.equal(groups[0].current.run_id, "run-1");
});

test("没有正在跑的版本时，最新的一条就是当前版", () => {
  const groups = BitableState.groupRecentRuns([
    { run_id: "run-new", record_id: "rec-a", status: "succeeded" },
    { run_id: "run-old", record_id: "rec-a", status: "failed" },
  ]);

  assert.equal(groups[0].current.run_id, "run-new");
  assert.deepEqual(groups[0].history.map((run) => run.run_id), ["run-old"]);
});

test("任务列表渲染的徽章文案来自共享助手", () => {
  const app = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8",
  );

  assert.match(app, /BitableState\.liveClaimBadge\(/);
  assert.match(app, /bitable-task-badge/);
});

test("返工没有改动时，面板要解释原因并给出下一步", () => {
  const app = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8",
  );

  // 用户问过「改后与改前一致为什么会出现这种问题」——光说「一致」等于没说。
  assert.equal(
    /"改后与改前一致"/.test(app),
    false,
    "不该只有一句「改后与改前一致」",
  );
  assert.match(app, /本次返工没有改变提示词正文/);
  // 要指出真实原因：上一版正文里已经写了这条要求（融合器据此判定无需改动）。
  assert.match(app, /上一版/);
  // 并给出可执行的下一步：重复同一句没用，应该写成禁止项或更可判定的描述。
  assert.match(app, /禁止项/);
  assert.match(app, /更可判定/);
});

test("live claim badge prefers the freshly polled run status", () => {
  const task = {
    record_id: "rec-1",
    claimed_run_id: "run-1",
    claim_status: "处理中",
  };

  // 轮询到的新鲜运行状态优先：徽章要跟着运行走，而不是停在领取那一刻。
  assert.deepEqual(
    BitableState.liveClaimBadge(task, "run-1", {
      label: "等待你审核",
      tone: "attention",
    }),
    { label: "等待你审核", tone: "attention" },
  );
  assert.deepEqual(
    BitableState.liveClaimBadge(task, "run-1", {
      label: "正在生成内容",
      tone: "running",
    }),
    { label: "正在生成内容", tone: "busy" },
  );
  assert.deepEqual(
    BitableState.liveClaimBadge(task, "run-1", {
      label: "生成完成",
      tone: "success",
    }),
    { label: "生成完成", tone: "done" },
  );
  // 没有新鲜状态时退回任务自带的 claim_status。
  assert.deepEqual(
    BitableState.liveClaimBadge(task, "run-1", null),
    { label: "处理中", tone: "busy" },
  );
  // 未领取的任务没有徽章。
  assert.equal(
    BitableState.liveClaimBadge({ record_id: "rec-2" }, null, {
      label: "等待你审核",
      tone: "attention",
    }),
    null,
  );
});

test("任务记录 只在状态会自己变的时候盯，不在状态没变时反复刷新", () => {
  const app = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8",
  );

  // 走查时用户看到的问题：任务记录只能靠手动/偶发刷新，状态长期是旧的。
  assert.match(app, /BitableState\.liveClaimBadge\(/);
  assert.match(app, /document\.hidden/);
  assert.match(app, /visibilitychange/);
  // 后台刷新失败要静默：服务重启的几秒里不能每 5 秒弹一次全局错误。
  assert.match(app, /loadRecentRuns\(\{ silent: true \}\)/);
  assert.match(app, /if \(!silent\) showError\(error\)/);

  // 用户要求：不要定时刷新，要「状态更新的时候刷新」。
  // 等待审批 / 等待成片审核是停在等人操作上的，状态不会自己变 —— 那时不该有任何轮询。
  assert.match(app, /SELF_PROGRESSING_RUN_STATUSES/);
  assert.match(app, /function needsRunWatch\(/);
  assert.match(app, /function scheduleRunWatch\(/);
  assert.match(app, /function stopRunWatch\(/);
  assert.equal(
    /setInterval\(refreshBitablePanel/.test(app),
    false,
    "不该再有固定节奏刷新整个面板",
  );
  // 正在看的那条运行状态一变，立刻对一次任务记录。
  assert.match(app, /lastViewedRun/);
  assert.match(app, /scheduleRunWatch\(\)/);
});
test("retry delivery has loading, success and failure states", () => {
  let state = BitableState.createState();
  state = BitableState.retryStarted(state, "run-1");
  assert.deepEqual(state.deliveryRetry, {
    phase: "loading",
    runId: "run-1",
    error: "",
  });

  state = BitableState.retrySucceeded(state);
  assert.equal(state.deliveryRetry.phase, "ready");

  state = BitableState.retryStarted(state, "run-1");
  state = BitableState.retryFailed(state, "结果列冲突");
  assert.equal(state.deliveryRetry.phase, "error");
  assert.equal(state.deliveryRetry.error, "结果列冲突");
});

test("production task keeps delivery block state through a scan", () => {
  let state = BitableState.createState();
  state = BitableState.scanSucceeded(state, "animation", [{
    record_id: "rec-no-maker",
    display_text: "需求 A",
    progress: "制作中",
    maker_name: null,
    deliverable: false,
    delivery_block_reason: "缺少需求制作人",
  }]);

  assert.equal(state.categories.animation.tasks[0].progress, "制作中");
  assert.equal(state.categories.animation.tasks[0].deliverable, false);
  assert.equal(state.categories.animation.tasks[0].delivery_block_reason, "缺少需求制作人");
});

test("recent runs survive resetting the active task context", () => {
  let state = BitableState.createState();
  state = BitableState.claimStarted(state, "rec-1", "animation");
  state = BitableState.claimSucceeded(state, "run-active");
  state = BitableState.recentSucceeded(state, [
    { run_id: "run-old", status: "succeeded" },
  ]);
  state = BitableState.resetRunContext(state);

  assert.equal(state.claim.runId, null);
  assert.equal(state.claim.phase, "idle");
  assert.deepEqual(state.recentRuns, [{ run_id: "run-old", status: "succeeded" }]);
});

test("category tabs keep independent scan results", () => {
  let state = BitableState.createState();
  state = BitableState.scanStarted(state, "animation");
  state = BitableState.scanSucceeded(state, "animation", [
    { record_id: "rec-animation", task_type: "动画类" },
  ]);
  state = BitableState.selectCategory(state, "portrait");

  assert.equal(state.activeCategory, "portrait");
  assert.equal(BitableState.activeCategoryState(state).scan.phase, "idle");

  state = BitableState.scanStarted(state, "portrait");
  state = BitableState.scanSucceeded(state, "portrait", [
    { record_id: "rec-portrait", task_type: "真人类" },
  ]);
  state = BitableState.selectCategory(state, "animation");

  assert.deepEqual(
    BitableState.activeCategoryState(state).tasks.map((task) => task.record_id),
    ["rec-animation"],
  );
});

test("claim success retains the task only in its own category", () => {
  let state = BitableState.createState();
  state = BitableState.scanSucceeded(
    state,
    "animation",
    [{ record_id: "rec-animation" }],
  );
  state = BitableState.scanSucceeded(
    state,
    "portrait",
    [{ record_id: "rec-portrait" }],
  );
  state = BitableState.claimStarted(state, "rec-portrait", "portrait");
  state = BitableState.claimSucceeded(state, "run-portrait");

  assert.equal(state.categories.portrait.tasks.length, 1);
  assert.equal(
    state.categories.portrait.tasks[0].claimed_run_id,
    "run-portrait",
  );
  assert.equal(state.categories.animation.tasks.length, 1);
  assert.equal(state.categories.animation.tasks[0].record_id, "rec-animation");
  assert.equal(state.categories.animation.tasks[0].claimed_run_id, undefined);
  assert.equal(state.claim.category, "portrait");
});

test("a portrait scan failure does not clear animation results", () => {
  let state = BitableState.createState();
  state = BitableState.scanSucceeded(
    state,
    "animation",
    [{ record_id: "rec-animation" }],
  );
  state = BitableState.scanFailed(state, "portrait", "真人视图读取失败");

  assert.deepEqual(
    state.categories.animation.tasks,
    [{ record_id: "rec-animation" }],
  );
  assert.equal(state.categories.animation.scan.phase, "ready");
  assert.equal(state.categories.portrait.scan.phase, "error");
  assert.equal(
    state.categories.portrait.scan.error,
    "真人视图读取失败",
  );
});

test("run stage exposes asset preparation, provider generation and delivery", () => {
  assert.equal(BitableState.runStage({
    status: "running",
    operations: [{ phase: "intent_created", provider_task_id: null }],
  }), "正在准备参考素材并提交");

  assert.equal(BitableState.runStage({
    status: "waiting_provider",
    operations: [{ phase: "submitted", provider_task_id: "task-1" }],
  }), "Seedance 正在生成");

  assert.equal(BitableState.runStage({
    status: "delivering",
    operations: [{ phase: "succeeded", provider_task_id: "task-1" }],
  }), "正在写入结果表");
});

test("run elapsed time keeps increasing until a terminal status", () => {
  const createdAt = "2026-07-23T10:00:00+00:00";
  const updatedAt = "2026-07-23T10:00:08+00:00";

  assert.equal(BitableState.runElapsedMs({
    status: "waiting_provider",
    created_at: createdAt,
    updated_at: updatedAt,
  }, Date.parse("2026-07-23T10:00:20+00:00")), 20_000);

  assert.equal(BitableState.runElapsedMs({
    status: "succeeded",
    created_at: createdAt,
    updated_at: updatedAt,
  }, Date.parse("2026-07-23T10:00:20+00:00")), 8_000);
});
