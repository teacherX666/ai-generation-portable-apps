"use strict";

/**
 * 任务记录的两条验收（用户 2026-09-16 走查提出）：
 *  1. 同一需求的重跑不再新开任务记录，而是叠在同一条任务下的历史版本里；
 *  2. 不要定时刷新面板 —— 只有状态会「自己往前走」时才盯，状态没变就不请求。
 *
 * 走的是跟 app_category_errors.test.cjs 同一套极简 DOM 脚手架。
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

class FakeNode {
  constructor(tagName = "div", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.disabled = false;
    this.hidden = id === "error-message";
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
  for (let index = 0; index < 8; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function findNodes(node, predicate, found = []) {
  if (predicate(node)) found.push(node);
  node.children.forEach((child) => findNodes(child, predicate, found));
  return found;
}

function textsOf(node, tagName) {
  return findNodes(node, (item) => item.tagName === tagName).map(
    (item) => item.textContent,
  );
}

async function loadApp(fetch) {
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
  };
  const context = {
    BitableState,
    ReferenceMutationState,
    ReferenceUploadState,
    ReviewState,
    document,
    fetch,
    confirm: () => true,
    location: { reload() {} },
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
  return {
    getNode,
    tick: async () => {
      await Promise.all([...intervals.values()].map((callback) => callback()));
      await settle();
    },
    intervalCount: () => intervals.size,
  };
}

function stubFetch(runs, calls) {
  return async (url) => {
    calls.push(url);
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, runs.active);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, runs.recent);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/runs/")) {
      return jsonResponse(200, {
        run_id: url.split("/").at(-1),
        status: "waiting_approval",
        events: [],
        approval: { tasks: [] },
      });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("同一条需求的重跑叠成一条任务记录，历史版本可展开预览", async () => {
  const calls = [];
  const app = await loadApp(
    stubFetch(
      {
        active: [],
        recent: [
          {
            run_id: "run-3",
            record_id: "rec-a",
            display_text: "拿着吧你！",
            status: "已完成",
            updated_at: "2026-09-16 10:57:02",
            rerunnable: true,
          },
          {
            run_id: "run-2",
            record_id: "rec-a",
            display_text: "拿着吧你！",
            status: "失败",
            updated_at: "2026-09-16 10:53:19",
          },
          {
            run_id: "run-1",
            record_id: "rec-a",
            display_text: "拿着吧你！",
            status: "失败",
            updated_at: "2026-09-16 10:35:06",
          },
        ],
      },
      calls,
    ),
  );

  const list = app.getNode("recent-run-list");
  // 三次尝试 → 一条任务记录（重跑不再新开）。
  assert.equal(list.children.length, 1);
  assert.equal(list.children[0].dataset.recordId, "rec-a");
  assert.equal(list.children[0].dataset.runId, "run-3");
  assert.equal(
    app.getNode("run-history-summary").textContent,
    "共 1 条 · 3 个版本",
  );

  // 展开前只有当前版，不带历史行。
  assert.deepEqual(
    textsOf(list, "STRONG").filter((text) => text.startsWith("第 ")),
    [],
  );

  const toggle = findNodes(
    list,
    (node) => node.tagName === "BUTTON" && node.textContent === "历史 2 次",
  );
  assert.equal(toggle.length, 1);
  await toggle[0].dispatch("click");

  // 展开后历史版本叠在同一条任务下面，从最新往前编号。
  const versionTitles = textsOf(app.getNode("recent-run-list"), "STRONG").filter(
    (text) => text.startsWith("第 "),
  );
  assert.deepEqual(versionTitles, ["第 2 版 · 执行失败", "第 1 版 · 执行失败"]);
  const versionRows = findNodes(
    app.getNode("recent-run-list"),
    (node) => node.dataset && node.dataset.runId === "run-2",
  );
  assert.equal(versionRows.length, 1);
  // 历史版本仍然能点「查看」预览（能力不因分组而缩水）。
  assert.ok(
    findNodes(versionRows[0], (node) => node.tagName === "BUTTON").some(
      (node) => node.textContent === "查看",
    ),
  );
});

test("状态全在等人操作时，一个请求都不发（不再是定时刷新）", async () => {
  const calls = [];
  const app = await loadApp(
    stubFetch(
      {
        active: [],
        recent: [
          {
            run_id: "run-wait",
            record_id: "rec-a",
            display_text: "等待审批",
            status: "待审批",
          },
        ],
      },
      calls,
    ),
  );

  const before = calls.length;
  await app.tick();
  await app.tick();
  assert.equal(calls.length, before, "等待审批不会自己变，不该有任何轮询");
  assert.equal(app.intervalCount(), 0);
});

test("有任务在自行推进时才起盯守，停下来了就停", async () => {
  const calls = [];
  const app = await loadApp(
    stubFetch(
      {
        active: [
          {
            run_id: "run-gen",
            record_id: "rec-a",
            display_text: "生成中",
            status: "生成中",
          },
        ],
        recent: [],
      },
      calls,
    ),
  );

  assert.equal(app.intervalCount(), 1, "有任务在生成时应该盯住它");
  const before = calls.length;
  await app.tick();
  assert.ok(calls.length > before, "盯守期间应当对一次任务记录");
});