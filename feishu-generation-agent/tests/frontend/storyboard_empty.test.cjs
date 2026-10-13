"use strict";

/**
 * 删掉某段的分镜图后，服务端会把这一段的候选清空（见 `graph/runtime.py` 的
 * `_drop_storyboard_candidate`）—— 这样它绝不会被「默认第一张」再选回来。
 *
 * 代价是 `candidates` 会变空，前端如果按「候选非空」过滤，整行连
 * 「重新生成这一张」都会一起消失，用户再也点不到（用户 2026-09-23：
 * 「分镜图我明明已经删掉他了不需要了还是能影响我」）。
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const staticDir = path.resolve(
  __dirname,
  "../../src/feishu_generation_agent/web/static",
);
const appSource = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const styles = fs.readFileSync(path.join(staticDir, "styles.css"), "utf8");

function storyboardSection() {
  const start = appSource.indexOf("function storyboardSection(task)");
  assert.ok(start > 0, "找不到 storyboardSection");
  const end = appSource.indexOf("function regenerateStoryboardFrame", start);
  assert.ok(end > start, "找不到 storyboardSection 的结尾");
  return appSource.slice(start, end);
}

test("候选被删光的段仍然渲染一行", () => {
  const section = storyboardSection();
  // 旧的过滤条件「候选非空才渲染」必须去掉。
  assert.doesNotMatch(
    section,
    /Array\.isArray\(frame\?\.candidates\)\s*&&\s*frame\.candidates\.length/,
  );
  assert.match(section, /本段关键帧已删除/);
  assert.match(section, /"storyboard-empty"/);
});

test("没有候选时不高亮第一张候选", () => {
  const section = storyboardSection();
  assert.doesNotMatch(section, /frame\.candidates\[0\]\.asset_id/);
});

test("空态说明有样式", () => {
  assert.match(styles, /\.storyboard-empty\s*\{[^}]*\}/);
});

test("选图时其它段不许兜底成第一张候选", () => {
  // 用户删掉过某段的关键帧 → 那一段不再高亮任何候选；如果这里给别的段兜底成
  // `candidates[0]`，随便在别的段点一下就会把删掉的那一段又补上一张。
  const start = appSource.indexOf("function selectStoryboardCandidate(");
  assert.ok(start > 0, "找不到 selectStoryboardCandidate");
  const end = appSource.indexOf("function storyboardSection(", start);
  assert.ok(end > start, "找不到 selectStoryboardCandidate 的结尾");
  const body = appSource.slice(start, end);
  assert.doesNotMatch(body, /item\.candidates\[0\]\?\.asset_id/);
  assert.match(body, /item\.candidates\.find\(\(one\) => one\.selected\)\?\.asset_id \|\| null/);
});

test("批准 payload 里没有高亮的段回传 null", () => {
  const reviewState = fs.readFileSync(
    path.join(staticDir, "review-state.js"),
    "utf8",
  );
  const start = reviewState.indexOf("function approvalTask(");
  assert.ok(start > 0, "找不到 approvalTask");
  const end = reviewState.indexOf("function buildApprovalPayload(", start);
  assert.ok(end > start, "找不到 approvalTask 的结尾");
  const body = reviewState.slice(start, end);
  assert.doesNotMatch(body, /candidates\[0\]/);
  assert.match(body, /\|\| null/);
});
