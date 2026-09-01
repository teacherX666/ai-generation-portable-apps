// 分镜布局（previz）使用说明配图截图脚本。
// 用独立测试实例（DATA_DIR=/tmp/previz-manual PORT=8897）避免动生产项目数据，
// 通过真实 UI 操作搭出演示场景（新项目/镜头/人物/道具/相机/渲染/标注）。

import { chromium } from '/Users/260413a/ai-generation-portable-apps/infinite-canvas/web/node_modules/playwright/index.mjs';

const OUT = '/Users/260413a/ai-generation-portable-apps/docs/previz-manual/assets/';
const PREVIZ_URL = 'http://127.0.0.1:8897/';
const PORTAL_URL = 'https://localhost:9090';

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
});

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

try {
  // A：Portal 入口（只截标签栏，iframe 内容是生产实例不截取）
  const portalCtx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const SESSION = process.env.SD_MANUAL_SESSION;
  if (SESSION) await portalCtx.addCookies([{ name: 'session', value: SESSION, domain: 'localhost', path: '/' }]);
  const portalPage = await portalCtx.newPage();
  await portalPage.goto(PORTAL_URL + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await portalPage.waitForSelector('#tab-btn-previz', { timeout: 20000 });
  await portalPage.click('#tab-btn-previz');
  await portalPage.waitForTimeout(800);
  const tb = await portalPage.locator('.app-tabs-bar').boundingBox();
  await portalPage.screenshot({
    path: OUT + 'A-previz-portal.png',
    clip: { x: 0, y: Math.max(0, tb.y - 10), width: 1440, height: tb.height + 20 },
  });
  console.log('shot A-previz-portal');
  await portalCtx.close();

  // ============ 测试实例：搭演示场景 ============
  page.on('dialog', (d) => {
    const msg = d.message();
    if (msg.includes('项目名称')) d.accept('演示项目');
    else d.accept('演示');   // 改名等
  });
  await page.goto(PREVIZ_URL, { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForSelector('#viewport canvas', { timeout: 20000 });
  await page.waitForTimeout(1500);

  await page.click('#btn-project-new');
  await page.waitForTimeout(500);
  await page.click('#btn-shot-new');
  await page.waitForTimeout(500);
  await page.click('#btn-add-char');
  await page.waitForTimeout(500);
  // 加两个道具让场景有内容
  await page.click('#btn-add-prop');
  await page.locator('#prop-picker button', { hasText: '箱子' }).click();
  await page.waitForTimeout(400);
  await page.click('#btn-add-prop');
  await page.locator('#prop-picker button', { hasText: '树木' }).click();
  await page.waitForTimeout(400);

  // 选中人物（原点处），右侧对象面板出现详情
  const vp = await page.locator('#viewport').boundingBox();
  await page.mouse.click(vp.x + vp.width / 2, vp.y + vp.height / 2);
  await page.waitForTimeout(600);
  const objVisible = await page.locator('#object-detail').evaluate((el) => !el.hidden);
  console.log('object detail visible:', objVisible);

  // B 全景
  await shot('B-previz-overview');
  // C 对象面板
  await shot('C-previz-object', page.locator('#side-panel'), 6);
  // D 相机面板
  await page.click('#ptab-camera');
  await page.waitForTimeout(400);
  await shot('D-previz-camera', page.locator('#side-panel'), 6);
  // G 镜头列表
  await shot('G-previz-shots', page.locator('#shot-list'), 6);

  // E 渲染快照模态
  await page.click('#btn-render');
  await page.waitForTimeout(3500);
  const modalVisible = await page.locator('#render-modal').evaluate((el) => !el.hidden);
  console.log('render modal visible:', modalVisible);
  await shot('E-previz-render', page.locator('#render-modal .modal-box'), 6);

  // F 标注模式（画一个红框）
  await page.click('#btn-render-anno');
  await page.waitForTimeout(300);
  const stage = await page.locator('#render-stage').boundingBox();
  await page.mouse.move(stage.x + stage.width * 0.2, stage.y + stage.height * 0.2);
  await page.mouse.down();
  await page.mouse.move(stage.x + stage.width * 0.55, stage.y + stage.height * 0.6, { steps: 5 });
  await page.mouse.up();
  await page.waitForTimeout(400);
  await shot('F-previz-anno', page.locator('#render-modal .modal-box'), 6);
  await page.click('#btn-render-close');
  await page.waitForTimeout(300);

  console.log('ALL-PREVIZ-SHOTS-DONE');
} finally {
  await browser.close();
}
