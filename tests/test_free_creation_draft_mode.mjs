// 自由创作「样片模式（Draft）」的前端接线。
//
// 需求（用户 2026-10-15）：「还是接在自由创作里的，云端模型加一个样片模式」。
//
// 官方两段式（《创建视频生成任务》+《Seedance 2.5 教程 · 样片模式》）：
//   Step1  `draft: true` → 480P 低成本样片（开启后**只允许 480p**，其他分辨率直接报错）。
//   Step2  `content:[{type:'draft_task', draft_task:{id: <Step1 的任务 id>}}]`
//          → 正式成片；提示词/参考素材/随机种子/宽高比/时长由模型自动复用、禁止重复传入，
//          只重新指定分辨率。id 必须是 Step1 的 `id`，**不是** upstreamTaskId。
//
// 这里守的是前端的三条不变量：
//   1) 样片模式条目 = 独立云端模型，分辨率锁死 480p、下发的仍是**真实模型 id**；
//   2) 第一段提交带 draft=1（普通生成绝不能带 —— 带了就变成一次 480p 样片，白烧一次生成）；
//   3) 第二段只发 draft_task_id + 目标分辨率，不带 media，且成片结果上不再出现该按钮。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const APP_JS = path.join(ROOT, 'portal', 'static', 'free-creation', 'app.js');

const DRAFT_ID = 'seedance-2.5-draft';
const REAL_ID = 'doubao-seedance-2-5-260628';

class FakeClassList {
  constructor() { this.items = new Set(); }
  add(...names) { names.forEach((n) => this.items.add(n)); }
  remove(...names) { names.forEach((n) => this.items.delete(n)); }
  contains(name) { return this.items.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.items.has(name) : Boolean(force);
    if (on) this.items.add(name); else this.items.delete(name);
    return on;
  }
}

let nodeSeq = 0;

class FakeElement {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase();
    this.nodeId = ++nodeSeq;
    this.children = [];
    this.dataset = {};
    this.style = { setProperty() {} };
    this.classList = new FakeClassList();
    this.attrs = {};
    this.listeners = {};
    this.value = '';
    this.textContent = '';
    this.innerHTML = '';
    this.hidden = false;
    this.disabled = false;
    this.inert = false;
    this.scrollHeight = 0;
    this.offsetWidth = 0;
    this.files = [];
    this._q = new Map();
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener() {}
  dispatch(type, extra = {}) {
    const event = { type, preventDefault() {}, stopImmediatePropagation() {}, ...extra };
    (this.listeners[type] || []).forEach((fn) => fn(event));
  }
  setAttribute(name, value) { this.attrs[name] = String(value); if (name === 'id') this.id = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; }
  appendChild(child) { this.children.push(child); return child; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren() { this.children.length = 0; }
  cloneNode() { return new FakeElement(this.tagName); }
  querySelector(sel) {
    if (!this._q.has(sel)) this._q.set(sel, new FakeElement());
    return this._q.get(sel);
  }
  querySelectorAll() { return []; }
  closest() { return new FakeElement('div'); }
  setPointerCapture() {}
  releasePointerCapture() {}
  scrollIntoView() {}
  focus() {}
  setSelectionRange() {}
  click() { this.dispatch('click'); }
  get firstElementChild() { return this.children[0] || null; }
}

function makeTemplate() {
  const el = new FakeElement('template');
  el.content = { firstElementChild: new FakeElement('div') };
  return el;
}

function buildContext() {
  const registry = new Map();
  const doc = {
    visibilityState: 'visible',
    listeners: {},
    querySelector(sel) {
      if (!registry.has(sel)) registry.set(sel, sel.includes('template') ? makeTemplate() : new FakeElement());
      return registry.get(sel);
    },
    getElementById(id) { return doc.querySelector(`#${id}`); },
    querySelectorAll() { return []; },
    createElement: (tag) => new FakeElement(tag),
    addEventListener(type, fn) { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
  };
  doc.body = new FakeElement('body');
  doc.head = new FakeElement('head');

  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };

  const calls = { posts: [] };
  //: 云端视频模型来自 /seedance/api/schema（= providers.json 的 client 视图）。
  //: 分辨率刻意和线上 providers.json 一致（2.5 = 480p/720p/1080p：成片阶段固定 1080p，
  //: 声明里缺了它，seedance 自己的前置校验就会把合法的成片请求拦下来）。
  const schemas = {
    '/seedance/api/schema': {
      providers: {
        volcengine: {
          label: '火山方舟',
          models: [{ id: REAL_ID, label: 'Seedance 2.5', duration_range: [4, 30], resolutions: ['480p', '720p', '1080p'], ratios: ['16:9', '9:16'] }],
          defaults: {},
        },
      },
    },
    '/nano-banana/api/schema': {
      providers: {
        comfyui_local: { label: 'local', models: [{ id: 'auto', label: 'auto' }], defaults: {} },
      },
    },
    '/volcengine-portrait/api/config': { local_gateway: { modules: [] } },
  };

  const fetchMock = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    if (method === 'POST') {
      calls.posts.push({ url, body: JSON.parse(options.body || '{}') });
      return { ok: true, status: 200, json: async () => ({ ok: true, job_id: 'job-draft-1' }) };
    }
    if (schemas[url]) return { ok: true, status: 200, json: async () => schemas[url] };
    if (String(url).startsWith('/api/platform/history')) return { ok: true, status: 200, json: async () => ({ items: [] }) };
    if (String(url).startsWith('/api/auth')) return { ok: true, status: 200, json: async () => ({ username: 'tester', role: 'user' }) };
    return {
      ok: true,
      status: 200,
      headers: { get: () => null },
      blob: async () => ({ type: 'video/mp4', size: 1024 }),
      json: async () => ({}),
    };
  };

  class FakeFile {
    constructor(parts, name, opts = {}) { this.name = name; this.type = opts.type || ''; this.size = 1024; }
  }

  class FakeFileReader {
    readAsDataURL() { this.result = 'data:image/png;base64,AAAA'; if (this.onload) this.onload(); }
  }

  const sandbox = {
    console,
    document: doc,
    localStorage,
    fetch: fetchMock,
    structuredClone: (o) => JSON.parse(JSON.stringify(o)),
    crypto: { randomUUID: () => `uuid-${++nodeSeq}` },
    File: FakeFile,
    FileReader: FakeFileReader,
    URL: { createObjectURL: () => 'blob:fake', revokeObjectURL() {} },
    requestAnimationFrame: () => 0,
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    Event: class { constructor(type) { this.type = type; } },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.window.addEventListener = () => {};

  const context = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), context, { filename: APP_JS });
  return { context, doc, calls };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const read = (context, expression) => vm.runInContext(expression, context);

function selectDraftModel(context) {
  vm.runInContext(`
    state.selected.video = state.models.video.find((model) => model.id === '${DRAFT_ID}') || null;
    applyModelDefaults(state.selected.video);
    state.mode = 'video';
  `, context);
}

function seedTask(context, task) {
  //: 包一层 IIFE：同一个 context 里要造多条任务，裸的 `const now` 会重复声明报错。
  vm.runInContext(`
    (() => {
      const now = Date.now();
      const task = ${JSON.stringify(task)};
      task.createdAt = now; task.updatedAt = now; task.conversationId = 'conv-1';
      state.conversations = [{
        id: 'conv-1', mode: 'video', prompt: '一只猫在弹钢琴',
        params: { video: { ratio: '16:9', resolution: '720p', duration: 8, repeat_count: 1 },
                  image: { aspect_ratio: '1:1', image_size: '2K', repeat_count: 1 } },
        tasks: [task], createdAt: now, updatedAt: now,
      }];
      state.activeConversationId = 'conv-1';
      state.activeTaskId = task.id;
      state.mode = 'video';
      renderPreview();
      renderParamSummary();
    })();
  `, context);
}

// ── 1. 模型条目本身 ──────────────────────────────────────────────────────────

export async function testDraftEntryIsACloudModelPinnedTo480p() {
  const { context } = buildContext();
  await flush(); await flush();

  const draft = read(context, `JSON.stringify(state.models.video.find((m) => m.id === '${DRAFT_ID}'))`);
  assert.ok(draft, '云端模型列表里必须有样片模式入口');
  const model = JSON.parse(draft);
  assert.equal(model.modelId, REAL_ID, '下发的必须是真实模型 id（伪 id 会让方舟报 InvalidEndpointOrModel.NotFound）');
  assert.equal(model.provider, 'volcengine');
  assert.equal(model.local, false, '样片模式是云端模型，不是本地能力');
  assert.deepEqual(model.resolutions, ['480p'], '样片模式只允许 480p —— 锁死，别让用户选错');
  assert.equal(model.draft, true);
  assert.equal(model.badge, 'DRAFT');
  assert.deepEqual(model.duration, [4, 30], '时长上限跟随真实 2.5 模型');
  assert.ok(!model.module, '它不是本地网关能力（module 为空）');
}

export async function testDraftEntryDefaultsTo480p() {
  const { context } = buildContext();
  await flush(); await flush();
  selectDraftModel(context);
  assert.equal(read(context, 'state.params.video.resolution'), '480p');
}

export async function testDraftEntryDoesNotStealTheDefaultSelection() {
  const { context } = buildContext();
  await flush(); await flush();
  assert.equal(read(context, 'state.selected.video.id'), REAL_ID, '默认仍应选中真实的 Seedance 2.5，而不是样片模式');
}

export async function testDraftDisablesContinueEditing() {
  const { context } = buildContext();
  await flush(); await flush();
  selectDraftModel(context);
  vm.runInContext('renderParamSummary()', context);
  const result = read(context, `JSON.stringify({ disabled: els.refine.disabled, title: els.refine.title, verb: els.generateLabel.textContent })`);
  const state_ = JSON.parse(result);
  assert.equal(state_.disabled, true, '样片模式没有「在原结果上继续修改」的语义，按钮该禁用');
  assert.match(state_.title, /样片/, '禁用原因要说清是样片模式，而不是笼统的「抠图/超分不支持」');
  assert.match(state_.verb, /样片/, '生成按钮文案应提示这是样片');
}

// ── 2. 第一段提交 ────────────────────────────────────────────────────────────

export async function testDraftSubmitSendsDraftFlag() {
  const { context } = buildContext();
  await flush(); await flush();
  selectDraftModel(context);
  vm.runInContext(`els.prompt.value = '一只猫在弹钢琴'`, context);

  const built = await read(context, 'buildPayload()');
  assert.equal(built.endpoint, '/seedance/api/jobs/json');
  assert.equal(built.payload.draft, '1', '第一段必须下发 draft=1');
  assert.equal(built.payload.model, REAL_ID);
  assert.equal(built.payload.resolution, '480p');
  assert.equal(built.payload.provider, 'volcengine');
  assert.ok(built.payload.prompt.includes('猫'), '第一段要带提示词 —— 样片的意义就是把提示词一起验证掉');
}

export async function testPlainVideoSubmitHasNoDraftFlag() {
  const { context } = buildContext();
  await flush(); await flush();
  vm.runInContext(`
    state.selected.video = state.models.video.find((m) => m.id === '${REAL_ID}');
    applyModelDefaults(state.selected.video);
    els.prompt.value = '一只猫在弹钢琴';
  `, context);

  const built = await read(context, 'buildPayload()');
  assert.ok(!('draft' in built.payload), '普通生成带上 draft 会变成一次 480p 样片 —— 白烧一次生成');
}

export async function testDraftRejectsContinueEditing() {
  const { context } = buildContext();
  await flush(); await flush();
  selectDraftModel(context);
  vm.runInContext(`
    state.editSourceTaskId = 'src-task-1';
    els.prompt.value = '把红球改成蓝球';
  `, context);

  const message = await read(context, 'buildPayload().then(() => "").catch((error) => error.message)');
  assert.match(message, /样片模式/, message);
  assert.match(message, /出成片/, '错误文案要给出替代路径');
}

// ── 3. 第二段：用这条样片出成片 ──────────────────────────────────────────────

function draftTask(extra = {}) {
  return {
    id: 'draft-task-1', app: 'seedance', base: '/seedance', jobPath: '/api/jobs', mode: 'video',
    prompt: '一只猫在弹钢琴', model: 'Seedance 2.5 · 样片模式（480P 快速试错）',
    modelId: REAL_ID, provider: 'volcengine',
    params: { ratio: '16:9', resolution: '480p', duration: 8, repeat_count: 1 },
    attachments: [], status: 'success', resultUrl: '/seedance/api/download/draft-abc',
    resultTaskId: 'cgt-draft-abc', resultName: 'draft.mp4', draft: true, ...extra,
  };
}

export async function testDraftButtonAppearsOnDraftResultOnly() {
  const { context } = buildContext();
  await flush(); await flush();

  seedTask(context, draftTask());
  const shown = JSON.parse(read(context, `JSON.stringify({ hidden: els.draftFinalize.hidden, label: els.draftFinalizeLabel.textContent })`));
  assert.equal(shown.hidden, false, '样片结果出来后必须给出「用这条样片出成片」入口');
  //: 成片阶段**只支持 1080p**（官方 Step2 参数表），按钮上要写清楚，别让用户以为能选 720p。
  assert.match(shown.label, /1080p/, shown.label);

  // 普通生成的结果不是样片：它的任务 id 填进 draft_task 会被方舟拒，按钮必须收起来。
  seedTask(context, draftTask({ id: 'plain-1', draft: false, resultTaskId: 'cgt-normal-xyz' }));
  assert.equal(read(context, 'els.draftFinalize.hidden'), true, '普通结果不该出现样片入口');

  // 成片自己也不该出现（否则会拿成片的 id 再去出成片）。
  seedTask(context, draftTask({ id: 'final-1', draft: false, draftFinalizeOf: 'draft-task-1' }));
  assert.equal(read(context, 'els.draftFinalize.hidden'), true, '成片结果上不该再出现该入口');
}

export async function testFinalizeSendsOnlyDraftTaskAndResolution() {
  const { context, calls } = buildContext();
  await flush(); await flush();
  seedTask(context, draftTask());

  vm.runInContext(`els.draftFinalize.dispatch('click')`, context);
  await flush(); await flush(); await flush();

  const post = calls.posts.find((item) => item.url === '/seedance/api/jobs/json');
  assert.ok(post, '成片必须提交到 seedance 的 JSON 入口');
  assert.equal(post.body.draft_task_id, 'cgt-draft-abc', 'id 必须是 Step1 返回的任务 id');
  assert.equal(post.body.model, REAL_ID);
  assert.equal(post.body.resolution, '1080p', '成片阶段官方只支持 1080p（实测 720p 会被同步拒）');
  assert.equal(post.body.duration, -1, '第二段不指定时长（由模型沿用样片），发 -1 让后端按真实时长回填计费');
  assert.deepEqual(post.body.media, {}, '第二段不该再挂素材：会白跑一遍上传，还可能撞「禁止重复传入」');
  assert.ok(!('draft' in post.body), '成片阶段不能带 draft（那会又变成一次样片）');

  const tasks = JSON.parse(read(context, 'JSON.stringify(activeConversation().tasks.map((t) => ({ id: t.id, of: t.draftFinalizeOf || null, jobId: t.jobId || null, draft: Boolean(t.draft) })))'));
  const final = tasks.find((item) => item.of === 'draft-task-1');
  assert.ok(final, '成片必须作为新任务进同一个对话（否则结果无处显示）');
  assert.equal(final.jobId, 'job-draft-1');
  assert.equal(final.draft, false, '成片自身不带样片标记');
}

export async function testFinalizeRefusesWithoutUpstreamTaskId() {
  const { context, calls } = buildContext();
  await flush(); await flush();
  seedTask(context, draftTask({ resultTaskId: '' }));

  const before = calls.posts.length;
  await read(context, `finalizeDraft(activeTask()).then(() => 'done')`);
  assert.equal(calls.posts.length, before, '拿不到样片任务 id 时绝不能提交（否则会打一次必然失败的付费请求）');
}

if (import.meta.url === `file://${process.argv[1]}` || process.argv[1]?.endsWith('test_free_creation_draft_mode.mjs')) {
  const tests = [
    ['样片模式是独立云端模型且锁死 480P', testDraftEntryIsACloudModelPinnedTo480p],
    ['选中样片模式后分辨率就是 480P', testDraftEntryDefaultsTo480p],
    ['样片模式没抢走默认选中的 Seedance 2.5', testDraftEntryDoesNotStealTheDefaultSelection],
    ['样片模式禁用「继续修改」并说明原因', testDraftDisablesContinueEditing],
    ['第一段提交下发 draft=1', testDraftSubmitSendsDraftFlag],
    ['普通生成不带 draft', testPlainVideoSubmitHasNoDraftFlag],
    ['样片模式拒绝「继续修改」并给出替代路径', testDraftRejectsContinueEditing],
    ['「用这条样片出成片」只出现在样片结果上', testDraftButtonAppearsOnDraftResultOnly],
    ['成片只发 draft_task_id + 目标分辨率', testFinalizeSendsOnlyDraftTaskAndResolution],
    ['拿不到样片任务 id 时拒绝提交', testFinalizeRefusesWithoutUpstreamTaskId],
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
