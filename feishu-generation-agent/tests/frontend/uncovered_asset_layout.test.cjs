"use strict";

/**
 * 「未使用素材」卡片曾经逐字换行 + 卡片右侧大片空白（2026-10-08 用户报）。
 *
 * 根因：`.excluded-asset-row` 的模板是「68px 预览列 + 1fr 正文列」，而 app.js 给
 * **未使用素材**建的行里只有 `.excluded-asset-copy`（没有预览元素）→ grid 自动放置
 * 把它放进第 1 列（68px），第 2 列空着 → 中文按最小内容宽度折行、卡片却仍被父级
 * grid 拉伸到整行宽。
 *
 * 这些断言锁住的是**修法**：给「未使用素材」那个列表加作用域修饰类，换成单列 +
 * 正文占满。它们不锁具体像素（那要靠真实浏览器），只锁「别把 scoped 规则删掉 /
 * 别改回两列 / 别用 word-break: break-all 强制切断中文」。
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

const SCOPE = ".excluded-asset-list--uncovered";

function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = styles.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  return match?.[1] || "";
}

test("未使用素材列表带自己的作用域修饰类（样式不外溢到其它区块）", () => {
  assert.match(
    html,
    /id="uncovered-asset-list"[^>]*class="[^"]*excluded-asset-list--uncovered/,
    "未使用素材列表必须挂 .excluded-asset-list--uncovered",
  );
  const occurrences = html.match(/excluded-asset-list--uncovered/g) || [];
  assert.equal(occurrences.length, 1, "修饰类只应出现在「未使用素材」这一个容器上");
});

test("未使用素材行改成单列，正文不再被塞进 68px 预览列", () => {
  const rowRule = cssRule(`${SCOPE} .excluded-asset-row`);
  assert.match(
    rowRule,
    /grid-template-columns:\s*minmax\(\s*0\s*,\s*1fr\s*\)/,
    `行模板必须是单列 minmax(0, 1fr)，实际：${rowRule}`,
  );
  assert.doesNotMatch(rowRule, /68px/, "未使用素材行不该再引用 68px 预览列");
  assert.match(
    cssRule(`${SCOPE} .excluded-asset-copy`),
    /width:\s*100%/,
    "正文容器要占满卡片可用宽度",
  );
  assert.match(
    cssRule(SCOPE),
    /gap:\s*(0?\.[7-9]\d*rem|[1-9]\d*(\.\d+)?rem)/,
    `卡片之间要有可见间距（> 基础 .55rem），实际：${cssRule(SCOPE)}`,
  );
});

test("长串靠 overflow-wrap 兜底，不用 word-break 强制切断中文", () => {
  const paragraphRule = cssRule(`${SCOPE} .excluded-asset-copy p`);
  assert.match(paragraphRule, /overflow-wrap:\s*(anywhere|break-word)/);
  assert.doesNotMatch(
    styles,
    /word-break:\s*break-all/,
    "word-break: break-all 会把中文以外的词也强行切断",
  );
});

test("未使用素材的行确实没有预览元素（这正是两列模板不适用的原因）", () => {
  const start = appSource.indexOf('byId("uncovered-asset-list")');
  assert.ok(start > 0, "找不到未使用素材的渲染入口");
  const render = appSource.slice(appSource.indexOf("const uncovered ="), start);
  assert.match(render, /element\("div", "excluded-asset-row"\)/);
  assert.match(render, /element\("div", "excluded-asset-copy"\)/);
  assert.doesNotMatch(
    render,
    /excluded-asset-placeholder|createElement\("img"\)|createElement\("video"\)/,
    "未使用素材行不挂预览；一旦挂了预览就要重新考虑列模板",
  );
});
