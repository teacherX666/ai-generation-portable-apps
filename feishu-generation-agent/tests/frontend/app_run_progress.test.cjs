"use strict";

/**
 * 运行视图里的任务级进度：状态文案旁显示「已完成 / 总数」。
 *
 * 用户 2026-09-23：「3 个任务完成 2 个，界面还是只显示『生成中』，看不出在动」。
 * 进度由服务端在 `view.progress` 里给出（执行阶段不在 checkpoint 里，后端按
 * operations + artifacts 现算），前端只负责在**生成中**的状态文案后加 `2/3`。
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
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll() { return []; }
  setAttribute() {}
  removeAttribute() {}
  scrollIntoView() {}
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() { return payload; },
  };
}

async function settle() {
  for (let i = 0; i < 8; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const approval = {
  revision: 1,
  document_summary: "",
  tasks: [],
  media_assets: [],
  excluded_assets: [],
  selected_task_ids: [],
  coverage: {},
  validation_issues: [],
  ingest_issue_records: [],
  vision_issues: [],
};

async function loadApp(view) {
  const nodes = new Map();
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
    // app.js 会在 document 上装「拖放防误伤」的全局监听（2026-09-29）—— 假 DOM 也得有
    // 这个方法，否则 app.js 一加载就 TypeError。
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
    fetch: async (url) => {
      if (url === "/api/health") {
        return jsonResponse(200, { modes: { bitable: true } });
      }
      if (url === "/api/provider-preferences") return jsonResponse(200, {});
      if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
      if (url === "/api/bitable/active-runs") {
        return jsonResponse(200, [
          {
            run_id: view.run_id,
            display_text: "current",
            status: view.status,
          },
        ]);
      }
      if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
      if (url.startsWith("/api/runs/")) return jsonResponse(200, view);
      throw new Error(`unexpected request: ${url}`);
    },
    confirm: () => true,
    location: { reload() {} },
    setInterval: () => 1,
    clearInterval() {},
    console,
  };
  const source = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8"
  );
  vm.runInNewContext(source, context);
  await settle();
  return { getNode };
}

function viewFor(status, progress) {
  return {
    run_id: "run-progress",
    thread_id: "thread-progress",
    source_url: "https://example.invalid/progress",
    status,
    events: [],
    privacy: {},
    progress,
    approval: { ...approval, document_title: "current" },
  };
}

test("running run shows completed/total next to the status label", async () => {
  const app = await loadApp(
    viewFor("running", { completed: 2, total: 3 })
  );

  assert.equal(app.getNode("status-badge").textContent, "正在执行 2/3");
  assert.equal(app.getNode("run-status").textContent, "正在执行 2/3");
});

test("progress is hidden for a single task and for non-generating states", async () => {
  const single = await loadApp(
    viewFor("running", { completed: 1, total: 1 })
  );
  assert.equal(single.getNode("status-badge").textContent, "正在执行");

  const review = await loadApp(
    viewFor("waiting_review", { completed: 3, total: 3 })
  );
  assert.equal(review.getNode("status-badge").textContent, "成片与结果");

  const missing = await loadApp(viewFor("running", null));
  assert.equal(missing.getNode("status-badge").textContent, "正在执行");
});
