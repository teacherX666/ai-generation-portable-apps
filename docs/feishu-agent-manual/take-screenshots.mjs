// 飞书任务 Agent（审批工作区）使用说明配图截图脚本。
// 页面会扫描真实多维表格任务 → 每个截图前强制写入演示 DOM（与真实渲染同款类名）。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';

const OUT = '/Users/260413a/ai-generation-portable-apps/docs/feishu-agent-manual/assets/';
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

// 演示「多维表格任务」行（与 app.js renderBitableTasks 同款类名）
function injectBitable() {
  const list = document.getElementById('bitable-task-list');
  if (!list) return;
  list.innerHTML = '';
  const mk = (title, meta, warning) => {
    const card = document.createElement('article');
    card.className = 'bitable-task';
    const identity = document.createElement('div');
    const h3 = document.createElement('h3');
    h3.textContent = title;
    identity.appendChild(h3);
    for (const m of meta) {
      const p = document.createElement('p');
      p.className = 'bitable-task-meta';
      p.textContent = m;
      identity.appendChild(p);
    }
    if (warning) {
      const w = document.createElement('p');
      w.className = 'bitable-task-warning';
      w.textContent = warning;
      identity.appendChild(w);
    }
    const link = document.createElement('a');
    link.textContent = '查看需求来源';
    link.href = '#';
    const claim = document.createElement('button');
    claim.className = 'primary';
    claim.type = 'button';
    claim.textContent = '开始分析';
    card.append(identity, link, claim);
    return card;
  };
  list.append(
    mk('Q3 品牌宣传片（动画类）', ['进度：待执行', '类型：动画类', '制作人：需求方-小王']),
    mk('代言人夏季海报（图片类）', ['进度：待执行', '类型：图片类', '制作人：需求方-小李']),
  );
  const status = document.getElementById('bitable-status');
  if (status) status.textContent = '发现 2 条可处理任务，请手动选择一条。';
}

// 演示「审批面板任务卡」（与 renderTask 同款类名）
function injectTaskCards() {
  const list = document.getElementById('task-list');
  if (!list) return;
  list.innerHTML = '';
  const card = document.createElement('article');
  card.className = 'task-card';
  card.innerHTML = `
    <div class="task-title-row">
      <input type="checkbox" checked aria-label="选择任务 1">
      <div>
        <h3>镜头1：产品特写旋转展示</h3>
        <span class="task-type">image_to_video · 置信度 0.92</span>
      </div>
    </div>
    <div class="task-grid">
      <div class="field field-wide"><label>提示词</label><textarea rows="3">镜头围绕桌上的香水瓶缓慢旋转 180°，晨光从侧窗洒入，瓶身高光流动，背景虚化的花园，电影感</textarea></div>
      <div class="field field-wide"><label>负面约束</label><textarea rows="2">不要出现人物、不要文字、不要水印</textarea></div>
      <div class="field"><label>画面比例</label><select><option>16:9</option><option selected>9:16</option></select></div>
      <div class="field"><label>视频时长</label><input type="number" value="8"></div>
      <div class="field"><label>分辨率</label><input value="720p"></div>
      <div class="field"><label>声音</label><select><option>开启</option><option selected>关闭</option></select></div>
    </div>
    <div class="task-notes">
      <span class="note">假设：素材图片1为产品主视觉</span>
      <span class="note">警告：参考图2清晰度偏低，建议更换</span>
    </div>`;
  list.appendChild(card);
}

// 演示审批底部操作栏
function injectActionBar() {
  for (const id of ['approve-button', 'reject-button', 'cancel-button']) {
    const b = document.getElementById(id);
    if (b) b.hidden = false;
  }
  const t = document.getElementById('action-title');
  if (t) t.textContent = '等待审批';
  const p = document.getElementById('polling-note');
  if (p) p.textContent = '计划已生成，请检查任务明细后批准或退回';
}

// 演示成片预览区
function injectArtifacts() {
  const sec = document.getElementById('artifact-review');
  if (!sec) return;
  sec.hidden = false;
  const list = document.getElementById('artifact-list');
  if (list) list.innerHTML = '<p class="mode-message">成片生成完成后显示在这里，可预览确认。</p>';
}

try {
  // A：Portal 入口
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#tab-btn-feishu-generation-agent', { timeout: 20000 });
  await page.click('#tab-btn-feishu-generation-agent');
  await page.waitForTimeout(1500);
  const tb = await page.locator('.app-tabs-bar').boundingBox();
  await page.screenshot({
    path: OUT + 'A-agent-portal.png',
    clip: { x: 0, y: Math.max(0, tb.y - 10), width: 1440, height: tb.height + 20 },
  });
  console.log('shot A-agent-portal');

  // 审批工作区：直连页面截图（Portal iframe 在无头 Chrome 下偶发
  // ERR_TOO_MANY_RETRIES，直连渲染完全一致；页面内容全部注入演示态）
  const fp = page;
  await fp.goto(BASE + '/feishu-generation-agent/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await fp.waitForSelector('#direct-run-panel', { timeout: 20000 });
  await fp.waitForTimeout(2000);

  // 停止轮询干扰（把 interval 全清掉，纯前端演示）
  await fp.evaluate(() => {
    for (let i = 1; i < 99999; i++) clearInterval(i);
    for (let i = 1; i < 99999; i++) clearTimeout(i);
  });

  // C 直接分析面板
  await fp.fill('#direct-run-url', 'https://xxx.feishu.cn/wiki/需求文档-示例');
  await fp.evaluate(() => {
    const m = document.getElementById('direct-run-mode');
    if (m) m.value = 'image';
  });
  await shot('C-agent-direct', fp.locator('#direct-run-panel'), 6);

  // D 多维表格任务区（演示行）
  await fp.evaluate(injectBitable);
  await fp.waitForTimeout(300);
  await shot('D-agent-bitable', fp.locator('#bitable-panel'), 6);

  // E 审批面板（演示任务卡 + 操作栏）
  await fp.evaluate(injectTaskCards);
  await fp.evaluate(injectActionBar);
  await fp.evaluate(() => {
    const dt = document.getElementById('document-title');
    if (dt) dt.textContent = 'Q3 品牌宣传片 · 需求文档';
    const cov = document.getElementById('coverage-label');
    if (cov) cov.textContent = '已使用 3 / 共 5 张';
  });
  await fp.waitForTimeout(300);
  await shot('E-agent-review', fp.locator('.review-panel'), 6);

  // F 成片预览区 + 操作栏（底部 action-bar 含批准按钮）
  await fp.evaluate(injectArtifacts);
  await fp.evaluate(() => {
    const s = document.getElementById('status-badge');
    if (s) s.textContent = '等待成片确认';
  });
  await fp.waitForTimeout(300);
  await shot('F-agent-actions', fp.locator('footer.action-bar'), 4);
  await shot('G-agent-artifacts', fp.locator('#artifact-review'), 6);

  console.log('ALL-AGENT-SHOTS-DONE');
} finally {
  await browser.close();
}
