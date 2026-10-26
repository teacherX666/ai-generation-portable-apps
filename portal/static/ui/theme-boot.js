/*
 * theme-boot.js — 首帧之前把主题写进 <html>，并在同源文档之间同步。
 *
 * 必须放在每个入口页 <head> 的**第一个样式表之前**（阻塞式执行 ⇒ 首帧前生效），
 * 否则深色用户会先看到一帧白闪再变黑。`tests/frontend/dark_mode_guards.test.cjs`
 * 会逐页断言这个顺序。
 *
 * 规则（见 docs/2026-10-26-dark-mode-design.md §3.1 / §3.3 / §3.4）：
 *   * 单一状态源 localStorage['portal_theme']，取值 'light' | 'dark'，缺省 = 浅色；
 *   * 页面上的唯一真源是 <html data-theme="dark">，所有样式只认它；
 *   * 同源文档（Portal 与经它反代加载的 iframe）用 storage 事件同步，不刷新页面；
 *   * 不同源直连时读不到共享值 ⇒ 保持浅色，绝不抛错。
 */
(function () {
  var KEY = "portal_theme";
  var root = document.documentElement;

  function apply(value) {
    if (value === "dark") root.setAttribute("data-theme", "dark");
    else root.removeAttribute("data-theme");
  }

  var stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch (error) {
    /* 隐私模式 / 禁用存储：保持浅色，绝不因为读不到主题把页面搞崩 */
    stored = null;
  }
  apply(stored);

  // storage 事件只在**别的文档**写 localStorage 时触发 —— 正好是"Portal 切、iframe 跟"。
  window.addEventListener("storage", function (event) {
    if (!event || event.key !== KEY) return;
    apply(event.newValue);
  });
})();
