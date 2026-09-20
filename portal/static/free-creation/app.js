"use strict";

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
const UPSCALE_TARGETS = [
  { label: "1080p", value: "1080p", width: 1920, height: 1080 },
  { label: "2K", value: "2K", width: 2560, height: 1440 },
  { label: "4K", value: "4K", width: 3840, height: 2160 },
];
const SEGMENT_THRESHOLDS = [
  { label: "宽松 0.2", value: 0.2 },
  { label: "标准 0.3", value: 0.3 },
  { label: "严格 0.4", value: 0.4 },
  { label: "很严格 0.5", value: 0.5 },
];
// 网关 read_json_body 上限 200MB，base64 放大 4/3 → 输入视频卡在 120MB。
const UPSCALE_MAX_INPUT_BYTES = 120 * 1024 * 1024;
const HISTORY_SOURCES = {
  seedance: { mode: "video", mount: "/seedance" },
  "nano-banana": { mode: "image", mount: "/nano-banana" },
  "volcengine-portrait": { mode: "video", mount: "/volcengine-portrait" },
  dreamina: { mode: null, mount: "/dreamina" },
};
const HISTORY_SYNC_INTERVAL = 15000;
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
    video: { ratio: "16:9", resolution: "720p", duration: 8, repeat_count: 1, upscale_target: "1080p", crf: 18 },
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
  const conversations = [...regular.slice(0, 80), ...imported];
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
}
function restoreConversationDraft(conversation) {
  if (!conversation) return;
  const draft = conversation.params && typeof conversation.params === "object" ? conversation.params : {};
  state.params.video = { ...state.params.video, ...(draft.video || {}) };
  state.params.image = { ...state.params.image, ...(draft.image || {}) };
  els.prompt.value = conversation.prompt || "";
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
function setBusy(value) { state.busy = value; els.generate.classList.toggle("is-busy", value); els.generate.disabled = value; }
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
    state.attachments = [];
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
  state.models = { video, image };
  state.selected.video = video.find((model) => model.available && model.id === "doubao-seedance-2-5-260628") || video.find((model) => model.available) || video[0] || null;
  state.selected.image = image.find((model) => model.available && model.local) || image.find((model) => model.available) || image[0] || null;
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
      upscale_target: ["1080p", "2K", "4K"].includes(current.upscale_target) ? current.upscale_target : "1080p",
      crf: clampNumber(current.crf, 12, 28, 18),
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
    // 超分是「换任务」不是「换模型」：比例/分辨率/时长都不适用，只有目标尺寸和画质。
    if (model.module === "video_upscale") {
      params.upscale_target = clampOption(params.upscale_target, UPSCALE_TARGETS.map((item) => item.value), "1080p");
      params.crf = clampNumber(params.crf, 12, 28, Number(d.crf) || 18);
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
        paramNumberField("画质 CRF", "crf", params.crf, 12, 28, "", "数值越小越清晰、文件越大（默认 18）"),
      );
      // 实测：15 秒 / 720p 竖屏素材要切 18 块、约 27 分钟。先说清楚，别让用户
      // 盯着一个不动的 "running" 以为卡死了。
      const hint = document.createElement("p");
      hint.className = "fc-param-hint";
      hint.textContent = "本地超分很慢：按片段逐块重算，约 15 秒的 720p 素材要 25~30 分钟。提交后请保持页面打开，界面上会显示已用时长。";
      els.paramForm.append(hint);
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
  const isEdit = Boolean(state.editSourceTaskId) && canRefine(model);
  const capability = model?.module;
  if (els.modelSummary) els.modelSummary.textContent = model?.label || "\u9009\u62e9\u6a21\u578b";
  const verb = capability === "image_segment" ? "开始抠图" : capability === "video_upscale" ? "开始超分" : state.mode === "video" ? "\u751f\u6210\u89c6\u9891" : "\u751f\u6210\u56fe\u7247";
  if (els.generateLabel) els.generateLabel.textContent = isEdit ? "\u63d0\u4ea4\u4fee\u6539" : verb;
  els.generate.setAttribute("aria-label", isEdit ? "\u63d0\u4ea4\u4fee\u6539" : verb);
  els.generate.setAttribute("title", isEdit ? "\u63d0\u4ea4\u4fee\u6539" : verb);
  // 抠图 / 超分没有「在原结果上继续改」的语义，禁用而不是隐藏（避免按钮位置跳动）。
  const refineAllowed = canRefine(model);
  els.refine.disabled = !refineAllowed;
  els.refine.setAttribute("aria-pressed", String(isEdit));
  els.refine.classList.toggle("is-active", isEdit);
  els.refine.title = refineAllowed ? "\u5728\u5f53\u524d\u7ed3\u679c\u4e0a\u7ee7\u7eed\u4fee\u6539" : "抠图 / 超分不支持继续修改";
  const chips = [];
  if (capability === "image_segment") chips.push(`${values.max_objects} 个物体`, `阈值 ${values.threshold}`);
  else if (capability === "video_upscale") chips.push(values.upscale_target, `CRF ${values.crf}`);
  else if (state.mode === "video" && isEdit) chips.push("\u667a\u80fd\u89c6\u9891\u7f16\u8f91");
  else if (state.mode === "video") chips.push(values.ratio, values.resolution, `${values.duration}s`, values.repeat_count > 1 ? `\u00d7${values.repeat_count}` : "\u4e0d\u91cd\u590d");
  else chips.push(values.aspect_ratio, values.image_size, values.repeat_count > 1 ? `\u00d7${values.repeat_count}` : "1 \u5f20");
  els.paramSummary.innerHTML = chips.map((chip) => `<span class="fc-param-chip"><strong>${escapeHtml(chip)}</strong></span>`).join("");
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
  [...fileList].forEach((file) => {
    const kind = mediaKind(file);
    if (kind === "unknown") { toast(`不支持的文件：${file.name}`, "error"); return; }
    state.attachments.push({ id: uid(), file, kind, previewUrl: kind === "image" ? URL.createObjectURL(file) : "" });
  });
  renderAttachments();
}
function renderAttachments() {
  els.attachments.innerHTML = "";
  state.attachments.forEach((item) => {
    const node = $("#asset-template").content.firstElementChild.cloneNode(true); node.dataset.id = item.id;
    const preview = node.querySelector(".fc-asset-preview");
    if (item.kind === "image") { const image = document.createElement("img"); image.src = item.previewUrl; image.alt = ""; preview.append(image); }
    else preview.textContent = item.kind === "video" ? "VID" : "AUD";
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
    const target = UPSCALE_TARGETS.find((item) => item.value === params.upscale_target) || UPSCALE_TARGETS[0];
    return {
      endpoint: `${localModuleBase("video_upscale")}/jobs/json`, app: "local-ai", base: LOCAL_AI_MOUNT, jobPath: "/api/video_upscale/jobs",
      payload: {
        values: {
          provider: "comfyui_local",
          upscale_model: model.defaults?.upscale_model || model.variants?.[0]?.id || "flashvsr_v1.1_full_3b",
          target_width: target.width,
          target_height: target.height,
          crf: Number(params.crf),
          repeat_count: 1,
          concurrency: 1,
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
  const isEdit = Boolean(editSourceTaskId);
  const requestedChange = isEdit ? editInstruction() : "";
  if (isEdit && !requestedChange) throw new Error("请补充具体修改要求");
  if (state.mode === "video") {
    const local = model.provider === "comfyui_local";
    const hasEditVideo = isEdit && state.attachments.some((item) => item.kind === "video" && item.sourceTaskId === editSourceTaskId);
    // 本地 H3 没有「在原视频上改」这条路径：它会把 duration=-1 兜成 12 秒、当成一次
    // 全新生成，白烧算力。这里硬拦，而不是悄悄退化成普通生成。
    if (isEdit && local) throw new Error("本地视频模型不支持继续修改原视频，请改用云端 Seedance，或直接重新生成");
    if (isEdit && !hasEditVideo) throw new Error("视频编辑需要保留原视频素材");
    const prompt = hasEditVideo
      ? `\u7f16\u8f91\u53c2\u8003\u89c6\u9891\u3002\u4fdd\u6301\u539f\u89c6\u9891\u4e3b\u4f53\u3001\u6784\u56fe\u4e0e\u8fd0\u52a8\u8fde\u7eed\u6027\uff0c\u53ea\u6267\u884c\u4ee5\u4e0b\u4fee\u6539\uff1a${requestedChange}`
      : els.prompt.value.trim();
    return {
      endpoint: "/seedance/api/jobs/json", app: "seedance", base: "/seedance", jobPath: "/api/jobs",
      payload: {
        // provider 由所选模型决定：本地走 comfyui_local（后端会把 base_url 锁到本地
        // 网关并清空密钥），云端走 volcengine（后端注入公司 key，浏览器永远拿不到）。
        provider: model.provider,
        model: model.id,
        prompt,
        task_mode: hasEditVideo ? "edit" : "reference",
        duration: hasEditVideo ? -1 : Number(params.duration),
        ratio: hasEditVideo ? "adaptive" : params.ratio,
        resolution: params.resolution,
        repeat_count: Number(params.repeat_count),
        concurrency: 1,
        vary_seed: true,
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
  if (!state.editSourceTaskId && EDIT_MARKER_RE.test(els.prompt.value)) { toast("已退出编辑态，请重新点「继续修改」再提交", "error"); els.prompt.focus(); return; }
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
        cell.href = url; cell.target = "_blank"; cell.rel = "noopener";
        cell.download = `${task.resultName || "result"}-${index + 1}`;
        cell.title = `下载第 ${index + 1} 张`;
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
    els.mediaLoading.hidden = false;
    const title = task.status === "failed" ? "生成失败" : task.status === "success" ? "正在读取结果" : "正在生成";
    els.previewMedia.innerHTML = `<div style="text-align:center;color:var(--muted);font-size:12px;line-height:1.7"><strong style="display:block;color:var(--text);font-size:15px">${title}</strong><span>${escapeHtml(task.error || task.progress || "请稍候…")}</span></div>`;
    els.download.disabled = true;
  }
  renderHistory(); renderConversations();
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
  const successes = (activeConversation()?.tasks || []).filter((task) => task.resultUrl);
  const shown = successes.slice(-HISTORY_RENDER_LIMIT);
  const stripOpen = els.historyStrip.classList.contains("is-open");
  els.historyStrip.innerHTML = "";
  shown.forEach((task, index) => {
    const node = $("#history-template").content.firstElementChild.cloneNode(true);
    node.dataset.id = task.id;
    node.style.animationDelay = `${Math.min(index * 35, 210)}ms`;
    node.classList.toggle("is-active", task.id === state.activeTaskId);
    const thumb = node.querySelector(".fc-history-thumb");
    if (task.mode === "video") {
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
  els.download.addEventListener("click", () => {
    const task = activeTask(); if (!task?.resultUrl) return;
    const urls = Array.isArray(task.resultUrls) && task.resultUrls.length ? task.resultUrls : [task.resultUrl];
    urls.forEach((url, index) => {
      const link = document.createElement("a");
      link.href = url;
      link.download = urls.length > 1 ? `${task.resultName || "result"}-${index + 1}` : (task.resultName || "creation");
      link.target = "_blank"; link.click();
    });
  });
  els.historyToggle.addEventListener("click", () => setHistoryStripOpen(!els.historyStrip.classList.contains("is-open")));
  els.refine.addEventListener("click", async () => {
    const task = latestSuccess();
    if (!task) return;
    const originalPrompt = String(task.prompt || "");
    state.activeTaskId = task.id;
    try {
      const response = await fetch(task.resultUrl);
      if (!response.ok) throw new Error(`读取结果失败 (${response.status})`);
      const blob = await response.blob();
      const name = task.resultName || (task.mode === "video" ? "previous.mp4" : "previous.png");
      const file = new File([blob], name, { type: blob.type || (task.mode === "video" ? "video/mp4" : "image/png") });
      state.editSourceTaskId = task.id;
      state.editBasePrompt = originalPrompt;
      state.attachments = [{
        id: uid(),
        file,
        kind: task.mode === "video" ? "video" : "image",
        previewUrl: task.mode === "image" ? URL.createObjectURL(file) : "",
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
  els.generate.addEventListener("click", generate); els.upload.addEventListener("click", () => els.fileInput.click());
  els.fileInput.addEventListener("change", () => { addFiles(els.fileInput.files); els.fileInput.value = ""; });
  els.modeSwitch.querySelectorAll(".fc-mode-option").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  els.modelModeSwitch.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  $("#model-bubble").addEventListener("click", () => openPopover("model-popover")); $("#params-bubble").addEventListener("click", () => openPopover("params-popover"));
  $$("[data-close]").forEach((button) => button.addEventListener("click", () => closePopover(button.dataset.close)));
  document.addEventListener("click", (event) => { if (!event.target.closest(".fc-popover, #model-bubble, #params-bubble, #upload-bubble, #generate")) closeAllPopovers(); });
  const composer = els.prompt.closest(".fc-composer");
  ["dragenter", "dragover"].forEach((name) => composer.addEventListener(name, (event) => { event.preventDefault(); $("#drop-overlay").hidden = false; }));
  ["dragleave", "drop"].forEach((name) => composer.addEventListener(name, (event) => { event.preventDefault(); $("#drop-overlay").hidden = true; }));
  composer.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  els.previewPanel.addEventListener("animationend", (event) => { if (event.animationName === "fc-history-switch") els.previewPanel.classList.remove("is-switching"); });
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
