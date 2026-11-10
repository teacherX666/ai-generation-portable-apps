"use strict";

/**
 * 「已排除的素材」行上的两个新操作（2026-11-04 用户需求）。
 *
 * 用户原话：「已排除的素材可不可以加一个删除和重新挂素材的功能」。两个语义由他自己选定：
 *
 *   * **删除** = 从**这次运行**里移除该素材，服务器上的文件保留
 *     → `DELETE /api/runs/{run_id}/assets/{asset_id}`（新增端点）；
 *   * **重新挂** = 把这一条**恢复并挂回某个任务**（挂上后自动从「已排除」消失）
 *     → 复用已有的 `PATCH /api/runs/{run_id}/tasks/{task_id}/references`，
 *       请求体必须带上**该任务原有的参考**，不能把原来的挤掉。
 *
 * 角色规则（首帧/尾帧必须成对、first_last_frame 只收两张…）**不在前端复刻** ——
 * 服务端拒绝时把它的原话显示出来即可（仓里的教训：两处各判一套必然打架）。
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
  for (let index = 0; index < 10; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function findNodes(node, predicate, found = []) {
  if (predicate(node)) found.push(node);
  (node.children || []).forEach((child) => findNodes(child, predicate, found));
  return found;
}

function buttonsOf(node) {
  return findNodes(node, (child) => child.tagName === "BUTTON");
}

function buttonByText(node, text) {
  return buttonsOf(node).find((button) => button.textContent === text) || null;
}

function allText(node) {
  let text = node.textContent || "";
  (node.children || []).forEach((child) => {
    text += " " + allText(child);
  });
  return text;
}

async function loadApp(fetch, overrides = {}) {
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
    ...overrides,
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
  };
}

const TASK = {
  task_id: "task-1",
  task_type: "image_to_video",
  title: "巨型南瓜",
  source_block_ids: ["block-1"],
  user_intent: "生成片头",
  prompt: "吉卜力风格。参考 @图片1。",
  reference_images: [{ asset_id: "image-1", role: "reference_image", order: 1 }],
  reference_plan: [
    {
      asset_id: "image-1",
      role: "global",
      segment_index: null,
      usable: true,
      reason: "角色参考图",
    },
  ],
  aspect_ratio: "9:16",
  duration: 12,
  resolution: "720p",
};

const VIEW = {
  run_id: "run-cur",
  thread_id: "t-cur",
  record_id: "rec-a",
  status: "waiting_approval",
  events: [],
  privacy: {},
  approval: {
    tasks: [TASK],
    media_assets: [
      {
        asset_id: "image-1",
        mime_type: "image/png",
        preview_url: "/api/runs/run-cur/references/image-1/content",
      },
      {
        asset_id: "image-3",
        mime_type: "image/png",
        preview_url: "/api/runs/run-cur/references/image-3/content",
      },
    ],
    excluded_assets: [
      { asset_id: "image-3", reason: "计划没有具体使用这张素材" },
    ],
  },
  artifacts: [],
};

function stubFetch(calls, options = {}) {
  return async (url, init = {}) => {
    const method = String(init.method || "GET").toUpperCase();
    calls.push({
      url,
      method,
      body: init.body ? JSON.parse(init.body) : null,
    });
    if (url === "/api/health") return jsonResponse(200, { modes: { bitable: true } });
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [
        { run_id: "run-cur", record_id: "rec-a", display_text: "巨型南瓜", status: "待审批", artifact_count: 0 },
      ]);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        { run_id: "run-cur", record_id: "rec-a", display_text: "巨型南瓜", status: "待审批", updated_at: "2026-11-04 10:00:00", artifact_count: 0 },
      ]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/bitable/record-runs?")) {
      return jsonResponse(200, { record_id: "rec-a", runs: [] });
    }
    if (method === "DELETE" && url.startsWith("/api/runs/run-cur/assets/")) {
      return jsonResponse(200, { status: "removed" });
    }
    if (method === "PATCH" && url.endsWith("/references")) {
      if (options.referencePatchError) {
        return jsonResponse(400, { detail: options.referencePatchError });
      }
      return jsonResponse(200, { status: "updated" });
    }
    if (url.startsWith("/api/runs/run-cur")) {
      return jsonResponse(200, VIEW);
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  };
}

test("每条已排除素材都给了「删除」和「重新挂…」两个入口", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const list = app.getNode("excluded-asset-list");
  assert.match(allText(list), /image-3/, "前置条件：image-3 在「已排除」里");
  assert.ok(buttonByText(list, "删除"), "缺「删除」按钮");
  assert.ok(buttonByText(list, "重新挂…"), "缺「重新挂」按钮");
});

test("点「删除」走 DELETE /api/runs/{run}/assets/{asset_id}", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  await buttonByText(app.getNode("excluded-asset-list"), "删除").dispatch("click");
  await settle();

  const deletes = calls.filter((call) => call.method === "DELETE");
  assert.equal(deletes.length, 1, "应当正好发一次 DELETE");
  assert.equal(deletes[0].url, "/api/runs/run-cur/assets/image-3");
});

test("确认框里点「取消」就不发请求", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls), { confirm: () => false });

  await buttonByText(app.getNode("excluded-asset-list"), "删除").dispatch("click");
  await settle();

  assert.equal(
    calls.filter((call) => call.method === "DELETE").length,
    0,
    "用户没确认就不许删",
  );
});

test("「重新挂…」列出任务与用途，挂上时带上该任务原有参考", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));
  const list = app.getNode("excluded-asset-list");

  await buttonByText(list, "重新挂…").dispatch("click");
  await settle();

  const selects = findNodes(list, (node) => node.tagName === "SELECT");
  assert.equal(selects.length, 2, "要有一个任务下拉 + 一个用途下拉");
  const taskSelect = selects[0];
  assert.deepEqual(
    taskSelect.children.map((option) => option.value),
    ["task-1"],
    "任务下拉列的是当前计划里的任务",
  );
  assert.match(allText(taskSelect), /巨型南瓜/, "任务要有标题，别只给 task_id");
  const roleSelect = selects[1];
  assert.deepEqual(
    roleSelect.children.map((option) => option.value),
    ["reference_image", "first_frame", "last_frame"],
    "图片素材的用途：参考图 / 首帧 / 尾帧",
  );

  await buttonByText(list, "挂上").dispatch("click");
  await settle();

  const patches = calls.filter((call) => call.method === "PATCH");
  assert.equal(patches.length, 1, "应当正好发一次 PATCH");
  assert.equal(patches[0].url, "/api/runs/run-cur/tasks/task-1/references");
  assert.deepEqual(
    patches[0].body.references,
    [
      { asset_id: "image-1", role: "reference_image", order: 1 },
      { asset_id: "image-3", role: "reference_image", order: 2 },
    ],
    "必须带上原有的 image-1，不能把它挤掉；新素材排在最后",
  );
});

test("服务端拒绝时把它的原话显示出来（规则不在前端复刻）", async () => {
  const calls = [];
  const app = await loadApp(
    stubFetch(calls, { referencePatchError: "首帧和尾帧必须成对出现" }),
  );
  const list = app.getNode("excluded-asset-list");

  await buttonByText(list, "重新挂…").dispatch("click");
  await settle();
  await buttonByText(list, "挂上").dispatch("click");
  await settle();

  assert.match(
    app.getNode("error-message").textContent,
    /首帧和尾帧必须成对出现/,
  );
});
