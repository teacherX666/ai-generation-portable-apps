"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const staticDir = path.resolve(__dirname, "../../src/feishu_generation_agent/web/static");
const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
const app = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const styles = fs.readFileSync(path.join(staticDir, "styles.css"), "utf8");

test("workflow uses user-facing status guidance instead of raw internal state", () => {
  assert.match(html, /id="run-guidance"/);
  assert.match(html, /当前阶段/);
  assert.match(html, /任务状态/);
  assert.match(app, /waiting_approval:\s*\{ label: "等待你审核"/);
  assert.match(app, /delivery_failed:\s*\{ label: "写入结果表失败"/);
  assert.match(app, /statusBadge\.textContent = statusInfo\.label/);
});

test("technical workflow events are progressively disclosed", () => {
  assert.match(html, /<details class="workflow-details">/);
  assert.match(html, /查看技术执行轨迹/);
  assert.match(styles, /\.workflow-details/);
});

test("bottom actions are contextual and direct url supports enter", () => {
  assert.match(html, /id="approve-button"[^>]*hidden/);
  assert.match(html, /批准并开始生成/);
  assert.match(app, /approveButton\.hidden = !canReview/);
  assert.match(app, /retryDeliveryButton\.hidden = status !== "delivery_failed"/);
  assert.match(app, /directRunUrl\.addEventListener\("keydown"/);
});
