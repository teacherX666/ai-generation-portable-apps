// 分镜布局浏览器冒烟测试：真实加载页面，断言模块求值、WebGL、核心交互全链路。
//
// 依赖：playwright（复用 infinite-canvas/web/node_modules 的安装）+
//       系统 Chrome（channel: 'chrome'）。curl/语法检查发现不了的问题
//       （import map 解析、模块求值异常、WebGL、DOM 接线）只有真浏览器能抓。
//
// 运行（DATA_DIR 必须每次全新，否则会读到上次运行存下的镜头/角色，断言失败）：
//   cd /Users/260413a/ai-generation-portable-apps/previz
//   rm -rf /tmp/previz-smoke
//   DATA_DIR=/tmp/previz-smoke PORT=8897 /opt/homebrew/bin/python3.12 app.py &
//   SERVER_PID=$!
//   node /Users/260413a/ai-generation-portable-apps/previz/tests/smoke-browser.mjs
//   kill $SERVER_PID
//
// 断言失败或出现 [pageerror] 即视为红。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';

const URL = process.env.PREVIZ_SMOKE_URL || 'http://127.0.0.1:8897/';
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage();
const pageErrors = [];
const consoleErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });

try {
  await page.goto(URL, { waitUntil: 'networkidle', timeout: 20000 });
  await page.waitForTimeout(2000);

  // 1. 模块求值 + WebGL 场景（app.js 若 import 失败，canvas 不会出现）
  const canvas = await page.evaluate(() => !!document.querySelector('#viewport canvas'));
  assert.ok(canvas, '#viewport 内应有 WebGL canvas（模块加载失败则没有）');
  assert.ok(!pageErrors.length, '不应有 pageerror: ' + pageErrors.join(' | '));
  assert.ok(!consoleErrors.filter((t) => !t.includes('404')).length,
    '不应有 console.error（favicon 除外）: ' + consoleErrors.join(' | '));

  // 2. 建镜头 + 加人物 + 加道具（接线与 THREE 场景）
  await page.click('#btn-shot-new');
  await page.waitForTimeout(300);
  assert.equal(await page.locator('.shot-item').count(), 1);
  await page.click('#btn-add-char');
  await page.click('#btn-add-prop');
  await page.click('#prop-picker button');   // 选第一类（箱子）
  await page.waitForTimeout(300);
  assert.equal(await page.locator('.char-label').count(), 1);

  // 2.5 碰撞体积：加一面墙，断言生成在原点的新道具被 SAT 最小穿透完全推开——
  //     不重叠，且间距 ∈ [0.001, 0.2]（推挤应刚好分离，推飞太远也是失败）
  //     选中道具后 picker 自动收起，需先重开
  await page.click('#btn-add-prop');
  await page.locator('#prop-picker button', { hasText: '墙壁' }).click();
  await page.waitForTimeout(300);
  const push = await page.evaluate(async () => {
    const { obbPenetration } = await import('/core.js');
    const s = window.__previzDebug;
    const recs = [...s.props.values()];
    if (recs.length < 2) return null;
    const foot = (rec) => {
      const sx = Math.abs(rec.group.scale.x), sz = Math.abs(rec.group.scale.z);
      const cx = rec.footprint.cx || 0, cz = rec.footprint.cz || 0;
      const cos = Math.cos(rec.group.rotation.y), sin = Math.sin(rec.group.rotation.y);
      return { x: rec.group.position.x + (cx * cos - cz * sin) * sx,
               z: rec.group.position.z + (cx * sin + cz * cos) * sz,
               hx: (rec.footprint ? rec.footprint.hx : 0.4) * sx,
               hz: (rec.footprint ? rec.footprint.hz : 0.4) * sz,
               ry: rec.group.rotation.y };
    };
    const a = foot(recs[0]), b = foot(recs[1]);
    // 间距 = 各轴 (|d| - projA - projB) 的最大值；>0 即分离（与 obbPenetration 同公式）
    const gapOf = (p, q) => {
      const ca = Math.cos(p.ry), sa = Math.sin(p.ry);
      const cb = Math.cos(q.ry), sb = Math.sin(q.ry);
      const dx = q.x - p.x, dz = q.z - p.z;
      let gap = -Infinity;
      for (const [ux, uz] of [[ca, sa], [-sa, ca], [cb, sb], [-sb, cb]]) {
        const projA = p.hx * Math.abs(ux * ca + uz * sa) + p.hz * Math.abs(ux * -sa + uz * ca);
        const projB = q.hx * Math.abs(ux * cb + uz * sb) + q.hz * Math.abs(ux * -sb + uz * cb);
        gap = Math.max(gap, Math.abs(ux * dx + uz * dz) - (projA + projB));
      }
      return gap;
    };
    return { overlap: obbPenetration(a, b) !== null, gap: gapOf(a, b) };
  });
  assert.ok(push, '应有墙与箱两个道具');
  assert.ok(!push.overlap, '生成在原点的新道具应被 SAT 推挤到完全不重叠');
  assert.ok(push.gap >= 0.001 && push.gap <= 0.2,
    `推挤间距应 ∈ [0.001, 0.2]（推飞太远也是失败），实际 ${push.gap.toFixed(4)}`);

  // 3. 渲染快照 → 预览弹窗 + 存档成功（覆盖 防抖冲刷 + multipart 存档链路）
  await page.click('#btn-render');
  await page.waitForTimeout(3500);
  assert.equal(await page.locator('#render-modal').evaluate((el) => el.hidden), false,
    '渲染后预览弹窗应可见');
  const status = (await page.locator('#render-status').textContent()) || '';
  assert.ok(status.includes('已存档'), '存档状态应为「已存档 ✓」，实际: ' + status);

  console.log('SMOKE-PASS');
} finally {
  await browser.close();
}
