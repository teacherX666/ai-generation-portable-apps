"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const PromptDiff = require(
  "../../src/feishu_generation_agent/web/static/prompt-diff.js"
);

test("splitClauses 保留标点、丢弃空白", () => {
  const clauses = PromptDiff.splitClauses("一只猫在跑，背景很暗。\n动作要慢");
  assert.deepEqual(clauses, ["一只猫在跑，", "背景很暗。", "动作要慢"]);
});

test("splitClauses 空文本返回空数组", () => {
  assert.deepEqual(PromptDiff.splitClauses(""), []);
  assert.deepEqual(PromptDiff.splitClauses(null), []);
});

test("diffPrompt 完全相同时没有变更", () => {
  const result = PromptDiff.diffPrompt("一只猫在跑，背景很暗。", "一只猫在跑，背景很暗。");
  assert.equal(result.hasChanges, false);
  assert.deepEqual(result.segments, [
    { type: "same", text: "一只猫在跑，背景很暗。" },
  ]);
});

test("diffPrompt 标出新增的句子", () => {
  const result = PromptDiff.diffPrompt(
    "一只猫在跑。",
    "一只猫在跑。背景很暗。"
  );
  assert.equal(result.hasChanges, true);
  assert.deepEqual(result.segments, [
    { type: "same", text: "一只猫在跑。" },
    { type: "added", text: "背景很暗。" },
  ]);
  assert.equal(result.stats.added, 1);
  assert.equal(result.stats.removed, 0);
});

test("diffPrompt 标出删掉的句子", () => {
  const result = PromptDiff.diffPrompt(
    "一只猫在跑。背景很暗。",
    "一只猫在跑。"
  );
  assert.deepEqual(result.segments, [
    { type: "same", text: "一只猫在跑。" },
    { type: "removed", text: "背景很暗。" },
  ]);
  assert.equal(result.stats.removed, 1);
});

test("diffPrompt 替换句子时先删后增", () => {
  const result = PromptDiff.diffPrompt("背景很暗。", "背景明亮。");
  assert.equal(result.hasChanges, true);
  assert.deepEqual(result.segments, [
    { type: "removed", text: "背景很暗。" },
    { type: "added", text: "背景明亮。" },
  ]);
});

test("reworkComparison 没有返工记录时不可对比", () => {
  const result = PromptDiff.reworkComparison({ prompt: "原始画面。" });
  assert.equal(result.hasRework, false);
  assert.deepEqual(result.requirements, []);
  assert.equal(result.basePrompt, "");
});

test("reworkComparison 给出原始提示词、历次要求与差异", () => {
  const result = PromptDiff.reworkComparison({
    prompt: "原始画面。动作再慢一点。",
    rework_base_prompt: "原始画面。",
    rework_requirements: ["动作再慢一点", "背景太暗"],
  });
  assert.equal(result.hasRework, true);
  assert.equal(result.basePrompt, "原始画面。");
  assert.equal(result.currentPrompt, "原始画面。动作再慢一点。");
  assert.deepEqual(result.requirements, ["动作再慢一点", "背景太暗"]);
  assert.equal(result.hasChanges, true);
  assert.ok(
    result.segments.some((s) => s.type === "added" && s.text.includes("动作再慢一点"))
  );
});

test("reworkComparison 在正文里带老标记时也能识别为返工过", () => {
  const result = PromptDiff.reworkComparison({
    prompt: "原始画面。\n【返工要求】动作再慢一点",
  });
  assert.equal(result.hasRework, true);
  assert.equal(result.basePrompt, "原始画面。");
});

test("reworkComparison 忽略空白要求项", () => {
  const result = PromptDiff.reworkComparison({
    prompt: "原始画面。",
    rework_base_prompt: "原始画面。",
    rework_requirements: ["  ", "动作再慢一点", ""],
  });
  assert.deepEqual(result.requirements, ["动作再慢一点"]);
});