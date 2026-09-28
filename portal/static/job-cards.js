/*!
 * job-cards.js — 依赖无关的任务卡片实现，供本仓库的 iframe 子应用共用。
 *
 * 为什么存在：seedance / nano-banana / portal / free-creation 各自写了一套卡片，
 * 是同构复制后各自演化 —— 结果「改一处另一处还是旧的」，字段还因为
 * `if (user) items.push(用户)` 变成了「每个任务属性个数都不一样」。
 * 这里收拢成单一源，由 tools/sync-ui.py 同步到各应用。
 *
 * 设计约束（见 docs/2026-09-28-shared-job-cards-design.md）：
 *   - 客户端零构建：必须能直接 <script src> 引入，故为 ES5 + IIFE。
 *   - 平台通用：**不假设自己在 iframe 里**，appPath 由调用方给或从路径推断。
 *   - 差异注入：缩略图渲染与预览行为由调用方以回调提供（图片与视频本质不同）。
 */
(function (global) {
  'use strict';

  /** 终态集合。**必须含 'interrupted'** —— 卡片会画「已中断」样式，
   *  它若不在这里，已中断的任务会被误判成「可取消」（2026-09-28 修掉的真 bug）。 */
  var TERMINAL_STATUSES = [
    'succeeded', 'success', 'failed', 'fail', 'failure',
    'cancelled', 'canceled', 'interrupted'
  ];

  function isTerminal(status) {
    return TERMINAL_STATUSES.indexOf(String(status || '').toLowerCase()) >= 0;
  }

  /** 后端对每个非终态任务都接受取消，UI 与这个契约保持一致。 */
  function isCancellableJob(jobOrStatus) {
    var status = typeof jobOrStatus === 'string' ? jobOrStatus : (jobOrStatus && jobOrStatus.status);
    var n = String(status || '').toLowerCase();
    return Boolean(n) && !isTerminal(n);
  }

  /** 卡片/徽标上显示的状态。**已有产出但状态还没走到终态时按 succeeded 显示**，
   *  否则会出现「图都已经出来了，卡片还写着运行中」。 */
  function jobDisplayStatus(job) {
    var raw = String((job && job.status) || '').toLowerCase();
    var hasResult = !!(job && (
      (job.results && job.results.length)
      || (job.result && job.result.results && job.result.results.length)
      || job.first_url
    ));
    if (hasResult && raw && !isTerminal(raw)) return 'succeeded';
    return raw;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function jobRequestValues(job) {
    return (job && (job.params || job.form)) || ((job && job.request && job.request.values) || {});
  }

  /** 实际种子。nano-banana / seedance 目前都还没把 actual seed 回写进任务，
   *  所以多数情况取不到 → 详情弹窗显示「随机」。等后端带上真值，这里自动生效。 */
  function jobSeed(job) {
    var p = jobRequestValues(job);
    var result = (job && job.result) || {};
    return String(p.seed || result.seed || (job && job.seed) || '').trim();
  }

  /** 从路径推断应用挂载前缀，兼容 Portal 代理与独立模式。 */
  function inferAppPath(pathname) {
    var p = String(pathname || (typeof location !== 'undefined' ? location.pathname : ''));
    var m = p.match(/^\/(seedance|nano-banana|dreamina|volcengine-portrait|rag-assistant)\//);
    return m ? '/' + m[1] : '';
  }

  /**
   * 详情字段的**纯数据**形式。
   *
   * 这是字段定义的唯一来源：命令式建 DOM 的 iframe 应用（seedance / nano-banana）
   * 用 renderDetailHtml 把它拼成 HTML；而 Portal 用 petite-vue 声明式模板，
   * 吃不了 HTML 串，就用 v-for 遍历这份数组。两边因此共用同一份字段定义，
   * 不会各自演化。
   *
   * 字段恒为四格、顺序固定：Seed / 运行耗时 / 使用模型 / 用户。
   * 空值一律以 '—' 呈现，**绝不省略整格**。
   */
  function detailFields(job, config) {
    var cfg = config || {};
    var values = jobRequestValues(job);
    var user = String((job && job.username) || '').trim();
    var runtimeText = typeof cfg.formatRuntime === 'function'
      ? cfg.formatRuntime(job)
      : (job && job.runtime_text);
    // 模型名同理可注入：各应用会把接入点 id 翻成可读 label（例如
    // ep-20260912121809-6wt62 → 「Seedream 5.0 Pro」，火山通道尤其需要），
    // 这套映射依赖各应用自己的模型目录，共享库不重复实现。
    var modelText = typeof cfg.formatModel === 'function'
      ? cfg.formatModel(job)
      : String((job && job.model) || values.custom_model || values.model || '');
    return [
      { label: 'Seed', value: jobSeed(job) || '随机', key: 'seed' },
      { label: '运行耗时', value: runtimeText || '未记录', key: 'runtime' },
      { label: '使用模型', value: modelText || '未记录', key: 'model' },
      { label: '用户', value: user || '—', key: 'user' }
    ];
  }

  /**
   * 详情弹窗的 meta 网格 HTML（命令式 DOM 的应用用这个）。
   * 字段定义来自 detailFields，与 Portal 的模板渲染共用同一份。
   */
  function renderDetailHtml(job, config) {
    return detailFields(job, config || {}).map(function (item) {
      return '<div class="job-detail-meta-item">'
        + '<span class="job-detail-meta-label">' + esc(item.label) + '</span>'
        + '<span class="job-detail-meta-value"' + (item.key === 'runtime' ? ' data-meta="runtime"' : '')
        + ' title="' + esc(item.value) + '">' + esc(item.value) + '</span>'
        + '</div>';
    }).join('');
  }

  /**
   * 建一个绑定了 config 的卡片实例。
   * 配置项全部可缺省：回调缺失时降级为 no-op，appPath 缺失时从路径推断 —— 
   * 调用方不该因为少传一个字段就崩。
   */
  function create(config) {
    var cfg = config || {};
    if (!cfg.appPath) cfg.appPath = inferAppPath();
    if (!cfg.thumbKind) cfg.thumbKind = 'image';
    if (typeof cfg.openPreview !== 'function') cfg.openPreview = function () {};
    if (typeof cfg.onCancel !== 'function') cfg.onCancel = function () {};
    return {
      config: cfg,
      renderDetailHtml: function (job) { return renderDetailHtml(job, cfg); }
    };
  }

  global.JobCards = {
    TERMINAL_STATUSES: TERMINAL_STATUSES,
    isTerminal: isTerminal,
    isCancellableJob: isCancellableJob,
    jobDisplayStatus: jobDisplayStatus,
    detailFields: detailFields,
    renderDetailHtml: renderDetailHtml,
    inferAppPath: inferAppPath,
    create: create
  };
})(typeof window !== 'undefined' ? window : this);
