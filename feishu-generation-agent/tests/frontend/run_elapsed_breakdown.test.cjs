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