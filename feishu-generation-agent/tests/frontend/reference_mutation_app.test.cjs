"use strict";

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
    this.files = [];
    this.className = "";
    this.listeners = new Map();
    // `classList` 必须**真的**起作用：参考图卡片上的「用清洗版 / 用原图」靠它标出
    // 当前生效的那一版（2026-10-26）。以前这里是个空壳 `toggle() {}`，于是"按钮高亮"
    // 在测试里永远断言不出来。
    this.classList = {
      contains: (name) => this._classes().includes(name),
      add: (...names) => names.forEach((name) => this._setClass(name, true)),
      remove: (...names) => names.forEach((name) => this._setClass(name, false)),
      toggle: (name, force) => {
        const want = force === undefined ? !this.classList.contains(name) : Boolean(force);
        this._setClass(name, want);
        return want;
      },
    };
  }

  _classes() {
    return String(this.className).split(/\s+/).filter(Boolean);
  }

  // 真 DOM 有它，而 app.js 的"任务列表已经渲染过没有"判据（`shouldRefreshTaskEditor`
  // 的 `hasRenderedTasks`）就靠它 —— 缺了它判据永远走"没渲染过"的分支，
  // 「点了按钮界面不重建」这个 bug 在测试里就复现不出来（2026-10-26 踩到）。
  get childElementCount() {
    return this.children.length;
  }

  _setClass(name, on) {
    const classes = new Set(this._classes());
    if (on) classes.add(name);
    else classes.delete(name);
    this.className = [...classes].join(" ");
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  dispatch(name) {
    return Promise.all(
      (this.listeners.get(name) || []).map((listener) => listener({
        target: this,
        stopPropagation() {},
      })),
    );
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  contains(node) {
    if (node === this) return true;
    return descendants(this).includes(node);
  }
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

function byClass(node, className) {
  return descendants(node).filter((child) => (
    String(child.className).split(/\s+/).includes(className)
  ));
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() { return payload; },
  };
}

function runView() {
  return {
    run_id: "run-1",
    thread_id: "thread-1",
    source_url: "https://acme.feishu.cn/docx/reference-actions",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "参考素材操作测试",
      revision: 7,
      document_summary: "",
      selected_task_ids: [],
      media_assets: [{
        asset_id: "asset-1",
        media_kind: "image",
        preview_url: "/api/assets/asset-1",
      }],
      excluded_assets: [],
      vision_descriptions: [],
      validation_issues: [],
      ingest_issue_records: [],
      vision_issues: [],
      coverage: {
        successful_total: 1,
        referenced_count: 1,
        excluded_count: 0,
        uncovered_count: 0,
        failed_count: 0,
      },
      tasks: [{
        task_id: "task-1",
        task_type: "image_to_video",
        title: "火锅",
        prompt: "参考图1中的火锅持续沸腾",
        negative_constraints: [],
        reference_mode: "multi_reference",
        reference_images: [{
          asset_id: "asset-1",
          role: "reference_image",
          order: 1,
        }],
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
    File,
    FormData,
    ReferenceMutationState,
    ReferenceUploadState,
    ReviewState,
    document: {
      createElement: (tagName) => new FakeNode(tagName),
      getElementById: getNode,
      querySelector: () => new FakeNode("main"),
      // 「任务编辑器里还有焦点就不重建」的守卫要读它（app.js `taskEditorFocused`）。
      activeElement: null,
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
  return { getNode, document: context.document };
}

function fetchForRun(requests, {
  deleteGate = null,
  watermarkCleanup = null,
  references = null,
  mediaAssets = null,
} = {}) {
  const view = runView();
  // 参考图去水印的进度台账随运行视图下发；测试可以注入它来看卡片上的反馈/切换。
  if (watermarkCleanup) view.approval.watermark_cleanup = watermarkCleanup;
  // 素材组合（图片 + 视频）要能按用例改：替换视频的坑只在「图片占着 1、视频在 2」时出现。
  if (references) view.approval.tasks[0].reference_images = references;
  if (mediaAssets) view.approval.media_assets = mediaAssets;
  return async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{ run_id: "run-1", display_text: "火锅" }]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url === "/api/runs/run-1" && (!options.method || options.method === "GET")) {
      return jsonResponse(200, view);
    }
    if (url === "/api/runs/run-1/references" && options.method === "POST") {
      return jsonResponse(201, { asset_id: "asset-2" });
    }
    // 「用清洗版 / 用原图」：真服务端会把选择写进台账、随后 GET 就能读到 —— 桩也要这样，
    // 否则测不出"点完界面能不能跟着变"。
    if (
      url === "/api/runs/run-1/references/asset-1/watermark-decision"
      && options.method === "POST"
    ) {
      const body = JSON.parse(options.body);
      const entry = (view.approval.watermark_cleanup ||= {})["asset-1"] || {};
      (view.approval.watermark_cleanup)["asset-1"] = entry;
      entry.decision = body.decision;
      if (body.selected) entry.selected = body.selected;
      else delete entry.selected;
      return jsonResponse(200, { ok: true, cleanup: entry });
    }
    // 「重新清洗」：真实现场每张要 70~90 秒（整张重绘），桩里立刻返回**新版本**，
    // 用来守住"洗完缩略图要立刻换成新版"这条。
    if (
      url === "/api/runs/run-1/references/asset-1/reclean"
      && options.method === "POST"
    ) {
      const cleanup = (view.approval.watermark_cleanup ||= {});
      const entry = (cleanup["asset-1"] ||= {});
      const versions = Array.isArray(entry.versions) ? entry.versions : (entry.versions = []);
      const version = versions.length + 1;
      const path = `data/runs/run-1/inputs/asset-1-nowm-${version}.png`;
      versions.push({ version, path, adopted: false });
      entry.status = "adopted";
      entry.decision = "cleaned";
      entry.selected = path;
      entry.cleaned = path;
      return jsonResponse(200, { ok: true, cleanup: entry });
    }
    if (
      url === "/api/runs/run-1/tasks/task-1/references/asset-1"
      && options.method === "DELETE"
    ) {
      if (deleteGate) await deleteGate.promise;
      return jsonResponse(200, { ok: true });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("replacement uses a native label and reports success beside the references", async () => {
  const requests = [];
  const app = await loadApp(fetchForRun(requests));
  const taskList = app.getNode("task-list");
  const replaceInput = byClass(taskList, "reference-file-input")[0];
  const replaceLabel = byClass(taskList, "reference-replace-label")[0];

  assert.equal(replaceLabel.tagName, "LABEL");
  assert.equal(replaceLabel.htmlFor, replaceInput.id);
  assert.notEqual(replaceInput.hidden, true);

  replaceInput.files = [new File(["replacement"], "replacement.png", {
    type: "image/png",
  })];
  await replaceInput.dispatch("change");
  await settle();

  assert.equal(
    requests.some((request) => (
      request.options.method === "POST"
      && request.url === "/api/runs/run-1/references"
    )),
    true,
  );
  assert.match(
    byClass(taskList, "reference-section-feedback")[0].textContent,
    /参考素材已替换/,
  );
});

// 2026-10-30（用户）：「为什么我飞书模块替换视频会失败」。
// 生产实测 run `31770465`：图片1(order=1) + 视频1(order=2)，替换视频返回
// 422「图片引用无效」。根因在提交的 order —— 输入框里显示的是**同类型序号**
// （视频1 → 1），而它在任务里的**全局槽位**是 2；提交 1 就与图片1 撞车，
// 后端 `canonicalize_references` 判 order 重复。替换图片没事，正因为图片的
// 同类型序号恰好等于全局槽位。
test("替换视频提交的是全局槽位号，不是同类型序号", async () => {
  const requests = [];
  const app = await loadApp(fetchForRun(requests, {
    references: [
      { asset_id: "asset-1", role: "reference_image", order: 1 },
      { asset_id: "asset-video", role: "reference_video", order: 2 },
    ],
    mediaAssets: [
      { asset_id: "asset-1", media_kind: "image", preview_url: "/api/assets/asset-1" },
      { asset_id: "asset-video", media_kind: "video", preview_url: "/api/assets/asset-video" },
    ],
  }));
  const taskList = app.getNode("task-list");
  const replaceInputs = byClass(taskList, "reference-file-input");
  assert.equal(replaceInputs.length, 2, "两行素材各有一个「替换」选择器");

  // 行按 order 排 → 第 2 行是视频（用 id 里的 asset_id 钉住，别靠位置猜）
  const videoInput = replaceInputs.find((input) => String(input.id).includes("asset-video"));
  assert.ok(videoInput, "视频那一行的「替换」选择器要在");
  videoInput.files = [new File(["replacement"], "new-clip.mp4", { type: "video/mp4" })];
  await videoInput.dispatch("change");
  await settle();

  const posted = requests.find((request) => (
    request.options.method === "POST"
    && request.url === "/api/runs/run-1/references"
  ));
  assert.ok(posted, "替换视频必须真的发请求");
  assert.equal(
    posted.options.body.get("order"),
    "2",
    "要提交全局槽位 2；提交同类型序号 1 会与图片1 撞车 → 422「图片引用无效」",
  );
  assert.equal(posted.options.body.get("replaces_asset_id"), "asset-video");
  assert.equal(posted.options.body.get("role"), "reference_video");
});

test("delete shows an immediate busy state and sends the request once", async () => {  let releaseDelete;
  const deleteGate = {
    promise: new Promise((resolve) => { releaseDelete = resolve; }),
  };
  const requests = [];
  const app = await loadApp(fetchForRun(requests, { deleteGate }));
  const taskList = app.getNode("task-list");
  const deleteButton = byClass(taskList, "reference-delete-button")[0];

  const pending = deleteButton.dispatch("click");
  await settle();

  assert.equal(deleteButton.disabled, true);
  assert.equal(deleteButton.textContent, "删除中…");
  assert.equal(
    requests.filter((request) => request.options.method === "DELETE").length,
    1,
  );

  releaseDelete();
  await pending;
});

// 这一条是**真正执行 render** 的（不是源文本断言）：2026-10-20 我在 referenceRow 里把
// `cleanupStatus` 的 const 声明留在原位、却在上面的缩略图逻辑里引用了它 —— TDZ 直接抛
// ReferenceError → 整个审批界面渲染不出来（用户原话：「审核界面直接没了」）。
// 源文本断言抓不到这类错误，只有把 render 跑一遍才抓得到。
test("去水印被采用的参考图默认给清洗版，且能切回原图", async () => {
  const requests = [];
  const app = await loadApp(fetchForRun(requests, {
    watermarkCleanup: {
      "asset-1": {
        status: "adopted",
        message: "已去水印并采用（与实物差异 3.4 ≤ 12；原图保留，可在审批页对比）",
        fidelity: 3.4,
        // 真实台账一定有 `cleaned`（采用 ⟹ 清洗版文件存在）。这里对齐真相：前端按
        // "有没有清洗版"决定给不给切换，与后端 `effective_cleanup_path` 同一口径。
        cleaned: "data/runs/run-1/inputs/asset-1-nowm.png",
      },
    },
  }));
  const taskList = app.getNode("task-list");
  const toggle = byClass(taskList, "reference-version-button")[0];
  const image = descendants(taskList).find((node) => node.tagName === "IMG");

  assert.ok(toggle, "被采用的参考图必须有「看原图」切换（原图永远要能看到）");
  assert.equal(toggle.textContent, "看原图");
  assert.equal(toggle.dataset.version, "cleaned");
  assert.ok(image, "参考图缩略图要在（渲染没被异常打断）");
  assert.match(String(image.src), /\/api\/assets\/asset-1$/, "默认显示清洗版");

  await toggle.dispatch("click");
  assert.equal(toggle.textContent, "看清洗版", "切过去之后按钮要能切回来");
  assert.equal(toggle.dataset.version, "original");
  assert.match(String(image.src), /original=1/, "原图走同一个接口 + ?original=1");

  await toggle.dispatch("click");
  assert.equal(toggle.dataset.version, "cleaned");
  assert.match(String(image.src), /\/api\/assets\/asset-1\?/, "切回来后仍取清洗版");
});

// 2026-10-23（用户）：「9090 端口飞书模块去水印怎么失效了，手机卖点两次任务都有水印」。
// 护栏把好结果误杀（108 对生产图里 30% 被拒）之外，**被拒时界面连清洗版都不给看**是另一半：
// 用户只看到一句"已保留原图"，既没法判断护栏判得对不对，也没法推翻它。
// 这条守着：护栏判 kept 时清洗版照样能看（切换按钮必须在），且**默认仍是原图**。
test("护栏拒绝（kept）的参考图也要能看清洗版", async () => {
  const requests = [];
  const app = await loadApp(fetchForRun(requests, {
    watermarkCleanup: {
      "asset-1": {
        status: "kept",
        message: "清洗改写了画面内容（最差局部差异 42 > 35、受影响 2/1024 块），已保留原图",
        fidelity: 3.6,
        cleaned: "data/runs/run-1/inputs/asset-1-nowm.png",
      },
    },
  }));
  const taskList = app.getNode("task-list");
  const toggle = byClass(taskList, "reference-version-button")[0];
  const image = descendants(taskList).find((node) => node.tagName === "IMG");

  assert.ok(toggle, "护栏判 kept 时也必须给「看清洗版」，否则用户没法判断/推翻护栏");
  // 用户没选过 → 护栏判定仍然生效，默认给**原图**（不能悄悄换成清洗版）。
  assert.equal(toggle.textContent, "看清洗版");
  assert.equal(toggle.dataset.version, "original");
  assert.match(String(image.src), /original=1/, "kept 时默认显示原图");

  await toggle.dispatch("click");
  assert.equal(toggle.dataset.version, "cleaned");
  assert.match(String(image.src), /\/api\/assets\/asset-1\?/, "点一下就能看清洗版");
});

// 2026-10-26（用户）：「9090 端口飞书模块新加的用清洗版点了没反应」。
// 真实现场：POST 成功、台账也写进去了（`decision: "cleaned"`），但界面**纹丝不动** ——
// 按钮不高亮、缩略图仍给原图。根因不在后端，在"重建判据"：`poll()` 只在
// `shouldRefreshTaskEditor` 为真时才重建任务编辑器，而它的身份判据只看
// `{tasks, selected_task_ids}`，去水印选择不在里面。
// 这条守着：点完「用清洗版」，**本行自己**就要把结果显示出来。
function keptCleanupLedger() {
  return {
    "asset-1": {
      status: "kept",
      message: "清洗改写了画面内容（最差局部差异 112 > 35），已保留原图",
      fidelity: 7.9,
      versions: [{
        version: 1,
        path: "data/runs/run-1/inputs/asset-1-nowm.png",
        adopted: false,
      }],
      cleaned: "data/runs/run-1/inputs/asset-1-nowm.png",
      decision: "auto",
    },
  };
}

function isActive(node) {
  return String(node.className).split(/\s+/).includes("is-active");
}

function findButton(taskList, label) {
  return descendants(taskList).find(
    (node) => node.tagName === "BUTTON" && node.textContent === label,
  );
}

function firstImage(taskList) {
  return descendants(taskList).find((node) => node.tagName === "IMG");
}

test("点「用清洗版」立刻生效：按钮高亮 + 缩略图换成清洗版", async () => {
  const requests = [];
  const app = await loadApp(fetchForRun(requests, { watermarkCleanup: keptCleanupLedger() }));
  const taskList = app.getNode("task-list");

  // 护栏判 kept、用户还没选过 → 默认给**原图**、按钮不高亮。
  assert.match(String(firstImage(taskList).src), /original=1/);
  assert.equal(isActive(findButton(taskList, "用清洗版")), false);

  await findButton(taskList, "用清洗版").dispatch("click");
  await settle();

  const posted = requests.find((request) => (
    request.url === "/api/runs/run-1/references/asset-1/watermark-decision"
    && request.options.method === "POST"
  ));
  assert.ok(posted, "点「用清洗版」必须把选择存回服务端");
  assert.equal(JSON.parse(posted.options.body).decision, "cleaned");
  assert.equal(
    isActive(findButton(taskList, "用清洗版")),
    true,
    "选中的那一版必须高亮 —— 否则用户看到的就是「点了没反应」",
  );
  assert.equal(isActive(findButton(taskList, "用原图")), false);
  assert.match(
    String(firstImage(taskList).src),
    /version=1/,
    "缩略图要跟着切到清洗版",
  );
  // 明确回执：点的是**已经选中**的那一版时界面本来就不会变，只有这条提示能证明"点到了"。
  assert.match(
    app.getNode("action-toast-text").textContent,
    /提交时用去水印版/,
    "点完要有一条确认提示（用户原话就是「点了没反应」）",
  );
});

test("任务编辑器里还有焦点时点「用清洗版」也要立刻有反馈", async () => {
  // 重建被挂起的场景（正在编辑提示词时很容易发生）：整页不重建，反馈只能靠本行自己做。
  const requests = [];
  const app = await loadApp(fetchForRun(requests, { watermarkCleanup: keptCleanupLedger() }));
  const taskList = app.getNode("task-list");
  const textarea = descendants(taskList).find((node) => node.tagName === "TEXTAREA");
  assert.ok(textarea, "任务编辑器里要有可聚焦的输入框（否则这条测不到「焦点挂起」那条路）");
  app.document.activeElement = textarea;

  await findButton(taskList, "用清洗版").dispatch("click");
  await settle();

  assert.equal(isActive(findButton(taskList, "用清洗版")), true);
  assert.match(String(firstImage(taskList).src), /version=1/);

  await findButton(taskList, "用原图").dispatch("click");
  await settle();

  assert.equal(isActive(findButton(taskList, "用原图")), true);
  assert.match(String(firstImage(taskList).src), /original=1/, "改回原图同样要立刻可见");
});

test("点「重新清洗」后缩略图要立刻换成刚洗出来的那一版", async () => {
  const requests = [];
  const app = await loadApp(fetchForRun(requests, { watermarkCleanup: keptCleanupLedger() }));
  const taskList = app.getNode("task-list");
  // 刻意让编辑器里还有焦点：整页重建会被挂起，只能靠本行自己换图。
  app.document.activeElement = descendants(taskList).find((node) => node.tagName === "TEXTAREA");

  await findButton(taskList, "重新清洗").dispatch("click");
  await settle();

  assert.equal(
    requests.some((request) => (
      request.url === "/api/runs/run-1/references/asset-1/reclean"
      && request.options.method === "POST"
    )),
    true,
    "「重新清洗」必须真的发请求",
  );
  assert.match(
    String(firstImage(taskList).src),
    /version=2/,
    "洗完要立刻显示新的一版（旧版仍可切换回去）",
  );
  assert.equal(isActive(findButton(taskList, "用清洗版")), true);
  assert.equal(findButton(taskList, "重新清洗").disabled, false, "按钮要恢复可用");
});
