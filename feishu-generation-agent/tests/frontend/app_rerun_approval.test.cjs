"use strict";

/**
 * 「重新运行」与「重跑选中任务」必须是**同一条状态机**（用户口径 2026-09-24）：
 *
 *   ① 复用上一版**已批准的计划**（不重新规划，省一次规划）；
 *   ② 返工要求有内容 → 调 rework 融合改写提示词；没内容 → 一个字都不改；
 *   ③ 新运行停在**待审批**，用户能看到改后的提示词/参数，还能继续编辑；
 *   ④ 点「批准并开始生成」之后才真正提交生成。
 *
 * 改前（bug）：重跑选中任务走 `/api/runs/{id}/artifact-review`（**原地**重跑），
 * 提示词改完立刻开始生成 —— 用户根本没有确认机会；底部的「重新运行」还挂着
 * 「已复用上一版计划重跑，不会重新规划」的旧文案。
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

const SOURCE_RUN = "run-source";
const NEW_RUN = "run-new";

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

async function settle() {
  for (let index = 0; index < 8; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function taskPayload(prompt) {
  return {
    task_id: "task-1",
    task_type: "image_to_video",
    title: "花火大会",
    prompt,
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
  };
}

function artifactPayload() {
  return {
    artifact_id: "artifact-1",
    task_id: "task-1",
    kind: "video",
    size: 1024,
    preview_url: "/api/artifacts/artifact-1",
    shot_index: 1,
    shot_total: 2,
    slot_index: 1,
    slot_total: 1,
    task_title: "第一个镜头",
  };
}

function viewFor(runId, status, { prompt, artifacts = [] } = {}) {
  return {
    run_id: runId,
    thread_id: `thread-${runId}`,
    source_url: "https://acme.feishu.cn/docx/rerun-approval",
    status,
    events: [],
    privacy: {},
    artifacts,
    delivery: null,
    approval: {
      document_title: "重跑审批测试",
      revision: 3,
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
      tasks: [taskPayload(prompt)],
    },
  };
}

/** 一条「已批准过」的源运行 + 重跑出来的新运行（停在待审批）。 */
function harness({ sourceStatus, artifacts = [], sourcePrompt = "上一版提示词", newPrompt = "改写后的提示词" }) {
  const requests = [];
  const state = { current: SOURCE_RUN };
  const views = {
    [SOURCE_RUN]: viewFor(SOURCE_RUN, sourceStatus, { prompt: sourcePrompt, artifacts }),
    [NEW_RUN]: viewFor(NEW_RUN, "waiting_approval", { prompt: newPrompt }),
  };
  const fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/provider-preferences") return jsonResponse(200, {});
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [{
        run_id: state.current,
        display_text: "花火大会",
        status: state.current === SOURCE_RUN ? "已完成" : "待审批",
        rerunnable: true,
      }]);
    }
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{
        run_id: state.current,
        display_text: "花火大会",
        status: "已完成",
      }]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (
      options.method === "POST"
      && url === `/api/bitable/runs/${SOURCE_RUN}/rerun`
    ) {
      state.current = NEW_RUN;
      return jsonResponse(202, { run_id: NEW_RUN });
    }
    if (
      options.method === "POST"
      && url === `/api/bitable/runs/${SOURCE_RUN}/rerun-selected`
    ) {
      state.current = NEW_RUN;
      return jsonResponse(202, { run_id: NEW_RUN });
    }
    if (options.method === "POST" && url.endsWith("/artifact-review")) {
      return jsonResponse(202, { ok: true });
    }
    if (url.startsWith("/api/runs/")) {
      const runId = url.slice("/api/runs/".length);
      if (views[runId]) return jsonResponse(200, views[runId]);
    }
    throw new Error(`unexpected request: ${url} ${options.method || "GET"}`);
  };
  return { fetch, requests };
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

function postTo(requests, suffix) {
  return requests.find(
    (request) => request.url.endsWith(suffix) && request.options.method === "POST",
  );
}

test("重新运行：不带返工要求 → 提示词原样保留，新运行停在待审批", async () => {
  const harnessed = harness({ sourceStatus: "succeeded" });
  const app = await loadApp(harnessed.fetch);

  assert.equal(app.getNode("rerun-button").hidden, false, "已结束的多维表格运行要能重跑");
  assert.equal(app.getNode("rerun-button").disabled, false);

  await app.getNode("rerun-button").dispatch("click");
  await settle();

  const sent = postTo(harnessed.requests, `/api/bitable/runs/${SOURCE_RUN}/rerun`);
  assert.ok(sent, "「重新运行」必须走重跑端点");
  assert.deepEqual(JSON.parse(sent.options.body), { feedback: "" });

  // ③ 新运行停在待审批：状态条换成「等待你审核」，批准/退回按钮出现。
  assert.equal(app.getNode("status-badge").textContent, "等待你审核");
  assert.equal(app.getNode("approve-button").hidden, false);
  assert.equal(app.getNode("reject-button").hidden, false);

  // ④ 重跑自己绝不能顺手提交生成或审批决定。
  assert.equal(postTo(harnessed.requests, "/decision"), undefined, "不能顺带批准");
  assert.equal(
    harnessed.requests.some((request) => request.url.includes("/artifact-review")),
    false,
    "不能走原地重跑",
  );

  // 文案必须准确：这次没填返工要求 = 提示词一个字都没改。
  assert.equal(
    app.getNode("action-toast-text").textContent,
    "未填返工要求，提示词原样保留，请审核后批准",
  );
});

test("重跑选中任务：带返工要求 → 融合改写提示词，停在待审批（不再原地重跑）", async () => {
  const harnessed = harness({
    sourceStatus: "waiting_review",
    artifacts: [artifactPayload()],
    newPrompt: "上一版提示词（结尾定格更稳）",
  });
  const app = await loadApp(harnessed.fetch);

  const checkbox = app.getNode("artifact-list").querySelectorAll("input")[0];
  assert.ok(checkbox, "成片卡片上要有「选择此条重跑」复选框");
  checkbox.checked = true;
  await checkbox.dispatch("change");
  await settle();
  assert.equal(
    app.getNode("adjust-artifacts-button").disabled,
    false,
    "只勾选即可点：返工要求可以不填",
  );

  app.getNode("artifact-review-feedback").value = "结尾定格再稳一点";
  await app.getNode("artifact-review-feedback").dispatch("input");
  await app.getNode("adjust-artifacts-button").dispatch("click");
  await settle();

  const sent = postTo(
    harnessed.requests,
    `/api/bitable/runs/${SOURCE_RUN}/rerun-selected`,
  );
  assert.ok(sent, "「重跑选中任务」必须走 rerun-selected（新运行 → 待审批）");
  assert.deepEqual(JSON.parse(sent.options.body), {
    action: "adjust",
    feedback: "结尾定格再稳一点",
    task_ids: ["task-1"],
  });
  assert.equal(
    postTo(harnessed.requests, "/artifact-review"),
    undefined,
    "不能再走原地重跑（那条路会直接开始生成）",
  );

  // ③ 停在审批页，改后的提示词进编辑器，批准按钮可用。
  assert.equal(app.getNode("status-badge").textContent, "等待你审核");
  assert.equal(app.getNode("approve-button").hidden, false);
  const editor = app.getNode("task-list").querySelectorAll("textarea")[0];
  assert.ok(editor, "审批页必须能看到改后的提示词");
  assert.equal(editor.value, "上一版提示词（结尾定格更稳）");

  assert.equal(
    app.getNode("action-toast-text").textContent,
    "已按返工要求优化提示词，请审核后批准",
  );
});

test("重跑选中任务：返工要求为空 → 提示词原样保留，同样进审批页", async () => {
  const harnessed = harness({
    sourceStatus: "waiting_review",
    artifacts: [artifactPayload()],
  });
  const app = await loadApp(harnessed.fetch);

  const checkbox = app.getNode("artifact-list").querySelectorAll("input")[0];
  checkbox.checked = true;
  await checkbox.dispatch("change");
  await settle();
  await app.getNode("adjust-artifacts-button").dispatch("click");
  await settle();

  const sent = postTo(
    harnessed.requests,
    `/api/bitable/runs/${SOURCE_RUN}/rerun-selected`,
  );
  assert.ok(sent, "空返工要求也要能重跑");
  assert.deepEqual(JSON.parse(sent.options.body), {
    action: "adjust",
    feedback: "",
    task_ids: ["task-1"],
  });
  assert.equal(app.getNode("status-badge").textContent, "等待你审核");
  assert.equal(
    app.getNode("action-toast-text").textContent,
    "未填返工要求，提示词原样保留，请审核后批准",
  );
});

test("重跑选中任务：没勾任何成片时不发请求，并说清缺什么", async () => {
  const harnessed = harness({
    sourceStatus: "waiting_review",
    artifacts: [artifactPayload()],
  });
  const app = await loadApp(harnessed.fetch);

  assert.equal(app.getNode("adjust-artifacts-button").disabled, true);
  assert.equal(
    app.getNode("adjust-artifacts-hint").textContent,
    "请先在上方成片卡片里勾选「选择此条重跑」",
  );

  await app.getNode("adjust-artifacts-button").dispatch("click");
  await settle();
  assert.equal(
    harnessed.requests.some((request) => request.url.includes("rerun")),
    false,
    "没勾选就不该发重跑请求",
  );
});
