// === Portal-level feedback and first-run help ===
(function () {
  function ensureToastStack() {
    let stack = document.getElementById('portalToastStack');
    if (stack) return stack;
    stack = document.createElement('div');
    stack.id = 'portalToastStack';
    stack.className = 'portal-toast-stack';
    stack.setAttribute('aria-live', 'polite');
    document.body.appendChild(stack);
    return stack;
  }

  window.portalToast = function (message, tone = 'info') {
    if (!message) return;
    const stack = ensureToastStack();
    const toast = document.createElement('div');
    toast.className = 'portal-toast' + (tone ? ' is-' + tone : '');
    toast.setAttribute('role', tone === 'danger' ? 'alert' : 'status');
    const text = document.createElement('span');
    text.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'portal-toast__close';
    close.setAttribute('aria-label', '关闭提示');
    close.textContent = '×';
    close.addEventListener('click', () => toast.remove());
    toast.append(text, close);
    stack.appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
  };

  window.portalConfirm = function (message, opts = {}) {
    return new Promise((resolve) => {
      const title = opts.title || '请确认';
      const confirmText = opts.confirmText || '确认';
      const cancelText = opts.cancelText || '取消';
      const danger = !!opts.danger;
      const backdrop = document.createElement('div');
      backdrop.className = 'portal-modal-backdrop';
      const modal = document.createElement('div');
      modal.className = 'portal-modal';
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');
      const heading = document.createElement('h3');
      heading.textContent = title;
      const body = document.createElement('p');
      body.textContent = message;
      const actions = document.createElement('div');
      actions.className = 'portal-modal__actions';
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'portal-modal__btn';
      cancelBtn.textContent = cancelText;
      const confirmBtn = document.createElement('button');
      confirmBtn.type = 'button';
      confirmBtn.className = 'portal-modal__btn ' + (danger ? 'portal-modal__btn--danger' : 'portal-modal__btn--primary');
      confirmBtn.textContent = confirmText;
      actions.append(cancelBtn, confirmBtn);
      modal.append(heading, body, actions);
      backdrop.appendChild(modal);
      document.body.appendChild(backdrop);

      let settled = false;
      function close(value) {
        if (settled) return;
        settled = true;
        backdrop.remove();
        document.removeEventListener('keydown', onKeydown);
        resolve(value);
      }
      function onKeydown(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(false); }
      }
      cancelBtn.addEventListener('click', () => close(false));
      confirmBtn.addEventListener('click', () => close(true));
      backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(false); });
      document.addEventListener('keydown', onKeydown);
      requestAnimationFrame(() => (danger ? confirmBtn : cancelBtn).focus());
    });
  };

  window.portalPrompt = function (message, opts = {}) {
    return new Promise((resolve) => {
      const title = opts.title || '请输入';
      const value = opts.value || '';
      const placeholder = opts.placeholder || '';
      const confirmText = opts.confirmText || '确定';
      const cancelText = opts.cancelText || '取消';
      const backdrop = document.createElement('div');
      backdrop.className = 'portal-modal-backdrop';
      const modal = document.createElement('div');
      modal.className = 'portal-modal';
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');
      const heading = document.createElement('h3');
      heading.textContent = title;
      const body = document.createElement('p');
      body.textContent = message;
      const field = document.createElement('input');
      field.type = 'text';
      field.className = 'portal-modal__input';
      field.value = value;
      if (placeholder) field.placeholder = placeholder;
      const actions = document.createElement('div');
      actions.className = 'portal-modal__actions';
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'portal-modal__btn';
      cancelBtn.textContent = cancelText;
      const confirmBtn = document.createElement('button');
      confirmBtn.type = 'button';
      confirmBtn.className = 'portal-modal__btn portal-modal__btn--primary';
      confirmBtn.textContent = confirmText;
      actions.append(cancelBtn, confirmBtn);
      modal.append(heading, body, field, actions);
      backdrop.appendChild(modal);
      document.body.appendChild(backdrop);

      let settled = false;
      function close(result) {
        if (settled) return;
        settled = true;
        backdrop.remove();
        document.removeEventListener('keydown', onKeydown);
        resolve(result);
      }
      function onKeydown(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(null); }
        else if (e.key === 'Enter') { e.preventDefault(); close(field.value); }
      }
      cancelBtn.addEventListener('click', () => close(null));
      confirmBtn.addEventListener('click', () => close(field.value));
      backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(null); });
      field.addEventListener('keydown', onKeydown);
      document.addEventListener('keydown', onKeydown);
      requestAnimationFrame(() => { field.focus(); field.select(); });
    });
  };

  function switchPortalTab(name) {
    const btn = document.querySelector('.app-tab[data-tab="' + name + '"]');
    if (btn) activatePortalTab(btn);
  }

  function ensureHelpDialog() {
    let dialog = document.getElementById('portalHelpDialog');
    if (dialog) return dialog;
    dialog = document.createElement('dialog');
    dialog.id = 'portalHelpDialog';
    dialog.className = 'portal-help-dialog';
    dialog.innerHTML = `
      <div class="portal-help-head">
        <h2>新手指南</h2>
        <button class="portal-help-close" type="button" data-close>关闭</button>
      </div>
      <p>按下面顺序走一遍，就能完成你的第一个图片和视频任务。</p>
      <div class="portal-help-steps">
        <div class="portal-help-step"><span class="portal-help-step__num">1</span><div><b>配置 API Key</b><span>先把个人密钥保存到密钥库，再粘贴到创作模块。</span><button type="button" data-goto="keys">去配置密钥</button></div></div>
        <div class="portal-help-step"><span class="portal-help-step__num">2</span><div><b>跑一张测试图</b><span>在图片生成模块用一句简单提示词生成一张图。</span><button type="button" data-goto="nb">去生成图片</button></div></div>
        <div class="portal-help-step"><span class="portal-help-step__num">3</span><div><b>跑一个短视频</b><span>在视频生成模块用同一句提示词生成 4–5 秒视频。</span><button type="button" data-goto="seedance">去生成视频</button></div></div>
        <div class="portal-help-step"><span class="portal-help-step__num">4</span><div><b>查看历史记录</b><span>确认图片和视频都能在这里找到并下载。</span><button type="button" data-goto="history">查看历史</button></div></div>
        <div class="portal-help-step"><span class="portal-help-step__num">5</span><div><b>有报错问助手</b><span>把报错文字或截图贴给报错助手。</span><button type="button" data-goto="rag-assistant">去问报错助手</button></div></div>
      </div>
    `;
    document.body.appendChild(dialog);
    dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
    dialog.querySelectorAll('[data-goto]').forEach((btn) => {
      btn.addEventListener('click', () => {
        switchPortalTab(btn.dataset.goto);
        dialog.close();
      });
    });
    return dialog;
  }

  const helpBtn = document.getElementById('helpBtn');
  if (helpBtn) {
    helpBtn.addEventListener('click', () => ensureHelpDialog().showModal());
  }

  window.portalHelp = ensureHelpDialog;

  function maybeFirstRunHelp() {
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      if (location.pathname.startsWith('/login')) { clearInterval(timer); return; }
      if (localStorage.getItem('portal_onboarded') === '1') { clearInterval(timer); return; }
      const label = document.getElementById('userLabel');
      if (label && label.textContent) {
        if (document.body.classList.contains('portal-home-active')) return;
        clearInterval(timer);
        ensureHelpDialog().showModal();
      } else if (tries >= 20) {
        clearInterval(timer);
      }
    }, 300);
  }

  const helpDialog = ensureHelpDialog();
  helpDialog.addEventListener('close', () => {
    localStorage.setItem('portal_onboarded', '1');
    if (window.portalAnalytics) window.portalAnalytics.track('onboarding_completed');
  });

  setTimeout(maybeFirstRunHelp, 600);
})();

// === Portal job notification presenter ===
(function () {
  let _notifiedJobs = null;
  function _notifyLoadSeen() {
    if (_notifiedJobs) return _notifiedJobs;
    try { _notifiedJobs = JSON.parse(localStorage.getItem('aiPortal.notifiedJobs') || '{}') || {}; }
    catch (e) { _notifiedJobs = {}; }
    return _notifiedJobs;
  }
  function normalizeNotifyStatus(s) {
    if (['succeeded', 'success', 'completed'].includes(s)) return 'succeeded';
    if (['failed', 'fail', 'failure'].includes(s)) return 'failed';
    if (['cancelled', 'canceled'].includes(s)) return 'cancelled';
    return s;
  }
  window.__requestNotifyPermission = function () {
    try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}
  };

  // === 页面内任务完成弹窗（主提醒通道）===
  // 系统通知不够显眼（用户盯着页面时根本看不到），改为顶部滑入的
  // 卡片弹窗：带状态色、应用名、「查看结果」直达对应 tab。页面切走
  // （document.hidden）时才补系统 Notification + 标题闪烁兜底。
  function _notifyEsc(s) { return s ? String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : ''; }
  let _notifyPopStyles = false;
  function _notifyEnsureStyles() {
    if (_notifyPopStyles) return;
    _notifyPopStyles = true;
    const style = document.createElement('style');
    style.textContent = [
      '#portalNotifyStack{position:fixed;top:64px;right:16px;z-index:999999;display:flex;flex-direction:column;gap:10px;pointer-events:none;transition:right .2s ease}',
      // 导演台展开时右侧 320px 被占：弹窗自动左移到导演台左侧，避免视觉重叠
      'body.director-open #portalNotifyStack{right:356px}',
      '.portal-notify-pop{pointer-events:auto;width:340px;max-width:calc(100vw - 32px);background:var(--surface,#fff);color:var(--text,#172033);border:1px solid var(--border,#d9e0ea);border-left:4px solid #10b981;border-radius:10px;box-shadow:0 12px 32px rgba(20,32,51,.18);padding:12px 14px;font-size:13px;animation:portalNotifyIn .28s cubic-bezier(.2,.9,.3,1.2)}',
      '.portal-notify-pop.is-bad{border-left-color:#ef4444}',
      '.portal-notify-pop.is-cancel{border-left-color:#f59e0b}',
      '@keyframes portalNotifyIn{from{transform:translateX(30px);opacity:0}to{transform:translateX(0);opacity:1}}',
      '.portal-notify-pop__head{display:flex;align-items:center;gap:8px;font-weight:700}',
      '.portal-notify-pop__icon{font-size:16px}',
      '.portal-notify-pop__title{flex:1}',
      '.portal-notify-pop__close{border:0;background:none;color:var(--text-secondary,#475569);font-size:16px;cursor:pointer;padding:0 4px;line-height:1}',
      '.portal-notify-pop__close:hover{color:var(--text,#172033)}',
      '.portal-notify-pop__body{color:var(--text-secondary,#475569);margin:6px 0 8px;font-size:12px}',
      '.portal-notify-pop__go{border:1px solid var(--accent,#235fd6);background:var(--accent,#235fd6);color:#fff;font-size:12px;padding:5px 12px;border-radius:6px;cursor:pointer;font-weight:600}',
      '.portal-notify-pop__go:hover{background:var(--accent-hover,#184fbf)}',
    ].join('\n');
    document.head.appendChild(style);
  }
  function _notifyPopup(norm, label, tab) {
    try {
      _notifyEnsureStyles();
      let stack = document.getElementById('portalNotifyStack');
      if (!stack) {
        stack = document.createElement('div');
        stack.id = 'portalNotifyStack';
        stack.setAttribute('aria-live', 'assertive');
        document.body.appendChild(stack);
      }
      const ok = norm === 'succeeded';
      const cancel = norm === 'cancelled';
      const title = (label || '生成任务') + (ok ? ' 已完成' : cancel ? ' 已取消' : ' 已结束');
      const body = ok ? '结果已就绪，点击「查看结果」直接打开。' : cancel ? '任务已取消，可重新发起。' : '任务失败了，点击查看详情与原因。';
      const pop = document.createElement('div');
      pop.className = 'portal-notify-pop' + (ok ? '' : cancel ? ' is-cancel' : ' is-bad');
      pop.setAttribute('role', 'alert');
      pop.innerHTML =
        '<div class="portal-notify-pop__head"><span class="portal-notify-pop__icon">' + (ok ? '✅' : cancel ? '⛔' : '❌') + '</span>'
        + '<span class="portal-notify-pop__title">' + _notifyEsc(title) + '</span>'
        + '<button class="portal-notify-pop__close" type="button" aria-label="关闭">×</button></div>'
        + '<div class="portal-notify-pop__body">' + _notifyEsc(body) + '</div>'
        + (tab ? '<button class="portal-notify-pop__go" type="button">查看结果 →</button>' : '');
      pop.querySelector('.portal-notify-pop__close').addEventListener('click', () => pop.remove());
      const go = pop.querySelector('.portal-notify-pop__go');
      if (go) {
        go.addEventListener('click', () => {
          const btn = document.querySelector('.app-tab[data-tab="' + tab + '"]');
          if (btn && typeof activatePortalTab === 'function') activatePortalTab(btn);
          pop.remove();
        });
      }
      stack.appendChild(pop);
      // 防止堆积：同一时间最多 3 条，超出移除最旧
      while (stack.children.length > 3) stack.firstElementChild.remove();
      setTimeout(() => pop.remove(), 15000);
    } catch (e) { /* 弹窗尽力而为 */ }
  }

  window.__notifyJobDone = function (jobId, status, label, tab) {
    try {
      if (jobId === undefined || jobId === null || jobId === '') return;
      const norm = normalizeNotifyStatus(String(status));
      const map = _notifyLoadSeen();
      if (map[jobId] === norm) return; // 已通知过（含子应用侧先弹）
      map[jobId] = norm;
      const keys = Object.keys(map);
      if (keys.length > 200) keys.slice(0, keys.length - 200).forEach((k) => { delete map[k]; });
      localStorage.setItem('aiPortal.notifiedJobs', JSON.stringify(map));
      const ok = norm === 'succeeded';
      const title = (label || '生成任务') + (ok ? ' 已完成' : ' 已结束');
      const body = ok ? '结果已就绪，回到页面即可查看和下载。' : '任务以「' + norm + '」结束，请回到页面查看详情。';
      // 页面内弹窗永远弹（主通道）；切走时才补系统通知 + 标题闪烁
      _notifyPopup(norm, label, tab);
      if (document.hidden) {
        if ('Notification' in window && Notification.permission === 'granted') {
          try {
            const n = new Notification(title, { body: body, tag: 'ai-portal-job-done' });
            n.onclick = () => { try { window.focus(); } catch (e) {} n.close(); };
          } catch (e) { /* 构造失败时降级标题闪烁 */ }
        }
        _notifyFlashTitle(title);
      }
    } catch (e) { /* 通知尽力而为，绝不打断主流程 */ }
  };
  window.notifyJobDone = window.__notifyJobDone;
  let _notifyFlashTimer = null;
  function _notifyFlashTitle(message) {
    try {
      const base = document.title;
      let count = 0;
      const tick = () => {
        count += 1;
        document.title = (count % 2 === 1) ? ('✅ ' + message + ' — ' + base) : base;
        if (count >= 10) { clearInterval(_notifyFlashTimer); _notifyFlashTimer = null; document.title = base; }
      };
      if (_notifyFlashTimer) clearInterval(_notifyFlashTimer);
      _notifyFlashTimer = setInterval(tick, 1500);
      tick();
    } catch (e) {}
  }

  // 通知只看自己的任务：终态转场检测按当前登录用户名过滤。
  // 子应用 /api/jobs 返回全量任务（管理员能看到所有人的），不过滤
  // 的话管理员会收到全公司每个任务完成的提醒。
})();

// === Portal job status controller ===
(function () {
  const ACTIVE = new Set(['queued', 'pending', 'running', 'querying', 'resuming', 'waiting_provider', 'uploading', 'submitting']);
  const SUCCESS = new Set(['succeeded', 'success', 'completed', 'done']);
  const FAILURE = new Set(['failed', 'fail', 'failure', 'cancelled', 'canceled', 'error']);
  const SPECS = [
    { tab: 'feishu-generation-agent', app: 'feishu-generation-agent', label: '飞书创作助手' },
    { tab: 'seedance', app: 'seedance', label: '视频生成' },
    { tab: 'nb', app: 'nano-banana', label: '图片生成' },
    { tab: 'dreamina', app: 'dreamina', label: '即梦创作' },
    { tab: 'volcengine-portrait', app: 'volcengine-portrait', label: '人像视频' },
  ];
  const POLL_MS = 15000;
  let username = '';
  let role = '';
  let state = { seeded: false, seen: {}, unread: {} };
  let stateKey = '';
  let identityLoaded = false;
  const latestActive = {};
  const latestQueue = {};
  const acknowledgeTimers = new Map();

  function normalize(status) {
    const value = String(status || '').toLowerCase();
    if (ACTIVE.has(value)) return 'active';
    if (SUCCESS.has(value)) return 'success';
    if (FAILURE.has(value)) return 'failure';
    return '';
  }

  function readList(response) {
    if (Array.isArray(response)) return response;
    if (Array.isArray(response?.items)) return response.items;
    if (Array.isArray(response?.jobs)) return response.jobs;
    return [];
  }

  function jobId(job) {
    const value = job?.id ?? job?.job_id;
    return value === undefined || value === null ? '' : String(value);
  }

  function isOwn(job) {
    return Boolean(username) && String(job?.username || job?.user || '') === username;
  }

  function loadState() {
    stateKey = 'aiPortal.jobStatus.v1:' + (username || 'anonymous');
    try { state = JSON.parse(localStorage.getItem(stateKey) || '{}') || {}; } catch (e) { state = {}; }
    state.seeded = Boolean(state.seeded);
    state.seen = state.seen && typeof state.seen === 'object' ? state.seen : {};
    state.unread = state.unread && typeof state.unread === 'object' ? state.unread : {};
  }

  function saveState() {
    try { localStorage.setItem(stateKey, JSON.stringify(state)); } catch (e) {}
  }

  async function loadIdentity() {
    if (identityLoaded) return;
    try {
      const me = await api('/api/auth/me');
      username = String(me?.username || '');
      role = String(me?.role || '');
    } catch (e) {
      username = '';
      role = '';
    }
    loadState();
    identityLoaded = true;
  }

  function ensureBadge(spec) {
    const button = document.querySelector('.app-tab[data-tab="' + spec.tab + '"]');
    if (!button) return null;
    let wrap = button.querySelector('.portal-nav__badges');
    if (!wrap) {
      wrap = document.createElement('span');
      wrap.className = 'portal-nav__badges';
      wrap.setAttribute('aria-hidden', 'true');
      button.appendChild(wrap);
    }
    let badge = wrap.querySelector('.portal-nav__badge--status');
    if (!badge) {
      wrap.replaceChildren();
      badge = document.createElement('span');
      badge.className = 'portal-nav__badge portal-nav__badge--status';
      badge.hidden = true;
      wrap.appendChild(badge);
    }
    return badge;
  }

  function unreadCount(app, kind) {
    return Object.keys(state.unread?.[app]?.[kind] || {}).length;
  }

  function render(spec, activeCount, queueItem) {
    const badge = ensureBadge(spec);
    if (!badge) return;
    const failed = unreadCount(spec.app, 'failure');
    const succeeded = unreadCount(spec.app, 'success');
    const tone = failed ? 'danger' : succeeded ? 'success' : activeCount ? 'active' : '';
    badge.className = 'portal-nav__badge portal-nav__badge--status' + (tone ? ' is-' + tone : '');
    badge.textContent = activeCount ? String(activeCount) : '';
    badge.hidden = !tone;
    const title = [];
    if (activeCount) title.push('运行中：' + activeCount);
    if (succeeded) title.push('未读成功：' + succeeded);
    if (failed) title.push('未读失败：' + failed);
    if (queueItem) title.push('排队第 ' + queueItem.queue_position + ' 位，预计约 ' + queueItem.eta_minutes + ' 分钟');
    badge.title = title.join('；');
  }

  function observe(spec, jobs) {
    const previous = state.seen[spec.app] || {};
    const next = {};
    for (const job of jobs) {
      const id = jobId(job);
      if (!id) continue;
      const current = normalize(job.status);
      next[id] = current;
      if (!state.seeded || !isOwn(job) || previous[id] !== 'active' || !['success', 'failure'].includes(current)) continue;
      state.unread[spec.app] = state.unread[spec.app] || {};
      state.unread[spec.app][current] = state.unread[spec.app][current] || {};
      state.unread[spec.app][current][id] = Date.now();
      window.__notifyJobDone?.(id, current === 'success' ? 'succeeded' : 'failed', spec.label, spec.tab);
    }
    state.seen[spec.app] = next;
  }

  function clearTerminal(tab) {
    const spec = SPECS.find((item) => item.tab === tab);
    if (!spec || !state.unread[spec.app]) return;
    delete state.unread[spec.app].success;
    delete state.unread[spec.app].failure;
    if (!Object.keys(state.unread[spec.app]).length) delete state.unread[spec.app];
    saveState();
  }

  function currentTab() {
    return document.querySelector('.app-tab.active')?.dataset.tab || '';
  }

  function hasUnread(spec) {
    return Boolean(spec) && (unreadCount(spec.app, 'success') > 0 || unreadCount(spec.app, 'failure') > 0);
  }

  function acknowledgeTab(tab) {
    const spec = SPECS.find((item) => item.tab === tab);
    if (!hasUnread(spec)) return;
    clearTimeout(acknowledgeTimers.get(tab));
    clearTerminal(tab);
    render(spec, latestActive[spec.app] || 0, latestQueue[spec.app]);
  }

  function scheduleAcknowledge(tab) {
    if (currentTab() !== tab) return;
    const spec = SPECS.find((item) => item.tab === tab);
    if (!hasUnread(spec)) return;
    clearTimeout(acknowledgeTimers.get(tab));
    acknowledgeTimers.set(tab, setTimeout(() => {
      if (currentTab() === tab) acknowledgeTab(tab);
    }, 2000));
  }

  const interactionTargets = new WeakSet();
  const interactionFrames = new WeakSet();
  function bindInteractionTarget(target, tab) {
    if (!target || interactionTargets.has(target)) return;
    interactionTargets.add(target);
    ['pointerdown', 'keydown', 'input', 'change', 'submit'].forEach((eventName) => {
      target.addEventListener(eventName, () => scheduleAcknowledge(tab), true);
    });
  }

  function bindModuleInteractions(spec) {
    if (!spec?.tab) return;
    const panel = document.getElementById('tab-' + spec.tab);
    if (!panel) return;
    const iframe = panel.querySelector('iframe.portal-iframe');
    if (!iframe) {
      bindInteractionTarget(panel, spec.tab);
      return;
    }
    const bindFrame = () => {
      try { bindInteractionTarget(iframe.contentDocument, spec.tab); } catch (e) {}
    };
    if (!interactionFrames.has(iframe)) {
      interactionFrames.add(iframe);
      iframe.addEventListener('load', bindFrame);
    }
    bindFrame();
  }
  async function refresh() {
    await loadIdentity();
    const [historyResponse, queueResponse] = await Promise.all([
      api('/api/platform/history?limit=200&days=30').catch(() => []),
      api('/api/platform/queue').catch(() => ({ items: [] })),
    ]);
    const jobs = readList(historyResponse);
    const queueItems = Array.isArray(queueResponse?.items) ? queueResponse.items : [];
    for (const spec of SPECS) {
      const moduleJobs = jobs.filter((job) => String(job?.app || '') === spec.app);
      observe(spec, moduleJobs);
      const visibleJobs = role === 'admin' ? moduleJobs : moduleJobs.filter(isOwn);
      const activeCount = visibleJobs.filter((job) => normalize(job.status) === 'active').length;
      const queueItem = queueItems.find((item) => item.app === spec.app);
      latestActive[spec.app] = activeCount;
      latestQueue[spec.app] = queueItem;
      bindModuleInteractions(spec);
      render(spec, activeCount, queueItem);
    }
    state.seeded = true;
    saveState();
  }

  document.addEventListener('portal:tabchange', (event) => {
    const tab = event.detail?.tab || '';
    acknowledgeTab(tab);
    bindModuleInteractions(SPECS.find((spec) => spec.tab === tab));
    refresh();
  });
  SPECS.forEach((spec) => { ensureBadge(spec); bindModuleInteractions(spec); });
  refresh();
  setInterval(refresh, POLL_MS);
})();
// === Contextual per-module help ===
(function () {
  const MODULE_HELP = {
    'feishu-generation-agent': {
      title: '飞书创作助手',
      purpose: '从飞书文档拆解任务，审批后批量生成图片或视频。',
      steps: ['打开飞书文档并授权读取', '选择要拆解的任务并提交审批', '审批通过后自动排队生成'],
      result: '结果进入全局「创作记录」，可在那里下载或复用参数。',
      docUrl: 'https://redcqchina.feishu.cn/docx/F9MRdZqkGoH1zvxKWoDc6MbBnpe',
    },
    'seedance': {
      title: '视频生成',
      purpose: '文生视频、图生视频和首尾帧视频。',
      steps: ['选好模型、时长和分辨率', '输入提示词，可 @ 引用参考图', '提交后等待排队生成'],
      result: '完成后进入「创作记录」，可预览和下载视频。',
      docUrl: 'https://redcqchina.feishu.cn/docx/KOtMdAlFeoWYmUxFui1cngvlnsc',
    },
    'nb': {
      title: '图片生成',
      purpose: '文生图、图生图和批量出图。',
      steps: ['选择供应商（免费本地或付费）', '填写提示词或上传参考图', '提交生成并下载图片'],
      result: '图片结果进入「创作记录」，可复用参数再次生成。',
      docUrl: 'https://redcqchina.feishu.cn/docx/MEsId6oYwotQp6xxBSocP7K7ncg',
    },
    'volcengine-portrait': {
      title: '人像视频',
      purpose: '数字人口播和虚拟人物视频。',
      steps: ['上传或选择人像资产', '填写口播文案或提示词', '提交生成口播视频'],
      result: '视频进入「创作记录」，可下载。',
      docUrl: 'https://redcqchina.feishu.cn/docx/ZrqUdhuxfo1KtSxH1Toca7iVnhx',
    },
    'infinite-canvas': {
      title: '创意画布',
      purpose: '节点式组织参考图、提示词和模型。',
      steps: ['新建画布并添加图片/提示词节点', '把节点连接到模型', '运行生成并查看结果'],
      result: '生成结果回到画布，也可在全局历史中查找。',
      docUrl: 'https://redcqchina.feishu.cn/docx/AUn7dNuY4oghlNxEkulcK2oJnhb',
    },
    'previz': {
      title: '分镜预演',
      purpose: '3D 摆位、机位设计和镜头快照。',
      steps: ['新建分镜场景', '摆放模型和机位', '导出镜头快照'],
      result: '快照保存在分镜模块内，可用于后续视频生成。',
      docUrl: 'https://redcqchina.feishu.cn/docx/UbpUdfC4zo42Q8xJPWqcT3isnbb',
    },
    'dreamina': {
      title: '即梦创作',
      purpose: '管理即梦账号并生成图片/视频。',
      steps: ['先安装或登录即梦账号', '选择账号和生成模式', '提交任务并等待结果'],
      result: '结果进入「创作记录」，可下载。',
      docUrl: 'https://redcqchina.feishu.cn/docx/NxwZdjsCzo4VAnxaoJccN9kKnvg',
    },
    'rag-assistant': {
      title: '报错助手',
      purpose: '粘贴报错信息，快速定位原因和下一步。',
      steps: ['复制报错文字或截图', '粘贴到输入框并提交', '按返回的建议处理'],
      result: '处理建议直接显示在当前页面。',
      docUrl: 'https://redcqchina.feishu.cn/docx/LPf8dKvx7ocfnRxbgCXcL5dfnOb',
    },
    'history': {
      title: '创作记录',
      purpose: '跨应用查找图片、视频和任务。',
      steps: ['用筛选或搜索定位记录', '点卡片查看详情', '下载或复用参数'],
      result: '下载的文件保存在浏览器下载目录。',
    },
    'keys': {
      title: '我的密钥',
      purpose: '集中保存个人 API Key，复制到创作模块。',
      steps: ['添加密钥并选择供应商', '点「复制」或「应用到图片生成」', '在对应模块检查生效'],
      result: '密钥只保存在服务端，明文不跨页面展示。',
    },
    'stats': {
      title: '使用统计',
      purpose: '查看用量；管理员可维护用户和密钥。',
      steps: ['选择时间范围查看统计', '管理员可导出 CSV', '按用户或日期查看趋势'],
      result: '导出的 CSV 可直接用 Excel/WPS 打开。',
    },
    _fallback: {
      title: '模块帮助',
      purpose: '当前模块的简要说明。',
      steps: ['了解这个模块能做什么', '按页面提示完成第一步', '到「创作记录」查看结果'],
      result: '生成结果一般会进入全局「创作记录」。',
    },
  };

  function ensureModuleHelpDialog() {
    let dialog = document.getElementById('portalModuleHelpDialog');
    if (dialog) return dialog;
    dialog = document.createElement('dialog');
    dialog.id = 'portalModuleHelpDialog';
    dialog.className = 'portal-help-dialog portal-module-help';
    dialog.innerHTML =
      '<div class="portal-help-head">' +
      '<h2 id="moduleHelpTitle">模块帮助</h2>' +
      '<button class="portal-help-close" type="button" data-close>关闭</button>' +
      '</div>' +
      '<p id="moduleHelpPurpose"></p>' +
      '<div class="portal-help-steps" id="moduleHelpSteps"></div>' +
      '<p class="portal-module-help-result" id="moduleHelpResult"></p>' +
      '<a id="moduleHelpDoc" class="module-help-doc" target="_blank" rel="noopener" href="#" hidden>查看使用说明</a>';
    document.body.appendChild(dialog);
    dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
    return dialog;
  }

  function activeModuleHelp() {
    const btn = document.querySelector('.app-tab.active') || document.querySelector('.app-tab');
    return (btn && MODULE_HELP[btn.dataset.tab]) || MODULE_HELP._fallback;
  }

  function openModuleHelp() {
    const dialog = ensureModuleHelpDialog();
    const help = activeModuleHelp();
    dialog.querySelector('#moduleHelpTitle').textContent = help.title;
    dialog.querySelector('#moduleHelpPurpose').textContent = help.purpose;
    dialog.querySelector('#moduleHelpSteps').innerHTML = help.steps.map((s, i) =>
      '<div class="portal-help-step"><span class="portal-help-step__num">' + (i + 1) + '</span><div><b>' + s + '</b></div></div>'
    ).join('');
    dialog.querySelector('#moduleHelpResult').textContent = help.result;
    const docLink = dialog.querySelector('#moduleHelpDoc');
    if (docLink) {
      if (help.docUrl) { docLink.href = help.docUrl; docLink.hidden = false; }
      else { docLink.hidden = true; }
    }
    dialog.showModal();
  }

  const btn = document.getElementById('moduleHelpBtn');
  if (btn) btn.addEventListener('click', openModuleHelp);
  window.portalModuleHelp = openModuleHelp;
})();
// === Release notice ===
(function () {
  const VERSION = 'nav-home-2026-09-03';
  const notice = document.getElementById('releaseNotice');
  if (!notice) return;
  const close = () => { notice.hidden = true; try { localStorage.setItem('portal_release_seen', VERSION); } catch (e) {} };
  document.getElementById('releaseNoticeClose')?.addEventListener('click', close);
  document.getElementById('releaseNoticeConfirm')?.addEventListener('click', close);
  notice.addEventListener('click', (e) => { if (e.target === notice) close(); });
  let seen = false;
  try { seen = localStorage.getItem('portal_release_seen') === VERSION; } catch (e) {}
  if (!seen) window.setTimeout(() => { notice.hidden = false; }, 180);
})();
