/**
 * shared-ui/job-cards.js 的契约测试。
 *
 * 在受控沙箱里求值共享库（它是 IIFE，需要 window 存在），断言它的对外契约：
 *   1) 终态集合必须含 interrupted —— 卡片会画「已中断」样式，但判定不认它时，
 *      已中断的任务会被误判为「可取消」（2026-09-28 修掉的真 bug）；
 *   2) jobDisplayStatus：已有产出但状态未到终态时按 succeeded 显示。
 *
 * 后续任务会往本文件追加：字段恒四格的渲染契约、副本一致性、model-capabilities 一致性。
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

const src = read('shared-ui/job-cards.js');
// 阈值只用来确认「不是空壳」。本任务阶段库里还只有纯逻辑（渲染在下一个任务加），
// 所以取 500 而不是上千 —— 断言要盯住"有没有实现"，不是"文件够不够大"。
assert.ok(src.length > 500, 'shared-ui/job-cards.js 应包含实际实现');

const sandbox = {
  window: {},
  document: { createElement: () => ({ style: {}, dataset: {}, appendChild() {}, addEventListener() {} }) },
};
const JobCards = new Function('window', 'document', src + '\nreturn window.JobCards;')(
  sandbox.window,
  sandbox.document,
);
assert.ok(JobCards, 'shared-ui/job-cards.js 应挂载 window.JobCards');

// --- 契约 1：interrupted 属于终态，已中断任务不可取消 ---
assert.equal(JobCards.isCancellableJob('interrupted'), false, '已中断不得可取消');
assert.equal(JobCards.isCancellableJob('cancelled'), false, '已取消不得可取消');
assert.equal(JobCards.isCancellableJob('running'), true, '运行中可取消');
assert.equal(JobCards.isCancellableJob('queued'), true, '排队中可取消');

// --- 契约 2：有产出就按 succeeded 显示 ---
assert.equal(JobCards.jobDisplayStatus({ status: 'running', results: [{}] }), 'succeeded');
assert.equal(JobCards.jobDisplayStatus({ status: 'running', first_url: 'x' }), 'succeeded');
assert.equal(JobCards.jobDisplayStatus({ status: 'running' }), 'running');
assert.equal(JobCards.jobDisplayStatus({ status: 'SUCCEEDED' }), 'succeeded', '大小写归一');
assert.equal(JobCards.jobDisplayStatus({ status: 'failed', results: [{}] }), 'failed');
assert.equal(JobCards.jobDisplayStatus(null), '', '空 job 不得抛错');

// --- 契约 3：详情弹窗字段恒为四格 ---
// 这是本次的核心修复：此前字段是 `if (user) items.push(用户)` 条件拼装的，
// username 有值就 4 格、没值就 3 格 —— 用户看到的就是「每个任务属性个数都不一样」。
const labelsOf = (html) => [...html.matchAll(/job-detail-meta-label">([^<]*)</g)].map((m) => m[1]);
const EXPECTED_LABELS = ['Seed', '运行耗时', '使用模型', '用户'];

const withUser = JobCards.renderDetailHtml(
  { job_id: 'a', status: 'succeeded', username: 'alice', params: { seed: '42', model: 'm1' } },
  { appPath: '' },
);
assert.deepEqual(labelsOf(withUser), EXPECTED_LABELS, '有用户名时四格');

const noUser = JobCards.renderDetailHtml(
  { job_id: 'b', status: 'succeeded', params: { model: 'm1' } },
  { appPath: '' },
);
assert.deepEqual(labelsOf(noUser), EXPECTED_LABELS, 'username 为空时「用户」格仍须存在');
assert.match(noUser, /job-detail-meta-item[\s\S]*?用户[\s\S]*?—/, '空值应显示 — 而不是隐藏整格');

// Seed 取不到真实值时显示「随机」（后端尚未回写 actual seed）
assert.match(noUser, /Seed[\s\S]*?随机/, 'Seed 缺失应显示 随机');

// --- 契约 4：配置缺省不得抛错，appPath 可推断（规格 4.4 节）---
const bare = JobCards.create({});
assert.ok(bare, 'create({}) 应返回实例而不是抛错');
assert.equal(typeof bare.renderDetailHtml, 'function', '实例应提供 renderDetailHtml');
assert.equal(JobCards.inferAppPath('/seedance/index.html'), '/seedance', 'Portal 代理模式');
assert.equal(JobCards.inferAppPath('/nano-banana/index.html'), '/nano-banana');
assert.equal(JobCards.inferAppPath('/index.html'), '', '独立模式无前缀');

// 注入的 formatRuntime 应被采用（各应用日期解析不同，属差异注入）
const injected = JobCards.renderDetailHtml(
  { job_id: 'c', status: 'running' },
  { formatRuntime: () => '已运行 55秒' },
);
assert.match(injected, /已运行 55秒/, 'config.formatRuntime 注入应生效');

// --- 契约 5：两份副本必须与共享源逐字相同 ---
// 副本由 tools/sync-ui.py 生成；改了源忘记同步时，这里会立刻失败。
for (const target of ['seedance/static/job-cards.js', 'nano-banana/static/job-cards.js', 'portal/static/job-cards.js']) {
  assert.equal(
    read(target),
    src,
    `${target} 必须与 shared-ui/job-cards.js 一致 —— 请跑 python tools/sync-ui.py`,
  );
}

// 注入的 formatModel 也应被采用：接入点 id → 可读 label 的映射依赖各应用自己的
// 模型目录（火山通道的 ep-xxx 尤其需要），共享库不得硬用 job.model 把它退化。
const withModel = JobCards.renderDetailHtml(
  { job_id: 'd', status: 'succeeded', model: 'ep-20260912121809-6wt62' },
  { formatModel: () => 'Seedream 5.0 Pro' },
);
assert.match(withModel, /Seedream 5\.0 Pro/, 'config.formatModel 注入应生效');

const withoutModel = JobCards.renderDetailHtml(
  { job_id: 'e', status: 'succeeded', model: 'doubao-seedance-2-5-260628' },
  {},
);
assert.match(withoutModel, /doubao-seedance-2-5-260628/, '未注入时回退到 job.model');

// --- 契约 6：detailFields 是字段定义的唯一来源 ---
// Portal 用 petite-vue 声明式模板，吃不了 HTML 串，只能 v-for 遍历这份数据。
// 两边（HTML 渲染 / Vue 模板）必须同源，否则又回到"各写一份、各自演化"。
const fields = JobCards.detailFields({ job_id: 'f', status: 'running' }, {});
assert.deepEqual(fields.map((f) => f.label), EXPECTED_LABELS, 'detailFields 与 HTML 渲染必须同源');
assert.equal(fields[3].value, '—', '无用户名时 value 是 —（该项仍在，不被省略）');
assert.equal(fields[1].key, 'runtime', '运行耗时项带 key，供模板定位刷新');
assert.equal(fields[0].value, '随机', 'Seed 缺失显示 随机');

// --- 一致性：model-capabilities.js 也必须与共享源一致 ---
// 它长期躺在 shared-ui/ 里却没人同步 —— nano-banana 的副本悄悄比共享源多了 4 行
// 能力校验（modes / response_formats），谁也没发现。2026-09-28 把那 4 行回迁进
// 共享源后三方统一，这条断言防止再次漂移。
const capSrc = read('shared-ui/model-capabilities.js');
for (const target of ['seedance/static/model-capabilities.js', 'nano-banana/static/model-capabilities.js']) {
  assert.equal(read(target), capSrc, `${target} 必须与 shared-ui/model-capabilities.js 一致`);
}

console.log('job cards: contract ok');
