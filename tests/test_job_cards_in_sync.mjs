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

console.log('job cards: contract ok');
