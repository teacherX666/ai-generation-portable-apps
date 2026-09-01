// Dreamina（Portal 原生面板）使用说明配图截图脚本。
// 账号列表/历史/任务是真实数据 → 全部注入演示态；参考图走真实上传链路拿真实 URL。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const OUT = '/Users/260413a/ai-generation-portable-apps/docs/dreamina-manual/assets/';
const BASE = 'https://localhost:9090';

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const ctx = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
});
const SESSION_TOKEN = process.env.SD_MANUAL_SESSION;
if (!SESSION_TOKEN) throw new Error('缺少 SD_MANUAL_SESSION');
await ctx.addCookies([{ name: 'session', value: SESSION_TOKEN, domain: 'localhost', path: '/' }]);
const page = await ctx.newPage();

async function shot(name, locator = null, pad = 8) {
  if (locator) {
    await locator.scrollIntoViewIfNeeded();
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

// 演示参考图：山水插画
function drawDemo() {
  const c = document.createElement('canvas');
  c.width = 640; c.height = 640;
  const g = c.getContext('2d');
  const sky = g.createLinearGradient(0, 0, 0, 640);
  sky.addColorStop(0, '#ffe8c8'); sky.addColorStop(0.6, '#ffc9a3'); sky.addColorStop(1, '#f49f7a');
  g.fillStyle = sky; g.fillRect(0, 0, 640, 640);
  // 太阳
  g.fillStyle = '#ff7b54';
  g.beginPath(); g.arc(320, 250, 72, 0, 7); g.fill();
  // 远山
  g.fillStyle = '#7a6a8f';
  g.beginPath(); g.moveTo(0, 430); g.quadraticCurveTo(160, 250, 340, 430); g.closePath(); g.fill();
  g.fillStyle = '#5d4f75';
  g.beginPath(); g.moveTo(260, 430); g.quadraticCurveTo(430, 270, 640, 430); g.closePath(); g.fill();
  // 水面
  const w = g.createLinearGradient(0, 430, 0, 640);
  w.addColorStop(0, '#c98f6f'); w.addColorStop(1, '#8a5a48');
  g.fillStyle = w; g.fillRect(0, 430, 640, 210);
  // 水纹
  g.strokeStyle = 'rgba(255,255,255,0.35)'; g.lineWidth = 3;
  for (let i = 0; i < 5; i++) {
    g.beginPath();
    g.moveTo(80, 480 + i * 30);
    g.quadraticCurveTo(320, 470 + i * 30, 560, 480 + i * 30);
    g.stroke();
  }
  // 小船
  g.fillStyle = '#4a3b33';
  g.beginPath(); g.moveTo(220, 520); g.lineTo(420, 520); g.lineTo(390, 560); g.lineTo(250, 560); g.closePath(); g.fill();
  g.strokeStyle = '#4a3b33'; g.lineWidth = 5;
  g.beginPath(); g.moveTo(320, 520); g.lineTo(320, 440); g.stroke();
  g.fillStyle = '#f7f0e6';
  g.beginPath(); g.moveTo(324, 448); g.lineTo(430, 495); g.lineTo(324, 515); g.closePath(); g.fill();
  return c.toDataURL('image/png');
}

try {
  // A：Portal 入口
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#tab-btn-dreamina', { timeout: 20000 });
  await page.click('#tab-btn-dreamina');
  await page.waitForFunction(() => window._dmApp, null, { timeout: 20000 });
  await page.waitForTimeout(1200);
  const tb = await page.locator('.app-tabs-bar').boundingBox();
  await page.screenshot({
    path: OUT + 'A-dm-portal.png',
    clip: { x: 0, y: Math.max(0, tb.y - 10), width: 1440, height: tb.height + 20 },
  });
  console.log('shot A-dm-portal');

  // 注入演示账号状态（覆盖真实账号列表）
  const DEMO_PROMPT = '一只白色的小狐狸坐在森林中的木桩上，阳光透过树叶洒下光斑，温馨治愈的插画风格';
  await page.evaluate((prompt) => {
    const app = window._dmApp;
    app.accounts = [
      { id: 'demo-acc-1', name: '公司共享账号A', logged_in: true, credit: { total_credit: 12345 } },
      { id: 'demo-acc-2', name: '公司共享账号B', logged_in: true, credit: { total_credit: 8600 } },
      { id: 'demo-acc-3', name: '备用账号C', logged_in: false, credit: null },
    ];
    app.activeAccount = 'demo-acc-1';
    app.loggedIn = true;
    app.credit = '12,345 点';
    app.dispatchMode = 'round_robin';
    document.querySelector('#dm-form textarea[name="prompt"]').value = prompt;
  }, DEMO_PROMPT);
  await page.waitForTimeout(600);

  // B 全景（图片·文生图默认）
  await shot('B-dm-overview');

  // C 图片模式表单（子 tab + prompt + 比例/分辨率）
  await shot('C-dm-image-form', page.locator('#dm-form'));

  // D 图生图参考图（切子 tab + 真实上传演示图）
  await page.locator('.sub-tabs button', { hasText: '图生图' }).first().click();
  await page.waitForTimeout(400);
  const imgB64 = await page.evaluate(drawDemo);
  fs.writeFileSync('/tmp/dm-manual-demo.png', Buffer.from(imgB64.split(',')[1], 'base64'));
  await page.setInputFiles('#dm-imageRefs input[type="file"]', '/tmp/dm-manual-demo.png');
  await page.waitForFunction(() => document.querySelector('#dm-imageRefs .drop .preview'), null, { timeout: 15000 });
  await page.waitForTimeout(500);
  const demoUrl = await page.evaluate(() => {
    const img = document.querySelector('#dm-imageRefs .drop .preview');
    return img ? img.getAttribute('src') : '';
  });
  console.log('demo upload url:', demoUrl.slice(0, 50));
  await shot('D-dm-img2img', page.locator('#dm-form'));

  // E 视频·首尾帧（时长/视频分辨率/模型版本）
  await page.locator('.major-tabs button', { hasText: '视频' }).first().click();
  await page.waitForTimeout(400);
  await page.locator('.sub-tabs button', { hasText: '首尾帧' }).first().click();
  await page.waitForTimeout(400);
  await shot('E-dm-frames2video', page.locator('#dm-form'));

  // F 视频·多帧（关键帧工具栏）
  await page.locator('.sub-tabs button', { hasText: '多帧' }).first().click();
  await page.waitForTimeout(400);
  await shot('F-dm-multiframe', page.locator('#dm-form'));

  // G 任务卡（运行中演示卡）
  await page.evaluate(() => {
    const app = window._dmApp;
    app.wsTab = 'jobs';
    const el = document.getElementById('dm-jobsList');
    el.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'result ui-job-status-card is-running';
    card.innerHTML = '<div class="ui-job-status-card__title"><span class="ui-badge ui-badge--info">处理中</span> · Job demo8888</div>' +
      '<div class="ui-job-status-card__events">' +
      '<div><span class="ui-job-status-card__event-time">09:12:03</span> 任务已提交</div>' +
      '<div><span class="ui-job-status-card__event-time">09:12:08</span> 正在排队，等待公司账号空闲</div>' +
      '<div><span class="ui-job-status-card__event-time">09:12:15</span> 开始生成……</div>' +
      '</div>';
    el.prepend(card);
  });
  await page.waitForTimeout(400);
  await shot('G-dm-jobs', page.locator('#tab-dreamina .workspace .panel'));

  // H 历史记录（用真实渲染函数 + 演示数据 + 真实图 URL 当缩略图）
  await page.evaluate((url) => {
    const app = window._dmApp;
    app.wsTab = 'history';
    const list = document.getElementById('dm-historyList');
    list.innerHTML = '';
    const rel = url.replace('/dreamina', '');
    const items = [
      { status: 'completed', task_type: 'image2image', created_at: '2026-08-28 09:15', params: { prompt: '把参考图改成黄昏色调，保留构图' }, result: { files: [rel] }, cli_logs: [] },
      { status: 'completed', task_type: 'text2video(首尾帧)', created_at: '2026-08-27 16:40', params: { prompt: '清晨的城市街景，镜头缓慢推进' }, result: { files: [] }, cli_logs: [] },
      { status: 'failed', task_type: 'text2image', created_at: '2026-08-27 11:02', params: { prompt: '雨天霓虹灯街道' }, result: { files: [] }, cli_logs: ['登录失效，请重新登录'] },
    ];
    for (const it of items) list.appendChild(app._dmBuildHistCard(it));
  }, demoUrl);
  await page.waitForTimeout(600);
  await shot('H-dm-history', page.locator('#tab-dreamina .workspace .panel'));

  console.log('ALL-DM-SHOTS-DONE');
} finally {
  await browser.close();
}
