import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const eventsEl = { textContent: '主题状态负责渲染此处' };
const resultsEl = { innerHTML: '', querySelector() { return null; }, appendChild() {} };
const document = {
  getElementById(id) {
    if (id === 'sd-events') return eventsEl;
    if (id === 'sd-results') return resultsEl;
    return null;
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return { style: {}, classList: { add() {} }, appendChild() {} }; },
  head: { appendChild() {} },
  body: { appendChild() {} },
};
const localStorage = {
  getItem() { return null; },
  setItem() {},
  removeItem() {},
};
const window = {
  location: { pathname: '/seedance/index.html', search: '' },
  _dlProgress: {},
};
const sandbox = {
  window, document, localStorage,
  PetiteVue: { createApp() { return { mount() {} }; } },
  URL, URLSearchParams, Blob,
  FormData: class FormData {},
  DataTransfer: class DataTransfer {},
  Event: class Event {},
  crypto: { randomUUID: () => 'workspace-test' },
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  console, setTimeout, setInterval: () => 1, clearInterval() {},
  alert() {}, confirm: () => true,
};

vm.createContext(sandbox);
// 共享卡片库先加载：app.js 的 isCancellableJob / jobDisplayStatus 是它的薄封装
// （`window.JobCards.xxx`），缺了它 _renderJobToDom / _buildJobTile 直接 TypeError。
vm.runInContext(fs.readFileSync('seedance/static/job-cards.js', 'utf8'), sandbox);
vm.runInContext(fs.readFileSync('seedance/static/app.js', 'utf8'), sandbox);

const app = window.SeedanceApp();
app._renderJobToDom({
  status: 'running',
  done: 1,
  total: 2,
  events: [{ time: '10:00:00', message: '任务一运行中' }],
  results: [],
  errors: [],
});

assert.match(resultsEl.innerHTML, /任务一运行中/, '结果面板仍应刷新任务进度');
assert.equal(
  eventsEl.textContent,
  '主题状态负责渲染此处',
  '结果重绘不能直接改写由主题响应式状态管理的日志框',
);


// The history grid is DOM-rendered (renderJobsGrid -> _buildJobTile), so the
// cancel control lives in app.js rather than an index.html v-if template.
assert.match(fs.readFileSync('seedance/static/index.html', 'utf8'), /sd-jobsGrid/, 'history grid container must exist');
// 2026-10-19 修：这里原来断言源码里有 `isCancellableJob(j)`，而实际调用形态是
// `isCancellableJob(jobDisplayStatus(j))`（要考虑「有产出但状态没走到终态」的显示归一）
// —— HEAD 里就没有 `isCancellableJob(j)` 这个串，这条断言在改动前一直是红的。
assert.match(fs.readFileSync('seedance/static/app.js', 'utf8'), /isCancellableJob\(jobDisplayStatus\(j\)\)/, 'history jobs should expose cancellation');
assert.match(fs.readFileSync('seedance/static/app.js', 'utf8'), /isCancellableJob/, 'all non-terminal states should be cancellable');
assert.match(fs.readFileSync('seedance/static/app.js', 'utf8'), /_cancellingJobIds/, 'cancel requests should be deduplicated');

console.log('seedance topic render: ok');
