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

  append(...children) {
    this.children.push(...children);
  }

  prepend(...children) {
    this.children.unshift(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  querySelectorAll(selector) {
    const tags = new Set(
      selector.split(",").map((value) => value.trim().toUpperCase()),
    );
    const matches = [];
    const visit = (node) => {
      if (tags.has(node.tagName)) matches.push(node);
      node.children.forEach(visit);
    };
    this.children.forEach(visit);
    return matches;
  }

  setAttribute() {}
  removeAttribute() {}
  scrollIntoView() {}
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() {
      return payload;
    },
  };
}

async function settle() {
  for (let index = 0; index < 8; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function loadApp(fetch) {
  const nodes = new Map();
  const intervals = new Map();
  let intervalId = 0;
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
    // app.js 会在 document 上装「拖放防误伤」的全局监听（2026-09-29）—— 假 DOM 也得有
    // 这个方法，否则 app.js 一加载就 TypeError，整个文件 50+ 条用例一起红。
    listeners: {},
    addEventListener(name, listener) {
      (this.listeners[name] ||= []).push(listener);
    },
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
    setInterval: (callback) => {
      intervalId += 1;
      intervals.set(intervalId, callback);
      return intervalId;
    },
    clearInterval(id) { intervals.delete(id); },
    console,
  };
  const source = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8",
  );
  vm.runInNewContext(source, context);
  await settle();
  return {
    getNode,
    tick: async () => {
      await Promise.all([...intervals.values()].map((callback) => callback()));
    },
  };
}

function baseFetch(categoryResponse) {
  return async (url, options = {}) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") return jsonResponse(200, []);
    if (url.startsWith("/api/bitable/tasks?")) {
      const category = new URL(url, "https://local.invalid").searchParams.get(
        "category",
      );
      return categoryResponse(category);
    }
    if (options.method === "POST" && url.includes("/claim")) {
      return jsonResponse(409, { detail: "该任务已被领取" });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("scan errors stay with their category instead of the global error box", async () => {
  const app = await loadApp(
    baseFetch((category) =>
      category === "portrait"
        ? jsonResponse(503, { detail: "真人视图读取失败" })
        : jsonResponse(200, []),
    ),
  );

  await app.getNode("portrait-category-tab").dispatch("click");
  await app.getNode("animation-category-tab").dispatch("click");

  assert.equal(app.getNode("error-message").hidden, true);
  assert.equal(
    app.getNode("error-message").textContent.includes("真人视图读取失败"),
    false,
  );

  await app.getNode("portrait-category-tab").dispatch("click");

  assert.equal(app.getNode("bitable-status").textContent, "真人视图读取失败");
  assert.equal(app.getNode("error-message").hidden, true);
});

test("claim errors do not remain in the global box after a cached category switch", async () => {
  const app = await loadApp(
    baseFetch((category) =>
      jsonResponse(
        200,
        category === "animation"
          ? [{ record_id: "rec-animation", display_text: "动画需求" }]
          : [],
      ),
    ),
  );
  await app.getNode("portrait-category-tab").dispatch("click");
  await app.getNode("animation-category-tab").dispatch("click");
  const claimButton = app
    .getNode("bitable-task-list")
    .querySelectorAll("button")[0];

  await claimButton.dispatch("click");
  await app.getNode("portrait-category-tab").dispatch("click");

  assert.equal(app.getNode("error-message").hidden, true);
  assert.equal(
    app.getNode("error-message").textContent.includes("该任务已被领取"),
    false,
  );
});

test("task history includes reruns and allows switching between active and cancelled runs", async () => {
  const activeRun = {
    run_id: "run-rerun-active",
    thread_id: "thread-rerun-active",
    source_url: "https://acme.feishu.cn/docx/active",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "重新运行后的任务",
      revision: 2,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      selected_task_ids: [],
      coverage: {},
      validation_issues: [],
      ingest_issue_records: [],
      vision_issues: [],
    },
  };
  const cancelledRun = {
    ...activeRun,
    run_id: "run-cancelled",
    thread_id: "thread-cancelled",
    status: "cancelled",
    approval: { ...activeRun.approval, document_title: "已取消任务" },
  };
  const views = new Map([
    [activeRun.run_id, activeRun],
    [cancelledRun.run_id, cancelledRun],
  ]);
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{
        run_id: activeRun.run_id,
        display_text: "重新运行后的任务",
        status: "待审批",
      }]);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [{
        run_id: cancelledRun.run_id,
        display_text: "已取消任务",
        status: "失败",
        rerunnable: true,
      }]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url.startsWith("/api/runs/")) {
      const runId = url.split("/").at(-1);
      return jsonResponse(200, views.get(runId));
    }
    throw new Error(`unexpected request: ${url}`);
  });

  let rows = app.getNode("recent-run-list").children;
  assert.deepEqual(rows.map((row) => row.dataset.runId), [
    activeRun.run_id,
    cancelledRun.run_id,
  ]);
  assert.equal(app.getNode("run-history-summary").textContent, "1 个进行中 · 共 2 条");
  assert.equal(rows[0].querySelectorAll("button")[0].textContent, "当前查看");
  assert.equal(rows[0].querySelectorAll("button").length, 1);

  const switcher = app.getNode("current-run-switcher");
  assert.equal(switcher.disabled, false);
  assert.deepEqual(switcher.children.map((option) => option.value), [
    activeRun.run_id,
    cancelledRun.run_id,
  ]);

  switcher.value = cancelledRun.run_id;
  await switcher.dispatch("change");
  assert.equal(app.getNode("document-title").textContent, "已取消任务");

  rows = app.getNode("recent-run-list").children;
  assert.equal(rows[0].querySelectorAll("button")[0].textContent, "查看");
  switcher.value = activeRun.run_id;
  await switcher.dispatch("change");
  assert.equal(app.getNode("document-title").textContent, "重新运行后的任务");
  assert.equal(app.getNode("status-badge").textContent, "等待你审核");
});

test("approval view distinguishes blocking document and nonblocking asset issues", async () => {
  const runView = {
    run_id: "run-ingest-issues",
    thread_id: "thread-ingest-issues",
    source_url: "https://acme.feishu.cn/docx/issues",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "素材读取测试",
      revision: 7,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      selected_task_ids: [],
      coverage: {
        successful_total: 1,
        referenced_count: 1,
        excluded_count: 0,
        uncovered_count: 0,
        failed_count: 1,
      },
      validation_issues: [],
      ingest_issue_records: [
        {
          severity: "blocking",
          code: "sheet_export_timeout",
          display_message: "飞书电子表格导出超时，请稍后重试",
        },
        {
          severity: "asset",
          code: "media_download_failed",
          display_message: "文档图片下载失败，其他素材可继续处理",
          asset_id: "image-4",
          asset_kind: "image",
          failure_reason: "temporary",
        },
      ],
      blocking_ingest_issues: [],
      asset_ingest_issues: [],
      vision_issues: ["素材 asset-2 视觉分析失败：图片无法识别"],
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{
        run_id: runView.run_id,
        display_text: "素材读取测试",
      }]);
    }
    if (url === `/api/runs/${runView.run_id}`) {
      return jsonResponse(200, runView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  assert.equal(
    app.getNode("blocking-ingest-issues").textContent,
    "文档读取阻塞：飞书电子表格导出超时，请稍后重试",
  );
  assert.equal(app.getNode("blocking-ingest-issues").hidden, false);
  assert.equal(
    app.getNode("asset-ingest-issue-list").children[0].textContent,
    "图片 image-4：暂时下载失败，可点击重新读取",
  );
  assert.equal(app.getNode("asset-ingest-issues").hidden, false);
  assert.equal(app.getNode("retry-failed-assets-button").hidden, false);
  assert.equal(
    app.getNode("vision-issues").textContent,
    "素材识别失败（不影响其他素材）：素材 asset-2 视觉分析失败：图片无法识别",
  );
  assert.equal(app.getNode("vision-issues").hidden, false);
});

test("approval view surfaces every reviewer risk note", async () => {
  const notes = [
    "计划将镜头1、镜头2、镜头3合并为单个8秒视频任务，合并后单镜头时长被压缩，可能影响三组事件的完整呈现。",
    "计划 duration 设为8秒，文档未明确各镜头时长与总时长，该时长属推断，需人工确认。",
  ];
  const runView = {
    run_id: "run-audit-notes",
    thread_id: "thread-audit-notes",
    source_url: "https://acme.feishu.cn/docx/audit",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "审查员风险提示",
      revision: 3,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      selected_task_ids: [],
      coverage: {},
      validation_issues: [],
      ingest_issue_records: [],
      blocking_ingest_issues: [],
      asset_ingest_issues: [],
      vision_issues: [],
      audit_issues: notes,
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{
        run_id: runView.run_id,
        display_text: "审查员风险提示",
      }]);
    }
    if (url === `/api/runs/${runView.run_id}`) {
      return jsonResponse(200, runView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  // 默认展开：藏在折叠里等于没提示（用户 2026-09-21 巨型南瓜就是这么踩的）。
  assert.equal(app.getNode("audit-issues").hidden, false);
  assert.equal(app.getNode("audit-issues").open, true);
  assert.equal(app.getNode("audit-issues-count").textContent, "（2 条）");
  assert.deepEqual(
    app.getNode("audit-issues-list").children.map((item) => item.textContent),
    notes,
  );
});

test("审查员风险提示条数很多时只铺前几条，其余可展开", async () => {
  // 用户 2026-09-22：「为什么审查员风险提示有时候能显示 50 多条」。
  // 审查员是逐条列举遗漏、没有条数上限（实测 11~53 条），全铺出来会把审批页
  // 刷成一堵墙 —— 默认只铺前 5 条，其余折在「展开全部」后面。
  const notes = Array.from({ length: 53 }, (_, index) => `第 ${index + 1} 条风险提示`);
  const runView = {
    run_id: "run-audit-many",
    thread_id: "thread-audit-many",
    source_url: "https://acme.feishu.cn/docx/audit-many",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "风险提示很多",
      revision: 2,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      selected_task_ids: [],
      coverage: {},
      validation_issues: [],
      ingest_issue_records: [],
      blocking_ingest_issues: [],
      asset_ingest_issues: [],
      vision_issues: [],
      audit_issues: notes,
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{ run_id: runView.run_id, display_text: "风险提示很多" }]);
    }
    if (url === `/api/runs/${runView.run_id}`) return jsonResponse(200, runView);
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  const list = app.getNode("audit-issues-list");
  const more = app.getNode("audit-issues-more");
  assert.equal(app.getNode("audit-issues-count").textContent, "（53 条）");
  assert.equal(list.children.length, 5, "默认只铺前 5 条");
  assert.equal(list.children[0].textContent, "第 1 条风险提示");
  assert.equal(more.hidden, false);
  assert.equal(more.textContent, "展开全部 53 条");

  await more.dispatch("click");
  assert.equal(list.children.length, 53, "展开后要能看到全部");
  assert.equal(more.textContent, "只看前 5 条");

  await more.dispatch("click");
  assert.equal(list.children.length, 5, "可以再收起来");
});

test("approval view hides the reviewer block when the audit found nothing", async () => {
  const runView = {
    run_id: "run-audit-clean",
    thread_id: "thread-audit-clean",
    source_url: "https://acme.feishu.cn/docx/audit-clean",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "无风险提示",
      revision: 1,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      selected_task_ids: [],
      coverage: {},
      validation_issues: [],
      ingest_issue_records: [],
      blocking_ingest_issues: [],
      asset_ingest_issues: [],
      vision_issues: [],
      audit_issues: [],
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{
        run_id: runView.run_id,
        display_text: "无风险提示",
      }]);
    }
    if (url === `/api/runs/${runView.run_id}`) {
      return jsonResponse(200, runView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  assert.equal(app.getNode("audit-issues").hidden, true);
  assert.equal(app.getNode("audit-issues-list").children.length, 0);
});

test("approval view keeps the recovered asset result visible after retry", async () => {
  const runView = {
    run_id: "run-assets-recovered",
    thread_id: "thread-assets-recovered",
    source_url: "https://acme.feishu.cn/docx/recovered",
    status: "waiting_approval",
    events: [{ node: "retry_failed_assets", status: "completed" }],
    privacy: {},
    approval: {
      document_title: "素材已恢复",
      revision: 8,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      selected_task_ids: [],
      coverage: {
        successful_total: 2,
        referenced_count: 2,
        excluded_count: 0,
        uncovered_count: 0,
        failed_count: 0,
      },
      validation_issues: [],
      ingest_issue_records: [],
      blocking_ingest_issues: [],
      asset_ingest_issues: [],
      vision_issues: [],
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{
        run_id: runView.run_id,
        display_text: "素材已恢复",
      }]);
    }
    if (url === `/api/runs/${runView.run_id}`) {
      return jsonResponse(200, runView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  assert.equal(app.getNode("asset-ingest-issues").hidden, false);
  assert.equal(app.getNode("retry-failed-assets-button").hidden, true);
  assert.equal(
    app.getNode("retry-failed-assets-feedback").textContent,
    "失败素材已全部恢复，无需重新读取",
  );
});

test("failed runs show the safe provider error in the review panel", async () => {
  const runView = {
    run_id: "run-provider-error",
    thread_id: "thread-provider-error",
    source_url: "https://acme.feishu.cn/docx/provider-error",
    status: "failed",
    events: [],
    privacy: {},
    execution_records: [{
      task_id: "task-video",
      provider: "seedance",
      status: "submission_uncertain",
      error: {
        category: "provider_terminal_error",
        message: "生成服务拒绝了请求",
        retryable: false,
        code: "submit_http_400",
      },
    }],
    approval: {
      document_title: "供应商错误测试",
      revision: 1,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      coverage: {},
      validation_issues: [],
      ingest_issue_records: [],
      vision_issues: [],
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{ run_id: runView.run_id }]);
    }
    if (url === `/api/runs/${runView.run_id}`) {
      return jsonResponse(200, runView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  assert.equal(
    app.getNode("execution-errors").textContent,
    "生成失败：Seedance：生成服务拒绝了请求（submit_http_400）",
  );
  assert.equal(app.getNode("execution-errors").hidden, false);
});

test("failed history runs explain the missing artifacts instead of hiding the preview", async () => {
  const runView = {
    run_id: "run-failed-no-artifacts",
    thread_id: "thread-failed-no-artifacts",
    source_url: "https://acme.feishu.cn/wiki/failed",
    status: "failed",
    events: [],
    privacy: {},
    artifacts: [],
    last_error: {
      category: "permission_error",
      message: "The workflow node could not be completed",
      technical_detail: "GET /open-apis/wiki/v2/spaces/get_node: HTTP 400, code=131006",
      retryable: false,
    },
    approval: {
      document_title: "驶向大海",
      revision: 1,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      coverage: {},
      validation_issues: [],
      ingest_issue_records: [],
      vision_issues: [],
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{ run_id: runView.run_id }]);
    }
    if (url === `/api/runs/${runView.run_id}`) {
      return jsonResponse(200, runView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  assert.equal(app.getNode("artifact-review").hidden, false);
  assert.equal(app.getNode("artifact-list").children.length, 0);
  assert.equal(
    app.getNode("artifact-review-message").textContent,
    "本次运行未生成成片，请在下方的失败原因中查看详情。",
  );
  assert.equal(app.getNode("artifact-review-feedback-box").hidden, true);
  // 失败的那一版没有成片可勾选 → 「导出到结果表 / 重跑选中任务」仍然不出现；
  // 但重跑入口只在预览页（任务记录里的重跑按钮已删），所以这里要保留
  // 「重跑这一版」，否则失败的任务没地方重跑。
  assert.equal(app.getNode("artifact-review-actions").hidden, false);
  assert.equal(app.getNode("confirm-artifacts-button").hidden, true);
  assert.equal(app.getNode("adjust-artifacts-button").hidden, true);
  assert.equal(app.getNode("rerun-artifacts-button").hidden, false);
  assert.equal(
    app.getNode("validation-issues").textContent,
    "飞书应用没有权限读取该文档或素材，请检查文档分享与应用权限。",
  );
  assert.equal(app.getNode("permission-guide").hidden, false);
  assert.match(
    app.getNode("permission-guide-intro").textContent,
    /飞书任务agent.*知识库.*协作者/,
  );
});

test("trash supports paging, search, status filters, and filtered empty states", async () => {
  const archivedRuns = Array.from({ length: 10 }, (_, index) => ({
    run_id: `run-archived-${index + 1}`,
    display_text: index === 8 ? "目标失败任务" : `已删除任务 ${index + 1}`,
    status: index === 8 ? "failed" : "succeeded",
    updated_at: `2026-08-${String(index + 1).padStart(2, "0")}T10:00:00+08:00`,
  }));
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/archived-runs") {
      return jsonResponse(200, archivedRuns);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  await app.getNode("trash-button").dispatch("click");
  await settle();

  const trashList = app.getNode("trash-list");
  assert.equal(trashList.children.length, 8);
  assert.equal(app.getNode("trash-page-info").textContent, "第 1 / 2 页");
  assert.equal(app.getNode("trash-pagination").hidden, false);
  assert.equal(app.getNode("trash-prev").disabled, true);

  await app.getNode("trash-next").dispatch("click");
  assert.equal(trashList.children.length, 2);
  assert.equal(
    trashList.children[0].children[0].children[0].textContent,
    "目标失败任务",
  );
  assert.equal(app.getNode("trash-page-info").textContent, "第 2 / 2 页");
  assert.equal(app.getNode("trash-next").disabled, true);

  const search = app.getNode("trash-search");
  search.value = "目标";
  await search.dispatch("input");
  assert.equal(trashList.children.length, 1);
  assert.equal(app.getNode("trash-page-info").textContent, "第 1 / 1 页");
  assert.equal(app.getNode("trash-pagination").hidden, true);
  assert.equal(
    app.getNode("trash-result-summary").textContent,
    "找到 1 条，共 10 条已删除任务",
  );

  search.value = "";
  await search.dispatch("input");
  const statusFilter = app.getNode("trash-status-filter");
  assert.deepEqual(statusFilter.children.map((option) => option.value), [
    "",
    "生成完成",
    "执行失败",
  ]);
  statusFilter.value = "执行失败";
  await statusFilter.dispatch("change");
  assert.equal(trashList.children.length, 1);
  assert.equal(
    trashList.children[0].children[0].children[0].textContent,
    "目标失败任务",
  );

  search.value = "不存在";
  await search.dispatch("input");
  assert.equal(trashList.children.length, 1);
  assert.equal(trashList.children[0].textContent, "没有符合条件的任务。");
  assert.equal(
    app.getNode("trash-result-summary").textContent,
    "找到 0 条，共 10 条已删除任务",
  );
});


test("trash marks automatically archived records without claiming the user deleted them", async () => {
  // 用户 2026-09-24：「我不是只保留十个任务记录吗，多余的进回收站」。自动归档的
  // 记录必须出现在回收站里，而且要说清**不是**用户自己删的 —— 时间戳也只能叫
  // 「最后更新」（归档刻意不改 updated_at）。
  const archivedRuns = [
    {
      run_id: "run-auto-1",
      record_id: "rec-auto-1",
      display_text: "自动归档的任务",
      status: "succeeded",
      updated_at: "2026-08-01T10:00:00+08:00",
      auto_archived: true,
    },
    {
      run_id: "run-manual-1",
      record_id: "rec-manual-1",
      display_text: "手动删除的任务",
      status: "failed",
      updated_at: "2026-08-02T10:00:00+08:00",
      auto_archived: false,
    },
  ];
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/archived-runs") {
      return jsonResponse(200, archivedRuns);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  await app.getNode("trash-button").dispatch("click");
  await settle();

  const trashList = app.getNode("trash-list");
  assert.equal(trashList.children.length, 2);
  const rows = trashList.children.map((row) => row.children[0]);
  const autoDetails = rows.find(
    (details) => details.children[0].textContent === "自动归档的任务",
  );
  const manualDetails = rows.find(
    (details) => details.children[0].textContent === "手动删除的任务",
  );
  assert.ok(autoDetails, "自动归档的记录要能在回收站里找到");
  assert.ok(manualDetails);

  // 记录名仍是第一个子节点（既有回收站测试依赖这个结构）。
  const autoNote = autoDetails.children[autoDetails.children.length - 1];
  assert.equal(autoNote.className, "trash-auto-note");
  assert.match(autoNote.textContent, /自动归档/);
  assert.match(autoNote.textContent, /最近 10 条/);
  assert.match(autoDetails.children[1].textContent, /最后更新/);
  assert.doesNotMatch(autoDetails.children[1].textContent, /删除时间/);

  // 用户自己删的那条照旧：两行元信息，写「删除时间」。
  assert.equal(manualDetails.children.length, 2);
  assert.match(manualDetails.children[1].textContent, /删除时间/);

  // 恢复按钮对两种来源都在。
  assert.equal(
    trashList.children[0].children[1].children[0].textContent,
    "恢复",
  );
});


test("cancelling a run restores history controls and allows switching immediately", async () => {
  let cancelled = false;
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
  const views = {
    "run-current": {
      run_id: "run-current",
      thread_id: "thread-current",
      source_url: "https://example.invalid/current",
      status: "waiting_approval",
      events: [],
      privacy: {},
      approval: { ...approval, document_title: "当前任务" },
    },
    "run-other": {
      run_id: "run-other",
      thread_id: "thread-other",
      source_url: "https://example.invalid/other",
      status: "succeeded",
      events: [],
      privacy: {},
      artifacts: [],
      approval: { ...approval, document_title: "其他已完成任务" },
    },
  };
  const app = await loadApp(async (url, options = {}) => {
    if (url === "/api/health") return jsonResponse(200, { modes: { bitable: true } });
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, cancelled ? [] : [{
        run_id: "run-current",
        display_text: "当前任务",
        status: "待审批",
      }]);
    }
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        ...(cancelled ? [{
          run_id: "run-current",
          display_text: "当前任务",
          status: "失败",
          rerunnable: true,
        }] : []),
        {
          run_id: "run-other",
          display_text: "其他已完成任务",
          status: "已完成",
          rerunnable: true,
        },
      ]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url === "/api/runs/run-current/decision" && options.method === "POST") {
      cancelled = true;
      views["run-current"] = { ...views["run-current"], status: "cancelled" };
      return jsonResponse(200, { ok: true });
    }
    if (url.startsWith("/api/runs/")) {
      return jsonResponse(200, views[url.split("/").at(-1)]);
    }
    throw new Error(`unexpected request: ${url}`);
  });

  await app.getNode("cancel-button").dispatch("click");
  await settle();

  const rows = app.getNode("recent-run-list").children;
  const otherRow = rows.find((row) => row.dataset.runId === "run-other");
  const openButton = otherRow.querySelectorAll("button")[0];
  assert.equal(openButton.textContent, "查看");
  assert.equal(openButton.disabled, false);
  assert.equal(app.getNode("current-run-switcher").disabled, false);

  await openButton.dispatch("click");
  assert.equal(app.getNode("document-title").textContent, "其他已完成任务");
});

test("artifact review restores saved rerun selection and feedback", async () => {
  const runId = "run-artifact-restore";
  const view = {
    run_id: runId,
    thread_id: "thread-artifact-restore",
    source_url: "https://example.invalid/artifact-restore",
    status: "succeeded",
    events: [],
    privacy: {},
    delivery: null,
    artifacts: [{
      artifact_id: "artifact-1",
      task_id: "task-1",
      kind: "video",
      size: 1024,
      preview_url: "/api/artifacts/artifact-1",
    }],
    artifact_review: {
      feedback: "继续调整动作衔接",
      decision: {
        action: "adjust",
        feedback: "继续调整动作衔接",
        task_ids: ["task-1"],
      },
    },
    approval: {
      document_title: "成品重跑恢复测试",
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
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, [{
      run_id: runId,
      display_text: "成品重跑恢复测试",
      status: "已完成",
      rerunnable: true,
    }]);
    if (url === `/api/runs/${runId}`) return jsonResponse(200, view);
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  const switcher = app.getNode("current-run-switcher");
  switcher.value = runId;
  await switcher.dispatch("change");
  await settle();

  const checkbox = app.getNode("artifact-list").querySelectorAll("input")[0];
  assert.equal(checkbox.checked, true);
  assert.equal(
    app.getNode("artifact-review-feedback").value,
    "继续调整动作衔接",
  );
  assert.equal(app.getNode("adjust-artifacts-button").disabled, false);
});


test("completed run keeps the shared result table link visible", async () => {
  const deliveredRunId = "run-delivered";
  const pendingRunId = "run-pending";
  const resultTableUrl = "https://tenant.feishu.cn/base/result-table";
  const approval = {
    document_title: "\u7ed3\u679c\u8868\u94fe\u63a5\u6d4b\u8bd5",
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
  const deliveredView = {
    run_id: deliveredRunId,
    thread_id: "thread-delivered",
    source_url: "https://example.invalid/delivered",
    status: "succeeded",
    events: [],
    privacy: {},
    result_table_url: resultTableUrl,
    delivery: {
      status: "succeeded",
      target_type: "production_result_record",
      result_table_url: resultTableUrl,
    },
    artifacts: [{
      artifact_id: "artifact-1",
      task_id: "task-1",
      kind: "video",
      size: 1024,
      preview_url: "/api/artifacts/artifact-1",
    }],
    approval,
  };
  const pendingView = {
    run_id: pendingRunId,
    thread_id: "thread-pending",
    source_url: "https://example.invalid/pending",
    status: "succeeded",
    events: [],
    privacy: {},
    delivery: null,
    artifacts: [],
    approval,
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/recent-runs") {
      return jsonResponse(200, [
        {
          run_id: deliveredRunId,
          display_text: "\u5df2\u5bfc\u51fa\u4efb\u52a1",
          status: "\u5df2\u5b8c\u6210",
          rerunnable: true,
        },
        {
          run_id: pendingRunId,
          display_text: "\u672a\u5bfc\u51fa\u4efb\u52a1",
          status: "\u5df2\u5b8c\u6210",
          rerunnable: true,
        },
      ]);
    }
    if (url === `/api/runs/${deliveredRunId}`) {
      return jsonResponse(200, deliveredView);
    }
    if (url === `/api/runs/${pendingRunId}`) {
      return jsonResponse(200, pendingView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  const switcher = app.getNode("current-run-switcher");
  switcher.value = deliveredRunId;
  await switcher.dispatch("change");
  await settle();

  const link = app.getNode("artifact-result-table-link");
  assert.equal(link.href, resultTableUrl);
  assert.equal(link.hidden, false);
  assert.equal(app.getNode("retry-delivery-button").hidden, true);

  switcher.value = pendingRunId;
  await switcher.dispatch("change");
  await settle();

  assert.equal(link.hidden, true);
});


test("artifact rerun goes back to approval instead of re-running in place", async () => {
  // 2026-09-24 用户口径：「我的返工要求重跑选中任务应该是优化我的提示词然后让我
  // 确认啊」——以前走 `/api/runs/{id}/artifact-review`（**原地**重跑），提示词改完
  // 立刻开始生成，用户没有确认机会。现在与「重新运行」同一条逻辑：新建一条运行，
  // 复用已批准计划 + 融合返工要求，**停在待审批**等用户点「批准并开始生成」。
  const sourceRunId = "run-artifact-source";
  const newRunId = "run-artifact-rerun";
  const artifact = {
    artifact_id: "artifact-1",
    task_id: "task-1",
    kind: "video",
    size: 1024,
    preview_url: "/api/artifacts/artifact-1",
    shot_index: 1,
    shot_total: 2,
    slot_index: 1,
    slot_total: 1,
    task_title: "第一个镜头",
  };
  const approval = {
    document_title: "成品重跑测试",
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
  const sourceView = {
    run_id: sourceRunId,
    thread_id: "thread-artifact-source",
    source_url: "https://example.invalid/artifact-rerun",
    status: "waiting_review",
    events: [],
    privacy: {},
    delivery: null,
    artifacts: [artifact],
    approval,
  };
  const rerunView = {
    ...sourceView,
    run_id: newRunId,
    thread_id: "thread-artifact-rerun",
    status: "waiting_approval",
    artifacts: [],
  };
  let currentRunId = sourceRunId;
  let rerunRequests = 0;
  let artifactReviewRequests = 0;
  let rerunBody = null;
  const app = await loadApp(async (url, options = {}) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{
        run_id: currentRunId,
        display_text: "成品重跑测试",
        status: "待确认",
      }]);
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, [{
      run_id: currentRunId,
      display_text: "成品重跑测试",
      status: "待确认",
      rerunnable: false,
    }]);
    if (url === `/api/runs/${sourceRunId}`) return jsonResponse(200, sourceView);
    if (url === `/api/runs/${newRunId}`) return jsonResponse(200, rerunView);
    if (url === `/api/bitable/runs/${sourceRunId}/rerun-selected`) {
      rerunRequests += 1;
      rerunBody = JSON.parse(options.body);
      currentRunId = newRunId;
      return jsonResponse(202, { run_id: newRunId });
    }
    if (url === `/api/runs/${sourceRunId}/artifact-review`) {
      artifactReviewRequests += 1;
      return jsonResponse(202, { ok: true });
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url} ${options.method || "GET"}`);
  });

  const switcher = app.getNode("current-run-switcher");
  switcher.value = sourceRunId;
  await switcher.dispatch("change");
  await settle();

  // 卡片要标出「镜头 1/2」，否则多条成片长得一样、不知道该勾哪一条。
  const cardLabels = app
    .getNode("artifact-list")
    .querySelectorAll("span")
    .map((node) => node.textContent);
  assert.ok(
    cardLabels.some((text) => /镜头 1\/2/.test(text) && /第一个镜头/.test(text)),
    `成片卡片必须标出镜头号与标题，实际：${JSON.stringify(cardLabels)}`,
  );

  const checkbox = app.getNode("artifact-list").querySelectorAll("input")[0];
  checkbox.checked = true;
  await checkbox.dispatch("change");
  app.getNode("artifact-review-feedback").value = "继续调整动作衔接";
  await app.getNode("artifact-review-feedback").dispatch("input");
  assert.equal(app.getNode("adjust-artifacts-button").disabled, false);

  await app.getNode("adjust-artifacts-button").dispatch("click");
  await settle();

  assert.equal(rerunRequests, 1, "必须新建一条运行并进审批页");
  assert.equal(artifactReviewRequests, 0, "不能再原地重跑（会直接开始生成）");
  assert.deepEqual(rerunBody, {
    action: "adjust",
    feedback: "继续调整动作衔接",
    task_ids: ["task-1"],
  });
  // ③ 停在新运行的审批页：状态条换成「等待你审核」，批准按钮出现。
  assert.equal(app.getNode("status-badge").textContent, "等待你审核");
  assert.equal(app.getNode("approve-button").hidden, false);
  assert.equal(
    app.getNode("action-toast-text").textContent,
    "已按返工要求优化提示词，请审核后批准",
  );
});

test("completed runs keep generated artifacts visible without review controls", async () => {
  let artifactReviewRequests = 0;
  const completed = {
    run_id: "run-completed-artifacts",
    thread_id: "thread-completed-artifacts",
    source_url: "https://example.invalid/completed",
    status: "succeeded",
    events: [],
    privacy: {},
    delivery: {
      target_type: "production_result_record",
      result_table_url: "https://example.invalid/results",
    },
    artifacts: [{
      artifact_id: "image-1",
      kind: "image",
      size: 1024,
      preview_url: "/api/artifacts/image-1",
    }],
    approval: {
      document_title: "已完成素材任务",
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
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") return jsonResponse(200, { modes: { bitable: true } });
    if (url === "/api/bitable/active-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, [{
      run_id: completed.run_id,
      display_text: "已完成素材任务",
      status: "已完成",
      result_table_url: completed.delivery.result_table_url,
      rerunnable: true,
    }]);
    if (url === `/api/runs/${completed.run_id}`) return jsonResponse(200, completed);
    if (url === `/api/runs/${completed.run_id}/artifact-review`) {
      artifactReviewRequests += 1;
      return jsonResponse(200, { ok: true });
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  const switcher = app.getNode("current-run-switcher");
  switcher.value = completed.run_id;
  await switcher.dispatch("change");

  assert.equal(app.getNode("status-badge").textContent, "生成完成");
  assert.equal(app.getNode("artifact-review").hidden, false);
  assert.equal(app.getNode("artifact-list").children.length, 1);
  assert.equal(app.getNode("artifact-review-feedback-box").hidden, true);
  assert.equal(app.getNode("artifact-review-actions").hidden, true);
  assert.equal(app.getNode("confirm-artifacts-button").disabled, true);
  assert.equal(app.getNode("adjust-artifacts-button").disabled, true);
  assert.equal(app.getNode("artifact-review-feedback").disabled, true);
  assert.equal(app.getNode("delivery-target").hidden, false);

  // 即使旧页面或竞态残留了可点击控件，终态任务也不能再次导出或调整。
  await app.getNode("confirm-artifacts-button").dispatch("click");
  app.getNode("artifact-review-feedback").value = "再调整一次";
  await app.getNode("adjust-artifacts-button").dispatch("click");
  assert.equal(artifactReviewRequests, 0);
});
