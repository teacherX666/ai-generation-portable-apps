/*
 * 深色模式验收审计（2026-10-26 加入）
 *
 * 为什么要有它：设计文档里原本写的是"每模块浅/深成对截图 + 视觉模型扫破绽"，但这台机器上
 * **截图拿不到** —— DSH 的浏览器宿主要页签在前台才合成画面，自动化会话里的页签是隐藏态
 * （`document.body.clientWidth === 0`、`visibilityState === 'hidden'`），`browser_screenshot`
 * 一律 30s 超时。于是改成这套**程序化审计**：直接在真实浏览器里遍历 DOM 算 WCAG 对比度，
 * 并单独抓"深色下仍是大片浅底"的孤岛。它比肉眼截图更可复跑，也是第 1 期所有"0 违规"结论的来源。
 *
 * 怎么用（两种都行）：
 *   1) 在 9090 页面打开 DevTools 控制台，把本文件整段粘进去 → 返回 JSON；
 *   2) 由 Agent 用 browser_execute 执行同一段代码。
 *
 * 覆盖范围：当前页面（Portal 外壳）+ 传入的 iframe id 列表（子应用是独立文档，要各自审计）。
 * 每个文档都会跑两遍：先浅色、后深色，然后恢复原状。
 *
 * 判据：
 *   * 对比度：正文 ≥ 4.5:1、大号/粗体（≥18.66px，或 ≥14px 且 600+）≥ 3:1；
 *   * 浅色孤岛：深色下有效底色亮度 > 0.7 且面积 > 25000px²；
 *   * 排除项：渐变文字（background-clip:text / color:transparent）、alpha < 0.3 的文字色。
 *
 * ⚠️ 背景解析的三个坑（第 1 期实测踩过，别再踩）：
 *   a) `background-color: rgba(0,0,0,0)` 的祖先要**继续往上找**，否则会把文字判成"白底上的浅字"；
 *   b) 整页底色常常是 `background-image: gradient`（飞书登录/工作区都是），此时按主题取一个
 *      代表色（深 #101419 / 浅 #f3f6fa），否则渐变页会出现成片假阳性；
 *   c) 属性选择器/内联 style 里的颜色字符串不算"自己声明的底色"。
 *
 * ⚠️⚠️ 顺序坑（2026-10-26 第二次踩）：**必须先注入 `*{transition:none}`，再切 data-theme**。
 *   顺序反了的话，在自动化会话里（页签是隐藏态、过渡不推进）会读到**冻结的旧颜色** ——
 *   实测飞书 `.recent-run`（`transition: background .15s`）被误判成"深色下还是浅底"，
 *   一口气报出 20 条假违规。下面的 `runFor()` 已经是正确顺序（kill 在前、切主题在后）。
 */
(async () => {
  const IFrameIds = []; // 例：['iframe-seedance', 'iframe-nb', 'iframe-feishu-generation-agent']

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const parse = (value) => {
    const m = String(value).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[,/]/).map((x) => parseFloat(x));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const lin = (c) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const contrast = (a, b) => { const x = lum(a); const y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
    a: 1,
  });

  const CANVAS = { dark: { r: 16, g: 20, b: 25, a: 1 }, light: { r: 243, g: 246, b: 250, a: 1 } };

  function auditDocument(doc, win, mode) {
    const gcs = (el) => win.getComputedStyle(el);
    const bgOf = (el) => {
      let node = el;
      while (node && node !== doc.documentElement) {
        const st = gcs(node);
        const c = parse(st.backgroundColor);
        if (c && c.a > 0.55) return c;                       // (a) 只认不透明的
        if (st.backgroundImage && /gradient/.test(st.backgroundImage)) return CANVAS[mode]; // (b)
        node = node.parentElement;
      }
      const html = parse(gcs(doc.documentElement).backgroundColor);
      if (html && html.a > 0.55) return html;
      return CANVAS[mode];
    };

    const violations = [];
    const islands = [];
    for (const el of doc.querySelectorAll('body *')) {
      if (!(el instanceof win.HTMLElement)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 30 || rect.height < 16) continue;
      const st = gcs(el);
      if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) < 0.25) continue;

      const bgc = parse(st.backgroundColor);
      if (mode === 'dark' && bgc && bgc.a > 0.6 && lum(bgc) > 0.7 && rect.width * rect.height > 25000) {
        islands.push(`${el.tagName.toLowerCase()}.${String(el.className).split(/\s+/).slice(0, 2).join('.')} `
          + `${Math.round(rect.width)}x${Math.round(rect.height)} ${st.backgroundColor}`);
      }

      const ownText = [...el.childNodes].filter((n) => n.nodeType === 3 && n.textContent.trim().length > 1);
      if (!ownText.length) continue;
      const fg = parse(st.color);
      if (!fg || fg.a < 0.3) continue;                        // 渐变文字 / 透明色
      if (st.webkitTextFillColor === 'transparent' || st.backgroundClip === 'text' || st.webkitBackgroundClip === 'text') continue;

      const bg = bgOf(el);
      const ratio = contrast(over(fg, bg), bg);
      const size = parseFloat(st.fontSize) || 13;
      const bold = (parseInt(st.fontWeight, 10) || 400) >= 600;
      const floor = (size >= 18.66 || (size >= 14 && bold)) ? 3 : 4.5;
      if (ratio < floor) {
        violations.push({
          selector: `${el.tagName.toLowerCase()}.${String(el.className).split(/\s+/)[0]}`,
          text: ownText.map((n) => n.textContent.trim()).join(' ').slice(0, 20),
          ratio: Number(ratio.toFixed(2)),
          floor,
          color: st.color,
          background: `rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})`,
        });
      }
    }
    return {
      elements: doc.querySelectorAll('body *').length,
      violations: violations.length,
      worst: violations.sort((a, b) => a.ratio - b.ratio).slice(0, 8),
      islands: [...new Set(islands)].slice(0, 8),
    };
  }

  function runFor(doc, win) {
    const kill = doc.createElement('style');
    kill.textContent = '*{transition:none!important;animation:none!important}';
    doc.head.appendChild(kill);
    const result = {};
    for (const mode of ['light', 'dark']) {
      if (mode === 'dark') doc.documentElement.setAttribute('data-theme', 'dark');
      else doc.documentElement.removeAttribute('data-theme');
      result[mode] = auditDocument(doc, win, mode);
    }
    doc.documentElement.removeAttribute('data-theme');
    kill.remove();
    return result;
  }

  const report = { portal: runFor(document, window), iframes: {} };
  for (const id of IFrameIds) {
    const frame = document.getElementById(id);
    if (!frame || !frame.contentDocument) { report.iframes[id] = 'not-loaded'; continue; }
    report.iframes[id] = runFor(frame.contentDocument, frame.contentWindow);
    await sleep(200);
  }
  return JSON.stringify(report, null, 1);
})();
