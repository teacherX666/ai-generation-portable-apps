// 并发生成（concurrency / repeat_count > 1）时一个任务会有多条产出。
// 回归目标：任务矩阵的格子必须把每条产出都渲染出来（点哪张预览哪张），
// 而不是永远只画第一条 —— 这是「并发生成只能看到一条预览」的根因。
//
// 直接跑真实的 _buildJobTile（vm 里加载 app.js），配一个最小 DOM shim。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const root = process.cwd();

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag || '').toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = { setProperty() {}, removeProperty() {}, getPropertyValue() { return ''; } };
    this.attrs = {};
    this.textContent = '';
    this._className = '';
    this._innerHTML = '';
    this._listeners = {};
    this.open = false;
  }
  get className() { return this._className; }
  set className(v) { this._className = String(v == null ? '' : v); }
  get classList() {
    const self = this;
    return {
      add(...cs) {
        const set = new Set(self._className.split(/\s+/).filter(Boolean));
        cs.forEach((c) => set.add(c));
        self._className = [...set].join(' ');
      },
      remove(...cs) {
        self._className = self._className.split(/\s+/).filter((c) => c && !cs.includes(c)).join(' ');
      },
      contains(c) { return self._className.split(/\s+/).includes(c); },
    };
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) { this._innerHTML = String(v == null ? '' : v); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get childElementCount() { return this.children.length; }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  insertBefore(child, ref) {
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(child); else this.children.splice(i, 0, child);
    child.parentNode = this;
    return child;
  }
  replaceWith(next) {
    const p = this.parentNode;
    if (!p) return;
    const i = p.children.indexOf(this);
    if (i >= 0) { p.children[i] = next; next.parentNode = p; }
  }
  remove() {
    const p = this.parentNode;
    if (!p) return;
    const i = p.children.indexOf(this);
    if (i >= 0) p.children.splice(i, 1);
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener() {}
  fire(type) { (this._listeners[type] || []).forEach((fn) => fn({ stopPropagation() {}, preventDefault() {} })); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  dispatchEvent() {}
  // Portal 的 app.js 在加载时会量 header 高度 / 挂 no-op 监听，这里补齐这些 API
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  closest() { return null; }
  contains() { return false; }
  removeChild() {}
  insertAdjacentHTML() {}
  replaceChildren() {}
  focus() {}
  blur() {}
  click() {}
  scrollIntoView() {}
  reset() {}
  submit() {}
  setSelectionRange() {}
  getContext() { return null; }
  cloneNode() { return new FakeNode(this.tagName); }
  matches(sel) {
    if (sel.startsWith('.')) return this.classList.contains(sel.slice(1));
    return this.tagName === sel.toUpperCase();
  }
  querySelector(sel) {
    for (const c of this.children) {
      if (c.matches(sel)) return c;
      const hit = c.querySelector(sel);
      if (hit) return hit;
    }
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      for (const c of node.children) {
        if (c.matches(sel)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
}

function makeSandbox(pathname) {
  const previewDialog = new FakeNode('dialog');
  const previewBody = new FakeNode('div');
  const byId = { previewDialog, previewDialogBody: previewBody };
  const document = {
    getElementById(id) { return byId[id] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement(tag) { return new FakeNode(tag); },
    head: new FakeNode('head'),
    body: new FakeNode('body'),
  };
  const sandbox = {
    window: { location: { pathname, search: '' }, _dlProgress: {}, addEventListener() {} },
    document,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    PetiteVue: { createApp() { return { mount() {} }; } },
    URL, URLSearchParams, Blob, console, setTimeout, setInterval: () => 1, clearInterval() {},
    FormData: class FormData {}, DataTransfer: class DataTransfer {}, Event: class Event {},
    crypto: { randomUUID: () => 'workspace-test' },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    alert() {}, confirm: () => true,
    navigator: { userAgent: 'node' },
  };
  vm.createContext(sandbox);
  return { sandbox, previewDialog, previewBody };
}

function loadApp(relPath, factoryName, pathname) {
  const { sandbox, previewDialog, previewBody } = makeSandbox(pathname);
  vm.runInContext(fs.readFileSync(relPath, 'utf8'), sandbox);
  return { app: sandbox.window[factoryName](), previewDialog, previewBody };
}

function tileMedia(app, job) {
  const tile = app._buildJobTile(job);
  const media = tile.querySelector('.job-tile-media');
  assert.ok(media, '格子必须有媒体区');
  return { tile, media };
}

function seedanceJob(n, status = 'succeeded') {
  const results = [];
  for (let i = 1; i <= n; i++) {
    results.push({ download_url: '/api/download/run' + i + '.mp4', filename: 'run' + i + '.mp4', index: i });
  }
  return { job_id: 'sd-' + n, status, results };
}

// ---- seedance（视频，产出在 result.results[].download_url） ----
{
  const { app, previewBody } = loadApp('seedance/static/app.js', 'SeedanceApp', '/seedance/index.html');

  // 1 条产出：保持原来的单图布局
  const single = tileMedia(app, seedanceJob(1));
  assert.equal(single.media.children.length, 1, '单条产出只画一个缩略图');
  assert.ok(!single.media.classList.contains('job-tile-media--multi'), '单条产出不需要多图布局');
  assert.equal(single.media.querySelector('.job-tile-count'), null, '单条产出不显示 ×N 角标');

  // 2 条产出（concurrency=2）：两条都要画出来，点第二条预览第二条
  const two = tileMedia(app, seedanceJob(2));
  assert.ok(two.media.classList.contains('job-tile-media--multi'), '并发产出的格子要用多图布局');
  const twoThumbs = two.media.querySelectorAll('.job-tile-thumb');
  assert.equal(twoThumbs.length, 2, '2 条产出必须画 2 个缩略图（回归：原来只画 1 个）');
  assert.equal(twoThumbs[0].src, '/seedance/api/download/run1.mp4');
  assert.equal(twoThumbs[1].src, '/seedance/api/download/run2.mp4');
  const count = two.media.querySelector('.job-tile-count');
  assert.ok(count, '并发产出要有 ×N 角标');
  assert.equal(count.textContent, '×2');

  twoThumbs[1].fire('click');
  const shown = previewBody.querySelector('video');
  assert.ok(shown, '点缩略图要打开放大预览');
  assert.equal(shown.src, '/seedance/api/download/run2.mp4', '点第 2 张要预览第 2 条，而不是永远第一条');
  const strip = previewBody.querySelector('.preview-strip');
  assert.ok(strip, '多条产出时预览弹窗要有编号条');
  assert.equal(strip.querySelectorAll('.preview-strip-item').length, 2);
  assert.ok(strip.querySelectorAll('.preview-strip-item')[1].classList.contains('is-active'), '当前条要高亮');

  // 4 条产出：2×2 铺满，不出现 +N
  const four = tileMedia(app, seedanceJob(4));
  assert.equal(four.media.querySelectorAll('.job-tile-thumb').length, 4, '4 条产出画 4 个缩略图');
  assert.equal(four.media.querySelector('.job-tile-thumb--more'), null, '4 条刚好铺满，不需要 +N');

  // 6 条产出：3 张 + 「+3」格子（凑满 2×2，不让一个格子挂十几条取流）
  const six = tileMedia(app, seedanceJob(6));
  assert.equal(six.media.querySelectorAll('.job-tile-thumb').length, 4, '3 张缩略图 + 1 个 +N 格子');
  const more = six.media.querySelector('.job-tile-thumb--more');
  assert.ok(more, '超出上限要有 +N 格子');
  assert.equal(more.textContent, '+3');
  assert.equal(six.media.querySelector('.job-tile-count').textContent, '×6');

  // 结果逐步产出时格子要重画（渲染签名覆盖全部产出 URL）
  const running = seedanceJob(1, 'running');
  const tile = app._buildJobTile(running);
  const before = tile.dataset.preview;
  const grown = Object.assign({}, running, {
    status: 'succeeded',
    results: running.results.concat([{ download_url: '/api/download/run2.mp4', filename: 'run2.mp4', index: 2 }]),
  });
  assert.ok(!before.includes('run2.mp4'), '单条产出时签名里不该有第二条');
  assert.ok(app._buildJobTile(grown).dataset.preview.includes('run2.mp4'), '新增产出必须改变渲染签名');
}

// ---- nano-banana（图片，产出在 result.results[].images[]） ----
{
  const { app, previewBody } = loadApp('nano-banana/static/app.js', 'NanoBananaApp', '/nano-banana/index.html');
  const job = {
    job_id: 'nb-2',
    status: 'succeeded',
    results: [
      { images: [{ download_url: '/api/media/a.png', filename: 'a.png' }] },
      { images: [{ download_url: '/api/media/b.png', filename: 'b.png' }] },
    ],
  };
  const { media } = tileMedia(app, job);
  assert.ok(media.classList.contains('job-tile-media--multi'), 'nano-banana 并发产出同样要多图布局');
  const thumbs = media.querySelectorAll('.job-tile-thumb');
  assert.equal(thumbs.length, 2, 'result.results[].images[] 里的每条产出都要画（回归：原来只画第一张）');
  assert.equal(thumbs[1].src, '/nano-banana/api/media/b.png');
  thumbs[1].fire('click');
  const shown = previewBody.querySelector('img');
  assert.ok(shown, '点缩略图要打开放大预览');
  assert.equal(shown.src, '/nano-banana/api/media/b.png');
  assert.ok(previewBody.querySelector('.preview-strip'), '多条产出时预览弹窗要有编号条');
}

// ---- Portal 即梦历史卡片：同样的「只看 files[0]」问题 ----
// 真的把 portal/static/app.js 加载进 vm，从 createApp 的选项里取出
// DreaminaApp 工厂，直接调它的 _dmBuildHistCard。
{
  let captured = null;
  const node = (tag) => new FakeNode(tag);
  const document = {
    // Portal 的 app.js 加载时会 document.querySelector(...) 后直接量尺寸，不能返回 null
    getElementById() { return node('div'); },
    querySelector() { return node('div'); },
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement(tag) { return node(tag); },
    createTextNode() { return node('text'); },
    head: node('head'), body: node('body'), documentElement: node('html'), cookie: '',
  };
  const sandbox = {
    window: {
      location: { pathname: '/', search: '', hash: '', href: 'http://x/' },
      localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
      addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
      _dlProgress: {}, navigator: { userAgent: 'node' },
    },
    document,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    PetiteVue: { createApp(options) { captured = options; return { mount() {} }; } },
    URL, URLSearchParams, Blob, console, setTimeout, setInterval: () => 1, clearInterval() {}, clearTimeout() {},
    FormData: class FormData { set() {} delete() {} },
    DataTransfer: class DataTransfer {}, Event: class Event {}, CustomEvent: class CustomEvent {},
    crypto: { randomUUID: () => 'x' },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '' }),
    alert() {}, confirm: () => true, requestAnimationFrame: () => 1,
    location: { pathname: '/', search: '', replace() {} },
    navigator: { userAgent: 'node' },
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync('portal/static/app.js', 'utf8'), sandbox);
  assert.ok(captured && captured.DreaminaApp, 'Portal 应暴露 DreaminaApp 工厂');
  const dm = captured.DreaminaApp();

  const card = (files) => dm._dmBuildHistCard({
    status: 'completed', created_at: '2026-09-17T16:00:00', task_type: 'image',
    params: { prompt: 'p' }, result: { files },
  });
  const mediaOf = (tile) => tile.children.find((c) => String(c.className).indexOf('dm-tile-media') >= 0);
  const thumbsOf = (media) => media.children.filter((c) => String(c.className).indexOf('dm-tile-thumb') >= 0);

  const one = mediaOf(card(['/outputs/a.png']));
  assert.equal(thumbsOf(one).length, 1, '单条产出仍画一个缩略图');
  assert.ok(!one.classList.contains('dm-tile-media--multi'), '单条产出不用多图布局');

  const two = mediaOf(card(['/outputs/a.png', '/outputs/b.png']));
  assert.ok(two.classList.contains('dm-tile-media--multi'), '即梦并发生成要用多图布局');
  assert.equal(thumbsOf(two).length, 2, '即梦 2 条产出必须画 2 个缩略图（回归：原来只画 files[0]）');
  assert.equal(two.children.find((c) => c.classList.contains('dm-tile-count')).textContent, '×2');

  const six = mediaOf(card(['/o/a.png', '/o/b.png', '/o/c.png', '/o/d.png', '/o/e.png', '/o/f.png']));
  const more = six.children.find((c) => c.classList.contains('dm-tile-thumb--more'));
  assert.equal(thumbsOf(six).length, 4, '即梦 6 条产出：3 张缩略图 + 1 个 +N 格子');
  assert.equal(more.textContent, '+3');

  // 放大预览里要能在多条产出之间切换
  assert.equal(typeof dm.openDmZoom, 'function', '即梦放大预览入口应存在');
  const portalSrc = fs.readFileSync('portal/static/app.js', 'utf8');
  assert.match(portalSrc, /preview-strip-item/, '即梦放大预览要能在多条产出间切换');
  assert.doesNotMatch(portalSrc, /const thumb = files\[0\] \?/, '即梦卡片不应再从 files[0] 取唯一缩略图');
}

console.log('job tile multi preview: ok');
