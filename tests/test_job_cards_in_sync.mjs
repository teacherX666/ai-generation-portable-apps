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

console.log('job cards: contract ok');
