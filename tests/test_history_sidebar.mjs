import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appJs = fs.readFileSync(path.join(ROOT, "portal/static/app.js"), "utf8");

const elStub = () => ({ addEventListener() {}, classList: { toggle() {} }, style: {}, value: "" });
globalThis.document = { getElementById: () => elStub(), querySelector: () => null, querySelectorAll: () => [], body: elStub(), addEventListener() {} };
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async () => {} } }, configurable: true });
globalThis.location = { pathname: "/", search: "", replace() {} };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
globalThis.PetiteVue = { createApp: () => ({ mount() {} }), reactive: (v) => v };

const fn = new Function(appJs + "\nreturn { HistoryApp };");
const { HistoryApp } = fn();
if (typeof HistoryApp !== "function") throw new Error("HistoryApp not defined");
const app = HistoryApp();
if (typeof app.openDetail !== "function" || typeof app.downloadItem !== "function") {
  throw new Error("openDetail/downloadItem missing");
}
if (app.statusText("done") !== "已成功") throw new Error("statusText mapping broken");
if (app.statusText("failed") !== "已失败") throw new Error("statusText failed broken");
if (app.shortTime(0) !== "—") throw new Error("shortTime empty broken");
console.log("history sidebar: ok");
process.exit(0);
