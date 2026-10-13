"use strict";

/**
 * 「模型审片」区块右侧堆叠（2026-10-08 用户报：「模型审片区块的右边如果前面的
 * 内容较多就会出现堆叠的情况，占很多行」）。
 *
 * 真实浏览器实测到的根因（不是第一直觉的「没换行」）：
 *   1) `.take-finding-why` 是 `flex: 1`（= `1 1 0%`）。flex-basis 为 0 的项
 *      **不分担收缩**；左侧 `.take-finding-issue` 的 flex-basis 是 auto（max-content，
 *      很长），负剩余空间全由它一家吸收，收缩完刚好用光，于是「剩余空间」为 0，
 *      `flex-grow: 1` 一点也拿不到 → why 只落到自己的自动最小尺寸（min-width:auto
 *      = min-content，实测 39px）→ 中文按 3 字一行堆成 31 行、整行 616px 高。
 *   2) `.take-finding-issue` 压根没有规则 → `min-width: auto`，长串（URL）不参与收缩，
 *      把 `.take-findings-list` 的 auto 单列轨道撑到 850~2535px，整行溢出卡片右侧。
 *   3) `.take-finding-at` / `.take-finding-adopt` 没有 `flex-shrink: 0`，右侧按钮被压到
 *      47px（正常 60px）。
 *
 * 这些断言锁的是**修法**（不锁像素，像素靠真实浏览器量）：
 *   · 右栏（时间/徽标/采纳）不被压缩、不换行；
 *   · 左栏允许收缩（min-width 不是 auto）并能正常换行，长串靠 overflow-wrap 兜底；
 *   · 单列网格锁进容器（minmax(0, 1fr)），别被长串撑破；
 *   · 规则全部限定在这个区块自己的 class 前缀里，不外溢到全局。
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
const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
// 注释里也会出现选择器/属性名（本项目 CSS 注释写得很密），先剥掉注释再断言。
const styles = fs
  .readFileSync(path.join(staticDir, "styles.css"), "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "");

//: 逐条切出「选择器 { 声明 }」。这样 `.take-finding-issue` 不会误命中
//: `.take-finding.is-violation .take-finding-issue { ... }`（是两条不同的规则）。
const CSS_RULES = (() => {
  const rules = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = re.exec(styles))) {
    rules.push({
      selectors: match[1].split(",").map((item) => item.trim()).filter(Boolean),
      body: match[2],
    });
  }
  return rules;
})();

function cssRule(selector) {
  return CSS_RULES.filter((rule) => rule.selectors.includes(selector))
    .map((rule) => rule.body)
    .join("\n");
}

test("审片行：右侧的元信息（时间/徽标/采纳按钮）不被压缩、不换行", () => {
  const at = cssRule(".take-finding-at");
  assert.match(at, /flex:\s*0\s+0\s+auto/, `时间列必须定宽不压缩，实际：${at}`);
  assert.match(at, /white-space:\s*nowrap/, `时间列不许折行，实际：${at}`);

  const kind = cssRule(".take-finding-kind");
  assert.match(kind, /flex:\s*0\s+0\s+auto/, `徽标必须定宽不压缩，实际：${kind}`);
  assert.match(kind, /white-space:\s*nowrap/, `徽标不许折行，实际：${kind}`);

  const adopt = cssRule(".take-finding-adopt");
  assert.match(
    adopt,
    /flex:\s*0\s+0\s+auto/,
    `「采纳」按钮不许被左侧长文本挤走/压扁，实际：${adopt}`,
  );
  assert.match(adopt, /white-space:\s*nowrap/, `「采纳」按钮不许自己换行，实际：${adopt}`);
  assert.match(adopt, /margin-left:\s*auto/, "「采纳」按钮要始终右对齐");
});

test("审片行：左栏可收缩且能正常换行（min-width 不能是 auto）", () => {
  const issue = cssRule(".take-finding-issue");
  assert.ok(issue, "必须给 .take-finding-issue 明确规则，否则 min-width 默认 auto");
  assert.doesNotMatch(
    issue,
    /min-width:\s*auto/,
    `左栏正文不许保留 min-width:auto（长内容会去侵占右侧空间），实际：${issue}`,
  );
  assert.match(issue, /min-width:\s*min\(/, `左栏要有下限，实际：${issue}`);
  assert.match(issue, /overflow-wrap:\s*anywhere/, `长串靠 overflow-wrap 兜底，实际：${issue}`);

  const why = cssRule(".take-finding-why");
  assert.doesNotMatch(
    why,
    /min-width:\s*auto/,
    `理由列不许保留 min-width:auto（会被压成几十像素的竖条），实际：${why}`,
  );
  // 曾经的写法 `flex: 1`（basis 0）正是堆叠的根因：它一点也分不到收缩后的剩余空间。
  assert.doesNotMatch(
    why,
    /flex:\s*1\s*;/,
    `理由列不能再写 flex: 1（basis 0% 不分担收缩 → 被压成 39px 竖条），实际：${why}`,
  );
  assert.match(why, /min-width:\s*min\(/, `理由列要有下限，实际：${why}`);
  assert.match(why, /overflow-wrap:\s*anywhere/, `长串靠 overflow-wrap 兜底，实际：${why}`);
});

test("审片列表：单列锁进容器宽度，长串（URL）撑不破卡片", () => {
  const listRule = cssRule(".take-findings-list");
  assert.match(
    listRule,
    /grid-template-columns:\s*minmax\(\s*0\s*,\s*1fr\s*\)/,
    `单列必须是 minmax(0, 1fr)，否则 auto 轨道会被长串撑宽，实际：${listRule}`,
  );
  assert.match(
    cssRule(".take-finding"),
    /min-width:\s*0/,
    "行本身作为 grid 子项也要 min-width:0",
  );
  assert.match(cssRule(".take-findings p"), /overflow-wrap:\s*anywhere/);
});

test("审片标题行：右侧「让模型看一遍」按钮不被左侧文字挤走", () => {
  const headButton = cssRule(".take-findings-head button");
  assert.match(headButton, /flex:\s*0\s+0\s+auto/, `标题行按钮定宽，实际：${headButton}`);
  assert.match(headButton, /white-space:\s*nowrap/, `标题行按钮不换行，实际：${headButton}`);
});

test("不改全局：新增规则全部限定在 .take-finding* 前缀里，且不强制切断中文", () => {
  const scoped = CSS_RULES.filter((rule) => /min\(5em/.test(rule.body));
  assert.ok(scoped.length >= 2, "应当有两条带 5em 下限的规则（正文 + 理由）");
  scoped.forEach((rule) => {
    rule.selectors.forEach((selector) => {
      assert.ok(
        selector.startsWith(".take-finding"),
        `带下限的规则必须限定在审片区块自己的 class 前缀里，实际选择器：${selector}`,
      );
    });
  });
  // 区块内所有相关选择器都带 .take-finding 前缀（不许出现裸 p / button / .card）。
  const blockRules = CSS_RULES.filter((rule) =>
    rule.selectors.some((selector) => /^\.take-findings?\b/.test(selector)),
  );
  assert.ok(blockRules.length >= 8, "找不到审片区块的样式规则，选择器可能被改名了");
  assert.doesNotMatch(
    styles,
    /word-break:\s*break-all/,
    "word-break: break-all 会把中文以外的词也强行切断",
  );
});

test("审片行的 DOM 结构没变：徽标 / 时间 / 正文 / 理由 / 采纳 依次排列", () => {
  assert.match(
    html,
    /id="take-findings-list"[^>]*class="take-findings-list"/,
    "审片清单容器要保留 .take-findings-list",
  );
  const start = appSource.indexOf("function renderTakeFindings");
  assert.ok(start > 0, "找不到 renderTakeFindings");
  const render = appSource.slice(start, start + 4000);
  const order = [
    'element("span", "take-finding-kind"',
    'element("span", "take-finding-at"',
    'element("span", "take-finding-issue"',
    'element("span", "take-finding-why"',
    'element("button", "take-finding-adopt"',
  ];
  let cursor = -1;
  order.forEach((snippet) => {
    const at = render.indexOf(snippet);
    assert.ok(at > cursor, `行内元素顺序变了（${snippet}）—— 布局是基于这个顺序的`);
    cursor = at;
  });
});
