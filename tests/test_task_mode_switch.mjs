// Task-type mode switch.
//
// Ark 2.5 auto-classifies each request as reference / extend / edit from the
// prompt. If a user picks up an "extend" phrase (say "续写") in an otherwise
// reference-shaped submission, the job gets rejected with "you asked for edit
// but sent a positive duration" — but only *after* the queue wait.
//
// This switch moves the classification into an explicit picker, so ratio /
// duration collapse to the required values before submission. Per-mode memory
// means switching away and back restores what the user typed.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function makeOption(value) {
  // dataset 必须给：syncFrameRatioLock / syncDraftResolution 都用 dataset 记「临时补的选项」，
  // 假元素缺它会在切换任务类型时直接 TypeError（这个文件因此长期一条用例都跑不到）。
  return { value, textContent: value, dataset: {} };
}

function makeSelect(name, values) {
  const options = values.map(makeOption);
  const select = {
    name,
    tagName: 'SELECT',
    type: 'select-one',
    options,
    dataset: {},
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

const modelSelect = makeSelect('model', ['doubao-seedance-2-5-260628']);
const durationInput = { name: 'duration', type: 'number', value: '12', min: '4', max: '30' };
const resolutionSelect = makeSelect('resolution', ['480p', '720p']);
const ratioSelect = makeSelect('ratio', ['16:9', '9:16', '1:1', '4:3', '3:4', '21:9', 'adaptive']);

const form = {
  elements: { model: modelSelect, duration: durationInput, resolution: resolutionSelect, ratio: ratioSelect },
  addEventListener() {},
};
const listeners = {};
modelSelect.addEventListener = (event, fn) => { listeners[event] = fn; };

const document = {
  getElementById(id) { return id === 'sd-form' ? form : null; },
  querySelector(selector) {
    const match = selector.match(/name="([^"]+)"/);
    return match ? form.elements[match[1]] || null : null;
  },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return { value: '', textContent: '' }; },
  head: { appendChild() {} }, body: { appendChild() {} },
};

const sandbox = {
  window: { location: { pathname: '/seedance/index.html' }, _dlProgress: {} },
  document,
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  FormData: class { constructor() {} set() {} get() {} },
  PetiteVue: { createApp() { return { mount() {} }; } },
  URL, URLSearchParams, Blob, File: class {},
  fetch: async () => ({}),
  crypto: { randomUUID: () => 'uuid' },
  console, setTimeout, setInterval: () => 1, clearInterval() {},
  alert() {}, confirm: () => true, DataTransfer: class {},
  // 锁 ratio / 锁分辨率时会 new Option(...) 补一个占位选项（真浏览器行为）。
  Option: class { constructor(text, value) { this.textContent = text; this.value = value; this.dataset = {}; } },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('seedance/static/app.js', 'utf8'), sandbox);

const providers = JSON.parse(fs.readFileSync('seedance/providers.json', 'utf8'));
const app = sandbox.window.SeedanceApp();
app.providers = { volcengine: providers.providers.volcengine };
app.models = providers.providers.volcengine.models.map(m => ({
  // key 必须有：applyModelLimits 与样片模式守卫都按 key 找当前条目；缺了它会在
  // `item.key === this.model` 上退化成「匹配第一个条目」（undefined === undefined）。
  key: m.id,
  id: m.id,
  label: m.label,
  duration_range: m.duration_range || null,
  resolutions: m.resolutions || null,
  ratios: m.ratios || null,
  // 与 buildUnifiedVideoModels 一致：样片模式能力来自 providers.json 的 capabilities.draft。
  draft: !!(m.capabilities && m.capabilities.draft),
}));
app.applyModelLimits();  // seeds the ratio/duration option lists on 2.5

// --- edit mode locks ratio to adaptive and duration to -1 ----------------
app.taskMode = 'edit';
app.changeTaskMode();
assert.equal(ratioSelect.value, 'adaptive',
  'edit tasks reject any ratio other than adaptive — must be set on switch');
assert.equal(durationInput.value, '-1',
  'edit tasks reject positive durations — must be set to the sentinel');

// Even if the form got out of sync with memory (say another handler wrote a
// positive number into the input while we were still in edit mode), a fresh
// switch to edit must re-apply the lock — not read the corrupted memory back.
durationInput.value = '25';
ratioSelect.value = '16:9';
app.taskMode = 'reference';
app.changeTaskMode();  // leaving edit records adaptive/-1 into mem.edit (self-heal)
app.taskMode = 'edit';
app.changeTaskMode();
assert.equal(durationInput.value, '-1',
  're-entering edit must lock duration back to -1 regardless of the form value at the moment of the switch');
assert.equal(ratioSelect.value, 'adaptive',
  're-entering edit must lock ratio back to adaptive');

// --- extend mode locks ratio, but keeps a positive duration --------------
app.taskMode = 'extend';
app.changeTaskMode();
assert.equal(ratioSelect.value, 'adaptive', 'extend also requires adaptive ratio');
assert.ok(Number(durationInput.value) > 0,
  `extend duration must be a positive number, got ${durationInput.value}`);

// --- switching back to reference restores what was there before ---------
// Preload memory: pretend the user typed 12 at 16:9 before ever leaving.
app.taskMode = 'reference';
ratioSelect.value = '16:9';
durationInput.value = '12';
app.changeTaskMode();  // save these into the reference memory
// Now leave to edit and come back.
app.taskMode = 'edit';
app.changeTaskMode();
assert.equal(durationInput.value, '-1', 'edit locks -1');
app.taskMode = 'reference';
app.changeTaskMode();
assert.equal(ratioSelect.value, '16:9', 'reference must restore the ratio the user last chose');
assert.equal(durationInput.value, '12', 'reference must restore the duration the user last chose');

// --- each mode keeps its own memory --------------------------------------
app.taskMode = 'extend';
app.changeTaskMode();
durationInput.value = '7';                      // user tweaks the extend duration
app.taskMode = 'edit';
app.changeTaskMode();
assert.equal(durationInput.value, '-1');
app.taskMode = 'extend';
app.changeTaskMode();
assert.equal(durationInput.value, '7',
  'extend must remember its own duration, not fall back to a default');

// --- swaps between edit and extend must not corrupt each other ----------
app.taskMode = 'edit';
app.changeTaskMode();
assert.equal(durationInput.value, '-1');
app.taskMode = 'extend';
app.changeTaskMode();
assert.equal(durationInput.value, '7', 'extend memory survives an edit round-trip');

// --- reference mode does not force a ratio -----------------------------
// User interaction order matches what the <select @change> would do:
// the user tweaks the ratio *while already in reference mode* — then the
// switch to extend and back must not overwrite that choice.
if (app.taskMode !== 'reference') {
  app.taskMode = 'reference';
  app.changeTaskMode();
}
ratioSelect.value = '9:16';
// Simulate a later reference-mode operation that captures the ratio into
// its memory (in the real UI, changeTaskMode also runs on same-mode
// selection because the <select> fires @change unconditionally).
app._taskModeMemory.reference.ratio = '9:16';

app.taskMode = 'extend';
app.changeTaskMode();
assert.equal(ratioSelect.value, 'adaptive', 'extend forces adaptive');

app.taskMode = 'reference';
app.changeTaskMode();
assert.equal(ratioSelect.value, '9:16',
  'reference must not silently rewrite the ratio just because extend used adaptive');

// --- 样片模式（Seedance 2.5 Draft，2026-10-19 接入）---------------------
// 官方硬约束：开启样片模式后**只允许 480p**，传其他分辨率直接报错。所以切到样片模式就把
// 分辨率钉成 480p 并置灰；离开时解锁。置灰的控件不进 FormData，提交处会显式 set，见 app.js。
app.taskMode = 'draft';
app.changeTaskMode();
assert.equal(resolutionSelect.value, '480p', '样片模式必须把分辨率钉成 480p');
assert.equal(resolutionSelect.disabled, true, '样片模式下分辨率应置灰（提交处再显式写一次）');
assert.ok(Number(durationInput.value) > 0, '样片模式的时长不受特殊限制，应保留正数');
assert.notEqual(ratioSelect.value, '', '样片模式的 ratio 不该被清空');

app.taskMode = 'reference';
app.changeTaskMode();
assert.equal(resolutionSelect.disabled, false, '离开样片模式必须解锁分辨率');

// 模型不支持样片模式（没有 capabilities.draft）时，切过去要**退回 reference** 并给出提示，
// 而不是留一个注定让方舟报错的组合。
app.models = app.models.map(m => Object.assign({}, m, { draft: false }));
app.taskMode = 'draft';
app.changeTaskMode();
assert.equal(app.taskMode, 'reference', '不支持的模型上切样片模式必须自动退回');
assert.match(app.modelHint || '', /样片模式/, '退回原因必须说清楚是「不支持样片模式」');
app.models = providers.providers.volcengine.models.map(m => ({
  key: m.id, id: m.id, label: m.label, duration_range: m.duration_range || null,
  resolutions: m.resolutions || null, ratios: m.ratios || null,
  draft: !!(m.capabilities && m.capabilities.draft),
}));
app.applyModelLimits();

// 样片模式能力必须走**生产路径**传上来：/api/config → providersFromConfig()（它会给每个模型
// 重建一份字段固定的对象）→ buildUnifiedVideoModels()。2026-10-19 真机上就是这里漏了
// capabilities，结果「所有模型都不支持样片模式」——功能整个失效但不报错。所以这条断言刻意
// 经过 providersFromConfig，而不是直接喂 providers.json 的原始对象。
const normalizedProviders = sandbox.providersFromConfig(providers.providers);
const unified = sandbox.buildUnifiedVideoModels(normalizedProviders, true);
const flag25 = unified.find(m => (m.routes || []).some(r => r.modelId === 'doubao-seedance-2-5-260628'));
const flag20 = unified.find(m => (m.routes || []).some(r => r.modelId === 'doubao-seedance-2-0-260128'));
assert.equal(flag25.draft, true, '2.5 必须被标成支持样片模式（经 providersFromConfig 这条生产路径）');
assert.equal(flag20.draft, false, '2.0 不支持样片模式（官方能力表：样片模式仅 2.5）');
assert.ok(flag25.routes.every(r => r.modelId !== 'doubao-seedance-2-5-260628' || r.draft),
  '线路级 draft 标记也要传到位（activeRoute.draft 是 UI 的第二个判据来源）');

console.log('test_task_mode_switch.mjs: all assertions passed');
