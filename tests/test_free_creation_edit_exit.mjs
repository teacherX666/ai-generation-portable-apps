// 自由创作「继续修改」退出编辑态后的状态一致性。
//
// Bug（修复前）：clearEditContext() 只清 state.editSourceTaskId / editBasePrompt，
// 从不清理输入框。于是「点历史缩略图」「点参考素材上的 ×」「修改成功后自动退出」
// 这三条路径会把编辑上下文清掉，而输入框里仍留着 `原提示词\n\n修改要求：…`。
// 之后再点生成，buildPayload() 里 isEdit 已经是 false，于是走非编辑分支
// `prompt = els.prompt.value.trim()`，把这段「修改要求」原样塞进一次**普通生成**：
// 视频 task_mode 退回 reference、duration/ratio 用常规值；编辑态按钮文案、
// 智能视频编辑标签、编辑基准角标也全部消失。
//
// 不变量：编辑上下文被清除后，输入框不得再残留「修改要求」片段。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const APP_JS = path.join(ROOT, 'portal', 'static', 'free-creation', 'app.js');

const FRAGMENT = '\u4fee\u6539\u8981\u6c42'; // 修改要求

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
  // 缓存，保证代码绑过监听的节点与测试拿到的是同一个对象
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
  const schemas = {
    '/seedance/api/schema': {
      providers: {
        volcengine: {
          label: 'volcengine',
          models: [{ id: 'doubao-seedance-2-5-260628', label: 'Seedance 2.5' }],
          duration_range: [4, 15],
          resolutions: ['720p'],
          ratios: ['16:9'],
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
      return { ok: true, status: 200, json: async () => ({ ok: true, job_id: 'job-1' }) };
    }
    if (schemas[url]) return { ok: true, status: 200, json: async () => schemas[url] };
    if (String(url).startsWith('/api/platform/history')) return { ok: true, status: 200, json: async () => ({ items: [] }) };
    if (String(url).startsWith('/api/auth')) return { ok: true, status: 200, json: async () => ({ username: 'tester', role: 'user' }) };
    // 结果文件下载（继续修改的第一步）
    return {
      ok: true,
      status: 200,
      blob: async () => ({ type: 'video/mp4', size: 1024 }),
      json: async () => ({}),
    };
  };

  class FakeFile {
    constructor(parts, name, opts = {}) {
      this.name = name;
      this.type = opts.type || '';
      this.size = 1024;
    }
  }

  class FakeFileReader {
    readAsDataURL() { this.result = 'data:video/mp4;base64,AAAA'; if (this.onload) this.onload(); }
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

function seedSuccessfulVideoTask(context) {
  vm.runInContext(`
    const now = Date.now();
    const task = {
      id: 'src-task-1', app: 'seedance', mode: 'video', prompt: '\u4e00\u53ea\u732b\u5728\u5f39\u94a2\u7434',
      model: 'Seedance 2.5', modelId: 'doubao-seedance-2-5-260628', provider: 'volcengine',
      params: { ratio: '16:9', resolution: '720p', duration: 8, repeat_count: 1 },
      attachments: [], status: 'success', resultUrl: '/seedance/api/download/abc',
      resultName: 'cat.mp4', conversationId: 'conv-1', createdAt: now, updatedAt: now,
    };
    state.conversations = [{
      id: 'conv-1', mode: 'video', prompt: task.prompt,
      params: { video: { ratio: '16:9', resolution: '720p', duration: 8, repeat_count: 1 },
                image: { aspect_ratio: '1:1', image_size: '2K', repeat_count: 1 } },
      tasks: [task], createdAt: now, updatedAt: now,
    }];
    state.activeConversationId = 'conv-1';
    state.activeTaskId = 'src-task-1';
    state.mode = 'video';
    renderPreview();
    renderParamSummary();
  `, context);
}

async function enterEditMode(context) {
  vm.runInContext(`els.refine.dispatch('click')`, context);
  await flush(); await flush(); await flush();
}

function read(context, expression) {
  return vm.runInContext(expression, context);
}

// ── 复现：移除源素材会把编辑上下文清掉，但输入框残留「修改要求」 ────────────────
export async function testSourceRemovalDoesNotLeakEditFragment() {
  const { context } = buildContext();
  await flush(); await flush();
  seedSuccessfulVideoTask(context);

  await enterEditMode(context);
  assert.equal(read(context, 'state.editSourceTaskId'), 'src-task-1', '应进入编辑态');
  assert.equal(read(context, 'els.generateLabel.textContent'), '\u63d0\u4ea4\u4fee\u6539', '生成按钮应变成「提交修改」');
  assert.ok(read(context, 'els.prompt.value').includes(FRAGMENT), '输入框应追加「修改要求：」');

  // 点参考素材上的 × —— 这是合法的「移除源素材」操作，会清除编辑上下文
  vm.runInContext(`
    const node = els.attachments.children[0];
    node.querySelector('button').dispatch('click');
  `, context);

  assert.equal(read(context, 'state.editSourceTaskId'), null, '移除源素材后应退出编辑态');
  assert.ok(
    !read(context, 'els.prompt.value').includes(FRAGMENT),
    '退出编辑态后输入框不得再残留「修改要求」片段，否则它会被当普通提示词提交',
  );
}

// ── 复现：切换历史缩略图同样会清编辑态，但不清输入框 ──────────────────────────
export async function testHistorySwitchDoesNotLeakEditFragment() {
  const { context } = buildContext();
  await flush(); await flush();
  seedSuccessfulVideoTask(context);

  await enterEditMode(context);
  assert.ok(read(context, 'els.prompt.value').includes(FRAGMENT));

  // 点另一个历史缩略图 → switchActiveTask() → clearEditContext()
  vm.runInContext(`
    const other = activeConversation().tasks[0];
    const again = { ...other, id: 'src-task-2' };
    activeConversation().tasks.push(again);
    switchActiveTask('src-task-2');
  `, context);

  assert.equal(read(context, 'state.editSourceTaskId'), null, '切换任务后应退出编辑态');
  assert.ok(
    !read(context, 'els.prompt.value').includes(FRAGMENT),
    '退出编辑态后输入框不得再残留「修改要求」片段',
  );
}

// ── 端到端：残片真的会被当成普通生成提交出去 ────────────────────────────────
export async function testSubmitAfterLostEditContextIsNotPlainGeneration() {
  const { context, calls } = buildContext();
  await flush(); await flush();
  seedSuccessfulVideoTask(context);

  await enterEditMode(context);
  vm.runInContext(`els.prompt.value += '\u628a\u732b\u6362\u6210\u72d7';`, context);

  // 用户顺手点了一下另一个历史缩略图，编辑上下文被清掉，输入框却还留着残片
  vm.runInContext(`
    activeConversation().tasks.push({ ...activeConversation().tasks[0], id: 'src-task-2' });
    switchActiveTask('src-task-2');
  `, context);
  assert.equal(read(context, 'state.editSourceTaskId'), null);

  vm.runInContext(`els.generate.dispatch('click')`, context);
  await flush(); await flush(); await flush();

  const submit = calls.posts.filter((p) => p.url === '/seedance/api/jobs/json').pop();
  assert.ok(submit, '应发出一次生成提交');
  assert.ok(
    !String(submit.body.prompt || '').includes(FRAGMENT),
    `普通生成不应携带「修改要求」残片，实际收到: ${submit.body.prompt}`,
  );
}

if (import.meta.url === `file://${process.argv[1]}` || process.argv[1]?.endsWith('test_free_creation_edit_exit.mjs')) {
  const tests = [
    ['移除源素材不泄漏编辑残片', testSourceRemovalDoesNotLeakEditFragment],
    ['切换历史缩略图不泄漏编辑残片', testHistorySwitchDoesNotLeakEditFragment],
    ['编辑态丢失后提交的不是普通生成', testSubmitAfterLostEditContextIsNotPlainGeneration],
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
