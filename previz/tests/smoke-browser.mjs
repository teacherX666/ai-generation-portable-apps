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
import fs from 'node:fs';

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
      // 与生产 footprintOf 同源公式：偏移先缩放后按 three.js Y 旋转
      const sx = Math.abs(rec.group.scale.x), sz = Math.abs(rec.group.scale.z);
      const ox = (rec.footprint.cx || 0) * sx, oz = (rec.footprint.cz || 0) * sz;
      const cos = Math.cos(rec.group.rotation.y), sin = Math.sin(rec.group.rotation.y);
      return { x: rec.group.position.x + ox * cos + oz * sin,
               z: rec.group.position.z - ox * sin + oz * cos,
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

  // 2.6 按需渲染：空闲时 three 帧计数必须静止（持续 60fps 会把 macOS WindowServer
  //     饿死——2026-08-31 看门狗强杀实锤）；交互时帧计数应增长
  const framesIdle = async (ms) => {
    const a = await page.evaluate(() => window.__previzDebug.renderer.info.render.frame);
    await page.waitForTimeout(ms);
    const b = await page.evaluate(() => window.__previzDebug.renderer.info.render.frame);
    return { a, b };
  };
  const idle1 = await framesIdle(2500);
  assert.equal(idle1.b, idle1.a, `空闲 2.5s 帧计数不应增长（按需渲染失效），${idle1.a}→${idle1.b}`);
  // 拖拽道具 → 帧增长
  const stageBox = await page.locator('#viewport').boundingBox();
  await page.mouse.move(stageBox.x + stageBox.width / 2, stageBox.y + stageBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(stageBox.x + stageBox.width / 2 + 120, stageBox.y + stageBox.height / 2, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(400);
  const afterDrag = await page.evaluate(() => window.__previzDebug.renderer.info.render.frame);
  assert.ok(afterDrag > idle1.b, `拖拽后帧计数应增长，${idle1.b}→${afterDrag}`);
  // 松手后再静止 2.5s → 帧计数再次静止
  const idle2 = await framesIdle(2500);
  assert.equal(idle2.b, idle2.a, `拖拽停止后帧计数应静止，${idle2.a}→${idle2.b}`);
  console.log(`按需渲染验证：空闲静止 ${idle1.a}→${idle1.b}，拖拽后 ${afterDrag}`);

  // 3. 渲染快照 → 预览弹窗 + 存档成功（覆盖 防抖冲刷 + multipart 存档链路）
  await page.click('#btn-render');
  await page.waitForTimeout(3500);
  assert.equal(await page.locator('#render-modal').evaluate((el) => el.hidden), false,
    '渲染后预览弹窗应可见');
  const status = (await page.locator('#render-status').textContent()) || '';
  assert.ok(status.includes('已存档'), '存档状态应为「已存档 ✓」，实际: ' + status);

  // 4. 图片标注：开标注 → 画矩形框 → 合并 → 断言 img src 变化（标注版数据 URL）
  await page.click('#btn-render-anno');
  const stage = await page.locator('#render-stage').boundingBox();
  await page.mouse.move(stage.x + stage.width * 0.2, stage.y + stage.height * 0.2);
  await page.mouse.down();
  await page.mouse.move(stage.x + stage.width * 0.5, stage.y + stage.height * 0.5, { steps: 5 });
  await page.mouse.up();
  await page.waitForTimeout(300);
  const annoCount = await page.locator('#anno-layer rect').count();
  assert.equal(annoCount, 1, '应画出 1 个矩形框');
  const srcBefore = await page.locator('#render-img').getAttribute('src');
  await page.click('#anno-bake');
  await page.waitForFunction((before) => {
    const img = document.getElementById('render-img');
    return img.src !== before && img.src.startsWith('data:image/png');
  }, srcBefore, { timeout: 5000 });
  const srcAfter = await page.locator('#render-img').getAttribute('src');
  assert.notEqual(srcAfter, srcBefore, '合并后 img src 应变为标注版');
  assert.ok(srcAfter.startsWith('data:image/png'), '合并结果应为 PNG data URL');
  // 合并后 PNG 尺寸应等于渲染尺寸（标题里的 width×height）
  const title = (await page.locator('#render-title').textContent()) || '';
  const m = title.match(/(\d+)\s*×\s*(\d+)/);
  assert.ok(m, '渲染标题应含 width×height，实际: ' + title);
  const expectedW = Number(m[1]), expectedH = Number(m[2]);
  const dims = await page.evaluate(async (src) => {
    const img = new Image();
    img.src = src;
    await img.decode();
    return { w: img.naturalWidth, h: img.naturalHeight };
  }, srcAfter);
  assert.equal(dims.w, expectedW, `合并后 PNG 宽度应等于渲染宽度 ${expectedW}，实际 ${dims.w}`);
  assert.equal(dims.h, expectedH, `合并后 PNG 高度应等于渲染高度 ${expectedH}，实际 ${dims.h}`);
  console.log(`合并后 PNG 尺寸 ${dims.w}×${dims.h}（渲染标题 ${expectedW}×${expectedH}）`);
  // 合并后 SVG 标注层应清空；标注仍激活，可直接在标注版底图上继续画（原图可继续改）
  assert.equal(await page.locator('#anno-layer rect').count(), 0, '合并后标注层应清空');
  await page.mouse.move(stage.x + stage.width * 0.6, stage.y + stage.height * 0.6);
  await page.mouse.down();
  await page.mouse.move(stage.x + stage.width * 0.7, stage.y + stage.height * 0.7, { steps: 3 });
  await page.mouse.up();
  await page.waitForTimeout(300);
  assert.equal(await page.locator('#anno-layer rect').count(), 1, '合并后应能在标注版底图上继续画矩形框');
  // 撤销 → 清空
  await page.click('#anno-undo');
  await page.waitForTimeout(150);
  assert.equal(await page.locator('#anno-layer rect').count(), 0, '撤销后矩形框应消失');
  // 0×0 守卫：纯单击（未拖拽）不入栈、不残留临时矩形
  await page.mouse.click(stage.x + stage.width * 0.4, stage.y + stage.height * 0.4);
  await page.waitForTimeout(200);
  assert.equal(await page.locator('#anno-layer rect').count(), 0, '纯单击不应产生矩形框');
  // 右键守卫：button!==0 不进入绘制
  await page.mouse.click(stage.x + stage.width * 0.45, stage.y + stage.height * 0.45, { button: 'right' });
  await page.waitForTimeout(200);
  assert.equal(await page.locator('#anno-layer rect').count(), 0, '右键不应产生矩形框');
  await page.click('#btn-render-anno');   // 关闭标注

  // 5. 文字标注：T 文字 → prompt 对话框 → SVG text → 合并（标注版底图仍可继续标）
  await page.click('#btn-render-anno');   // 重新打开标注
  await page.click('#anno-text');
  page.on('dialog', (d) => d.accept('红点'));
  await page.mouse.click(stage.x + stage.width * 0.3, stage.y + stage.height * 0.3);
  await page.waitForTimeout(300);
  assert.equal(await page.locator('#anno-layer text').count(), 1, '文字标注后应有 1 个 SVG text');
  assert.equal(await page.locator('#anno-layer text').textContent(), '红点', 'SVG text 内容应为「红点」');
  const textSrcBefore = await page.locator('#render-img').getAttribute('src');
  await page.click('#anno-bake');
  await page.waitForFunction((before) => {
    const img = document.getElementById('render-img');
    return img.src !== before && img.src.startsWith('data:image/png');
  }, textSrcBefore, { timeout: 5000 });
  const textSrcAfter = await page.locator('#render-img').getAttribute('src');
  assert.notEqual(textSrcAfter, textSrcBefore, '文字合并后 img src 应变更为标注版');

  // 6. 上传路径：canvas 生成 16×8 PNG → 上传进标注画板 → 画框合并 → 尺寸/下载断言
  const pngB64 = await page.evaluate(() => {
    const c = document.createElement('canvas');
    c.width = 16; c.height = 8;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#4488cc';
    ctx.fillRect(0, 0, 16, 8);
    return c.toDataURL('image/png').split(',')[1];
  });
  fs.writeFileSync('/tmp/anno-upload.png', Buffer.from(pngB64, 'base64'));
  await page.setInputFiles('#anno-file', '/tmp/anno-upload.png');
  await page.waitForTimeout(600);
  assert.equal(await page.locator('#render-modal').evaluate((el) => el.hidden), false,
    '上传后预览弹窗应可见');
  const upTitle = (await page.locator('#render-title').textContent()) || '';
  assert.ok(upTitle.includes('anno-upload.png'), '上传后标题应含文件名，实际: ' + upTitle);
  assert.ok(upTitle.includes('16×8'), '上传后标题应含 16×8，实际: ' + upTitle);
  const upSrc = await page.locator('#render-img').getAttribute('src');
  assert.ok(upSrc.startsWith('data:image/png'), '上传图 img src 应为 PNG data URL');
  assert.equal(await page.locator('#anno-layer').getAttribute('viewBox'), '0 0 16 8',
    'viewBox 应等于上传图尺寸 16×8');
  // 上传图同样可标注：画框 → 合并 → 尺寸保持 16×8
  // （小图场景下打开标注会让 anno-tools 出现、modal 重排居中——bbox 必须在切换后
  //   且布局稳定后再取，否则拖拽坐标会落在 modal-box 上）
  await page.click('#btn-render-anno');
  await page.waitForTimeout(300);
  const upStage = await page.locator('#render-stage').boundingBox();
  await page.mouse.move(upStage.x + upStage.width * 0.25, upStage.y + upStage.height * 0.25);
  await page.mouse.down();
  await page.mouse.move(upStage.x + upStage.width * 0.5, upStage.y + upStage.height * 0.5, { steps: 3 });
  await page.mouse.up();
  await page.waitForTimeout(300);
  assert.equal(await page.locator('#anno-layer rect').count(), 1, '上传图应能画矩形框');
  const upBefore = await page.locator('#render-img').getAttribute('src');
  await page.click('#anno-bake');
  await page.waitForFunction((before) => {
    const img = document.getElementById('render-img');
    return img.src !== before && img.src.startsWith('data:image/png');
  }, upBefore, { timeout: 5000 });
  const upDims = await page.evaluate(async (src) => {
    const img = new Image();
    img.src = src;
    await img.decode();
    return { w: img.naturalWidth, h: img.naturalHeight };
  }, await page.locator('#render-img').getAttribute('src'));
  assert.equal(upDims.w, 16, `上传图合并后宽度应为 16，实际 ${upDims.w}`);
  assert.equal(upDims.h, 8, `上传图合并后高度应为 8，实际 ${upDims.h}`);
  // 下载入口在新会话（上传图）上可用，文件名走 filenameBase
  const dlPromise = page.waitForEvent('download');
  await page.click('#btn-render-download');
  const dl = await dlPromise;
  assert.equal(dl.suggestedFilename(), 'anno-upload.png',
    '上传会话下载文件名应为 anno-upload.png，实际: ' + dl.suggestedFilename());

  // 7. SVG 拒绝：关闭模态后传 SVG → modal 保持 hidden、无报错
  await page.click('#btn-render-close');
  await page.waitForTimeout(200);
  await page.setInputFiles('#anno-file', {
    name: 't.svg',
    mimeType: 'image/svg+xml',
    buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>'),
  });
  await page.waitForTimeout(400);
  assert.equal(await page.locator('#render-modal').evaluate((el) => el.hidden), true,
    'SVG 上传应被拒绝（modal 保持 hidden）');

  // 8. JPEG 烘焙：上传 JPEG → 画框 → 合并 → 结果 data:image/jpeg、尺寸不变
  const jpgB64 = await page.evaluate(() => {
    const c = document.createElement('canvas');
    c.width = 64; c.height = 48;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#ff8800';
    ctx.fillRect(0, 0, 64, 48);
    return c.toDataURL('image/jpeg', 0.9).split(',')[1];
  });
  const jpgPath = '/tmp/anno-upload.jpg';
  fs.writeFileSync(jpgPath, Buffer.from(jpgB64, 'base64'));
  await page.setInputFiles('#anno-file', { name: 'anno-upload.jpg', mimeType: 'image/jpeg', buffer: fs.readFileSync(jpgPath) });
  await page.waitForTimeout(600);
  assert.equal(await page.locator('#render-modal').evaluate((el) => el.hidden), false,
    'JPEG 上传后预览弹窗应可见');
  const jpTitle = (await page.locator('#render-title').textContent()) || '';
  assert.ok(jpTitle.includes('64×48'), 'JPEG 上传后标题应含 64×48，实际: ' + jpTitle);
  await page.click('#btn-render-anno');
  await page.waitForTimeout(300);
  const jpStage = await page.locator('#render-stage').boundingBox();
  await page.mouse.move(jpStage.x + jpStage.width * 0.25, jpStage.y + jpStage.height * 0.25);
  await page.mouse.down();
  await page.mouse.move(jpStage.x + jpStage.width * 0.5, jpStage.y + jpStage.height * 0.5, { steps: 3 });
  await page.mouse.up();
  await page.waitForTimeout(300);
  assert.equal(await page.locator('#anno-layer rect').count(), 1, 'JPEG 上传图应能画矩形框');
  const jpBefore = await page.locator('#render-img').getAttribute('src');
  await page.click('#anno-bake');
  await page.waitForFunction((before) => {
    const img = document.getElementById('render-img');
    return img.src !== before;
  }, jpBefore, { timeout: 5000 });
  const jpAfter = await page.locator('#render-img').getAttribute('src');
  assert.ok(jpAfter.startsWith('data:image/jpeg'),
    'JPEG 源图烘焙结果应为 data:image/jpeg（跟随源图格式），实际前缀: ' + jpAfter.slice(0, 22));
  const jpDims = await page.evaluate(async (src) => {
    const img = new Image();
    img.src = src;
    await img.decode();
    return { w: img.naturalWidth, h: img.naturalHeight };
  }, jpAfter);
  assert.equal(jpDims.w, 64, `JPEG 合并后宽度应为 64，实际 ${jpDims.w}`);
  assert.equal(jpDims.h, 48, `JPEG 合并后高度应为 48，实际 ${jpDims.h}`);
  console.log(`JPEG 烘焙 ${jpDims.w}×${jpDims.h} → ${jpAfter.slice(0, 22)}`);
  assert.ok(!pageErrors.length, '全流程后不应有 pageerror: ' + pageErrors.join(' | '));

  console.log('SMOKE-PASS');
} finally {
  await browser.close();
}
