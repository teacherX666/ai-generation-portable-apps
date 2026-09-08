// 创意画布（无限画布）使用说明配图截图脚本。
// 通过生产 Portal（https://localhost:9090）用临时会话访问画布，创建演示项目，
// 截图后删除项目与临时会话。运行前需先往 portal/state/sessions.json 注入临时
// 会话并导出 IC_MANUAL_SESSION（见脚本底部注释）。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const OUT = '/Users/260413a/ai-generation-portable-apps/docs/infinite-canvas-manual/assets/';
const BASE = 'https://localhost:9090';
const SESSION_TOKEN = process.env.IC_MANUAL_SESSION;

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const ctx = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
});
if (!SESSION_TOKEN) throw new Error('缺少 IC_MANUAL_SESSION（先往 portal/state/sessions.json 注入临时会话）');
await ctx.addCookies([{ name: 'session', value: SESSION_TOKEN, domain: 'localhost', path: '/' }]);
const page = await ctx.newPage();
let projectId = null;

async function shot(name, locator = null, pad = 8) {
  if (locator) {
    await locator.scrollIntoViewIfNeeded().catch(() => {});
    const box = await locator.boundingBox();
    if (!box) throw new Error(`no box for ${name}`);
    const clip = {
      x: Math.max(0, box.x - pad),
      y: Math.max(0, box.y - pad),
      width: Math.min(1440, box.width + pad * 2),
      height: Math.min(900, box.height + pad * 2),
    };
    await page.screenshot({ path: OUT + name + '.png', clip });
  } else {
    await page.screenshot({ path: OUT + name + '.png' });
  }
  console.log('shot', name);
}

// 演示参考图：柔和渐变底 + 香水瓶剪影（在页面内绘制，转 File 后放进图片节点）
const DEMO_B64 = await (async () => {
  const p = await ctx.newPage();
  await p.goto('about:blank');
  const b64 = await p.evaluate(() => {
    const c = document.createElement('canvas');
    c.width = 720; c.height = 720;
    const g = c.getContext('2d');
    const bg = g.createLinearGradient(0, 0, 720, 720);
    bg.addColorStop(0, '#fde8d7'); bg.addColorStop(0.55, '#f6c9a6'); bg.addColorStop(1, '#c98d5f');
    g.fillStyle = bg; g.fillRect(0, 0, 720, 720);
    const glow = g.createRadialGradient(360, 300, 30, 360, 300, 380);
    glow.addColorStop(0, 'rgba(255,255,255,0.95)'); glow.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = glow; g.fillRect(0, 0, 720, 720);
    // 瓶身
    g.fillStyle = 'rgba(255,255,255,0.92)';
    g.beginPath(); g.roundRect(300, 220, 120, 280, 22); g.fill();
    g.fillStyle = '#b97a4a';
    g.beginPath(); g.roundRect(332, 120, 56, 90, 10); g.fill();
    g.fillStyle = 'rgba(255,255,255,0.55)';
    g.beginPath(); g.roundRect(318, 250, 28, 210, 12); g.fill();
    // 标签
    g.fillStyle = '#8a5a35'; g.font = 'bold 34px sans-serif'; g.textAlign = 'center';
    g.fillText('ROSE', 360, 380);
    g.font = '24px sans-serif'; g.fillStyle = '#a06b45';
    g.fillText('EAU DE PARFUM', 360, 420);
    return c.toDataURL('image/png').split(',')[1];
  });
  await p.close();
  return b64;
})();
const demoBuf = Buffer.from(DEMO_B64, 'base64');

try {
  // --- A. 项目列表（整页） ---
  await page.goto(BASE + '/infinite-canvas/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  await shot('A-canvas-home');

  // --- 新建画布 → 空画布 ---
  await page.locator('button:has-text("新建画布")').first().click();
  await page.waitForTimeout(2500);
  projectId = page.url().split('/').filter(Boolean).pop();
  console.log('project id:', projectId);
  await shot('B-canvas-empty');

  // --- C. 右键菜单（创建节点） ---
  await page.mouse.click(700, 300);
  await page.mouse.click(700, 300, { button: 'right' });
  await page.waitForTimeout(900);
  const menu = page.locator('[role="menu"][aria-label="创建节点"]').first();
  await shot('C-canvas-menu', menu, 24);
  // 关闭右键菜单
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);

  // --- 拖入演示图 → 画布自动创建「参考图片」节点 ---
  await page.evaluate(async ({ b64, x, y }) => {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const dt = new DataTransfer();
    dt.items.add(new File([arr], 'demo.png', { type: 'image/png' }));
    const el = document.elementFromPoint(x, y) || document.body;
    const ev = new DragEvent('drop', { bubbles: true, cancelable: true, clientX: x, clientY: y, dataTransfer: dt });
    el.dispatchEvent(ev);
  }, { b64: DEMO_B64, x: 350, y: 280 });
  await page.waitForTimeout(3000); // 上传 + 节点创建

  // --- 右键建「提示词」节点并写提示词 ---
  async function createNodeViaMenu(title, x, y) {
    await page.mouse.click(x, y);
    await page.mouse.click(x, y, { button: 'right' });
    await page.waitForTimeout(700);
    const item = page.locator(`[role="menuitem"]:has-text("${title}")`).first();
    await item.click();
    await page.waitForTimeout(700);
  }
  await createNodeViaMenu('提示词', 950, 320);
  const textarea = page.locator('textarea').first();
  if (await textarea.count()) {
    await textarea.click();
    await textarea.fill('参考图 1 的香水瓶造型，晨光中的花园，露珠、柔光、浅景深，广告海报质感');
    await page.waitForTimeout(400);
  }

  // --- 右键建「图片生成」节点 ---
  await createNodeViaMenu('图片生成', 620, 560);
  await shot('D-canvas-nodes');

  // --- E. 图片生成节点（选中态：模型下拉 + 运行按钮） ---
  const genNode = page.locator('[data-node-id]', { hasText: '图片生成' }).first();
  if (await genNode.count()) {
    await genNode.click();
    await page.waitForTimeout(900);
    await shot('E-canvas-config', genNode, 20);
  } else {
    await shot('E-canvas-config');
  }
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);

  // --- F. 人像资产库面板 ---
  await page.locator('button:has-text("人像资产库")').first().click();
  await page.waitForTimeout(1800);
  const libPanel = page.locator('aside[aria-label="人像资产库"]').first();
  if (await libPanel.count()) {
    await shot('F-canvas-asset-library', libPanel, 16);
  } else {
    await shot('F-canvas-asset-library');
  }

  console.log('ALL SHOTS DONE');
} catch (e) {
  console.error('SCREENSHOT FLOW ERROR:', e.message);
} finally {
  // 清理：删除演示项目
  if (projectId) {
    try {
      const ok = await page.evaluate(async (id) => {
        const r = await fetch(`/infinite-canvas/api/v1/projects/${encodeURIComponent(id)}`, { method: 'DELETE' });
        return r.ok || r.status === 204;
      }, projectId);
      console.log('demo project deleted:', ok);
    } catch (e) { console.error('delete project failed:', e.message); }
  }
  await browser.close();
}
