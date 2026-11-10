"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const BitableState = require(
  "../../src/feishu_generation_agent/web/static/bitable-state.js"
);

const T0 = "2026-09-16T09:39:09Z";
const T_APPROVE_START = "2026-09-16T09:42:30Z";
const T_END = "2026-09-16T09:55:46Z";
const END_MS = Date.parse(T_END);

test("runElapsedBreakdown 把人工审批停留从总耗时里拆出来", () => {
  const view = {
    status: "waiting_review",
    created_at: T0,
    updated_at: T_END,
    events: [
      { node: "plan_requirements", status: "completed", created_at: "2026-09-16T09:40:02Z" },
      { node: "validate_plan", status: "completed", created_at: "2026-09-16T09:40:15Z" },
      { node: "human_approval", status: "started", created_at: T_APPROVE_START },
      {
        node: "verify_and_download_artifacts",
        status: "completed",
        created_at: T_END,
      },
    ],
  };

  const breakdown = BitableState.runElapsedBreakdown(view, END_MS);

  assert.equal(breakdown.totalMs, 997_000);
  assert.equal(breakdown.humanMs, 135_000);
  assert.equal(breakdown.systemMs, 862_000);
});

test("runElapsedBreakdown 没有人工等待时全部计入系统耗时", () => {
  const view = {
    status: "succeeded",
    created_at: T0,
    updated_at: "2026-09-16T09:41:09Z",
    events: [
      { node: "ingest_source", status: "completed", created_at: T0 },
      { node: "human_approval", status: "completed", created_at: "2026-09-16T09:41:09Z" },
    ],
  };

  const breakdown = BitableState.runElapsedBreakdown(
    view,
    Date.parse("2026-09-16T09:41:09Z")
  );

  assert.equal(breakdown.humanMs, 0);
  assert.equal(breakdown.systemMs, 120_000);
});

test("runElapsedBreakdown 把审片停留也算作人工耗时", () => {
  const view = {
    status: "running",
    created_at: T0,
    updated_at: "2026-09-16T09:59:46Z",
    events: [
      {
        node: "verify_and_download_artifacts",
        status: "completed",
        created_at: T_END,
      },
      {
        node: "execute_selected_tasks",
        status: "started",
        created_at: "2026-09-16T09:59:46Z",
      },
    ],
  };

  const breakdown = BitableState.runElapsedBreakdown(
    view,
    Date.parse("2026-09-16T09:59:46Z")
  );

  // 审片停留 4 分钟应算人工，其余（含之前的审批）算系统
  assert.equal(breakdown.humanMs, 240_000);
});

test("runElapsedBreakdown 视图非法时返回空", () => {
  assert.deepEqual(BitableState.runElapsedBreakdown(null), {
    totalMs: null,
    humanMs: 0,
    systemMs: null,
  });
});

// 2026-10-20 用户：「怎么生成累计耗时这么长」。
// 实测 run 1e4f0e24：真实生成到 09:13:37 就结束了，停在 waiting_review 两小时，
// 界面却显示累计 2h46m —— 因为那时**只扣事件之间**的间隔，最后这段"还在等你看"的
// 等待从没被扣掉，全算成系统耗时，随时间无限增长。系统耗时不该包含「等你看」的时间。
test("还停在审片时，最后那段等待也算人工（累计耗时不该随停留时间涨）", () => {
  const twoHoursLater = Date.parse(T_END) + 2 * 3600_000;
  const view = {
    status: "waiting_review",
    created_at: T0,
    updated_at: T_END,
    events: [
      { node: "plan_requirements", status: "completed", created_at: "2026-09-16T09:40:02Z" },
      { node: "validate_plan", status: "completed", created_at: "2026-09-16T09:40:15Z" },
      { node: "human_approval", status: "started", created_at: T_APPROVE_START },
      { node: "verify_and_download_artifacts", status: "completed", created_at: T_END },
    ],
  };

  const breakdown = BitableState.runElapsedBreakdown(view, twoHoursLater);

  assert.equal(
    breakdown.systemMs,
    862_000,
    "系统耗时必须只有真正的生成时间（停两小时不该涨到 2h+）",
  );
  assert.equal(
    breakdown.humanMs,
    135_000 + 2 * 3600_000,
    "两小时停留要算进人工",
  );
  assert.equal(breakdown.totalMs, 862_000 + 135_000 + 2 * 3600_000);
});

test("还停在计划审批时同样算人工", () => {
  const oneHourLater = Date.parse(T_APPROVE_START) + 3600_000;
  const view = {
    status: "waiting_approval",
    created_at: T0,
    updated_at: T_APPROVE_START,
    events: [
      { node: "validate_plan", status: "completed", created_at: "2026-09-16T09:40:15Z" },
      { node: "human_approval", status: "started", created_at: T_APPROVE_START },
    ],
  };

  const breakdown = BitableState.runElapsedBreakdown(view, oneHourLater);

  // 系统 = created→approval 开始 − 计划审批停留(135s)
  assert.equal(
    breakdown.systemMs,
    Date.parse(T_APPROVE_START) - Date.parse(T0) - 135_000,
  );
  assert.equal(breakdown.humanMs, 135_000 + 3600_000);
});

test("正在跑的任务，尾部时间仍算系统（别把真干活的时间当人工扣掉）", () => {
  const tenMinutesLater = Date.parse("2026-09-16T09:50:00Z");
  const view = {
    status: "running",
    created_at: T0,
    updated_at: "2026-09-16T09:40:00Z",
    events: [
      { node: "human_approval", status: "completed", created_at: "2026-09-16T09:40:00Z" },
      { node: "execute_selected_tasks", status: "running", created_at: "2026-09-16T09:40:00Z" },
    ],
  };

  const breakdown = BitableState.runElapsedBreakdown(view, tenMinutesLater);

  assert.equal(breakdown.humanMs, 0, "没有人工等待");
  assert.equal(
    breakdown.systemMs,
    tenMinutesLater - Date.parse(T0),
    "执行中的 10 分钟必须留在系统耗时里",
  );
});