"use strict";

//: 版本自愈：如果服务器上的 app.js 版本串和自己被加载时的版本串不一致，说明浏览器还在
//: 跑旧代码，直接 reload 自己（在 iframe 里就是刷新这个 iframe）。
//:
//: 为什么需要它：自由创作跑在 Portal 的 iframe 里，iframe 一旦加载完成就**不会**因为
//: 外层页面改动而重新导航；用户「Ctrl+F5 了还是没变化」就是这个原因。实测（2026-09-21）
//: 用真实会话直接取服务端文件，`app.js` / `styles.css` 都是最新的，问题 100% 在浏览器。
(function watchBundleVersion() {
  const src = (document.currentScript && document.currentScript.src) || "";
  const ownVersion = (src.match(/[?&]v=([^&]+)/) || [])[1];
  if (!ownVersion) return;
  let checking = false;
  const check = async () => {
    if (checking || document.visibilityState !== "visible") return;
    checking = true;
    try {
      const response = await fetch("./index.html", { cache: "no-store" });
      const html = await response.text();
      const latest = (html.match(/app\.js\?v=([^"'&\s]+)/) || [])[1];
      if (latest && latest !== ownVersion) location.reload();
    } catch (error) {
      void error;   // 拿不到就算了，不打扰用户
    }
    checking = false;
  };
  window.setInterval(check, 20000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") check();
  });
})();

const STORAGE_KEY = "redcraft.free-creation.v1";
const SCHEMA_URLS = {
  video: "/seedance/api/schema",
  image: "/nano-banana/api/schema",
  portrait: "/volcengine-portrait/api/config",
  segment: "/local-ai/api/image_segment/schema",
  upscale: "/local-ai/api/video_upscale/schema",
};
// Portal 反代 /local-ai/* → 模型机上的本地 AI Port 网关（8801）。子应用自身没有这个
// 前缀，所以凡是直连网关的调用都要带上 LOCAL_AI_MOUNT。
const LOCAL_AI_MOUNT = "/local-ai";
const localModuleBase = (module) => `${LOCAL_AI_MOUNT}/api/${module}`;
// 网关里「换的是任务、不是模型」的两项能力：抠图输入 1 张图、输出多张 PNG；超分输入
// 视频、输出视频。它们的参数契约与默认值一律从网关自己的 /schema 读，前端不写死。
//
// 网关还暴露 image_local / video_local 两个模块，这里**故意不列**：它们跟「本地
// ComfyUI（免费）」下的图片模型 / MiniMax H3 视频模型是同一个模块（nano-banana 与
// seedance 已经把它包装成 comfyui_local provider），再列一遍就是同一个入口出现两次。
const LOCAL_CAPABILITIES = {
  image_segment: { mode: "image", label: "智能抠图 (SAM3)" },
  video_upscale: { mode: "video", label: "本地视频超分" },
};
// 只暴露「用户真的需要选」的参数，其余走网关默认值 —— 目标用户不是调参的人。
//
// 「分辨率档位」按平台统一口径 = **短边像素**，与视频生成模块（Seedance 的
// resolution + ratio）完全一致：9:16 + 720p = 720x1280，16:9 + 1080p = 1920x1080。
// 用户 2026-09-21 指出这里原先用的是「等比缩进横屏框」——竖屏 720x1280 选 1080p 会
// 被缩成 608x1080（比源素材还小），跟其它模块两套标准打架。现在只传档位，
// 输出框由网关按真实素材尺寸算（modules/video_upscale.py fit_box_for_short_side）。
const UPSCALE_TARGETS = [
  { label: "720p", value: "720p", shortSide: 720 },
  { label: "1080p", value: "1080p", shortSide: 1080 },
  { label: "2K", value: "2K", shortSide: 1440 },
  { label: "4K", value: "4K", shortSide: 2160 },
];
const SEGMENT_THRESHOLDS = [
  { label: "宽松 0.2", value: 0.2 },
  { label: "标准 0.3", value: 0.3 },
  { label: "严格 0.4", value: 0.4 },
  { label: "很严格 0.5", value: 0.5 },
];
// 网关 read_json_body 上限 200MB，base64 放大 4/3 → 输入视频卡在 120MB。
const UPSCALE_MAX_INPUT_BYTES = 120 * 1024 * 1024;
// 超分的速度模式：标准 = 原尺寸重建；快速 = 先把输入缩一半再重建（网关的
// resize_factor=0.5）。同一条 12.04 秒 720p 素材实测：标准 11.3 分钟 / 快速
// 4.8 分钟（快 2.4 倍）。但 1:1 放大并排对比下，快速版细节明显下降、边缘发糊、
// 皮肤过渡区出现伪影 —— 是实打实的画质换速度，所以交给用户按场景选。
const UPSCALE_SPEED_MODES = [
  { label: "标准（画质优先）", value: "standard" },
  { label: "快速（约 2.4 倍速，细节下降）", value: "fast" },
];
const HISTORY_SOURCES = {
  seedance: { mode: "video", mount: "/seedance" },
  "nano-banana": { mode: "image", mount: "/nano-banana" },
  "volcengine-portrait": { mode: "video", mount: "/volcengine-portrait" },
  dreamina: { mode: null, mount: "/dreamina" },
};
const HISTORY_SYNC_INTERVAL = 15000;

//: 渲染签名缓存：内容没变就不重建 DOM（用户 2026-09-21：「正在生成的时候历史记录下面
//: 一直在跳」—— 生成期间 reconcileRunningTasks 每 5 秒把历史条与会话列表整块重建）。
const renderSignature = { history: '', historyOpen: null, historyIds: null, conversations: '', preview: '' };
const stripOpenNow = () => document.querySelector('#history-strip').classList.contains('is-open');
const TERMINAL_SUCCESS = new Set(["succeeded", "success", "done", "completed", "completed_with_errors"]);
const TERMINAL_FAILURE = new Set(["failed", "error", "cancelled", "canceled", "interrupted"]);
const RUNNING = new Set(["pending", "queued", "submitted", "running", "processing", "generating"]);
let historySyncPromise = null;
let draftSaveTimer = null;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const uid = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

const els = {
  shell: $("#fc-shell"), sidebar: $("#fc-sidebar"), menu: $("#menu-button"),
  scrim: $("#sidebar-scrim"), newChat: $("#new-chat"), conversations: $("#conversation-list"),
  conversationCount: $("#conversation-count"),
  download: $("#download-latest"), stage: $("#stage"),
  previewPanel: $("#preview-panel"), previewMedia: $("#preview-media"), mediaLoading: $("#media-loading"),
  historyToggle: $("#history-toggle"), historyStrip: $("#history-strip"), refine: $("#refine-latest"),
  prompt: $("#prompt"), attachments: $("#attachment-list"), modeSwitch: $("#mode-switch"),
  modelSummary: $("#model-summary"), generateLabel: $("#generate-label"),
  modelModeSwitch: $("#model-mode-switch"), modelPopover: $("#model-popover"), modelList: $("#model-list"),
  paramsPopover: $("#params-popover"), paramForm: $("#param-form"), paramSummary: $("#param-summary"),
  fileInput: $("#file-input"), upload: $("#upload-bubble"), modelBubble: $("#model-bubble"), paramsBubble: $("#params-bubble"), generate: $("#generate"), toast: $("#toast"),
  serviceState: $("#service-state"), capabilityDot: $(".fc-capability-dot"),
  archiveToggle: $("#archive-toggle"), archivedList: $("#archived-list"), archivedCount: $("#archived-count"),
};

const state = {
  mode: "video", models: { video: [], image: [] }, selected: { video: null, image: null },
  params: {
    video: { ratio: "16:9", resolution: "720p", duration: 8, repeat_count: 1, upscale_target: "1080p", crf: 18, speed_mode: "standard" },
    image: { aspect_ratio: "1:1", image_size: "2K", repeat_count: 1, max_objects: 8, threshold: 0.3 },
  },
  attachments: [], conversations: [], activeConversationId: null, activeTaskId: null, busy: false, editSourceTaskId: null, editBasePrompt: "",
};

function loadStore() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    state.conversations = Array.isArray(parsed.conversations) ? parsed.conversations : [];
    state.activeConversationId = parsed.activeConversationId || null;
  } catch { state.conversations = []; }

  // 记录隔离（2026-09-15）：清掉以前从别的模块导入进来的历史对话
  // （带 historyConversation 标记）。只停止导入是不够的 —— 之前已经写进
  // localStorage 的混合记录不清掉，用户仍然会看到。
  // 清空后由下面的兜底逻辑（本函数末尾）自动补一个空白对话，不会出现零对话。
  state.conversations = state.conversations.filter((item) => !item?.historyConversation);

  const baseParams = structuredClone(state.params);
  state.conversations.forEach((conversation) => {
    const savedParams = conversation.params && typeof conversation.params === "object" ? conversation.params : {};
    conversation.prompt = typeof conversation.prompt === "string" ? conversation.prompt : "";
    conversation.params = {
      video: { ...baseParams.video, ...(savedParams.video || {}) },
      image: { ...baseParams.image, ...(savedParams.image || {}) },
    };
    conversation.mode = conversation.mode === "image" ? "image" : "video";
    const storedTitle = String(conversation.title || "").trim();
    const legacyAutoTitle = !storedTitle
      || /^对话\d+$/.test(storedTitle)
      || ["新对话", "鏂板璇?", "视频对话", "图片对话"].includes(storedTitle);
    conversation.titleCustom = conversation.titleCustom === true ? true : !legacyAutoTitle;
    if (!storedTitle || legacyAutoTitle) conversation.title = "新对话";
    conversation.archivedAt = conversation.archivedAt || null;
    conversation.tasks = Array.isArray(conversation.tasks) ? conversation.tasks : [];
    conversation.tasks.forEach((task) => { if (task?.resultUrl) task.status = "success"; });
  });
  if (!state.conversations.length) state.conversations = [newConversation()];
  let active = state.conversations.find((item) => item.id === state.activeConversationId && !item.archivedAt)
    || state.conversations.find((item) => !item.archivedAt);
  if (!active) {
    active = newConversation();
    state.conversations.unshift(active);
  }
  state.activeConversationId = active.id;
}function saveStore() {
  saveActiveConversationDraft();
  const regular = state.conversations.filter((item) => !item.historyConversation);
  const imported = state.conversations.filter((item) => item.historyConversation);
  //: 附件是内存里的 File 对象（切换对话时挂在 conversation.attachments 上），
  //: JSON 化会变成坏数据，写库前先剥掉。
  const conversations = [...regular.slice(0, 80), ...imported].map((item) => {
    if (!item || !item.attachments) return item;
    const { attachments, ...rest } = item;
    void attachments;
    return rest;
  });
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ conversations, activeConversationId: state.activeConversationId }));
}
function promptConversationTitle(prompt) {
  const text = String(prompt || "").replace(/\s+/g, " ").trim();
  if (!text) return "新对话";
  const sentences = text.match(/[^。！？!?.]+[。！？!?.]?/g) || [text];
  let candidate = sentences.slice(0, 2).join("").trim();
  const chars = [...candidate];
  let truncated = sentences.length > 2;
  if (chars.length > 26) {
    candidate = chars.slice(0, 26).join("");
    truncated = true;
  }
  if (truncated) candidate = candidate.replace(/[。！？!?.\s]+$/, "");
  return `${candidate}${truncated ? "..." : ""}`;
}
function conversationDisplayTitle(conversation) {
  if (!conversation) return "新对话";
  if (conversation.historyConversation && conversation.title) return conversation.title;
  if (conversation.titleCustom && conversation.title) return conversation.title;
  return promptConversationTitle(conversation.prompt);
}function newConversation() { return { id: uid(), title: "新对话", titleCustom: false, mode: state.mode || "video", prompt: "", params: structuredClone(state.params), tasks: [], createdAt: Date.now(), updatedAt: Date.now(), archivedAt: null }; }
function activeConversation() { return state.conversations.find((item) => item.id === state.activeConversationId) || state.conversations[0]; }
function saveActiveConversationDraft() {
  const conversation = activeConversation();
  if (!conversation) return;
  conversation.prompt = els.prompt ? els.prompt.value : conversation.prompt || "";
  conversation.params = structuredClone(state.params);
  //: 选中的模型也要存 —— 以前只存 prompt/params，刷新后 loadModels() 会把模型重置成默认的
  //: Seedance 2.5（用户 2026-09-21：「刷新的时候提示词选择的模型全部变成默认的了」）。
  conversation.selected = {
    video: state.selected.video?.id || "",
    image: state.selected.image?.id || "",
  };
}
function restoreConversationDraft(conversation) {
  if (!conversation) return;
  const draft = conversation.params && typeof conversation.params === "object" ? conversation.params : {};
  state.params.video = { ...state.params.video, ...(draft.video || {}) };
  state.params.image = { ...state.params.image, ...(draft.image || {}) };
  els.prompt.value = conversation.prompt || "";
  //: 模型要等 loadModels() 建好列表之后再套用（那时才有可选项），先记下来。
  state.pendingSelected = conversation.selected && typeof conversation.selected === "object"
    ? conversation.selected
    : null;
  autoResize();
}
function scheduleDraftSave() {
  clearTimeout(draftSaveTimer);
  draftSaveTimer = setTimeout(() => saveStore(), 180);
}
function latestTask(conversation = activeConversation()) { return conversation?.tasks.at(-1) || null; }
function activeTask() {
  const conversation = activeConversation();
  const selected = conversation?.tasks.find((item) => item.id === state.activeTaskId);
  if (selected) return selected;
  return conversation?.historyConversation ? latestSuccess(conversation) || latestTask(conversation) : latestTask(conversation);
}
function latestSuccess(conversation = activeConversation()) { return [...(conversation?.tasks || [])].reverse().find((item) => item.resultUrl) || null; }
//: 当前对话里正在跑的任务（有 → 生成按钮显示成方形暂停键）。
function runningTask(conversation = activeConversation()) {
  const tasks = conversation?.tasks || [];
  return tasks.find(
    (task) => task.jobId && RUNNING.has(String(task.status || "").toLowerCase()),
  ) || null;
}
//: 编辑输入超过这个大小就先提示一声（整段视频要读进内存，几十 MB 会明显卡一下）。
const EDIT_INPUT_WARN_BYTES = 40 * 1024 * 1024;
//: 「继续修改」要改的是**当前选中的那条**结果，不是永远最新那条。
//:
//: 用户 2026-09-21：「我编辑之前的历史视频好像界面卡住了，而且修改的是最新的视频，
//: 不能上传修改历史视频」。原来这里直接取 latestSuccess()，在历史条上点「继续修改」
//: 却改了最新一条 —— 现在优先用 state.activeTaskId（点历史条/结果卡片会更新它）。
function refineTarget(conversation = activeConversation()) {
  const tasks = conversation?.tasks || [];
  const active = tasks.find((item) => item.id === state.activeTaskId && item.resultUrl);
  return active || latestSuccess(conversation);
}
function formatTime(ts) { return new Date(ts || Date.now()).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])); }
function historySource(record) {
  const source = HISTORY_SOURCES[String(record?.app || "")];
  if (!source) return null;
  const kind = String(record?.kind || "").toLowerCase();
  const mode = source.mode || (kind === "video" ? "video" : kind === "image" ? "image" : null);
  return mode ? { ...source, mode } : null;
}

function historyResult(record) {
  const items = Array.isArray(record?.results) ? record.results : [];
  const kind = String(record?.kind || "").toLowerCase();
  return items.find((item) => (item?.url || item?.download_url) && (!item.kind || String(item.kind).toLowerCase() === kind))
    || items.find((item) => item?.url || item?.download_url)
    || null;
}

function directMediaUrl(task) {
  const sources = {
    seedance: { mount: "/seedance", port: 8787 },
    "nano-banana": { mount: "/nano-banana", port: 8797 },
    "volcengine-portrait": { mount: "/volcengine-portrait", port: 8891 },
  };
  const app = task.app || (task.mode === "video" ? "seedance" : "nano-banana");
  const source = sources[app];
  const raw = String(task.resultUrl || "");
  if (!source || !raw || /^(?:https?:)?\/\//i.test(raw)) return "";
  const path = raw.startsWith(source.mount) ? raw.slice(source.mount.length) : raw.startsWith("/api/") ? raw : "";
  return path ? `http://127.0.0.1:${source.port}${path}` : "";
}

function bindMediaFallback(media, task) {
  media.addEventListener("error", () => {
    const fallback = directMediaUrl(task);
    if (!fallback || media.dataset.fallbackSource === fallback) return;
    media.dataset.fallbackSource = fallback;
    media.src = fallback;
  });
}
function historyResultUrl(app, rawUrl) {
  const value = String(rawUrl || "").trim();
  if (!value) return "";
  if (/^(?:https?:)?\/\//i.test(value) || value.startsWith("data:") || value.startsWith("blob:")) return value;
  if (value.startsWith("/api/")) return `${HISTORY_SOURCES[app]?.mount || ""}${value}`;
  return value;
}

function historyTaskFromRecord(record, conversation, mode) {
  const result = historyResult(record);
  const rawUrl = result?.url || result?.download_url || record?.thumb_url || "";
  const resultUrl = historyResultUrl(record.app, rawUrl);
  const statusText = String(record?.status || "").toLowerCase();
  let status = "running";
  if (TERMINAL_FAILURE.has(statusText)) status = "failed";
  else if (TERMINAL_SUCCESS.has(statusText) || (statusText === "partial" && resultUrl) || resultUrl) status = "success";
  const submittedAt = Number(record?.submitted_at || 0) * 1000 || Date.now();
  const completedAt = Number(record?.completed_at || 0) * 1000;
  return {
    id: `history-${record.app}-${record.job_id}`,
    app: String(record.app || ""),
    jobId: String(record.job_id || ""),
    mode,
    prompt: String(record.prompt || record.title || "\u5386\u53f2\u751f\u6210\u4efb\u52a1"),
    model: String(record.model || record.app || ""),
    modelId: String(record.model || ""),
    provider: String(record.app || ""),
    params: record.params && typeof record.params === "object" ? structuredClone(record.params) : {},
    attachments: [],
    status,
    progress: status === "success" ? "\u5df2\u540c\u6b65\u5386\u53f2\u8bb0\u5f55" : status === "failed" ? "\u751f\u6210\u5931\u8d25" : "\u6b63\u5728\u540c\u6b65\u72b6\u6001",
    error: String(record.error || ""),
    resultUrl,
    resultName: result?.filename || `history-${record.job_id || "result"}`,
    conversationId: conversation.id,
    createdAt: submittedAt,
    updatedAt: Math.max(submittedAt, completedAt || 0),
    importedFromHistory: true,
  };
}

function mergeTaskFromHistory(task, record, source) {
  const snapshot = historyTaskFromRecord(record, { id: task.conversationId || "" }, source.mode);
  task.app = snapshot.app || task.app;
  task.jobId = snapshot.jobId || task.jobId;
  task.mode = snapshot.mode;
  if (snapshot.prompt) task.prompt = snapshot.prompt;
  if (snapshot.model) task.model = snapshot.model;
  if (snapshot.modelId) task.modelId = snapshot.modelId;
  if (snapshot.provider) task.provider = snapshot.provider;
  if (Object.keys(snapshot.params).length) task.params = snapshot.params;
  if (snapshot.resultUrl) {
    task.resultUrl = snapshot.resultUrl;
    task.resultName = snapshot.resultName || task.resultName;
  }
  task.status = snapshot.status;
  task.progress = snapshot.progress;
  task.error = snapshot.error || (snapshot.status === "failed" ? task.error : "");
  task.createdAt = task.createdAt || snapshot.createdAt;
  task.updatedAt = Math.max(Number(task.updatedAt || 0), snapshot.updatedAt);
  task.importedFromHistory = true;
  return task;
}

function findLocalHistoryTask(record, source) {
  const app = String(record.app || "");
  const jobId = String(record.job_id || "");
  for (const conversation of state.conversations) {
    const task = conversation.tasks.find((item) => String(item.jobId || "") === jobId
      && (!item.app || item.app === app)
      && (!item.mode || item.mode === source.mode));
    if (task) return task;
  }
  return null;
}

function ensureHistoryConversation(mode) {
  let conversation = state.conversations.find((item) => item.historyConversation && (item.mode || "video") === mode);
  if (!conversation) {
    conversation = {
      id: `history-${mode}`,
      title: mode === "video" ? "\u89c6\u9891\u751f\u6210\u8bb0\u5f55" : "\u56fe\u7247\u751f\u6210\u8bb0\u5f55",
      mode,
      historyConversation: true,
      tasks: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
      archivedAt: null,
    };
    state.conversations.push(conversation);
  }
  return conversation;
}

async function syncUnifiedHistory({ selectIfEmpty = false } = {}) {
  if (historySyncPromise) return historySyncPromise;
  historySyncPromise = (async () => {
    try {
      const response = await fetch("/api/platform/history?days=30&limit=200", {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) return false;
      const body = await response.json().catch(() => ({}));
      const records = Array.isArray(body.items) ? body.items : [];
      const unique = new Map();
      for (const record of records) {
        const source = historySource(record);
        if (!source || !record?.job_id) continue;
        const key = `${record.app}:${record.job_id}`;
        const previous = unique.get(key);
        const score = (historyResult(record) ? 2 : 0) + (record.thumb_url ? 1 : 0);
        const previousScore = previous ? (historyResult(previous) ? 2 : 0) + (previous.thumb_url ? 1 : 0) : -1;
        if (!previous || score > previousScore || (score === previousScore
          && Number(record.submitted_at || 0) >= Number(previous.submitted_at || 0))) {
          unique.set(key, record);
        }
      }

      const touched = new Set();
      const importedModes = new Set();
      for (const record of unique.values()) {
        const source = historySource(record);
        if (!source) continue;
        const local = findLocalHistoryTask(record, source);
        if (local) {
          mergeTaskFromHistory(local, record, source);
          continue;
        }
        const conversation = ensureHistoryConversation(source.mode);
        let task = conversation.tasks.find((item) => item.jobId === record.job_id && (!item.app || item.app === record.app));
        if (!task) {
          task = historyTaskFromRecord(record, conversation, source.mode);
          conversation.tasks.push(task);
        } else {
          mergeTaskFromHistory(task, record, source);
        }
        importedModes.add(source.mode);
        touched.add(conversation);
      }

      for (const conversation of touched) {
        conversation.tasks.sort((a, b) => Number(a.createdAt || 0) - Number(b.createdAt || 0));
        conversation.updatedAt = conversation.tasks.reduce((latest, task) => Math.max(latest, Number(task.updatedAt || 0)), 0);
      }

      const active = activeConversation();
      if (selectIfEmpty && (!active || !active.tasks?.length)) {
        const imported = state.conversations
          .filter((item) => item.historyConversation && !item.archivedAt && importedModes.has(item.mode) && item.tasks.length)
          .find((item) => item.mode === state.mode);
        if (imported) {
          state.activeConversationId = imported.id;
          const latest = latestSuccess(imported) || latestTask(imported);
          state.activeTaskId = latest?.id || null;
          els.prompt.value = latest?.prompt || "";
          autoResize();
        }
      }

      saveStore();
      renderConversations();
      renderPreview();
      return true;
    } catch (error) {
      console.warn("\u81ea\u7531\u521b\u4f5c\u5386\u53f2\u540c\u6b65\u5931\u8d25", error);
      return false;
    } finally {
      historySyncPromise = null;
    }
  })();
  return historySyncPromise;
}

// 因为一次网络抖动被标成 failed 的任务，重开页面时要能认领回来。只认「读取失败」
// 这种传输层原因，且限定在最近 30 分钟内，避免对真失败的任务无限重试。
function isTransportFailure(task) {
  if (String(task.status || "").toLowerCase() !== "failed") return false;
  if (!/^任务状态读取失败/.test(String(task.error || ""))) return false;
  return Date.now() - Number(task.updatedAt || task.createdAt || 0) < 30 * 60 * 1000;
}
async function reconcileRunningTasks() {
  const tasks = state.conversations
    .flatMap((conversation) => conversation.tasks)
    .filter((task) => task.jobId && (RUNNING.has(String(task.status || "").toLowerCase()) || isTransportFailure(task)))
    .slice(0, 24);
  if (!tasks.length) return false;

  let changed = false;
  await Promise.allSettled(tasks.map(async (task) => {
    if (task.app === "dreamina") return;
    const base = task.base || (task.app === "volcengine-portrait"
      ? "/volcengine-portrait"
      : task.mode === "video" ? "/seedance" : "/nano-banana");
    const jobPath = task.jobPath || "/api/jobs";
    try {
      const response = await fetch(`${base}${jobPath}/${encodeURIComponent(task.jobId)}`, {
        cache: "no-store",
        headers: { "X-Workspace-Id": task.conversationId || activeConversation()?.id || "" },
      });
      if (!response.ok) return;
      const data = await response.json().catch(() => ({}));
      const status = String(data.status || "running").toLowerCase();
      const resultItems = [
        ...(Array.isArray(data.results) ? data.results : []),
        ...(Array.isArray(data.job?.results) ? data.job.results : []),
        ...(Array.isArray(data.result?.results) ? data.result.results : []),
      ];
      const result = resultItems.find((item) => item?.download_url || item?.url);
      const progress = data.done && data.total ? `${data.done}/${data.total}` : progressText(task, data, status);
      const nextStatus = TERMINAL_FAILURE.has(status)
        ? "failed"
        : TERMINAL_SUCCESS.has(status) || (status === "partial" && (result?.download_url || result?.url)) || result
          ? "success"
          : "running";
      const nextError = nextStatus === "failed"
        ? String(data.error || data.errors?.[0]?.message || data.errors?.[0] || progress)
        : "";
      if (progress !== task.progress || nextStatus !== task.status || (result && !task.resultUrl) || nextError !== task.error) {
        changed = true;
      }
      task.progress = progress;
      task.status = nextStatus;
      task.error = nextError;
      if (result) {
        const rawUrl = result.download_url || result.url;
        task.resultUrl = absoluteMediaUrl(base, rawUrl);
        const urls = (Array.isArray(result.download_urls) ? result.download_urls : []).map((url) => absoluteMediaUrl(base, url)).filter(Boolean);
        task.resultUrls = urls.length ? urls : [task.resultUrl];
        task.resultName = result.filename || task.resultName;
      }
      task.updatedAt = Date.now();
    } catch {
      // History sync remains the fallback when a child app is offline.
    }
  }));

  if (changed) {
    saveStore();
    renderConversations();
    renderPreview();
    renderParamSummary();   // 发送键要在「生成中 ↔ 空闲」之间切换
  }
  return changed;
}

async function syncLiveState(options = {}) {
  // 记录隔离（2026-09-15，用户要求）：**不再导入**「视频生成 / 图片生成 / 人像生成」
  // 等模块的历史。这里原来每 15 秒把别的模块历史整份导进来（一次最多 200 条），
  // 两个后果：① 用户在本页看到不属于自己的记录；② 打开页面时发起上百个大文件
  // 请求（实测 372 次 / 185 个文件 / 约 1.8GB），把页面和 Portal 代理一起压死。
  // 现在本页只显示自己产生的记录。options 保留仅为兼容现有调用方。
  void options;
  return reconcileRunningTasks();
}

function toast(message, type = "") {
  els.toast.textContent = message;
  els.toast.className = `fc-toast is-visible${type === "error" ? " is-error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { els.toast.className = "fc-toast"; }, 3200);
}
//: 生成按钮在"有任务在跑"时要变成暂停键，所以**不能再靠 disabled 拦住重复提交**
//: （以前 setBusy(true) 会把按钮禁掉，用户既不能生成也不能暂停，正是
//: 「当前任务在进行的话没办法开始生成」）。重复提交由 generate() 里的 state.busy 兜住。
function setBusy(value) { state.busy = value; els.generate.classList.toggle("is-busy", value); }
// 「继续修改」会把 `修改要求：` 追加进输入框。退出编辑态时如果不把它一起收回去，
// 残片会跟着下一次提交走：buildPayload() 里 isEdit 已经是 false，于是这段文本被
// 当成普通提示词发出去（视频退回 task_mode=reference，编辑态按钮/角标也全没了）。
const EDIT_MARKER_RE = /(^|\n)[ \t]*\u4fee\u6539\u8981\u6c42[ \t]*[:\uff1a]/;
function stripEditFragment() {
  const current = String(els.prompt.value || "");
  const match = current.match(EDIT_MARKER_RE);
  if (!match) return;
  const head = current.slice(0, match.index).replace(/\s+$/, "");
  els.prompt.value = head || String(state.editBasePrompt || "").trim();
}
function clearEditContext({ render = true } = {}) {
  stripEditFragment();
  state.editSourceTaskId = null;
  state.editBasePrompt = "";
  if (render) renderParamSummary();
}
function editInstruction() {
  const prompt = els.prompt.value.trim();
  if (!state.editSourceTaskId) return prompt;
  const base = String(state.editBasePrompt || "").trim();
  if (base && prompt.startsWith(base)) {
    return prompt.slice(base.length).replace(/^\s*\u4fee\u6539\u8981\u6c42\s*[:：]?\s*/, "").trim();
  }
  return prompt.replace(/^\s*\u4fee\u6539\u8981\u6c42\s*[:：]?\s*/, "").trim();
}
function setMode(mode) {
  let conversation = activeConversation();
  const switchConversation = !conversation || (conversation.mode || "video") !== mode;
  if (state.mode !== mode || switchConversation) clearEditContext({ render: false });
  if (switchConversation) saveActiveConversationDraft();
  state.mode = mode;
  if (switchConversation) {
    conversation = state.conversations
      .filter((item) => !item.archivedAt && (item.mode || "video") === mode)
      .sort((a, b) => b.updatedAt - a.updatedAt);
    conversation = conversation.find((item) => item.tasks?.length) || conversation[0];
    if (!conversation) {
      conversation = newConversation();
      state.conversations.unshift(conversation);
    }
    state.activeConversationId = conversation.id;
    state.activeTaskId = null;
    //: 附件跟着对话走（用户 2026-09-21：「切换对话，之前上传的视频就没了」）。
    //: File 对象只在内存里有效，所以切走时留在对话对象上、切回来再取回；
    //: saveStore() 会把它剥掉，避免写进 localStorage 变成坏数据。
    const outgoing = state.conversations.find((item) => item.id !== conversation.id && item.attachments === state.attachments);
    if (outgoing) outgoing.attachments = state.attachments;
    state.attachments = Array.isArray(conversation.attachments) ? conversation.attachments : [];
    renderAttachments();
    restoreConversationDraft(conversation);
    const latest = latestSuccess(conversation) || latestTask(conversation);
    state.activeTaskId = latest?.id || null;
    els.prompt.value = conversation.prompt || latest?.prompt || "";
    autoResize();
  }
  els.shell.dataset.mode = mode;
  els.modeSwitch.dataset.mode = mode;
  els.modelModeSwitch.querySelectorAll("button").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === mode));
  els.modeSwitch.querySelectorAll(".fc-mode-option").forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  saveStore();
  renderConversations();
  renderModelList();
  renderParams();
  renderParamSummary();
  renderPreview();
}function normalizeModels({ seedance, nano, portrait, segment, upscale }) {
  const video = [], image = [];
  Object.entries(seedance.providers || {}).forEach(([key, provider]) => {
    const local = key === "comfyui_local";
    (provider.models || []).forEach((model) => video.push({
      id: model.id, label: model.label || model.id, provider: key, providerLabel: provider.label || key,
      local, available: true, badge: local ? "FREE" : "CLOUD",
      duration: model.duration_range || provider.duration_range || [4, 15],
      resolutions: model.resolutions || provider.resolutions || ["480p", "720p"],
      ratios: model.ratios || provider.ratios || ["16:9", "9:16", "1:1"],
      defaults: { ...(provider.defaults || {}), ...(model.defaults || {}) },
    }));
  });
  Object.entries(nano.providers || {}).forEach(([key, provider]) => {
    const local = key === "comfyui_local";
    (provider.models || []).forEach((model) => image.push({
      id: model.id, label: model.label || model.id, provider: key, providerLabel: provider.label || key,
      local, available: true, badge: local ? "FREE" : key === "volcengine" ? "SERVER KEY" : "CLOUD",
      sizes: model.capabilities?.image_size || provider.image_size_options || ["1K", "2K"],
      ratios: model.capabilities?.aspect_ratio || provider.aspect_ratio_options || ["auto", "1:1", "16:9", "9:16", "4:3", "3:4"],
      defaults: { ...(provider.defaults || {}), ...(model.defaults || {}) },
    }));
  });
  // 本地网关的抠图 / 超分：网关报了这两个模块（且 schema 拉得到）才可点，否则灰显
  // 「待接入」。portrait 配置本身拉失败时不据此判死，退回「schema 能拉到就算在线」。
  const gatewayKnown = Boolean(portrait?.local_gateway);
  const readyModules = new Set((portrait?.local_gateway?.modules || []).map((item) => String(item.id || "")));
  const addLocalCapability = (id, schema) => {
    const spec = LOCAL_CAPABILITIES[id];
    if (!spec) return;
    const provider = (schema?.providers || {}).comfyui_local || {};
    const ready = Boolean(schema) && (gatewayKnown ? readyModules.has(id) : true);
    (spec.mode === "image" ? image : video).push({
      id, label: spec.label, provider: "local_gateway", providerLabel: "本地 AI Port",
      local: true, available: ready, badge: ready ? "FREE" : "待接入", module: id,
      duration: [1, 30], resolutions: ["1080p", "2K", "4K"], ratios: ["auto"], sizes: ["1K", "2K"],
      variants: (provider.models || []).map((item) => ({ id: item.id, label: item.label || item.id })),
      defaults: { ...(provider.defaults || {}) },
    });
  };
  addLocalCapability("image_segment", segment);
  addLocalCapability("video_upscale", upscale);
  //: 独立的「视频编辑」入口（用户 2026-09-21 要求「单独再开一个模型，视频编辑」）。
  //:
  //: 选它即进入编辑态：原视频由用户**自己上传**或从历史里选，不再绑死"最新一条"
  //: （那正是「不能上传修改历史视频」的根源）。提交时固定 task_mode=edit、
  //: duration=-1、ratio=adaptive —— 由后端 seedance 按编辑任务处理。
  video.unshift({
    id: "seedance-2.5-edit",
    //: 下发给 Ark 的**真实模型 id** —— 上面那个 id 只是界面用的伪 id（下拉框需要一个唯一值），
    //: 不能直接当模型名发出去，否则 Ark 报
    //: `InvalidEndpointOrModel.NotFound: The model or endpoint seedance-2.5-edit does not exist`
    //: （2026-09-21 线上实测）。
    modelId: "doubao-seedance-2-5-260628",
    label: "Seedance 2.5 · 视频编辑（局部修改已有视频）",
    provider: "volcengine",
    providerLabel: "火山方舟",
    local: false,
    available: true,
    badge: "2.5 专属",
    editOnly: true,
    duration: [-1, -1],
    resolutions: ["720p", "1080p", "2K"],
    ratios: ["adaptive"],
    defaults: {},
  });
  state.models = { video, image };
  state.selected.video = video.find((model) => model.available && model.id === "doubao-seedance-2-5-260628") || video.find((model) => model.available) || video[0] || null;
  state.selected.image = image.find((model) => model.available && model.local) || image.find((model) => model.available) || image[0] || null;
  //: 套用草稿里存的模型选择（刷新后不该被重置成默认；2026-09-21）。
  const pending = state.pendingSelected;
  if (pending && typeof pending === "object") {
    const videoPick = video.find((model) => model.id === pending.video && model.available);
    const imagePick = image.find((model) => model.id === pending.image && model.available);
    if (videoPick) state.selected.video = videoPick;
    if (imagePick) state.selected.image = imagePick;
  }
  state.pendingSelected = null;
  if (state.selected.video) {
    const d = state.selected.video.defaults || {};
    const start = state.selected.video.duration?.[0] || 4;
    const end = state.selected.video.duration?.[1] || 15;
    const current = state.params.video || {};
    state.params.video = {
      ratio: clampOption(current.ratio, state.selected.video.ratios, d.ratio || "16:9"),
      resolution: clampOption(current.resolution, state.selected.video.resolutions, d.resolution || "720p"),
      duration: clampNumber(current.duration, start, end, d.duration || 8),
      repeat_count: clampNumber(current.repeat_count, 1, 4, d.repeat_count || 1),
      upscale_target: UPSCALE_TARGETS.some((item) => item.value === current.upscale_target) ? current.upscale_target : "1080p",
      crf: clampNumber(current.crf, 12, 28, 18),
      speed_mode: current.speed_mode === "fast" ? "fast" : "standard",
    };
  }
  if (state.selected.image) {
    const d = state.selected.image.defaults || {};
    const current = state.params.image || {};
    state.params.image = {
      aspect_ratio: clampOption(current.aspect_ratio, state.selected.image.ratios, d.aspect_ratio || "auto"),
      image_size: clampOption(current.image_size, state.selected.image.sizes, d.image_size || "2K"),
      repeat_count: clampNumber(current.repeat_count, 1, 8, d.repeat_count || 1),
      max_objects: clampNumber(current.max_objects, 1, 20, d.max_objects || 8),
      threshold: [0.2, 0.3, 0.4, 0.5].includes(Number(current.threshold)) ? Number(current.threshold) : Number(d.threshold) || 0.3,
    };
  }
  els.serviceState.textContent = `${video.filter((m) => m.available).length + image.filter((m) => m.available).length} 个模型可用`;
  els.capabilityDot.classList.add("is-ready");
}

async function loadModels() {
  try {
    const [video, image, portrait, segment, upscale] = await Promise.all(
      Object.values(SCHEMA_URLS).map((url) => fetch(url).then((res) => res.ok ? res.json() : null).catch(() => null)),
    );
    normalizeModels({ seedance: video || {}, nano: image || {}, portrait: portrait || {}, segment, upscale });
    renderModelList(); renderParams(); renderParamSummary();
  } catch (error) { els.serviceState.textContent = "模型服务加载失败"; toast(`模型加载失败：${error.message}`, "error"); }
}
function currentModel() { return state.selected[state.mode]; }
function clampNumber(value, min, max, fallback) { const number = Number(value); if (!Number.isFinite(number)) return Number(fallback) || min; return Math.min(max, Math.max(min, Math.round(number))); }
function clampOption(value, options, fallback) { return options?.includes(value) ? value : options?.includes(fallback) ? fallback : options?.[0] || value; }
function renderModelList() {
  els.modelList.innerHTML = "";
  const groups = new Map([["云端模型", []], ["本地能力", []]]);
  (state.models[state.mode] || []).forEach((model) => groups.get(model.local ? "本地能力" : "云端模型").push(model));
  [...groups.entries()].filter(([, items]) => items.length).forEach(([label, items]) => {
    const title = document.createElement("div"); title.className = "fc-model-group-title"; title.textContent = label; els.modelList.append(title);
    items.forEach((model) => {
      const button = document.createElement("button"); button.type = "button"; button.className = "fc-model-option";
      button.disabled = !model.available; button.classList.toggle("is-active", currentModel()?.id === model.id && currentModel()?.provider === model.provider);
      button.innerHTML = `<span class="fc-model-symbol"><svg viewBox="0 0 24 24"><path d="m12 3-1.9 5.1L5 10l5.1 1.9L12 17l1.9-5.1L19 10l-5.1-1.9Z"/></svg></span><span class="fc-model-copy"><strong>${escapeHtml(model.label)}</strong><small>${escapeHtml(model.providerLabel || model.provider)}</small></span><span class="fc-model-badge">${escapeHtml(model.badge || "")}</span>`;
      button.addEventListener("click", () => {
        if (!model.available) { toast(`${model.label} 尚未接入自由创作执行层`, "error"); return; }
        // 换到不支持「继续修改」的模型（抠图 / 超分 / 本地视频）时，先把编辑态收干净，
        // 否则残存的编辑标记会让下一次提交被拒或退化。
        if (!canRefine(model) && state.editSourceTaskId) clearEditContext({ render: false });
        state.selected[state.mode] = model; applyModelDefaults(model); renderModelList(); renderParams(); renderParamSummary(); closePopover("model-popover");
      });
      els.modelList.append(button);
    });
  });
}
function applyModelDefaults(model) {
  const d = model.defaults || {};
  const params = state.params[state.mode];
  if (state.mode === "video") {
    // 超分是「换任务」不是「换模型」：比例/分辨率/时长都不适用，只有目标尺寸、
    // 画质和速度模式。
    if (model.module === "video_upscale") {
      params.upscale_target = clampOption(params.upscale_target, UPSCALE_TARGETS.map((item) => item.value), "1080p");
      params.crf = clampNumber(params.crf, 12, 28, Number(d.crf) || 18);
      params.speed_mode = clampOption(params.speed_mode, UPSCALE_SPEED_MODES.map((item) => item.value), "standard");
      return;
    }
    params.ratio = clampOption(params.ratio, model.ratios, d.ratio || "16:9");
    params.resolution = clampOption(params.resolution, model.resolutions, d.resolution || "720p");
    params.duration = clampNumber(params.duration, model.duration?.[0] || 4, model.duration?.[1] || 15, d.duration || 8);
    params.repeat_count = clampNumber(params.repeat_count, 1, 4, d.repeat_count || 1);
    return;
  }
  if (model.module === "image_segment") {
    params.max_objects = clampNumber(params.max_objects, 1, 20, Number(d.max_objects) || 8);
    params.threshold = clampOption(Number(params.threshold), SEGMENT_THRESHOLDS.map((item) => item.value), Number(d.threshold) || 0.3);
    return;
  }
  params.aspect_ratio = clampOption(params.aspect_ratio, model.ratios, d.aspect_ratio || "auto");
  params.image_size = clampOption(params.image_size, model.sizes, d.image_size || "2K");
  params.repeat_count = clampNumber(params.repeat_count, 1, 8, d.repeat_count || 1);
}
function paramField(label, options, key, value) {
  const wrap = document.createElement("div");
  wrap.className = "fc-param-field";
  wrap.innerHTML = `<label>${escapeHtml(label)}</label><div class="fc-param-options"></div>`;
  wrap.addEventListener("click", (event) => event.stopPropagation());
  const row = wrap.querySelector(".fc-param-options");
  options.forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `fc-param-option${String(option) === String(value) ? " is-active" : ""}`;
    button.textContent = option;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      state.params[state.mode][key] = option;
      row.querySelectorAll(".fc-param-option").forEach((item) => item.classList.toggle("is-active", item === button));
      renderParamSummary();
      saveStore();
    });
    row.append(button);
  });
  return wrap;
}

// 选项带「显示名 + 真实值」的下拉式参数（值可以是数字或对象），抠图阈值、超分目标
// 分辨率用得到。paramField 只支持「显示值 = 提交值」的字符串选项，不动它。
function paramChoiceField(label, choices, key, value) {
  const wrap = document.createElement("div");
  wrap.className = "fc-param-field";
  wrap.innerHTML = `<label>${escapeHtml(label)}</label><div class="fc-param-options"></div>`;
  wrap.addEventListener("click", (event) => event.stopPropagation());
  const row = wrap.querySelector(".fc-param-options");
  choices.forEach((choice) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `fc-param-option${String(choice.value) === String(value) ? " is-active" : ""}`;
    button.textContent = choice.label;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      state.params[state.mode][key] = choice.value;
      row.querySelectorAll(".fc-param-option").forEach((item) => item.classList.toggle("is-active", item === button));
      renderParamSummary();
      saveStore();
    });
    row.append(button);
  });
  return wrap;
}

function paramNumberField(label, key, value, min, max, unit, rangeText) {
  const safeValue = clampNumber(value, min, max, value);
  const wrap = document.createElement("div");
  wrap.className = "fc-param-field";
  wrap.innerHTML = `<label>${escapeHtml(label)}</label><div class="fc-param-number"><input type="number" inputmode="numeric" min="${min}" max="${max}" step="1" value="${safeValue}"><span>${escapeHtml(unit)}</span></div><small class="fc-param-range">${escapeHtml(rangeText)}</small>`;
  wrap.addEventListener("click", (event) => event.stopPropagation());
  const input = wrap.querySelector("input");
  const commit = (clampToRange) => {
    const number = Number(input.value);
    if (!Number.isFinite(number)) {
      if (clampToRange) input.value = String(state.params[state.mode][key] ?? safeValue);
      return;
    }
    const normalized = Math.min(max, Math.max(min, Math.round(number)));
    state.params[state.mode][key] = normalized;
    if (clampToRange) input.value = String(normalized);
    renderParamSummary();
    saveStore();
  };
  input.addEventListener("input", () => commit(false));
  input.addEventListener("change", () => commit(true));
  input.addEventListener("blur", () => commit(true));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      input.blur();
    }
  });
  return wrap;
}

function renderParams() {
  const model = currentModel();
  els.paramForm.innerHTML = "";
  if (!model?.available) {
    els.paramForm.innerHTML = "<p class=\"fc-model-copy\"><small>\u5f53\u524d\u6a21\u578b\u5c1a\u672a\u63a5\u5165\u6267\u884c\u80fd\u529b\u3002</small></p>";
    return;
  }
  const params = state.params[state.mode];
  if (state.mode === "video") {
    if (model.module === "video_upscale") {
      els.paramForm.append(
        paramChoiceField("目标分辨率", UPSCALE_TARGETS, "upscale_target", params.upscale_target),
        paramChoiceField("速度模式", UPSCALE_SPEED_MODES, "speed_mode", params.speed_mode),
        paramNumberField("画质 CRF", "crf", params.crf, 12, 28, "", "数值越小越清晰、文件越大（默认 18）"),
      );
      // 目标框是「等比缩进 box」：竖屏素材选 1080p 会被缩成 608x1080，比源素材还小
      // （用户 2026-09-21 实测反馈「超分完比原视频还小」）。这里直接把实际输出尺寸
      // 摆出来，并在会缩小时警告。素材尺寸由 probeVideoSize() 异步补上，
      // 切换目标时由 renderParamSummary() 刷新同一元素。
      const hint = document.createElement("p");
      hint.className = "fc-param-hint";
      hint.id = "upscale-fit-hint";
      hint.textContent = upscaleFitHint(params);
      els.paramForm.append(hint);
      return;
    }
    //: 编辑模式（「Seedance 2.5 · 视频编辑」）：Ark 规定 `duration` 必须 -1、
    //: `ratio` 必须 adaptive（见 portal/ark_errors.py 的错误翻译），**这两个值不由用户选**。
    //: 以前照常渲染数字输入框，min/max 都是 -1 → 浏览器把非法值显示成 0，用户以为
    //: "只能选 -1 秒、默认 0"（2026-09-21）。这里直接不给这两个控件，改成说明文字。
    if (model.editOnly) {
      els.paramForm.append(
        paramField("分辨率", model.resolutions || ["720p"], "resolution", params.resolution),
        paramNumberField("重复次数", "repeat_count", params.repeat_count, 1, 4, "次", "范围 1-4 次"),
      );
      const editHint = document.createElement("p");
      editHint.className = "fc-param-hint";
      editHint.textContent = "编辑模式：时长由模型按原视频自动决定（接口固定 -1），比例固定为「自适应」—— 不用你选，提交时会自动带上。";
      els.paramForm.append(editHint);
      return;
    }
    const start = model.duration?.[0] || 4;
    const end = model.duration?.[1] || 15;
    els.paramForm.append(
      paramField("\u753b\u9762\u6bd4\u4f8b", model.ratios || ["16:9"], "ratio", params.ratio),
      paramField("\u5206\u8fa8\u7387", model.resolutions || ["720p"], "resolution", params.resolution),
      paramNumberField("\u65f6\u957f", "duration", params.duration, start, end, "\u79d2", `\u8303\u56f4 ${start}-${end} \u79d2`),
      paramNumberField("\u91cd\u590d\u6b21\u6570", "repeat_count", params.repeat_count, 1, 4, "\u6b21", "\u8303\u56f4 1-4 \u6b21"),
    );
  } else {
    if (model.module === "image_segment") {
      els.paramForm.append(
        paramNumberField("最多抠出几个物体", "max_objects", params.max_objects, 1, 20, "个", "范围 1-20 个"),
        paramChoiceField("检测灵敏度", SEGMENT_THRESHOLDS, "threshold", params.threshold),
      );
      return;
    }
    els.paramForm.append(
      paramField("\u753b\u9762\u6bd4\u4f8b", model.ratios || ["auto"], "aspect_ratio", params.aspect_ratio),
      paramField("\u5c3a\u5bf8", model.sizes || ["2K"], "image_size", params.image_size),
      paramNumberField("\u91cd\u590d\u6b21\u6570", "repeat_count", params.repeat_count, 1, 8, "\u5f20", "\u8303\u56f4 1-8 \u5f20"),
    );
  }
}
function renderParamSummary() {
  const model = currentModel();
  const values = state.params[state.mode];
  const isEdit = (Boolean(state.editSourceTaskId) || Boolean(model?.editOnly)) && canRefine(model);
  const capability = model?.module;
  if (els.modelSummary) els.modelSummary.textContent = model?.label || "\u9009\u62e9\u6a21\u578b";
  const verb = capability === "image_segment" ? "开始抠图" : capability === "video_upscale" ? "开始超分" : state.mode === "video" ? "\u751f\u6210\u89c6\u9891" : "\u751f\u6210\u56fe\u7247";
  //: 有任务在跑时，这个按钮变成**方形暂停键**，点它就是取消（用户 2026-09-21 要求：
  //: 「当前任务在进行的话没办法开始生成，那个键就会变成一个暂停任务的标记」）。
  const running = runningTask();
  els.generate.classList.toggle("is-running", Boolean(running));
  const actionLabel = running ? "暂停任务" : (isEdit ? "提交修改" : verb);
  if (els.generateLabel) els.generateLabel.textContent = actionLabel;
  els.generate.setAttribute("aria-label", actionLabel);
  els.generate.setAttribute("title", actionLabel);
  // 抠图 / 超分没有「在原结果上继续改」的语义，禁用而不是隐藏（避免按钮位置跳动）。
  const refineAllowed = canRefine(model);
  els.refine.disabled = !refineAllowed;
  els.refine.setAttribute("aria-pressed", String(isEdit));
  els.refine.classList.toggle("is-active", isEdit);
  els.refine.title = refineAllowed ? "\u5728\u5f53\u524d\u7ed3\u679c\u4e0a\u7ee7\u7eed\u4fee\u6539" : "抠图 / 超分不支持继续修改";
  const chips = [];
  if (capability === "image_segment") chips.push(`${values.max_objects} 个物体`, `阈值 ${values.threshold}`);
  else if (capability === "video_upscale") chips.push(values.upscale_target, values.speed_mode === "fast" ? "快速" : "标准", `CRF ${values.crf}`);
  else if (state.mode === "video" && isEdit) chips.push("\u667a\u80fd\u89c6\u9891\u7f16\u8f91");
  else if (state.mode === "video") chips.push(values.ratio, values.resolution, `${values.duration}s`, values.repeat_count > 1 ? `\u00d7${values.repeat_count}` : "\u4e0d\u91cd\u590d");
  else chips.push(values.aspect_ratio, values.image_size, values.repeat_count > 1 ? `\u00d7${values.repeat_count}` : "1 \u5f20");
  els.paramSummary.innerHTML = chips.map((chip) => `<span class="fc-param-chip"><strong>${escapeHtml(chip)}</strong></span>`).join("");
  // 切换「目标分辨率」时刷新超分的尺寸提示（素材宽高是异步补上的，所以这里也要更新）。
  const fitHint = document.getElementById("upscale-fit-hint");
  if (fitHint) fitHint.textContent = upscaleFitHint(values);
}
// 「继续修改」只在云端 Seedance 的视频编辑与图片修改里有实现；本地视频模型、抠图、
// 超分都没有这个能力（本地视频会把编辑请求当成一次全新生成，白烧算力）。
function canRefine(model) { return !model?.module && !(model?.local && state.mode === "video"); }function autoResize() { els.prompt.style.height = "auto"; els.prompt.style.height = `${Math.min(220, els.prompt.scrollHeight)}px`; }

function mediaKind(file) {
  const mime = (file.type || "").toLowerCase(), ext = (file.name || "").split(".").pop()?.toLowerCase();
  if (mime.startsWith("image/") || ["png", "jpg", "jpeg", "webp", "gif"].includes(ext)) return "image";
  if (mime.startsWith("video/") || ["mp4", "mov", "webm", "mkv"].includes(ext)) return "video";
  if (mime.startsWith("audio/") || ["mp3", "wav", "m4a", "aac", "ogg"].includes(ext)) return "audio";
  return "unknown";
}
function addFiles(fileList) {
  const added = [];
  [...fileList].forEach((file) => {
    const kind = mediaKind(file);
    if (kind === "unknown") { toast(`不支持的文件：${file.name}`, "error"); return; }
    //: 视频也要给预览：以前只给图片建 objectURL，视频在附件条上只显示「VID」两个字，
    //: 用户看不出自己传的是哪一段（2026-09-21：「上传的参考视频参考图没有预览」）。
    const previewUrl = kind === "image" || kind === "video" ? URL.createObjectURL(file) : "";
    const item = { id: uid(), file, kind, previewUrl };
    state.attachments.push(item);
    added.push(item);
  });
  renderAttachments();
  // 视频素材读一下宽高：超分的「目标分辨率」是等比缩进一个框，竖屏素材可能被缩得
  // 比源素材还小（用户 2026-09-21 反馈「超分完比原视频还小」）。拿到尺寸才能在
  // 参数面板里把真实输出尺寸和警告摆出来。
  added
    .filter((item) => item.kind === "video" && item.previewUrl)
    .forEach((item) => probeVideoSize(item).then(() => { renderParams(); renderParamSummary(); }));
}
// 用离屏 video 读素材宽高；拿不到就返回 null（UI 退回只显示耗时提示，不影响提交）。
function probeVideoSize(attachment) {
  return new Promise((resolve) => {
    const probe = document.createElement("video");
    let settled = false;
    const finish = (size) => {
      if (settled) return;
      settled = true;
      attachment.width = size ? size.width : 0;
      attachment.height = size ? size.height : 0;
      probe.removeAttribute("src");
      resolve(size);
    };
    probe.muted = true;
    probe.playsInline = true;
    probe.preload = "metadata";
    probe.addEventListener("loadedmetadata", () => finish({ width: probe.videoWidth, height: probe.videoHeight }), { once: true });
    probe.addEventListener("error", () => finish(null), { once: true });
    setTimeout(() => finish(null), 8000);
    probe.src = attachment.previewUrl;
  });
}
// 与网关 fit_box_for_short_side 同一套算法：短边 = 档位，保持素材宽高比，取偶数。
// （工作流里 ImageResizeKJv2 的 divisible_by=2，所以尺寸必须是偶数。）
function fitBoxForShortSide(source, shortSide) {
  if (!source || !shortSide || !source.width || !source.height) return null;
  const k = shortSide / Math.min(source.width, source.height);
  return {
    width: Math.max(2, Math.round(source.width * k / 2) * 2),
    height: Math.max(2, Math.round(source.height * k / 2) * 2),
  };
}
// 超分参数面板的说明文字：把「素材 → 实际输出」算出来。
// 口径 = 短边档位（与视频生成模块一致），所以竖屏 720x1280 选 1080p = 1080x1920。
function upscaleFitHint(params) {
  const video = state.attachments.find((item) => item.kind === "video" && item.width);
  const base = "本地超分耗时约等于「素材时长 × 1 分钟」。快速模式约 2.4 倍速，但细节会下降。";
  const target = UPSCALE_TARGETS.find((item) => item.value === params.upscale_target) || UPSCALE_TARGETS[1];
  if (!video) return `分辨率档位按「短边」算（与视频生成模块一致）。${base}`;
  const box = fitBoxForShortSide({ width: video.width, height: video.height }, target.shortSide);
  if (!box) return base;
  const k = target.shortSide / Math.min(video.width, video.height);
  const head = `素材 ${video.width}×${video.height} → 输出 ${box.width}×${box.height}`;
  if (k < 1) return `${head}（比素材小）。${base}`;
  return `${head}（${target.label} = 短边 ${target.shortSide}，放大 ${k.toFixed(2)} 倍）。${base}`;
}
function renderAttachments() {
  els.attachments.innerHTML = "";
  state.attachments.forEach((item) => {
    const node = $("#asset-template").content.firstElementChild.cloneNode(true); node.dataset.id = item.id;
    const preview = node.querySelector(".fc-asset-preview");
    //: 图片 / 视频都出缩略图；拿不到预览地址（例如从草稿恢复、File 已不在）时退回类型标签，
    //: 不留下一个破图。
    if (item.kind === "image" && item.previewUrl) {
      const image = document.createElement("img"); image.src = item.previewUrl; image.alt = ""; image.decoding = "async";
      preview.append(image);
    } else if (item.kind === "video" && item.previewUrl) {
      const video = document.createElement("video");
      video.src = item.previewUrl; video.muted = true; video.playsInline = true;
      video.preload = "metadata";   // 只取首帧当封面，不整段加载
      preview.append(video);
    } else {
      preview.textContent = item.kind === "video" ? "VID" : item.kind === "image" ? "IMG" : "AUD";
    }
    node.querySelector("strong").textContent = item.file.name;
    node.querySelector("span").textContent = `${item.kind === "image" ? "图片" : item.kind === "video" ? "视频" : "音频"} · ${(item.file.size / 1024 / 1024).toFixed(1)} MB`;
    node.querySelector("button").addEventListener("click", () => {
      state.attachments = state.attachments.filter((entry) => entry.id !== item.id);
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
      if (item.sourceTaskId && item.sourceTaskId === state.editSourceTaskId) clearEditContext({ render: false });
      renderAttachments();
      renderParamSummary();
    });
    els.attachments.append(node);
  });
}
function renderConversations() {
  const activeItems = state.conversations.filter((item) => !item.archivedAt && (item.mode || "video") === state.mode).sort((a, b) => b.updatedAt - a.updatedAt);
  const archivedItems = state.conversations.filter((item) => item.archivedAt && (item.mode || "video") === state.mode).sort((a, b) => b.archivedAt - a.archivedAt);
  //: 同样加签名：生成期间 renderPreview 末尾会级联调用本函数，内容没变就不该重建
  //: （否则侧栏列表每 5 秒重建一次，看起来在闪）。
  const signature = [
    state.mode,
    state.activeConversationId || "",
    activeItems.map((item) => [item.id, conversationDisplayTitle(item), item.updatedAt].join("|")).join(";"),
    archivedItems.map((item) => [item.id, item.archivedAt].join("|")).join(";"),
  ].join("::");
  if (renderSignature.conversations === signature) return;
  renderSignature.conversations = signature;
  els.conversations.innerHTML = "";
  els.conversationCount.textContent = String(activeItems.length);
  activeItems.forEach((conversation) => els.conversations.append(conversationNode(conversation, false)));
  els.archivedCount.textContent = String(archivedItems.length);
  els.archivedList.innerHTML = "";
  archivedItems.forEach((conversation) => els.archivedList.append(conversationNode(conversation, true)));
  if (!activeItems.length) els.conversations.innerHTML = '<p class="fc-empty-conversations">暂无对话</p>';
}

function conversationNode(conversation, archived) {
  const template = archived ? $("#archived-template") : $("#conversation-template");
  const node = template.content.firstElementChild.cloneNode(true);
  node.dataset.id = conversation.id;
  node.classList.toggle("is-active", conversation.id === state.activeConversationId && !archived);
  node.querySelector("strong").textContent = conversationDisplayTitle(conversation);
  node.querySelector("small").textContent = `${conversation.mode === "video" ? "视频" : "图片"} · ${formatTime(conversation.updatedAt)}`;
  node.querySelector(".fc-conversation-symbol").innerHTML = conversation.mode === "video"
    ? `<svg viewBox="0 0 24 24"><path d="m16 13 5 3V8l-5 3Z"/><rect x="3" y="6" width="13" height="12" rx="2"/></svg>`
    : `<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg>`;
  if (archived) {
    node.querySelector(".fc-restore").addEventListener("click", (event) => { event.stopPropagation(); restoreConversation(conversation.id); });
    return node;
  }
  const open = () => openConversation(conversation.id);
  node.addEventListener("click", (event) => { if (!event.target.closest(".fc-conversation-actions, .fc-conversation-rename")) open(); });
  node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  const rename = node.querySelector(".fc-conversation-rename");
  node.querySelector('[data-action="rename"]').addEventListener("click", (event) => {
    event.stopPropagation();
    node.classList.add("is-renaming");
    rename.hidden = false;
    rename.value = conversationDisplayTitle(conversation);
    rename.focus();
    rename.select();
  });
  node.querySelector('[data-action="archive"]').addEventListener("click", (event) => {
    event.stopPropagation();
    archiveConversation(conversation.id);
  });
  const commit = () => {
    const title = rename.value.trim();
    if (title) { conversation.title = title.slice(0, 40); conversation.titleCustom = true; }
    conversation.updatedAt = Date.now();
    rename.hidden = true;
    node.classList.remove("is-renaming");
    saveStore();
    renderConversations();
  };
  rename.addEventListener("blur", commit);
  rename.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); rename.blur(); }
    if (event.key === "Escape") { rename.value = conversationDisplayTitle(conversation); rename.blur(); }
  });  return node;
}

function archiveConversation(id) {
  const conversation = state.conversations.find((item) => item.id === id);
  if (!conversation) return;
  saveStore();
  conversation.archivedAt = Date.now();
  conversation.updatedAt = Date.now();
  if (state.activeConversationId === id) {
    const next = state.conversations.find((item) => !item.archivedAt && (item.mode || "video") === conversation.mode);
    if (next) openConversation(next.id);
    else startNewConversation();
  } else {
    saveStore();
    renderConversations();
  }
}
function restoreConversation(id) {
  const conversation = state.conversations.find((item) => item.id === id);
  if (!conversation) return;
  conversation.archivedAt = null;
  conversation.updatedAt = Date.now();
  openConversation(id);
}function openConversation(id) {
  saveStore();
  const conversation = state.conversations.find((item) => item.id === id);
  if (!conversation) return;
  state.activeConversationId = id;
  state.activeTaskId = null;
  state.attachments = [];
  clearEditContext({ render: false });
  renderAttachments();
  restoreConversationDraft(conversation);
  const latest = latestSuccess() || latestTask();
  if (!conversation.prompt && latest?.prompt) conversation.prompt = latest.prompt;
  state.activeTaskId = latest?.id || null;
  els.prompt.value = conversation.prompt || "";
  autoResize();
  setHistoryStripOpen(false);
  setMode(conversation.mode || "video");
  renderConversations();
  renderPreview({ animateSwitch: true });
  closeSidebar();
  saveStore();
}function resetComposer() {
  state.attachments.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl));
  state.attachments = [];
  els.prompt.value = "";
  clearEditContext({ render: false });
  autoResize();
  renderAttachments();
  renderParamSummary();
}
function startNewConversation() { saveStore(); const conversation = newConversation(); state.conversations.unshift(conversation); state.activeConversationId = conversation.id; state.activeTaskId = null; restoreConversationDraft(conversation); resetComposer(); renderConversations(); renderPreview(); saveStore(); els.prompt.focus(); }function fileToDataUrl(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); }); }
async function buildPayload() {
  const model = currentModel(), params = state.params[state.mode], media = {};
  const capability = model.module;
  const images = state.attachments.filter((item) => item.kind === "image");
  const videos = state.attachments.filter((item) => item.kind === "video");
  const audios = state.attachments.filter((item) => item.kind === "audio");
  const mediaItem = async (attachment) => ({ data_url: await fileToDataUrl(attachment.file), filename: attachment.file.name });

  // 智能抠图：1 张图进、多张 PNG 出（最多 max_objects 个物体）。参数契约见模型机
  // 网关的 /api/image_segment/schema，本地免费，不需要任何密钥。
  // 注意提交体是 {values, files}（网关实现只认这个；schema 里 example 写的扁平 media
  // 是文档与实现不一致，以 static/image_segment/app.js 的实际提交为准）。
  if (capability === "image_segment") {
    if (images.length !== 1 || videos.length || audios.length) throw new Error("智能抠图需要且只需要 1 张图片");
    return {
      endpoint: `${localModuleBase("image_segment")}/jobs/json`, app: "local-ai", base: LOCAL_AI_MOUNT, jobPath: "/api/image_segment/jobs",
      payload: {
        values: {
          provider: "comfyui_local",
          max_objects: Number(params.max_objects),
          threshold: Number(params.threshold),
          filter_abstract: true,
          repeat_count: 1,
          concurrency: 1,
        },
        files: { image_1: await mediaItem(images[0]) },
      },
    };
  }
  // 视频超分：1 个视频进、1 个视频出。同样本地免费、无密钥。
  if (capability === "video_upscale") {
    if (videos.length !== 1) throw new Error("视频超分需要且只需要 1 个视频");
    // 网关 read_json_body 上限 200MB，而 base64 会把体积放大 4/3 —— 输入超过 120MB
    // 就会被网关判成 invalid content-length，这里提前拦下并给可操作的提示。
    if (videos[0].file.size > UPSCALE_MAX_INPUT_BYTES) throw new Error(`视频超分输入过大（${(videos[0].file.size / 1024 / 1024).toFixed(0)}MB，上限 120MB），请先剪短或压缩`);
    const target = UPSCALE_TARGETS.find((item) => item.value === params.upscale_target) || UPSCALE_TARGETS[1];
    // 档位按「短边」解释，输出框由网关用真实素材尺寸算（权威）。这里同时把前端
    // 自己探到的尺寸算成框传过去，只作为网关读不到素材时的兜底。
    const fallbackBox = videos[0].width
      ? fitBoxForShortSide({ width: videos[0].width, height: videos[0].height }, target.shortSide)
      : null;
    return {
      endpoint: `${localModuleBase("video_upscale")}/jobs/json`, app: "local-ai", base: LOCAL_AI_MOUNT, jobPath: "/api/video_upscale/jobs",
      payload: {
        values: {
          provider: "comfyui_local",
          upscale_model: model.defaults?.upscale_model || model.variants?.[0]?.id || "flashvsr_v1.1_full_3b",
          target_short_side: target.shortSide,
          ...(fallbackBox ? { target_width: fallbackBox.width, target_height: fallbackBox.height } : {}),
          crf: Number(params.crf),
          repeat_count: 1,
          concurrency: 1,
          // 快速模式：网关把这个值写进 FlashVSR 节点的 resize_factor，先把输入帧
          // 缩一半再重建。标准模式传 1.0（= 原尺寸，与网关默认一致）。
          resize_factor: params.speed_mode === "fast" ? 0.5 : 1.0,
        },
        files: { video_1: await mediaItem(videos[0]) },
      },
    };
  }

  let imageIndex = 1, videoIndex = 1, audioIndex = 1;
  for (const attachment of state.attachments) {
    const item = await mediaItem(attachment);
    if (state.mode === "image") {
      if (attachment.kind !== "image") throw new Error("图片模式只接受图片素材");
      media[`image_${imageIndex++}`] = item;
    } else if (attachment.kind === "image") media[`ref_image_${imageIndex++}`] = item;
    else if (attachment.kind === "video") media[`ref_video_${videoIndex++}`] = item;
    else media[`ref_audio_${audioIndex++}`] = item;
  }
  const editSourceTaskId = state.editSourceTaskId;
  //: 选了独立的「视频编辑」模型（model.editOnly）也算编辑态 —— 这时原视频由用户
  //: 自己上传，不要求它来自某条历史结果。
  const isEdit = Boolean(editSourceTaskId) || Boolean(model?.editOnly);
  const requestedChange = isEdit ? editInstruction() : "";
  if (isEdit && !requestedChange) throw new Error("请补充具体修改要求");
  if (state.mode === "video") {
    const local = model.provider === "comfyui_local";
    const hasEditVideo = isEdit && state.attachments.some(
      (item) => item.kind === "video" && (!editSourceTaskId || item.sourceTaskId === editSourceTaskId),
    );
    // 本地 H3 没有「在原视频上改」这条路径：它会把 duration=-1 兜成 12 秒、当成一次
    // 全新生成，白烧算力。这里硬拦，而不是悄悄退化成普通生成。
    if (isEdit && local) throw new Error("本地视频模型不支持继续修改原视频，请改用云端 Seedance，或直接重新生成");
    if (isEdit && !hasEditVideo) throw new Error("视频编辑需要一段原视频：请上传视频，或从历史结果上点「继续修改」");
    const prompt = hasEditVideo
      //: 外壳提示词要**要求看得见的变化**。旧写法「保持原视频主体、构图与运动连续性，
      //: 只执行以下修改：…」等于给了模型"什么都不改"的许可 —— 2026-09-21 实测：
      //: 产物与输入**逐帧几乎完全一致**（241 帧/10.04s/720×1280 全同，同帧像素差仅 3.9~4.3/255
      //: ≈ 重编码噪声），用户反馈「编辑功能感觉没变化」。
      ? `编辑这段参考视频：必须按下面的要求改变画面，改完后要能明显看出与原视频不同（除非要求本身写了保持原样）；未被要求改动的部分保持主体、构图与运动连续性。修改要求：${requestedChange}`
      : els.prompt.value.trim();
    return {
      endpoint: "/seedance/api/jobs/json", app: "seedance", base: "/seedance", jobPath: "/api/jobs",
      payload: {
        // provider 由所选模型决定：本地走 comfyui_local（后端会把 base_url 锁到本地
        // 网关并清空密钥），云端走 volcengine（后端注入公司 key，浏览器永远拿不到）。
        provider: model.provider,
        //: 发真实模型 id：编辑入口的 `id` 是界面伪 id，真名在 `modelId`（2026-09-21 修）。
        model: model.modelId || model.id,
        prompt,
        task_mode: hasEditVideo ? "edit" : "reference",
        duration: hasEditVideo ? -1 : Number(params.duration),
        ratio: hasEditVideo ? "adaptive" : params.ratio,
        resolution: params.resolution,
        repeat_count: Number(params.repeat_count),
        concurrency: 1,
        vary_seed: true,
        //: 生成音频（Seedance 原生带声）。以前前端**从不传这个字段**，后端
        //: `parse_bool(get_field(form,"generate_audio"))` 拿到空值 → False →
        //: 生成的视频一律没有声音（用户 2026-09-21：「为什么生成的视频都没有声音」）。
        generate_audio: "1",
        media,
      },
    };
  }
  const prompt = isEdit
    ? `\u57fa\u4e8e\u53c2\u8003\u56fe\u7247\u6267\u884c\u4fee\u6539\u3002\u4fdd\u6301\u672a\u8981\u6c42\u6539\u53d8\u7684\u5185\u5bb9\uff0c\u53ea\u6267\u884c\u4ee5\u4e0b\u4fee\u6539\uff1a${requestedChange}`
    : els.prompt.value.trim();
  return { endpoint: "/nano-banana/api/jobs/json", app: "nano-banana", base: "/nano-banana", jobPath: "/api/jobs", payload: { provider: model.provider, model: model.id, mode: state.attachments.length ? "img2img" : "text2img", prompt, aspect_ratio: params.aspect_ratio, image_size: params.image_size, repeat_count: Number(params.repeat_count), concurrency: 1, vary_seed: true, media } };
}async function generate() {
  if (state.busy) return;
  const prompt = els.prompt.value.trim(), model = currentModel();
  const capability = model?.module;
  // 抠图 / 超分不需要提示词：抠图由模型自己反推画面描述，超分是对既有画面做重建。
  if (!prompt && !capability) { toast("请先描述你想创作的内容", "error"); els.prompt.focus(); return; }
  if (state.editSourceTaskId && !canRefine(model)) { toast("抠图 / 超分 / 本地视频不支持继续修改", "error"); return; }
  if (state.editSourceTaskId && !editInstruction()) { toast("请补充具体修改要求", "error"); els.prompt.focus(); return; }
  if (!state.editSourceTaskId && !model?.editOnly && EDIT_MARKER_RE.test(els.prompt.value)) { toast("已退出编辑态，请重新点「继续修改」再提交", "error"); els.prompt.focus(); return; }
  if (!model?.available) { toast("当前模型尚未接入执行能力", "error"); return; }
  const route = taskRoute(model, state.mode);
  const taskPrompt = prompt || (capability === "image_segment" ? "智能抠图" : capability === "video_upscale" ? "视频超分" : "");
  const conversation = activeConversation();
  const task = { id: uid(), app: route.app, base: route.base, jobPath: route.jobPath, module: capability || null, mode: state.mode, prompt: taskPrompt, model: model.label, modelId: model.id, provider: model.provider, params: structuredClone(state.params[state.mode]), attachments: state.attachments.map((item) => item.file.name), status: "running", progress: "正在提交任务", conversationId: conversation.id, editSourceTaskId: state.editSourceTaskId || null, createdAt: Date.now(), updatedAt: Date.now() };
  conversation.tasks.push(task); conversation.updatedAt = task.createdAt;
  state.activeTaskId = task.id; renderConversations(); renderPreview(); setBusy(true); saveStore();
  try {
    const { endpoint, payload, app, base, jobPath } = await buildPayload();
    task.app = app; task.base = base; task.jobPath = jobPath;
    const preflight = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json", "X-Workspace-Id": conversation.id }, body: JSON.stringify({ ...payload, dry_run: true }) });
    const preflightBody = await preflight.json().catch(() => ({}));
    if (!preflight.ok || preflightBody.ok === false) throw new Error(preflightBody.error || preflightBody.detail || `生成参数预检失败 (${preflight.status})`);
    const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json", "X-Workspace-Id": conversation.id }, body: JSON.stringify(payload) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || body.ok === false) throw new Error(body.error || body.detail || `提交失败 (${response.status})`);
    task.jobId = body.job_id; task.progress = app === "local-ai" ? "任务已进入本地队列" : "任务已进入云端队列"; saveStore();
    pollTask(task).catch((error) => { task.status = "failed"; task.error = error.message; task.updatedAt = Date.now(); saveStore(); renderPreview(); renderConversations(); });
  } catch (error) {
    task.status = "failed"; task.error = error.message; task.updatedAt = Date.now(); saveStore(); renderConversations(); renderPreview(); toast(error.message, "error");
  } finally { setBusy(false); }
}
// 任务落到哪个后端：抠图 / 超分在模型机的本地网关（/local-ai），其余按模式走 seedance
// / nano-banana。base 是「拼结果下载 URL」的前缀，jobPath 是「拼任务状态 URL」的路径。
function taskRoute(model, mode) {
  if (model?.module) return { app: "local-ai", base: LOCAL_AI_MOUNT, jobPath: `/api/${model.module}/jobs` };
  return mode === "video"
    ? { app: "seedance", base: "/seedance", jobPath: "/api/jobs" }
    : { app: "nano-banana", base: "/nano-banana", jobPath: "/api/jobs" };
}
function absoluteMediaUrl(base, url) {
  const value = String(url || "");
  return value.startsWith("http") || value.startsWith("data:") || value.startsWith("blob:") ? value : `${base}${value}`;
}
function formatElapsed(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60), seconds = total % 60;
  if (minutes < 60) return `${minutes}:${String(seconds).padStart(2, "0")}`;
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}
// 本地网关只回一个 status、不报 done/total，于是一个 27 分钟的超分任务在界面上
// 跟「卡死」长得一模一样（实测用户就是这么反馈的）。至少把已用时长显示出来，
// 让用户看得出它在动；云端任务本来就有 done/total，行为不变。
function progressText(task, data, status) {
  if (data?.done && data?.total) return `${data.done}/${data.total}`;
  const started = Number(task.createdAt || Date.now());
  const elapsed = formatElapsed(Date.now() - started);
  return task.app === "local-ai" ? `本地计算中 · 已用 ${elapsed}` : `${status} · 已用 ${elapsed}`;
}
async function pollTask(task) {
  const base = task.base || (task.mode === "video" ? "/seedance" : "/nano-banana");
  const jobPath = task.jobPath || "/api/jobs";
  while (RUNNING.has(task.status)) {
    await new Promise((resolve) => setTimeout(resolve, 2500));
    // 一次网络抖动 / 502 不该把一个跑了半小时的任务判死。实测：本地超分收尾时整台
    // 机器被压满，浏览器这一发轮询超时，任务就被标成 failed —— 后端其实成功了，
    // 结果永远认领不回来。所以这里连续重试几次再放弃。
    let response = null, lastError = null;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      try {
        const candidate = await fetch(`${base}${jobPath}/${encodeURIComponent(task.jobId)}`, { headers: { "X-Workspace-Id": task.conversationId || activeConversation().id } });
        if (candidate.ok) { response = candidate; break; }
        lastError = new Error(`任务状态读取失败 (${candidate.status})`);
      } catch (error) {
        lastError = new Error(`任务状态读取失败（${error?.message || "网络错误"}）`);
      }
      if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, 2000 * (attempt + 1)));
    }
    if (!response) throw lastError || new Error("任务状态读取失败");
    const data = await response.json(), status = String(data.status || "running").toLowerCase();
    task.progress = progressText(task, data, status); task.updatedAt = Date.now();
    const result = (data.results || []).find((item) => item.download_url || item.url);
    if (result) {
      task.resultUrl = absoluteMediaUrl(base, result.download_url || result.url);
      // 抠图一次出多张 PNG：download_urls 是全部结果，第一张仍放在 resultUrl 供预览/历史用。
      const urls = (Array.isArray(result.download_urls) ? result.download_urls : []).map((url) => absoluteMediaUrl(base, url)).filter(Boolean);
      task.resultUrls = urls.length ? urls : [task.resultUrl];
      task.resultName = result.filename || `${task.mode === "video" ? "video" : "image"}-${task.id.slice(0, 8)}`;
    }
    if (TERMINAL_SUCCESS.has(status) || (status === "partial" && task.resultUrl)) {
      task.status = "success"; task.updatedAt = Date.now();
      if (task.editSourceTaskId && task.editSourceTaskId === state.editSourceTaskId) clearEditContext({ render: false });
      saveStore(); renderPreview(); renderConversations(); renderParamSummary(); renderHistory(); return;
    }
    // 本地网关把失败原因放在 results[0].error（顶层只有 status:"failed"），
    // 只读 data.error 的话用户只会看到一句没用的 "failed"。
    if (TERMINAL_FAILURE.has(status)) {
      const firstError = (data.errors || [])[0];
      const resultError = (data.results || []).find((item) => item?.error)?.error;
      const message = (typeof firstError === "string" ? firstError : firstError?.message)
        || (typeof resultError === "string" ? resultError : resultError?.message)
        || (typeof data.error === "string" ? data.error : "")
        || task.progress || "生成失败";
      throw new Error(message);
    }
    saveStore(); renderPreview();
  }
}
function renderPreview({ animateSwitch = false } = {}) {
  const task = activeTask();
  if (!task) { els.previewPanel.hidden = true; els.previewPanel.classList.remove("is-switching", "is-editing"); els.stage.classList.remove("is-preview"); els.download.disabled = true; return; }
  els.previewPanel.hidden = false; els.stage.classList.add("is-preview");
  els.previewPanel.classList.toggle("is-editing", Boolean(state.editSourceTaskId && task.id === state.editSourceTaskId));
  if (animateSwitch) {
    els.previewPanel.classList.remove("is-switching");
    void els.previewPanel.offsetWidth;
    requestAnimationFrame(() => els.previewPanel.classList.add("is-switching"));
  }
  if (task.resultUrl) {
    els.mediaLoading.hidden = true; els.previewMedia.innerHTML = "";
    const urls = Array.isArray(task.resultUrls) && task.resultUrls.length > 1 ? task.resultUrls : [task.resultUrl];
    if (urls.length > 1) {
      // 智能抠图一次出多张（最多 max_objects 个物体）。网格里每张都能单独下载，
      // 底部「下载结果」会把这批一起下载。
      const grid = document.createElement("div"); grid.className = "fc-result-grid";
      urls.forEach((url, index) => {
        const cell = document.createElement("a"); cell.className = "fc-result-cell";
        //: 这里也用 blob 下载（同上面的原因）：直接 `<a href download>` 在自签 HTTPS 下
        //: 会被下载管理器拒掉，报"网络问题"。
        cell.href = url; cell.rel = "noopener";
        cell.download = `${task.resultName || "result"}-${index + 1}`;
        cell.title = `下载第 ${index + 1} 张`;
        cell.addEventListener("click", (event) => {
          event.preventDefault();
          blobDownload(url, cell.download).catch((error) => toast(error.message || "下载失败", "error"));
        });
        const image = document.createElement("img"); image.src = url; image.alt = `${task.prompt} ${index + 1}`; image.loading = "lazy"; image.decoding = "async";
        cell.append(image); grid.append(cell);
      });
      els.previewMedia.append(grid); els.download.disabled = false;
    } else {
      const media = document.createElement(task.mode === "video" ? "video" : "img"); bindMediaFallback(media, task); media.src = task.resultUrl;
      if (task.mode === "video") { media.controls = true; media.playsInline = true; media.preload = "metadata"; } else { media.alt = task.prompt; media.loading = "eager"; media.decoding = "async"; }
      els.previewMedia.append(media); els.download.disabled = false;
    }
  } else {
    //: 那三个跳动的点**只在真正跑的时候**显示。以前无论什么状态都 `hidden = false`，
    //: 生成失败后它们还在动，看着像"还能继续生成"（用户 2026-09-21）。
    const isRunning = RUNNING.has(String(task.status || "").toLowerCase());
    els.mediaLoading.hidden = !isRunning;
    const title = task.status === "failed" ? "生成失败" : task.status === "success" ? "正在读取结果" : "正在生成";
    //: 取消入口**已经移到发送键**（有任务在跑时它变成方形暂停键，用户 2026-09-21 要求），
    //: 预览区这里只留状态文字，不再放按钮。
    //: 进度那行单独给个 span：秒级刷新只改它的文本，不重建整个预览（否则视频会被重载）。
    els.previewMedia.innerHTML = `<div style="text-align:center;color:var(--muted);font-size:12px;line-height:1.7"><strong style="display:block;color:var(--text);font-size:15px">${title}</strong><span class="fc-progress-text">${escapeHtml(task.error || task.progress || "请稍候…")}</span></div>`;
    els.download.disabled = true;
  }
  renderHistory(); renderConversations();
}
//: 取消正在跑的任务：调子应用的 `POST /api/jobs/{id}/cancel`。
//:
//: 用户 2026-09-21：「正在生成的任务能不能取消」—— 接口早就有，前端一直没接。
async function cancelActiveJob(task) {
  if (!task?.jobId) return;
  const base = task.base || (task.app === "volcengine-portrait"
    ? "/volcengine-portrait"
    : task.mode === "video" ? "/seedance" : "/nano-banana");
  const jobPath = task.jobPath || "/api/jobs";
  try {
    const response = await fetch(`${base}${jobPath}/${encodeURIComponent(task.jobId)}/cancel`, {
      method: "POST",
      headers: { "X-Workspace-Id": task.conversationId || activeConversation()?.id || "" },
    });
    if (!response.ok) throw new Error(`取消失败 (${response.status})`);
    task.status = "cancelled";
    task.progress = "已取消";
    task.updatedAt = Date.now();
    saveStore();
    renderPreview();
    renderConversations();
    renderHistory();
    renderParamSummary();   // 发送键要变回「生成视频」
    toast("已取消这次生成");
  } catch (error) {
    toast(error.message || "取消失败", "error");
  }
}

//: 自签 HTTPS 下 `<a href download>` 直接 click 会失败：浏览器**下载管理器**把它当成
//: 一次独立请求、重新校验证书，而它对自签证书的容忍度比页面上下文严格得多 →
//: Chrome 报「检查互联网连接 / 网络问题」（本机文档里记过的老坑，Portal 主壳、
//: seedance、nano-banana 都改过了，自由创作漏了）。
//: 正确做法：fetch → blob → objectURL → a[download].click()，blob: 协议绕过下载管理器。
async function blobDownload(url, filename) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`下载失败 (${response.status})`);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename || "download";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 5000);
}

function setHistoryStripOpen(open) {
  const visible = Boolean(open);
  els.historyStrip.classList.toggle("is-open", visible);
  els.historyStrip.inert = !visible;
  els.historyStrip.setAttribute("aria-hidden", String(!visible));
  els.historyToggle.setAttribute("aria-expanded", String(visible));
  // 展开时才真正给缩略图挂 src：收起状态下创建 <video> 也一样会发请求，
  // 历史条一多就会把页面和 Portal 代理一起堵死。
  if (visible) renderHistory();
}

function switchActiveTask(id) {
  if (state.activeTaskId === id) return;
  state.activeTaskId = id;
  clearEditContext({ render: false });
  renderParamSummary();
  renderPreview({ animateSwitch: true });
}
// 历史条一次最多渲染这么多张缩略图。
// 真实故障（2026-09-15）：历史一次导入最多 200 条，而 renderHistory 一条不落地
// 给每条创建 <video preload="metadata"> 并挂真实 src。实测该用户有 185 条可下载
// 视频、能查到体积的 103 个文件合计 987MB（平均 9.6MB、最大 18.4MB）。一打开
// 自由创作就发起上百个大文件请求，页面与 Portal 代理双双卡死 —— 表现为“打不开”。
const HISTORY_RENDER_LIMIT = 12;

function renderHistory() {
  //: 历史条也要包含**正在生成**的那条（用户 2026-09-21：「查看历史记录里面没有正在生成
  //: 的任务」）。原来只 filter(resultUrl)，于是"生成中"的任务在历史里完全看不到。
  const successes = (activeConversation()?.tasks || []).filter(
    (task) => task.resultUrl || RUNNING.has(String(task.status || "").toLowerCase()),
  );
  const shown = successes.slice(-HISTORY_RENDER_LIMIT);
  //: 渲染签名：内容没变就**不重建 DOM**。
  //:
  //: 用户 2026-09-21：「正在生成的时候历史记录下面一直在跳」。原因是生成期间
  //: reconcileRunningTasks 每 5 秒一次，只要有任何进度变化就整块重建：`<video>`
  //: 被重新创建（重新拉流、闪一下），入场动画 `fc-strip` 也每 5 秒重播一次。
  const signature = shown
    .map((task) => [task.id, task.status, task.resultUrl, task.id === state.activeTaskId].join("|"))
    .join(";");
  if (renderSignature.history === signature && renderSignature.historyOpen === stripOpenNow()) return;
  const known = renderSignature.historyIds || new Set();
  const nextIds = new Set(shown.map((task) => task.id));
  renderSignature.history = signature;
  renderSignature.historyIds = nextIds;
  renderSignature.historyOpen = stripOpenNow();
  const stripOpen = stripOpenNow();
  els.historyStrip.innerHTML = "";
  shown.forEach((task, index) => {
    const node = $("#history-template").content.firstElementChild.cloneNode(true);
    node.dataset.id = task.id;
    // 只有**新出现**的那一条才播入场动画；老条目重建时保持静止（否则看起来一直在跳）。
    if (!known.has(task.id)) node.style.animationDelay = `${Math.min(index * 35, 210)}ms`;
    else node.classList.add("fc-no-enter");
    node.classList.toggle("is-active", task.id === state.activeTaskId);
    const thumb = node.querySelector(".fc-history-thumb");
    //: 正在生成的那条没有 resultUrl，缩略图位置给一个"生成中"标记（不挂 video/img，
    //: 否则会拿 undefined 当 src 发请求）。
    if (!task.resultUrl) {
      node.classList.add("is-pending");
      thumb.textContent = "生成中";
    } else if (task.mode === "video") {
      const video = document.createElement("video");
      video.muted = true;
      // 收起时先不挂 src；展开后由 setHistoryStripOpen 触发一次重渲染再加载
      video.preload = stripOpen ? "metadata" : "none";
      if (stripOpen) {
        bindMediaFallback(video, task);
        video.src = task.resultUrl;
      }
      thumb.append(video);
    } else {
      const image = document.createElement("img");
      image.alt = "";
      if (stripOpen) {
        bindMediaFallback(image, task);
        image.src = task.resultUrl;
      }
      thumb.append(image);
    }
    node.querySelector("strong").textContent = task.prompt.slice(0, 28);
    node.querySelector("small").textContent = formatTime(task.updatedAt);
    // 历史卡片只负责"切到这一版"。**不在这里放「继续修改」**（用户 2026-09-21 要求去掉）；
    // 要改某一版：先点这条历史选中它，再点工具条的「继续修改」；或用视频模型里的
    // 「Seedance 2.5 · 视频编辑」自己上传原视频。
    node.addEventListener("click", () => switchActiveTask(task.id));
    els.historyStrip.append(node);
  });
  els.historyToggle.hidden = successes.length < 2;
  if (successes.length < 2) setHistoryStripOpen(false);
}
function openPopover(id) {
  closeAllPopovers();
  const popover = document.getElementById(id);
  popover.hidden = false;
  const anchor = id === "model-popover" ? els.modelBubble : els.paramsBubble;
  requestAnimationFrame(() => positionPopover(popover, anchor));
}
function closePopover(id) { document.getElementById(id).hidden = true; }
function closeAllPopovers() { $$(".fc-popover").forEach((popover) => { popover.hidden = true; }); }
function positionPopover(popover, anchor) {
  if (window.innerWidth <= 700 || !anchor) return;
  const rect = anchor.getBoundingClientRect();
  const width = popover.offsetWidth || 420;
  const height = popover.offsetHeight || 420;
  let left = rect.left - width + rect.width;
  let top = rect.top - height - 10;
  left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
  if (top < 66) top = Math.min(window.innerHeight - height - 12, rect.bottom + 10);
  popover.style.left = `${left}px`;
  popover.style.top = `${top}px`;
}
function syncSidebarAccessibility() {
  const expanded = window.innerWidth <= 1080
    ? els.shell.classList.contains("sidebar-open")
    : !els.shell.classList.contains("sidebar-collapsed");
  const label = expanded ? "\u6536\u8d77\u4fa7\u680f" : "\u5c55\u5f00\u4fa7\u680f";
  els.menu.setAttribute("aria-expanded", String(expanded));
  els.menu.setAttribute("aria-label", label);
  els.menu.setAttribute("title", label);
}
function openSidebar() {
  els.shell.classList.add("sidebar-open");
  syncSidebarAccessibility();
}
function closeSidebar() {
  els.shell.classList.remove("sidebar-open");
  syncSidebarAccessibility();
}
function toggleSidebar() {
  if (window.innerWidth <= 1080) {
    els.shell.classList.remove("sidebar-collapsed");
    els.shell.classList.toggle("sidebar-open");
    syncSidebarAccessibility();
    return;
  }
  els.shell.classList.remove("sidebar-open");
  els.shell.classList.toggle("sidebar-collapsed");
  localStorage.setItem(`${STORAGE_KEY}.sidebar`, els.shell.classList.contains("sidebar-collapsed") ? "1" : "0");
  syncSidebarAccessibility();
}
function setupDraggableBubbles() {
  $$(".fc-bubble").forEach((bubble) => {
    let pointerId = null, startX = 0, startY = 0, moved = false;
    bubble.addEventListener("pointerdown", (event) => { pointerId = event.pointerId; startX = event.clientX; startY = event.clientY; moved = false; bubble.setPointerCapture(pointerId); bubble.classList.add("is-dragging"); });
    bubble.addEventListener("pointermove", (event) => {
      if (pointerId !== event.pointerId) return;
      const dx = event.clientX - startX, dy = event.clientY - startY; moved = Math.hypot(dx, dy) > 5;
      bubble.style.setProperty("--tx", `${Math.max(-80, Math.min(80, dx))}px`); bubble.style.setProperty("--ty", `${Math.max(-60, Math.min(60, dy))}px`);
    });
    const release = (event) => {
      if (pointerId !== event.pointerId) return;
      bubble.classList.remove("is-dragging"); bubble.style.setProperty("--tx", "0px"); bubble.style.setProperty("--ty", "0px");
      if (moved) bubble.dataset.suppressClick = "1"; pointerId = null;
    };
    bubble.addEventListener("pointerup", release); bubble.addEventListener("pointercancel", release);
    bubble.addEventListener("click", (event) => { if (bubble.dataset.suppressClick === "1") { delete bubble.dataset.suppressClick; event.preventDefault(); event.stopImmediatePropagation(); } }, true);
  });
}
function bindEvents() {
  els.menu.addEventListener("click", toggleSidebar); els.scrim.addEventListener("click", closeSidebar);
  els.archiveToggle.addEventListener("click", () => { els.archivedList.hidden = !els.archivedList.hidden; els.archiveToggle.setAttribute("aria-expanded", String(!els.archivedList.hidden)); });
  els.newChat.addEventListener("click", startNewConversation);
  els.download.addEventListener("click", async () => {
    const task = activeTask(); if (!task?.resultUrl) return;
    const urls = Array.isArray(task.resultUrls) && task.resultUrls.length ? task.resultUrls : [task.resultUrl];
    for (const [index, url] of urls.entries()) {
      const name = urls.length > 1 ? `${task.resultName || "result"}-${index + 1}` : (task.resultName || "creation");
      try {
        await blobDownload(url, name);
      } catch (error) {
        toast(error.message || "下载失败", "error");
        return;
      }
      if (urls.length > 1) await new Promise((resolve) => setTimeout(resolve, 400));
    }
  });
  els.historyToggle.addEventListener("click", () => setHistoryStripOpen(!els.historyStrip.classList.contains("is-open")));
  //: 历史条横向滚动：Windows 上滚轮默认不驱动横向容器，用户只能去抓那条细细的
  //: 滚动条（用户 2026-09-21：「历史记录没有滑动条，无法切到后面的视频」）。
  //: 这里把纵向滚轮转成横向滚动，滚轮/触控板都能直接翻到后面的版本。
  els.historyStrip.addEventListener("wheel", (event) => {
    if (!event.deltaY) return;
    if (els.historyStrip.scrollWidth <= els.historyStrip.clientWidth) return;
    event.preventDefault();
    els.historyStrip.scrollLeft += event.deltaY;
  }, { passive: false });
  els.refine.addEventListener("click", async () => {
    const task = refineTarget();
    if (!task) return;
    const originalPrompt = String(task.prompt || "");
    state.activeTaskId = task.id;
    // 整段视频要读进内存才能当编辑输入（几十~上百 MB），以前这里**没有任何反馈**，
    // 用户看到的就是"界面卡住"（2026-09-21）。现在：按钮变文案 + 大文件先提示。
    const previousLabel = els.refine.textContent;
    els.refine.disabled = true;
    els.refine.textContent = task.mode === "video" ? "载入原视频…" : "载入原图…";
    try {
      const response = await fetch(task.resultUrl);
      if (!response.ok) throw new Error(`读取结果失败 (${response.status})`);
      const declared = Number(response.headers.get("content-length") || 0);
      if (declared > EDIT_INPUT_WARN_BYTES) {
        const mb = Math.round(declared / 1048576);
        toast(`原视频约 ${mb}MB，载入需要几秒，请稍候`, "info");
      }
      const blob = await response.blob();
      const name = task.resultName || (task.mode === "video" ? "previous.mp4" : "previous.png");
      const file = new File([blob], name, { type: blob.type || (task.mode === "video" ? "video/mp4" : "image/png") });
      state.editSourceTaskId = task.id;
      state.editBasePrompt = originalPrompt;
      state.attachments = [{
        id: uid(),
        file,
        kind: task.mode === "video" ? "video" : "image",
        // 编辑态带进来的原视频也要出缩略图（同上：以前只有图片建预览）。
        previewUrl: task.mode === "image" || task.mode === "video" ? URL.createObjectURL(file) : "",
        sourceTaskId: task.id,
      }];
      els.prompt.value = `${originalPrompt}${originalPrompt ? "\n\n" : ""}\u4fee\u6539\u8981\u6c42\uff1a`;
      renderAttachments();
      renderPreview();
      renderParamSummary();
      autoResize();
      toast(task.mode === "video" ? "\u5df2\u8fdb\u5165\u89c6\u9891\u7f16\u8f91\u6a21\u5f0f\uff0c\u8bf7\u8865\u5145\u5177\u4f53\u4fee\u6539\u8981\u6c42" : "\u5df2\u8fdb\u5165\u56fe\u7247\u4fee\u6539\u6a21\u5f0f\uff0c\u8bf7\u8865\u5145\u5177\u4f53\u4fee\u6539\u8981\u6c42");
    } catch {
      state.attachments = [];
      clearEditContext({ render: false });
      els.prompt.value = originalPrompt;
      renderAttachments();
      renderParamSummary();
      autoResize();
      toast("\u65e0\u6cd5\u8bfb\u53d6\u5f53\u524d\u7ed3\u679c\uff0c\u672a\u8fdb\u5165\u4fee\u6539\u6a21\u5f0f", "error");
    }
    els.refine.disabled = false;
    els.refine.textContent = previousLabel;
    els.prompt.focus();
    els.prompt.setSelectionRange(els.prompt.value.length, els.prompt.value.length);
    els.prompt.closest(".fc-composer").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  els.prompt.addEventListener("input", () => {
    const conversation = activeConversation();
    if (conversation) conversation.prompt = els.prompt.value;
    renderConversations();
    autoResize();
    scheduleDraftSave();
  });
  els.prompt.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); generate(); } });
  //: 有任务在跑时这个键是"暂停"：点它取消当前任务，而不是再提交一个（用户 2026-09-21）。
  els.generate.addEventListener("click", () => {
    const running = runningTask();
    if (running) { cancelActiveJob(running); return; }
    generate();
  });
  els.upload.addEventListener("click", () => els.fileInput.click());
  els.fileInput.addEventListener("change", () => { addFiles(els.fileInput.files); els.fileInput.value = ""; });
  els.modeSwitch.querySelectorAll(".fc-mode-option").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  els.modelModeSwitch.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  $("#model-bubble").addEventListener("click", () => openPopover("model-popover")); $("#params-bubble").addEventListener("click", () => openPopover("params-popover"));
  $$("[data-close]").forEach((button) => button.addEventListener("click", () => closePopover(button.dataset.close)));
  document.addEventListener("click", (event) => { if (!event.target.closest(".fc-popover, #model-bubble, #params-bubble, #upload-bubble, #generate")) closeAllPopovers(); });
  const composer = els.prompt.closest(".fc-composer");
  //: 拖拽高亮用**计数器**，不能用 add/remove 直接切：`dragover` 每几毫秒触发一次，
  //: `dragleave` 只要拖过任何一个子元素就会触发 —— 直接 add/remove 会反复横跳，
  //: 表现就是"素材拖进来的时候页面一直闪"（用户 2026-09-21）。
  let dragDepth = 0;
  const dropOverlay = $("#drop-overlay");
  ["dragenter", "dragover"].forEach((name) => composer.addEventListener(name, (event) => {
    event.preventDefault();
    if (name === "dragenter") dragDepth += 1;
    dropOverlay.hidden = false;
  }));
  ["dragleave", "drop"].forEach((name) => composer.addEventListener(name, (event) => {
    event.preventDefault();
    if (name === "dragleave") {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth > 0) return;   // 还在内部移动，不收高亮
    } else {
      dragDepth = 0;
    }
    dropOverlay.hidden = true;
  }));
  composer.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  els.previewPanel.addEventListener("animationend", (event) => { if (event.animationName === "fc-history-switch") els.previewPanel.classList.remove("is-switching"); });
  //: 秒级刷新「已用时长」。以前这行只在 5 秒一次的 reconcileRunningTasks 里重算，
  //: 看起来就是"每 5 秒才跳一次"（用户 2026-09-21）。这里**只改那行文本**，不重建
  //: 预览 —— 重建会让 <video> 重新拉流。云端任务有 done/total 时不碰它。
  window.setInterval(() => {
    if (document.visibilityState !== "visible") return;
    const span = els.previewMedia ? els.previewMedia.querySelector(".fc-progress-text") : null;
    if (!span || !span.textContent.includes("已用")) return;
    const task = activeTask();
    if (!task || !RUNNING.has(String(task.status || "").toLowerCase())) return;
    const next = progressText(task, null, String(task.status || "").toLowerCase());
    if (span.textContent !== next) span.textContent = next;
  }, 1000);
  window.addEventListener("resize", () => { closeAllPopovers(); syncSidebarAccessibility(); });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); startNewConversation(); }
    if (event.key === "Escape") { closeAllPopovers(); closeSidebar(); }
  });
}
function init() {
  loadStore();
  const sidebarCollapsed = localStorage.getItem(`${STORAGE_KEY}.sidebar`) === "1";
  if (sidebarCollapsed) els.shell.classList.add("sidebar-collapsed");
  syncSidebarAccessibility();
  restoreConversationDraft(activeConversation());
  bindEvents(); setupDraggableBubbles(); setMode(activeConversation()?.mode || "video");
  renderConversations(); renderAttachments(); renderPreview(); renderParamSummary(); autoResize(); loadModels();
  syncLiveState({ selectIfEmpty: true });
  window.setInterval(() => { if (document.visibilityState === "visible") syncLiveState(); }, HISTORY_SYNC_INTERVAL);
  window.setInterval(() => { if (document.visibilityState === "visible") reconcileRunningTasks(); }, 5000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") syncLiveState(); });
}
init();
