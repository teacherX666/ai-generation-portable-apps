// Pure Portal utilities shared across the shell and sub-apps.
// Kept as plain globals (no modules, no build) so Windows/Mac/Linux all load
// the same way through <script> tags.
(function () {
  window.escHtml = function (s) {
    return s ? String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : '';
  };

  window.jobStatusLabel = function (status) {
    const map = {
      queued: '排队中', pending: '等待中', running: '处理中', querying: '查询中',
      succeeded: '已完成', success: '已完成', completed: '已完成',
      failed: '失败', failure: '失败', cancelled: '已取消', canceled: '已取消',
    };
    return map[String(status || '').toLowerCase()] || String(status || '未知');
  };

  window.jobStatusClass = function (status) {
    const s = String(status || '').toLowerCase();
    if (['succeeded', 'success', 'completed'].includes(s)) return 'is-success';
    if (['failed', 'failure'].includes(s)) return 'is-failed';
    if (['pending', 'queued'].includes(s)) return 'is-pending';
    if (s === 'querying') return 'is-querying';
    return 'is-running';
  };

  window.jobStatusBadgeTone = function (status) {
    const state = window.jobStatusClass(status);
    return state === 'is-success' ? 'success' : state === 'is-failed' ? 'danger' : state === 'is-pending' ? 'warning' : 'info';
  };
})();