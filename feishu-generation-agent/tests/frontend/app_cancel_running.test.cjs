"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BitableState = require(
  "../../src/feishu_generation_agent/web/static/bitable-state.js"
);
const ReferenceUploadState = require(
  "../../src/feishu_generation_agent/web/static/reference-upload-state.js"
);
const ReferenceMutationState = require(
  "../../src/feishu_generation_agent/web/static/reference-mutation-state.js"
);
const ReviewState = require(
  "../../src/feishu_generation_agent/web/static/review-state.js"
);

class FakeNode {
  constructor(tagName = "div", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.disabled = false;
    this.hidden = id === "error-message";
    this.textContent = "";
    this.value = "";
    this.listeners = new Map();
    this.classList = { toggle() {} };
  }
  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }
  async dispatch(name) {
    await Promise.all(
      (this.listeners.get(name) || []).map((listener) =>
        listener({ target: this })
      ),
    );
  }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll(selector) {
    const tags = new Set(selector.split(",").map((v) => v.trim().toUpperCase()));
    const matches = [];
    const visit = (node) => {
      if (tags.has(node.tagName)) matches.push(node);
      node.children.forEach(visit);
    };
    this.children.forEach(visit);
    return matches;
  }
  setAttribute() {}
  scrollIntoView() {}
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() { return payload; },
  };
}

async function settle() {
  for (let i = 0; i < 8; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function loadApp(fetch) {
  const nodes = new Map();
  const getNode = (id) => {
    if (!nodes.has(id)) nodes.set(id, new FakeNode("div", id));
    return nodes.get(id);
  };
  getNode("animation-category-tab").dataset.category = "animation";
  getNode("portrait-category-tab").dataset.category = "portrait";
  const document = {
    createElement: (tagName) => new FakeNode(tagName),
    getElementById: getNode,
    querySelector: () => new FakeNode("main"),
  };
  const context = {
    BitableState,
    ReferenceMutationState,
    ReferenceUploadState,
    ReviewState,
    document,
    fetch,
    confirm: () => true,
    location: { reload() {} },
    setInterval: () => 1,
    clearInterval() {},
    console,
  };
  const source = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8"
  );
  vm.runInNewContext(source, context);
  await settle();
  return { getNode };
}

const approval = {
  revision: 1,
  document_summary: "",
  tasks: [],
  media_assets: [],
  excluded_assets: [],
  selected_task_ids: [],
  coverage: {},
  validation_issues: [],
  ingest_issue_records: [],
  vision_issues: [],
};

test("a running run shows and uses the force-cancel endpoint", async () => {
  let cancelCalled = false;
  let decisionCalled = false;

  const view = {
    run_id: "run-running",
    thread_id: "thread-running",
    source_url: "https://example.invalid/running",
    status: "running",
    events: [],
    privacy: {},
    approval: { ...approval, document_title: "current" },
  };

  const app = await loadApp(async (url, options = {}) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/provider-preferences") return jsonResponse(200, {});
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [
        { run_id: "run-running", display_text: "current", status: "running" },
      ]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url === "/api/runs/run-running/cancel" && options.method === "POST") {
      cancelCalled = true;
      view.status = "cancelled";
      return jsonResponse(200, { run_id: "run-running", status: "cancelled" });
    }
    if (url === "/api/runs/run-running/decision") {
      decisionCalled = true;
      return jsonResponse(200, { ok: true });
    }
    if (url.startsWith("/api/runs/")) {
      return jsonResponse(200, view);
    }
    throw new Error(`unexpected request: ${url}`);
  });

  await settle();

  assert.equal(app.getNode("cancel-button").hidden, false);

  await app.getNode("cancel-button").dispatch("click");
  await settle();

  assert.equal(cancelCalled, true);
  assert.equal(decisionCalled, false);
});
