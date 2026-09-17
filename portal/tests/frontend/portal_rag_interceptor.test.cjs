"use strict";

/**
 * Portal 的「生成前知识库检查」拦截器（portal/static/js/portal-rag-interceptor.js）。
 *
 * 用户报的问题（2026-09-17）：「我生成提示词的时候就已经用了 RAG 增强效果，
 * 但是后面生成的时候还会触发弹窗」。
 *
 * 根因：飞书任务 Agent 的提示词是 planner 写的，而 planner 在**写之前**就已经
 * 拿文档文本查过知识库、把命中规则当上下文喂进去了
 * （feishu-generation-agent 的 integrations/rag_prompt_optimizer.py）。可是审批页
 * 点「批准生成」会 POST /api/runs/{id}/decision，body 里带着这些已经按知识库
 * 写好的 prompt —— 拦截器看到 `/api/runs` 就再查一次知识库并弹「检测到飞书
 * 知识库规则」，成了重复且必然误报的第二道闸。
 *
 * 这里锁住两件事：这个子应用的请求不再被拦；其它子应用照旧被拦。
 */

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = readFileSync(
  join(__dirname, "../../static/js/portal-rag-interceptor.js"),
  "utf8",
);

function makeNode(tag) {
  return {
    tag,
    className: "",
    textContent: "",
    type: "",
    style: { cssText: "" },
    children: [],
    listeners: {},
    removed: false,
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...items) {
      this.children.push(...items);
    },
    remove() {
      this.removed = true;
    },
    addEventListener(name, listener) {
      (this.listeners[name] = this.listeners[name] || []).push(listener);
    },
  };
}

function walk(node, visit) {
  visit(node);
  (node.children || []).forEach((child) => walk(child, visit));
}

function findByText(root, text) {
  let found = null;
  walk(root, (node) => {
    if (!found && node.textContent === text) found = node;
  });
  return found;
}

function loadInterceptor({ detected, matches = [] }) {
  const upstream = [];
  const preflightBodies = [];
  const appended = [];
  const alerts = [];

  const fetchImpl = async (input, init) => {
    const url = typeof input === "string" ? input : (input && input.url) || String(input);
    if (url.includes("/api/rag/preflight")) {
      preflightBodies.push(JSON.parse((init && init.body) || "{}"));
      return { json: async () => ({ detected, matches }) };
    }
    upstream.push(url);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };

  // window.alert 必须存在：拦截器用 notify() 提示「正在检查飞书知识库」，
  // 而 notify 的 catch 里还会再调一次 alert —— 缺了它整段检查会被静默跳过，
  // 测试就会「因为没检查」而通过（这个坑第一版就踩到了）。
  const fakeWindow = {
    fetch: fetchImpl,
    alert: (message) => alerts.push(message),
  };
  const document = {
    createElement: (tag) => makeNode(tag),
    body: { appendChild: (node) => appended.push(node) },
  };
  const context = {
    window: fakeWindow,
    document,
    Response,
    FormData,
    AbortController,
    setTimeout,
    clearTimeout,
    JSON,
    Promise,
    console,
  };
  vm.runInNewContext(SOURCE, context, { filename: "portal-rag-interceptor.js" });

  return {
    fetch: fakeWindow.fetch,
    upstream,
    preflightBodies,
    appended,
    alerts,
  };
}

async function post(fetchImpl, url, body) {
  return fetchImpl(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

test("飞书任务 Agent 的请求不再被重复拦一次知识库检查", async () => {
  const interceptor = loadInterceptor({ detected: true, matches: [{ title: "镜头粒度" }] });

  await post(
    interceptor.fetch,
    "/feishu-generation-agent/api/runs/run-1/decision",
    { action: "approve", tasks: [{ prompt: "镜头1：奶奶抡锤砸石膏腿。" }] },
  );

  assert.deepEqual(interceptor.preflightBodies, [], "不该再查一遍知识库");
  assert.equal(interceptor.appended.length, 0, "不该再弹窗");
  assert.deepEqual(interceptor.upstream, [
    "/feishu-generation-agent/api/runs/run-1/decision",
  ]);
});

test("其它子应用的生成请求照旧先查知识库", async () => {
  const interceptor = loadInterceptor({ detected: false });

  await post(interceptor.fetch, "/seedance/api/jobs", { prompt: "一只猫在跑" });

  assert.equal(interceptor.preflightBodies.length, 1);
  assert.equal(interceptor.preflightBodies[0].prompt, "一只猫在跑");
  assert.deepEqual(interceptor.upstream, ["/seedance/api/jobs"]);
});

test("命中规则时其它子应用仍然弹「检测到飞书知识库规则」", async () => {
  const interceptor = loadInterceptor({
    detected: true,
    matches: [{ title: "镜头粒度", content: "一镜到底要写清主体" }],
  });

  const pending = post(interceptor.fetch, "/nano-banana/api/jobs", {
    prompt: "一只猫在跑",
  });

  // 弹窗是异步挂上去的，等一拍再点「继续生成」。
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(interceptor.appended.length, 1, "应当弹窗");
  const overlay = interceptor.appended[0];
  const continueButton = findByText(overlay, "继续生成");
  assert.ok(continueButton, "弹窗要有「继续生成」");
  continueButton.listeners.click[0]();

  await pending;
  assert.deepEqual(interceptor.upstream, ["/nano-banana/api/jobs"]);
});