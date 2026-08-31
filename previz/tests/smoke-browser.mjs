// 分镜布局浏览器冒烟测试：真实加载页面，断言模块求值、WebGL、核心交互全链路。
//
// 依赖：playwright（复用 infinite-canvas/web/node_modules 的安装）+
//       系统 Chrome（channel: 'chrome'）。curl/语法检查发现不了的问题
//       （import map 解析、模块求值异常、WebGL、DOM 接线）只有真浏览器能抓。
//
// 运行：
//   cd /Users/260413a/ai-generation-portable-apps/previz
//   DATA_DIR=/tmp/previz-smoke PORT=8897 /opt/homebrew/bin/python3.12 app.py &
//   SERVER_PID=$!
//   cd ../infinite-canvas/web && node ../previz/tests/smoke-browser.mjs
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
  await page.waitForTimeout(300);
  assert.equal(await page.locator('.char-label').count(), 1);

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
