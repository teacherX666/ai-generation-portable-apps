// Portal API / workspace helpers shared across the shell and sub-apps.
// Plain globals, no build step, so the same script works on Windows and macOS.
(function () {
  window.workspaceId = function () {
    let id = localStorage.getItem('workspace_id');
    if (!id) { id = crypto.randomUUID(); localStorage.setItem('workspace_id', id); }
    return id;
  };

  window.api = async function (url, method, body) {
    try {
      const opts = { method: method || 'GET', headers: { 'X-Workspace-Id': window.workspaceId() } };
      if (body) opts.body = body;
      const res = await fetch(url, opts);
      return await res.json();
    } catch (e) { return null; }
  };
})();