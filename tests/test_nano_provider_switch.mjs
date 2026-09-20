import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class FakeFormData {
  constructor(form) {
    this.values = new Map();
    if (form && form.elements) {
      for (const [name, el] of Object.entries(form.elements)) {
        if (el && typeof el.value !== 'undefined' && !el.disabled) this.values.set(name, el.value);
      }
    }
  }
  set(name, value) { this.values.set(name, value); }
  delete(name) { this.values.delete(name); }
  get(name) { return this.values.get(name); }
  has(name) { return this.values.has(name); }
}

const keyInput = {
  name: 'api_key', value: 'original-t8star-key', type: 'password',
  disabled: false, readOnly: false, placeholder: '留空使用本地配置',
};
const seedInput = { name: 'seed', value: '123', type: 'number', disabled: false };
const varySeed = { name: 'vary_seed', value: 'on', type: 'checkbox', disabled: false, checked: true };
const sizeSelect = { name: 'image_size', value: '2K', type: 'select-one', tagName: 'SELECT', options: [] };
const modelSelect = { name: 'model', value: 'old-model', type: 'select-one', tagName: 'SELECT', options: [] };
const baseUrlInput = { name: 'base_url', value: 'https://ai.t8star.org', type: 'text', readOnly: false };
const form = { elements: { api_key: keyInput, base_url: baseUrlInput, seed: seedInput, vary_seed: varySeed, image_size: sizeSelect, model: modelSelect } };
const document = {
  getElementById(id) { return id === 'nb-form' ? form : null; },
  querySelector(selector) {
    const match = selector.match(/name="([^"]+)"/);
    return match ? form.elements[match[1]] || null : null;
  },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return { style: {}, classList: { add() {}, toggle() {} }, appendChild() {} }; },
  head: { appendChild() {} }, body: { appendChild() {} },
};
const localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const window = { location: { pathname: '/nano-banana/index.html' }, _dlProgress: {} };
const sandbox = {
  window, document, localStorage, FormData: FakeFormData,
  PetiteVue: { createApp() { return { mount() {} }; } },
  URL, URLSearchParams, Blob, File: class File {}, fetch: async () => ({}),
  crypto: { randomUUID: () => 'uuid' }, console, setTimeout, setInterval: () => 1,
  clearInterval() {}, alert() {}, confirm: () => true, DataTransfer: class {},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('nano-banana/static/model-capabilities.js', 'utf8'), sandbox);
vm.runInContext(fs.readFileSync('nano-banana/static/app.js', 'utf8'), sandbox);

const app = window.NanoBananaApp();
app.providers = {
  t8star: {
    label: 'T8Star', base_url: 'https://ai.t8star.org',
    image_size_options: ['1K', '2K', '4K'], supports_seed: true,
    models: [{ id: 'old-model', label: 'Old model' }, { id: 'disabled-model', label: 'Disabled model', disabled: true }], defaults: { model: 'old-model', image_size: '2K' },
  },
  volcengine: {
    label: '火山引擎官方', base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    company_key: true, company_key_available: true,
    image_size_options: ['1K', '1.5K', '2K'], supports_seed: false,
    max_reference_images: 10,
    models: [{ id: 'doubao-seedream-5-0-pro-260628' }],
    defaults: { model: 'doubao-seedream-5-0-pro-260628', image_size: '2K' },
  },
};
app.provider = 't8star';
app._activeProvider = 't8star';
app._personalKeyHint = '已检测到原供应商 key';

// PetiteVue's v-model may update the reactive value before @change runs.
app.provider = 'volcengine';
app.applyProvider('volcengine');
assert.equal(keyInput.value, '');
assert.equal(keyInput.readOnly, true);
assert.equal(app.baseUrlReadonly, true);
assert.match(keyInput.placeholder, /服务器托管/);
assert.deepEqual(Array.from(app.imageSizeOptions), ['1K', '1.5K', '2K']);
assert.equal(app.supportsSeed, false);
assert.equal(seedInput.disabled, true);
assert.equal(varySeed.disabled, true);
assert.equal(app.maxReferenceImages, 10);

app.models = sandbox.buildUnifiedImageModels(app.providers, true);
modelSelect.value = 'disabled-model';
assert.equal(app.ensureAvailableModel(), true, 'disabled image models must trigger fallback');
assert.equal(modelSelect.value, app.models.find((item) => !item.disabled).key, 'image fallback must choose the first enabled model');
assert.match(app.modelHint, /已失效/, 'image fallback must explain why the model changed');
const nanoIndex = fs.readFileSync('nano-banana/static/index.html', 'utf8');
assert.match(nanoIndex, /:disabled="!!m.disabled"/, 'image model options must be greyed out');
assert.match(nanoIndex, /已失效/, 'image model labels must explain the disabled state');

const managedSubmission = await app.formDataWithSavedMedia();
assert.equal(managedSubmission.has('api_key'), false);

app.provider = 't8star';
app.applyProvider('t8star');
assert.equal(keyInput.value, '', 'API key input is no longer restored in the UI');
assert.equal(app.baseUrlReadonly, false);
assert.deepEqual(Array.from(app.imageSizeOptions), ['1K', '2K', '4K']);
assert.equal(app.supportsSeed, true);
assert.equal(seedInput.disabled, false);
assert.equal(varySeed.disabled, false);
const realProviders = JSON.parse(fs.readFileSync('nano-banana/providers.json', 'utf8')).providers;
const merged = sandbox.buildUnifiedImageModels(realProviders, true);
const geminiGroup = merged.find((item) => item.key === 'gemini-3-pro-image');
assert.ok(geminiGroup, '1K/2K/4K Gemini variants must merge into one model');
assert.deepEqual(Array.from(geminiGroup.resolutions), ['1K', '2K', '4K'], 'merged model must expose all resolutions');
assert.equal(sandbox.routeForUnifiedImageModel(geminiGroup, '4K').modelId, 'gemini-3-pro-image-4k', '4K must route to the 4K backend model');
const qwenGroup = merged.find((item) => item.key.indexOf('qwen') >= 0);
assert.match(qwenGroup.label, /（本地免费）/, 'local models must be labelled as local free');
assert.doesNotMatch(nanoIndex, /name="api_key"/, 'image interface API key field must be removed');
assert.doesNotMatch(nanoIndex, /name="provider"/, 'image interface provider selector must be removed');
assert.doesNotMatch(nanoIndex, /cleanCache\(\)/, 'image interface cache cleanup must be removed');
app.providers = realProviders;
app.models = merged;
app.model = geminiGroup.key;
app.imageSize = '4K';
const routedSubmission = await app.formDataWithSavedMedia();
assert.equal(routedSubmission.get('model'), 'gemini-3-pro-image-4k', '4K selection must submit the 4K backend model');
assert.equal(routedSubmission.get('provider'), 't8star', 'merged image model must route back to the owning provider');
assert.equal(routedSubmission.has('api_key'), false, 'merged image submission must not send a client API key');
const qwenCaps = sandbox.window.ModelCapabilities.capabilitiesFor(realProviders, 'comfyui_local', 'qwen2511');
assert.deepEqual(Array.from(qwenCaps.modes), ['img2img'], 'per-model mode capability must reach the UI boundary');
const kreaCaps = sandbox.window.ModelCapabilities.capabilitiesFor(realProviders, 'comfyui_local', 'krea2_three_stage');
assert.deepEqual(Array.from(kreaCaps.modes), ['text2img'], 'text-only image models must constrain the mode selector');
console.log('nano provider switch: ok');
