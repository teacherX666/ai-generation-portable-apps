// Portal shell: tab switching, lazy iframe lifecycle, and app registry wiring.
// Plain globals and no build step, so it behaves the same on Windows and macOS.
(function () {
  function iframeTarget(iframe) {
    return iframe?.dataset.resolvedSrc || iframe?.dataset.src || iframe?.dataset.fallbackSrc || '';
  }

  function setIframeLoadState(iframe, state, message = '') {
    const panel = iframe?.closest('.iframe-panel');
    if (!panel) return;
    panel.classList.toggle('is-loading', state === 'loading');
    panel.classList.toggle('has-load-error', state === 'error');
    const status = panel.querySelector('.iframe-load-status');
    if (!status) return;
    status.hidden = state === 'ready';
    status.querySelector('.iframe-load-message').textContent = message || (state === 'error' ? '应用加载失败' : '正在加载应用…');
    status.querySelector('.iframe-retry').hidden = state !== 'error';
  }

  function injectPortalRagInterceptor(iframe, attempt = 0) {
    try {
      const doc = iframe.contentDocument;
      if (!doc || !doc.body) {
        if (attempt < 8) setTimeout(() => injectPortalRagInterceptor(iframe, attempt + 1), 250);
        return;
      }
      if (doc.getElementById('portal-rag-interceptor')) return;
      const script = doc.createElement('script');
      script.id = 'portal-rag-interceptor';
      script.src = '/js/portal-rag-interceptor.js';
      doc.body.appendChild(script);
    } catch (e) {
      if (attempt < 8) setTimeout(() => injectPortalRagInterceptor(iframe, attempt + 1), 250);
    }
  }
  function ensureIframeStatus(iframe) {
    const panel = iframe?.closest('.iframe-panel');
    if (!panel || panel.querySelector('.iframe-load-status')) return;
    const status = document.createElement('div');
    status.className = 'iframe-load-status';
    status.setAttribute('role', 'status');
    status.innerHTML = '<div class="spinner" aria-hidden="true"></div><p class="iframe-load-message">正在加载应用…</p><button class="iframe-retry ui-btn ui-btn--secondary" type="button" hidden>重新加载</button>';
    status.querySelector('.iframe-retry').addEventListener('click', () => loadPortalIframe(iframe, { force: true }));
    panel.insertBefore(status, iframe);
    iframe.addEventListener('load', () => { setIframeLoadState(iframe, 'ready'); injectPortalRagInterceptor(iframe); });
    iframe.addEventListener('error', () => setIframeLoadState(iframe, 'error', '应用加载失败，请检查服务状态后重试。'));
  }

  function loadPortalIframe(iframe, { force = false } = {}) {
    if (!iframe) return;
    ensureIframeStatus(iframe);
    const target = iframeTarget(iframe);
    if (!target) {
      setIframeLoadState(iframe, 'error', '应用地址尚未配置。');
      return;
    }
    if (!force && iframe.dataset.loaded === 'true') return;
    setIframeLoadState(iframe, 'loading');
    iframe.dataset.loaded = 'true';
    if (force && iframe.getAttribute('src') === target) {
      iframe.src = 'about:blank';
      requestAnimationFrame(() => { iframe.src = target; });
    } else {
      iframe.src = target;
    }
  }

  function loadIframeForPanel(panel) {
    const iframe = panel?.querySelector('iframe.portal-iframe');
    if (iframe) loadPortalIframe(iframe);
  }

  let hasActivatedPortalTab = false;

  function animatePortalPanel(panel, fromHome) {
    if (!panel || !hasActivatedPortalTab || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    panel.classList.remove('portal-tab-enter', 'portal-tab-enter-from-home');
    void panel.offsetWidth;
    panel.classList.add('portal-tab-enter');
    if (fromHome) panel.classList.add('portal-tab-enter-from-home');
    panel.addEventListener('animationend', () => {
      panel.classList.remove('portal-tab-enter', 'portal-tab-enter-from-home');
    }, { once: true });
  }

  function activatePortalTab(btn, { focus = false, persist = true } = {}) {
    if (!btn) return;
    const panel = document.getElementById('tab-' + btn.dataset.tab);
    if (!panel) return;
    const previousPanel = document.querySelector('.tab-panel.active');
    const wasHome = document.body.classList.contains('portal-home-active');
    document.querySelectorAll('.app-tab').forEach(t => {
      const active = t === btn;
      t.classList.toggle('active', active);
      t.setAttribute('aria-selected', active ? 'true' : 'false');
      t.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('.tab-panel').forEach(p => {
      const active = p === panel;
      p.classList.toggle('active', active);
      p.hidden = !active;
    });
    const mobileSelect = document.getElementById('mobileAppSelect');
    if (mobileSelect && mobileSelect.value !== btn.dataset.tab) mobileSelect.value = btn.dataset.tab;
    if (btn.dataset.tab !== 'home' && persist) { try { localStorage.setItem('portal_last_non_home_tab', btn.dataset.tab); } catch (e) {} }
    const isHome = btn.dataset.tab === 'home';
    document.body.classList.toggle('portal-home-active', isHome);
    if (previousPanel !== panel) animatePortalPanel(panel, wasHome && !isHome);
    hasActivatedPortalTab = true;
    const homeExit = document.getElementById('portalHomeExitBtn');
    if (homeExit) homeExit.hidden = !isHome;
    loadIframeForPanel(panel);
    if (persist) { try { localStorage.setItem('portal_active_tab', btn.dataset.tab); } catch (e) {} }
    if (focus) btn.focus();
    if (previousPanel !== panel) {
      document.dispatchEvent(new CustomEvent('portal:tabchange', {
        detail: {
          tab: btn.dataset.tab,
          previousTab: previousPanel?.id?.replace(/^tab-/, '') || '',
          isHome,
          wasHome,
          initial: !persist,
        },
      }));
    }
  }

  function syncPortalHeaderHeight() {
    if (typeof document.querySelector !== 'function') return;
    const header = document.querySelector('.topbar-stack');
    if (!header) return;
    document.documentElement.style.setProperty('--portal-header-height', `${Math.ceil(header.getBoundingClientRect().height)}px`);
  }

  async function initConfiguredIframes() {
    document.querySelectorAll('iframe.portal-iframe').forEach(iframe => {
      ensureIframeStatus(iframe);
      if (iframe.getAttribute('src')) {
        iframe.dataset.loaded = 'true';
        setIframeLoadState(iframe, 'ready');
        injectPortalRagInterceptor(iframe);
      }
      const fallback = iframe.dataset.src || iframe.dataset.fallbackSrc || iframe.getAttribute('src');
      if (fallback) iframe.dataset.resolvedSrc = fallback;
    });


    const initialName = (() => { try { return localStorage.getItem('portal_active_tab'); } catch (e) { return null; } })();
    const initialPortalTab = (initialName && portalTabButtons.find(btn => btn.dataset.tab === initialName)) || document.getElementById("portalHomeBtn") || portalTabButtons[0];
    if (initialPortalTab) activatePortalTab(initialPortalTab, { persist: false });

    const res = await api('/api/apps');
    if (!res?.ok || !Array.isArray(res.apps)) return;
    document.querySelectorAll('iframe[data-app]').forEach(iframe => {
      const app = res.apps.find(item => item.name === iframe.dataset.app);
      if (!app?.iframe_url) return;
      const fallback = iframe.dataset.fallbackSrc || '';
      if (app.mount === 'iframe' || !fallback || app.iframe_url !== fallback) {
        iframe.dataset.resolvedSrc = app.iframe_url;
      }
    });
  }

  const portalTabButtons = Array.from(document.querySelectorAll('.app-tab'));
  const mobileAppSelect = document.getElementById('mobileAppSelect');
  const portalHomeExitBtn = document.getElementById('portalHomeExitBtn');
  if (portalHomeExitBtn) portalHomeExitBtn.addEventListener('click', () => {
    let name = 'feishu-generation-agent';
    try { name = localStorage.getItem('portal_last_non_home_tab') || name; } catch (e) {}
    activatePortalTab(portalTabButtons.find(item => item.dataset.tab === name) || portalTabButtons[0]);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !document.body.classList.contains('portal-home-active')) return;
    portalHomeExitBtn?.click();
  });  const portalDebugReturnBtn = document.getElementById('portalDebugReturnBtn');
  if (portalDebugReturnBtn) portalDebugReturnBtn.addEventListener('click', () => {
    let name = 'feishu-generation-agent';
    try { name = localStorage.getItem('portal_last_non_home_tab') || name; } catch (e) {}
    const btn = portalTabButtons.find(item => item.dataset.tab === name) || portalTabButtons[0];
    activatePortalTab(btn);
  });
  if (mobileAppSelect) {
    mobileAppSelect.addEventListener('change', () => {
      const btn = portalTabButtons.find(item => item.dataset.tab === mobileAppSelect.value);
      activatePortalTab(btn);
    });
  }


  function setupNavCollapse() {
    const nav = document.querySelector('.portal-nav');
    const toggle = document.getElementById('portalNavToggle');
    if (!nav || !toggle) return;

    nav.querySelectorAll('.app-tab').forEach((btn) => {
      const label = btn.querySelector('.portal-nav__label')?.textContent.trim();
      if (!label) return;
      btn.dataset.collapsedLabel = label.slice(0, 1);
      btn.setAttribute('aria-label', label);
      btn.title = label;
    });
    nav.querySelectorAll('.portal-help-btn').forEach((btn) => {
      const label = btn.textContent.trim();
      if (label) btn.dataset.collapsedLabel = label.slice(0, 1);
    });

    let collapsed = false;
    try { collapsed = localStorage.getItem('portal_nav_collapsed') === '1'; } catch (e) {}
    function render() {
      nav.classList.toggle('is-collapsed', collapsed);
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-label', collapsed ? '展开导航栏' : '收缩导航栏');
      toggle.title = collapsed ? '展开导航栏' : '收缩导航栏';
      toggle.textContent = collapsed ? '›' : '‹';
    }
    toggle.addEventListener('click', () => {
      collapsed = !collapsed;
      try { localStorage.setItem('portal_nav_collapsed', collapsed ? '1' : '0'); } catch (e) {}
      render();
    });
    render();
  }

  setupNavCollapse();

  syncPortalHeaderHeight();
  if (typeof window.addEventListener === 'function') {
    window.addEventListener('resize', syncPortalHeaderHeight, { passive: true });
  }
  if ('ResizeObserver' in window && typeof document.querySelector === 'function') {
    const portalHeader = document.querySelector('.topbar-stack');
    if (portalHeader) new ResizeObserver(syncPortalHeaderHeight).observe(portalHeader);
  }

  portalTabButtons.forEach(btn => {
    btn.addEventListener('click', () => activatePortalTab(btn));
    btn.addEventListener('keydown', e => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key)) return;
      e.preventDefault();
      const index = portalTabButtons.indexOf(btn);
      const nextIndex = e.key === 'Home' ? 0
        : e.key === 'End' ? portalTabButtons.length - 1
        : (index + ((e.key === 'ArrowRight' || e.key === 'ArrowDown') ? 1 : -1) + portalTabButtons.length) % portalTabButtons.length;
      activatePortalTab(portalTabButtons[nextIndex], { focus: true });
    });
  });

  initConfiguredIframes();

  window.activatePortalTab = activatePortalTab;
  window.loadPortalIframe = loadPortalIframe;
  window.loadIframeForPanel = loadIframeForPanel;
  window.initConfiguredIframes = initConfiguredIframes;
})();
