'use strict';

// ============================================================
// Module 1: Mode Detection
// ============================================================
const IN_PORTAL = window.location.pathname.startsWith('/nano-banana/');
const APP_PATH  = IN_PORTAL ? '/nano-banana' : '';

// Lowercased job.status values considered terminal (used to gate poll loops and running-indicator recomputation).
var TERMINAL_STATUSES = new Set(['succeeded', 'success', 'failed', 'fail', 'failure', 'cancelled', 'canceled']);

// ============================================================
// 任务完成系统通知（浏览器 Notification + 标题闪烁，按 jobId 去重）
// Portal 反向代理下所有页面同源、localStorage 共享 → 子应用与 Portal
// 双侧检测到同一任务终态时只弹一次。Notification 需要安全上下文：
// 生产 HTTPS（自签证书点过「继续访问」后算安全上下文）可用，HTTP
// 测试环境自动降级为标题闪烁。
// ============================================================
var _notifiedJobs = null;
function _notifyLoadSeen() {
  if (_notifiedJobs) return _notifiedJobs;
  try { _notifiedJobs = JSON.parse(localStorage.getItem('aiPortal.notifiedJobs') || '{}') || {}; }
  catch (e) { _notifiedJobs = {}; }
  return _notifiedJobs;
}
function requestNotifyPermission() {
  // 必须在用户手势（提交点击）内调用；浏览器对每个站点只提示一次
  try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}
}
function confirmSafe(message, options) {
  try {
    var p = (window.parent && window.parent !== window) ? window.parent : window;
    if (typeof p.portalConfirm === 'function') return p.portalConfirm(message, options || {});
  } catch (e) {}
  return Promise.resolve(window.confirm(message));
}

function notifyJobDone(jobId, status, label) {
  try {
    // Portal iframe 内：交给父窗口统一弹窗（同源可直调；去重/页面内弹窗
    // 都在父层处理），独立模式才走本地下方逻辑
    if (window.parent && window.parent !== window && typeof window.parent.__notifyJobDone === 'function') {
      window.parent.__notifyJobDone(jobId, status, label, 'nb');
      return;
    }
    if (jobId === undefined || jobId === null || jobId === '') return;
    // 状态归一化：与 Portal 侧 15s 兜底轮询用同一套 token 去重
    var s = String(status).toLowerCase();
    var norm = ['succeeded', 'success', 'completed'].indexOf(s) >= 0 ? 'succeeded'
      : ['failed', 'fail', 'failure'].indexOf(s) >= 0 ? 'failed'
      : ['cancelled', 'canceled'].indexOf(s) >= 0 ? 'cancelled' : s;
    var map = _notifyLoadSeen();
    if (map[jobId] === norm) return; // 已通知过（含 Portal 侧先弹）
    map[jobId] = norm;
    var keys = Object.keys(map);
    if (keys.length > 200) keys.slice(0, keys.length - 200).forEach(function (k) { delete map[k]; });
    localStorage.setItem('aiPortal.notifiedJobs', JSON.stringify(map));
    var ok = norm === 'succeeded';
    var title = (label || '生成任务') + (ok ? ' 已完成' : ' 已结束');
    var body = ok ? '结果已就绪，回到页面即可查看和下载。' : '任务以「' + norm + '」结束，请回到页面查看详情。';
    if ('Notification' in window && Notification.permission === 'granted') {
      try {
        var n = new Notification(title, { body: body, tag: 'ai-portal-job-done' });
        n.onclick = function () { try { window.focus(); } catch (e) {} n.close(); };
      } catch (e) { /* 构造失败时降级标题闪烁 */ }
    }
    _notifyFlashTitle(title);
  } catch (e) { /* 通知尽力而为，绝不打断主流程 */ }
}
var _notifyFlashTimer = null;
function _notifyFlashTitle(message) {
  try {
    // iframe 模式下闪烁顶层标题（同源可访问 parent），独立模式闪自己
    var doc = (window.parent && window.parent !== window) ? window.parent.document : document;
    var base = doc.title;
    var count = 0;
    var tick = function () {
      count += 1;
      doc.title = (count % 2 === 1) ? ('✅ ' + message + ' — ' + base) : base;
      if (count >= 10) { clearInterval(_notifyFlashTimer); _notifyFlashTimer = null; doc.title = base; }
    };
    if (_notifyFlashTimer) clearInterval(_notifyFlashTimer);
    _notifyFlashTimer = setInterval(tick, 1500);
    tick();
  } catch (e) {}
}

// ============================================================
// Module 2: Utilities
// ============================================================
function _workspaceId() {
  const params = new URLSearchParams(window.location.search);
  let id = params.get('ws');
  if (!id) {
    id = localStorage.getItem('workspace_id');
    if (!id) { id = crypto.randomUUID(); localStorage.setItem('workspace_id', id); }
  }
  return id;
}

function getActiveWorkspaceId() {
  return window._activeWorkspaceId || _workspaceId();
}

async function api(url, method, body, workspaceOverride) {
  try {
    const wsId = workspaceOverride || getActiveWorkspaceId();
    const sep = url.includes('?') ? '&' : '?';
    const urlWithWs = url + sep + 'ws=' + encodeURIComponent(wsId);
    const headers = { 'X-Workspace-Id': wsId };
    const keyId = localStorage.getItem('portal_key_id_nano_banana');
    if (keyId) headers['X-Key-Id'] = keyId;
    const opts = { method: method || 'GET', headers };
    if (body) opts.body = body;
    const res = await fetch(urlWithWs, opts);
    return await res.json();
  } catch (e) { return null; }
}

// Status-aware single poll for pollJob's retry logic. Unlike api() — which
// collapses HTTP 404 / 5xx / network-error / bad-JSON all into null (or a
// truthy {error:...} body that pollJob looped on forever showing "unknown") —
// this distinguishes:
//   {kind:'ok', job}  HTTP 200 + a job object carrying a status field
//   {kind:'gone'}     HTTP 404 — job gone (sub-app restarted, JOBS cleared)
//   {kind:'error'}    network error / timeout / 5xx / non-JSON — transient
async function pollJobOnce(url, workspaceOverride) {
  try {
    const wsId = workspaceOverride || getActiveWorkspaceId();
    const sep = url.includes('?') ? '&' : '?';
    const urlWithWs = url + sep + 'ws=' + encodeURIComponent(wsId);
    const headers = { 'X-Workspace-Id': wsId };
    const keyId = localStorage.getItem('portal_key_id_nano_banana');
    if (keyId) headers['X-Key-Id'] = keyId;
    const res = await fetch(urlWithWs, { method: 'GET', headers });
    if (res.status === 404) return { kind: 'gone' };
    if (!res.ok) return { kind: 'error' };
    const job = await res.json();
    if (!job || typeof job.status === 'undefined') return { kind: 'error' };
    return { kind: 'ok', job };
  } catch (e) {
    return { kind: 'error' };
  }
}

function escHtml(s) { return s ? String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : ''; }

function jobStatusLabel(status) {
  var map = { queued: '排队中', pending: '等待中', running: '处理中', querying: '查询中', succeeded: '已完成', success: '已完成', completed: '已完成', failed: '失败', failure: '失败', cancelled: '已取消', canceled: '已取消' };
  return map[String(status || '').toLowerCase()] || String(status || '未知');
}

function jobStatusClass(status) {
  var s = String(status || '').toLowerCase();
  if (['succeeded', 'success', 'completed'].includes(s)) return 'is-success';
  if (['failed', 'failure'].includes(s)) return 'is-failed';
  if (['pending', 'queued'].includes(s)) return 'is-pending';
  if (s === 'querying') return 'is-querying';
  return 'is-running';
}

function jobStatusBadgeTone(status) {
  var state = jobStatusClass(status);
  return state === 'is-success' ? 'success' : state === 'is-failed' ? 'danger' : state === 'is-pending' ? 'warning' : 'info';
}

// 友好错误提示：把高频失败类型翻译成中文+下一步。返回原文兜底时
// 与 job.errors[0] 严格相等，调用方据此决定是否 escHtml。
function friendlyJobErrorHint(job) {
  var errors = (job && job.errors) || [];
  if (!errors.length) return '';
  var firstError = errors[0];
  if (/\[auth_failed\]/i.test(firstError) || /\bHTTP\s+401\b/i.test(firstError) || /\b401\s+Unauthorized\b/i.test(firstError)) return '❌ API Key 无效或已过期，请检查配置';
  if (/\[rate_limited\]/i.test(firstError) || /\bHTTP\s+429\b/i.test(firstError) || /\b429\s+Too Many Requests\b/i.test(firstError)) return '⏱️ 请求过于频繁，已自动重试多次仍失败，请稍后再试';
  if (/\[permission_denied\]/i.test(firstError) || /\bHTTP\s+403\b/i.test(firstError) || /\b403\s+Forbidden\b/i.test(firstError)) return '🚫 权限不足或配额已用完，请联系管理员';
  if (firstError.indexOf('[server_error]') >= 0) return '⚠️ API 服务暂时不可用，已自动重试失败，请稍后重试';
  if (firstError.indexOf('[network_error]') >= 0) return '🌐 网络连接失败，请检查网络或 API 地址';
  if (/requires at least one reference image|requires a reference image|需要参考图|至少.*参考图/i.test(firstError)) return '请上传至少一张参考图，或切换到文生图模型';
  if (/requires a prompt|需要提示词|请输入提示词|prompt is required/i.test(firstError)) return '请输入生成提示词';
  if (/ComfyUI.*未启动|未启动.*ComfyUI|timed out|WinError 10061|connection refused/i.test(firstError)) return '本地模型服务未就绪，正在自动拉起，请稍后重试';
  if (/model_kind.*已停用|已停用.*model_kind|not yet supported|unsupported model/i.test(firstError)) return '当前模型不可用，请切换到其它可用模型';
  if (/no output files|no images|produced no output|missing.*reference/i.test(firstError)) return '模型没有返回结果，请检查参考图或更换模型';
  return firstError;
}

// ============================================================
// Module 3: Form Field Helper
// ============================================================
function nbField(name) {
  const form = document.getElementById('nb-form');
  return form?.elements[name] || document.querySelector(`[form="nb-form"][name="${name}"]`) || document.querySelector(`[name="${name}"]`);
}

function clearPreview(drop) {
  drop.classList.remove('hasPreview');
  drop.querySelector('.preview')?.remove();
  const span = drop.querySelector('span');
  if (span) span.textContent = '未上传';
}

function clearAllMediaInputs() {
  document.querySelectorAll('.drop input[type="file"]').forEach(function (input) {
    input.value = '';
    const drop = input.closest('.drop');
    if (drop) clearPreview(drop);
  });
}

// ============================================================
// Module 4: File Drop Helpers
// ============================================================
function wireFileDrop(drop, input) {
  input.addEventListener('change', async function () {
    const f = input.files?.[0];
    if (!f) { clearPreview(drop); return; }
    // Immediate local preview
    const localUrl = URL.createObjectURL(f);
    showPreview(drop, input.name, localUrl, f.name);
    // Upload to server so the file survives tab switch / refresh / archive save.
    // The upload is owned by the topic that was active when the file was picked;
    // a response arriving after a topic switch must not touch the new topic.
    const ownerWsId = getActiveWorkspaceId();
    try {
      const fd = new FormData();
      fd.set(input.name, f);
      const res = await api(APP_PATH + '/api/media/upload', 'POST', fd, ownerWsId);
      if (getActiveWorkspaceId() !== ownerWsId) return;
      if (res && res.stored) {
        const app = window._app_nb;
        const media = (app && app.savedMedia) || window._currentSavedMedia || {};
        media[input.name] = {
          filename: res.filename,
          mime: res.mime,
          stored: res.stored,
          url: res.url,
        };
        if (app) app.savedMedia = media;
        window._currentSavedMedia = media;
        showPreview(drop, input.name, resolveMediaUrl(res.url), res.filename);
        try { URL.revokeObjectURL(localUrl); } catch (e) {}
        if (app && typeof app.saveWorkspaceDraft === 'function') app.saveWorkspaceDraft();
      } else {
        // Server rejected the upload (wrong content type / too large / network
        // drop). Roll back the local preview — otherwise the user believes the
        // reference material is saved and submits a job without it.
        clearPreview(drop);
        delete window._currentSavedMedia?.[input.name];
        const app = window._app_nb;
        if (app && app.savedMedia) delete app.savedMedia[input.name];
        alert('上传失败：' + ((res && res.error) ? res.error : '服务器未接受文件（请检查类型或大小）'));
      }
    } catch (e) {
      clearPreview(drop);
      delete window._currentSavedMedia?.[input.name];
      const app = window._app_nb;
      if (app && app.savedMedia) delete app.savedMedia[input.name];
      alert('上传失败：网络错误，请重试');
    }
  });
  drop.addEventListener('dragover', function (e) { e.preventDefault(); drop.classList.add('isDragging'); });
  drop.addEventListener('dragleave', function () { drop.classList.remove('isDragging'); });
  drop.addEventListener('drop', function (e) {
    e.preventDefault(); drop.classList.remove('isDragging');
    const f = e.dataTransfer?.files?.[0]; if (!f) return;
    const dt = new DataTransfer(); dt.items.add(f); input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function makeDrop(container, name, label) {
  const el = document.createElement('label');
  el.className = 'drop';
  el.textContent = label;
  const input = document.createElement('input');
  input.name = name; input.type = 'file'; input.accept = 'image/*';
  input.setAttribute('form', 'nb-form');
  const span = document.createElement('span');
  span.textContent = '未上传';
  const rmBtn = document.createElement('button');
  rmBtn.className = 'removeMediaBtn'; rmBtn.type = 'button'; rmBtn.textContent = '移除';
  rmBtn.addEventListener('click', function (e) {
    e.preventDefault(); e.stopPropagation();
    input.value = '';
    delete window._currentSavedMedia?.[name];
    clearPreview(el);
  });
  el.append(input, span, rmBtn);
  wireFileDrop(el, input);
  container.appendChild(el);
}

function showPreview(drop, name, url, filename) {
  drop.classList.add('hasPreview');
  drop.querySelector('.preview')?.remove();
  const kind = name && (name.includes('video') ? 'video' : name.includes('audio') ? 'audio' : 'image');
  const tag = kind === 'image' ? 'img' : kind === 'video' ? 'video' : 'audio';
  const media = document.createElement(tag);
  media.className = 'preview'; media.src = url;
  if (kind !== 'image') media.controls = true;
  if (kind !== 'audio') media.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); openPreview(kind || 'image', url); });
  drop.insertBefore(media, drop.querySelector('span'));
  const span = drop.querySelector('span');
  if (span) span.textContent = filename || '已上传';
}

function openPreview(kind, url) {
  var dlg = document.getElementById('previewDialog');
  if (!dlg) return;
  var body = document.getElementById('previewDialogBody');
  if (!body) return;
  body.innerHTML = '';
  var m = document.createElement(kind === 'image' ? 'img' : 'video');
  m.src = url; if (kind === 'video') m.controls = true;
  body.append(m); dlg.showModal();
}

// ============================================================
// Module 5: Image Resize Pipeline
// ============================================================
function appendDisabledResizeValues(data) {
  for (var _i = 0, _arr = ['resize_width', 'resize_height', 'resize_interpolation', 'resize_method', 'resize_condition', 'resize_multiple_of']; _i < _arr.length; _i++) {
    var name = _arr[_i];
    var input = nbField(name);
    if (input) data.set(name, input.value);
  }
}

function targetResizeSize(fileWidth, fileHeight) {
  var wInput = nbField('resize_width');
  var hInput = nbField('resize_height');
  var width = Math.max(1, Number(wInput ? wInput.value : 0) || fileWidth);
  var height = Math.max(1, Number(hInput ? hInput.value : 0) || fileHeight);
  var mInput = nbField('resize_multiple_of');
  var multiple = Math.max(0, Number(mInput ? mInput.value : 0) || 0);
  if (multiple > 1) {
    width = Math.max(multiple, Math.round(width / multiple) * multiple);
    height = Math.max(multiple, Math.round(height / multiple) * multiple);
  }
  var cInput = nbField('resize_condition');
  var condition = cInput ? cInput.value : 'always';
  if (condition === 'only_downscale' && (width >= fileWidth || height >= fileHeight)) return null;
  if (condition === 'only_upscale' && (width <= fileWidth || height <= fileHeight)) return null;
  return { width: width, height: height };
}

async function resizeImageFile(file) {
  var reInput = nbField('resize_enabled');
  if (!reInput || !reInput.checked || !file.type.startsWith('image/')) return file;
  var bitmap = await createImageBitmap(file);
  var target = targetResizeSize(bitmap.width, bitmap.height);
  if (!target) { bitmap.close(); return file; }
  var canvas = document.createElement('canvas');
  canvas.width = target.width;
  canvas.height = target.height;
  var ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = true;
  var riInput = nbField('resize_interpolation');
  ctx.imageSmoothingQuality = (riInput ? riInput.value : 'high');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  var sx = 0, sy = 0, sw = bitmap.width, sh = bitmap.height;
  var dx = 0, dy = 0, dw = canvas.width, dh = canvas.height;
  var rmInput = nbField('resize_method');
  var method = rmInput ? rmInput.value : 'stretch';
  if (method === 'contain' || method === 'cover') {
    var imageRatio = bitmap.width / bitmap.height;
    var targetRatio = canvas.width / canvas.height;
    if (method === 'contain') {
      if (imageRatio > targetRatio) {
        dw = canvas.width;
        dh = Math.round(canvas.width / imageRatio);
      } else {
        dh = canvas.height;
        dw = Math.round(canvas.height * imageRatio);
      }
      dx = Math.round((canvas.width - dw) / 2);
      dy = Math.round((canvas.height - dh) / 2);
    } else {
      if (imageRatio > targetRatio) {
        sw = Math.round(bitmap.height * targetRatio);
        sx = Math.round((bitmap.width - sw) / 2);
      } else {
        sh = Math.round(bitmap.width / targetRatio);
        sy = Math.round((bitmap.height - sh) / 2);
      }
    }
  }
  ctx.drawImage(bitmap, sx, sy, sw, sh, dx, dy, dw, dh);
  var blob = await new Promise(function (resolve) { canvas.toBlob(resolve, 'image/png'); });
  bitmap.close();
  if (!blob) return file;
  var stem = file.name.replace(/\.[^.]+$/, '');
  return new File([blob], stem + '_resized.png', { type: 'image/png' });
}

async function imageUrlToFile(url, filename) {
  var res = await fetch(url);
  var blob = await res.blob();
  return new File([blob], filename || 'image.png', { type: blob.type || 'image/png' });
}

// ============================================================
// Module 6: Media URL Helper
// ============================================================
function resolveMediaUrl(url) {
  if (url && url.startsWith('/api/')) return APP_PATH + url;
  return url;
}

// ============================================================
// Module 7: Provider Models (fallback)
// ============================================================
var FALLBACK_PROVIDERS = {
  comfyui_local: { label: 'Local ComfyUI (free)', base_url: 'http://127.0.0.1:8801', api_style: 'comfyui_workflow', image_size_options: ['1K', '1.5K', '2K'], models: [{ id: 'qwen2511', label: 'Qwen 2511' }, { id: 'flux2_klein_allinone', label: 'Klein' }, { id: 'krea2_three_stage', label: 'Krea T2I' }, { id: 'anime2real_auto', label: 'Anime2Real' }, { id: 'zimage_multifunction', label: 'Z-Image' }, { id: 'klein_true_v3_assets', label: 'Klein Assets' }, { id: 'krea2_style_transfer', label: 'Krea Style' }] },
  t8star: { label: 'T8Star Images API', base_url: 'https://ai.t8star.org', models: [{ id: 'nano-banana-2', label: 'nano-banana-2' }, { id: 'gemini-3.1-flash-image-preview', label: 'gemini-3.1-flash-image-preview' }, { id: 'gemini-3-pro-image-2k', label: 'gemini-3-pro-image-2k' }, { id: 'gemini-3-pro-image-4k', label: 'gemini-3-pro-image-4k' }, { id: 'gpt-image-2.5-flare', label: 'GPT Image 2.5 Flare' }, { id: 'gpt-image-2.5-flare-2k', label: 'GPT Image 2.5 Flare 2K' }, { id: 'gpt-image-2.5-flare-4k', label: 'GPT Image 2.5 Flare 4K' }, { id: 'gpt-image-2.5-sunburst', label: 'GPT Image 2.5 Sunburst' }, { id: 'gpt-image-2.5-sunburst-2k', label: 'GPT Image 2.5 Sunburst 2K' }, { id: 'gpt-image-2.5-sunburst-4k', label: 'GPT Image 2.5 Sunburst 4K' }] },
  gemini: { label: 'Chiyun', base_url: 'https://chiyun.work', models: [{ id: 'banana2-ssvip', label: 'banana2-ssvip' }, { id: 'nano-banana2[2K]-base', label: 'nano-banana2[2K]-base' }, { id: 'gpt-image-2', label: 'gpt-image-2' }] },
  volcengine: {
    label: '火山引擎官方 (Seedream)',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    company_key: true,
    company_key_available: false,
    image_size_options: ['1K', '1.5K', '2K'],
    max_reference_images: 10,
    supports_seed: false,
    models: [{ id: 'doubao-seedream-5-0-pro-260628', label: 'Seedream 5.0 Pro' }],
  },
};

// ============================================================
// Module 8: NanoBananaApp Factory
// ============================================================
function NanoBananaApp() {
  return {
    // ---- 8a. State Properties ----

    isStandalone: !IN_PORTAL,
    appPath: APP_PATH,
    appStatus: 'unknown',

    // Provider / API
    providers: {},
    provider: 't8star',
    models: [],
    baseUrl: 'https://ai.t8star.org',
    baseUrlReadonly: false,
    providerHint: '',
    keyHint: '',
    imageSizeOptions: ['1K', '2K', '4K'],
    model: '',
    imageSize: '',
    supportsSeed: true,
    maxReferenceImages: 14,
    modelCaps: null,
    modelHint: '',
    _providerKeys: {},
    _activeProvider: 't8star',
    _defaultProvider: 't8star',
    localReady: true,
    _personalKeyHint: '',
    outputDir: '',
    dirHandle: null,
    autoDownload: false,

    // Submission
    submitting: false,
    submittingRequest: false,
    optimizing: false,
    optimizedPrompt: '',
    optimizeError: '',
    statusText: '空闲',
    eventsText: '',
    runtimeTick: 0,

    // Archives
    archives: [],
    selectedArchive: '默认方案',
    archiveHint: '',
    currentSchemeName: '默认方案',
    isDirty: false,
    schemeNameInput: '',
    _dirtyDialogOpen: false,
    _dirtyPending: null,

    // Saved media (reference images from archive)
    savedMedia: {},

    // Workspace tabs
    wsTab: 'jobs',

    // Jobs list (drives green-dot indicator on non-active tabs)
    jobs: [],
    jobsLimit: 20,
    selectedJobId: null,
    selectedJobLabel: '',

    // Activity
    activityRecords: [],
    activityCounts: null,
    activityDetail: null,

    // Workspace system (standalone)
    workspaceId: '',
    workspaceName: '',
    workspaceHint: '',

    // Resize toggle
    resizeEnabled: false,

    // --- Tab bar state (Task 4) ---
    tabs: [],                   // [{id, name, running}]
    activeTabId: 'default',
    editingTabId: null,         // tab id being renamed inline, or null
    _closeConfirmTabId: null,   // tab id that opened the close-confirm modal
    _tabStateCache: {},         // { wsId: {statusText, eventsText, submitting, baseUrl, provider, models, workspaceName} }
    _topicSubmissionSeq: {},    // { wsId: latest submit sequence }
    _draftLoaded: false,        // set once a workspace draft has been restored (blocks config default clobbering)

    // ---- 8b. init() ----

    async init() {
      var self = this;
      window._app_nb = self;
      window._currentSavedMedia = self.savedMedia;
      setInterval(function () { self.runtimeTick = (self.runtimeTick + 1) % 1e9; }, 1000);

      // Workspace init
      self.workspaceId = _workspaceId();
      self.workspaceName = '默认主题';
      self.isStandalone = !IN_PORTAL;

      // Build upload slots (before config loads — synchronous DOM)
      self.buildUploadSlots();
      self.wireDrops();

      // Load server config
      try { await self.loadConfig(); } catch (e) { console.warn('loadConfig failed:', e); }

      // Legacy: also try raw /api/config (standalone path)
      if (!Object.keys(self.providers).length) {
        try {
          var wsId = getActiveWorkspaceId();
          var fallbackRes = await fetch(APP_PATH + '/api/config?ws=' + encodeURIComponent(wsId));
          if (fallbackRes.ok) await self.loadConfigFromResponse(fallbackRes);
        } catch (e) { /* ignore */ }
      }

      // Fallback providers if all else fails
      if (!Object.keys(self.providers).length) {
        self.providers = FALLBACK_PROVIDERS;
        self.applyProvider(self.provider);
      }

      // --- Tab bar restoration (Task 4) ---
      var raw = localStorage.getItem('nano-banana.tabs');
      if (raw) {
        try {
          var data = JSON.parse(raw);
          if (data.tabs && data.tabs.length) {
            self.tabs = data.tabs.map(function (t) { return { id: t.id, name: t.name || '未命名主题', running: false }; });
            self.activeTabId = data.activeTabId || data.tabs[0].id;
          }
        } catch (e) {}
      }
      if (!self.tabs.length) {
        var oldWsId = localStorage.getItem('workspace_id') || 'default';
        self.tabs = [{ id: oldWsId, name: self.workspaceName || '未命名主题', running: false }];
        self.activeTabId = oldWsId;
      }
      window._activeWorkspaceId = self.activeTabId;

      // Load archives (after tab restoration so the active workspace id is known)
      try { await self.loadArchives(); } catch (e) { console.warn('loadArchives failed:', e); }

      // Load workspace or server preset
      try { self.loadInitialPreset(); } catch (e) { console.warn('loadPreset failed:', e); }

      // Resize state initial sync
      self.updateResizeState();

      // Auto-save workspace on any form control change. The prompt <textarea>
      // lives outside <form id="nb-form"> (linked via form="nb-form"), so listen
      // at the app root and filter to controls that carry a name.
      var nbRoot = document.getElementById('nb-app');
      if (nbRoot) {
        var autoSave = function (e) {
          var t = e.target;
          if (!t || !t.name) return;
          self.isDirty = true;
          self.scheduleWorkspaceSave();
        };
        nbRoot.addEventListener('input', autoSave);
        nbRoot.addEventListener('change', autoSave);
      }

      // Flush the workspace draft immediately on unload so even a quick
      // refresh (before the 500ms debounce fires) keeps the latest prompt/params.
      var flushDraft = function () { try { self.saveWorkspaceDraft(); } catch (e) {} };
      window.addEventListener('pagehide', flushDraft);
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'hidden') flushDraft();
      });

      // Download links: use blob download to avoid iframe navigation timeout
      var dlContainer = document.getElementById('nb-results');
      if (dlContainer) {
        dlContainer.addEventListener('click', function (e) {
          var btn = e.target.closest('.dl-btn');
          if (!btn) return;
          e.preventDefault();
          var u = btn.dataset.url;
          var fn = btn.dataset.filename || 'image';
          if (u) self._blobDownload(u, fn);
        });
      }

      // Global 5s tick: refresh jobs list so every tab's green-dot indicator
      // stays fresh, not just the tab that submitted. Skip while the page is
      // hidden to avoid burning cycles when the tab is in the background.
      self._loadJobsTimer = setInterval(function () {
        if (document.visibilityState !== 'hidden') self.loadJobs();
      }, 5000);
      // Also fire once at init to populate tab.running on first load.
      try { self.loadJobs(); } catch (e) { /* silent */ }
    },

    // ---- 8c. loadConfig / applyProvider ----

    async loadConfig() {
      var res = await api(APP_PATH + '/api/config');
      if (!res || !res.providers) {
        this.providers = FALLBACK_PROVIDERS;
        this.applyProvider('t8star');
        return;
      }
      await this.loadConfigFromResponse({ ok: true, json: function () { return Promise.resolve(res); } });
    },

    async loadConfigFromResponse(response) {
      var data;
      try { data = await response.json(); } catch (e) { return; }
      if (!data || !data.providers) return;
      this.providers = data.providers;
      this.localReady = data.local_ready !== false;
      // 默认供应商以 default_provider 为准。曾有「保留当前选择」的启发式：
      // 新页面的下拉框默认是第一个选项，会把浏览器默认误当作用户选择，
      // 导致默认值（火山引擎）被 comfyui 顶掉。用户显式选择由草稿/存档
      // 的 applyPreset 路径保留，不需要这里的启发式。
      var defaultP = data.default_provider || Object.keys(data.providers)[0];
      this._defaultProvider = defaultP;
      this.applyProvider(defaultP);
      // Ensure select syncs
      var self = this;
      setTimeout(function () {
        if (self._draftLoaded) return;
        var s = document.querySelector('#nb-form select[name="provider"]');
        if (s && s.value !== defaultP) s.value = defaultP;
        if (data.providers[defaultP]) self.applyProvider(defaultP);
      }, 0);
      var activeCfg = this.providers[this.provider] || {};
      if (!activeCfg.company_key) {
        this._personalKeyHint = data.has_key ? '已检测到 key: ' + (data.masked_key || '') : '未检测到本地 key';
        this.keyHint = this._personalKeyHint;
      }
    },

    onProviderChange(value) {
      if (value === "comfyui_local" && this.localReady === false) {
        this.applyProvider(value, true);
        this.providerHint = "本地模型未连接，请先启动本地模型，或手动选择云端模型后重试。";
        var s = nbField("provider");
        if (s && s.value !== value) s.value = value;
        return;
      }
      this.applyProvider(value);
    },

    applyProvider(provider, skipDefaults) {
      var cfg = this.providers[provider];
      if (!cfg) return;
      var keyInput = nbField('api_key');
      var providerKeys = this.providerKeyBucket();
      // v-model can update ``provider`` before @change invokes this method;
      // keep an independent applied-provider pointer so the outgoing key is
      // always saved under the provider it actually belonged to.
      var previousProvider = this._activeProvider || this.provider;
      var previousCfg = this.providers[previousProvider] || {};
      if (!skipDefaults && keyInput && previousProvider && !previousCfg.company_key) {
        providerKeys[previousProvider] = keyInput.value;
      }
      this.provider = provider;
      this._activeProvider = provider;
      this.baseUrl = cfg.base_url || '';
      this.baseUrlReadonly = !!cfg.company_key;
      this.providerHint = cfg.hint || '';
      this.models = cfg.models || [];
      this.imageSizeOptions = (cfg.image_size_options || ['1K', '2K', '4K']).slice();
      this.supportsSeed = cfg.supports_seed !== false;
      this.maxReferenceImages = Number(cfg.max_reference_images || 14);

      if (keyInput) {
        if (cfg.company_key) {
          keyInput.value = '';
          keyInput.readOnly = true;
          keyInput.placeholder = cfg.company_key_available
            ? '已使用官方密钥（服务器托管）'
            : '官方密钥未配置';
          this.keyHint = cfg.company_key_available
            ? '已使用 Seedance 相同的火山方舟密钥（服务器托管）'
            : '服务器尚未检测到火山方舟密钥，请联系维护者';
        } else {
          keyInput.readOnly = false;
          keyInput.placeholder = '留空使用本地配置';
          if (Object.prototype.hasOwnProperty.call(providerKeys, provider)) {
            keyInput.value = providerKeys[provider];
          }
          this.keyHint = this._personalKeyHint;
        }
      }
      var seedInput = nbField('seed');
      var varySeedInput = nbField('vary_seed');
      if (seedInput) seedInput.disabled = !this.supportsSeed;
      if (varySeedInput) varySeedInput.disabled = !this.supportsSeed;
      this.applyReferenceImageLimit();
      var capTimer = this;
      setTimeout(function () { capTimer.applyModelCapabilities(); }, 0);
      // When restoring a saved draft/preset the form already carries this tab's
      // own aspect_ratio / image_size / etc. Re-applying provider defaults here
      // (async, after applyPreset filled the fields) would clobber them back to
      // defaults on every tab switch. skipDefaults lets the restore path keep
      // provider metadata (base_url/models) without overwriting form values.
      if (skipDefaults) return;
      var self = this;
      setTimeout(function () {
        if (self._draftLoaded) return;
        var defaults = cfg.defaults || {};
        for (var k in defaults) {
          if (!Object.prototype.hasOwnProperty.call(defaults, k)) continue;
          var v = defaults[k];
          var el = document.querySelector('#nb-form [name="' + k + '"]');
          if (!el || el.type === 'file') continue;
          if (el.type === 'checkbox') el.checked = !!v;
          else if (el.tagName === 'SELECT') {
            var opts = el.options;
            var found = false;
            for (var i = 0; i < opts.length; i++) { if (opts[i].value === String(v)) { found = true; break; } }
            if (found) el.value = v;
          } else el.value = v;
        }
        self.updateResizeState();
      });
    },

    providerKeyBucket() {
      var wsId = this.activeTabId || 'default';
      if (!this._providerKeys[wsId]) this._providerKeys[wsId] = {};
      return this._providerKeys[wsId];
    },

    applyReferenceImageLimit() {
      var limit = this.maxReferenceImages || 14;
      document.querySelectorAll('#nb-imageRefs .drop').forEach(function (drop) {
        var input = drop.querySelector && drop.querySelector('input[type="file"]');
        var match = input && input.name && input.name.match(/^image_(\d+)$/);
        var enabled = !match || Number(match[1]) <= limit;
        if (drop.style) drop.style.display = enabled ? '' : 'none';
        if (input) input.disabled = !enabled;
      });
    },

    applyModelCapabilities() {
      var provider = nbField('provider') ? nbField('provider').value : this.provider;
      var model = nbField('model') ? nbField('model').value : '';
      var caps = (window.ModelCapabilities && window.ModelCapabilities.capabilitiesFor)
        ? window.ModelCapabilities.capabilitiesFor(this.providers, provider, model)
        : null;
      this.modelCaps = caps;
      var hints = [];
      var maxRef = caps && caps.max_reference_images != null ? Number(caps.max_reference_images) : null;
      if (maxRef != null && this.maxReferenceImages !== maxRef) {
        this.maxReferenceImages = maxRef;
        this.applyReferenceImageLimit();
        hints.push('最多参考图 ' + maxRef + ' 张');
      }

      var sizeSel = nbField('image_size');
      if (sizeSel && caps && Array.isArray(caps.image_size) && caps.image_size.length) {
        var prev = sizeSel.value;
        var keep = caps.image_size.indexOf(prev) >= 0 ? prev : caps.image_size[0];
        // 纯数据驱动：option 由模板 v-for 渲染、选中值由 :value 绑定。
        // 此前命令式 innerHTML='' 重建 option 会清掉 petite-vue 的 v-for 块
        // DOM，而 tracked 块还在——下次 diff 对旧块 remove() 时 parentNode
        // 为 null，removeChild 崩溃拖死整个页面（2026-09-10 用户实锤）。
        this.imageSizeOptions = caps.image_size.slice();
        this.imageSize = keep;
        if (prev && prev !== keep) hints.push('尺寸 ' + prev + ' 不支持，已切换为 ' + keep);
      }

      var arSel = nbField('aspect_ratio');
      if (arSel && caps && Array.isArray(caps.aspect_ratio) && caps.aspect_ratio.length) {
        var ar = arSel.value;
        if (ar !== 'auto' && caps.aspect_ratio.indexOf(ar) < 0) {
          var fallback = caps.aspect_ratio[0];
          arSel.value = fallback;
          hints.push('比例 ' + ar + ' 不支持，已切换为 ' + fallback);
        }
      }

      this.modelHint = hints.join('；');
    },

    // ---- 8d. buildUploadSlots / wireDrops ----

    buildUploadSlots() {
      var ir = document.getElementById('nb-imageRefs');
      if (ir) {
        ir.innerHTML = '';
        for (var i = 1; i <= 14; i++) {
          makeDrop(ir, 'image_' + i, 'Image ' + i);
        }
      }
    },

    wireDrops() {
      var self = this;
      setTimeout(function () {
        document.querySelectorAll('#nb-app .drop').forEach(function (drop) {
          var input = drop.querySelector('input[type="file"]');
          if (input && !input.dataset.wired) {
            input.dataset.wired = '1';
            // makeDrop already calls wireFileDrop for basic change/drag/drop wiring.
            // Add savedMedia cleanup on remove button.
            var rmBtn = drop.querySelector('.removeMediaBtn');
            if (rmBtn) {
              rmBtn.addEventListener('click', function (e) {
                e.preventDefault(); e.stopPropagation();
                input.value = '';
                delete self.savedMedia[input.name];
                clearPreview(drop);
              });
            }
          }
        });
        self.applyReferenceImageLimit();
      }, 0);
    },

    async optimizePrompt() {
      var self = this;
      var ta = document.querySelector('textarea[name="prompt"]');
      var prompt = ta ? ta.value.trim() : '';
      if (!prompt) {
        self.optimizeError = '请先输入提示词';
        return;
      }
      self.optimizing = true;
      self.optimizeError = '';
      self.optimizedPrompt = '';
      var res = null;
      try {
        var rag = await fetch('/rag-assistant/api/rag/preflight', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: prompt, optimize: true }),
        }).then(function (r) { return r.json(); }).catch(function () { return null; });
        if (rag && rag.ok && rag.detected && rag.updated_prompt) {
          self.optimizedPrompt = rag.updated_prompt;
          self.optimizing = false;
          return;
        }
        var resp = await fetch('/director/api/optimize-prompt', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: prompt, mode: 'refine' }),
        });
        res = await resp.json();
      } catch (e) {
        res = null;
      }
      self.optimizing = false;
      if (res && res.ok && res.prompt) {
        self.optimizedPrompt = res.prompt;
      } else {
        self.optimizeError = (res && res.error) || '优化失败';
      }
    },

    applyOptimizedPrompt() {
      var ta = document.querySelector('textarea[name="prompt"]');
      if (ta && this.optimizedPrompt) {
        ta.value = this.optimizedPrompt;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
      }
      this.optimizedPrompt = '';
    },
    // ---- 8e. submit / pollJob / result display ----

    async submit() {
      var self = this;
      var ownerWorkspaceId = self.activeTabId;
      var ownerExists = function () { return self.tabs.some(function (t) { return t.id === ownerWorkspaceId; }); };
      var ownerCache = function () {
        if (!ownerExists()) return null;
        return (self._tabStateCache[ownerWorkspaceId] = self._tabStateCache[ownerWorkspaceId] || {});
      };
      var setOwnerState = function (name, value) {
        var cache = ownerCache();
        if (!cache) return;
        cache[name] = value;
        if (self.activeTabId === ownerWorkspaceId) self[name] = value;
      };
      if (self.submittingRequest) return;
      // 首次提交时请求系统通知权限（用户手势内调用才有效）
      requestNotifyPermission();
      var selectedProvider = nbField('provider') ? nbField('provider').value : self.provider;
      var selectedModel = nbField('model') ? nbField('model').value : '';
      // 本地模型未连接兜底：只要想用本地 ComfyUI 而网关不可达，就切回云端并
      // 拦截提交（草稿/旧缓存可能残留本地 provider/base_url，直接提交会打到
      // 127.0.0.1:8801 之类空端口）。
      if (!self.localReady && (selectedProvider === "comfyui_local"
          || /(^|:\/\/)127\.0\.0\.1(:|$)|\.local:8801/.test(String(self.baseUrl || '')))) {
        setOwnerState('submitting', false);
        setOwnerState('statusText', '本地模型未连接，请先启动本地模型，或手动选择云端模型后重试。');
        self.providerHint = '本地模型未连接，请先启动本地模型，或手动选择云端模型后重试。';
        return;
      }
      var hasReference = false;
      var refCount = 0;
      for (var ri = 1; ri <= 14; ri++) {
        var refInput = nbField('image_' + ri);
        if (refInput && refInput.files && refInput.files.length > 0) { hasReference = true; refCount++; }
        else if (self.savedMedia && self.savedMedia['image_' + ri]) { hasReference = true; refCount++; }
      }
      if (selectedProvider === 'comfyui_local' && selectedModel === 'qwen2511' && !hasReference) {
        setOwnerState('statusText', 'Qwen 2511 \u662F\u56FE\u7247\u7F16\u8F91\u6A21\u578B\uFF0C\u8BF7\u5148\u4E0A\u4F20\u81F3\u5C11\u4E00\u5F20\u53C2\u8003\u56FE\uFF1B\u5982\u9700\u6587\u751F\u56FE\u8BF7\u5207\u6362 Krea T2I\u3002');
        return;
      }
      var caps = self.modelCaps;
      if (caps) {
        var sizeSel = nbField('image_size');
        var arSel = nbField('aspect_ratio');
        var sizeVal = sizeSel ? sizeSel.value : '';
        var arVal = arSel ? arSel.value : '';
        var problems = [];
        if (Array.isArray(caps.image_size) && caps.image_size.length && sizeVal !== 'auto' && caps.image_size.indexOf(sizeVal) < 0) {
          problems.push('尺寸 ' + sizeVal + ' 不支持，可用：' + caps.image_size.join(' / '));
        }
        if (Array.isArray(caps.aspect_ratio) && caps.aspect_ratio.length && arVal !== 'auto' && caps.aspect_ratio.indexOf(arVal) < 0) {
          problems.push('比例 ' + arVal + ' 不支持，可用：' + caps.aspect_ratio.join(' / '));
        }
        if (caps.max_reference_images != null && refCount > Number(caps.max_reference_images)) {
          problems.push('最多支持 ' + caps.max_reference_images + ' 张参考图');
        }
        if (problems.length) {
          setOwnerState('statusText', problems.join('?'));
          return;
        }
      }
      var submissionToken = (self._topicSubmissionSeq[ownerWorkspaceId] || 0) + 1;
      self._topicSubmissionSeq[ownerWorkspaceId] = submissionToken;
      var delivery = {
        dirHandle: self.dirHandle,
        autoDownload: self.autoDownload,
        outputDir: self.outputDir,
      };
      var cache = ownerCache();
      cache._submissionToken = submissionToken;
      cache._activeJobId = null;
      delete cache._latestJob;
      setOwnerState('submitting', true);
      setOwnerState('submittingRequest', true);
      setOwnerState('statusText', '提交中');
      var resultsEl = document.getElementById('nb-results');
      var eventsEl = document.getElementById('nb-events');
      if (self.activeTabId === ownerWorkspaceId) {
        if (resultsEl) resultsEl.innerHTML = '';
        if (eventsEl) eventsEl.textContent = '';
      }
      setOwnerState('eventsText', '');

      // Auto-save workspace draft before submit
      if (self.isStandalone) {
        try { self.saveWorkspaceDraft(); } catch (e) { /* ignore */ }
      }

      var data = await self.formDataWithSavedMedia({ resizeImages: true });
      var res;
      try {
        res = await api(APP_PATH + '/api/jobs', 'POST', data, ownerWorkspaceId);
      } catch (e) {
        setOwnerState('submitting', false);
        setOwnerState('statusText', '提交失败：网络异常，请重试');
        return;
      } finally {
        setOwnerState('submittingRequest', false);
      }
      if (!res || res.error) {
        setOwnerState('submitting', false);
        setOwnerState('statusText', (res && res.error) || '提交失败');
        return;
      }
      if (!ownerExists()) return;
      self._restoredPollIds = self._restoredPollIds || {};
      self._restoredPollIds[res.job_id] = true;
      setOwnerState('statusText', '已提交，任务 ' + res.job_id + ' 在后台运行');
      try { self.loadActivity(); } catch (e) { /* ignore */ }
      self.pollJob(res.job_id, ownerWorkspaceId, delivery);
    },

    async pollJob(jobId, ownerWorkspaceId, delivery) {
      var self = this;
      var ownerWsId = ownerWorkspaceId || self.activeTabId;
      var ownerExists = function () { return self.tabs.some(function (t) { return t.id === ownerWsId; }); };
      var isActiveTab = function () { return self.activeTabId === ownerWsId; };
      var cache = function () { return (self._tabStateCache[ownerWsId] = self._tabStateCache[ownerWsId] || {}); };

      var MAX_FAILS = 15;
      var consecutiveFails = 0;

      while (true) {
        if (!ownerExists()) break;
        var r = await pollJobOnce(APP_PATH + '/api/jobs/' + jobId, ownerWsId);
        if (!ownerExists()) break;
        if (r.kind === 'gone') break;
        if (r.kind === 'error') {
          consecutiveFails++;
          if (consecutiveFails >= MAX_FAILS) break;
          var wait = Math.min(10000, 2500 * Math.pow(1.5, consecutiveFails - 1));
          await new Promise(function (res) { setTimeout(res, wait); });
          continue;
        }
        consecutiveFails = 0;
        var job = r.job;
        if (job.workspace_id && job.workspace_id !== ownerWsId) break;

        cache()._latestJob = job;
        if (isActiveTab()) self._upsertJob(job);

        if (isActiveTab() && self.selectedJobId === jobId) {
          self.eventsText = (job.events || []).map(function (e) { return '[' + (e.time || '') + '] ' + (e.message || ''); }).join('\n');
          self._renderJobToDom(job, jobId);
        }

        if (TERMINAL_STATUSES.has((job.status || '').toLowerCase())) {
          notifyJobDone(jobId, job.status, '图片生成');
          var deliveryNote = '';
          if (job.status === 'succeeded' && delivery && delivery.dirHandle) {
            var saved = await self.saveToClient(job, delivery.dirHandle);
            if (saved) deliveryNote = ' · 已保存 ' + saved + ' 个文件到 ' + delivery.outputDir;
          } else if (job.status === 'succeeded' && delivery && delivery.autoDownload) {
            var downloaded = self.triggerDownloads(job);
            if (downloaded) deliveryNote = ' · 已下载 ' + downloaded + ' 个文件';
          }
          if (isActiveTab() && self.selectedJobId === jobId) {
            var s = String(job.status || '').toLowerCase();
            if (['succeeded', 'success', 'completed'].indexOf(s) >= 0) {
              self.selectedJobLabel = '已完成' + ((job.results || []).length ? '（' + job.results.length + ' 个结果）' : '') + deliveryNote;
            } else if (s === 'failed' || s === 'failure') {
              self.selectedJobLabel = '失败 · ' + String(friendlyJobErrorHint(job) || '未记录原因').slice(0, 80);
            } else {
              self.selectedJobLabel = '已取消';
            }
          }
          break;
        }
        await new Promise(function (r) { setTimeout(r, 2500); });
      }
      try { self.loadActivity(); } catch (e) { /* ignore */ }
      self.loadJobs();
    },

    // Extracted from pollJob so that both live polling (from pollJob) and
    // tab-switch rehydration (from loadTargetTabState) can rebuild the DOM
    // from a job snapshot. Structure must match the original pollJob output
    // verbatim so downstream click handlers (._blobDownload via .dl-btn) still
    // work.
    // 取消任务（对齐画布上游 c701c97/2bb7466 的交互与兜底文案）：
    // 排队中直接取消；运行中弹确认（已计费提示）。后端无取消 API 时
    // 走「取消标志 + 轮询点退出 + 结果丢弃」兜底；409 = 任务已结束。
    async cancelJob(jobId, status) {
      this.statusText = '正在取消任务...';
      let res;
      try {
        res = await api(APP_PATH + '/api/jobs/' + encodeURIComponent(jobId) + '/cancel', 'POST', null, this.activeTabId);
      } catch (e) {
        this.statusText = '取消失败：' + (e && e.message ? e.message : e);
        return;
      }
      if (res && res.ok) {
        this.statusText = '任务已取消，输入和参数已保留。';
        var j = (this.jobs || []).find(function (x) { return (x.job_id || x.id) === jobId; });
        if (j) this._upsertJob(Object.assign({}, j, { status: 'cancelled' }));
      } else {
        this.statusText = (res && res.error) || '取消失败：网络异常，请重试';
      }
    },
    _renderJobToDom(job, jobId) {
      var resultsEl = document.getElementById('nb-results');
      if (!resultsEl) return;

      // Note: no eventsEl DOM write here. The original nano-banana pollJob
      // only updated the reactive `self.eventsText` (bound to {{ eventsText }}
      // in the template); setEvents() routes that value correctly whether the
      // owning tab is active or cached. Writing #nb-events directly was scope
      // creep in the Task 5 extraction.

      // 渲染去重（性能修复，与 seedance 同源）：运行期每 2.5s 轮询一次，
      // 旧实现每次轮询整卡重建，N 张结果图每 2.5 秒被重新请求/排版一次，
      // 弱机上就是「点提交之后页面一直很卡」的主因。现在状态卡按签名
      // 去重、结果卡只增量追加新图，旧结果原样保留。
      var state = (this._renderState = this._renderState || {});
      var statusBox = resultsEl.querySelector('.ui-job-status-card-wrap');
      var resultBox = resultsEl.querySelector('.ui-result-cards');
      if (!statusBox || !resultBox || this._renderedJobId !== jobId) {
        // 容器缺失（submit 清空/切主题）或渲染的是另一个任务 → 全量重建
        resultsEl.innerHTML = '';
        statusBox = document.createElement('div');
        statusBox.className = 'ui-job-status-card-wrap';
        resultBox = document.createElement('div');
        resultBox.className = 'ui-result-cards';
        resultsEl.appendChild(statusBox);
        resultsEl.appendChild(resultBox);
        this._renderedJobId = jobId;
        delete state[jobId];
      }

      var events = job.events || [];
      var lastEvent = events.length ? events[events.length - 1].time + events[events.length - 1].message : '';
      var statusSig = [job.status, job.done, job.total, events.length + ':' + lastEvent, (job.errors || []).join('|')].join('\u0001');
      var flat = [];
      (job.results || []).forEach(function (r) {
        (r.images || []).forEach(function (im) { flat.push({ im: im, runIndex: r.index }); });
      });
      var resultsSig = flat.map(function (x) { return (x.im.download_url || '') + '|' + (x.im.filename || ''); }).join('\u0001');
      var prev = state[jobId] || { statusSig: '', resultsSig: '', count: 0, errorsCount: 0 };
      state[jobId] = { statusSig: statusSig, resultsSig: resultsSig, count: flat.length, errorsCount: (job.errors || []).length };

      // 状态卡（进度/事件/错误提示/取消按钮）：内容变化才重建
      if (statusSig !== prev.statusSig) {
        var eventsList = events.slice(-8).map(function (e) {
          return '<div><span class="ui-job-status-card__event-time">' + escHtml(e.time) + '</span> ' + escHtml(e.message) + '</div>';
        }).join('');

        // 友好错误提示：识别错误类型，显示用户友好的消息
        // （friendlyJobErrorHint 返回原文兜底时与 job.errors[0] 相等，需转义）
        var errorHint = friendlyJobErrorHint(job);
        if (errorHint && errorHint === (job.errors || [])[0]) errorHint = escHtml(errorHint);

        statusBox.innerHTML = '<article class="ui-job-status-card ' + jobStatusClass(job.status) + '">' +
          '<div class="ui-job-status-card__title"><span class="ui-badge ui-badge--' + jobStatusBadgeTone(job.status) + '">' + jobStatusLabel(job.status) + '</span> · ' + (job.done || 0) + '/' + (job.total || 0)
          + (jobId && !TERMINAL_STATUSES.has(String(job.status || '').toLowerCase())
             ? '<button type="button" class="cancel-job-btn" onclick="window._app_nb.cancelJob(\'' + escHtml(jobId) + '\',\'' + escHtml(job.status || 'queued') + '\')">取消任务</button>'
             : '')
          + (jobId && (job.results || []).length && TERMINAL_STATUSES.has(String(job.status || '').toLowerCase())
             ? '<button type="button" class="cancel-job-btn" onclick="window._app_nb.downloadAll(\'' + escHtml(jobId) + '\')">下载全部 (' + job.results.length + ')</button>'
             : '')
          + (jobId && job.retryable
             ? '<button type="button" class="cancel-job-btn" onclick="window._app_nb.retryJob(\'' + escHtml(jobId) + '\')">重试</button>'
             : '')
          + '</div>' +
          (errorHint ? '<div class="ui-job-status-card__error">' + errorHint + '</div>' : '') +
          (eventsList ? '<div class="ui-job-status-card__events">' + eventsList + '</div>' : '<div class="ui-job-status-card__events">等待服务器响应...</div>') +
          '</article>';
      }

      // Keep tab-state rehydration usable with lightweight embedded/test DOM
      // shims that lack insertAdjacentHTML. Real browsers continue to use the
      // incremental path below, preserving already-loaded images.
      if (typeof resultBox.insertAdjacentHTML !== 'function') {
        let html = statusBox.innerHTML || '';
        for (var li = 0; li < flat.length; li++) {
          var lx = flat[li];
          var limg = lx.im;
          var lurl = APP_PATH + limg.download_url;
          html += '<article class="result ui-result-card"><img class="ui-result-card__media" src="' + lurl + '" alt="生成结果" onclick="openPreview(\'image\',\'' + lurl + '\')"><a href="' + lurl + '" class="dl-btn ui-result-card__download" data-url="' + lurl + '" data-filename="' + escHtml(limg.filename) + '">下载</a><div class="ui-result-card__meta">Run ' + lx.runIndex + '</div></article>';
        }
        for (var le = 0; le < (job.errors || []).length; le++) {
          html += '<article class="ui-alert ui-alert--danger" role="alert">' + escHtml(job.errors[le]) + '</article>';
        }
        resultsEl.innerHTML = html;
        return;
      }

      // 结果卡：任务结果只会越来越多，增量追加新图即可；旧结果原样保留，
      // 轮询不再反复重建 <img>。
      if (resultsSig !== prev.resultsSig) {
        for (var gi = prev.count; gi < flat.length; gi++) {
          var x = flat[gi];
          var img = x.im;
          var url = APP_PATH + img.download_url;
          var safeFn = escHtml(img.filename);
          // insertAdjacentHTML（而非 innerHTML +=）：+= 会把容器内全部
          // 子元素重新解析，已加载的 <img> 会再次被销毁重拉。
          resultBox.insertAdjacentHTML('beforeend', '<article class="result ui-result-card"><img class="ui-result-card__media" src="' + url + '" alt="生成结果" onclick="openPreview(\'image\',\'' + url + '\')"><a href="' + url + '" class="dl-btn ui-result-card__download" data-url="' + url + '" data-filename="' + safeFn + '">下载</a><div class="ui-result-card__meta">Run ' + x.runIndex + '</div></article>');
        }
        var errs = job.errors || [];
        for (var ei = prev.errorsCount; ei < errs.length; ei++) {
          // 原始报错默认折叠，友好提示已在上方状态卡展示
          resultBox.insertAdjacentHTML('beforeend',
            '<details class="ui-raw-error" style="margin:8px 0;font-size:12px;color:#697386">'
            + '<summary style="cursor:pointer">查看原始报错 ' + (ei + 1) + '/' + errs.length + '</summary>'
            + '<pre style="white-space:pre-wrap;margin:6px 0 0;padding:8px;background:#fff1f0;border-radius:6px;color:#b42318">'
            + escHtml(errs[ei]) + '</pre></details>');
        }
      }
    },

    _clearTopicResultDom() {
      var resultsEl = document.getElementById('nb-results');
      var eventsEl = document.getElementById('nb-events');
      if (resultsEl) resultsEl.innerHTML = '';
      if (eventsEl) eventsEl.textContent = '';
    },

    // === 任务矩阵：历史任务一个格子（缩略图 / 状态 / 下载 / 详情） ===
    renderJobsGrid() {
      var self = this;
      var grid = document.getElementById('nb-jobsGrid');
      if (!grid) return;
      // 内存任务 + 持久化活动记录（去重：活动里有而内存里没有的才并入）
      var liveIds = {};
      (self.jobs || []).forEach(function (j) { liveIds[j.job_id] = true; });
      var merged = (self.jobs || []).slice();
      (self._activityRecords || []).forEach(function (rec) {
        if (!rec || !rec.job_id || liveIds[rec.job_id]) return;
        if (merged.length >= 20) return;
        merged.push(rec);
      });
      var items = merged.slice(0, 20);
      var emptyEl = document.getElementById('nb-jobsEmpty');
      if (emptyEl) emptyEl.style.display = items.length ? 'none' : '';
      var seen = {};
      var frag = document.createDocumentFragment();
      items.forEach(function (j) {
        seen[j.job_id] = true;
        var tile = grid.querySelector('[data-jid="' + CSS.escape(j.job_id) + '"]');
        if (!tile) {
          tile = self._buildJobTile(j);
          frag.appendChild(tile);
        } else {
          var badge = tile.querySelector('.job-tile-badge');
          if (badge && badge.textContent !== (j.status || '?')) {
            badge.textContent = j.status || '?';
            badge.className = 'job-tile-badge ' + (j.status || '');
          }
          tile.dataset.status = j.status || '';
        }
      });
      if (frag.childNodes.length) grid.prepend(frag);
      Array.prototype.slice.call(grid.children).forEach(function (el) {
        if (!seen[el.dataset.jid]) el.remove();
      });
    },

    _buildJobTile(j) {
      var self = this;
      var first = null;
      var rawResults = j.results || (j.result && j.result.results) || [];
      rawResults.forEach(function (r) {
        if (!first && r.images && r.images.length && r.images[0].download_url) {
          first = r.images[0];
        }
      });
      // 活动记录（内存剪枝后并入）没有 results：用摘要里的 first_url
      if (!first && j.first_url) first = { download_url: j.first_url, filename: j.first_filename || 'image' };
      var tile = document.createElement('div');
      tile.className = 'job-tile';
      tile.dataset.jid = j.job_id;
      tile.dataset.status = j.status || '';

      var media = document.createElement('div');
      media.className = 'job-tile-media';
      if (first) {
        var url = APP_PATH + first.download_url;
        var img = document.createElement('img');
        img.src = url;
        img.loading = 'lazy';
        img.alt = '结果预览';
        img.title = '点开预览';
        img.addEventListener('click', function (e) { e.stopPropagation(); openPreview('image', url); });
        // 产出文件可能已被 14 天清理策略删除：加载失败换过期占位
        img.addEventListener('error', function () {
          img.remove();
          var ph2 = document.createElement('span');
          ph2.className = 'job-tile-ph';
          ph2.textContent = '🗑';
          ph2.title = '产出文件已过期（保留 14 天后自动清理）';
          media.appendChild(ph2);
        });
        media.appendChild(img);
      } else {
        var ph = document.createElement('span');
        ph.className = 'job-tile-ph';
        ph.textContent = (j.status === 'failed' || j.status === 'failure') ? '❌' : '⏳';
        media.appendChild(ph);
      }
      tile.appendChild(media);

      var meta = document.createElement('div');
      meta.className = 'job-tile-meta';
      var badge = document.createElement('span');
      badge.className = 'job-tile-badge ' + (j.status || '');
      badge.textContent = j.status || '?';
      meta.appendChild(badge);
      var time = document.createElement('span');
      time.className = 'job-tile-time';
      time.textContent = (j.created_at || '').slice(5, 16);
      meta.appendChild(time);
      tile.appendChild(meta);

      var prompt = document.createElement('div');
      prompt.className = 'job-tile-prompt';
      var promptText = j.prompt || j.title || ((j.request && j.request.values && j.request.values.prompt) || '');
      prompt.textContent = promptText || '';
      prompt.title = promptText || '';
      tile.appendChild(prompt);

      var foot = document.createElement('div');
      foot.className = 'job-tile-foot';
      if (first) {
        var dl = document.createElement('button');
        dl.type = 'button';
        dl.className = 'job-tile-btn job-tile-btn--dl';
        dl.textContent = '⬇ 下载';
        dl.addEventListener('click', function (e) { e.stopPropagation(); self._blobDownload(APP_PATH + first.download_url, first.filename || 'image'); });
        foot.appendChild(dl);
      }
      if (j.retryable) {
        var rt = document.createElement('button');
        rt.type = 'button';
        rt.className = 'job-tile-btn job-tile-btn--retry';
        rt.textContent = '重试';
        rt.addEventListener('click', function (e) { e.stopPropagation(); self.retryJob(j.job_id || j.id); });
        foot.appendChild(rt);
      }
      var dt = document.createElement('button');
      dt.type = 'button';
      dt.className = 'job-tile-btn job-tile-btn--detail';
      dt.textContent = '详情';
      dt.addEventListener('click', function (e) { e.stopPropagation(); self.openJobDetail(j.job_id); });
      foot.appendChild(dt);
      tile.appendChild(foot);

      tile.addEventListener('click', function () { self.openJobDetail(j.job_id, j.id); });
      return tile;
    },

    // === 任务详情弹窗：请求（参数）与返回（事件/结果/错误） ===
    // 运行中面板的「详情」入口：从当前 tab 的运行态缓存解析活跃任务 id
    openActiveJobDetail() {
      var cache = (this._tabStateCache || {})[this.activeTabId];
      var jid = cache && cache._activeJobId;
      if (!jid) {
        if (typeof window.portalToast === 'function') window.portalToast('当前没有运行中的任务', 'info');
        return;
      }
      var rec = null;
      (this._activityRecords || []).forEach(function (r) { if (!rec && r.job_id === jid) rec = r; });
      this.openJobDetail(jid, rec && rec.id);
    },

    async openJobDetail(jobId, activityId) {
      var self = this;
      var job = null;
      try { job = await api(APP_PATH + '/api/jobs/' + encodeURIComponent(jobId)); } catch (e) { job = null; }
      // 单任务接口返回的是原始 JOBS 条目（id 为键），列表接口才是 job_id——统一归一化
      if (job && !job.job_id && job.id) job.job_id = job.id;
      // 参数不在内存任务字典里（存于活动记录 request）——用 job_id 反查活动记录 id
      if (!activityId && jobId) {
        (self._activityRecords || []).forEach(function (r) { if (!activityId && r.job_id === jobId) activityId = r.id; });
      }
      // 内存任务已被剪枝（重启/超出上限）或参数缺失 → 回退/合并持久化活动记录
      var liveHasParams = !!(job && ((job.params && Object.keys(job.params).length) || (job.form && Object.keys(job.form).length)));
      if ((!job || !job.job_id || !liveHasParams) && activityId) {
        try {
          var rec = await api(APP_PATH + '/api/activity/' + encodeURIComponent(activityId));
          if (rec && !rec.error) {
            if (!job || !job.job_id) {
              job = rec;
              job.job_id = job.job_id || job.id || jobId;
              var result0 = job.result || {};
              job.results = job.results || result0.results || [];
              job.events = job.events || result0.events || [];
              job.errors = job.errors || result0.errors || [];
            } else {
              // live 任务存在但参数缺失：只补参数/提示词/模型，其余用 live 的实时数据
              var result1 = rec.result || {};
              if (!liveHasParams) job.params = (rec.request && rec.request.values) || {};
              if (!(job.events && job.events.length)) job.events = result1.events || [];
              if (!(job.errors && job.errors.length)) job.errors = result1.errors || [];
            }
            job.params = job.params || (job.request && job.request.values) || {};
            job.prompt = job.prompt || job.title || ((job.request && job.request.values && job.request.values.prompt) || '');
            job.model = job.model || ((job.request && job.request.values && job.request.values.model) || '');
          }
        } catch (e2) { /* 活动记录拿不到时保留 live 数据 */ }
      }
      if (!job || !job.job_id) {
        if (typeof window.portalToast === 'function') window.portalToast('任务详情获取失败（任务可能已被清理）', 'danger');
        else alert('任务详情获取失败（任务可能已被清理）');
        return;
      }
      self._renderJobDetail(job);
    },
    _renderJobDetail(job) {
      var self = this;
      var overlay = document.getElementById('nb-job-detail');
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'nb-job-detail';
        overlay.className = 'job-detail-backdrop';
        overlay.hidden = true;
        overlay.addEventListener('click', function (e) { if (e.target === overlay) self.closeJobDetail(); });
        overlay.innerHTML =
          '<div class="job-detail-box">' +
          '  <div class="job-detail-head">' +
          '    <span class="job-detail-title">任务详情</span>' +
          '    <button type="button" class="job-detail-close" title="关闭">✕</button>' +
          '  </div>' +
          '  <div class="job-detail-body"></div>' +
          '</div>';
        overlay.querySelector('.job-detail-close').addEventListener('click', function () { self.closeJobDetail(); });
        document.body.appendChild(overlay);
      }
      var body = overlay.querySelector('.job-detail-body');
      body.innerHTML = '';
      var add = function (cls, html) { var d = document.createElement('div'); d.className = cls; d.innerHTML = html; body.appendChild(d); return d; };

      add('job-detail-ids',
        '<span class="job-detail-status status-badge ' + escHtml(job.status || '') + '">' + escHtml(job.status || '?') + '</span>' +
        '<span class="job-detail-mono">' + escHtml(job.job_id || '') + '</span>' +
        '<span class="job-detail-mono">' + escHtml(job.created_at || '') + '</span>');
      const errText = (Array.isArray(job.errors) && job.errors.length)
  ? job.errors.join('\n')
  : (job.error ? String(job.error) : '');
      if (errText) {
        add('job-detail-errors', escHtml(errText));
      }

      var params = {};
      var src = job.params || job.form || {};
      for (var k in src) params[k] = src[k];
      for (var k2 in params) {
        if (/key|secret|token|password/i.test(k2)) delete params[k2];
      }
      if (!params.prompt) params.prompt = job.prompt || '';
      if (!params.model) params.model = job.model || '';
      add('job-detail-section', '<h4>请求（提交参数）</h4><pre class="job-detail-pre">' + escHtml(JSON.stringify(params, null, 2)) + '</pre>');

      var events = Array.isArray(job.events) ? job.events : [];
      var eventsText = events.length
        ? events.map(function (e) { return '[' + (e.time || '') + '] ' + (e.message || ''); }).join('\n')
        : '（无事件记录）';
      add('job-detail-section', '<h4>返回（执行过程）</h4><pre class="job-detail-pre">' + escHtml(eventsText) + '</pre>');

      var flat = [];
      (Array.isArray(job.results) ? job.results : []).forEach(function (r) {
        (r.images || []).forEach(function (im) { flat.push(im); });
      });
      if (flat.length) {
        var sec = add('job-detail-section', '<h4>返回（产出）</h4>');
        var rows = document.createElement('div');
        rows.className = 'job-detail-results';
        flat.forEach(function (im) {
          if (!im.download_url) return;
          var row = document.createElement('div');
          row.className = 'job-detail-result';
          var name = document.createElement('span');
          name.textContent = im.filename || 'image';
          row.appendChild(name);
          var dl = document.createElement('button');
          dl.type = 'button';
          dl.className = 'job-tile-btn job-tile-btn--dl';
          dl.textContent = '⬇ 下载';
          dl.addEventListener('click', function () { self._blobDownload(APP_PATH + im.download_url, im.filename || 'image'); });
          row.appendChild(dl);
          rows.appendChild(row);
        });
        sec.appendChild(rows);
      }
      overlay.hidden = false;
    },
    closeJobDetail() {
      var overlay = document.getElementById('nb-job-detail');
      if (overlay) overlay.hidden = true;
      this.selectedJobId = null;
      this.selectedJobLabel = '';
      this.eventsText = '';
      this._renderedJobId = null;
      this._clearTopicResultDom();
    },

    async loadJobs() {
      var self = this;
      try {
        var res = await api(APP_PATH + '/api/jobs');
        if (res && Array.isArray(res.jobs)) {
          self.jobs = res.jobs;
          // 持久化活动记录并入矩阵：内存 JOBS 会随重启/剪枝清空，历史在 activity_log
          try {
            var act = await api(APP_PATH + '/api/activity');
            self._activityRecords = (act && (act.records || act.items)) || [];
          } catch (e) { self._activityRecords = self._activityRecords || []; }
          self.renderJobsGrid();
          // A hard refresh used to reset the form to idle and expose the
          // activity/history view, while the actual image job kept running.
          // Rebuild the original lower running-task panel from /api/jobs and
          // resume its existing poll/cancel flow for every live workspace.
          var restored = self._restoredPollIds || (self._restoredPollIds = {});
          (self.jobs || []).filter(function (job) {
            return !TERMINAL_STATUSES.has((job.status || '').toLowerCase());
          }).forEach(function (job) {
            var wsId = job.workspace_id || self.activeTabId;
            var cache = self._tabStateCache[wsId] || (self._tabStateCache[wsId] = {});
            cache._latestJob = job;
            var jid = job.job_id || job.id;
            if (!restored[jid]) {
              restored[jid] = true;
              self.pollJob(jid, wsId);
            }
          });
        } else if (res && res.error) {
          // Silent on error to avoid spamming the 5s loop
          return;
        }
        if (self.tabs && self.tabs.length) {
          self.tabs.forEach(function (tab) {
            tab.running = (self.jobs || []).some(function (job) {
              return !TERMINAL_STATUSES.has((job.status || '').toLowerCase()) && job.workspace_id === tab.id;
            });
          });
        }
      } catch (e) { /* silent */ }
    },

    visibleJobs() {
      return (this.jobs || []).slice(0, this.jobsLimit);
    },

    jobImages(job) {
      var out = [];
      ((job && job.results) || []).forEach(function (r) {
        (r.images || []).forEach(function (im) {
          if (im.download_url) out.push(im);
        });
      });
      return out;
    },

    isCancellableJob: function (jobOrStatus) {
      var status = typeof jobOrStatus === 'string' ? jobOrStatus : (jobOrStatus && jobOrStatus.status);
      var normalized = String(status || '').toLowerCase();
      return Boolean(normalized) && !TERMINAL_STATUSES.has(normalized);
    },

    selectJob(jobId) {
      var cache = this._tabStateCache[this.activeTabId] || {};
      var job = (this.jobs || []).find(function (j) { return (j.job_id || j.id) === jobId; })
        || ((cache._latestJob && ((cache._latestJob.job_id || cache._latestJob.id) === jobId)) ? cache._latestJob : null);
      this.selectedJobId = jobId;
      this.selectedJobLabel = job ? ((job.status || 'queued') + ' · ' + String(job.prompt || '').slice(0, 40)) : '任务详情';
      this._renderedJobId = null;
      if (job) {
        this.eventsText = (job.events || []).map(function (e) { return '[' + (e.time || '') + '] ' + (e.message || ''); }).join('\n');
        this._renderJobToDom(job, jobId);
      } else {
        this.eventsText = '';
        this._clearTopicResultDom();
      }
    },

    _upsertJob(job) {
      try {
        var id = job.job_id || job.id;
        var list = (this.jobs || []).slice();
        var idx = list.findIndex(function (j) { return (j.job_id || j.id) === id; });
        if (idx >= 0) list.splice(idx, 1, job);
        else list.unshift(job);
        this.jobs = list;
      } catch (e) {
        this.jobs = [job].concat((this.jobs || []).filter(function (x) { return (x.job_id || x.id) !== id; }));
      }
    },

    // ---- 8f. saveToClient / triggerDownloads / _blobDownload ----

    async saveToClient(job, dirHandle) {
      try {
        var files = [];
        for (var ri = 0; ri < (job.results || []).length; ri++) {
          var r = job.results[ri];
          for (var ii = 0; ii < (r.images || []).length; ii++) {
            var img = r.images[ii];
            if (img.download_url) files.push({ url: APP_PATH + img.download_url, filename: img.filename });
          }
        }
        for (var fi = 0; fi < files.length; fi++) {
          var f = files[fi];
          var resp = await fetch(f.url);
          var blob = await resp.blob();
          var fh = await dirHandle.getFileHandle(f.filename, { create: true });
          var w = await fh.createWritable();
          await w.write(blob);
          await w.close();
        }
        return files.length;
      } catch (e) {
        console.warn('saveToClient failed:', e);
        return 0;
      }
    },

    triggerDownloads(job) {
      var urls = [];
      for (var ri = 0; ri < (job.results || []).length; ri++) {
        var r = job.results[ri];
        for (var ii = 0; ii < (r.images || []).length; ii++) {
          var img = r.images[ii];
          if (img.download_url) urls.push({ url: APP_PATH + img.download_url, filename: img.filename });
        }
      }
      for (var ui = 0; ui < urls.length; ui++) {
        this._blobDownload(urls[ui].url, urls[ui].filename);
      }
      return urls.length;
    },

    // 中断任务一键重试：后端按落盘参数重提新任务，前端切到新任务轮询
    async retryJob(jobId) {
      var ownerWsId = this.activeTabId;
      const res = await api(APP_PATH + '/api/jobs/' + encodeURIComponent(jobId) + '/retry', 'POST', null, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      if (!res || !res.ok) {
        this.statusText = (res && res.error) || '重试失败：网络异常，请稍后重试';
        return;
      }
      this.submitting = true;
      this.statusText = '已重试，任务 ' + res.job_id + ' 在后台运行';
      this.selectedJobId = res.job_id;
      this.selectedJobLabel = '已重试，任务 ' + res.job_id + ' 在后台运行';
      this._renderedJobId = null;
      this.eventsText = '';
      this._clearTopicResultDom();
      this._restoredPollIds = this._restoredPollIds || {};
      this._restoredPollIds[res.job_id] = true;
      try { this.loadJobs(); } catch (e) { /* ignore */ }
      this.pollJob(res.job_id, ownerWsId, {
        dirHandle: this.dirHandle, autoDownload: this.autoDownload, outputDir: this.outputDir,
      });
    },

    // 一键下载全部：错开 400ms 逐个下载，避免并发打满 Portal 代理缓冲
    downloadAll(jobId) {
      var app = this;
      var cache = this._tabStateCache[this.activeTabId];
      var job = (this.jobs || []).find(function (j) { return (j.id || j.job_id) === jobId; })
        || (cache && cache._latestJob && (cache._latestJob.id === jobId || cache._latestJob.job_id === jobId) && cache._latestJob);
      if (!job || !(job.results || []).length) {
        this.statusText = '没有可下载的结果';
        return;
      }
      var count = 0;
      (job.results || []).forEach(function (r, i) {
        var imgs = r.images || [];
        imgs.forEach(function (im, k) {
          if (im.download_url) {
            count += 1;
            setTimeout(function () {
              app._blobDownload(APP_PATH + im.download_url, im.filename || ('result-' + (im.index ?? k)));
            }, (i * imgs.length + k) * 400);
          }
        });
      });
      this.statusText = '开始下载 ' + count + ' 个结果…';
    },

    async _blobDownload(url, filename) {
      // fetch → blob → <a download> dodges the self-signed-cert trap (Chrome's
      // download manager re-validates out of page context and rejects our LAN
      // cert). Cost: whole file into memory, no native progress — so we stream
      // the response and render our own progress bar (window._dlProgress).
      var bar = window._dlProgress ? window._dlProgress.start(filename) : null;
      try {
        var resp = await fetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        var blob = bar ? await bar.readBlob(resp) : await resp.blob();
        var blobUrl = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function() { URL.revokeObjectURL(blobUrl); }, 1000);
        if (bar) bar.done();
      } catch (e) {
        if (bar) bar.fail();
        // 不回退 <a download> 直链：无效 token 会把 404/JSON 错误体存成 .txt 文件；
        // 明确提示失败原因，让用户知道文件可能已被清理。
        var _msg = '下载失败：文件可能已被清理或网络异常，请稍后重试';
        if (typeof window.portalToast === 'function') window.portalToast(_msg, 'danger');
        else alert(_msg);
      }
    },

    // ---- 8g. Output directory methods ----

    async chooseOutputDir() {
      // Delivery settings live in the per-topic cache, so a picker that resolves
      // after the user switched topics must not rewrite the new topic's target.
      var ownerWsId = this.activeTabId;
      var res = await api(APP_PATH + '/api/choose-output-dir', 'POST', null, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      if (res && res.path) { this.outputDir = res.path; this.dirHandle = null; return; }
      if (window.showDirectoryPicker) {
        try {
          var handle = await window.showDirectoryPicker({ mode: 'readwrite' });
          if (this.activeTabId !== ownerWsId) return;
          this.dirHandle = handle;
          this.outputDir = this.dirHandle.name;
          this.statusText = '已选择: ' + this.outputDir;
          return;
        } catch (e) { /* user cancelled */ }
        if (this.activeTabId !== ownerWsId) return;
      }
      this.autoDownload = true;
      this.outputDir = '浏览器下载';
      if (res && res.remote && !window.isSecureContext) {
        this.statusText = '提示：HTTPS 访问可启用目录选择功能';
      }
    },

    async desktopOutput() {
      var ownerWsId = this.activeTabId;
      var res = await api(APP_PATH + '/api/default-output-dir', 'GET', null, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      if (res && res.path) this.outputDir = res.path;
    },

    async openOutputDir() {
      if (this.dirHandle && !this.outputDir.includes('/')) {
        this.statusText = '文件将保存到 "' + this.outputDir + '"（浏览器限制无法代为打开）';
        return;
      }
      var ownerWsId = this.activeTabId;
      var data = new FormData(); data.set('output_dir', this.outputDir);
      var res = await api(APP_PATH + '/api/open-output-dir', 'POST', data, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      if (res && res.remote) this.statusText = '远程客户端不支持打开服务端目录';
    },

    async cleanCache() {
      var res = await api(APP_PATH + '/api/cleanup-cache', 'POST');
      if (res) alert('清理完成：素材 ' + (res.media_deleted || 0) + ' 个，日志 ' + (res.logs_deleted || 0) + ' 个');
    },

    // ---- 8h. 配置方案（存档）CRUD ----

    async loadArchives() {
      var ownerWsId = this.activeTabId;
      var res = await api(APP_PATH + '/api/archives', 'GET', null, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      this.archives = (res && res.archives) || [];
      this._normalizeSchemeSelection();
    },

    _normalizeSchemeSelection() {
      var list = this.archives || [];
      var exists = function (name) {
        return name === '默认方案' || list.some(function (a) { return a.name === name; });
      };
      if (!exists(this.currentSchemeName)) this.currentSchemeName = '默认方案';
      if (!exists(this.selectedArchive)) this.selectedArchive = '默认方案';
    },

    nextSchemeName() {
      var used = {};
      (this.archives || []).forEach(function (a) { used[a.name] = true; });
      var n = 1;
      while (used['方案' + n]) n++;
      return '方案' + n;
    },

    async _saveScheme(name) {
      var ownerWsId = this.activeTabId;
      var data = await this.formDataWithSavedMedia({});
      if (this.savedMedia && Object.keys(this.savedMedia).length) {
        data.set('saved_media', JSON.stringify(this.savedMedia));
      }
      data.set('archive_name', name);
      var res = await api(APP_PATH + '/api/archive/save', 'POST', data, ownerWsId);
      if (this.activeTabId !== ownerWsId) return false;
      if (!res || res.ok === false) {
        this.archiveHint = '保存失败：' + ((res && res.error) || '网络异常');
        return false;
      }
      if (res.media) this.savedMedia = res.media;
      window._currentSavedMedia = this.savedMedia;
      var savedName = res.archive || name;
      await this.loadArchives();
      if (this.activeTabId !== ownerWsId) return false;
      this.selectedArchive = savedName;
      this.currentSchemeName = savedName;
      this.isDirty = false;
      this.schemeNameInput = '';
      this.archiveHint = '已保存方案：' + savedName;
      this.saveWorkspaceDraft();
      return true;
    },

    async saveCurrentScheme() {
      var name = (this.schemeNameInput || '').trim() || this.nextSchemeName();
      await this._saveScheme(name);
    },

    async onSchemeSelect() {
      var name = this.selectedArchive;
      if (!name || name === this.currentSchemeName) return;
      if (!(await this._ensureNotDirty('加载方案'))) {
        this.selectedArchive = this.currentSchemeName;
        return;
      }
      if (name === '默认方案') {
        this.resetToFactoryDefaults();
      } else {
        await this.loadScheme(name);
      }
    },

    async loadScheme(name) {
      if (!name || name === '默认方案') { this.resetToFactoryDefaults(); return; }
      var ownerWsId = this.activeTabId;
      var data = new FormData(); data.set('archive_name', name);
      var res = await api(APP_PATH + '/api/archive/load', 'POST', data, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      if (!res || !res.values) { this.archiveHint = '读取失败'; return; }
      await this.applyPreset(res);
      this.currentSchemeName = name;
      this.isDirty = false;
      this.selectedArchive = name;
      this.saveWorkspaceDraft();
      this.archiveHint = '已加载方案：' + name;
    },

    async updateScheme() {
      var name = this.selectedArchive;
      if (!name || name === '默认方案') { this.archiveHint = '默认方案为只读，不可更新'; return; }
      await this._saveScheme(name);
    },

    async renameScheme() {
      var name = this.selectedArchive;
      if (!name || name === '默认方案') { this.archiveHint = '默认方案为只读，不可重命名'; return; }
      var newName = (prompt('请输入新的方案名', name) || '').trim();
      if (!newName || newName === name) return;
      var ownerWsId = this.activeTabId;
      var data = new FormData(); data.set('archive_name', name); data.set('new_name', newName);
      var res = await api(APP_PATH + '/api/archive/rename', 'POST', data, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      if (!res || res.ok === false) { this.archiveHint = '重命名失败：' + ((res && res.error) || '网络异常'); return; }
      var renamed = res.archive || name;
      await this.loadArchives();
      if (this.activeTabId !== ownerWsId) return;
      if (this.currentSchemeName === name) this.currentSchemeName = renamed;
      this.selectedArchive = renamed;
      this.archiveHint = '已重命名：' + renamed;
      this.saveWorkspaceDraft();
    },

    async deleteScheme() {
      var name = this.selectedArchive;
      if (!name) return;
      if (name === '默认方案') { this.archiveHint = '默认方案为只读，不可删除'; return; }
      if (!await confirmSafe('确定删除方案「' + name + '」？此操作不可恢复。')) return;
      var ownerWsId = this.activeTabId;
      var data = new FormData(); data.set('archive_name', name);
      var res = await api(APP_PATH + '/api/archive/delete', 'POST', data, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      if (!res || res.ok === false) {
        this.archiveHint = '删除失败：' + ((res && res.error) || '网络异常');
        return;
      }
      await this.loadArchives();
      if (this.activeTabId !== ownerWsId) return;
      if (this.currentSchemeName === name) {
        this.currentSchemeName = '默认方案';
        this.selectedArchive = '默认方案';
      }
      this.archiveHint = '已删除：' + name;
      this.saveWorkspaceDraft();
    },

    resetToFactoryDefaults() {
      try { localStorage.removeItem('nano-banana.workspace.' + this.activeTabId); } catch (e) {}

      var promptEl = document.querySelector('textarea[name="prompt"][form="nb-form"]');
      var keepPrompt = promptEl ? promptEl.value : '';
      var form = document.getElementById('nb-form');
      if (form) form.reset();
      if (promptEl) promptEl.value = keepPrompt;

      clearAllMediaInputs();
      this.savedMedia = {};
      window._currentSavedMedia = this.savedMedia;

      this.outputDir = '';
      this.dirHandle = null;
      this.autoDownload = false;

      this._draftLoaded = false;
      this.applyProvider(this._defaultProvider);

      this.currentSchemeName = '默认方案';
      this.isDirty = false;
      this.selectedArchive = '默认方案';
      this.schemeNameInput = '';
      this.archiveHint = '已恢复默认方案';

      var self = this;
      setTimeout(function () { self.saveWorkspaceDraft(); }, 120);
    },

    _ensureNotDirty(actionLabel) {
      var self = this;
      if (!this.isDirty) return Promise.resolve(true);
      return new Promise(function (resolve) {
        self._dirtyPending = { actionLabel: actionLabel || '切换', resolve: resolve };
        self._dirtyDialogOpen = true;
      });
    },

    async _dirtySaveAndContinue() {
      var pending = this._dirtyPending;
      this._dirtyDialogOpen = false;
      this._dirtyPending = null;
      if (!pending) return;
      var resolve = pending.resolve;
      if (this.currentSchemeName === '默认方案') { resolve(true); return; }
      var ok = await this._saveScheme(this.currentSchemeName);
      resolve(!!ok);
    },

    _dirtyDiscardAndContinue() {
      var pending = this._dirtyPending;
      this._dirtyDialogOpen = false;
      this._dirtyPending = null;
      this.isDirty = false;
      if (pending) pending.resolve(true);
    },

    _dirtyCancel() {
      var pending = this._dirtyPending;
      this._dirtyDialogOpen = false;
      this._dirtyPending = null;
      if (pending) pending.resolve(false);
    },
    // ---- 8i. Activity methods ----

    async loadActivity() {
      var ownerWsId = this.activeTabId;
      var res = await api(APP_PATH + '/api/activity', 'GET', null, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      this.activityRecords = (res && res.records) || [];
      this.activityCounts = (res && res.counts) || null;
      this.activityDetail = null;
    },

    formatRuntime: function (job) {
      var _ = this.runtimeTick;
      var start = job.started_at || job.submitted_at;
      if (!start) return '';
      var status = String(job.status || '').toLowerCase();
      var running = ['queued', 'pending', 'running', 'querying'].indexOf(status) >= 0;
      if (running) {
        var sec = Math.max(0, Math.floor(Date.now() / 1000 - start));
        return '已运行 ' + (sec >= 60 ? Math.floor(sec / 60) + '分' + (sec % 60) + '秒' : sec + '秒');
      }
      if (job.finished_at && job.started_at) {
        var sec2 = Math.max(0, Math.floor(job.finished_at - job.started_at));
        return '耗时 ' + (sec2 >= 60 ? Math.floor(sec2 / 60) + '分' + (sec2 % 60) + '秒' : sec2 + '秒');
      }
      return '';
    },

    async showDetail(id) {
      var ownerWsId = this.activeTabId;
      var res = await api(APP_PATH + '/api/activity/' + id, 'GET', null, ownerWsId);
      if (this.activeTabId !== ownerWsId) return;
      if (res) this.activityDetail = res;
    },

    restoreActivity() {
      var r = this.activityDetail && this.activityDetail.restore;
      if (!r) { alert('该记录无法恢复'); return; }
      this.applyPreset(r);
      if (r.values && r.values.provider && this.providers[r.values.provider]) {
        this.applyProvider(r.values.provider, true);
      }
      this.wsTab = 'jobs';
    },

    // ---- 8j. Preset / workspace methods ----

    applyPreset(preset) {
      clearAllMediaInputs();
      var values = (preset && preset.values) || {};
      if (values.provider && values.api_key !== undefined) {
        var presetProvider = this.providers[values.provider] || {};
        if (!presetProvider.company_key) {
          this.providerKeyBucket()[values.provider] = String(values.api_key || '');
        }
      }
      for (var k in values) {
        if (!Object.prototype.hasOwnProperty.call(values, k)) continue;
        var v = values[k];
        if (k === 'model') this.model = v;
        if (k === 'image_size') this.imageSize = v;
        var el = nbField(k);
        if (!el) continue;
        if (el.type === 'checkbox') {
          el.checked = ['1', 'true', 'yes', 'on'].includes(String(v).toLowerCase());
        } else if (el.type !== 'file') {
          el.value = v;
        }
      }
      // Sync reactive state for known v-model fields
      if (values.output_dir !== undefined) this.outputDir = values.output_dir;
      if (values.base_url !== undefined) this.baseUrl = values.base_url;
      if (values.workspace_name !== undefined) this.workspaceName = values.workspace_name;

      // Update provider if needed. skipDefaults: the draft's own field values
      // were just applied above; don't let applyProvider reset them to defaults.
      if (values.provider && this.providers[values.provider]) {
        this.applyProvider(values.provider, true);
      }

      // applyProvider() resets baseUrl to the provider default; restore the
      // saved base_url afterwards so a user-customized endpoint is preserved.
      if (values.base_url !== undefined) this.baseUrl = values.base_url;

      // Update resize state
      this.updateResizeState();

      // Capture the desired model / image_size; the actual DOM restore happens in the
      // microtask at the end of this method (see the comment there).
      var desiredModel = values.model;
      var desiredSize = values.image_size;
      var self = this;

      // Restore saved media
      var media = (preset && preset.media) || {};
      this.savedMedia = {};
      window._currentSavedMedia = this.savedMedia;
      for (var n in media) {
        if (!Object.prototype.hasOwnProperty.call(media, n)) continue;
        var item = media[n];
        this.savedMedia[n] = item;
        var inp = nbField(n);
        var drop = inp && inp.closest('.drop');
        if (drop && item.url) {
          showPreview(drop, n, resolveMediaUrl(item.url), item.filename);
        }
      }
      var count = Object.keys(this.savedMedia).length;
      if (count) this.archiveHint = '已读取保存配置：' + count + ' 张图';
      // Restore v-for / capability-rebuilt selects once PetiteVue has flushed the
      // option re-render triggered by applyProvider() above. Use a microtask so it
      // runs BEFORE applyProvider()'s own setTimeout(applyModelCapabilities) — which
      // otherwise fires first with the stale first-option model and rebuilds image_size
      // to that wrong model's list (e.g. gemini-3-pro-image -> ['1K','2K'], dropping
      // the saved '4K'). We set model + image_size first, then call
      // applyModelCapabilities() ourselves to narrow options against the correct model.
      return new Promise(function (resolve) {
        queueMicrotask(function () {
          var mEl = nbField('model');
          if (mEl && desiredModel != null && Array.prototype.some.call(mEl.options, function (o) { return o.value === desiredModel; })) {
            mEl.value = desiredModel;
            self.model = desiredModel;
          }
          if (desiredSize != null) {
            var sEl = nbField('image_size');
            if (sEl) {
              var want = String(desiredSize);
              if (!Array.prototype.some.call(sEl.options, function (o) { return o.value === want; })) {
                var opt = document.createElement('option');
                opt.value = want;
                opt.textContent = want;
                sEl.appendChild(opt);
              }
              sEl.value = want;
              self.imageSize = desiredSize;
            }
          }
          self.applyModelCapabilities();
          resolve();
        });
      });

    },


    async loadInitialPreset(ownerWorkspaceId) {
      var ownerWsId = ownerWorkspaceId || this.activeTabId;
      if (this.activeTabId !== ownerWsId) return;
      var factoryReset = false;
      try { factoryReset = sessionStorage.getItem('nano-banana.factoryReset') === '1'; } catch (e) {}
      if (factoryReset) {
        try { sessionStorage.removeItem('nano-banana.factoryReset'); } catch (e) {}
        this.currentSchemeName = '默认方案';
        this.isDirty = false;
        this.selectedArchive = '默认方案';
        return;
      }
      // Always prefer the per-tab workspace draft (localStorage). It holds this
      // tab's api_key / provider / prompt — which the server preset intentionally
      // strips (api_key is masked/omitted server-side). Gating this on
      // isStandalone meant that in the portal (isStandalone === false) every tab
      // switch re-fetched the empty server preset and wiped the form. Matches
      // seedance's loadPreset() draft-first behavior.
      if (this.loadWorkspaceDraft()) return;

      // Fall back to the server preset only when this tab has no local draft
      // yet (e.g. first visit on a fresh browser, or a tab restored from server).
      var res = await fetch(APP_PATH + '/api/preset?ws=' + encodeURIComponent(ownerWsId), { headers: { 'X-Workspace-Id': ownerWsId } });
      if (res.ok) {
        var data = await res.json();
        if (this.activeTabId === ownerWsId) this.applyPreset(data);
      }
    },

    // ---- Workspace System ----

    collectWorkspaceValues() {
      var form = document.getElementById('nb-form');
      var values = {};
      var collect = function (el) {
        if (!el || !el.name || el.type === 'file') return;
        values[el.name] = el.type === 'checkbox' ? (el.checked ? 'on' : '') : el.value;
      };
      if (form) {
        for (var i = 0; i < form.elements.length; i++) collect(form.elements[i]);
      }
      // prompt <textarea> and any other controls linked via form="nb-form" but
      // placed outside the <form> element must still be captured.
      var linked = document.querySelectorAll('[form="nb-form"]');
      for (var j = 0; j < linked.length; j++) {
        var el = linked[j];
        if (el.name && !(el.name in values)) collect(el);
      }
      return values;
    },

    mediaSnapshot(src) {
      src = src || this.savedMedia;
      return JSON.parse(JSON.stringify(src || {}));
    },

    localWorkspaceSnapshot() {
      var values = this.collectWorkspaceValues();
      // collectWorkspaceValues() reads the DOM, which can lag the reactive
      // state when this runs synchronously right after applyPreset() /
      // applyProvider() (v-model + v-for re-render are async). Pin the
      // authoritative values so refresh never resurrects a stale provider/base.
      values.provider = this.provider;
      values.base_url = this.baseUrl;
      if (this.model) values.model = this.model;
      if (this.imageSize) values.image_size = this.imageSize;
      return {
        name: this.workspaceName || '默认主题',
        values: values,
        media: this.mediaSnapshot(),
        saved_at: Date.now(),
        currentSchemeName: this.currentSchemeName,
        selectedArchive: this.selectedArchive,
      };
    },

    scheduleWorkspaceSave() {
      clearTimeout(this._workspaceSaveTimer);
      this._workspaceSaveTimer = setTimeout(() => this.saveWorkspaceDraft(), 500);
    },

    async saveWorkspaceDraft() {
      try {
        var payload = this.localWorkspaceSnapshot();
        // Key must track activeTabId so each tab's draft stays isolated.
        // Using this.workspaceId (fixed at init) caused all tabs to overwrite one another.
        var key = 'nano-banana.workspace.' + this.activeTabId;
        localStorage.setItem(key, JSON.stringify(payload));
        this.workspaceHint = '已保存草稿：' + (payload.name || '');
      } catch (e) {
        this.workspaceHint = '保存草稿失败';
      }
    },

    loadWorkspaceDraft() {
      // Key must track activeTabId — see saveWorkspaceDraft.
      var key = 'nano-banana.workspace.' + this.activeTabId;
      this.workspaceHint = '当前是独立主题页，可与其它主题并发提交';
      var raw = localStorage.getItem(key);
      if (!raw) return false;
      try {
        var draft = JSON.parse(raw);
        this.workspaceName = draft.name || this.workspaceName;
        this.applyPreset({ values: draft.values || {}, media: draft.media || {} });
        this.currentSchemeName = draft.currentSchemeName || '默认方案';
        this.selectedArchive = draft.selectedArchive || draft.currentSchemeName || '默认方案';
        this._normalizeSchemeSelection();
        this._draftLoaded = true;
        setTimeout(() => { this._draftLoaded = false; }, 250);
        this.workspaceHint = '已读取主题草稿：' + (this.workspaceName || '');
        return true;
      } catch (e) {
        return false;
      }
    },

    // ============================================================
    // TAB BAR METHODS (Task 4)
    // ============================================================
    saveTabsToLocalStorage() {
      localStorage.setItem('nano-banana.tabs', JSON.stringify({
        tabs: this.tabs.map(function (t) { return { id: t.id, name: t.name }; }),
        activeTabId: this.activeTabId,
      }));
    },

    async newTab() {
      if (!(await this._ensureNotDirty('新建任务'))) return;
      this.saveCurrentTabState();
      var id = 'ws-' + Date.now() + '-' + Math.random().toString(16).slice(2, 7);
      this.tabs.push({ id: id, name: '未命名主题', running: false });
      this.activeTabId = id;
      window._activeWorkspaceId = id;
      this.workspaceName = '';
      this.currentSchemeName = '默认方案';
      this.isDirty = false;
      this.schemeNameInput = '';
      this.savedMedia = {};
      this.outputDir = '';
      this.dirHandle = null;
      this.autoDownload = false;
      var form = document.querySelector('#nb-form');
      if (form) form.reset();
      // form.reset() clears file inputs' .files but not the preview <img>
      // that showPreview() manually injected into each .drop — mirror the cleanup
      // applyPreset() already does so the new tab starts truly blank.
      clearAllMediaInputs();
      this.statusText = '空闲';
      this.eventsText = '';
      this.submitting = false;
      this.selectedJobId = null;
      this.selectedJobLabel = '';
      this.submittingRequest = false;
      this._clearTopicResultDom();
      this.saveTabsToLocalStorage();
      var self = this;
      setTimeout(function () { self._scrollActiveTabIntoView(); }, 0);
    },

    async switchTab(id) {
      if (id === this.activeTabId || this.editingTabId) return;
      if (!(await this._ensureNotDirty('切换任务'))) return;
      this.saveCurrentTabState();
      this.activeTabId = id;
      window._activeWorkspaceId = id;
      this.loadTargetTabState();
      this.saveTabsToLocalStorage();
      var self = this;
      setTimeout(function () { self._scrollActiveTabIntoView(); }, 0);
    },

    startEditTab(id) { this.editingTabId = id; },

    finishEditTab(id, name) {
      var trimmed = (name || '').trim() || '未命名主题';
      var tab = this.tabs.find(function (t) { return t.id === id; });
      if (tab) {
        tab.name = trimmed;
        if (id === this.activeTabId) this.workspaceName = trimmed;
        if (typeof this.saveWorkspaceDraft === 'function') this.saveWorkspaceDraft();
        this.saveTabsToLocalStorage();
      }
      this.editingTabId = null;
    },

    closeTab(id) {
      var tab = this.tabs.find(function (t) { return t.id === id; });
      if (!tab || this.tabs.length <= 1) return;
      if (tab.running) { this._closeConfirmTabId = id; return; }
      this._forceCloseTab(id);
    },

    _forceCloseTab(id) {
      var idx = this.tabs.findIndex(function (t) { return t.id === id; });
      if (idx < 0 || this.tabs.length <= 1) return;
      this.tabs.splice(idx, 1);
      localStorage.removeItem('nano-banana.workspace.' + id);
      delete this._tabStateCache[id];
      if (this.activeTabId === id) {
        this.activeTabId = this.tabs[Math.max(0, idx - 1)].id;
        window._activeWorkspaceId = this.activeTabId;
        this.loadTargetTabState();
      }
      this.saveTabsToLocalStorage();
    },

    saveCurrentTabState() {
      var wsId = this.activeTabId;
      if (typeof this.saveWorkspaceDraft === 'function') this.saveWorkspaceDraft();
      // Preserve any fields already set on the cache (Task 5 will add job snapshots).
      this._tabStateCache[wsId] = Object.assign({}, this._tabStateCache[wsId] || {}, {
        statusText: this.statusText,
        eventsText: this.eventsText,
        submitting: this.submitting,
        selectedJobId: this.selectedJobId,
        selectedJobLabel: this.selectedJobLabel,
        baseUrl: this.baseUrl,
        provider: this.provider,
        models: this.models ? JSON.parse(JSON.stringify(this.models)) : [],
        workspaceName: this.workspaceName,
        outputDir: this.outputDir,
        dirHandle: this.dirHandle,
        autoDownload: this.autoDownload,
        currentSchemeName: this.currentSchemeName,
        isDirty: this.isDirty,
      });
    },

    loadTargetTabState() {
      var self = this;
      var wsId = this.activeTabId;
      var cache = this._tabStateCache[wsId] || {};
      this.statusText = cache.statusText || '空闲';
      this.eventsText = cache.eventsText || '';
      this.submitting = cache.submitting || false;
      this.selectedJobId = cache.selectedJobId || null;
      this.selectedJobLabel = cache.selectedJobLabel || '';
      if (cache.baseUrl !== undefined) this.baseUrl = cache.baseUrl;
      if (cache.provider !== undefined) {
        var savedProvider = cache.provider;
        this.provider = savedProvider;
        this._activeProvider = savedProvider;
      }
      if (cache.models !== undefined) this.models = cache.models;
      if (cache.workspaceName !== undefined) this.workspaceName = cache.workspaceName;
      this.outputDir = cache.outputDir !== undefined ? cache.outputDir : '';
      this.dirHandle = cache.dirHandle || null;
      this.autoDownload = cache.autoDownload || false;
      this.currentSchemeName = cache.currentSchemeName || '默认方案';
      this.isDirty = !!cache.isDirty;
      this.schemeNameInput = '';
      var form = document.querySelector('#nb-form');
      if (form) form.reset();
      this.savedMedia = {};
      if (typeof this.loadInitialPreset === 'function') this.loadInitialPreset();

      // If a background pollJob stashed a job snapshot for this tab, replay it
      // into the DOM. Otherwise clear any stale DOM left by the previous tab.
      // The cache key already proves ownership, so a snapshot predating backend
      // workspace_id persistence is still this tab's own result.
      if (this.selectedJobId) {
        var job = (this.jobs || []).find(function (j) { return (j.job_id || j.id) === this.selectedJobId; }.bind(this))
          || ((cache._latestJob && ((cache._latestJob.job_id || cache._latestJob.id) === this.selectedJobId)) ? cache._latestJob : null);
        if (job) {
          this.eventsText = (job.events || []).map(function (e) { return '[' + (e.time || '') + '] ' + (e.message || ''); }).join('\n');
          self._renderJobToDom(job, this.selectedJobId);
        } else {
          self._clearTopicResultDom();
        }
      } else {
        delete cache._latestJob;
        self._clearTopicResultDom();
      }
    },

    _scrollActiveTabIntoView() {
      var el = document.querySelector('.app-tab.active');
      if (el && el.scrollIntoView) el.scrollIntoView({ inline: 'nearest', block: 'nearest' });
    },

    // ---- 8k. Resize state / form data helpers ----

    updateResizeState() {
      var self = this;
      var reInput = nbField('resize_enabled');
      self.resizeEnabled = reInput ? reInput.checked : false;
      var controls = document.querySelector('.resizeControls');
      if (controls) {
        controls.classList.toggle('isDisabled', !self.resizeEnabled);
        controls.querySelectorAll('input, select').forEach(function (el) {
          el.disabled = !self.resizeEnabled;
        });
      }
    },

    async formDataWithSavedMedia(options) {
      options = options || {};
      var form = document.getElementById('nb-form');
      if (!form) return new FormData();
      var data = new FormData(form);
      appendDisabledResizeValues(data);
      var savedForBackend = {};
      for (var k in this.savedMedia) {
        if (Object.prototype.hasOwnProperty.call(this.savedMedia, k)) {
          savedForBackend[k] = this.savedMedia[k];
        }
      }
      var providerCfg = this.providers[this.provider] || {};
      if (providerCfg.company_key) data.delete('api_key');
      var maxRefs = Number(providerCfg.max_reference_images || 14);
      for (var refIndex = maxRefs + 1; refIndex <= 14; refIndex++) {
        data.delete('image_' + refIndex);
        delete savedForBackend['image_' + refIndex];
      }
      if (options.resizeImages) {
        var reInput = nbField('resize_enabled');
        var resizeEnabled = reInput ? reInput.checked : false;
        if (resizeEnabled) {
          for (var i = 1; i <= 14; i++) {
            var name = 'image_' + i;
            var input = nbField(name);
            var file = (input && input.files && input.files[0]) || null;
            if (!file && savedForBackend[name]) {
              file = await imageUrlToFile(resolveMediaUrl(savedForBackend[name].url), savedForBackend[name].filename);
            }
            if (!file) continue;
            var resized = await resizeImageFile(file);
            data.set(name, resized, resized.name);
            delete savedForBackend[name];
          }
        }
      }
      data.set('saved_media', JSON.stringify(savedForBackend));
      return data;
    },

    // ---- 8l. Preview dialog ----

    closePreview() {
      var dlg = document.getElementById('previewDialog');
      if (dlg) dlg.close();
    },

    onPreviewDialogClick(e) {
      if (e.target === e.currentTarget) e.target.close();
    },
  };
}

// ============================================================
// Module 9: Mount PetiteVue
// ============================================================
window.NanoBananaApp = NanoBananaApp;
PetiteVue.createApp({ NanoBananaApp }).mount();

// ============================================================
// Module 10: DOMContentLoaded — additional wiring
// ============================================================
document.addEventListener('DOMContentLoaded', function () {
  // Close preview dialog on Escape key
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var dlg = document.getElementById('previewDialog');
      if (dlg && dlg.open) dlg.close();
    }
  });
});

// === Download progress bar (shared, self-contained) ===================
// blob-download reads the whole file into browser memory with no native
// progress UI. This overlay reads the response as a stream and shows a
// bottom-of-screen bar ("已下载 42.0 / 180.0 MB") so users don't think it hung.
// Injects its own DOM+CSS on first use; concurrent downloads each get a row.
(function () {
  if (window._dlProgress) return;
  var MB = 1024 * 1024;
  var container = null;
  function ensureContainer() {
    if (container) return container;
    var style = document.createElement('style');
    style.textContent =
      '#_dlProgWrap{position:fixed;left:16px;bottom:16px;z-index:99999;display:flex;flex-direction:column;gap:8px;pointer-events:none}' +
      '#_dlProgWrap .dlp{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:240px;max-width:340px;box-shadow:var(--shadow-md);font-size:12px;pointer-events:auto}' +
      '#_dlProgWrap .dlp .name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:6px}' +
      '#_dlProgWrap .dlp .track{height:6px;background:var(--surface-sunken);border-radius:3px;overflow:hidden}' +
      '#_dlProgWrap .dlp .fill{height:100%;width:0;background:var(--accent);transition:width .15s ease}' +
      '#_dlProgWrap .dlp .txt{margin-top:5px;color:var(--muted);font-size:11px}' +
      '#_dlProgWrap .dlp.done .fill{background:var(--success)}' +
      '#_dlProgWrap .dlp.fail .fill{background:var(--danger)}';
    document.head.appendChild(style);
    container = document.createElement('div');
    container.id = '_dlProgWrap';
    document.body.appendChild(container);
    return container;
  }
  function fmt(bytes) { return (bytes / MB).toFixed(1); }
  window._dlProgress = {
    start: function (filename) {
      var wrap = ensureContainer();
      var row = document.createElement('div');
      row.className = 'dlp';
      row.innerHTML =
        '<div class="name">⬇ ' + (filename || '下载中') + '</div>' +
        '<div class="track"><div class="fill"></div></div>' +
        '<div class="txt">准备中…</div>';
      wrap.appendChild(row);
      var fill = row.querySelector('.fill');
      var txt = row.querySelector('.txt');
      var removed = false;
      function remove(delay) {
        if (removed) return; removed = true;
        setTimeout(function () { if (row.parentNode) row.parentNode.removeChild(row); }, delay);
      }
      return {
        readBlob: async function (resp) {
          var total = Number(resp.headers.get('Content-Length')) || 0;
          if (!resp.body || !resp.body.getReader) { txt.textContent = '下载中…'; return await resp.blob(); }
          var reader = resp.body.getReader();
          var chunks = [];
          var received = 0;
          for (;;) {
            var r = await reader.read();
            if (r.done) break;
            chunks.push(r.value);
            received += r.value.length;
            if (total) {
              var pct = Math.min(100, received / total * 100);
              fill.style.width = pct.toFixed(1) + '%';
              txt.textContent = '已下载 ' + fmt(received) + ' / ' + fmt(total) + ' MB (' + pct.toFixed(0) + '%)';
            } else {
              txt.textContent = '已下载 ' + fmt(received) + ' MB';
            }
          }
          // 保留响应 Content-Type：Blob 默认 text/plain 会让无扩展名文件
          // 被 Chrome 补成 .txt（下载 4MB 原图却存成 txt 的根因）
          return new Blob(chunks, { type: resp.headers.get('Content-Type') || 'application/octet-stream' });
        },
        done: function () {
          row.classList.add('done');
          fill.style.width = '100%';
          txt.textContent = '完成';
          remove(1200);
        },
        fail: function () {
          row.classList.add('fail');
          txt.textContent = '下载出错，已尝试直接下载';
          remove(2500);
        },
      };
    },
  };
})();
