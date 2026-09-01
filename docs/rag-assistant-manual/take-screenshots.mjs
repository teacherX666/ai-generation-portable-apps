// 报错问答助手（rag-assistant）使用说明配图截图脚本。
// 聊天记录是真实数据 → 注入演示对话；页面直连（iframe 在无头 Chrome 偶发代理问题）。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';

const OUT = '/Users/260413a/ai-generation-portable-apps/docs/rag-assistant-manual/assets/';
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

// 演示「报错截图」缩略图
function drawErrorShot() {
  const c = document.createElement('canvas');
  c.width = 480; c.height = 300;
  const g = c.getContext('2d');
  g.fillStyle = '#0f172a'; g.fillRect(0, 0, 480, 300);
  g.font = 'bold 20px ui-monospace, monospace';
  g.fillStyle = '#f8fafc';
  g.fillText('任务失败', 30, 60);
  g.font = '15px ui-monospace, monospace';
  g.fillStyle = '#fbbf24';
  g.fillText('Ark API Error: invalid param', 30, 110);
  g.fillStyle = '#94a3b8';
  g.fillText('ratio \"21:7\" 不在支持列表 [16:9 9:16 ...]', 30, 145);
  g.fillText('trace: submit -> build_payload', 30, 178);
  g.fillStyle = '#ef4444';
  g.fillText('[failed] job aborted', 30, 220);
  g.strokeStyle = '#334155'; g.lineWidth = 2;
  g.strokeRect(10, 10, 460, 280);
  return c.toDataURL('image/png');
}

try {
  // A：Portal 入口
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#tab-btn-rag-assistant', { timeout: 20000 });
  await page.click('#tab-btn-rag-assistant');
  await page.waitForTimeout(800);
  const tb = await page.locator('.app-tabs-bar').boundingBox();
  await page.screenshot({
    path: OUT + 'A-rag-portal.png',
    clip: { x: 0, y: Math.max(0, tb.y - 10), width: 1440, height: tb.height + 20 },
  });
  console.log('shot A-rag-portal');

  // 助手页面直连
  await page.goto(BASE + '/rag-assistant/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#q', { timeout: 20000 });
  await page.waitForTimeout(800);

  // B 空状态 + 输入区（填演示报错文字）
  await page.fill('#q', '生成任务失败：Ark API Error: invalid param，ratio 参数 21:7 不在支持列表');
  await shot('B-rag-empty', null);

  // C 对话示例（演示问答 + 复制按钮 + 缩略图）
  const errShot = await page.evaluate(drawErrorShot);
  await page.evaluate(({ errShot }) => {
    const chat = document.getElementById('chat');
    const q = document.createElement('div');
    q.className = 'msg q';
    q.innerHTML = '生成任务失败：Ark API Error: invalid param，ratio 参数 21:7 不在支持列表';
    chat.appendChild(q);
    const a = document.createElement('div');
    a.className = 'msg a';
    a.innerHTML =
      '<div class="answer-head"><span class="answer-label">回答</span><button type="button" class="copy-answer ui-btn ui-btn--secondary">复制</button></div>' +
      '<section class="answer-section"><div class="answer-section-title">问题原因</div><div class="answer-section-body"><p>ratio 参数「21:7」不是方舟支持的画幅比例。有效值：16:9、9:16、1:1、4:3、3:4、21:9、9:21、adaptive。</p></div></section>' +
      '<section class="answer-section"><div class="answer-section-title">解决办法</div><div class="answer-section-body"><p>把「比例」改为 21:9 或 adaptive 后重新提交即可。已在提交表单里校验过该参数，可直接改。</p></div></section>';
    chat.appendChild(a);
    // 缩略图预览
    const prev = document.getElementById('previews');
    if (prev) {
      const t = document.createElement('div');
      t.className = 'thumb';
      t.innerHTML = '<img src="' + errShot + '" alt="报错截图"><span class="x">×</span>';
      prev.appendChild(t);
    }
    chat.scrollTop = chat.scrollHeight;
  }, { errShot });
  await page.waitForTimeout(500);
  await shot('C-rag-chat', null);

  console.log('ALL-RAG-SHOTS-DONE');
} finally {
  await browser.close();
}
