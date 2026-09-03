// Portal module registry: single source of truth for top-level navigation.
// iframe/component apps come from /api/apps; Portal-native panels live here.
(function () {
  window.PORTAL_NATIVE_COMPONENTS = [
    { name: 'history', display_name: '创作记录' },
    { name: 'keys', display_name: '我的密钥' },
    { name: 'stats', display_name: '使用统计' },
  ];

  // Apps present in apps.json but not top-level navigation entries.
  window.PORTAL_NON_NAV_APPS = ['director'];
  window.PORTAL_APP_TAB_ALIASES = { 'nano-banana': 'nb' };

  async function fetchApps() {
    try {
      const res = await api('/api/apps');
      if (res && res.ok && Array.isArray(res.apps)) return res.apps;
    } catch (e) {}
    return [];
  }

  function expectedNavNames(apps) {
    const set = new Set(window.PORTAL_NATIVE_COMPONENTS.map((c) => c.name));
    apps.forEach((app) => {
      if (!window.PORTAL_NON_NAV_APPS.includes(app.name)) set.add(window.PORTAL_APP_TAB_ALIASES[app.name] || app.name);
    });
    return [...set];
  }

  function domNames() {
    const tabs = [...document.querySelectorAll('.app-tab[data-tab]')].map((b) => b.dataset.tab);
    const panels = [...document.querySelectorAll('.tab-panel[id^="tab-"]')].map((p) => p.id.slice(4));
    const mobile = [...document.querySelectorAll('#mobileAppSelect option')].map((o) => o.value).filter(Boolean);
    return { tabs, panels, mobile };
  }

  function diff(expected, actual, label) {
    const expectedSet = new Set(expected);
    const actualSet = new Set(actual);
    const missing = [...expectedSet].filter((n) => !actualSet.has(n));
    const extra = [...actualSet].filter((n) => !expectedSet.has(n));
    if (missing.length) console.warn('[portal-module-registry] ' + label + ' 缺少入口:', missing);
    if (extra.length) console.warn('[portal-module-registry] ' + label + ' 多余入口:', extra);
    return { missing, extra };
  }

  async function checkPortalModuleConsistency() {
    const apps = await fetchApps();
    const expected = expectedNavNames(apps);
    const { tabs, panels, mobile } = domNames();
    const result = {
      expected,
      tabs,
      panels,
      mobile,
      tabDiff: diff(expected, tabs, 'tab'),
      panelDiff: diff(expected, panels, 'panel'),
      mobileDiff: diff(expected, mobile, '移动端'),
    };
    window.__portalModuleRegistryCheck = result;
    return result;
  }

  window.checkPortalModuleConsistency = checkPortalModuleConsistency;

  function run() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => checkPortalModuleConsistency());
    } else {
      checkPortalModuleConsistency();
    }
  }
  run();
})();