// 「Token 消耗（火山方舟）」区块的前端回归测试。
//
// 仓库既有做法（见 test_history_sidebar.mjs）：stub 掉 DOM 全局，把 app.js 源码塞进
// new Function 取出组件工厂，然后直接调它的方法。这里同样处理 StatsApp。
//
// 两层保障：
//   1. 方法行为：区间数据 → 每人每应用 token 与合计。
//   2. 模板/方法不脱节：解析真实 index.html 里的那个 <section>，确认它引用的每个
//      方法在 StatsApp 上真的存在。这能挡住「改了模板没加方法」把整页 Vue 打挂 ——
//      而登录态不可得，线上没法用眼睛验。

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appJs = fs.readFileSync(path.join(ROOT, "portal/static/app.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(ROOT, "portal/static/index.html"), "utf8");

const elStub = () => ({ addEventListener() {}, classList: { toggle() {} }, style: {}, value: "", textContent: "", dataset: {} });
globalThis.document = {
  getElementById: () => elStub(),
  querySelector: () => null,
  querySelectorAll: () => [],
  body: elStub(),
  addEventListener() {},
  createElement: () => elStub(),
};
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async () => {} } }, configurable: true });
globalThis.location = { pathname: "/", search: "", replace() {}, protocol: "https:", host: "x" };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
globalThis.PetiteVue = { createApp: () => ({ mount() {} }), reactive: (v) => v };
globalThis.setInterval = () => 0;

const fn = new Function(appJs + "\nreturn { StatsApp };");
const { StatsApp } = fn();
if (typeof StatsApp !== "function") throw new Error("StatsApp not defined");

function fail(msg) {
  console.error("FAIL: " + msg);
  process.exit(1);
}

// ── 1. 方法行为 ────────────────────────────────────────────────────────────────
const app = StatsApp();
app.rangeData = {
  dates: ["2026-10-10", "2026-10-11", "2026-10-12"],
  users: {
    alice: {
      seedance: { images: [0, 0, 0], seconds: [15, 0, 15], tokens: [260100, 0, 540900] },
    },
    bob: {
      "nano-banana": { images: [0, 2, 0], seconds: [0, 0, 0], tokens: [0, 23107, 0] },
    },
    carol: {
      // 走 Chiyun/本地通道：有产出但没有任何 token
      "nano-banana": { images: [1, 0, 0], seconds: [0, 0, 0], tokens: [0, 0, 0] },
    },
  },
};

// 千分位格式化（模板里的展示口径）
if (app.fmtTokens(540900) !== "540,900") fail("fmtTokens(540900) => " + app.fmtTokens(540900));
if (app.fmtTokens(0) !== "0") fail("fmtTokens(0) => " + app.fmtTokens(0));
if (app.fmtTokens(undefined) !== "0") fail("fmtTokens(undefined) => " + app.fmtTokens(undefined));

// 每人合计（跨应用、跨日期求和）
const alice = app.rangeTokenTotals("alice");
if (alice.total !== 801000) fail("alice total => " + alice.total);
if (alice.perApp.seedance !== 801000) fail("alice perApp.seedance => " + alice.perApp.seedance);
const bob = app.rangeTokenTotals("bob");
if (bob.total !== 23107) fail("bob total => " + bob.total);
if (bob.perApp["nano-banana"] !== 23107) fail("bob perApp[nano-banana] => " + bob.perApp["nano-banana"]);

// 一张图都没走方舟的用户：总量为 0（区块靠这个把行藏起来）
const carol = app.rangeTokenTotals("carol");
if (carol.total !== 0) fail("carol total should be 0, got " + carol.total);

// 只在真的产生过 token 时才出列，且按固定顺序
const list = app.tokenAppList();
if (list.join(",") !== "seedance,nano-banana") fail("tokenAppList => " + list.join(","));

// 全场合计
if (app.rangeTokenGrandTotal() !== 824107) fail("grand total => " + app.rangeTokenGrandTotal());

// 未知用户不炸
if (app.rangeTokenTotals("nobody").total !== 0) fail("unknown user should be 0");

// 老数据（没有 tokens 键）不炸、按 0 处理
app.rangeData = { dates: ["2026-10-01"], users: { dave: { seedance: { images: [0], seconds: [12] } } } };
if (app.rangeTokenTotals("dave").total !== 0) fail("legacy row total should be 0");
if (app.tokenAppList().length !== 0) fail("legacy row must not produce token columns");
if (app.rangeTokenGrandTotal() !== 0) fail("legacy grand total should be 0");

// 空区间
app.rangeData = { dates: [], users: {} };
if (app.rangeTokenGrandTotal() !== 0) fail("empty range grand total should be 0");
if (app.tokenAppList().length !== 0) fail("empty range must not produce token columns");

// ── 2. 模板与公法不脱节 ───────────────────────────────────────────────────────
// 每个区块：marker 定位 <section>，needles 断言关键绑定在场。
// 2026-10-12 合并页面：按用户统计 / 按日期查看 / 按日期段查询 / Token 消耗 已被
// 总览 + 使用记录 + 汇总 取代（保留「按用户趋势」「最近活动」）。
const SECTIONS = [
  {
    marker: "总览",
    needles: ["genResetAndLoad()", "genUserOptions()", "genAppOptions()",
              "fmtTokens(genData.total || 0)", "fmtMoney((genData.summary || {}).amount)"],
    label: "总览",
  },
  {
    marker: "使用记录",
    needles: ["exportGenerations()", "exportGenerationsSummary()",
              "fmtUsage(row)", "fmtMoney(row.amount)", "genModelLabel(row)",
              "genPromptBrief(row)", "fmtUnitPrice(row)", "genStatusText(row.status)",
              "setGenPage(genPage - 1)", "setGenPage(genPage + 1)", "genTotalPages()"],
    label: "使用记录",
  },
  {
    marker: "汇总（按天 × 计费项",
    needles: ["(genData.billSummary || []).length", "b['计费项']", "b['金额(元)']"],
    label: "汇总",
  },
];

const JS_BUILTINS = new Set([
  "Object", "String", "Number", "Math", "JSON", "Array", "Boolean", "Date",
  "parseInt", "parseFloat", "isNaN", "Set",
]);

for (const spec of SECTIONS) {
  // 用 `<h2>标题` 定位，别用裸字符串：导航分组标题也可能叫「总览」（2026-10-13 就撞了），
  // 裸匹配会抓到导航那一行，再往前找 <section> 就落到别的区块上。
  const start = indexHtml.indexOf("<h2>" + spec.marker);
  if (start < 0) fail("index.html 里找不到「" + spec.marker + "」区块");
  const sectionStart = indexHtml.lastIndexOf("<section", start);
  const end = indexHtml.indexOf("</section>", sectionStart);
  if (end < 0) fail(spec.marker + " 区块没有正常闭合");
  const section = indexHtml.slice(sectionStart, end);

  // 只认「名字紧跟 (」的调用形式；不吞 v-for="x in (...)"/v-if="a (b)" 里的 Vue 关键字。
  const referenced = new Set();
  const callRe = /(\.?)([A-Za-z_$][\w$]*)\(/g;
  let m;
  while ((m = callRe.exec(section)) !== null) {
    if (m[1] === ".") continue;
    if (JS_BUILTINS.has(m[2])) continue;
    referenced.add(m[2]);
  }
  // 汇总区块是纯数据绑定（不调组件方法），所以「零方法引用」不算失败；
  // 真正要守住的是下面那些关键绑定必须在场。
  for (const name of referenced) {
    if (typeof app[name] !== "function") {
      fail(`模板引用了 StatsApp 上不存在的方法 ${name}()（${spec.label} 区块）—— 页面会渲染失败`);
    }
  }
  for (const needle of spec.needles) {
    if (!section.includes(needle)) fail(`${spec.label} 区块缺少绑定：${needle}`);
  }
  console.log(`${spec.label}: ok (${referenced.size} methods: ${[...referenced].sort().join(", ")})`);
}
if (!/管理员可见全部用户/.test(JSON.parse(JSON.stringify(indexHtml)).toString())) {
  fail("页面缺少可见性说明");
}

// ── 3. 生成明细的金额格式化与「不猜价格」 ─────────────────────────────────────
app.genData = {
  rows: [
    { kind: "video", tokens: 40594, images: 0, seconds: 4, amount: 0.9337,
      unit_price: 23, unit: "CNY/百万token", price_family: "Seedance 2.0 mini",
      task_ids: ["cgt-x"], job_id: "j1", username: "alice", app: "seedance", status: "succeeded" },
    { kind: "image", tokens: 0, images: 2, seconds: 0, amount: null,
      unit_price: null, unit: "", price_family: "", task_ids: [], job_id: "j2",
      username: "bob", app: "nano-banana", status: "succeeded", model: "banana2-ssvip" },
  ],
  summary: { count: 2, tokens: 40594, amount: 0.9337, priced_count: 1, unpriced_count: 1 },
};
if (app.fmtMoney(0.9337) !== "¥0.9337") fail("fmtMoney small => " + app.fmtMoney(0.9337));
if (app.fmtMoney(12.5) !== "¥12.50") fail("fmtMoney large => " + app.fmtMoney(12.5));
if (app.fmtMoney(null) !== "—") fail("无单价必须显示「—」而不是 0");
if (app.fmtUsage(app.genData.rows[0]) !== "40,594 token · 4s") fail("fmtUsage video => " + app.fmtUsage(app.genData.rows[0]));
if (app.fmtUsage(app.genData.rows[1]) !== "2 张") fail("fmtUsage image => " + app.fmtUsage(app.genData.rows[1]));
if (app.genTaskId(app.genData.rows[0]) !== "cgt-x") fail("genTaskId should prefer ark task id");
if (app.genTaskId(app.genData.rows[1]) !== "j2") fail("genTaskId falls back to job id");
if (app.fmtUnitPrice(app.genData.rows[0]) !== "23 元/百万") fail("fmtUnitPrice => " + app.fmtUnitPrice(app.genData.rows[0]));
if (app.fmtUnitPrice(app.genData.rows[1]) !== "—") fail("fmtUnitPrice null => " + app.fmtUnitPrice(app.genData.rows[1]));
if (app.genModelLabel(app.genData.rows[1]) !== "banana2-ssvip") fail("genModelLabel should fall back to raw model");
const summary = app.genRangeSummary();
if (!summary.includes("2 次生成") || !summary.includes("1 条无单价")) fail("genRangeSummary => " + summary);

console.log("token section: all sections ok");
process.exit(0);
