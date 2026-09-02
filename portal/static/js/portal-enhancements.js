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
  });

  setTimeout(maybeFirstRunHelp, 600);
})();

// === Running-task indicators in the left navigation ===
(function () {
  const ACTIVE_STATUSES = new Set(['queued', 'pending', 'running', 'querying', 'resuming', 'waiting_provider', 'uploading', 'submitting']);
  function isActive(status) {
    return ACTIVE_STATUSES.has(String(status || '').toLowerCase());
  }

  function ensureBadges() {
    document.querySelectorAll('.app-tab').forEach((btn) => {
      if (!btn.querySelector('.portal-nav__badge')) {
        const badge = document.createElement('span');
        badge.className = 'portal-nav__badge';
        badge.hidden = true;
        badge.textContent = '';
        btn.appendChild(badge);
      }
    });
  }

  function setBadge(tab, count) {
    const badge = document.querySelector('.app-tab[data-tab="' + tab + '"] .portal-nav__badge');
    if (!badge) return;
    badge.textContent = count > 0 ? String(count) : '';
    badge.hidden = count <= 0;
  }

  async function countActive(spec) {
    try {
      const res = await api(spec.url);
      const list = Array.isArray(res) ? res : (res?.jobs || res?.items || []);
      if (!Array.isArray(list)) return 0;
      return list.filter((job) => job && isActive(job.status)).length;
    } catch (e) {
      return 0;
    }
  }

  const specs = [
    { tab: 'seedance', app: 'seedance', url: '/seedance/api/jobs' },
    { tab: 'nb', app: 'nano-banana', url: '/nano-banana/api/jobs' },
    { tab: 'dreamina', app: 'dreamina', url: '/dreamina/api/jobs' },
    { tab: 'volcengine-portrait', app: 'volcengine-portrait', url: '/volcengine-portrait/api/jobs' },
  ];

  async function refresh() {
    ensureBadges();
    await Promise.all(specs.map(async (spec) => {
      const count = await countActive(spec);
      setBadge(spec.tab, count);
    }));
  }

  async function refreshQueueMeta() {
    try {
      const res = await api('/api/platform/queue');
      const items = (res && res.ok && Array.isArray(res.items)) ? res.items : [];
      specs.forEach((spec) => {
        const badge = document.querySelector('.app-tab[data-tab="' + spec.tab + '"] .portal-nav__badge');
        if (!badge) return;
        const first = items.find((it) => it.app === spec.app);
        badge.title = first ? '排队第 ' + first.queue_position + ' 位，预计约 ' + first.eta_minutes + ' 分钟' : '';
      });
    } catch (e) { /* queue metadata is best-effort */ }
  }

  refresh();
  refreshQueueMeta();
  setInterval(refresh, 15000);
  setInterval(refreshQueueMeta, 15000);
})();
// === Global prompt optimization entry ===
(function () {
  const btn = document.getElementById('optimizeBtn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    if (document.body.classList.contains('director-collapsed')) {
      const toggle = document.getElementById('director-toggle');
      if (toggle) toggle.click();
    }
    setTimeout(() => {
      const input = document.querySelector('.director-sidebar .director-field textarea');
      if (input) input.focus();
    }, 80);
  });
})();
// === Contextual per-module help ===
(function () {
  const MODULE_HELP = {
    'feishu-generation-agent': {
      title: '飞书创作助手',
      purpose: '从飞书文档拆解任务，审批后批量生成图片或视频。',
      steps: ['打开飞书文档并授权读取', '选择要拆解的任务并提交审批', '审批通过后自动排队生成'],
      result: '结果进入全局「创作记录」，可在那里下载或复用参数。',
    },
    'seedance': {
      title: '视频生成',
      purpose: '文生视频、图生视频和首尾帧视频。',
      steps: ['选好模型、时长和分辨率', '输入提示词，可 @ 引用参考图', '提交后等待排队生成'],
      result: '完成后进入「创作记录」，可预览和下载视频。',
    },
    'nb': {
      title: '图片生成',
      purpose: '文生图、图生图和批量出图。',
      steps: ['选择供应商（免费本地或付费）', '填写提示词或上传参考图', '提交生成并下载图片'],
      result: '图片结果进入「创作记录」，可复用参数再次生成。',
    },
    'volcengine-portrait': {
      title: '人像视频',
      purpose: '数字人口播和虚拟人物视频。',
      steps: ['上传或选择人像资产', '填写口播文案或提示词', '提交生成口播视频'],
      result: '视频进入「创作记录」，可下载。',
    },
    'infinite-canvas': {
      title: '创意画布',
      purpose: '节点式组织参考图、提示词和模型。',
      steps: ['新建画布并添加图片/提示词节点', '把节点连接到模型', '运行生成并查看结果'],
      result: '生成结果回到画布，也可在全局历史中查找。',
    },
    'previz': {
      title: '分镜预演',
      purpose: '3D 摆位、机位设计和镜头快照。',
      steps: ['新建分镜场景', '摆放模型和机位', '导出镜头快照'],
      result: '快照保存在分镜模块内，可用于后续视频生成。',
    },
    'dreamina': {
      title: '即梦创作',
      purpose: '管理即梦账号并生成图片/视频。',
      steps: ['先安装或登录即梦账号', '选择账号和生成模式', '提交任务并等待结果'],
      result: '结果进入「创作记录」，可下载。',
    },
    'rag-assistant': {
      title: '报错助手',
      purpose: '粘贴报错信息，快速定位原因和下一步。',
      steps: ['复制报错文字或截图', '粘贴到输入框并提交', '按返回的建议处理'],
      result: '处理建议直接显示在当前页面。',
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
      '<p class="portal-module-help-result" id="moduleHelpResult"></p>';
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
    dialog.showModal();
  }

  const btn = document.getElementById('moduleHelpBtn');
  if (btn) btn.addEventListener('click', openModuleHelp);
  window.portalModuleHelp = openModuleHelp;
})();