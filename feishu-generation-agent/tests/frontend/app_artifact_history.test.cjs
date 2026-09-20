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

function stubFetch(calls, { activeRun = true, currentStatus = "waiting_review", generatingRun = false, uncoveredAssets = false } = {}) {
  return async (url) => {
    calls.push(url);
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, activeRun
        ? [{ run_id: "run-3", record_id: "rec-a", display_text: "拿着吧你", status: "待确认成片", artifact_count: 2 }]
        : []);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        { run_id: "run-3", record_id: "rec-a", display_text: "拿着吧你", status: "已完成", updated_at: "2026-09-17 10:20:00", artifact_count: 2 },
        { run_id: "run-2", record_id: "rec-a", display_text: "拿着吧你", status: "已完成", updated_at: "2026-09-17 09:40:00", artifact_count: 1 },
        { run_id: "run-1", record_id: "rec-a", display_text: "拿着吧你", status: "失败", updated_at: "2026-09-17 09:10:00", artifact_count: 0 },
        { run_id: "run-b", record_id: "rec-b", display_text: "脱毛", status: generatingRun ? "生成中" : "已完成", updated_at: "2026-09-17 09:00:00", artifact_count: 4 },
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
          approval: {
            tasks: [],
            // 未覆盖的素材：既没被引用、也没被排除 —— 批准按钮会一直是灰的。
            media_assets: uncoveredAssets
              ? [
                  { asset_id: "image-4", mime_type: "image/png" },
                  { asset_id: "image-5", mime_type: "image/png" },
                ]
              : [],
            excluded_assets: [],
          },
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

test("任务记录里不再有「重跑」按钮；重跑入口在预览页且已合并", async () => {
  const failed = await loadApp(stubFetch([], { currentStatus: "failed" }));
  assert.equal(
    allText(failed.getNode("recent-run-list")).includes("重跑"),
    false,
    "任务记录里不该再有重跑按钮",
  );
  // 2026-09-18 用户要求把「重新运行」和「重跑选中任务」合并成一个：
  // 返工要求为空 → 显示「退回审核」（不用 AI）；有内容 → 显示「重跑选中任务」。
  assert.equal(failed.getNode("rerun-artifacts-button").hidden, true);
  assert.equal(failed.getNode("adjust-artifacts-button").hidden, false);
  assert.equal(
    failed.getNode("adjust-artifacts-button").textContent.trim(),
    "退回审核",
  );

  const reviewing = await loadApp(stubFetch([], { currentStatus: "waiting_review" }));
  assert.equal(reviewing.getNode("adjust-artifacts-button").hidden, false);
  assert.equal(
    reviewing.getNode("adjust-artifacts-button").textContent.trim(),
    "退回审核",
  );
});

test("任务记录外面直接看得到已成片条数", async () => {
  const app = await loadApp(stubFetch([]));
  const rows = Array.from(app.getNode("recent-run-list").children);

  // rec-a 的历次尝试成片数累加：2 + 1 + 0 = 3 条。
  assert.ok(
    allText(rows[0]).includes("已成片 3 条"),
    "rec-a 应显示累加的成片条数，实际：" + allText(rows[0]),
  );
  assert.ok(allText(rows[1]).includes("已成片 4 条"));
});

test("成片预览的重绘签名带上 busy（否则置灰的按钮永远回不来）", () => {
  const app = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8",
  );
  // 卡片按钮按 state.busy 置灰，而重绘只在签名变化时发生 —— 签名少了 busy，
  // 「查看这一版」会在选中运行的瞬间被置灰后再也回不来（用户报「点不了」）。
  assert.match(app, /status: view\.status,[\s\S]{0,220}?busy: state\.busy,/);
  // 更根本的一条：历史卡片的「查看这一版」是纯导航，**不能**按 busy 置灰 ——
  // 终态运行不会再轮询，置灰后没有任何机会恢复。
  assert.equal(/open\.disabled = state\.busy/.test(app), false);
});

test("生成中的任务不给删除按钮（只看 active 标记不可靠）", async () => {
  // 别的窗口/会话起的任务在本页拿不到 active 标记 —— 旧代码只看 run.active，
  // 于是「生成中」的行也挂着删除按钮，点了会把正在跑的任务删掉。
  const app = await loadApp(stubFetch([], { generatingRun: true }));

  const rows = Array.from(app.getNode("recent-run-list").children);
  const generating = rows.find((row) => row.dataset.runId === "run-b");
  assert.ok(generating, "测试数据里应有一个生成中的任务（run-b）");
  assert.deepEqual(
    findNodes(generating, (node) => node.tagName === "BUTTON").map(
      (button) => button.textContent,
    ),
    ["查看"],
    "生成中的任务只能查看，不能删",
  );
});

test("未使用素材在审批页可以直接排除（覆盖门要人做决定，界面得给入口）", async () => {
  const calls = [];
  const app = await loadApp(
    stubFetch(calls, { currentStatus: "waiting_approval", uncoveredAssets: true }),
  );

  const list = app.getNode("uncovered-asset-list");
  const buttons = findNodes(list, (node) => node.tagName === "BUTTON");
  assert.deepEqual(
    buttons.map((button) => button.textContent),
    ["排除", "排除"],
    "两个未覆盖素材各给一个排除入口",
  );
  assert.ok(allText(list).includes("image-4"));

  await buttons[0].dispatch("click");

  assert.ok(
    calls.some((url) => String(url).includes("/excluded-assets")),
    "点排除要真的调接口，实际：" + JSON.stringify(calls.slice(-3)),
  );
});

test("空闲时降到 15 秒心跳，不再每 5 秒刷", async () => {
  const calls = [];
  // 没有进行中的运行（也就不会被自动恢复并起运行详情轮询）。
  const app = await loadApp(stubFetch(calls, { activeRun: false }));

  const before = calls.length;
  await app.tick();
  await app.tick();
  // 空闲 tick 不该立刻发请求（降频），但必须留一个心跳：别的窗口/会话开始的
  // 任务本页看不到「自行推进」，没有心跳就永远刷不出来（2026-09-17 实测）。
  assert.equal(calls.length, before, "空闲 tick 不该立刻发请求");
  assert.equal(app.intervalCount(), 1, "要留一个慢速心跳");
});