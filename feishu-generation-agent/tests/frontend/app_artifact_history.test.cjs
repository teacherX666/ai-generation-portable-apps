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
    // app.js 会在 document 上装「拖放防误伤」的全局监听（2026-09-29）—— 假 DOM 也得有
    // 这个方法，否则 app.js 一加载就 TypeError，整个文件 50+ 条用例一起红。
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

test("记录里「生成中」的那版要当代表：下拉跳得到，且不给删除", async () => {
  // 用户 2026-09-22：「不能跳到正在生成的任务里」「只要有正在进行的任务，
  // 删除键就不应该在」。两个接口都**不下发 active 字段**（recent-runs 的 SQL
  // 本身就只取 active=0），所以「生成中」在 recent-runs 里是 active=null；
  // 只看 active 的话，latestRunsByRecord 的「有成片的优先」会把上一版已完成
  // 的行挑成代表 —— 那一行是已完成状态，删除键照样出现。
  const calls = [];
  const app = await loadApp(async (url) => {
    calls.push(url);
    if (url === "/api/health") return jsonResponse(200, { modes: { bitable: true } });
    if (url === "/api/bitable/active-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        {
          run_id: "run-done", record_id: "rec-x", display_text: "巨型南瓜",
          status: "已完成", artifact_count: 1, updated_at: "2026-09-22T01:00:00+00:00",
        },
        {
          run_id: "run-busy", record_id: "rec-x", display_text: "巨型南瓜",
          status: "生成中", artifact_count: 0, updated_at: "2026-09-22T02:00:00+00:00",
        },
      ]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/runs/")) {
      return jsonResponse(200, {
        run_id: url.split("/").at(-1),
        status: "running",
        events: [],
        privacy: {},
        approval: { tasks: [] },
        artifacts: [],
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });

  const rows = app.getNode("recent-run-list").children.filter(
    (node) => node.dataset && node.dataset.runId,
  );
  assert.equal(rows.length, 1, "一条记录一行");
  assert.equal(rows[0].dataset.runId, "run-busy", "代表必须是正在跑的那版");
  assert.match(
    findNodes(rows[0], (node) => node.tagName === "P")[0].textContent,
    /^进行中/,
  );
  assert.deepEqual(
    findNodes(rows[0], (node) => node.tagName === "BUTTON").map((b) => b.textContent),
    ["查看"],
    "记录上有进行中的任务时不能给删除",
  );
  assert.ok(
    findNodes(app.getNode("current-run-switcher"), (node) => node.tagName === "OPTION")
      .some((option) => option.value === "run-busy"),
    "下拉里要能选到正在生成的那条",
  );
});

test("历史版本拉取失败时不能谎报「这一版没有成片」", async () => {
  // 服务重启期间的一次失败，以前会被当成「空成片」永久缓存 —— 卡片永远写
  // 「这一版没有成片。」且不再重试（用户 2026-09-22「有些卡片没有预览」）。
  const calls = [];
  const app = await loadApp(async (url) => {
    calls.push(url);
    if (url === "/api/health") return jsonResponse(200, { modes: { bitable: true } });
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [
        { run_id: "run-cur", record_id: "rec-z", display_text: "蛋糕", status: "待确认成片", artifact_count: 1 },
      ]);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        {
          run_id: "run-old", record_id: "rec-z", display_text: "蛋糕",
          status: "已完成", artifact_count: 1, updated_at: "2026-09-22T01:00:00+00:00",
        },
      ]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url === "/api/runs/run-old") throw new Error("boom");
    if (url.startsWith("/api/runs/")) {
      return jsonResponse(200, {
        run_id: url.split("/").at(-1),
        status: "waiting_review",
        events: [],
        privacy: {},
        approval: { tasks: [] },
        artifacts: [VIDEO_ARTIFACT("task-1")],
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });

  const strip = app.getNode("artifact-list");
  const text = allText(strip);
  assert.ok(text.includes("预览暂时没取到"), "失败要说清是没取到，实际：" + text);
  assert.equal(
    text.includes("这一版没有成片"),
    false,
    "拉失败不能谎报没有成片",
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

test("「重跑选中任务」置灰时必须说明缺什么", async () => {
  // 用户 2026-09-21：「重跑选中任务现在又点不动了」——按钮要求「勾选成片」，
  // 以前缺什么都不说，用户只看到一个灰按钮。
  //
  // 2026-09-24 起**返工要求不再是前置条件**：留空 = 提示词原样保留、进审批页复核
  //（用户口径：「如果返工要求里没内容则是直接不改提示词原封不动让我重新审核」）。
  const calls = [];
  const app = await loadApp(stubFetch(calls));
  const button = app.getNode("adjust-artifacts-button");
  const hint = app.getNode("adjust-artifacts-hint");

  assert.equal(button.hidden, false, "成片可审核时按钮要出现");
  assert.equal(button.disabled, true);
  assert.equal(hint.hidden, false);
  assert.equal(hint.textContent, "请先在上方成片卡片里勾选「选择此条重跑」");

  const checkbox = findNodes(
    app.getNode("artifact-list"),
    (node) => node.tagName === "INPUT",
  )[0];
  assert.ok(checkbox, "成片卡片上应当有「选择此条重跑」复选框");
  checkbox.checked = true;
  await checkbox.dispatch("change");

  assert.equal(button.disabled, false, "勾选就够了 —— 返工要求可以留空");
  assert.equal(hint.hidden, true);
  assert.equal(hint.textContent, "");

  // 填了返工要求也不会变回「不可点」，只是这次会先融合改写提示词。
  const feedback = app.getNode("artifact-review-feedback");
  feedback.value = "南瓜滚落的位置不对，请重写这一段动作";
  await feedback.dispatch("input");

  assert.equal(button.disabled, false);
  assert.equal(hint.hidden, true);
});

/** 历史滑条里某一条 run 的格子。 */
function historyCard(app, runId) {
  return findNodes(
    app.getNode("artifact-list"),
    (node) => String(node.className || "").includes("artifact-history-card")
      && node.dataset
      && node.dataset.runId === runId,
  )[0];
}

/** 一条记录：当前版 + 一个成功的往次版 + 一个失败的往次版。 */
function failedAttemptFetch(extra = {}) {
  return async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [
        {
          run_id: "run-cur", record_id: "rec-h", display_text: "切割汽车",
          status: "待确认成片", artifact_count: 1,
        },
      ]);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        {
          run_id: "run-cur", record_id: "rec-h", display_text: "切割汽车",
          status: "待确认成片", updated_at: "2026-09-24T04:00:00+00:00", artifact_count: 1,
        },
        {
          run_id: "run-good", record_id: "rec-h", display_text: "切割汽车",
          status: "已完成", updated_at: "2026-09-24T03:00:00+00:00", artifact_count: 1,
        },
        {
          run_id: "run-bad", record_id: "rec-h", display_text: "切割汽车",
          status: "失败", updated_at: "2026-09-24T02:00:00+00:00", artifact_count: 0,
          last_error: extra.payloadLastError ?? null,
        },
      ]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/bitable/record-runs?")) {
      // 后端按记录返回**完整**历次尝试（窗口内这几条本来就有，合并后不该重复）。
      return jsonResponse(200, {
        record_id: "rec-h",
        runs: [
          {
            run_id: "run-cur", record_id: "rec-h", display_text: "切割汽车",
            status: "待确认成片", updated_at: "2026-09-24T04:00:00+00:00", artifact_count: 1,
          },
          {
            run_id: "run-good", record_id: "rec-h", display_text: "切割汽车",
            status: "已完成", updated_at: "2026-09-24T03:00:00+00:00", artifact_count: 1,
          },
          {
            run_id: "run-bad", record_id: "rec-h", display_text: "切割汽车",
            status: "失败", updated_at: "2026-09-24T02:00:00+00:00", artifact_count: 0,
            last_error: extra.payloadLastError ?? null,
            runtime_status: extra.payloadRuntimeStatus ?? null,
          },
        ],
      });
    }
    if (url === "/api/runs/run-bad" && extra.detailThrows) {
      throw new Error("boom");
    }
    if (url.startsWith("/api/runs/")) {
      const runId = url.split("/").at(-1);
      if (runId === "run-bad") {
        return jsonResponse(200, {
          run_id: "run-bad",
          status: extra.detailStatus || "failed",
          events: extra.events || [],
          privacy: {},
          approval: { tasks: [] },
          artifacts: [],
          last_error: extra.detailLastError || null,
          execution_records: extra.executionRecords || [],
        });
      }
      return jsonResponse(200, {
        run_id: runId,
        status: runId === "run-cur" ? "waiting_review" : "succeeded",
        events: [],
        privacy: {},
        approval: { tasks: [] },
        artifacts: [VIDEO_ARTIFACT("task-1")],
      });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("失败的尝试也进历史滑条：失败徽标 + 灰化 + 失败原因，且不给下载", async () => {
  // 用户 2026-09-24：「『成品与结果』区少了某些记录」—— 失败的尝试以前在
  // recent-runs 的 SQL 里就被 hidden=1 挡掉，滑条上一条都没有，用户看不到
  // 「我失败过几次、为什么失败」。
  const app = await loadApp(failedAttemptFetch({
    executionRecords: [
      {
        provider: "seedance2.5",
        status: "failed",
        error: {
          category: "validation_error",
          message: "生成参数不符合供应商要求",
          code: "generation_asset_mapping_mismatch",
        },
      },
    ],
  }));

  const bad = historyCard(app, "run-bad");
  assert.ok(bad, "失败的尝试必须有格子（以前整条被吞掉）");
  assert.ok(
    String(bad.className).includes("is-failed"),
    "失败格子要灰化，别和成功版混淆：" + String(bad.className),
  );
  assert.equal(bad.dataset.failed, "1");
  assert.ok(
    findNodes(bad, (node) => node.textContent === "失败").length >= 1,
    "要有「失败」徽标",
  );
  // 原因优先级：last_error.message → 失败事件摘要 → 执行记录里的错误。
  // 生产上大多数失败只有最后这一条（last_error 是 null、事件里也没有 failed）。
  assert.ok(
    allText(bad).includes("失败原因：seedance2.5：生成参数不符合供应商要求"),
    "要点开就能看到失败原因，实际：" + allText(bad),
  );
  assert.equal(
    bad.title,
    "失败原因：seedance2.5：生成参数不符合供应商要求",
    "悬停也要能看到",
  );
  // 失败版**不提供**下载 / 送结果表入口（它没有可交付的成片）。
  assert.equal(/下载/.test(allText(bad)), false, "失败格子不能有下载入口");
  assert.equal(
    findNodes(
      bad,
      (node) => String(node.className || "").includes("artifact-download-button"),
    ).length,
    0,
  );
  assert.equal(
    findNodes(bad, (node) => node.tagName === "A").length,
    0,
    "失败格子不能有结果表链接",
  );

  // 成功版本一点没变：还能预览。
  const good = historyCard(app, "run-good");
  assert.ok(good, "成功版仍在滑条里");
  assert.equal(String(good.className).includes("is-failed"), false);
  assert.ok(findNodes(good, (node) => node.tagName === "VIDEO").length >= 1);
});

test("失败原因拿不到就如实说「未记录失败原因」，不编", async () => {
  const app = await loadApp(failedAttemptFetch());

  const bad = historyCard(app, "run-bad");
  assert.ok(bad, "失败的尝试必须有格子");
  assert.ok(
    allText(bad).includes("失败原因：未记录失败原因"),
    "没有原因就如实说没有，实际：" + allText(bad),
  );
});

test("那次运行的详情拉不到时，用 recent-runs 下发的 last_error 当原因", async () => {
  // 落库的 last_error 是服务端记的（例如「本地运行已删除」），详情接口挂了也不该
  // 把已知的原因丢掉。
  const app = await loadApp(failedAttemptFetch({
    detailThrows: true,
    payloadLastError: "本地运行已删除",
  }));

  const bad = historyCard(app, "run-bad");
  assert.ok(bad, "失败的尝试必须有格子");
  assert.ok(
    allText(bad).includes("失败原因：本地运行已删除"),
    "要用落库的原因，实际：" + allText(bad),
  );
});

test("last_error.message 优先于失败事件与执行记录", async () => {
  const app = await loadApp(failedAttemptFetch({
    detailLastError: { category: "provider_terminal_error", message: "生成服务返回错误" },
    events: [{ node: "generate", status: "failed", summary: "节点失败摘要" }],
    executionRecords: [
      { provider: "seedance2.5", status: "failed", error: { message: "执行记录里的原因" } },
    ],
  }));

  const bad = historyCard(app, "run-bad");
  assert.ok(
    allText(bad).includes("失败原因：生成服务返回错误"),
    "优先 last_error.message，实际：" + allText(bad),
  );
});

test("运行时状态是 cancelled 的尝试：写「这次被取消了」，不编具体错误", async () => {
  // 用户 2026-09-24：「有 2 条运行时状态是 cancelled、没有任何错误记录」，界面
  // 只显示「未记录失败原因」，看不出这次是被取消的。运行时状态带出来后要能说清，
  // 但**绝不**编一个具体错误出来。
  const app = await loadApp(failedAttemptFetch({
    detailStatus: "cancelled",
    payloadRuntimeStatus: "cancelled",
  }));

  const bad = historyCard(app, "run-bad");
  assert.ok(bad, "失败的尝试必须有格子");
  assert.ok(
    allText(bad).includes("失败原因：这次被取消了"),
    "要说清是被取消的，实际：" + allText(bad),
  );
  assert.equal(bad.title, "失败原因：这次被取消了", "悬停也要一致");
});

test("详情拉不到时，payload 里的 runtime_status=cancelled 也能说明是被取消", async () => {
  // 运行详情接口挂了（服务重启中）时，手上还有 recent-runs 下发的运行时状态，
  // 不该退化成「暂时没取到」。
  const app = await loadApp(failedAttemptFetch({
    detailThrows: true,
    payloadRuntimeStatus: "cancelled",
  }));

  const bad = historyCard(app, "run-bad");
  assert.ok(bad, "失败的尝试必须有格子");
  assert.ok(
    allText(bad).includes("失败原因：这次被取消了"),
    "实际：" + allText(bad),
  );
});

test("运行时状态是 failed 且没有原因：照旧说「未记录失败原因」，不冒充取消", async () => {
  const app = await loadApp(failedAttemptFetch({
    detailStatus: "failed",
    payloadRuntimeStatus: "failed",
  }));

  const bad = historyCard(app, "run-bad");
  assert.ok(bad, "失败的尝试必须有格子");
  assert.ok(
    allText(bad).includes("失败原因：未记录失败原因"),
    "实际：" + allText(bad),
  );
  assert.equal(allText(bad).includes("这次被取消了"), false);
});

/** 窗口里只有当前这条记录；老记录的历次尝试只在 record-runs 里。 */
function outOfWindowFetch(calls) {
  const runsForRecord = [
    {
      run_id: "run-cur", record_id: "rec-old", display_text: "老需求",
      status: "失败", updated_at: "2026-09-20T02:00:00+00:00", artifact_count: 0,
      runtime_status: "failed",
    },
    {
      run_id: "run-old-good", record_id: "rec-old", display_text: "老需求",
      status: "已完成", updated_at: "2026-09-18T02:00:00+00:00", artifact_count: 1,
    },
    {
      run_id: "run-old-cancelled", record_id: "rec-old", display_text: "老需求",
      status: "失败", updated_at: "2026-09-15T02:00:00+00:00", artifact_count: 0,
      last_error: null, runtime_status: "cancelled",
    },
    {
      run_id: "run-old-failed", record_id: "rec-old", display_text: "老需求",
      status: "失败", updated_at: "2026-09-12T02:00:00+00:00", artifact_count: 0,
      last_error: "生成参数不符合供应商要求", runtime_status: "failed",
    },
  ];
  return async (url) => {
    calls.push(url);
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [runsForRecord[0]]);
    }
    if (url === "/api/bitable/recent-runs") {
      // 窗口切片：这条记录只露了当前这一条（老尝试全被窗口挡在外面）。
      return jsonResponse(200, [runsForRecord[0]]);
    }
    if (url.startsWith("/api/bitable/record-runs?")) {
      return jsonResponse(200, { record_id: "rec-old", runs: runsForRecord });
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/runs/")) {
      const runId = url.split("/").at(-1);
      const known = runsForRecord.find((run) => run.run_id === runId);
      return jsonResponse(200, {
        run_id: runId,
        status: runId === "run-old-cancelled"
          ? "cancelled"
          : (runId === "run-cur" ? "failed" : "succeeded"),
        events: [],
        privacy: {},
        approval: { tasks: [] },
        artifacts: runId === "run-old-good" ? [VIDEO_ARTIFACT("task-1")] : [],
        last_error: null,
        execution_records: [],
        record_id: known ? known.record_id : null,
      });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("打开某条记录时按 record_id 取完整历次尝试：窗口外的老失败也看得到", async () => {
  // 用户 2026-09-24：「窗口外老记录的失败尝试看不到」——实测 89 条失败尝试里
  // 45 条属于「最近 10 条任务」窗口外的老记录。全局列表保持窗口不变，前端打开
  // 某条记录时另拉一份**完整**历史喂给「往次成片」滑条。
  const calls = [];
  const app = await loadApp(outOfWindowFetch(calls));

  assert.ok(
    calls.some((url) => url.startsWith("/api/bitable/record-runs?run_id=run-cur")),
    "要按当前运行去取它所属记录的完整历史，实际：" + calls.join(" | "),
  );
  // 窗口外的那两次失败尝试现在都在滑条上。
  const cancelled = historyCard(app, "run-old-cancelled");
  const failed = historyCard(app, "run-old-failed");
  assert.ok(cancelled, "窗口外被取消的尝试要出现在滑条上");
  assert.ok(failed, "窗口外失败的尝试要出现在滑条上");
  assert.ok(
    allText(cancelled).includes("失败原因：这次被取消了"),
    "实际：" + allText(cancelled),
  );
  assert.ok(
    allText(failed).includes("失败原因：生成参数不符合供应商要求"),
    "实际：" + allText(failed),
  );
  // 窗口内那条成功的往次版照样在，没被完整历史挤掉。
  assert.ok(historyCard(app, "run-old-good"), "成功的往次版仍要在");
  // 全局「最近任务」列表**不变**：这条记录仍只占一行（payload 没被撑大）。
  assert.equal(app.getNode("recent-run-list").children.length, 1);
  assert.ok(
    app.getNode("run-history-summary").textContent.includes("共 1 条"),
    "实际：" + app.getNode("run-history-summary").textContent,
  );
});

test("record-runs 拉不到时不影响滑条：退回窗口里的数据，且不弹全局错误", async () => {
  const inner = failedAttemptFetch({ payloadLastError: "本地运行已删除" });
  const app = await loadApp(async (url) => {
    if (url.startsWith("/api/bitable/record-runs?")) throw new Error("boom");
    return inner(url);
  });

  // 窗口里那条失败尝试仍能正常渲染（原因用 payload 里的）。
  const bad = historyCard(app, "run-bad");
  assert.ok(bad, "完整历史拉不到不该把窗口里的版本也弄丢");
  assert.ok(
    allText(bad).includes("失败原因：本地运行已删除"),
    "实际：" + allText(bad),
  );
  assert.equal(app.getNode("recent-run-list").children.length, 1);
});
