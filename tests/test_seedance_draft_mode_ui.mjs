// 视频生成（seedance 独立 UI）里的「样片模式」交互 —— 二级预览窗口 + 继续生成出成片。
//
// 用户 2026-10-19 定的交互：
//   1. 任务类型选「样片模式」→ 第一段出 480P 样片（draft=1，仅 Seedance 2.5 支持）；
//   2. 样片成功 → **自动弹出**二级窗口预览样片；
//   3. 窗口里点「继续生成」→ 第二段只发 draft_task_id + resolution=1080p
//      （官方 Step2 参数表：提示词/素材/时长/比例/种子/有声/任务类型一律自动复用、禁止传入）；
//   4. 成片好了 → 窗口里的播放器**直接换成 1080P 成片**；
//   5. 事后想回到窗口 → 点任务列表里带「样片」角标的卡片（缩略图同理）。
//
// 这里守的就是这五条。第二段的请求体形状尤其重要：多发一个素材就会被方舟同步拒
// （官方原文：「即使传入值与 Draft 阶段使用的一致，也会触发报错」）。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const APP_JS = path.join(ROOT, 'seedance', 'static', 'app.js');

class FakeElement {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = { cssText: '', setProperty() {} };
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    this.attrs = {};
    this.listeners = {};
    this.value = '';
    this.textContent = '';
    this._html = '';
    this.hidden = false;
    this.disabled = false;
    this.src = '';
    this.title = '';
    this.files = [];
    this._q = new Map();
    this.parentNode = null;
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, extra = {}) {
    const event = { type, preventDefault() {}, stopPropagation() {}, ...extra };
    (this.listeners[type] || []).forEach((fn) => fn(event));
  }
  setAttribute(name, value) { this.attrs[name] = String(value); if (name === 'id') this.id = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  remove() {}
  replaceWith() {}
  querySelector(selector) {
    if (!this._q.has(selector)) {
      const el = new FakeElement(selector.includes('video') ? 'video' : 'div');
      el.parentNode = this;
      this._q.set(selector, el);
    }
    return this._q.get(selector);
  }
  querySelectorAll() { return []; }
  closest() { return null; }
  set innerHTML(value) { this._html = value; }
  get innerHTML() { return this._html; }
}

function makeSelect(name, values) {
  const options = values.map((value) => ({ value, textContent: value, dataset: {} }));
  const select = {
    name, tagName: 'SELECT', type: 'select-one', options, dataset: {},
    _value: values[0],
    get value() { return this._value; },
    set value(v) { this._value = v; },
    appendChild(node) { this.options.push(node); },
  };
  Object.defineProperty(select, 'innerHTML', {
    get() { return ''; },
    set(v) { if (v === '') this.options = []; },
  });
  return select;
}

const DRAFT_ID = 'cgt-20261019145332-mk7hx';
const FINAL_ID = 'cgt-20261019150557-gtrun';

function draftJob(extra = {}) {
  return Object.assign({
    job_id: 'job-draft-1',
    status: 'succeeded',
    prompt: '一只橘猫在木地板上伸懒腰',
    model: 'doubao-seedance-2-5-260628',
    params: { draft: '1', resolution: '480p', model: 'doubao-seedance-2-5-260628' },
    results: [{ index: 1, task_id: DRAFT_ID, download_url: '/api/download/draft-token', filename: 'draft.mp4' }],
    created_at: '2026-10-19 14:54:54',
  }, extra);
}

function finalJob(extra = {}) {
  return Object.assign({
    job_id: 'job-final-1',
    status: 'succeeded',
    prompt: '基于样片生成正式成片',
    model: 'doubao-seedance-2-5-260628',
    params: { draft_task_id: DRAFT_ID, resolution: '1080p' },
    results: [{ index: 1, task_id: FINAL_ID, download_url: '/api/download/final-token', filename: 'final.mp4' }],
    errors: [],
    events: [],
    created_at: '2026-10-19 15:05:57',
  }, extra);
}

// 活动记录详情（/api/activity/<id>）：列表接口为了瘦身不下发 request/restore，
// 所以「这条是不是样片」只能取详情后再判 —— 造两种详情来验分流。
function activityDetail(isDraft) {
  const values = isDraft
    ? { draft: '1', resolution: '480p', model: 'doubao-seedance-2-5-260628', prompt: '一只橘猫' }
    : { resolution: '720p', model: 'doubao-seedance-2-5-260628', prompt: '普通任务' };
  return {
    id: isDraft ? 'act-draft-1' : 'act-plain-1',
    job_id: isDraft ? 'job-draft-1' : 'job-plain-1',
    status: 'succeeded',
    created_at: '2026-10-19 14:54:54',
    title: isDraft ? '一只橘猫' : '普通任务',
    model: 'doubao-seedance-2-5-260628',
    request: { values },
    result: { results: [{ index: 1, task_id: isDraft ? DRAFT_ID : 'cgt-plain', download_url: isDraft ? '/api/download/draft-token' : '/api/download/p', filename: 'x.mp4' }] },
  };
}

function buildContext() {  const registry = new Map();
  const form = new FakeElement('form');
  form.id = 'sd-form';
  const modelSelect = makeSelect('model', ['doubao-seedance-2-5-260628']);
  const resolutionSelect = makeSelect('resolution', ['480p', '720p', '1080p']);
  const ratioSelect = makeSelect('ratio', ['16:9', '9:16', 'adaptive']);
  const durationInput = { name: 'duration', type: 'number', value: '12', min: '-1', max: '30' };
  form.elements = { model: modelSelect, duration: durationInput, resolution: resolutionSelect, ratio: ratioSelect };
  const body = new FakeElement('body');
  body.appendChild = (el) => { if (el && el.id) registry.set(el.id, el); return el; };

  const document = {
    title: '',
    body,
    createElement: (tag) => new FakeElement(tag),
    getElementById(id) { return registry.get(id) || (id === 'sd-form' ? form : null); },
    querySelector(selector) {
      const match = selector.match(/name="([^"]+)"/);
      return match ? form.elements[match[1]] || null : null;
    },
    querySelectorAll() { return []; },
    addEventListener() {},
  };

  const calls = { posts: [], gets: [] };
  const storage = new Map();
  const json = (payload, status = 200) => ({ ok: status < 400, status, json: async () => payload });
  const plainJob = {
    job_id: 'job-plain-1', status: 'succeeded', prompt: '普通任务', params: {},
    results: [{ index: 1, task_id: 'cgt-plain', download_url: '/api/download/p', filename: 'p.mp4' }],
    errors: [], events: [],
  };
  const fetchMock = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const target = String(url);
    if (method === 'POST') {
      calls.posts.push({ url: target, body: options.body });
      return json({ ok: true, job_id: 'job-final-1' });
    }
    calls.gets.push(target);
    if (target.includes('job-final-1')) return json(finalJob());
    if (target.includes('job-draft-1')) return json(draftJob());
    if (target.includes('job-plain-1')) return json(plainJob);
    // 活动记录详情（列表接口不下发 request/restore，所以点开时要取一次详情）
    if (target.includes('/api/activity/act-draft-1')) return json(activityDetail(true));
    if (target.includes('/api/activity/act-plain-1')) return json(activityDetail(false));
    return json({ ok: true, jobs: [] });
  };

  const sandbox = {
    window: { location: { pathname: '/seedance/index.html' }, _dlProgress: {} },
    document,
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
    // 记下 set 了哪些字段 —— 第二段「只发 draft_task_id + 分辨率」就是这么验的。
    FormData: class {
      constructor() { this.map = {}; }
      set(k, v) { this.map[k] = v; }
      get(k) { return this.map[k]; }
      delete(k) { delete this.map[k]; }
      has(k) { return k in this.map; }
    },
    fetch: fetchMock,
    PetiteVue: { createApp() { return { mount() {} }; } },
    URL, URLSearchParams, Blob, File: class {},
    crypto: { randomUUID: () => 'uuid' },
    console,
    // 轮询里的 `await new Promise(r => setTimeout(r, 2500))` 必须能真的 resolve，否则测试挂死；
    // 间隔压成 0 让用例秒级跑完。setInterval 保持 no-op（否则进程不退出）。
    setTimeout: (fn) => { const t = globalThis.setTimeout(fn, 0); if (t && typeof t.unref === 'function') t.unref(); return t; },
    setInterval: () => 1,
    clearInterval() {},
    alert() {}, confirm: () => true, DataTransfer: class {},
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; this.dataset = {}; } },
  };
  sandbox.window.parent = sandbox.window;   // 独立模式：window.parent === window
  vm.createContext(sandbox);
  // job-cards.js 必须先加载：app.js 的 isCancellableJob / jobDisplayStatus 是它的薄封装
  // （`window.JobCards.jobDisplayStatus(j)`），缺了它建卡片就报 undefined。
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'seedance', 'static', 'job-cards.js'), 'utf8'), sandbox);
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), sandbox);

  const app = sandbox.window.SeedanceApp();
  app.tabs = [{ id: 'A', name: 'A' }];
  app.activeTabId = 'A';
  app.loadJobs = () => {};
  app.renderJobsGrid = () => {};
  app._upsertJob = () => {};
  app._renderJobToDom = () => {};
  app.providers = { volcengine: { label: '火山方舟', base_url: 'https://ark.example/api/v3' } };
  app.model = 'doubao-seedance-2-5-260628';
  app.models = [{ key: 'doubao-seedance-2-5-260628', label: 'Seedance 2.5', draft: true, resolutions: ['480p', '720p', '1080p'] }];
  return { sandbox, app, document, calls, registry, form, modelSelect, resolutionSelect, ratioSelect, durationInput };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// ── 1. 识别「样片任务」与它的上游任务 id ────────────────────────────────────

export async function testDraftJobDetection() {
  const { sandbox } = buildContext();
  assert.equal(sandbox.isDraftJob(draftJob()), true, 'params.draft=1 的任务必须认成样片');
  assert.equal(sandbox.isDraftJob({ params: { draft: '0' } }), false);
  assert.equal(sandbox.isDraftJob({ params: {} }), false, '普通任务不能被误认成样片');
  assert.equal(sandbox.draftTaskIdOf(draftJob()), DRAFT_ID, '成片阶段要填的就是这个 id');
  assert.equal(sandbox.draftTaskIdOf({ results: [] }), '', '拿不到就返回空串，绝不瞎编');
  assert.equal(sandbox.firstResultOf(draftJob()).url, '/seedance/api/download/draft-token');
}

// ── 2. 二级窗口：显示样片 + 记住上游任务 id ────────────────────────────────

export async function testModalShowsDraftPreview() {
  const { app, document } = buildContext();
  await app.openDraftPreview(draftJob());
  const overlay = document.getElementById('sd-draft-preview');
  assert.ok(overlay, '应当创建二级窗口');
  assert.equal(overlay.hidden, false, '窗口必须可见');
  assert.match(overlay.querySelector('#sd-draft-video').src, /draft-token/, '播放器应是样片');
  assert.equal(overlay.querySelector('#sd-draft-title').textContent, '样片预览（480P）');
  assert.match(overlay.querySelector('#sd-draft-status').textContent, new RegExp(DRAFT_ID),
    '状态行要写出样片任务 id（用户排查时要能对上）');
  assert.equal(overlay.querySelector('#sd-draft-finalize').disabled, false);
}

export async function testModalDisablesFinalizeWithoutTaskId() {
  const { app, document } = buildContext();
  // 故意用一个 mock 不认识的 job_id：openDraftPreview 会去拉权威详情，若 mock 返回了
  // 带 task_id 的「标准样片」，这条用例就测不到「拿不到 id」的分支了。
  const noIdJob = draftJob({
    job_id: 'job-draft-notask',
    results: [{ index: 1, download_url: '/api/download/x', filename: 'x.mp4' }],
  });
  await app.openDraftPreview(noIdJob);
  const overlay = document.getElementById('sd-draft-preview');
  assert.equal(overlay.querySelector('#sd-draft-finalize').disabled, true,
    '没有上游任务 id 就不能点「继续生成」（否则必然打一次失败的付费请求）');
  assert.match(overlay.querySelector('#sd-draft-status').textContent, /无法出成片/);
}

// ── 3. 「继续生成」：只发 draft_task_id + 1080p，然后换成真片 ───────────────

export async function testFinalizeSendsMinimalBodyAndSwapsToFinal() {
  const { app, document, calls } = buildContext();
  await app.openDraftPreview(draftJob());
  const overlay = document.getElementById('sd-draft-preview');
  overlay.querySelector('#sd-draft-finalize').dispatch('click');
  for (let i = 0; i < 12; i += 1) await tick();

  const post = calls.posts.find((p) => p.url.includes('/api/jobs'));
  assert.ok(post, '「继续生成」必须提交到 /api/jobs');
  const body = post.body.map;
  assert.equal(body.draft_task_id, DRAFT_ID, '第二段的 id 必须是样片任务的 id');
  assert.equal(body.model, 'doubao-seedance-2-5-260628', 'model 必须与 Draft 阶段一致');
  assert.equal(body.resolution, '1080p', '官方 Step2：resolution 必须传且只能是 1080p');
  assert.equal(body.duration, '-1', '第二段不指定时长（沿用样片），发 -1 让后端按实际时长回填');
  assert.ok(!('draft' in body), '成片阶段不能带 draft（那会又变成一次样片）');
  assert.ok(!('first_frame' in body) && !('ref_image_1' in body) && !('ref_video_1' in body),
    '素材一律不能重复传入 —— 官方明说「即使与 Draft 阶段一致也会报错」');

  assert.match(overlay.querySelector('#sd-draft-video').src, /final-token/,
    '成片好了要把窗口里的播放器换成 1080P 成片（「把样片变为真片」）');
  assert.equal(overlay.querySelector('#sd-draft-title').textContent, '1080P 成片');
  assert.match(overlay.querySelector('#sd-draft-status').textContent, /已完成/);
}

export async function testReopenShowsExistingFinal() {
  const { app, document } = buildContext();
  app.jobs = [draftJob(), finalJob()];
  await app.openDraftPreview(draftJob());
  const overlay = document.getElementById('sd-draft-preview');
  assert.match(overlay.querySelector('#sd-draft-video').src, /final-token/,
    '这条样片已经出过成片 → 打开窗口应直接显示成片态');
  assert.match(overlay.querySelector('#sd-draft-title').textContent, /已出片/);
}

// ── 4. 卡片路由：样片卡片进二级窗口，普通卡片仍进详情 ───────────────────────
//
// ⚠️ 卡片的数据来源是**任务列表**接口，而它为了瘦身**不下发 params**（活动列表也不下发
// request）。所以「卡片快照里有 params.draft」这个形状**现实中不存在** —— 2026-10-20
// 的 bug 就是这么漏掉的：测试用了详情接口的形状，真机上判据恒为 false，点样片卡片
// 只会开普通详情弹窗，样片窗口再也回不去。下面两个形状都要覆盖。

//: 模拟 /api/jobs 列表项：只有列表真正下发的字段（**没有 params**）。
function listShaped(job) {
  const copy = Object.assign({}, job);
  delete copy.params;
  delete copy.request;
  delete copy.restore;
  return copy;
}

export async function testDraftTileOpensModalFromListShape() {
  const { app, document } = buildContext();
  const detailCalls = [];
  app.openJobDetail = (...args) => { detailCalls.push(args); };
  const tile = app._buildJobTile(listShaped(draftJob()));
  tile.dispatch('click');
  for (let i = 0; i < 8; i += 1) await tick();
  assert.equal(document.getElementById('sd-draft-preview').hidden, false,
    '列表形状（无 params）的样片卡片也必须能打开二级窗口 —— 要补取一次详情再判');
  assert.equal(detailCalls.length, 0, '不该退化成通用详情弹窗');
}

export async function testDraftTileOpensModalWhenSnapshotHasParams() {
  const { app, document } = buildContext();
  const detailCalls = [];
  app.openJobDetail = (...args) => { detailCalls.push(args); };
  const tile = app._buildJobTile(draftJob());
  tile.dispatch('click');
  for (let i = 0; i < 8; i += 1) await tick();
  assert.equal(document.getElementById('sd-draft-preview').hidden, false, '点样片卡片应打开二级窗口');
  assert.equal(detailCalls.length, 0, '不该再弹通用详情弹窗');
  const chip = tile.children.find((c) => c.className === 'job-tile-meta')
    .children.find((c) => String(c.className || '').includes('job-tile-chip--draft'));
  assert.ok(chip, '卡片带 draft 标志时要有「样片」角标');
  assert.equal(chip.textContent, '样片');
}

export async function testListShapedPlainTileStillOpensDetail() {
  const { app, document } = buildContext();
  const detailCalls = [];
  app.openJobDetail = (...args) => { detailCalls.push(args); };
  // 普通任务：列表形状 + 详情接口返回普通任务
  const tile = app._buildJobTile(listShaped({
    job_id: 'job-plain-1', status: 'succeeded', prompt: '普通任务',
    results: [{ index: 1, task_id: 'cgt-plain', download_url: '/api/download/p', filename: 'p.mp4' }],
  }));
  tile.dispatch('click');
  for (let i = 0; i < 8; i += 1) await tick();
  assert.equal(detailCalls.length, 1, '普通卡片必须继续走详情弹窗');
  assert.ok(!document.getElementById('sd-draft-preview'), '不该冒出样片窗口');
}

export async function testUnknownJobFallsBackToDetail() {
  const { app, document } = buildContext();
  const detailCalls = [];
  app.openJobDetail = (...args) => { detailCalls.push(args); };
  // 详情接口也取不到（任务被重启/剪枝清掉）→ 不能卡住，退回通用详情
  const tile = app._buildJobTile(listShaped(Object.assign(draftJob(), { job_id: 'job-gone-1' })));
  tile.dispatch('click');
  for (let i = 0; i < 8; i += 1) await tick();
  assert.equal(detailCalls.length, 1, '取不到详情时要退回通用详情，不能什么都不发生');
  assert.ok(!document.getElementById('sd-draft-preview'), '信息不足时不该猜成样片窗口');
}

export async function testDraftTileOpensModal() {
  const { app, document, calls } = buildContext();
  const detalCalls = [];
  app.openJobDetail = (...args) => { detalCalls.push(args); };
  const tile = app._buildJobTile(draftJob());
  tile.dispatch('click');
  await tick();
  assert.equal(document.getElementById('sd-draft-preview').hidden, false, '点样片卡片应打开二级窗口');
  assert.equal(detalCalls.length, 0, '不该再弹通用详情弹窗');
  const chip = tile.children.find((c) => c.className === 'job-tile-meta')
    .children.find((c) => String(c.className || '').includes('job-tile-chip--draft'));
  assert.ok(chip, '样片卡片必须有「样片」角标，否则用户不知道怎么回去');
  assert.equal(chip.textContent, '样片');
}

export async function testNormalTileStillOpensDetail() {
  const { app, document, calls } = buildContext();
  const detalCalls = [];
  app.openJobDetail = (...args) => { detalCalls.push(args); };
  const tile = app._buildJobTile({
    job_id: 'job-plain-1', status: 'succeeded', prompt: '普通任务', params: {},
    results: [{ index: 1, task_id: 'cgt-plain', download_url: '/api/download/p', filename: 'p.mp4' }],
  });
  tile.dispatch('click');
  await tick();
  assert.equal(detalCalls.length, 1, '普通卡片必须继续走详情弹窗');
  assert.ok(!document.getElementById('sd-draft-preview'), '不该冒出样片窗口');
}

// ── 5. 样片跑完自动弹窗 ────────────────────────────────────────────────────

export async function testPollJobAutoOpensModalForDraft() {
  const { app } = buildContext();
  const opened = [];
  app.openDraftPreview = async (job) => { opened.push(job && (job.job_id || job.id)); };
  await app.pollJob('job-draft-1', 'A', null);
  assert.deepEqual(opened, ['job-draft-1'], '样片任务跑完应当自动弹二级窗口');
}

export async function testPollJobDoesNotAutoOpenForNormalJob() {
  const { app } = buildContext();
  const opened = [];
  app.openDraftPreview = async (job) => { opened.push(job); };
  await app.pollJob('job-plain-1', 'A', null);
  assert.deepEqual(opened, [], '普通任务成功不该弹样片窗口');
}

// ── 6. 活动记录（持久化历史）：样片的进二级窗口，其余仍是原始详情 ──────────────

export async function testActivityDraftRecordOpensModal() {
  const { app, document } = buildContext();
  const detailCalls = [];
  app.showDetail = (id) => { detailCalls.push(id); };
  await app.openActivityRecord({ id: 'act-draft-1', job_id: 'job-draft-1', status: 'succeeded' });
  const overlay = document.getElementById('sd-draft-preview');
  assert.ok(overlay && overlay.hidden === false, '样片的活动记录点开应进二级窗口');
  assert.match(overlay.querySelector('#sd-draft-video').src, /draft-token/, '窗口里应是这条样片');
  assert.match(overlay.querySelector('#sd-draft-status').textContent, new RegExp(DRAFT_ID),
    '要能从活动记录里拿到上游任务 id（第二段靠它）');
  assert.equal(overlay.querySelector('#sd-draft-finalize').disabled, false);
  assert.deepEqual(detailCalls, [], '样片不该再走原始请求/事件详情');
}

export async function testActivityPlainRecordKeepsDetail() {
  const { app, document } = buildContext();
  const detailCalls = [];
  app.showDetail = (id) => { detailCalls.push(id); };
  await app.openActivityRecord({ id: 'act-plain-1', job_id: 'job-plain-1', status: 'succeeded' });
  assert.deepEqual(detailCalls, ['act-plain-1'], '普通活动记录必须维持原来的详情视图');
  assert.ok(!document.getElementById('sd-draft-preview'), '不该冒出样片窗口');
}

export async function testActivityDraftDetectionHandlesBothShapes() {
  const { sandbox } = buildContext();
  assert.equal(sandbox.activityRecordIsDraft({ request: { values: { draft: '1' } } }), true, 'request.values 里的 draft');
  assert.equal(sandbox.activityRecordIsDraft({ restore: { values: { draft: 1 } } }), true, 'restore.values 里的 draft（重启恢复副本）');
  assert.equal(sandbox.activityRecordIsDraft({ request: { parsed: { values: { draft: 'on' } } } }), true, 'json 提交走 request.parsed.values');
  assert.equal(sandbox.activityRecordIsDraft({ request: { values: { resolution: '720p' } } }), false, '普通记录不能被误判');
  assert.equal(sandbox.activityRecordIsDraft(null), false);
}

if (import.meta.url === `file://${process.argv[1]}` || process.argv[1]?.endsWith('test_seedance_draft_mode_ui.mjs')) {
  const tests = [
    ['样片任务识别与上游任务 id', testDraftJobDetection],
    ['二级窗口显示样片', testModalShowsDraftPreview],
    ['拿不到样片 id 时禁用「继续生成」', testModalDisablesFinalizeWithoutTaskId],
    ['「继续生成」只发 draft_task_id + 1080p 并换成真片', testFinalizeSendsMinimalBodyAndSwapsToFinal],
    ['已出片的样片再次打开显示成片', testReopenShowsExistingFinal],
    ['样片卡片点开二级窗口（带角标）', testDraftTileOpensModal],
    ['列表形状（无 params）的样片卡片也能进二级窗口', testDraftTileOpensModalFromListShape],
    ['带 params 的卡片形状仍然进二级窗口', testDraftTileOpensModalWhenSnapshotHasParams],
    ['列表形状的普通卡片仍进详情', testListShapedPlainTileStillOpensDetail],
    ['详情取不到时退回通用详情（不卡住）', testUnknownJobFallsBackToDetail],
    ['普通卡片仍走详情弹窗', testNormalTileStillOpensDetail],
    ['样片跑完自动弹窗', testPollJobAutoOpensModalForDraft],
    ['普通任务跑完不弹窗', testPollJobDoesNotAutoOpenForNormalJob],
    ['活动记录里的样片点开进二级窗口', testActivityDraftRecordOpensModal],
    ['活动记录里的普通记录仍走详情', testActivityPlainRecordKeepsDetail],
    ['活动记录的样片判定（两种数据形状）', testActivityDraftDetectionHandlesBothShapes],
  ];
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok   ${name}`);
    } catch (error) {
      failed += 1;
      console.log(`FAIL ${name}\n     ${error.message}`);
    }
  }
  console.log(failed ? `\n${failed} failed` : '\nall passed');
  process.exitCode = failed ? 1 : 0;
}
