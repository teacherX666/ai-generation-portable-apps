"use strict";

/**
 * ① 参考素材「单击预览」（用户 2026-10-10：「新增参考素材也能点击预览的功能」）。
 *
 * 这里锁的是**接线**（源文本级断言），真正的交互（lightbox 打开 / Esc 关闭 / 点空白
 * 关闭 / 不破坏原有操作）必须在真实浏览器里验 —— 本仓用极简假 DOM，没有
 * addEventListener/closest，这类 bug 假 DOM 测不出来（CLAUDE.local.md 记过多次）。
 *
 * 顺带锁住一个**真实缺陷**：`openImageLightbox` 一直在 `byId("image-lightbox")`，
 * 但这个元素从来没有写进 index.html —— 于是「双击任意图片放大」在生产里是静默失效的
 * （拿不到元素就 return）。所以第一条断言就是「overlay 必须在 HTML 里存在」。
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
const styles = fs
  .readFileSync(path.join(staticDir, "styles.css"), "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "");

test("lightbox 元素真的写在 index.html 里（否则整套预览静默失效）", () => {
  assert.match(html, /id="image-lightbox"/, "缺 #image-lightbox（openImageLightbox 拿不到元素）");
  assert.match(html, /id="image-lightbox-img"/, "缺 #image-lightbox-img");
  assert.match(
    html,
    /id="image-lightbox"[^>]*hidden/,
    "overlay 必须默认 hidden，否则一进页面就挡住界面",
  );
});

test("只复用同一个 lightbox，不另造第二套", () => {
  const ids = appSource.match(/"image-lightbox(-img)?"/g) || [];
  assert.ok(ids.length > 0, "app.js 必须用 byId 找 image-lightbox");
  assert.doesNotMatch(
    appSource,
    /id\s*=\s*["']image-lightbox-2["']/,
    "不允许再造一套预览层",
  );
  assert.match(appSource, /function openImageLightbox\(/);
  assert.match(appSource, /function closeImageLightbox\(/);
  // Esc 与点空白关闭仍然在
  assert.match(appSource, /event\.key === "Escape"/);
  assert.match(
    appSource,
    /lightboxOverlay\.addEventListener\("click", closeImageLightbox\)/,
  );
});

test("单击预览做能力探测（极简假 DOM 下不能抛异常）", () => {
  const start = appSource.indexOf("function attachSingleClickPreview(");
  assert.ok(start > 0, "找不到 attachSingleClickPreview");
  const end = appSource.indexOf("function previewButton(", start);
  const body = appSource.slice(start, end > start ? end : start + 2000);
  assert.match(
    body,
    /typeof node\.addEventListener !== "function"/,
    "必须先探测 addEventListener 再挂事件",
  );
  assert.match(body, /if \(!node \|\| !url/, "空节点/空 URL 直接返回");
});

test("参考素材缩略图、排除素材、成片卡片都挂了单击预览", () => {
  // 参考素材行（审批页的参考图列表）
  const refStart = appSource.indexOf("function referenceRow(");
  const refEnd = appSource.indexOf("function referenceSection(", refStart);
  const referenceRow = appSource.slice(refStart, refEnd);
  assert.match(
    referenceRow,
    /attachSingleClickPreview\(image, agentUrl\(asset\.preview_url\)\)/,
    "参考素材缩略图必须能单击预览",
  );
  assert.match(
    referenceRow,
    /if \(!isVideo && !isAudio/,
    "视频/音频有播放控件，不能抢它们的单击",
  );
  // 排除素材行
  assert.match(appSource, /attachSingleClickPreview\(preview, agentUrl\(item\.preview_url\)\)/);
  // 成片卡与往次成片卡
  const attachCalls = appSource.match(/attachSingleClickPreview\(image, agentUrl\(artifact\.preview_url\)\)/g) || [];
  assert.equal(attachCalls.length, 2, "成片卡与往次成片卡各一处");
});

test("分镜候选：预览是独立按钮，不抢「点一下 = 选中这张」", () => {
  const start = appSource.indexOf("function storyboardSection(task)");
  const end = appSource.indexOf("function regenerateStoryboardFrame(", start);
  const section = appSource.slice(start, end);
  assert.match(
    section,
    /choice\.addEventListener\("click", \(\) => \{\s*selectStoryboardCandidate/,
    "缩略图单击仍然必须是「选中这张关键帧」",
  );
  assert.match(section, /previewButton\(/, "分镜候选要有独立的预览按钮");
  assert.match(
    section,
    /storyboard-candidate-cell/,
    "候选按钮与预览按钮要并排放在 cell 里（button 不能套 button）",
  );
  // 预览按钮必须阻止冒泡，否则会顺带把候选选中
  const pbStart = appSource.indexOf("function previewButton(");
  const pb = appSource.slice(pbStart, pbStart + 1200);
  assert.match(pb, /stopPropagation/, "预览按钮必须 stopPropagation");
});

test("未使用素材那一行保持**不挂**缩略图（不回退 2026-10-08 的单列排版修复）", () => {
  const start = appSource.indexOf('byId("uncovered-asset-list")');
  const render = appSource.slice(appSource.indexOf("const uncovered ="), start);
  assert.doesNotMatch(
    render,
    /createElement\("img"\)/,
    "未使用素材行不挂缩略图：挂上就必须改回两列模板，等于回退排版修复",
  );
});

test("新样式的类名在 styles.css 里有定义", () => {
  assert.match(styles, /\.previewable\b/);
  assert.match(styles, /\.preview-button\b/);
  assert.match(styles, /\.storyboard-candidate-cell\b/);
});
