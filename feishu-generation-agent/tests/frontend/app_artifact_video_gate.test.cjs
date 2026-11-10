"use strict";

/**
 * 成片预览的**取流闸门**（2026-11-04）。
 *
 * 用户先报：「Portal 开始任务 / 重新运行的时候经常弹『此页面没有响应』」——
 * 「此页面没有响应」是 Chromium 在**渲染主线程被卡住十几秒**时才弹的框，而这里以前是
 * **一版一个 `<video preload="metadata">` 真 src、条数没有上限**：生产库里同一条记录最多
 * 39 个版本（≥20 版的有 8 条）→ 一屏几十路并发取流，而且点重跑时面板整块重建、全部重新取流。
 * 同族事故这个仓踩过两次并各自收口：seedance 2026-09-02（`JOB_TILE_MAX_THUMBS = 4`）、
 * 自由创作 2026-09-15（`HISTORY_RENDER_LIMIT = 12`）。
 *
 * 随后用户给了最终口径（2026-11-04）：
 *   「前面的视频以及最新的视频不要做这个点击加载，只有后面看不到的视频才做点击加载，
 *     并且要放预览图，让用户能准确选择」
 *
 * 所以本文件钉的是这四条：
 *   ① **前面的**（当前运行的成片）和**往次里最新的一版** → 自动加载、直接能播；
 *   ② **更老的往次版本** → 先只放**预览图**（静音、无控件、src 带 `#t=0.5` 的首帧），
 *      点「▶ 点击播放」才升级成播放器；
 *   ③ 往次版本一次最多铺 6 个，其余折进「展开全部 N 版」；
 *   ④ 同一个成片的 `<video>` 元素**复用** —— 重绘只搬位置，不重建、不重新取流。
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

function byClass(root, className) {
  return findNodes(root, (node) => String(node.className || "").includes(className));
}

function mediaOf(app) {
  return findNodes(app.getNode("artifact-list"), (node) => node.tagName === "VIDEO");
}

function playButtonsOf(app) {
  return byClass(app.getNode("artifact-list"), "artifact-video-play");
}

function historyCardsOf(app) {
  return byClass(app.getNode("artifact-list"), "artifact-history-card");
}

function cardOfRun(app, runId) {
  return historyCardsOf(app).find((card) => card.dataset.runId === runId) || null;
}

function expandButtonOf(app) {
  return byClass(app.getNode("artifact-list"), "artifact-history-more")[0] || null;
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
  };
}

const ARTIFACT = (taskId) => ({
  artifact_id: `art-${taskId}`,
  task_id: taskId,
  kind: "video",
  status: "ready",
  mime_type: "video/mp4",
  size: 1024 * 1024,
  preview_url: `/api/runs/x/artifacts/${taskId}.mp4`,
});

//: 生产实测：同一条记录最多 39 个版本，≥20 版的有 8 条，≥10 版的有 46 条。
const SIBLING_COUNT = 20;
//: 只有最新的两版给成片：最新那版要能直接播，第二新那版用来验「预览图 + 点击才播」。
const SIBLINGS_WITH_ARTIFACT = new Set(["run-old-1", "run-old-2"]);

function siblingRun(index) {
  return {
    run_id: `run-old-${index}`,
    record_id: "rec-a",
    display_text: "拿着吧你",
    status: "已完成",
    updated_at: `2026-09-17 09:${String(index).padStart(2, "0")}:00`,
    artifact_count: SIBLINGS_WITH_ARTIFACT.has(`run-old-${index}`) ? 1 : 0,
  };
}

function stubFetch(calls) {
  return async (url) => {
    calls.push(url);
    if (url === "/api/health") return jsonResponse(200, { modes: { bitable: true } });
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [
        { run_id: "run-cur", record_id: "rec-a", display_text: "拿着吧你", status: "待确认成片", artifact_count: 1 },
      ]);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        { run_id: "run-cur", record_id: "rec-a", display_text: "拿着吧你", status: "已完成", updated_at: "2026-09-17 10:20:00", artifact_count: 1 },
      ]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/bitable/record-runs?")) {
      // 形状是 {runs, record_id}（不是裸数组）—— 见 ensureRecordRuns。
      return jsonResponse(200, {
        record_id: "rec-a",
        runs: Array.from({ length: SIBLING_COUNT }, (_, i) => siblingRun(i + 1)),
      });
    }
    if (url.startsWith("/api/runs/run-cur")) {
      return jsonResponse(200, {
        run_id: "run-cur",
        thread_id: "t-cur",
        record_id: "rec-a",
        status: "waiting_review",
        events: [],
        privacy: {},
        approval: { tasks: [], media_assets: [], excluded_assets: [] },
        artifacts: [ARTIFACT("task-cur")],
      });
    }
    if (url.startsWith("/api/runs/run-old-")) {
      const runId = url.split("/").at(-1);
      return jsonResponse(200, {
        run_id: runId,
        thread_id: "t-" + runId,
        record_id: "rec-a",
        status: "succeeded",
        events: [],
        approval: { tasks: [] },
        artifacts: SIBLINGS_WITH_ARTIFACT.has(runId)
          ? [ARTIFACT(`${runId}-task`)]
          : [],
        updated_at: "2026-09-17 09:00:00",
      });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("往次版本一次只铺 6 个，其余折进「展开全部 N 版」", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const cards = historyCardsOf(app);
  assert.equal(
    cards.length,
    6,
    `同一条记录有 ${SIBLING_COUNT} 个版本，一屏最多只铺 6 张卡（实际 ${cards.length}）`,
  );
  const more = expandButtonOf(app);
  assert.ok(more, "被折起来的版本必须有个「展开全部」入口，不能让人找不到");
  assert.match(more.textContent, new RegExp(`展开全部 ${SIBLING_COUNT} 版`));
});

test("点「展开全部」后所有版本都出来，按钮变成收起", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  await expandButtonOf(app).dispatch("click");
  await settle();

  assert.equal(
    historyCardsOf(app).length,
    SIBLING_COUNT,
    "展开后每个版本都要能看到",
  );
  assert.match(expandButtonOf(app).textContent, /只看最近 6 版/, "展开后要能收回去");
});

test("前面的视频（当前成片）不做点击加载 —— 直接能播", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const players = findNodes(app.getNode("artifact-list"), (node) =>
    String(node.className || "").includes("artifact-card")
    && !String(node.className || "").includes("artifact-history-card"),
  );
  assert.equal(players.length, 1, "当前成片一张卡");
  const video = findNodes(players[0], (node) => node.tagName === "VIDEO")[0];
  assert.ok(video, "当前成片要有视频元素");
  assert.equal(video.controls, true, "当前成片直接就是播放器");
  assert.match(String(video.src), /task-cur\.mp4$/, "直接挂了 src，不用点");
  assert.equal(
    String(video.src).includes("#t="),
    false,
    "播放器形态不该带首帧锚点",
  );
  assert.equal(
    playButtonsOf(app).some((btn) => btn.hidden === false && btn.parentNode),
    false,
    "当前成片不该出现「点击播放」遮罩",
  );
});

test("往次里**最新的一版**直接能播，**更老的**只给预览图 + 点击播放", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const newest = cardOfRun(app, "run-old-1");
  const older = cardOfRun(app, "run-old-2");
  assert.ok(newest && older, "两个有成片的往次版本都要在");

  const newestVideo = findNodes(newest, (n) => n.tagName === "VIDEO")[0];
  assert.equal(newestVideo.controls, true, "最新的那一版照旧直接能播");
  assert.match(String(newestVideo.src), /run-old-1-task\.mp4$/);

  const olderVideo = findNodes(older, (n) => n.tagName === "VIDEO")[0];
  assert.equal(olderVideo.controls, false, "更老的版本先不是播放器");
  assert.equal(olderVideo.muted, true, "预览图静音");
  assert.match(
    String(olderVideo.src),
    /run-old-2-task\.mp4#t=0\.5$/,
    "预览图靠 #t=0.5 让浏览器画出首帧，用户才能认出是哪一版",
  );
  const play = findNodes(
    older,
    (n) => String(n.className || "").includes("artifact-video-play"),
  )[0];
  assert.ok(play, "更老的版本要有「点击播放」入口");
  assert.equal(play.textContent, "▶ 点击播放");
  assert.equal(play.hidden, false);
});

test("点「▶ 点击播放」之后，预览图升级成播放器", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const older = cardOfRun(app, "run-old-2");
  const video = findNodes(older, (n) => n.tagName === "VIDEO")[0];
  const play = findNodes(
    older,
    (n) => String(n.className || "").includes("artifact-video-play"),
  )[0];

  await play.dispatch("click");
  await settle();

  assert.equal(video.controls, true, "点完要变成能拖进度条的播放器");
  assert.equal(video.muted, false, "点完要能出声");
  assert.equal(video.preload, "auto");
  assert.equal(play.hidden, true, "遮罩要让位，别挡着画面");
});

test("展开/收起重绘时复用同一个 <video>，不重建也不重新取流", async () => {
  const calls = [];
  const app = await loadApp(stubFetch(calls));

  const before = mediaOf(app)[0];
  const urlBefore = String(before.src);
  assert.ok(urlBefore, "前置条件：当前成片的视频已挂 src");

  await expandButtonOf(app).dispatch("click");
  await settle();

  const after = mediaOf(app)[0];
  assert.equal(
    after,
    before,
    "同一个成片的 <video> 必须是同一个对象 —— 重建就等于把正在播的那一路掐掉重来",
  );
  assert.equal(String(after.src), urlBefore, "复用之后 src 不能丢");
});
