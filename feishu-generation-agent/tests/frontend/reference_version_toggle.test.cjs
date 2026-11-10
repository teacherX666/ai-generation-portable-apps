"use strict";

/**
 * 参考图的「看原图 / 看已去水印版」切换（用户 2026-10-20 选定）。
 *
 * 背景：参考图去水印被**采用**后，缩略图默认切到清洗版 —— 原图在界面上就看不到了。
 * 而用户 2026-10-10 撤掉旧版去水印功能的理由正是「它会把我的原图改掉，跟原图不一样」。
 * 所以：**原图必须永远看得到**，且切换后单击放大预览要跟着当前显示的版本走。
 *
 * 这一层用源文本断言（本仓前端测试的既有约定：极简假 DOM 测不出交互，交互必须真机验），
 * 真正的 HTTP 行为由 `_diag/verify_reference_preview_fixed.py` 在真实服务上验。
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const agentDir = path.resolve(__dirname, "../..");
const staticDir = path.join(agentDir, "src/feishu_generation_agent/web/static");
const appSource = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const webSource = fs.readFileSync(
  path.join(agentDir, "src/feishu_generation_agent/web/app.py"),
  "utf8",
);
const runtimeSource = fs.readFileSync(
  path.join(agentDir, "src/feishu_generation_agent/graph/runtime.py"),
  "utf8",
);
const moduleSource = fs.readFileSync(
  path.join(
    agentDir,
    "src/feishu_generation_agent/integrations/reference_watermark_clean.py",
  ),
  "utf8",
);

function referenceRow() {
  const start = appSource.indexOf("function referenceRow(");
  const end = appSource.indexOf("function referenceSection(", start);
  assert.ok(start > 0 && end > start, "找不到 referenceRow");
  return appSource.slice(start, end);
}

test("有清洗版的参考图给出「看原图 / 看清洗版」切换", () => {
  const row = referenceRow();
  // 2026-10-23 放开：以前只在 `status === "adopted"` 时给切换，于是**护栏拒绝（kept）的图
  // 用户根本看不到清洗版**，既没法判断护栏判得对不对、也没法推翻它 —— 而那正是用户
  // 「手机卖点两次任务都有水印」看到的现象。
  assert.match(
    row,
    /const hasCleaned = cleanupVersions\.length > 0 \|\| Boolean\(cleanupStatus\?\.cleaned\)/,
    "判定依据改成「有没有产出过清洗版」（含只有 cleaned 的旧台账），不再看护栏采没采纳",
  );
  assert.match(
    row,
    /if \(hasCleaned && !isVideo && !isAudio\)/,
    "切换按钮以「有清洗版」为条件",
  );
  assert.match(row, /"看原图"/, "默认显示清洗版 → 提供「看原图」");
  assert.match(row, /"看清洗版"/, "切到原图后按钮要能切回来");
  assert.match(row, /reference-version-button/, "切换按钮要挂在参考图那一行里");
  assert.match(row, /versionToggle/, "切换按钮必须接进 rowChildren");
});

test("给出「用清洗版 / 用原图 / 重新清洗」三个动作", () => {
  const row = referenceRow();
  assert.match(row, /"用清洗版"/, "要能把「用清洗版」的选择存回去");
  assert.match(row, /"用原图"/, "要能把「用原图」的选择存回去");
  assert.match(row, /"重新清洗"/, "要能重洗这一张");
  assert.match(row, /watermark-decision/, "选择要 POST 到决定端点");
  assert.match(row, /\/reclean/, "重洗要 POST 到重洗端点");
  assert.match(row, /reference-cleanup-controls/, "控件要接进 rowChildren");
  assert.match(row, /cleanupVersions\.length > 1/, "有多个历史版本时要给版本选择器");
});

test("切换走 ?original=1（不新增端点），放大预览跟当前版本走", () => {
  const row = referenceRow();
  assert.match(row, /\?original=1/, "原图入口就是同一个取图接口加 ?original=1");
  assert.match(
    row,
    /attachSingleClickPreview\(image, currentImageUrl\)/,
    "放大预览必须传函数：点击时才取当前版本的 URL，否则切换后放大还是旧版本",
  );
  assert.equal(
    (row.match(/attachSingleClickPreview\(/g) || []).length,
    1,
    "只能挂一次：既挂固定 URL 又挂函数会让一次点击打开两层预览",
  );
  const start = appSource.indexOf("function attachSingleClickPreview(");
  const body = appSource.slice(start, start + 1400);
  assert.match(body, /typeof url === "function"/, "attachSingleClickPreview 要支持函数形式的 URL");
  assert.match(body, /resolveUrl\(\)/, "点击/回车时再解析 URL");
});

test("后端把 ?original=1 翻成 prefer_cleaned=False，默认仍给清洗版", () => {
  assert.match(
    webSource,
    /original: bool = False/,
    "取图端点要有 original 查询参数",
  );
  assert.match(
    webSource,
    /prefer_cleaned=not original/,
    "original=1 时必须关掉「优先清洗版」",
  );
  assert.match(
    runtimeSource,
    /prefer_cleaned: bool = True/,
    "get_reference_file 要有开关，默认保持原行为",
  );
  assert.match(
    runtimeSource,
    /if prefer_cleaned:\s*\n\s*cleaned = _adopted_cleanup_path/,
    "清洗版偏好必须被开关包住",
  );
});

test("清洗版路径要绝对化（否则缩略图 404、预览空白）", () => {
  // 2026-10-23：解析逻辑收进 `resolve_cleaned_path`（预览与**提交**共用同一口径，
  // 免得两处各判一套又变回不一致）—— **不变量不变**：相对路径必须按应用根目录绝对化。
  const start = moduleSource.indexOf("def resolve_cleaned_path(");
  assert.ok(start > 0, "找不到 resolve_cleaned_path");
  const body = moduleSource.slice(start, start + 900);
  assert.match(
    body,
    /Path\.cwd\(\) \/ path/,
    "台账里的相对路径必须按应用根目录解析成绝对路径",
  );
  assert.match(
    runtimeSource,
    /resolved_path = cleaned\.resolve\(\)/,
    "跟绝对允许根比对前必须 resolve（2026-10-20 的 404 根因）",
  );
});
