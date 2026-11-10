import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const mounted = {};
const genericElement = {
  style: {},
  addEventListener() {},
  appendChild() {},
  removeChild() {},
  classList: { add() {}, remove() {}, toggle() {} },
};
const document = {
  querySelectorAll() { return []; },
  querySelector() { return null; },
  getElementById() { return genericElement; },
  createElement() { return { ...genericElement, style: {}, classList: genericElement.classList }; },
  head: genericElement,
  body: genericElement,
};

const requests = [];
let createResponse = { ok: true, group_id: 'group-new' };
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || 'GET' });
  let data;
  if (url === '/api/apps') {
    data = { ok: true, apps: [] };
  } else if (url === '/api/auth/me') {
    data = { ok: true, username: 'tester', role: 'user' };
  } else if (url === '/volcengine-portrait/api/virtual/groups' && options.method === 'POST') {
    data = createResponse;
  } else if (url === '/volcengine-portrait/api/virtual/groups') {
    data = { ok: true, groups: [{ group_id: 'group-new', name: '新建组' }] };
  } else {
    data = { ok: false, error: `unexpected request: ${url}` };
  }
  return { ok: true, status: 200, json: async () => data };
}

const localStorage = {
  values: new Map(),
  getItem(key) { return this.values.get(key) || null; },
  setItem(key, value) { this.values.set(key, value); },
};
const location = {
  protocol: 'https:', pathname: '/', search: '',
  replace() {},
};
const window = { location, _dlProgress: {}, isSecureContext: true };
const sandbox = {
  window, document, localStorage, location, fetch,
  PetiteVue: {
    createApp(components) {
      Object.assign(mounted, components);
      return { mount() {} };
    },
  },
  crypto: { randomUUID: () => 'workspace-test' },
  URL, URLSearchParams, Blob,
  FormData: class FormData {},
  DataTransfer: class DataTransfer {},
  Event: class Event {},
  navigator: {}, console,
  setTimeout: () => 1,
  setInterval: () => 1,
  clearInterval() {},
  alert() {},
  confirm: () => true,
};

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('portal/static/app.js', 'utf8'), sandbox);

const app = mounted.VolcenginePortraitApp();
// 2026-10-19 修：这里原来写死 `doubao-seedance-2-0-mini-260615`，而 providers.json 里的 mini
// 早已换成接入点 id（`ep-20260912121738-vtd78`）且 `disabled: false` —— 断言依赖的是写入时的
// 账号开通状态，配置一变整条用例就断在这里（后半段全跑不到）。改成按配置驱动：
// 有 disabled 模型就验它；没有就注入一个探针，保住「可见但不可选 + 回退 + 文案解释」的不变量。
const disabledModel = app.portraitModels.find(m => m.disabled);
const probeId = disabledModel ? disabledModel.id : '__disabled_probe__';
if (!disabledModel) {
  app.portraitModels.push({ id: probeId, label: '已失效（探针）', disabled: true });
}
const mini = app.portraitModels.find(m => m.id === probeId);
assert.equal(mini.disabled, true, 'Seedance disabled 模型必须在 Portal 选择器里可见但不可选');
app.model = mini.id;
assert.equal(app.ensureAvailablePortraitModel(), true, 'stale disabled model selection must trigger fallback');
assert.notEqual(app.model, probeId, 'Portal fallback must move off the disabled model');
assert.match(app.modelHint, /已失效/, 'Portal fallback must explain why the model changed');
const portalHtml = fs.readFileSync('portal/static/index.html', 'utf8');
assert.match(portalHtml, /!!m\.disabled/, 'Portal must grey out disabled models');
assert.match(portalHtml, /已失效/, 'Portal disabled-model label must explain its state');
app.groupName = '新建组';
await app.createGroup();

assert.equal(app.groupId, 'group-new');
assert.equal(app.assetGroupId, 'group-new');
assert.deepEqual(
  app.groups.map(group => ({ ...group })),
  [{ group_id: 'group-new', name: '新建组' }],
  '创建成功后应自动重新读取组列表',
);
assert.equal(
  requests.filter(request => request.url === '/volcengine-portrait/api/virtual/groups' && request.method === 'GET').length,
  1,
  '创建成功后应发起一次组列表刷新请求',
);

requests.length = 0;
createResponse = { ok: false, error: '创建失败' };
const failedApp = mounted.VolcenginePortraitApp();
await failedApp.createGroup();
assert.equal(failedApp.uploadError, true);
assert.equal(
  requests.some(request => request.url === '/volcengine-portrait/api/virtual/groups' && request.method === 'GET'),
  false,
  '创建失败时不应刷新组列表',
);

console.log('portrait group refresh: ok');
