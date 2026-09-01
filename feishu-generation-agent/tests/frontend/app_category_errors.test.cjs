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
    "utf8",
  );
  vm.runInNewContext(source, context);
  await settle();
  return { getNode };
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
  assert.equal(app.getNode("artifact-review-actions").hidden, true);
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
    "成片与结果",
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

  assert.equal(app.getNode("status-badge").textContent, "成片与结果");
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
