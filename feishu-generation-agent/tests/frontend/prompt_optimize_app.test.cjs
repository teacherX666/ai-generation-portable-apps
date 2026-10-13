"use strict";

/**
 * 审批页（等待你审核）的**原地**提示词优化。
 *
 * 2026-09-23 用户要求：「在等待你审核的阶段，也可以使用退回重跑功能优化提示词」。
 * 勾选复用审批页已有的任务勾选框 —— 勾哪几条就优化哪几条。
 */

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC = join(__dirname, "../../src/feishu_generation_agent/web/static");
const BitableState = require(join(STATIC, "bitable-state.js"));
const ReferenceMutationState = require(join(STATIC, "reference-mutation-state.js"));
const ReferenceUploadState = require(join(STATIC, "reference-upload-state.js"));
const ReviewState = require(join(STATIC, "review-state.js"));

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
    this.className = "";
    this.listeners = new Map();
    this.classList = { toggle() {} };
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  dispatch(name) {
    return Promise.all(
      (this.listeners.get(name) || []).map((listener) => listener({ target: this })),
    );
  }

  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll(selector) {
    const tags = new Set(
      selector.split(",").map((value) => value.trim().toUpperCase()),
    );
    return descendants(this).filter((node) => tags.has(node.tagName));
  }
  setAttribute(name, value) { this[name] = String(value); }
  removeAttribute(name) { delete this[name]; }
  scrollIntoView() {}
  focus() {}
}

function descendants(node) {
  const result = [];
  const visit = (child) => {
    if (!child || typeof child !== "object") return;
    result.push(child);
    (child.children || []).forEach(visit);
  };
  (node.children || []).forEach(visit);
  return result;
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() { return payload; },
  };
}

function approvalView() {
  return {
    run_id: "run-1",
    thread_id: "thread-1",
    source_url: "https://acme.feishu.cn/docx/optimize",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "提示词优化测试",
      revision: 7,
      document_summary: "",
      selected_task_ids: [],
      media_assets: [],
      excluded_assets: [],
      vision_descriptions: [],
      validation_issues: [],
      ingest_issue_records: [],
      vision_issues: [],
      coverage: {
        successful_total: 0,
        referenced_count: 0,
        excluded_count: 0,
        uncovered_count: 0,
        failed_count: 0,
      },
      tasks: [{
        task_id: "task-1",
        task_type: "image_to_video",
        title: "自由女神",
        prompt: "自由女神在晨光中缓缓抬起手臂",
        negative_constraints: [],
        reference_mode: "multi_reference",
        reference_images: [],
        aspect_ratio: "16:9",
        duration: 10,
        resolution: "720p",
        generate_audio: false,
        output_count: 1,
        assumptions: [],
        warnings: [],
        blocking_issues: [],
      }],
    },
  };
}

async function settle() {
  for (let index = 0; index < 8; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function loadApp(fetch) {
  const nodes = new Map();
  const getNode = (id) => {
    if (!nodes.has(id)) nodes.set(id, new FakeNode("div", id));
    return nodes.get(id);
  };
  getNode("animation-category-tab").dataset.category = "animation";
  getNode("portrait-category-tab").dataset.category = "portrait";
  const context = {
    BitableState,
    ReferenceMutationState,
    ReferenceUploadState,
    ReviewState,
    document: {
      createElement: (tagName) => new FakeNode(tagName),
      getElementById: getNode,
      querySelector: () => new FakeNode("main"),
      // app.js 会在 document 上装「拖放防误伤」的全局监听（2026-09-29）—— 假 DOM 也得有
      // 这个方法，否则 app.js 一加载就 TypeError。
      listeners: {},
      addEventListener(name, listener) {
        (this.listeners[name] ||= []).push(listener);
      },
    },
    fetch,
    confirm: () => true,
    location: { pathname: "/", reload() {} },
    setInterval: () => 1,
    clearInterval() {},
    console,
  };
  for (const file of ["api-paths.js", "app.js"]) {
    vm.runInNewContext(readFileSync(join(STATIC, file), "utf8"), context);
  }
  await settle();
  return { getNode };
}

function fetchForRun(requests) {
  const view = approvalView();
  return async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{ run_id: "run-1", display_text: "自由女神" }]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url === "/api/runs/run-1" && (!options.method || options.method === "GET")) {
      return jsonResponse(200, view);
    }
    if (url === "/api/runs/run-1/optimize-prompts" && options.method === "POST") {
      return jsonResponse(200, {
        run_id: "run-1",
        status: "updated",
        optimized_task_ids: ["task-1"],
        unchanged_task_ids: [],
      });
    }
    throw new Error(`unexpected request: ${url} ${options.method || "GET"}`);
  };
}

test("approval page can optimise the selected prompts in place", async () => {
  const requests = [];
  const app = await loadApp(fetchForRun(requests));

  // 审批页才显示这块。任务默认是**全选**的（ReviewState.initialSelection：
  // 进入等待审批且服务端没给选择时默认全选），所以一开始只差「填要求」。
  assert.equal(app.getNode("prompt-optimize-box").hidden, false);
  assert.equal(app.getNode("optimize-prompts-button").disabled, true);
  assert.match(app.getNode("optimize-prompts-hint").textContent, /填写优化要求/);

  const taskList = app.getNode("task-list");
  const checkbox = taskList.querySelectorAll("input")[0];
  assert.equal(checkbox.dataset.taskId, "task-1");
  assert.equal(checkbox.checked, true, "审批页任务默认全选");

  // 取消勾选 → 按钮仍点不动，提示换成「先勾选」。
  checkbox.checked = false;
  await checkbox.dispatch("change");
  await settle();
  assert.equal(app.getNode("optimize-prompts-button").disabled, true);
  assert.match(app.getNode("optimize-prompts-hint").textContent, /勾选要优化的任务/);

  // 重新勾上 → 提示回到「填要求」。
  checkbox.checked = true;
  await checkbox.dispatch("change");
  await settle();
  assert.match(app.getNode("optimize-prompts-hint").textContent, /填写优化要求/);

  const feedback = app.getNode("prompt-optimize-feedback");
  feedback.value = "手臂抬起的动作再慢一点";
  await feedback.dispatch("input");
  await settle();
  assert.equal(app.getNode("optimize-prompts-button").disabled, false);

  await app.getNode("optimize-prompts-button").dispatch("click");
  await settle();

  const sent = requests.find((request) => (
    request.url === "/api/runs/run-1/optimize-prompts"
    && request.options.method === "POST"
  ));
  assert.ok(sent, "必须打到原地优化端点");
  assert.deepEqual(JSON.parse(sent.options.body), {
    task_ids: ["task-1"],
    feedback: "手臂抬起的动作再慢一点",
  });

  // 优化完仍停在审批页，输入框清空，且**没有**触发重新规划 / 新运行。
  assert.equal(app.getNode("prompt-optimize-box").hidden, false);
  assert.equal(app.getNode("prompt-optimize-feedback").value, "");
  assert.equal(
    requests.some((request) => request.url.includes("/decision")),
    false,
    "优化不能顺带提交审批决定",
  );
  assert.equal(
    requests.some((request) => request.url.includes("/rerun")),
    false,
    "优化不能新建运行",
  );
});

test("prompt optimiser stays hidden outside the approval stage", async () => {
  const requests = [];
  const fetch = fetchForRun(requests);
  const app = await loadApp(async (url, options = {}) => {
    if (url === "/api/runs/run-1" && (!options.method || options.method === "GET")) {
      return jsonResponse(200, { ...approvalView(), status: "succeeded" });
    }
    return fetch(url, options);
  });

  assert.equal(app.getNode("prompt-optimize-box").hidden, true);
});
