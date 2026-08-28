import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), 'utf8');

const sharedCore = read('shared-ui/portal-ui-core.css');
assert.ok(sharedCore.length > 100, 'shared UI Core should not be empty');

const syncedTargets = [
  'portal/static/ui/portal-ui-core.css',
  'seedance/static/ui/portal-ui-core.css',
  'nano-banana/static/ui/portal-ui-core.css',
  'dreamina/static/ui/portal-ui-core.css',
  'rag-assistant/static/ui/portal-ui-core.css',
  'volcengine-portrait/static/ui/portal-ui-core.css',
  'feishu-generation-agent/src/feishu_generation_agent/web/static/ui/portal-ui-core.css',
];
for (const target of syncedTargets) {
  assert.equal(read(target), sharedCore, `${target} must match shared UI Core`);
}

const entryExpectations = [
  ['portal/static/index.html', '/ui/portal-ui-core.css'],
  ['seedance/static/index.html', 'ui/portal-ui-core.css'],
  ['nano-banana/static/index.html', 'ui/portal-ui-core.css'],
  ['dreamina/static/index.html', '/ui/portal-ui-core.css'],
  ['rag-assistant/static/index.html', 'ui/portal-ui-core.css'],
  ['volcengine-portrait/static/index.html', '/ui/portal-ui-core.css'],
  ['feishu-generation-agent/src/feishu_generation_agent/web/static/index.html', 'static/ui/portal-ui-core.css'],
];
for (const [entry, href] of entryExpectations) {
  assert.match(read(entry), new RegExp(`href=["']${href.replaceAll('/', '\\/')}["']`), `${entry} should load ${href}`);
}

const portal = read('portal/static/index.html');
const tabNames = [...portal.matchAll(/<button\b[^>]*class="[^"]*app-tab[^"]*"[^>]*data-tab="([^"]+)"/g)].map((match) => match[1]);
const panelNames = [...portal.matchAll(/<div\b[^>]*class="[^"]*tab-panel[^"]*"[^>]*id="tab-([^"]+)"/g)].map((match) => match[1]);
assert.equal(tabNames.length, 10, 'Portal should expose ten application tabs');
assert.deepEqual([...new Set(tabNames)].sort(), [...new Set(panelNames)].sort(), 'Portal tabs and panels must expose the same names');

for (const name of tabNames) {
  const button = portal.match(new RegExp(`<button\\b[^>]*data-tab="${name}"[^>]*>`));
  assert.ok(button, `Portal tab ${name} should exist`);
  assert.match(button[0], /\brole="tab"/);
  assert.match(button[0], /\baria-selected="(?:true|false)"/);
  assert.match(button[0], new RegExp(`\\baria-controls="tab-${name}"`));
  const panel = portal.match(new RegExp(`<div\\b[^>]*id="tab-${name}"[^>]*>`));
  assert.ok(panel, `Portal panel ${name} should exist`);
  assert.match(panel[0], /\brole="tabpanel"/);
  assert.match(panel[0], new RegExp(`\\baria-labelledby="tab-btn-${name}"`));
}

const iframePanels = ['seedance', 'nb', 'feishu-generation-agent', 'infinite-canvas', 'rag-assistant'];
for (const name of iframePanels) {
  assert.match(
    portal,
    new RegExp(`<div\\b[^>]*class="[^"]*tab-panel iframe-panel[^"]*"[^>]*id="tab-${name}"[\\s\\S]*?<iframe[^>]+class="portal-iframe"`),
    `Portal iframe panel ${name} should contain a portal iframe`,
  );
}

const portalStyles = read('portal/static/styles.css');
assert.match(portalStyles, /body\s*\{[^}]*height:\s*100vh[^}]*overflow:\s*hidden/s, 'Portal body must own the outer viewport');
assert.match(portalStyles, /\.tab-panel\.active\s*\{[^}]*overflow:\s*auto/s, 'Portal native panels must own their scroll area');
assert.match(portalStyles, /\.tab-panel\.iframe-panel\.active\s*\{[^}]*overflow:\s*hidden/s, 'Portal iframe panels must suppress nested outer scrolling');
assert.match(portalStyles, /\.tab-panel\[hidden\]\s*\{[^}]*display:\s*none\s*!important/s, 'Hidden Portal panels must never participate in layout');
assert.match(portalStyles, /\.tab-panel\.iframe-panel\.active\s*\{[^}]*flex:\s*1 1 auto/s, 'Active iframe panels must consume the remaining Portal height');
assert.match(portalStyles, /\.portal-content\.ui-admin-page--narrow\s*\{[^}]*max-width:\s*920px/s, 'Portal compatibility CSS must preserve the narrow stats page modifier');

const sharedLayout = read('shared-ui/styles/layout.css');
assert.match(sharedLayout, /\.ui-portal-content\s*>\s*\.tab-panel\s*\{[^}]*display:\s*none[^}]*flex:\s*0 0 auto/s, 'Shared Portal layout must hide inactive panels by default');
assert.match(sharedLayout, /iframe\.portal-iframe\s*\{[^}]*flex:\s*1 1 auto[^}]*min-height:\s*0/s, 'Shared Portal layout must allow iframe content to shrink without overlap');

const sharedComponents = read('shared-ui/styles/components.css');
assert.match(sharedComponents, /\.ui-btn--secondary:hover[^}]*color:\s*var\(--ui-text-primary\)[^}]*background:\s*var\(--ui-bg-sunken\)/s, 'Secondary hover must keep readable semantic contrast');
assert.match(sharedComponents, /\.ui-btn--ghost:hover[^}]*color:\s*#172033[^}]*background:\s*#eef2f7/s, 'Ghost hover must gain a readable background');
assert.match(sharedComponents, /\.ui-btn\s*\{[^}]*width:\s*auto[^}]*height:\s*auto[^}]*white-space:\s*nowrap/s, 'Shared buttons must override legacy full-width button rules');
assert.match(sharedComponents, /\.ui-table-actions\s*\{[^}]*flex-wrap:\s*nowrap[^}]*gap:/s, 'Table action groups must keep compact actions aligned horizontally');
assert.match(sharedComponents, /\.ui-segment__button\s*\{[^}]*width:\s*auto[^}]*flex:\s*0 0 auto/s, 'Segment buttons must not inherit page-wide button sizing');
assert.doesNotMatch(sharedComponents, /\.ui-tab\s*\{[^}]*(?:width|height):\s*auto/s, 'Shared tabs must not override application navigation sizing');
assert.match(sharedComponents, /\.ui-form-grid--action\s*\{[^}]*grid-template-columns:[^}]*max-content/s, 'Action forms should keep their submit action at the end of the desktop row');
assert.match(sharedComponents, /\.ui-form-grid--action\s*\{[^}]*margin-block-end:\s*var\(--ui-space-4\)/s, 'Action forms should keep clear vertical separation from the result content below');
assert.match(sharedComponents, /\.ui-form-grid--action\s*>\s*label\s*\{[^}]*min-width:\s*0[^}]*margin:\s*0/s, 'Action form labels should not inherit legacy margins or overflow their grid cells');
assert.match(sharedComponents, /\.ui-form-grid--action\s*>\s*label\s*>\s*input[\s\S]*?height:\s*36px[\s\S]*?padding-block:\s*0/s, 'Action form controls should share one explicit baseline and height');
assert.match(sharedComponents, /\.ui-form-grid__actions\s*\{[^}]*justify-content:\s*flex-start/s, 'Form actions should occupy a dedicated left-aligned row');
assert.match(sharedComponents, /\.ui-form-grid__end-action\s*\{[^}]*justify-content:\s*flex-end/s, 'End actions should align to the last column of an action form');
assert.match(sharedComponents, /\.ui-date-field\s*\{[^}]*align-items:\s*center[^}]*margin:\s*0/s, 'Shared date fields must align labels and inputs on one baseline');
assert.match(sharedComponents, /\.ui-switch-field\s*\{[^}]*white-space:\s*nowrap/s, 'Switch labels must stay on one line');
assert.match(sharedComponents, /\.ui-switch-field\s*>\s*input\[type="checkbox"\]:checked\s*\{[^}]*background:\s*var\(--ui-action-primary\)/s, 'Shared switch controls should expose a clear checked state');
assert.match(sharedComponents, /\.ui-select\.ui-select--sm\s*\{[^}]*width:\s*auto[^}]*min-height:\s*30px/s, 'Compact selects should not expand to the full table cell width');
assert.match(sharedComponents, /\.ui-btn--warning-outline\s*\{[^}]*color:\s*var\(--ui-color-danger-text\)[^}]*border-color:\s*var\(--ui-color-danger\)/s, 'Warning outline actions should be visibly distinct without using a solid danger fill');

assert.match(portal, /class="hist-filters ui-filter-bar"/, 'History filters should use the shared filter bar primitive');
assert.match(portal, /class="ui-table-actions"[\s\S]*?toggleEnabled[\s\S]*?resetPassword/, 'User management actions should share one horizontal action group');
assert.match(portal, /class="portal-content ui-admin-page ui-admin-page--narrow"/, 'Stats and management should use the narrow admin page contract');
assert.match(portal, /class="admin-form ui-form-grid ui-form-grid--action"[\s\S]*?角色[\s\S]*?class="ui-form-grid__end-action"[\s\S]*?创建用户/, 'User creation action should sit at the far end of the username form row');
assert.match(portal, /class="ui-select ui-select--sm"[^>]*setRole/, 'User roles should use the compact shared select');
assert.match(portal, /class="ui-btn ui-btn--warning-outline ui-btn--sm"[^>]*resetPassword/, 'Password reset should use the warning action hierarchy');
assert.match(portal, /<h2>飞书日报<\/h2>[\s\S]*?class="ui-toolbar ui-toolbar--wrap ui-toolbar--start"[\s\S]*?class="ui-date-field"/, 'Feishu report actions should use the shared left-aligned toolbar and date field');
assert.match(portal, /Portal 基础 URL[\s\S]*?<\/label>\s*<label class="ui-switch-field">[\s\S]*?启用定时/, 'Feishu scheduling switch should share the row after the Portal URL field');
assert.match(portal, /<h2>按日期段查询 \/ 导出<\/h2>[\s\S]*?class="ui-toolbar__group"[\s\S]*?rangeStart[\s\S]*?rangeEnd[\s\S]*?<\/div>\s*<div class="ui-toolbar__group">[\s\S]*?loadRange\(\)[\s\S]*?exportRange\(\)/, 'Date range fields should stay together on the left and actions in a separate group');

const portalScript = read('portal/static/app.js');
assert.match(portalScript, /--portal-header-height[\s\S]*ResizeObserver/, 'Director overlay offset must track the measured Portal header height');
assert.match(portalStyles, /body\.director-open\s+\.ui-portal-content\s*\{[^}]*padding-right:\s*320px/s, 'Expanded director should reserve space only inside the content region');
assert.doesNotMatch(portalStyles, /body\.director-(?:open|collapsed)\s*\{[^}]*padding-right/s, 'Director state must not resize the body or top navigation');
assert.match(portalStyles, /body\.director-collapsed\s+\.director-sidebar\s*\{[^}]*width:\s*0[^}]*background:\s*transparent/s, 'Collapsed director should leave only its external arrow visible');
assert.match(portalStyles, /body\.director-collapsed\s+\.director-header,[\s\S]*?\.director-body\s*\{[^}]*display:\s*none\s*!important/s, 'Collapsed director content must be fully hidden');

const feishuStyles = read('feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css');
assert.match(feishuStyles, /--brand:\s*#176b50/ , 'Feishu should retain its green approval accent');
assert.match(feishuStyles, /\.secondary:hover:not\(:disabled\)[^}]*background:\s*var\(--brand-soft-hover\)/s, 'Feishu secondary hover should remain visibly green');

for (const [entry, rootSelector] of [
  ['seedance/static/styles.css', '#sd-app'],
  ['nano-banana/static/styles.css', '#nb-app'],
]) {
  const styles = read(entry);
  assert.match(styles, /html\s*,\s*body\s*\{[^}]*height:\s*100%[^}]*overflow:\s*hidden/s, `${entry} must suppress body scrolling`);
  assert.match(styles, new RegExp(`${rootSelector.replace('#', '\\#')}[^\\{]*\\{[^}]*min-height:\\s*0`, 's'), `${entry} root must allow flex children to shrink`);
  assert.match(styles, /\.app-tabs\s*\{[^}]*background:\s*#111827[^}]*border-bottom:\s*1px solid #334155/s, `${entry} should retain the original dark workspace tab bar`);
  assert.match(styles, /pre\s*\{[^}]*background:\s*#111827[^}]*color:\s*#dbe7ff/s, `${entry} should retain readable dark activity logs`);
  assert.match(styles, /\.rowButtons\s*\{[^}]*repeat\(auto-fit,\s*minmax\(84px,\s*1fr\)\)/s, `${entry} action rows should adapt before buttons overlap`);
}

const rag = read('rag-assistant/static/index.html');
assert.match(rag, /main[^>]*style="[^"]*overflow-y:\s*auto|class="[^"]*ui-main[^"]*"/, 'RAG assistant should have a dedicated main scroll region');
assert.match(rag, /html,\s*body|overflow-hidden/, 'RAG assistant should suppress root-level duplicate scrolling');

console.log('ui core structure: ok');
