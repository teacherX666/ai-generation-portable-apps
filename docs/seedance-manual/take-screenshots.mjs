// Seedance 使用说明配图截图脚本。
//
// 用独立 workspace（?ws=docs-manual-demo）打开 seedance 页面，隔离真实用户数据；
// 注入演示状态（提示词 / canvas 绘制的参考图 / 多主题绿点 / 运行中卡片 / 历史 /
// 存档 / 活动详情 / 延长模式），逐张截图到 assets/。
//
// 运行：
//   node /Users/260413a/ai-generation-portable-apps/docs/seedance-manual/take-screenshots.mjs
//
// 依赖：playwright（复用 infinite-canvas/web/node_modules）+ 系统 Chrome；
//       Portal 必须在跑（https://localhost:9090，自签证书 → ignoreHTTPSErrors）。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const OUT = '/Users/260413a/ai-generation-portable-apps/docs/seedance-manual/assets/';
const BASE = 'https://localhost:9090';
const WS = 'docs-manual-demo';

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const ctx = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
});
// 临时截图会话（虚构用户「使用说明截图」，portal/state/sessions.json 注入，截完删除）
const SESSION_TOKEN = process.env.SD_MANUAL_SESSION || 'sd-manual-ce24eb08422d154109e32b9fd2f8f013';
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

// ---- 演示参考图：canvas 手绘一只宇航猫（星空 + 月球地面） --------------------
function drawDemoImage() {
  const c = document.createElement('canvas');
  c.width = 640; c.height = 640;
  const g = c.getContext('2d');
  const bg = g.createLinearGradient(0, 0, 0, 640);
  bg.addColorStop(0, '#0a0f2e'); bg.addColorStop(1, '#1b2a55');
  g.fillStyle = bg; g.fillRect(0, 0, 640, 640);
  for (let i = 0; i < 240; i++) {
    g.fillStyle = `rgba(255,255,255,${0.2 + Math.random() * 0.75})`;
    g.beginPath(); g.arc(Math.random() * 640, Math.random() * 390, Math.random() * 1.6, 0, 7); g.fill();
  }
  const mg = g.createLinearGradient(0, 420, 0, 640);
  mg.addColorStop(0, '#93a0ba'); mg.addColorStop(1, '#49536a');
  g.fillStyle = mg;
  g.beginPath(); g.ellipse(320, 720, 440, 310, 0, 0, 7); g.fill();
  g.fillStyle = 'rgba(40,48,66,.55)';
  g.beginPath(); g.ellipse(150, 560, 60, 22, 0, 0, 7); g.fill();
  g.beginPath(); g.ellipse(520, 610, 84, 30, 0, 0, 7); g.fill();
  g.beginPath(); g.ellipse(430, 520, 38, 14, 0, 0, 7); g.fill();
  // 尾巴
  g.strokeStyle = '#e8943a'; g.lineWidth = 26; g.lineCap = 'round';
  g.beginPath(); g.moveTo(228, 462); g.quadraticCurveTo(132, 386, 158, 296); g.stroke();
  // 身体 + 腿
  g.fillStyle = '#e8943a';
  g.beginPath(); g.ellipse(320, 472, 106, 72, 0, 0, 7); g.fill();
  g.beginPath(); g.ellipse(262, 520, 26, 20, 0, 0, 7); g.fill();
  g.beginPath(); g.ellipse(378, 520, 26, 20, 0, 0, 7); g.fill();
  // 背包
  g.fillStyle = '#b45309';
  g.beginPath(); g.ellipse(396, 442, 42, 54, -0.18, 0, 7); g.fill();
  g.fillStyle = '#fbbf24';
  g.beginPath(); g.arc(394, 444, 10, 0, 7); g.fill();
  // 头盔玻璃罩
  g.fillStyle = 'rgba(214,232,255,0.26)';
  g.beginPath(); g.arc(320, 332, 96, Math.PI, 0); g.fill();
  g.strokeStyle = '#c9d6e8'; g.lineWidth = 6;
  g.beginPath(); g.arc(320, 332, 96, Math.PI, 0); g.stroke();
  // 头 + 耳朵
  g.fillStyle = '#e8943a';
  g.beginPath(); g.arc(320, 348, 62, 0, 7); g.fill();
  g.beginPath(); g.moveTo(274, 308); g.lineTo(260, 254); g.lineTo(302, 292); g.fill();
  g.beginPath(); g.moveTo(366, 308); g.lineTo(380, 254); g.lineTo(338, 292); g.fill();
  // 脸
  g.fillStyle = '#1f2937';
  g.beginPath(); g.arc(298, 342, 8, 0, 7); g.fill();
  g.beginPath(); g.arc(342, 342, 8, 0, 7); g.fill();
  g.fillStyle = '#d97757';
  g.beginPath(); g.moveTo(320, 358); g.lineTo(311, 368); g.lineTo(329, 368); g.fill();
  g.strokeStyle = 'rgba(255,255,255,.85)'; g.lineWidth = 3;
  g.beginPath(); g.moveTo(272, 352); g.lineTo(232, 346); g.stroke();
  g.beginPath(); g.moveTo(368, 352); g.lineTo(408, 346); g.stroke();
  // 身后月尘
  for (let i = 0; i < 26; i++) {
    g.fillStyle = `rgba(147,160,186,${0.3 + Math.random() * 0.5})`;
    const x = 200 + Math.random() * 90, y = 430 + Math.random() * 110;
    g.beginPath(); g.arc(x, y, 2 + Math.random() * 5, 0, 7); g.fill();
  }
  return c.toDataURL('image/png');
}

try {
  // ================= A：Portal 入口（Seedance 标签高亮） =================
  const portalPage = await ctx.newPage();
  await portalPage.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await portalPage.waitForSelector('#tab-btn-seedance', { timeout: 20000 });
  await portalPage.waitForTimeout(800);
  const tabsBar = portalPage.locator('.app-tabs-bar');
  const tb = await tabsBar.boundingBox();
  await portalPage.screenshot({
    path: OUT + 'A-portal-entry.png',
    clip: { x: 0, y: Math.max(0, tb.y - 10), width: 1440, height: tb.height + 20 },
  });
  console.log('shot A-portal-entry');
  await portalPage.close();

  // ================= seedance 页面 =================
  await page.goto(`${BASE}/seedance/index.html?ws=${WS}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForFunction(() => window._app_sd && window._app_sd.tabs && window._app_sd.tabs.length, null, { timeout: 20000 });
  await page.waitForTimeout(1200);
  await page.evaluate(() => { clearInterval(window._app_sd._loadJobsTimer); });

  // 演示提示词
  const DEMO_PROMPT = '一只橘猫戴着宇航头盔，在月球表面慢跑，身后扬起月尘，电影感，8K 细节，镜头缓慢推近';
  await page.fill('textarea[name="prompt"]', DEMO_PROMPT);

  // 演示参考图：真实走上传链路（canvas 生成 → setInputFiles → 服务端存 → 预览）
  const imgB64 = await page.evaluate(drawDemoImage);
  fs.writeFileSync('/tmp/sd-manual-demo.png', Buffer.from(imgB64.split(',')[1], 'base64'));
  await page.setInputFiles('input[name="ref_image_1"]', '/tmp/sd-manual-demo.png');
  await page.waitForFunction(
    () => document.querySelector('#sd-imageRefs .drop .preview'),
    null, { timeout: 15000 }
  );
  await page.waitForTimeout(500);

  // 演示多主题（含一个绿点运行中）
  await page.evaluate((ws) => {
    const app = window._app_sd;
    app.tabs = [
      { id: ws, name: '主题 1 · 橘猫广告', running: false },
      { id: 'demo-topic-2', name: '主题 2 · 口红竖屏', running: true },
      { id: 'demo-topic-3', name: '主题 3 · 汽车场景', running: false },
    ];
  }, WS);
  await page.waitForTimeout(400);

  // B：界面全景
  await shot('B-overview');

  // C：提示词输入框特写
  await shot('C-prompt', page.locator('.promptPanel'));

  // D：素材上传区特写（参考图已上传预览）
  await shot('D-upload', page.locator('.uploadPanel'));

  // F：顶部多主题标签（绿点）
  await shot('F-tabs', page.locator('.app-tabs'), 6);

  // I：任务类型下拉
  await shot('I-taskmode', page.locator('#sd-form label', { hasText: '任务类型' }), 6);

  // E1：运行中状态（分两步：先让 v-if 渲染出 #sd-results 容器，再写入任务卡片，
  // 否则 _renderJobToDom 在同一 tick 找不到容器静默失败）
  await page.evaluate(() => {
    const app = window._app_sd;
    app.statusText = 'running 1/1';
    app.eventsText = [
      '[10:21:03] 任务已提交至火山方舟',
      '[10:21:05] 模型排队中，等待调度',
      '[10:21:11] 开始生成视频……',
      '[10:21:32] 生成完成，正在回传结果',
    ].join('\n');
  });
  await page.waitForFunction(() => document.getElementById('sd-results'));
  await page.evaluate(() => {
    window._app_sd._renderJobToDom({
      status: 'running', done: 1, total: 1,
      events: [
        { time: '10:21:03', message: '任务已提交至火山方舟' },
        { time: '10:21:05', message: '模型排队中，等待调度' },
        { time: '10:21:11', message: '开始生成视频……' },
        { time: '10:21:32', message: '生成完成，正在回传结果' },
      ],
    });
  });
  await page.waitForFunction(() => document.querySelector('#sd-results .ui-job-status-card'));
  await page.waitForTimeout(400);
  await shot('E1-running', page.locator('.resultPanel'));

  // E2：生成历史（含一条已完成记录）
  await page.evaluate((prompt) => {
    const app = window._app_sd;
    app.statusText = '空闲';
    app.eventsText = '';
    app.jobs = [{
      job_id: 'demo-j1', status: 'succeeded',
      prompt: prompt, username: '演示用户',
      created_at: '2026-08-30 10:24',
      submitted_at: 1780000000, started_at: 1780000001, finished_at: 1780000145,
      results: [{ download_url: '/api/download/demo-j1-1', filename: '橘猫月球漫步-1.mp4', index: 1, task_id: 't-01' }],
      errors: [],
    }];
  }, DEMO_PROMPT);
  await page.waitForTimeout(400);
  await shot('E2-history', page.locator('.resultPanel'));

  // G：✨ 优化结果
  await page.evaluate(() => {
    window._app_sd.optimizedPrompt =
      '画面中，一只戴着透明宇航头盔的橘猫在月球表面慢跑，身后扬起细碎的月尘。' +
      '低机位仰拍视角，前景散落尘埃颗粒，宇航服表面反射冷色环境光，背景星空深邃，' +
      '整体色调偏冷，胶片颗粒质感，镜头缓慢推近，电影感，8K 细节。';
  });
  await page.waitForTimeout(400);
  await shot('G-optimize', page.locator('.promptPanel'));

  // J：视频延长模式（2.5 模型，比例锁定 adaptive）
  await page.evaluate(() => {
    const app = window._app_sd;
    app.optimizedPrompt = '';
    const modelSel = document.querySelector('select[name="model"]');
    modelSel.value = 'doubao-seedance-2-5-260628';
    modelSel.dispatchEvent(new Event('change', { bubbles: true }));
    const taskSel = document.querySelector('#sd-form label select') ;
    // 任务类型 select 在「任务类型」label 内（v-model，无 name）
    const label = [...document.querySelectorAll('#sd-form label')]
      .find((l) => l.textContent.includes('任务类型'));
    const sel = label.querySelector('select');
    sel.value = 'extend';
    sel.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await page.waitForTimeout(500);
  const paramSection = page.locator('#sd-form section').filter({ hasText: '参数' }).first();
  await shot('J-extend', paramSection, 10);

  // H1：存档区
  await page.evaluate(() => {
    const app = window._app_sd;
    app.archives = [{ name: '口红广告-竖屏版' }, { name: '橘猫-16:9-4k' }];
    app.selectedArchive = '口红广告-竖屏版';
    app.archiveHint = '已读取存档：口红广告-竖屏版';
  });
  await page.waitForTimeout(400);
  await shot('H1-archives', page.locator('.archiveBox'), 10);

  // H2：活动记录详情（恢复参数）
  await page.evaluate((prompt) => {
    const app = window._app_sd;
    app.wsTab = 'activity';
    app.activityCounts = { total: 12, succeeded: 10, failed: 2 };
    app.activityRecords = [
      { id: 'act-1', status: 'succeeded', created_at: '2026-08-29 15:12:03', username: '演示用户', title: prompt.slice(0, 40) },
    ];
    app.activityDetail = {
      status: 'succeeded',
      created_at: '2026-08-29 15:12:03',
      restore: {
        values: {
          prompt: prompt,
          model: 'doubao-seedance-2-0-260128',
          duration: '12', resolution: '720p', ratio: '16:9',
          repeat_count: '1', concurrency: '1', output_name: '',
        },
        media: { ref_image_1: { filename: 'sd-manual-demo.png', mime: 'image/png' } },
      },
      result: { done: 1, total: 1, duration: 12 },
    };
  }, DEMO_PROMPT);
  await page.waitForTimeout(500);
  await shot('H2-activity', page.locator('.resultPanel'));

  console.log('ALL-SHOTS-DONE');
} finally {
  await browser.close();
}
