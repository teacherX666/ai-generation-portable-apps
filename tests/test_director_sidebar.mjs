import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appJs = fs.readFileSync(path.join(ROOT, "portal/static/app.js"), "utf8");

// 最小 DOM 桩：app.js 顶层只应触碰这些
const elStub = () => ({
  addEventListener() {}, classList: { toggle() {} }, style: {}, value: "",
});
globalThis.document = {
  getElementById: () => elStub(),
  querySelector: () => null,
  querySelectorAll: () => [],
  body: elStub(),
  addEventListener() {},
};
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true,
});
globalThis.location = { pathname: "/", search: "", replace() {} };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
globalThis.PetiteVue = {
  createApp: () => ({ mount() {} }),
  reactive: (v) => v,
};

// 把 IIFE 求值到共享作用域
const fn = new Function(appJs + "\nreturn { DirectorApp };");
const { DirectorApp } = fn();

if (typeof DirectorApp !== "function") {
  throw new Error("DirectorApp not defined");
}
const app = DirectorApp();
if (app.skills.length !== 3) throw new Error("skills must contain 3 entries");
if (typeof app.run !== "function" || typeof app.fillToImage !== "function") {
  throw new Error("run/fillToImage missing");
}
if (!/async init\(\)\s*\{[\s\S]*?classList\.toggle\("director-collapsed", this\.collapsed\)[\s\S]*?classList\.toggle\("director-open", !this\.collapsed\)/.test(appJs)) {
  throw new Error("director layout state must initialize before the first toggle");
}
app.resultText = "测试提示词";
app.fillToImage();
if (app.skill !== "text2image" || app.input !== "测试提示词") {
  throw new Error("fillToImage chaining broken");
}
console.log("director sidebar: ok");
// app.js 尾部有 portal 会话轮询代码，会保持事件循环不退出——断言完成后显式退出
process.exit(0);
