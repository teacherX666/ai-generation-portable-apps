// Portal API / workspace helpers shared across the shell and sub-apps.
// Plain globals, no build step, so the same script works on Windows and macOS.
(function () {
  window.workspaceId = function () {
    let id = localStorage.getItem('workspace_id');
    if (!id) { id = (window.crypto && typeof crypto.randomUUID === 'function') ? crypto.randomUUID() : ('ws-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10)); localStorage.setItem('workspace_id', id); }
    return id;
  };

  // ⚠️ 会话凭据必须与登录页（login.html）**用同一套**，否则会出现
  // 「登录页判已登录 → 跳 / ；外壳判未登录 → 跳 /login」的死循环：
  // 服务端取值顺序是 cookie 优先、其次 X-Session，只要两边提供的凭据不同
  // （cookie 失效但 localStorage token 有效，或反之），判断就会相反。
  // 2026-10-13 实测：切换 Portal 实例后 cookie 属于旧实例、localStorage 属于新实例 → 无限刷新。
  window.portalSessionToken = function () {
    try { return localStorage.getItem('portal_session') || ''; } catch (e) { return ''; }
  };

  window.api = async function (url, method, body) {
    try {
      const headers = { 'X-Workspace-Id': window.workspaceId() };
      const sess = window.portalSessionToken();
      if (sess) headers['X-Session'] = sess;
      const opts = { method: method || 'GET', headers, credentials: 'same-origin' };
      if (body) opts.body = body;
      const res = await fetch(url, opts);
      const data = await res.json().catch(function () { return null; });
      // 只对 Portal 自己的鉴权接口清理：子应用 API 的 401 与会话无关，误清会把用户踢下线
      if (res.status === 401 && /\/api\/auth\//.test(url)) {
        try { localStorage.removeItem('portal_session'); } catch (e) {}
      }
      return data;
    } catch (e) { return null; }
  };
})();