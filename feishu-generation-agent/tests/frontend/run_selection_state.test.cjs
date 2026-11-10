"use strict";

/**
 * 「选中的是哪一版」要看得见、也要记得住（2026-11-04 用户报）。
 *
 * 用户原话：「现在飞书的任务与历史好像没有保存状态，就是选择哪个历史的时候状态容易乱」，
 * 追问后澄清为「**更换任务后原本选择的历史版本偶尔会被修改，或者状态没有被保存**」。
 *
 * 已确认（可复现）的两个坑：
 *   A. 「切换任务」下拉的选项是**每条记录一个代表版**，而你可能在看**往次版本** ——
 *      `select.value = state.runId` 落在选项之外，真浏览器里 `selectedIndex = -1` ⇒
 *      下拉**显示空白**；左边任务列表的 `is-current` 也只匹配代表版 ⇒ 一行都不高亮。
 *      于是"我选的那一版"在界面上无处安放，看起来就像被改掉了。
 *   B. 前端只把「动画类/真人类/图片类」存进了 localStorage，**你在看哪一版从来没存过** ⇒
 *      刷新/换标签页就丢；加载时只是"恢复进行中的那条"。
 *
 * 本文件钉这三条最终行为：
 *   ① 在看往次版本时，下拉里**有它**且被选中（不会再空白）；
 *   ② 左边属于同一记录的那一行**高亮**，并标出「正在查看往次版本」；
 *   ③ 选中哪一版写进 localStorage，重新打开**优先回到它**；存档失效时回退旧行为并清掉。
 */

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BitableState = require(
  "../../src/feishu_generation_agent/web/static/bitable-state.js"
);
const ReferenceUploadState = require(
  "../../src/feishu_generation_agent/web/static/reference-upload-state.js"
);
const ReferenceMutationState = require(
  "../../src/feishu_generation_agent/web/static/reference-mutation-state.js"
);
const ReviewState = require(
  "../../src/feishu_generation_agent/web/static/review-state.js"
);

const ACTIVE_RUN_KEY = "feishu-agent.active-run";

class FakeNode {
  constructor(tagName = "div", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.value = "";
    this.listeners = new Map();
    this.classList = { toggle() {} };
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  async dispatch(name) {
    await Promise.all(
      (this.listeners.get(name) || []).map((listener) =>
        listener({ target: this })
      ),
    );
  }

  append(...children) {
    this.children.push(...children);
  }

  prepend(...children) {
    this.children.unshift(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  querySelectorAll() {
    return [];
  }

  setAttribute() {}
  removeAttribute() {}
  scrollIntoView() {}
}

function makeStorage(seed = {}) {
  const store = new Map(Object.entries(seed));
  return {
    store,
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  };
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() {
      return payload;
    },
  };
}

async function settle() {
  for (let index = 0; index < 12; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function findNodes(node, predicate, found = []) {
  if (predicate(node)) found.push(node);
  (node.children || []).forEach((child) => findNodes(child, predicate, found));
  return found;
}

function articles(node) {
  return findNodes(node, (child) => child.tagName === "ARTICLE");
}

function allText(node) {
  let text = node.textContent || "";
  (node.children || []).forEach((child) => {
    text += " " + allText(child);
  });
  return text;
}

//: 一条记录两个版本（run-2 新、run-1 旧）+ 另一条记录，模拟真实的「任务 + 往次版本」。
const RECENT_RUNS = [
  { run_id: "run-2", record_id: "rec-a", display_text: "巨型南瓜", status: "已完成", updated_at: "2026-11-04 10:20:00", artifact_count: 1 },
  { run_id: "run-1", record_id: "rec-a", display_text: "巨型南瓜", status: "已完成", updated_at: "2026-11-04 09:10:00", artifact_count: 1 },
  { run_id: "run-b", record_id: "rec-b", display_text: "脱毛", status: "已完成", updated_at: "2026-11-04 09:00:00", artifact_count: 1 },
];

function runView(runId) {
  return {
    run_id: runId,
    thread_id: `t-${runId}`,
    record_id: runId === "run-b" ? "rec-b" : "rec-a",
    status: "succeeded",
    events: [],
    privacy: {},
    approval: { tasks: [], media_assets: [], excluded_assets: [] },
    artifacts: [],
  };
}

function stubFetch(calls, options = {}) {
  return async (url, init = {}) => {
    const method = String(init.method || "GET").toUpperCase();
    calls.push(`${method} ${url}`);
    if (url === "/api/health") return jsonResponse(200, { modes: { bitable: true } });
    if (url === "/api/provider-preferences") return jsonResponse(200, {});
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, options.activeRuns || []);
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, RECENT_RUNS);
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/bitable/record-runs?")) {
      // ⚠️ record_id 必须与列表里的那条**一致**：两边不一致时
      // `needsRecordRuns()` 的判据会恒真，renderArtifactReview ↔ ensureRecordRuns
      // 变成微任务死循环（连定时器都饿死）。真实后端不会这样，夹具也别这样。
      const runId = decodeURIComponent((url.split("run_id=")[1] || "").split("&")[0]);
      const known = [...RECENT_RUNS, ...(options.activeRuns || [])].find(
        (run) => run.run_id === runId,
      );
      const recordId = known?.record_id || "rec-a";
      return jsonResponse(200, {
        record_id: recordId,
        runs: RECENT_RUNS.filter((run) => run.record_id === recordId),
      });
    }
    const matched = url.match(/^\/api\/runs\/([^/?]+)/);
    if (matched) {
      const runId = matched[1];
      if (options.missingRuns?.includes(runId)) {
        return jsonResponse(404, { detail: `运行 ${runId} 不存在` });
      }
      return jsonResponse(200, runView(runId));
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  };
}

async function loadApp(fetch, storage) {
  const nodes = new Map();
  const intervals = new Map();
  let intervalId = 0;
  const getNode = (id) => {
    if (!nodes.has(id)) nodes.set(id, new FakeNode("div", id));
    return nodes.get(id);
  };
  getNode("animation-category-tab").dataset.category = "animation";
  getNode("portrait-category-tab").dataset.category = "portrait";
  const document = {
    createElement: (tagName) => new FakeNode(tagName),
    getElementById: getNode,
    querySelector: () => new FakeNode("main"),
    listeners: {},
    addEventListener(name, listener) {
      (this.listeners[name] ||= []).push(listener);
    },
  };
  const context = {
    BitableState,
    ReferenceMutationState,
    ReferenceUploadState,
    ReviewState,
    document,
    fetch,
    confirm: () => true,
    localStorage: storage,
    location: { reload() {}, pathname: "/" },
    setInterval: (callback) => {
      intervalId += 1;
      intervals.set(intervalId, callback);
      return intervalId;
    },
    clearInterval(id) {
      intervals.delete(id);
    },
    console,
  };
  const source = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8",
  );
  vm.runInNewContext(source, context);
  await settle();
  return { getNode, calls: fetch.calls };
}

/** 选中 run-2，再从历史滑条点「查看这一版」切到 run-1（用户的真实操作路径）。 */
async function viewOlderVersion(app) {
  const switcher = app.getNode("current-run-switcher");
  switcher.value = "run-2";
  await switcher.dispatch("change");
  await settle();
  const cards = findNodes(
    app.getNode("artifact-list"),
    (node) => String(node.className || "").includes("artifact-history-card"),
  );
  const card = cards.find((node) => node.dataset.runId === "run-1");
  assert.ok(card, "前置条件：run-1 要出现在「往次版本」里");
  const open = findNodes(
    card,
    (node) => node.tagName === "BUTTON" && node.textContent === "查看这一版",
  )[0];
  await open.dispatch("click");
  await settle();
}

test("看往次版本时，下拉里有它并且被选中（不再空白）", async () => {
  const storage = makeStorage();
  const app = await loadApp(stubFetch([], {}), storage);

  await viewOlderVersion(app);

  const switcher = app.getNode("current-run-switcher");
  const values = switcher.children.map((option) => option.value);
  assert.ok(
    values.includes("run-1"),
    `下拉必须能表达"我在看 run-1"，实际选项：${values.join(",")}`,
  );
  assert.equal(switcher.value, "run-1", "选中值要落在选项里，否则真浏览器显示空白");
  const option = switcher.children.find((item) => item.value === "run-1");
  assert.match(
    allText(option),
    /往次/,
    "补进来的那一条要标出「往次」，别让人以为是另一条任务",
  );
});

test("看往次版本时，左边属于同一记录的那一行高亮并标出来", async () => {
  const app = await loadApp(stubFetch([], {}), makeStorage());

  await viewOlderVersion(app);

  const rows = articles(app.getNode("recent-run-list"));
  const current = rows.filter((row) => String(row.className).includes("is-current"));
  assert.equal(current.length, 1, "同一时刻只能有一行是「当前」");
  assert.equal(
    current[0].dataset.runId,
    "run-2",
    "看的是 rec-a 的往次版本 → 高亮应该落在这条记录的（代表版）那一行上",
  );
  assert.match(
    allText(current[0]),
    /正在查看往次版本/,
    "要写明「你正在看的是往次版本」",
  );
});

test("选中哪一版会写进 localStorage", async () => {
  const storage = makeStorage();
  const app = await loadApp(stubFetch([], {}), storage);

  await viewOlderVersion(app);

  const raw = storage.getItem(ACTIVE_RUN_KEY);
  assert.ok(raw, `要把选中的版本存起来（key=${ACTIVE_RUN_KEY}）`);
  const saved = JSON.parse(raw);
  assert.equal(saved.run_id, "run-1");
  assert.equal(saved.mode, "bitable", "通道也要存，否则直连/多维表格会认错");
});

test("重新打开优先回到你上次在看的那一版（而不是「进行中的那条」）", async () => {
  const storage = makeStorage({
    [ACTIVE_RUN_KEY]: JSON.stringify({ run_id: "run-1", mode: "bitable" }),
  });
  // 同时有一条**进行中**的任务 —— 旧行为会优先恢复它，从而"改掉"你的选择。
  const calls = [];
  const app = await loadApp(
    stubFetch(calls, {
      activeRuns: [
        { run_id: "run-3", record_id: "rec-c", display_text: "水晶泡泡", status: "生成中", artifact_count: 0 },
      ],
    }),
    storage,
  );

  const got = calls.filter((call) => call.startsWith("GET /api/runs/"));
  assert.ok(
    got.some((call) => call === "GET /api/runs/run-1"),
    `要恢复存档里的 run-1，实际：${got.join(" | ")}`,
  );
  assert.equal(
    app.getNode("current-run-switcher").value,
    "run-1",
    "下拉也要指着恢复回来的那一版",
  );
});

test("存档失效（运行已被删/归档）时回退旧行为", async () => {
  const storage = makeStorage({
    [ACTIVE_RUN_KEY]: JSON.stringify({ run_id: "run-gone", mode: "bitable" }),
  });
  const calls = [];
  const app = await loadApp(
    stubFetch(calls, {
      missingRuns: ["run-gone"],
      activeRuns: [
        { run_id: "run-3", record_id: "rec-c", display_text: "水晶泡泡", status: "生成中", artifact_count: 0 },
      ],
    }),
    storage,
  );
  assert.ok(app);
  assert.ok(
    calls.some((call) => call === "GET /api/runs/run-3"),
    `要回退到「恢复进行中的任务」，实际：${calls.filter((c) => c.startsWith("GET /api/runs/")).join(" | ")}`,
  );
});

test("存档失效且没有可回退的任务时，要把失效存档清掉", async () => {
  // 这条单独拎出来是因为：**有**回退任务时，回退本身会把存档覆盖成新 run，
  // 于是"到底有没有清掉失效项"看不出来（我第一版就漏了这个，变异测试当场抓住）。
  const storage = makeStorage({
    [ACTIVE_RUN_KEY]: JSON.stringify({ run_id: "run-gone", mode: "bitable" }),
  });
  const calls = [];
  await loadApp(
    stubFetch(calls, { missingRuns: ["run-gone"], activeRuns: [] }),
    storage,
  );

  assert.equal(
    storage.getItem(ACTIVE_RUN_KEY),
    null,
    "失效的存档必须清掉，否则每次打开都白拉一次不存在的运行",
  );
});
