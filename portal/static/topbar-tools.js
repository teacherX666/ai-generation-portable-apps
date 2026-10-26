/*
 * Portal 顶栏工具按钮：深色模式（月亮 ↔ 太阳）+ 鼠标拖尾（星形）。
 *
 * 2026-10-26 与深色模式一并重做：原来是 `effects.js` 里一个内联样式的裸 `<button>*</button>`，
 * 与设计系统无关、也偏丑。现在两个按钮同尺寸 / 同圆角 / 同悬停反馈，颜色全部走
 * `var(--muted) / var(--text) / var(--accent)`（→ shared-ui token，深浅自动跟着变）。
 *
 * 职责边界：
 *   * 本文件只管**按钮**：外观状态、点击、aria、以及把选择写进 localStorage；
 *   * 主题的"真源"是 `<html data-theme="dark">`（`ui/theme-boot.js` 在首帧前写好，
 *     并负责在同源文档间用 storage 事件同步）；
 *   * 拖尾的实际逻辑仍然在 `effects.js`（canvas 粒子），这里通过它暴露的
 *     `window.__portalFxTrail` 调用 —— 两边都不重复实现对方的活。
 *
 * 见 docs/2026-10-26-dark-mode-design.md §3.5。
 */
(function () {
  "use strict";

  var THEME_KEY = "portal_theme";
  var FX_KEY = "portal_fx_trail";

  /** 星形（四角星）图标：关闭时描边、开启时实心（填充由 CSS 控制）。 */
  var STAR_PATH = "M12 2.6l2.3 6.1 6.1 2.3-6.1 2.3L12 19.4l-2.3-6.1L3.6 11l6.1-2.3z";

  function readItem(key) {
    try {
      return localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function writeItem(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (error) {
      /* 隐私模式/禁用存储：按钮仍然可用，只是这一页记不住 */
    }
  }

  function isDark() {
    return document.documentElement.getAttribute("data-theme") === "dark";
  }

  function applyTheme(dark) {
    if (dark) document.documentElement.setAttribute("data-theme", "dark");
    else document.documentElement.removeAttribute("data-theme");
  }

  function fxEnabled() {
    var api = window.__portalFxTrail;
    if (api && typeof api.isEnabled === "function") return Boolean(api.isEnabled());
    return readItem(FX_KEY) === "on";
  }

  function buildThemeIcon() {
    var box = document.createElement("span");
    box.className = "portal-tool__icon";
    var disc = document.createElement("span");
    disc.className = "portal-tool__disc";
    var rays = document.createElement("span");
    rays.className = "portal-tool__rays";
    box.appendChild(disc);
    box.appendChild(rays);
    return box;
  }

  function buildStarIcon() {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", STAR_PATH);
    svg.appendChild(path);
    return svg;
  }

  function init() {
    var host = document.querySelector(".portal-bar-right");
    // 登录页没有这条栏 —— 没有就安静退出（主题仍然由 theme-boot 生效）。
    if (!host) return;

    var trailBtn = document.createElement("button");
    trailBtn.type = "button";
    trailBtn.className = "portal-tool portal-tool--trail";
    trailBtn.appendChild(buildStarIcon());

    var themeBtn = document.createElement("button");
    themeBtn.type = "button";
    themeBtn.className = "portal-tool portal-tool--theme";
    themeBtn.appendChild(buildThemeIcon());

    function paintTrail() {
      var on = fxEnabled();
      trailBtn.classList.toggle("is-on", on);
      trailBtn.setAttribute("aria-pressed", on ? "true" : "false");
      trailBtn.title = on ? "鼠标拖尾：开（点一下关掉）" : "鼠标拖尾：关（点一下打开）";
    }

    function paintTheme() {
      var dark = isDark();
      themeBtn.setAttribute("aria-pressed", dark ? "true" : "false");
      themeBtn.title = dark ? "浅色模式" : "深色模式";
    }

    trailBtn.addEventListener("click", function () {
      var api = window.__portalFxTrail;
      if (api && typeof api.toggle === "function") {
        api.toggle();
      } else {
        // effects.js 没加载（脚本顺序变了 / 加载失败）时的兜底：状态照样存，
        // 本次会话的拖尾不生效而已 —— 绝不因为一个装饰按钮把顶栏搞崩。
        var next = !fxEnabled();
        writeItem(FX_KEY, next ? "on" : "off");
      }
      paintTrail();
    });

    themeBtn.addEventListener("click", function () {
      var dark = !isDark();
      applyTheme(dark);
      writeItem(THEME_KEY, dark ? "dark" : "light");
      paintTheme();
    });

    // 别的文档（或本页其它脚本）改了这两个 key —— 跟着重画。
    // 主题变量本身由 theme-boot.js 应用，这里只负责按钮自己的外观。
    window.addEventListener("storage", function (event) {
      if (!event) return;
      if (event.key === THEME_KEY) paintTheme();
      else if (event.key === FX_KEY) paintTrail();
    });

    // theme-boot.js 已经在首帧前读过一次；这里再读一次是为了"同页有人切过"的场景。
    applyTheme(readItem(THEME_KEY) === "dark");

    // 顺序：拖尾在左、主题在右（用户 2026-10-26 选定的排布）。
    host.insertBefore(themeBtn, host.firstChild);
    host.insertBefore(trailBtn, themeBtn);
    paintTrail();
    paintTheme();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
