// 人像生成（volcengine-portrait，Portal 原生面板）使用说明配图截图脚本。
// 面板数据是真实资产/任务 → 全部用演示态覆盖（虚构资产 + data URL 缩略图 +
// ffmpeg 生成的小视频），截图不含任何真实同事素材。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import { execSync } from 'node:child_process';

const OUT = '/Users/260413a/ai-generation-portable-apps/docs/portrait-manual/assets/';
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

// 演示头像（真人感插画头像，单参数 hue）
function drawFace(hue) {
  const c = document.createElement('canvas');
  c.width = 160; c.height = 160;
  const g = c.getContext('2d');
  g.fillStyle = `hsl(${hue}, 55%, 88%)`; g.fillRect(0, 0, 160, 160);
  g.fillStyle = `hsl(${hue}, 30%, 68%)`;
  g.beginPath(); g.arc(80, 60, 34, 0, 7); g.fill();          // 头
  g.beginPath(); g.ellipse(80, 135, 52, 34, 0, 0, 7); g.fill(); // 肩
  g.fillStyle = `hsl(${hue}, 40%, 38%)`;
  g.beginPath(); g.arc(80, 44, 34, Math.PI, 0); g.fill();    // 头发
  g.fillStyle = '#1f2937';
  g.beginPath(); g.arc(68, 62, 4, 0, 7); g.fill();           // 眼睛
  g.beginPath(); g.arc(92, 62, 4, 0, 7); g.fill();
  g.fillStyle = `hsl(${hue}, 55%, 62%)`;
  g.beginPath(); g.arc(80, 74, 8, 0, Math.PI); g.fill();     // 微笑
  return c.toDataURL('image/png');
}

try {
  // A：Portal 入口
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#tab-btn-volcengine-portrait', { timeout: 20000 });
  await page.click('#tab-btn-volcengine-portrait');
  await page.waitForFunction(() => window._vpApp, null, { timeout: 20000 });
  await page.waitForTimeout(1200);
  const tb = await page.locator('.app-tabs-bar').boundingBox();
  await page.screenshot({
    path: OUT + 'A-portrait-portal.png',
    clip: { x: 0, y: Math.max(0, tb.y - 10), width: 1440, height: tb.height + 20 },
  });
  console.log('shot A-portrait-portal');

  // 演示数据：三个资产（两个头像 + 一个视频素材占位）、一个组、已选资产
  const face1 = await page.evaluate(drawFace, 18);
  const face2 = await page.evaluate(drawFace, 200);
  const DEMO_PROMPT = '让图片1中的女孩在樱花树下转身微笑，微风吹起头发，电影感，浅景深，4k';
  await page.evaluate(({ f1, f2, prompt }) => {
    const app = window._vpApp;
    app.groups = [
      { group_id: 'g_20260610_101122_abcd', name: '代言人-A组', asset_count: 3, created_at: '2026-06-10 10:11' },
      { group_id: 'g_20260702_153000_ef01', name: 'Q3活动组', asset_count: 5, created_at: '2026-07-02 15:30' },
    ];
    app.assets = [
      { asset_id: 'asset_demo_1001', file_name: '代言人A-正面微笑.png', url: f1, asset_type: 'Image', status: 'active', created_at: '2026-06-10 10:15' },
      { asset_id: 'asset_demo_1002', file_name: '代言人A-侧脸.png', url: f2, asset_type: 'Image', status: 'active', created_at: '2026-06-10 10:16' },
      { asset_id: 'asset_demo_1003', file_name: 'Q3-动捕走位.mp4', url: '', asset_type: 'Video', status: 'processing', created_at: '2026-07-02 15:35' },
    ];
    app.assetGroupId = 'g_20260610_101122_abcd';
    app.genAssetId = 'asset_demo_1001';
    app.extraAssetIds = ['asset_demo_1002'];
    app.prompt = prompt;
    app.duration = 12; app.resolution = '720p'; app.ratio = '16:9'; app.repeat = 1;
  }, { f1: face1, f2: face2, prompt: DEMO_PROMPT });
  await page.waitForTimeout(600);

  // B 全景（视口）
  await shot('B-portrait-overview');

  // C 左列（建组 + 上传素材）
  const leftCol = page.locator('#tab-volcengine-portrait .portrait-grid > div').first();
  await shot('C-portrait-leftcol', leftCol, 10);

  // D 我的资产
  const assetsCard = page.locator('#tab-volcengine-portrait section.card', { hasText: '我的资产' }).first();
  await shot('D-portrait-assets', assetsCard, 10);

  // E 生成视频区（带已完成结果小视频）——按 h2 过滤，避免命中顶部提示条里的「生成视频」字样
  const genCard = page.locator('#tab-volcengine-portrait section.card')
    .filter({ has: page.locator('h2', { hasText: '生成视频' }) }).first();
  execSync('ffmpeg -y -f lavfi -i color=c=0x7c5cff:s=320x180:d=2 -pix_fmt yuv420p /tmp/vp-demo.mp4 2>/dev/null');
  const mp4B64 = fs.readFileSync('/tmp/vp-demo.mp4').toString('base64');
  await page.evaluate((b64) => {
    const app = window._vpApp;
    app.events = '<div>10:21:03 任务已提交至火山方舟</div><div>10:21:11 开始生成视频……</div><div>10:21:42 生成完成，正在回传结果</div>';
    app.results = [{ url: 'data:video/mp4;base64,' + b64, filename: '樱花树下转身-1.mp4' }];
    app.statusText = '已完成';
  }, mp4B64);
  await page.waitForTimeout(600);
  await shot('E-portrait-generate', genCard, 10);

  // F 生成历史（无视频元素的纯状态行，避免裂图）
  await page.evaluate((prompt) => {
    const app = window._vpApp;
    app.jobs = [
      { job_id: 'vp-demo-j1', status: 'succeeded', prompt: prompt.slice(0, 40), username: '演示用户', created_at: '2026-08-28 10:24', submitted_at: 1780000000, started_at: 1780000001, finished_at: 1780000145, results: [] },
      { job_id: 'vp-demo-j2', status: 'failed', prompt: '让图片1中的女孩在雨夜街头回头……', username: '演示用户', created_at: '2026-08-28 09:02', results: [], errors: ['[permission_denied] 配额不足'] },
    ];
  }, DEMO_PROMPT);
  await page.waitForTimeout(600);
  const histCard = page.locator('#tab-volcengine-portrait section.card')
    .filter({ has: page.locator('h2', { hasText: '生成历史' }) }).first();
  await shot('F-portrait-history', histCard, 10);

  console.log('ALL-PORTRAIT-SHOTS-DONE');
} finally {
  await browser.close();
}
