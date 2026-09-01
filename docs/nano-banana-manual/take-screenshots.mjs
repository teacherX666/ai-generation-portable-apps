// 图像生成模块（nano-banana）使用说明配图截图脚本。模式同 seedance：
// 临时会话 + 独立 ws + 注入演示态。运行前需先往 portal/state/sessions.json
// 注入临时会话并导出 SD_MANUAL_SESSION。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const OUT = '/Users/260413a/ai-generation-portable-apps/docs/nano-banana-manual/assets/';
const BASE = 'https://localhost:9090';
const WS = 'docs-manual-nb';

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const ctx = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
});
const SESSION_TOKEN = process.env.SD_MANUAL_SESSION;
if (!SESSION_TOKEN) throw new Error('缺少 SD_MANUAL_SESSION（先注入临时会话）');
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

// 演示参考图：香氛产品海报（渐变底 + 瓶身 + 高光）
function drawDemoImage() {
  const c = document.createElement('canvas');
  c.width = 720; c.height = 720;
  const g = c.getContext('2d');
  const bg = g.createLinearGradient(0, 0, 720, 720);
  bg.addColorStop(0, '#fde8d7'); bg.addColorStop(0.55, '#f6c9a6'); bg.addColorStop(1, '#c98d5f');
  g.fillStyle = bg; g.fillRect(0, 0, 720, 720);
  // 圆形光晕
  const glow = g.createRadialGradient(360, 300, 30, 360, 300, 400);
  glow.addColorStop(0, 'rgba(255,255,255,0.85)'); glow.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = glow; g.fillRect(0, 0, 720, 720);
  // 影子
  g.fillStyle = 'rgba(120,70,40,0.25)';
  g.beginPath(); g.ellipse(360, 620, 150, 28, 0, 0, 7); g.fill();
  // 瓶身
  const body = g.createLinearGradient(300, 0, 460, 0);
  body.addColorStop(0, '#fff6ec'); body.addColorStop(0.5, '#f3d9bd'); body.addColorStop(1, '#caa377');
  g.fillStyle = body;
  g.beginPath(); g.roundRect(300, 320, 120, 280, 18); g.fill();
  // 瓶肩 + 瓶颈 + 瓶盖
  g.fillStyle = '#caa377';
  g.beginPath(); g.roundRect(322, 280, 76, 55, 10); g.fill();
  g.fillStyle = '#7c5a3a';
  g.beginPath(); g.roundRect(330, 230, 60, 58, 8); g.fill();
  // 标签
  g.fillStyle = 'rgba(255,255,255,0.92)';
  g.beginPath(); g.roundRect(312, 430, 96, 110, 8); g.fill();
  g.fillStyle = '#7c5a3a';
  g.font = 'bold 34px -apple-system, "PingFang SC", sans-serif';
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText('NANO', 360, 470);
  g.font = '15px -apple-system, "PingFang SC", sans-serif';
  g.fillStyle = '#a08363';
  g.fillText('EAU DE PARFUM', 360, 510);
  // 高光条
  g.fillStyle = 'rgba(255,255,255,0.55)';
  g.beginPath(); g.roundRect(312, 330, 26, 250, 13); g.fill();
  // 星光点缀
  for (let i = 0; i < 40; i++) {
    g.fillStyle = `rgba(255,255,255,${0.35 + Math.random() * 0.6})`;
    const x = Math.random() * 720, y = Math.random() * 560;
    g.beginPath(); g.arc(x, y, 1 + Math.random() * 3, 0, 7); g.fill();
  }
  return c.toDataURL('image/png');
}

try {
  // ============ A：Portal 入口（图像生成模块标签高亮） ============
  const portalPage = await ctx.newPage();
  await portalPage.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await portalPage.waitForSelector('#tab-btn-nb', { timeout: 20000 });
  await portalPage.click('#tab-btn-nb');
  await portalPage.waitForTimeout(1200);
  const tb = await portalPage.locator('.app-tabs-bar').boundingBox();
  await portalPage.screenshot({
    path: OUT + 'A-nb-portal.png',
    clip: { x: 0, y: Math.max(0, tb.y - 10), width: 1440, height: tb.height + 20 },
  });
  console.log('shot A-nb-portal');
  await portalPage.close();

  // ============ nano-banana 页面 ============
  await page.goto(`${BASE}/nano-banana/index.html?ws=${WS}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForFunction(() => window._app_nb && window._app_nb.tabs && window._app_nb.tabs.length, null, { timeout: 20000 });
  await page.waitForTimeout(1200);
  await page.evaluate(() => { clearInterval(window._app_nb._loadJobsTimer); });

  const DEMO_PROMPT = '参考 Image 1 的香水瓶造型，把它放进一个晨光中的花园，露珠、柔光、浅景深，广告海报质感';
  await page.fill('textarea[name="prompt"]', DEMO_PROMPT);

  // 上传演示参考图（真实链路）
  const imgB64 = await page.evaluate(drawDemoImage);
  fs.writeFileSync('/tmp/nb-manual-demo.png', Buffer.from(imgB64.split(',')[1], 'base64'));
  await page.setInputFiles('input[name="image_1"]', '/tmp/nb-manual-demo.png');
  await page.waitForFunction(
    () => document.querySelector('#nb-imageRefs .drop .preview'),
    null, { timeout: 15000 }
  );
  await page.waitForTimeout(500);
  const demoImgUrl = await page.evaluate(() => {
    const img = document.querySelector('#nb-imageRefs .drop .preview');
    return img ? img.getAttribute('src') : '';
  });
  console.log('demo upload url:', demoImgUrl.slice(0, 60));

  // 演示多主题（含绿点）
  await page.evaluate((ws) => {
    const app = window._app_nb;
    app.tabs = [
      { id: ws, name: '主题 1 · 香水海报', running: false },
      { id: 'nb-demo-2', name: '主题 2 · 人物精修', running: true },
      { id: 'nb-demo-3', name: '主题 3 · 电商白底图', running: false },
    ];
  }, WS);
  await page.waitForTimeout(400);

  // B 全景
  await shot('B-nb-overview');
  // C 提示词
  await shot('C-nb-prompt', page.locator('.workspace .panel').first());
  // D 参考图区
  await shot('D-nb-upload', page.locator('#nb-imageRefs').locator('..'));
  // F 多主题标签
  await shot('F-nb-tabs', page.locator('.app-tabs'), 6);
  // G 接口区
  await shot('G-nb-provider', page.locator('#nb-form section').first(), 10);
  // H 参数区
  const paramSec = page.locator('#nb-form section').filter({ hasText: '模式' }).first();
  await shot('H-nb-params', paramSec, 10);
  // I Image Resize 区
  await shot('I-nb-resize', page.locator('.resizeBox'), 10);

  // E1 运行中
  await page.evaluate(() => {
    const app = window._app_nb;
    app.statusText = 'running 1/1';
    app.eventsText = [
      '[09:12:03] 任务已提交',
      '[09:12:05] 模型排队中，等待调度',
      '[09:12:10] 开始绘制图像……',
      '[09:12:26] 生成完成，正在回传结果',
    ].join('\n');
  });
  await page.waitForFunction(() => document.getElementById('nb-results'));
  await page.evaluate(() => {
    window._app_nb._renderJobToDom({
      status: 'running', done: 1, total: 1,
      events: [
        { time: '09:12:03', message: '任务已提交' },
        { time: '09:12:05', message: '模型排队中，等待调度' },
        { time: '09:12:10', message: '开始绘制图像……' },
        { time: '09:12:26', message: '生成完成，正在回传结果' },
      ],
    });
  });
  await page.waitForFunction(() => document.querySelector('#nb-results .ui-job-status-card'));
  await page.waitForTimeout(400);
  await shot('E1-nb-running', page.locator('.workspace .panel').last());

  // E2 已完成结果（结果图用真实已上传的演示图 URL，避免裂图）
  await page.evaluate((url) => {
    const app = window._app_nb;
    app.statusText = '已完成';
    app.eventsText = '';
    window._app_nb._renderJobToDom({
      status: 'succeeded', done: 1, total: 1,
      events: [{ time: '09:12:26', message: '生成完成' }],
      results: [{ index: 1, task_id: 't-01',
        images: [{ download_url: url.replace('/nano-banana', ''), filename: '花园香水海报-1.png' }] }],
    });
  }, demoImgUrl);
  await page.waitForFunction(() => document.querySelector('#nb-results .ui-result-card img'));
  await page.waitForTimeout(500);
  await shot('E2-nb-result', page.locator('.workspace .panel').last());

  // J 存档区
  await page.evaluate(() => {
    const app = window._app_nb;
    app.archives = [{ name: '电商白底图模板' }, { name: '香水海报-方形' }];
    app.selectedArchive = '电商白底图模板';
    app.archiveHint = '已读取存档：电商白底图模板';
  });
  await page.waitForTimeout(400);
  await shot('J-nb-archives', page.locator('.archiveBox'), 10);

  // K 活动详情（恢复参数）
  await page.evaluate((prompt) => {
    const app = window._app_nb;
    app.wsTab = 'activity';
    app.activityCounts = { total: 9, succeeded: 8, failed: 1 };
    app.activityRecords = [
      { id: 'act-1', status: 'succeeded', created_at: '2026-08-28 09:15:22', username: '演示用户', title: prompt.slice(0, 40) },
    ];
    app.activityDetail = {
      status: 'succeeded',
      created_at: '2026-08-28 09:15:22',
      restore: {
        values: {
          prompt: prompt, mode: 'img2img', model: 'gemini-3-pro-image',
          aspect_ratio: 'auto', image_size: '2K', repeat_count: '1', concurrency: '1',
        },
        media: { image_1: { filename: 'nb-manual-demo.png', mime: 'image/png' } },
      },
      result: { done: 1, total: 1 },
    };
  }, DEMO_PROMPT);
  await page.waitForTimeout(500);
  await shot('K-nb-activity', page.locator('.workspace .panel').last());

  console.log('ALL-NB-SHOTS-DONE');
} finally {
  await browser.close();
}
