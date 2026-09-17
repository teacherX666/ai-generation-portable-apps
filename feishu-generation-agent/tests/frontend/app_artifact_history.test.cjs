"use strict";

/**
 * 成片预览里的「历史生成」。
 *
 * 用户澄清（2026-09-17）：「我说的是在预览里面体现历史，而不是让你把任务记录叠
 * 在一起，就是之前生成的视频，预览里面能看到历史生成的」。
 *
 * 所以两件事：
 *  1. 任务记录**一版一行**（不要把历次尝试合并成一条）；
 *  2. 预览区（成片与结果）里要把同一条需求**往次生成的成片**摆出来，能直接看/播。
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

function allText(node) {
  let text = node.textContent || "";
  (node.children || []).forEach((child) => {
    text += " " + allText(child);
  });
  return text;
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
  return {
    getNode,
    tick: async () => {
      await Promise.all([...intervals.values()].map((callback) => callback()));
      await settle();
    },
    intervalCount: () => intervals.size,
  };
}

const VIDEO_ARTIFACT = (taskId) => ({
  artifact_id: `art-${taskId}`,
  task_id: taskId,
  kind: "video",
  status: "ready",
  mime_type: "video/mp4",
  size: 1024 * 1024,
  preview_url: `/api/runs/x/artifacts/${taskId}.mp4`,
});

function stubFetch(calls, { activeRun = true, currentStatus = "waiting_review" } = {}) {
  return async (url) => {
    calls.push(url);
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, activeRun
        ? [{ run_id: "run-3", record_id: "rec-a", display_text: "拿着吧你", status: "待确认成片" }]
        : []);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        { run_id: "run-3", record_id: "rec-a", display_text: "拿着吧你", status: "已完成", updated_at: "2026-09-17 10:20:00" },
        { run_id: "run-2", record_id: "rec-a", display_text: "拿着吧你", status: "已完成", updated_at: "2026-09-17 09:40:00" },
        { run_id: "run-1", record_id: "rec-a", display_text: "拿着吧你", status: "失败", updated_at: "2026-09-17 09:10:00" },
        { run_id: "run-b", record_id: "rec-b", display_text: "脱毛", status: "已完成", updated_at: "2026-09-17 09:00:00" },
      ]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/runs/")) {
      const runId = url.split("/").at(-1);
      if (runId === "run-3") {
        return jsonResponse(200, {
          run_id: "run-3",
          thread_id: "t3",
          status: currentStatus,
          events: [],
          privacy: {},
          approval: { tasks: [] },
          artifacts: ["waiting_approval", "failed", "cancelled"].includes(currentStatus)
            ? []
            : [VIDEO_ARTIFACT("task-1")],
        });
      }
      if (runId === "run-2") {
        return jsonResponse(200, {
          run_id: "run-2",
          thread_id: "t2",
          status: "succeeded",
          events: [],
          privacy: {},
          approval: { tasks: [] },
          artifacts: [VIDEO_ARTIFACT("task-1")],
        });
      }
      return jsonResponse(200, {
        run_id: runId,
        thread_id: "t1",
        status: "failed",
        events: [],
        privacy: {},
        approval: { tasks: [] },
        artifacts: [],
      });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("任务记录一条记录一行：重跑不再多出一行", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const rows = app.getNode("recent-run-list").children;
  // rec-a 的三次尝试只占一行（代表＝进行中的 run-3），rec-b 一行。
  assert.equal(rows.length, 2, "一条记录一行");
  assert.deepEqual(
    rows.map((row) => row.dataset.runId),
    ["run-3", "run-b"],
  );
  assert.equal(app.getNode("run-history-summary").textContent, "1 个进行中 · 共 2 条");
  // 也不在列表里展开历次版本（那是成片预览横向滑条的事）。
  assert.equal(
    /历史 \d+ 次/.test(allText(app.getNode("recent-run-list"))),
    false,
  );
});

test("历史成片横向滑条就在「成片与结果」那一栏里，不另开一块", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const strip = app.getNode("artifact-list");
  const cards = findNodes(
    strip,
    (node) => String(node.className || "").includes("artifact-history-card"),
  );

  assert.ok(cards.length >= 1, "历史成片卡片要和当前成片在同一栏里");
  assert.ok(allText(strip).includes("历史生成"), "要有历史生成的分隔标记");
  // 不能另开一个往下的分区。
  assert.equal(app.getNode("artifact-history").children.length, 0);
  // 每个历史版本给一个切换入口。
  assert.ok(
    findNodes(cards[0], (node) => node.tagName === "BUTTON").some(
      (node) => node.textContent === "查看这一版",
    ),
  );
  // 历史成片要能直接播；别的记录（rec-b）不能串进来。
  assert.ok(findNodes(cards[0], (node) => node.tagName === "VIDEO").length >= 1);
  assert.equal(allText(strip).includes("脱毛"), false);
});

test("切换下拉也按记录去重（重跑不再多出一项）", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const switcher = app.getNode("current-run-switcher");
  assert.deepEqual(
    switcher.children.map((option) => option.value),
    ["run-3", "run-b"],
    "下拉里每条记录只一项",
  );
});

test("重跑后（审批/生成中）预览与历史仍然留着", async () => {
  const calls = [];
  const app = await loadApp(
    stubFetch(calls, { currentStatus: "waiting_approval" }),
  );

  // 这就是用户说的「重跑的时候看不到预览」：新版还在审批、没有成片，
  // 面板不能整块消失 —— 往次成片要继续看得到。
  assert.equal(app.getNode("artifact-review").hidden, false, "预览面板要留着");
  const strip = app.getNode("artifact-list");
  assert.ok(allText(strip).includes("历史生成"), "历史滑条要留着");
  assert.ok(
    findNodes(
      strip,
      (node) => String(node.className || "").includes("artifact-history-card"),
    ).length >= 1,
  );
  assert.ok(
    (app.getNode("artifact-review-message").textContent || "").includes("往次成片"),
    "要说明本次还没有成片、下面是往次成片",
  );
});

test("任务记录里不再有「重跑」按钮；失败的那一版到预览页去重跑", async () => {
  const failed = await loadApp(stubFetch([], { currentStatus: "failed" }));
  assert.equal(
    allText(failed.getNode("recent-run-list")).includes("重跑"),
    false,
    "任务记录里不该再有重跑按钮",
  );
  // 失败/取消没有成片可选，原来的「重跑选中任务」不会出现 —— 所以要给一个
  // 「重跑这一版」，否则失败的任务就没地方重跑了。
  assert.equal(failed.getNode("rerun-artifacts-button").hidden, false);

  const reviewing = await loadApp(stubFetch([], { currentStatus: "waiting_review" }));
  assert.equal(
    reviewing.getNode("rerun-artifacts-button").hidden,
    true,
    "能审片的运行走「重跑选中任务」，不需要这个按钮",
  );
});

test("状态全在等人操作时，一个请求都不发", async () => {
  const calls = [];
  // 没有进行中的运行（也就不会被自动恢复并起运行详情轮询），
  // 剩下的全是「等待你审核 / 已完成」—— 状态不会自己变，不该有任何轮询。
  const app = await loadApp(stubFetch(calls, { activeRun: false }));

  const before = calls.length;
  await app.tick();
  await app.tick();
  assert.equal(calls.length, before, "等待审核 / 已完成都不会自己变，不该轮询");
  assert.equal(app.intervalCount(), 0);
});