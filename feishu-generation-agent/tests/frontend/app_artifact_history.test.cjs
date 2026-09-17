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

function stubFetch(calls, { activeRun = true } = {}) {
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
          status: "waiting_review",
          events: [],
          privacy: {},
          approval: { tasks: [] },
          artifacts: [VIDEO_ARTIFACT("task-1")],
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

test("任务记录按每次运行逐条列出，不再把历次尝试叠成一条", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const rows = app.getNode("recent-run-list").children;
  assert.equal(rows.length, 4, "四次尝试就是四行");
  assert.deepEqual(
    rows.map((row) => row.dataset.runId),
    ["run-3", "run-2", "run-1", "run-b"],
  );
  assert.equal(app.getNode("run-history-summary").textContent, "1 个进行中 · 共 4 条");
  // 没有「历史 N 次」这种折叠开关了（「历史 · 状态」是本来就有的行内文案）。
  assert.equal(
    /历史 \d+ 次/.test(allText(app.getNode("recent-run-list"))),
    false,
  );
});

test("预览区把同一条需求的往次成片摆出来，能直接看", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const history = app.getNode("artifact-history");
  const text = allText(history);

  assert.equal(history.hidden, false, "预览区应显示历史成片区");
  assert.ok(text.includes("历史生成"), "要有历史生成分区");
  // run-2 是同一条记录的上一版，它的成片要能看到。
  assert.ok(text.includes("查看这一版"), "每个历史版本要有查看入口");
  const cards = findNodes(history, (node) => node.className === "artifact-history-card");
  assert.ok(cards.length >= 1, "至少渲染出上一版成片卡片");
  // 别的记录（rec-b）不能被串进来。
  assert.equal(text.includes("脱毛"), false);
  // 历史成片用 <video> 呈现（用户要「能看到历史生成的视频」）。
  assert.ok(
    findNodes(history, (node) => node.tagName === "VIDEO").length >= 1,
    "历史成片要能直接播",
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