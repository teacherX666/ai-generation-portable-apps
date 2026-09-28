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

  global.JobCards = {
    TERMINAL_STATUSES: TERMINAL_STATUSES,
    isTerminal: isTerminal,
    isCancellableJob: isCancellableJob,
    jobDisplayStatus: jobDisplayStatus
  };
})(typeof window !== 'undefined' ? window : this);
