(() => {
  "use strict";

  const ReviewState = globalThis.ReviewState;
  if (!ReviewState) throw new Error("审批草稿状态模块加载失败");
  const BitableState = globalThis.BitableState;
  if (!BitableState) throw new Error("多维表格状态模块加载失败");
  const ReferenceUploadState = globalThis.ReferenceUploadState;
  if (!ReferenceUploadState) throw new Error("参考图片上传状态模块加载失败");
  const ReferenceMutationState = globalThis.ReferenceMutationState;
  if (!ReferenceMutationState) throw new Error("参考素材操作状态模块加载失败");
  const PlannerPromptState = globalThis.PlannerPromptState;
  const ApiPaths = globalThis.ApiPaths;

  const BITABLE_CATEGORY_STORAGE_KEY = "feishu-agent.active-category";

  function initialBitableCategory() {
    try {
      const value = globalThis.localStorage?.getItem(BITABLE_CATEGORY_STORAGE_KEY);
      return ["animation", "portrait", "image"].includes(value) ? value : "animation";
    } catch {
      return "animation";
    }
  }

  function persistBitableCategory(category) {
    try {
      globalThis.localStorage?.setItem(BITABLE_CATEGORY_STORAGE_KEY, category);
    } catch {
      // Local storage is optional; category selection still works in-session.
    }
  }

  const state = {
    runId: null,
    view: null,
    busy: false,
    runMode: null,
    pollTimer: null,
    modes: { bitable: false, legacy_delivery: false },
    providers: null,
    providerDefaults: null,
    providerPreferences: null,
    bitable: BitableState.createState(initialBitableCategory()),
    review: ReviewState.createReviewState(),
    referenceUploads: ReferenceUploadState.createState(),
    referenceMutations: ReferenceMutationState.createState(),
    plannerPrompt: PlannerPromptState?.createPlannerPromptState?.() || null,
    artifactPreviewSignature: null,
    artifactRetryTaskIds: new Set(),
    artifactReviewRunId: null,
  };
  // 正在看的那条运行上次见到的状态：变了才去刷新任务记录（同一状态反复轮询
  // 时什么都不做）。切换运行时会自动重置，不会误判成「状态变了」。
  let lastViewedRun = { runId: null, status: null };
  const byId = (id) => document.getElementById(id);
  const errorMessage = byId("error-message");
  const taskList = byId("task-list");
  const rejectButton = byId("reject-button");
  const cancelButton = byId("cancel-button");
  const approveButton = byId("approve-button");
  const retryDeliveryButton = byId("retry-delivery-button");
  const retryFailedAssetsButton = byId("retry-failed-assets-button");
  const retryFailedAssetsFeedback = byId("retry-failed-assets-feedback");
  const confirmArtifactsButton = byId("confirm-artifacts-button");
  const adjustArtifactsButton = byId("adjust-artifacts-button");
  // 失败/取消的版本没有成片可勾选，用它重跑这一版（重跑入口只在预览页）。
  const rerunArtifactsButton = byId("rerun-artifacts-button");
  const artifactReview = byId("artifact-review");
  const artifactList = byId("artifact-list");
  const artifactReviewMessage = byId("artifact-review-message");
  const artifactResultLink = byId("artifact-result-table-link");
  const artifactReviewFeedbackBox = byId("artifact-review-feedback-box");
  const artifactReviewActions = byId("artifact-review-actions");
  const artifactReviewFeedback = byId("artifact-review-feedback");
  // 模型审片（自动找穿帮）：用户 2026-09-18 要求「不要让我自己找问题」。
  const takeFindings = byId("take-findings");
  const takeFindingsScan = byId("take-findings-scan");
  const takeFindingsSummary = byId("take-findings-summary");
  const takeFindingsList = byId("take-findings-list");
  // 文档名旁边的一键复制（用户 2026-09-18 要求：需求文档名很长，要能直接复制）。
  const copyDocumentTitle = byId("copy-document-title");
  const copyDocumentTitleFeedback = byId("copy-document-title-feedback");
  // 底部操作栏的失败提示（用户 2026-09-21：「重新运行点了没反应」——409 的说明
  // 只写在页面顶部的 #error-message 里，用户在页脚点按钮时根本看不到）。
  const actionToast = byId("action-toast");
  const actionToastText = byId("action-toast-text");
  const actionToastClose = byId("action-toast-close");
  const adjustArtifactsHint = byId("adjust-artifacts-hint");
  // 审批页的原地提示词优化（勾选任务 + 一句要求 → AI 只重写这几条提示词）。
  const promptOptimizeBox = byId("prompt-optimize-box");
  const promptOptimizeFeedback = byId("prompt-optimize-feedback");
  const optimizePromptsButton = byId("optimize-prompts-button");
  const optimizePromptsHint = byId("optimize-prompts-hint");
  const auditIssuesMore = byId("audit-issues-more");
  const conflictBox = byId("review-conflict");
  const conflictText = byId("review-conflict-text");
  const permissionGuide = byId("permission-guide");
  const permissionGuideIntro = byId("permission-guide-intro");
  const discardButton = byId("discard-review-draft");
  const scanBitableButton = byId("scan-bitable-button");
  const directRunUrl = byId("direct-run-url");
  const directRunMode = byId("direct-run-mode");
  const directRunButton = byId("direct-run-button");
  const directRunFeedback = byId("direct-run-feedback");
  const animationCategoryTab = byId("animation-category-tab");
  const portraitCategoryTab = byId("portrait-category-tab");
  const imageCategoryTab = byId("image-category-tab");
  const categoryTabs = [animationCategoryTab, portraitCategoryTab, imageCategoryTab];
  const bitableTaskList = byId("bitable-task-list");
  const bitableStatus = byId("bitable-status");
  const recentRunList = byId("recent-run-list");
  const runHistorySummary = byId("run-history-summary");
  const currentRunSwitcher = byId("current-run-switcher");
  const trashButton = byId("trash-button");
  const trashModal = byId("trash-modal");
  const trashList = byId("trash-list");
  const trashClose = byId("trash-close");
  const trashSearch = byId("trash-search");
  const trashStatusFilter = byId("trash-status-filter");
  const trashResultSummary = byId("trash-result-summary");
  const trashPagination = byId("trash-pagination");
  const trashPrev = byId("trash-prev");
  const trashNext = byId("trash-next");
  const trashPageInfo = byId("trash-page-info");
  const rerunButton = byId("rerun-button");
  const pollingNote = byId("polling-note");
  const actionTitle = byId("action-title");
  const runGuidance = byId("run-guidance");
  const plannerPromptEntry = byId("planner-prompt-entry");
  const plannerPromptButton = byId("planner-prompt-button");
  const plannerPromptMode = byId("planner-prompt-mode");
  const plannerPromptModal = byId("planner-prompt-modal");
  const plannerPromptModalMode = byId("planner-prompt-modal-mode");
  const plannerPromptText = byId("planner-prompt-text");
  const plannerPromptSave = byId("planner-prompt-save");
  const plannerPromptReset = byId("planner-prompt-reset");
  const plannerPromptFeedback = byId("planner-prompt-feedback");
  const advancedSettingsEntry = byId("advanced-settings-entry");
  const advancedSettingsButton = byId("advanced-settings-button");
  const advancedSettingsModal = byId("advanced-settings-modal");
  const advancedVideoProvider = byId("advanced-video-provider");
  const advancedImageProvider = byId("advanced-image-provider");
  // 规划方式（高级设置里手动切）：text = 逐张看图描述后规划；multimodal = 原图/视频一起交给模型。
  const advancedPlanningPipeline = byId("advanced-planning-pipeline");
  // 分镜候选数 K：用户 2026-09-22「把 K 的选择放在高级设置里供用户选择」。
  const advancedStoryboardCandidates = byId("advanced-storyboard-candidates");
  const advancedSettingsSave = byId("advanced-settings-save");
  const advancedSettingsFeedback = byId("advanced-settings-feedback");
  const TERMINAL_RUN_STATUSES = new Set([
    "succeeded", "completed_with_errors", "failed", "cancelled", "delivery_failed",
  ]);
  const RERUNNABLE_RUN_STATUSES = new Set([
    "succeeded", "completed_with_errors", "failed", "cancelled",
  ]);
  // Active (non-terminal) run states that can be force-cancelled via
  // POST /api/runs/{run_id}/cancel. waiting_approval / waiting_review keep
  // their own decision buttons, so they are intentionally excluded here.
  const CANCELLABLE_RUN_STATUSES = new Set([
    "created", "running", "resuming", "waiting_provider", "delivering",
  ]);
  const EXPORTABLE_RUN_STATUSES = new Set([
    "succeeded", "completed_with_errors", "delivery_failed",
  ]);
  const ARTIFACT_REVIEWABLE_STATUSES = new Set([
    "waiting_review", "succeeded", "completed_with_errors", "delivery_failed",
  ]);
  const RUN_STATUS_UI = {
    planning: { label: "正在生成计划", tone: "running", action: "系统正在读取文档并拆解任务，请稍候。" },
    running: { label: "正在执行", tone: "running", action: "任务正在处理中，页面会自动更新进度。" },
    resuming: { label: "正在恢复", tone: "running", action: "正在恢复上次中断的任务，请稍候。" },
    waiting_approval: { label: "等待你审核", tone: "attention", action: "检查并修改下方计划，确认无误后批准生成。" },
    waiting_provider: { label: "正在生成内容", tone: "running", action: "生成服务正在工作，可以留在此页等待自动更新。" },
    waiting_review: { label: "成片与结果", tone: "attention", action: "查看生成素材，确认满意后导出到结果表。" },
    delivering: { label: "正在写入结果表", tone: "running", action: "内容已生成，正在回写飞书，请不要重复提交。" },
    succeeded: { label: "生成完成", tone: "success", action: "视频已生成完成，可点击下方「导出到结果表」回写飞书。" },
    completed_with_errors: { label: "部分完成", tone: "warning", action: "部分内容生成失败，可查看错误后重新运行。" },
    delivery_failed: { label: "写入结果表失败", tone: "danger", action: "生成内容已保留，请重新写入结果表，不需要重新生成。" },
    failed: { label: "执行失败", tone: "danger", action: "查看页面中的失败原因，修正后可重新运行。" },
    cancelled: { label: "已取消", tone: "muted", action: "本次任务已取消，可以重新运行或开始下一条。" },
    "待审批": { label: "等待你审核", tone: "attention", action: "检查并修改下方计划，确认无误后批准生成。" },
    "待确认成片": { label: "成片与结果", tone: "attention", action: "查看生成素材，确认满意后导出到结果表。" },
    "生成中": { label: "正在生成内容", tone: "running", action: "生成服务正在工作。" },
    "回写中": { label: "正在写入结果表", tone: "running", action: "内容已生成，正在回写飞书。" },
    "已完成": { label: "成片与结果", tone: "success", action: "素材已导出到结果表，仍可继续查看。" },
    "失败": { label: "执行失败", tone: "danger", action: "查看失败原因后可以重新运行。" },
    "回写失败": { label: "写入结果表失败", tone: "danger", action: "生成内容已保留，可以重新写入。" },
  };

  function statusUi(status) {
    return RUN_STATUS_UI[status] || {
      label: status || "尚未创建",
      tone: "muted",
      action: "从上方选择任务开始处理。",
    };
  }

  //: 早期持久化的失败记录只有通用占位文案（"The workflow node could not be
  //: completed"），按错误类别补一句人话。运行详情面板与历史滑条共用这一张表，
  //: 免得两处对同一条失败给出不同说法。
  const FAILURE_CATEGORY_FALLBACK = {
    permission_error: "飞书应用没有权限读取该文档或素材，请检查文档分享与应用权限。",
    document_error: "文档读取失败，请检查文档内容或格式。",
    configuration_error: "服务配置缺失，请联系管理员检查飞书应用配置。",
    transient_error: "服务暂时不可用，请稍后重试。",
    provider_terminal_error: "生成服务返回错误，无法继续生成。",
    delivery_error: "结果写入飞书失败。",
    validation_error: "任务参数校验失败。",
  };
  //: 运行时只在节点没跑完时写这句，等于没写；遇到它就继续往下找真实原因。
  const GENERIC_FAILURE_MESSAGE = "The workflow node could not be completed";

  //: 运行时里「被取消」的几种写法。多维表格任务状态把它们都压成「失败」，
  //: 所以只从**运行时状态**判断：用户要的是「这次是被取消的」，而不是
  //: 一句「未记录失败原因」（2026-09-24）。绝不据此编具体错误。
  const CANCELLED_RUN_STATUSES = new Set(["cancelled", "canceled"]);

  /** 没有具体错误时，按**运行时状态**给一句实话；认不出就说不知道。 */
  function failureFallbackByStatus(status) {
    const value = String(status || "").trim().toLowerCase();
    if (CANCELLED_RUN_STATUSES.has(value)) return "这次被取消了";
    return "";
  }

  //: 这些状态下任务正在跑，才值得显示「已完成/总数」。
  const GENERATING_STATUSES = new Set([
    "created", "running", "resuming", "waiting_provider", "planning",
  ]);

  /**
   * 状态文案 + 任务级进度（用户 2026-09-23：「3 个任务完成 2 个，界面还是只显示
   * 『生成中』，看不出在动」）。
   *
   * 进度由服务端在运行视图的 `progress` 里给出（执行阶段不在 checkpoint 里，
   * 后端是按 operations + artifacts 现算的）。拿不到就不加后缀。
   * 极简假 DOM 测试环境没有 addEventListener 之类的能力，这里只读普通对象字段。
   */
  function statusLabelWithProgress(view, label) {
    const progress = view && view.progress;
    if (!progress || !GENERATING_STATUSES.has(view.status)) return label;
    const total = Number(progress.total);
    const completed = Number(progress.completed);
    if (!Number.isFinite(total) || total <= 1) return label;
    if (!Number.isFinite(completed) || completed < 0) return label;
    return `${label} ${Math.min(completed, total)}/${total}`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function detailText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => item.msg || JSON.stringify(item)).join("；");
    }
    if (detail && typeof detail === "object") return JSON.stringify(detail);
    return "请求失败";
  }

  function showError(error) {
    errorMessage.textContent = error instanceof Error ? error.message : String(error);
    errorMessage.hidden = false;
  }

  function clearError() {
    errorMessage.textContent = "";
    errorMessage.hidden = true;
  }

  //: 用户点按钮失败时的提示：除了顶部那条 #error-message，再在页脚附近浮一条，
  //: 保证「点下去到底发生了什么」当场可见。只给用户主动触发的动作调用 ——
  //: 后台轮询失败不能每 5 秒弹一次。
  let actionToastTimer = null;

  function hideActionToast() {
    if (actionToastTimer) {
      globalThis.clearTimeout(actionToastTimer);
      actionToastTimer = null;
    }
    if (actionToast) actionToast.hidden = true;
  }

  function showActionToast(message, { tone = "error", timeout = 8000 } = {}) {
    if (!actionToast || !message) return;
    actionToastText.textContent = String(message);
    actionToast.classList.toggle("is-success", tone === "success");
    actionToast.classList.toggle("is-info", tone === "info");
    actionToast.hidden = false;
    if (actionToastTimer && typeof globalThis.clearTimeout === "function") {
      globalThis.clearTimeout(actionToastTimer);
    }
    // 极简假 DOM（前端单测的 vm 沙箱）里没有 setTimeout —— 能力探测一下，
    // 否则任何触发 toast 的新路径都会在测试里炸掉（2026-09-24 重跑改到审批页时踩到）。
    if (typeof globalThis.setTimeout !== "function") return;
    actionToastTimer = globalThis.setTimeout(() => {
      actionToastTimer = null;
      actionToast.hidden = true;
    }, timeout);
  }

  /** 用户主动触发的动作失败：顶部错误条 + 页脚提示条，两边都写。 */
  function reportActionFailure(error) {
    showError(error);
    showActionToast(error instanceof Error ? error.message : String(error));
  }

  if (actionToastClose) {
    actionToastClose.addEventListener("click", hideActionToast);
  }

  function agentUrl(path) {
    if (/^(?:https?:|blob:)/i.test(path)) return path;
    return ApiPaths
      ? ApiPaths.apiUrl(globalThis.location?.pathname || "/", path)
      : path;
  }

  function artifactExtension(artifact) {
    if (artifact.kind === "video") return ".mp4";
    if (artifact.mime_type === "image/jpeg") return ".jpg";
    if (artifact.mime_type === "image/webp") return ".webp";
    return ".png";
  }

  /**
   * 成片卡片上的「镜头 2/3 · 第 1/4 条」标注。
   *
   * 服务端按分镜顺序返回成片并带上 shot_index / slot_index（见
   * `GraphRuntime._artifact_shot_meta`）。缺字段（旧 run 的产物）就退回 task_id，
   * 至少让用户能把卡片和 id 对上，而不是只看到一串哈希。
   */
  function artifactShotLabel(artifact) {
    if (!artifact) return "";
    const parts = [];
    const shotIndex = Number(artifact.shot_index);
    const shotTotal = Number(artifact.shot_total);
    if (Number.isFinite(shotIndex) && shotIndex > 0) {
      parts.push(
        Number.isFinite(shotTotal) && shotTotal > 1
          ? `镜头 ${shotIndex}/${shotTotal}`
          : `镜头 ${shotIndex}`,
      );
    }
    const slotIndex = Number(artifact.slot_index);
    const slotTotal = Number(artifact.slot_total);
    if (Number.isFinite(slotTotal) && slotTotal > 1 && Number.isFinite(slotIndex)) {
      parts.push(`第 ${slotIndex}/${slotTotal} 条`);
    }
    const title = typeof artifact.task_title === "string"
      ? artifact.task_title.trim()
      : "";
    if (title) parts.push(title);
    if (!parts.length && artifact.task_id) parts.push(String(artifact.task_id));
    return parts.join(" · ");
  }

  async function downloadArtifact(artifact, button) {
    if (!artifact?.preview_url) return;
    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = "\u4e0b\u8f7d\u4e2d...";
    try {
      const response = await fetch(agentUrl(artifact.preview_url), {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      if (!blob.size) throw new Error("\u6587\u4ef6\u4e3a\u7a7a");
      const taskId = String(artifact.task_id || "artifact").replace(/[^A-Za-z0-9._-]+/g, "-");
      const artifactId = String(artifact.artifact_id || "artifact").slice(0, 12);
      const filename = `feishu-${taskId}-${artifactId}${artifactExtension(artifact)}`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      const detail = error instanceof Error && error.message ? `\uff08${error.message}\uff09` : "";
      showError(new Error(`\u6210\u7247\u4e0b\u8f7d\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5${detail}`));
    } finally {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }
  async function api(url, options = {}) {
    const response = await fetch(agentUrl(url), options);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      const detail = payload && typeof payload === "object" ? payload.detail : payload;
      const error = new Error(detailText(detail));
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function renderPlannerPrompt() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    const prompt = state.plannerPrompt;
    plannerPromptEntry.hidden = !prompt.entryVisible;
    plannerPromptButton.disabled = prompt.entryDisabled;
    plannerPromptMode.textContent = prompt.modeLabel;
    plannerPromptModal.hidden = !prompt.editorOpen;
    plannerPromptModalMode.textContent = prompt.modeLabel;
    if (plannerPromptText.value !== prompt.promptText) {
      plannerPromptText.value = prompt.promptText;
    }
    plannerPromptText.disabled = !prompt.editable || prompt.saving || prompt.resetting;
    plannerPromptSave.disabled = prompt.saveDisabled;
    plannerPromptReset.disabled = prompt.resetDisabled;
    plannerPromptFeedback.textContent = prompt.statusMessage;
    plannerPromptFeedback.className = `planner-prompt-feedback${prompt.statusType ? ` is-${prompt.statusType}` : ""}`;
  }

  function providerOptions(kind) {
    return (state.providers?.[kind] || []).map((provider) => ({
      value: provider.name,
      label: `${provider.label}${
        provider.configured === false
          ? "（未配置）"
          : provider.reachable === false
            ? " (unavailable)"
            : ""
      }`,
      local: provider.mode === "local",
      reachable: provider.reachable,
      configured: provider.configured,
      model: provider.model,
      capabilities: provider.capabilities || null,
    }));
  }

  function videoOptions() {
    return providerOptions("video").filter((option) => !option.local);
  }

  function videoOptionFor(task) {
    const options = videoOptions();
    const selected = task.video_provider || state.providerDefaults?.video_provider;
    return options.find((option) => option.value === selected) || options[0] || null;
  }

  function videoCapabilities(task) {
    const option = videoOptionFor(task);
    if (option?.capabilities) return option.capabilities;
    return {
      duration_min: 4,
      duration_max: 15,
      default_duration: 10,
      resolutions: ["720p", "1080p"],
      default_resolution: "720p",
      aspect_ratios: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"],
      default_aspect_ratio: "16:9",
      max_output_count: 4,
      supports_audio: true,
    };
  }

  function clampNumber(value, minimum, maximum) {
    const number = Number(value);
    if (!Number.isFinite(number)) return minimum;
    return Math.min(maximum, Math.max(minimum, Math.round(number)));
  }

  function ratioValue(value) {
    if (!value || value === "adaptive") return null;
    const parts = String(value).toLowerCase().replace("×", "x").replace("*", "x").split(/[:x]/);
    if (parts.length !== 2) return null;
    const width = Number(parts[0]);
    const height = Number(parts[1]);
    if (!width || !height) return null;
    return width / height;
  }

  function nearestSupportedRatio(value, ratios) {
    if (ratios.includes(value)) return value;
    const target = ratioValue(value);
    if (target === null) return ratios[0] || "16:9";
    const numeric = ratios.filter((item) => item !== "adaptive" && ratioValue(item) !== null);
    if (!numeric.length) return ratios[0] || "16:9";
    return numeric.reduce((best, item) => (
      Math.abs(ratioValue(item) - target) < Math.abs(ratioValue(best) - target)
        ? item
        : best
    ));
  }

  function normalizeVideoTaskPatch(task, option) {
    const capabilities = option?.capabilities;
    if (!capabilities) return { video_provider: option?.value || task.video_provider };
    const duration = clampNumber(
      task.duration ?? capabilities.default_duration,
      capabilities.duration_min,
      capabilities.duration_max,
    );
    const resolutions = capabilities.resolutions || [];
    const resolution = resolutions.includes(task.resolution)
      ? task.resolution
      : (capabilities.default_resolution || resolutions[0] || "720p");
    const aspectRatios = capabilities.aspect_ratios || [];
    const aspectRatio = nearestSupportedRatio(
      task.aspect_ratio,
      aspectRatios.length ? aspectRatios : ["16:9"],
    );
    const outputCount = clampNumber(
      task.output_count ?? 1,
      1,
      capabilities.max_output_count || 1,
    );
    return {
      video_provider: option.value,
      duration,
      resolution,
      aspect_ratio: aspectRatio,
      output_count: outputCount,
    };
  }

  function resolutionLabel(value) {
    return String(value || "").toLowerCase() === "4k" ? "4K" : value;
  }

  function boundedNumberInput(value, options, onInput) {
    const control = document.createElement("input");
    control.className = "task-control";
    control.type = "number";
    control.min = String(options.min);
    control.max = String(options.max);
    control.step = String(options.step || 1);
    control.value = String(value ?? options.min);
    control.addEventListener("change", () => {
      const next = clampNumber(control.value, options.min, options.max);
      control.value = String(next);
      onInput(next);
    });
    return control;
  }

  function renderAdvancedSettings() {
    if (!plannerPromptModal || !advancedVideoProvider || !advancedImageProvider) return;
    const preferences = state.providerPreferences;
    if (!preferences) return;
    const videoOptions = providerOptions("video");
    const imageOptions = providerOptions("image");
    if (advancedVideoProvider.childElementCount === 0) {
      videoOptions.forEach((option) => {
        const node = element("option", "", option.label);
        node.value = option.value;
        advancedVideoProvider.append(node);
      });
    }
    if (advancedImageProvider.childElementCount === 0) {
      imageOptions.forEach((option) => {
        const node = element("option", "", option.label);
        node.value = option.value;
        advancedImageProvider.append(node);
      });
    }
    advancedVideoProvider.value = preferences.video_provider;
    advancedImageProvider.value = preferences.image_provider;
    if (advancedPlanningPipeline) {
      advancedPlanningPipeline.value = preferences.planning_pipeline === "multimodal"
        ? "multimodal"
        : "text";
    }
    if (advancedStoryboardCandidates) {
      const raw = preferences.storyboard_candidates;
      const count = Number.isFinite(Number(raw)) ? Number(raw) : 0;
      advancedStoryboardCandidates.value = String(
        Math.max(0, Math.min(4, Math.trunc(count))),
      );
    }
    advancedSettingsFeedback.textContent = "";
    advancedSettingsFeedback.className = "planner-prompt-feedback";
  }

  async function loadProviderPreferences() {
    try {
      const payload = await api("/api/provider-preferences");
      state.providerPreferences = payload;
      state.providerDefaults = payload;
      renderAdvancedSettings();
      renderProviderStatus();
    } catch (error) {
      showError(error);
    }
  }

  function openAdvancedSettings() {
    if (!plannerPromptModal || !state.providerPreferences) return;
    renderAdvancedSettings();
    plannerPromptModal.hidden = false;
  }

  function closeAdvancedSettings() {
    if (plannerPromptModal) plannerPromptModal.hidden = true;
  }

  async function saveProviderPreferences() {
    if (!advancedVideoProvider || !advancedImageProvider || !advancedSettingsSave) return;
    advancedSettingsSave.disabled = true;
    advancedSettingsFeedback.textContent = "保存中...";
    advancedSettingsFeedback.className = "planner-prompt-feedback is-loading";
    try {
      const payload = await api("/api/provider-preferences", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          video_provider: advancedVideoProvider.value,
          image_provider: advancedImageProvider.value,
          planning_pipeline: advancedPlanningPipeline
            ? advancedPlanningPipeline.value
            : "text",
          storyboard_candidates: advancedStoryboardCandidates
            ? Number(advancedStoryboardCandidates.value)
            : 0,
        }),
      });
      state.providerPreferences = payload;
      state.providerDefaults = payload;
      advancedSettingsFeedback.textContent = "模型偏好已保存";
      advancedSettingsFeedback.className = "planner-prompt-feedback is-success";
      renderAdvancedSettings();
      renderProviderStatus();
      if (state.view) render(ReviewState.draftView(state.review));
      setTimeout(closeAdvancedSettings, 700);
    } catch (error) {
      advancedSettingsFeedback.textContent = error.message || "保存失败";
      advancedSettingsFeedback.className = "planner-prompt-feedback is-error";
      showError(error);
    } finally {
      advancedSettingsSave.disabled = false;
    }
  }

  async function loadPlannerPrompt() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    try {
      const payload = await api("/api/planner-prompt");
      state.plannerPrompt = PlannerPromptState.applyPlannerPromptResponse(
        state.plannerPrompt, payload,
      );
    } catch (error) {
      state.plannerPrompt = {
        ...state.plannerPrompt,
        statusMessage: error.message,
        statusType: "error",
      };
    }
    renderPlannerPrompt();
  }

  async function savePlannerPrompt() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    if (state.plannerPrompt.saving || state.plannerPrompt.resetting) return;
    try {
      state.plannerPrompt = PlannerPromptState.beginPromptSave(state.plannerPrompt);
      renderPlannerPrompt();
      const payload = await api("/api/planner-prompt", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt_text: state.plannerPrompt.promptText }),
      });
      state.plannerPrompt = PlannerPromptState.finishPromptSave(state.plannerPrompt, payload);
    } catch (error) {
      state.plannerPrompt = PlannerPromptState.failPromptSave(
        state.plannerPrompt, error.message,
      );
    }
    renderPlannerPrompt();
  }

  async function resetPlannerPrompt() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    if (state.plannerPrompt.saving || state.plannerPrompt.resetting) return;
    if (!globalThis.confirm("恢复 Prime 将删除当前个人版本，是否继续？")) return;
    try {
      state.plannerPrompt = PlannerPromptState.beginPromptReset(state.plannerPrompt);
      renderPlannerPrompt();
      const payload = await api("/api/planner-prompt", { method: "DELETE" });
      state.plannerPrompt = PlannerPromptState.finishPromptReset(state.plannerPrompt, payload);
    } catch (error) {
      state.plannerPrompt = PlannerPromptState.failPromptReset(
        state.plannerPrompt, error.message,
      );
    }
    renderPlannerPrompt();
  }

  function closePlannerPromptEditor() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    const next = PlannerPromptState.requestPromptEditorClose(state.plannerPrompt);
    if (next.closeConfirmationNeeded) {
      if (!globalThis.confirm("尚有未保存的提示词，确定放弃这些修改吗？")) {
        state.plannerPrompt = { ...next, closeConfirmationNeeded: false };
      } else {
        state.plannerPrompt = PlannerPromptState.discardPromptEditorChanges(next);
      }
    } else {
      state.plannerPrompt = next;
    }
    renderPlannerPrompt();
  }

  function setBusy(value) {
    state.busy = value;
    directRunButton.disabled = value;
    directRunUrl.disabled = value;
    directRunMode.disabled = value;
    currentRunSwitcher.disabled = value || (state.bitable.recentRuns || []).length === 0;
    scanBitableButton.disabled = value || !state.modes.bitable;
    categoryTabs.forEach((tab) => {
      tab.disabled = value || !state.modes.bitable;
    });
    bitableTaskList.querySelectorAll("button").forEach((control) => {
      control.disabled = value;
    });
    updateActionAvailability();
    renderRecentRuns();
  }

  //: 上一次请求是「我们自己 PATCH 保存」引起的 —— 下一次轮询要认领这个变化，
  //: 不能当成别人改了（否则会弹「服务端计划已更新」并重建任务列表，打断输入）。
  let selfSavedPending = false;
  //: 上次真正重绘时的服务端数据签名（没变就不重绘）。
  let lastRenderedSignature = null;
  //: 停在待审批/待审核时的轮询降频。
  let pollTicks = 0;
  const PARKED_POLL_EVERY = 5;
  const PARKED_STATUSES = new Set(["waiting_approval", "waiting_review"]);
  function isParkedStatus(status) {
    return PARKED_STATUSES.has(status);
  }
  //: 任务列表有挂起的重建（重建时用户正在输入 → 推迟到失焦）。
  let pendingTaskRefresh = false;

  function taskEditorFocused() {
    const active = document.activeElement;
    if (!active || !taskList.contains(active)) return false;
    return (
      active.tagName === "TEXTAREA" ||
      active.tagName === "INPUT" ||
      active.isContentEditable === true
    );
  }

  // 失焦后把挂起的重建补上：轮询每秒一次，正常情况下下一次就会生效；
  // 但如果服务端已经没变化（shouldRefreshTaskEditor 返回 false），就得靠这里补。
  taskList.addEventListener("focusout", () => {
    if (!pendingTaskRefresh) return;
    pendingTaskRefresh = false;
    globalThis.setTimeout(() => {
      if (taskEditorFocused()) {
        pendingTaskRefresh = true;
        return;
      }
      render(state.view, { refreshTasks: true });
    }, 0);
  });

  function stopPolling() {    if (state.pollTimer !== null) globalThis.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function startPolling() {
    stopPolling();
    if (!state.runId || TERMINAL_RUN_STATUSES.has(state.view?.status)) return;
    state.pollTimer = globalThis.setInterval(() => poll(false), 1000);
  }

  /**
   * 徽章文案：优先用任务记录列表刚轮询到的运行状态（新鲜），拿不到才退回
   * 扫描时带的 claim_status（可能已经旧了）。
   */
  function claimBadgeFor(task, claimedRunId) {
    if (!claimedRunId) return null;
    const run = (state.bitable.recentRuns || []).find(
      (item) => item.run_id === claimedRunId,
    );
    return BitableState.liveClaimBadge(
      task,
      claimedRunId,
      run ? statusUi(run.status) : null,
    );
  }

  function renderBitableTasks() {
    const categoryState = BitableState.activeCategoryState(state.bitable);
    const scan = categoryState.scan;
    const tasks = categoryState.tasks;
    const activeCategory = state.bitable.activeCategory;
    scanBitableButton.disabled = !state.modes.bitable || scan.phase === "loading";
    categoryTabs.forEach((tab) => {
      const isActive = tab.dataset.category === activeCategory;
      tab.classList.toggle("is-active", isActive);
      tab.setAttribute("aria-selected", String(isActive));
      tab.disabled = state.busy || !state.modes.bitable;
    });
    if (scan.phase === "loading") bitableStatus.textContent = "正在读取多维表格…";
    else if (scan.phase === "error") bitableStatus.textContent = scan.error;
    else if (
      state.bitable.claim.phase === "conflict"
      && state.bitable.claim.category === activeCategory
    ) {
      bitableStatus.textContent = state.bitable.claim.error;
    } else if (scan.phase === "ready") {
      const claimed = tasks.filter((task) => task.claimed_run_id);
      const waitingCount = claimed.filter(
        (task) => claimBadgeFor(task, task.claimed_run_id)?.tone === "attention",
      ).length;
      const claimableCount = tasks.length - claimed.length;
      if (waitingCount) {
        bitableStatus.textContent = `${
          claimableCount ? `${claimableCount} 条可处理，` : ""
        }${claimed.length} 条已领取，其中 ${waitingCount} 条等你处理。`;
      } else if (claimableCount && claimed.length) {
        bitableStatus.textContent = `${claimableCount} 条可处理，${claimed.length} 条已领取。`;
      } else if (claimed.length) {
        bitableStatus.textContent = `${claimed.length} 条任务已领取，可在当前列表查看进度。`;
      } else if (claimableCount) {
        bitableStatus.textContent = `发现 ${claimableCount} 条可处理任务，请手动选择一条。`;
      } else {
        bitableStatus.textContent = "当前没有需求附件可读且进度符合规则的可处理任务。";
      }
    }

    const nodes = tasks.map((task) => {
      const card = element("article", "bitable-task");
      const identity = element("div", "");
      identity.append(element("h3", "", task.display_text || task.record_id));
      if (Object.hasOwn(task, "progress")) {
        identity.append(
          element("p", "bitable-task-meta", `进度：${task.progress || "—"}`),
          element("p", "bitable-task-meta", `类型：${task.task_type || "未分类"}`),
          element("p", "bitable-task-meta", `制作人：${task.maker_name || "未填写"}`),
        );
        if (!task.deliverable && task.delivery_block_reason) {
          identity.append(element("p", "bitable-task-warning", task.delivery_block_reason));
        }
      } else {
        const executors = task.executor_names?.length
          ? task.executor_names.join("、")
          : task.executor_open_ids?.length
          ? task.executor_open_ids.join("、")
          : "未指定";
        identity.append(element("p", "bitable-task-meta", `执行人：${executors}`));
      }
      const claimedRunId = task.claimed_run_id || (
        state.bitable.claim.recordId === task.record_id
          ? state.bitable.claim.runId
          : null
      );
      const badge = claimBadgeFor(task, claimedRunId);
      if (badge) {
        const badgeNode = element(
          "span",
          "bitable-task-badge",
          `状态：${badge.label}`,
        );
        badgeNode.dataset.tone = badge.tone;
        identity.append(badgeNode);
      }
      const link = element("a", "", "查看需求来源");
      link.href = task.source_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      const claim = element(
        "button",
        claimedRunId ? "secondary" : "primary",
        claimedRunId ? "查看当前任务" : "开始分析",
      );
      claim.type = "button";
      if (claimedRunId) {
        claim.addEventListener("click", () => viewRecentRun(claimedRunId));
      } else {
        claim.disabled = state.busy || state.bitable.claim.phase === "loading" || task.deliverable === false;
        claim.addEventListener("click", () => claimBitableTask(task.record_id));
      }
      card.append(identity, link, claim);
      return card;
    });
    if (scan.phase === "ready" && nodes.length === 0) {
      nodes.push(element("p", "bitable-empty", "没有可领取任务。"));
    }
    bitableTaskList.replaceChildren(...nodes);
    bitableTaskList.dataset.taskSig = taskListSignature(tasks);
    renderRecentRuns();
  }

  function renderRecentRuns() {
    const runs = state.bitable.recentRuns || [];
    const signature = JSON.stringify({
      selected: state.runId,
      busy: state.busy,
      runs: runs.map((run) => [
        run.run_id,
        run.display_text,
        run.status,
        run.active,
        run.result_table_url,
        run.rerunnable,
        run.artifact_count,
      ]),
    });
    if (recentRunList.dataset.renderSig === signature) return;
    recentRunList.dataset.renderSig = signature;
    const activeCount = runs.filter((run) => run.active).length;
    // 一条记录一行：重跑不再多出一行。历次版本去成片预览的横向滑条里看/切。
    const rows = BitableState.latestRunsByRecord(runs);
    // 外面直接看得到这条任务一共出了多少条成片（历次尝试累加）。
    const producedByRecord = new Map();
    runs.forEach((run) => {
      const key = run.record_id || `run:${run.run_id}`;
      producedByRecord.set(
        key,
        (producedByRecord.get(key) || 0) + (Number(run.artifact_count) || 0),
      );
    });
    runHistorySummary.textContent = runs.length
      ? `${activeCount ? `${activeCount} 个进行中 · ` : ""}共 ${rows.length} 条`
      : "进行中与历史任务都在这里";
    const switchOptions = rows.map((run) => {
      const option = element("option", "", `${run.display_text || run.run_id} · ${statusUi(run.status).label}`);
      option.value = run.run_id;
      return option;
    });
    if (!switchOptions.length) {
      const emptyOption = element("option", "", "暂无任务记录");
      emptyOption.value = "";
      switchOptions.push(emptyOption);
    }
    currentRunSwitcher.replaceChildren(...switchOptions);
    currentRunSwitcher.value = runs.some((run) => run.run_id === state.runId)
      ? state.runId
      : "";
    currentRunSwitcher.disabled = state.busy || runs.length === 0;

    // 一条记录一行，但**只要这条记录上还有进行中的版本**，删除就必须收起来
    // —— 删除是按记录归档的，删掉会把在跑/在等人的那条一起带走。
    const busyRecords = new Set(
      runs
        .filter((run) => run.active || RUN_BUSY_STATUSES.has(run.status))
        .map((run) => run.record_id || `run:${run.run_id}`),
    );
    const nodes = rows.map((run) => {
      const selected = run.run_id === state.runId;
      const row = element("article", `recent-run${selected ? " is-current" : ""}`);
      row.dataset.runId = run.run_id;
      if (run.record_id) row.dataset.recordId = run.record_id;
      const details = element("div", "recent-run-details");
      const produced = producedByRecord.get(run.record_id || `run:${run.run_id}`) || 0;
      details.append(
        element("strong", "", run.display_text || run.run_id),
        element(
          "p",
          "bitable-task-meta",
          `${run.active ? "进行中" : "历史"} · ${statusUi(run.status).label}`
            + (run.updated_at ? ` · ${formatRecentTime(run.updated_at)}` : "")
            + (produced ? ` · 已成片 ${produced} 条` : ""),
        ),
      );
      row.append(
        details,
        runActionsFor(run, {
          selected,
          recordBusy: busyRecords.has(run.record_id || `run:${run.run_id}`),
        }),
      );
      return row;
    });
    if (!nodes.length) nodes.push(element("p", "bitable-empty", "暂无任务记录。"));
    recentRunList.replaceChildren(...nodes);
  }


  /** 一条运行的按钮组（任务记录里只留「查看 / 删除」；重跑在成片与结果页）。 */
  function runActionsFor(run, { selected = false, recordBusy = false } = {}) {
    const actions = element("div", "recent-run-actions");
    const view = element(
      "button",
      selected ? "quiet-button is-current" : "quiet-button",
      selected ? "当前查看" : "查看",
    );
    view.type = "button";
    view.disabled = state.busy || selected;
    view.addEventListener("click", () => viewRecentRun(run.run_id));
    actions.append(view);
    if (run.result_table_url) {
      const link = element("a", "", "结果表");
      link.href = run.result_table_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      actions.append(link);
    }
    if (canDeleteRun(run) && !recordBusy) {
      const remove = element("button", "danger", "删除");
      remove.type = "button";
      remove.disabled = state.busy;
      remove.addEventListener("click", () => archiveBitableRun(run.run_id));
      actions.append(remove);
    }
    return actions;
  }

  function formatRecentTime(value) {
    // 统一走 BitableState.parseServerTime：没有时区的裸串按 UTC 解析，
    // 否则会被当成本地时间，显示比真实时间早 8 小时（用户 2026-09-18：「时间还是不对」）。
    const parsed = BitableState.parseServerTime(value);
    if (!Number.isFinite(parsed)) return "";
    return new Date(parsed).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  async function loadRecentRuns({ silent = false } = {}) {
    if (!state.modes.bitable) return;
    try {
      const [activeRuns, recentRuns] = await Promise.all([
        api("/api/bitable/active-runs"),
        api("/api/bitable/recent-runs"),
      ]);
      const merged = [];
      const seen = new Set();
      // 「进行中」不能只看接口的 active：recent-runs 里也会有「生成中」的行
      // （见 RUN_BUSY_STATUSES 的注释），漏判会让整条记录退回到上一版。
      const busy = (run) => Boolean(run?.active) || RUN_BUSY_STATUSES.has(run?.status);
      [...(activeRuns || []).slice().reverse().map((run) => ({ ...run, active: true })),
        ...(recentRuns || []).map((run) => ({ ...run, active: busy(run) }))]
        .forEach((run) => {
          if (!run?.run_id || seen.has(run.run_id)) return;
          seen.add(run.run_id);
          merged.push(run);
        });
      state.bitable = BitableState.recentSucceeded(state.bitable, merged);
      renderRecentRuns();
      // 徽章文案取自这份新鲜状态，所以列表也要跟着重算（内容没变则不重建 DOM，
      // 免得用户正要点「开始分析」时按钮被换掉）。
      refreshTaskListIfChanged();
      // 有新状态在自行推进就起盯守，全都在等人操作就把盯守停掉。
      scheduleRunWatch();
    } catch (error) {
      // 后台定时刷新失败（服务重启中、飞书抖动）不该每 5 秒弹一次全局错误：
      // 用户没法对它做任何事，手动「刷新任务」仍会把错误如实报出来。
      if (!silent) showError(error);
    }
  }

  // 任务记录 / 任务列表徽章**只在状态会自己变的时候**盯，而且只在签名变化时
// 重画。以前是固定 5 秒刷一次整个面板 —— 而等待审批 / 等待成片审核是停在等人
// 操作上的，状态不会自己变，那种轮询纯属白跑（用户要的是「状态更新才刷新」）。
  const SELF_PROGRESSING_RUN_STATUSES = new Set([
    // TableTaskStatus 的中文值 —— tasks / recent-runs 两个接口下发的就是这一套。
    "处理中",
    "生成中",
    "回写中",
    // 运行时英文状态（运行详情等其它来源）。
    "created",
    "planning",
    "running",
    "resuming",
    "waiting_provider",
    "delivering",
  ]);
  //: 记录上还压着这些状态的任务时：
  //:   ① 任务记录那一行必须指向**它**，不能退回到上一版已完成的行；
  //:   ② 这条记录不能给「删除」—— 删掉会把正在跑 / 正在等人的任务一起归档。
  //:
  //: 用户 2026-09-22：「不能跳到正在生成的任务里」「只要有正在进行的任务，
  //: 删除键就不应该在」。根因：两个接口都**不下发 active 字段**（recent-runs 的
  //: SQL 本身就只取 active=0），所以「生成中」的那条在 recent-runs 里是 active=null。
  //: latestRunsByRecord 的「有成片的优先」于是把上一版已完成的行选成了代表 ——
  //: 下拉跳不到在跑的那条，而那一行是已完成状态，删除键照样出现。
  const RUN_BUSY_STATUSES = new Set([
    ...SELF_PROGRESSING_RUN_STATUSES,
    // 停在等人操作上，但这条记录仍被占着（active=1），同样不能删。
    "待处理",
    "待审批",
    "待确认成片",
  ]);
  const RUN_WATCH_INTERVAL_MS = 5000;
  //: 空闲时的心跳：全都在等人操作也**慢速**对一次。
  //:
  //: 2026-09-17 用户报「任务记录更新变得不实时了」：实测接口是新的、页面 DOM 停在
  //: 几十分钟前 —— 因为「只在自行推进时盯守」在**别的窗口/会话**开始的任务上失效
  //: （本页看不到任何推进中的状态，就永远不会去对），一切停下来后更是彻底不再对。
  //: 改成：有推进中的任务 → 5 秒；空闲 → 每 3 个 tick（15 秒）对一次，仍远低于
  //: 原来的固定 5 秒轮询。
  const RUN_IDLE_TICKS = 3;
  let runWatchTimer = null;

  function needsRunWatch() {
    return (state.bitable.recentRuns || []).some((run) =>
      SELF_PROGRESSING_RUN_STATUSES.has(run.status),
    );
  }

  /** 只有**不在跑**的运行才给删除按钮（用户 2026-09-17：生成中的任务不要有删除按钮）。
   *
   * 以前只看 `run.active`，而 active 只来自 active-runs 接口 —— 别的窗口/会话起的
   * 任务在本页会显示成非 active，于是「生成中」的行也挂着删除按钮，点了会把正在跑
   * 的任务删掉。改成按状态判断：自推进中的状态一律不给删。
   */
  function canDeleteRun(run) {
    return !run.active && !SELF_PROGRESSING_RUN_STATUSES.has(run.status);
  }

  function stopRunWatch() {
    if (runWatchTimer !== null) globalThis.clearInterval(runWatchTimer);
    runWatchTimer = null;
  }

  /** 有任务在自行推进就 5 秒盯一次；空闲降到 15 秒心跳（跨窗口也能发现新任务）。 */
  function scheduleRunWatch() {
    if (runWatchTimer !== null) return; // 已经在盯了，别重复起
    let idleTicks = 0;
    runWatchTimer = globalThis.setInterval(() => {
      if (document.hidden) return;
      if (!needsRunWatch()) {
        idleTicks += 1;
        if (idleTicks < RUN_IDLE_TICKS) return;
      }
      idleTicks = 0;
      loadRecentRuns({ silent: true });
    }, RUN_WATCH_INTERVAL_MS);
  }

  function startBitableRefresh() {
    // 从别的标签页切回来时立刻对一次，别让用户盯着旧数据。
    // 用可选调用是刻意的：这套前端的测试跑在自制的极简 DOM 上，不一定实现
    // addEventListener；浏览器里它始终存在，缺了也只是少一次「切回来即刷新」。
    document.addEventListener?.("visibilitychange", () => {
      if (!document.hidden) loadRecentRuns({ silent: true });
    });
  }

  /** 任务列表内容（含徽章文案）没变就不重建 DOM，避免点击落空与闪烁。 */
  function taskListSignature(tasks) {
    return JSON.stringify(
      (tasks || []).map((task) => [
        task.record_id,
        claimBadgeFor(task, task.claimed_run_id)?.label || "",
      ]),
    );
  }

  function refreshTaskListIfChanged() {
    const current = BitableState.activeCategoryState(state.bitable);
    const signature = taskListSignature(current.tasks);
    if (bitableTaskList.dataset.taskSig === signature) return;
    renderBitableTasks();
  }

  async function archiveBitableRun(runId) {
    if (state.busy) return;
    try {
      await api(
        `/api/bitable/runs/${encodeURIComponent(runId)}/archive`,
        { method: "POST" },
      );
      await loadRecentRuns();
    } catch (error) {
      showError(error);
    }
  }

  const trashState = {
    runs: [],
    query: "",
    status: "",
    page: 1,
    pageSize: 8,
  };

  function archivedStatusLabel(run) {
    return statusUi(run?.status).label;
  }

  function formatArchivedTime(value) {
    // 同 formatRecentTime：没有时区的裸串按 UTC 解析，避免早 8 小时。
    const parsed = BitableState.parseServerTime(value);
    if (!Number.isFinite(parsed)) return "";
    return new Date(parsed).toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function syncTrashStatusOptions() {
    const statuses = [...new Set(trashState.runs.map(archivedStatusLabel).filter(Boolean))]
      .sort((left, right) => left.localeCompare(right, "zh-CN"));
    const options = [element("option", "", "全部状态")];
    options[0].value = "";
    statuses.forEach((status) => {
      const option = element("option", "", status);
      option.value = status;
      options.push(option);
    });
    if (trashState.status && !statuses.includes(trashState.status)) {
      trashState.status = "";
    }
    trashStatusFilter.replaceChildren(...options);
    trashStatusFilter.value = trashState.status;
  }

  function filteredArchivedRuns() {
    const query = trashState.query.trim().toLocaleLowerCase("zh-CN");
    return trashState.runs.filter((run) => {
      if (trashState.status && archivedStatusLabel(run) !== trashState.status) return false;
      if (!query) return true;
      return [run.display_text, run.run_id]
        .filter(Boolean)
        .some((value) => String(value).toLocaleLowerCase("zh-CN").includes(query));
    });
  }

  function renderArchivedRuns() {
    const filtered = filteredArchivedRuns();
    const pageCount = Math.max(1, Math.ceil(filtered.length / trashState.pageSize));
    trashState.page = Math.min(Math.max(1, trashState.page), pageCount);
    const start = (trashState.page - 1) * trashState.pageSize;
    const pageRuns = filtered.slice(start, start + trashState.pageSize);
    const nodes = pageRuns.map((run) => {
      const row = element("article", "trash-item");
      const details = element("div", "");
      // 自动归档（任务记录只保留最近 10 条，更早的由系统移进回收站）**不是**用户
      // 自己删的：时间戳只能说「最后更新」——写「删除时间」会让用户以为是自己误删。
      const autoArchived = Boolean(run.auto_archived);
      const metadata = [`状态：${archivedStatusLabel(run)}`];
      const updatedAt = formatArchivedTime(run.updated_at);
      if (updatedAt) {
        metadata.push(`${autoArchived ? "最后更新" : "删除时间"}：${updatedAt}`);
      }
      details.append(
        element("strong", "", run.display_text || run.run_id),
        element("p", "bitable-task-meta", metadata.join(" · ")),
      );
      if (autoArchived) {
        details.append(element(
          "p",
          "trash-auto-note",
          "自动归档：任务记录只保留最近 10 条，更早的移到这里（可恢复）",
        ));
      }
      const actions = element("div", "trash-item-actions");
      const restore = element("button", "secondary", "恢复");
      restore.type = "button";
      restore.disabled = state.busy;
      restore.addEventListener("click", () => restoreArchivedRun(run.run_id));
      actions.append(restore);
      row.append(details, actions);
      return row;
    });
    if (!nodes.length) {
      nodes.push(element(
        "p",
        "trash-empty",
        trashState.runs.length ? "没有符合条件的任务。" : "回收站是空的。",
      ));
    }
    trashList.replaceChildren(...nodes);
    trashResultSummary.textContent = trashState.runs.length
      ? `找到 ${filtered.length} 条，共 ${trashState.runs.length} 条已删除任务`
      : "暂无已删除任务";
    trashPagination.hidden = filtered.length <= trashState.pageSize;
    trashPageInfo.textContent = `第 ${trashState.page} / ${pageCount} 页`;
    trashPrev.disabled = trashState.page <= 1;
    trashNext.disabled = trashState.page >= pageCount;
  }

  async function loadArchivedRuns() {
    try {
      const runs = await api("/api/bitable/archived-runs");
      trashState.runs = Array.isArray(runs) ? runs : [];
      syncTrashStatusOptions();
      renderArchivedRuns();
    } catch (error) {
      trashState.runs = [];
      syncTrashStatusOptions();
      renderArchivedRuns();
      showError(error);
    }
  }

  async function restoreArchivedRun(runId) {
    if (state.busy) return;
    try {
      await api(
        `/api/bitable/runs/${encodeURIComponent(runId)}/restore`,
        { method: "POST" },
      );
      await loadArchivedRuns();
      await loadRecentRuns();
    } catch (error) {
      showError(error);
    }
  }

  function openTrash() {
    trashState.query = "";
    trashState.status = "";
    trashState.page = 1;
    trashSearch.value = "";
    trashStatusFilter.value = "";
    trashModal.hidden = false;
    loadArchivedRuns();
  }

  function closeTrash() {
    trashModal.hidden = true;
  }

  async function scanBitableTasks() {
    if (!state.modes.bitable) return;
    const category = state.bitable.activeCategory;
    const categoryState = BitableState.activeCategoryState(state.bitable);
    if (categoryState.scan.phase === "loading") return;
    state.bitable = BitableState.scanStarted(state.bitable, category);
    renderBitableTasks();
    clearError();
    try {
      const tasks = await api(
        `/api/bitable/tasks?category=${encodeURIComponent(category)}`,
      );
      state.bitable = BitableState.scanSucceeded(state.bitable, category, tasks);
    } catch (error) {
      state.bitable = BitableState.scanFailed(state.bitable, category, error.message);
    } finally {
      renderBitableTasks();
    }
  }
  async function startDirectRun() {
    if (state.busy) return;
    const url = directRunUrl.value.trim();
    const mode = directRunMode.value;
    if (!url) {
      directRunFeedback.textContent = "请先填入飞书文档或 wiki 链接。";
      return;
    }
    setBusy(true);
    clearError();
    directRunFeedback.textContent = "正在读取文档并生成计划…";
    try {
      const created = await api("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_url: url, planning_mode: mode }),
      });
      state.runId = created.run_id;
      state.runMode = "direct";
      state.review = ReviewState.createReviewState();
      state.referenceUploads = ReferenceUploadState.createState();
      state.referenceMutations = ReferenceMutationState.createState();
      directRunFeedback.textContent = "计划生成中，下方可查看并修改。";
      await poll(true);
      startPolling();
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      directRunFeedback.textContent = error.message || "生成失败，请检查链接是否可访问。";
    } finally {
      setBusy(false);
    }
  }

  async function claimBitableTask(recordId) {
    if (state.busy) return;
    const category = state.bitable.activeCategory;
    state.bitable = BitableState.claimStarted(state.bitable, recordId, category);
    renderBitableTasks();
    setBusy(true);
    clearError();
    try {
      const created = await api(
        `/api/bitable/tasks/${encodeURIComponent(recordId)}/claim`
          + `?category=${encodeURIComponent(category)}`,
        { method: "POST" },
      );
      state.bitable = BitableState.claimSucceeded(state.bitable, created.run_id);
      state.runId = created.run_id;
      state.runMode = "bitable";
      state.review = ReviewState.createReviewState();
      state.referenceUploads = ReferenceUploadState.createState();
      state.referenceMutations = ReferenceMutationState.createState();
      await poll(true);
      startPolling();
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      state.bitable = error.status === 409
        ? BitableState.claimConflict(state.bitable, error.message)
        : BitableState.claimConflict(state.bitable, error.message);
    } finally {
      setBusy(false);
      renderBitableTasks();
    }
  }

  async function selectBitableCategory(category) {
    if (
      state.busy
      || !state.modes.bitable
      || category === state.bitable.activeCategory
    ) return;
    state.bitable = BitableState.selectCategory(state.bitable, category);
    persistBitableCategory(category);
    renderBitableTasks();
    if (BitableState.activeCategoryState(state.bitable).scan.phase === "idle") {
      await scanBitableTasks();
    }
  }

  async function configureModes() {
    try {
      const health = await api("/api/health");
      state.modes = health.modes || state.modes;
      state.providers = health.providers || null;
      state.providerDefaults = health.defaults || null;
      renderProviderStatus();
      await loadProviderPreferences();
    } catch (error) {
      showError(error);
    }
    scanBitableButton.disabled = !state.modes.bitable;
    categoryTabs.forEach((tab) => {
      tab.disabled = !state.modes.bitable;
    });
    if (!state.modes.bitable) {
      bitableStatus.textContent = "多维表格尚未配置，请先补全表格链接、数据表和视图。";
    }
    if (state.modes.bitable && !state.runId) {
      await loadRecentRuns();
      try {
        const activeRuns = await api("/api/bitable/active-runs");
        const latest = Array.isArray(activeRuns) ? activeRuns.at(-1) : null;
        if (latest?.run_id) {
          state.runId = latest.run_id;
          state.runMode = "bitable";
          state.review = ReviewState.createReviewState();
          await poll(true);
          startPolling();
          bitableStatus.textContent = `已恢复进行中任务：${latest.display_text || latest.run_id}`;
          document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
        }
      } catch (error) {
        showError(error);
      }
    }
    if (
      state.modes.bitable
      && BitableState.activeCategoryState(state.bitable).scan.phase === "idle"
    ) {
      await scanBitableTasks();
    }
  }

  //: 正在提交重跑/调整请求。
  //:
  //: **刻意不用 setBusy** —— 这些请求在后端要"看上一版成片 + 融合返工要求"，
  //: 可能等一分钟；setBusy 会把整个界面锁死（用户 2026-09-18：
  //: 「重跑的时候其它按钮都点不了」）。这里只禁用触发按钮本身。
  let rerunRequesting = false;

  function setRerunRequesting(value) {
    rerunRequesting = value;
    if (pollingNote) {
      pollingNote.textContent = value
        ? "正在提交重跑：模型要先看上一版成片再融合要求，可能要等一分钟…"
        : "";
    }
    updateActionAvailability();
  }

  function updateActionAvailability() {
    const canReview = state.view && state.view.status === "waiting_approval";
    const canReviewArtifacts = Boolean(
      state.view
      && ARTIFACT_REVIEWABLE_STATUSES.has(state.view.status)
      && !state.view.delivery
    );
    const canAdjustArtifacts = Boolean(
      canReviewArtifacts
      && state.artifactRetryTaskIds.size > 0
    );
    const status = state.view?.status;
    const statusInfo = statusUi(status);
    const conflict = ReviewState.conflictMessage(state.review);
    const canCancelRun = CANCELLABLE_RUN_STATUSES.has(status);
    rejectButton.disabled = state.busy || !canReview;
    cancelButton.disabled = state.busy || (!canReview && !canCancelRun);
    approveButton.disabled = state.busy || !ReviewState.canApprove(state.review);
    // 交付已成功时不再显示「导出到结果表」，避免重复导出看起来像“点了没反应”。
    const deliverySucceeded = Boolean(
      state.view?.delivery && state.view.delivery.status === "succeeded"
    );
    const canExportDelivery = EXPORTABLE_RUN_STATUSES.has(status) && !deliverySucceeded;
    retryDeliveryButton.disabled = state.busy || !canExportDelivery;
    const retryableAssetIssues = (state.view?.approval?.ingest_issue_records || [])
      .filter((record) => record.severity === "asset" && record.code === "media_download_failed");
    retryFailedAssetsButton.disabled = state.busy || !canReview || retryableAssetIssues.length === 0;
    retryFailedAssetsButton.hidden = !canReview || retryableAssetIssues.length === 0;
    confirmArtifactsButton.disabled = state.busy || !canReviewArtifacts;
    adjustArtifactsButton.disabled = state.busy || rerunRequesting || !canAdjustArtifacts;
    // 「重跑选中任务」的置灰原因必须写出来（用户 2026-09-21：「又点不动了」）。
    // 前置条件只有一个：勾选至少一条成片。**返工要求可以不填** —— 不填就是
    // 「提示词原样保留、进审批页重新审核」（用户 2026-09-24 的口径），不是错误。
    const adjustBlockedReason = !canReviewArtifacts
      ? ""
      : state.busy || rerunRequesting
        ? "正在处理中，请稍候…"
        : state.artifactRetryTaskIds.size === 0
          ? "请先在上方成片卡片里勾选「选择此条重跑」"
          : "";
    if (adjustArtifactsHint) {
      adjustArtifactsHint.textContent = adjustBlockedReason;
      adjustArtifactsHint.hidden = !adjustBlockedReason;
    }
    adjustArtifactsButton.title = adjustBlockedReason
      || "把勾选的成片退回重跑：填了返工要求会先融合改写提示词，然后停在审批页等你确认";
    artifactReviewFeedback.disabled = state.busy || !canReviewArtifacts;
    // 审批页的原地提示词优化：勾选任务 + 填要求两个前置条件，缺哪个都说出来
    //（和「重跑选中任务」同一套口径 —— 灰按钮不写原因用户只会说「点不动」）。
    const selectedForOptimize = ReviewState.selectedTaskIds(state.review);
    const canOptimizePrompts = Boolean(
      canReview
      && selectedForOptimize.length > 0
      && promptOptimizeFeedback.value.trim()
      && !state.busy
      && !rerunRequesting
    );
    if (promptOptimizeBox) {
      promptOptimizeBox.hidden = !canReview;
    }
    if (optimizePromptsButton) {
      optimizePromptsButton.disabled = !canOptimizePrompts;
      const optimizeBlockedReason = !canReview
        ? ""
        : state.busy || rerunRequesting
          ? "正在处理中，请稍候…"
          : selectedForOptimize.length === 0
            ? "请先在上方任务列表里勾选要优化的任务"
            : !promptOptimizeFeedback.value.trim()
              ? "请填写优化要求"
              : "";
      optimizePromptsButton.title = optimizeBlockedReason
        || "用 AI 按你的要求重写所选任务的提示词（不重新规划）";
      if (optimizePromptsHint) {
        optimizePromptsHint.textContent = optimizeBlockedReason;
        optimizePromptsHint.hidden = !optimizeBlockedReason;
      }
    }
    if (promptOptimizeFeedback) {
      promptOptimizeFeedback.disabled = state.busy || !canReview;
    }
    const terminal = TERMINAL_RUN_STATUSES.has(state.view?.status);
    rerunButton.disabled = state.busy || rerunRequesting
      || state.runMode !== "bitable"
      || !RERUNNABLE_RUN_STATUSES.has(state.view?.status);
    if (rerunArtifactsButton) {
      rerunArtifactsButton.disabled = rerunRequesting;
    }
    rerunButton.hidden = state.runMode !== "bitable" || !RERUNNABLE_RUN_STATUSES.has(status);
    retryDeliveryButton.hidden = !canExportDelivery;
    rejectButton.hidden = !canReview;
    approveButton.hidden = !canReview;
    cancelButton.hidden = !(canReview || canCancelRun);
    cancelButton.textContent = (canCancelRun && !canReview)
      ? "取消运行" : "取消本次任务";
    actionTitle.textContent = statusInfo.label;
    byId("reject-feedback").disabled = state.busy || !canReview;
    taskList.querySelectorAll("input, textarea, select, button").forEach((control) => {
      control.disabled = state.busy || !canReview || Boolean(conflict);
    });
    conflictText.textContent = conflict;
    conflictBox.hidden = !conflict;
    discardButton.disabled = state.busy || !conflict;
  }

  function formatDuration(value) {
    if (typeof value !== "number") return "—";
    if (value < 1000) return `${value} ms`;
    if (value >= 60_000) {
      const minutes = Math.floor(value / 60_000);
      const seconds = Math.floor((value % 60_000) / 1000);
      return `${minutes} 分 ${String(seconds).padStart(2, "0")} 秒`;
    }
    return `${(value / 1000).toFixed(1)} s`;
  }

  function renderEvents(events) {
    const list = byId("event-list");
    const source = events || [];
    const signature = JSON.stringify(source);
    if (list.dataset.renderSig === signature) return;
    list.dataset.renderSig = signature;
    const nodes = source.map((event) => {
      const item = element("li", "event-item");
      const meta = element("div", "event-meta");
      meta.append(
        element("strong", "", `${event.node || "workflow"} · ${event.status || ""}`),
        element("span", "", formatDuration(event.duration_ms)),
      );
      item.append(meta, element("p", "event-summary", event.summary || ""));
      return item;
    });
    list.replaceChildren(...nodes);
  }

  function descriptionFor(assetId) {
    const descriptions = state.view?.approval?.vision_descriptions || [];
    return descriptions.find((item) => item.asset_id === assetId) || null;
  }

  function assetFor(assetId) {
    const assets = state.view?.approval?.media_assets || [];
    return assets.find((item) => item.asset_id === assetId) || null;
  }

  function field(labelText, control, wide = false) {
    const wrapper = element("div", wide ? "field field-wide" : "field");
    wrapper.append(element("label", "", labelText), control);
    return wrapper;
  }

  function videoField(labelText, control, hintText = "") {
    const wrapper = element("div", "video-param-field", "");
    wrapper.append(
      element("label", "video-param-label", labelText),
      control,
      element("span", "video-param-hint", hintText),
    );
    return wrapper;
  }

  function textArea(value, onInput, rows = 3, className = "") {
    const control = document.createElement("textarea");
    control.rows = rows;
    control.className = className;
    control.value = value || "";
    control.addEventListener("input", () => onInput(control.value));
    return control;
  }

  function textInput(value, onInput, type = "text") {
    const control = document.createElement("input");
    control.type = type;
    control.value = value ?? "";
    control.addEventListener("input", () => onInput(control.value));
    return control;
  }

  function imageProviderOptions(task) {
    const options = providerOptions("image");
    const selected = task.image_provider || state.providerDefaults?.image_provider;
    if (selected && !options.some((option) => option.value === selected)) {
      options.push({ value: selected, label: `${selected} (unavailable)`, local: false });
    }
    return options;
  }

  // 画风随剧本变，做成按钮直接追加到提示词末尾，人工审核时一键切换。
  const STYLE_PRESETS = [
    ["厚涂风", "3D 卡通动画风格，厚涂手绘风格，无明显笔触，画面平滑过渡自然"],
    ["3D CG 风", "3D 偏真人 CG 游戏插图风格，强烈的色彩对比性和撞色的感觉"],
    ["迪士尼风", "3D 卡通迪士尼风格，角色比例圆润，色彩明亮通透"],
  ];

  function providerPicker(task) {
    const control = document.createElement("select");
    control.className = "task-control";
    const options = imageProviderOptions(task);
    const preferred = state.providerDefaults?.image_provider || options[0]?.value || "";
    options.forEach((option) => {
      const node = element("option", "", option.label);
      node.value = option.value;
      node.selected = (task.image_provider || preferred) === option.value;
      control.append(node);
    });
    control.addEventListener("change", () => {
      updateTask(task.task_id, { image_provider: control.value });
    });
    return control;
  }
  function videoProviderPicker(task) {
    const control = document.createElement("select");
    control.className = "task-control";
    const options = videoOptions();
    const defaultModel = state.providerDefaults?.video_provider;
    const selected = options.some((option) => option.value === task.video_provider)
      ? task.video_provider
      : (options.some((option) => option.value === defaultModel)
        ? defaultModel
        : options[0]?.value || "");
    options.forEach((option) => {
      const node = element("option", "", option.label);
      node.value = option.value;
      node.selected = selected === option.value;
      control.append(node);
    });
    control.disabled = options.length <= 1;
    control.addEventListener("change", () => {
      const option = options.find((item) => item.value === control.value);
      updateTask(task.task_id, normalizeVideoTaskPatch(task, option));
      render(state.view, { refreshTasks: true });
    });
    return control;
  }

  //: 模型审片结果（按 run 缓存，切回来不用重新看片 —— 每次看片都是一次真实模型调用）。
  const takeFindingsCache = new Map();
  //: 审片请求进行中（只用来禁用按钮，避免重复点）。
  let takeFindingsScanning = false;

  /** 成片预览里的「模型审片」：自动列疑似穿帮，可一键采纳成返工反馈。 */
  function renderTakeFindings(view) {
    if (!takeFindings) return;
    // 只要有 run 就显示这个区块 —— 以前没有成片时整块隐藏，用户根本找不到入口
    // （「让模型看一遍找问题点不了」）。没成片时按钮照样可点，会说明原因。
    takeFindings.hidden = false;
    const videos = (view.artifacts || []).filter(
      (artifact) => artifact.kind === "video",
    );
    // 只在自己正在看片时禁用 —— **不跟 state.busy 走**：setBusy(false) 之后不一定
    // 会再渲染（有"变了才重绘"的签名门），按钮会永久卡在禁用状态
    //（用户 2026-09-18：「让模型看一遍找问题还是点不了」）。
    if (takeFindingsScan) {
      takeFindingsScan.disabled = takeFindingsScanning === true;
    }
    const findings = takeFindingsCache.get(view.run_id);
    if (!findings) {
      takeFindingsSummary.textContent = videos.length
        ? "点右边按钮，让模型看一遍这一版成片，自动列出疑似穿帮"
          + "（穿模 / 悬浮 / 多余肢体 / 动作跳变 / 口型对不上 / 道具突变…）。"
        : "这一版还没有成片，生成完成后就能让模型审片找穿帮。";
      takeFindingsList.replaceChildren();
      return;
    }
    takeFindingsSummary.textContent = findings.available
      ? findings.summary || "（模型没给出画面描述）"
      : findings.reason || "看片失败";
    const problems = findings.problems || [];
    if (findings.available && problems.length === 0) {
      takeFindingsList.replaceChildren(
        element("li", "take-finding", "模型没发现明显穿帮。"),
      );
      return;
    }
    takeFindingsList.replaceChildren(
      ...problems.map((problem) => {
        const row = element("li", "take-finding");
        // 「违背要求」是用户最在意的一类（提示词写了但没做到），单独标出来。
        if (problem.kind === "违背要求") {
          row.classList.add("is-violation");
          row.append(element("span", "take-finding-kind", "违背要求"));
        }
        row.append(element("span", "take-finding-at", problem.at || "—"));
        row.append(element("span", "take-finding-issue", problem.issue));
        if (problem.why) {
          row.append(element("span", "take-finding-why", problem.why));
        }
        const adopt = element("button", "take-finding-adopt", "采纳");
        adopt.type = "button";
        adopt.addEventListener("click", () => {
          if (!artifactReviewFeedback) return;
          const line = problem.kind === "违背要求"
            ? `没按要求：${problem.issue}`
            : `穿帮：${problem.at ? `${problem.at} ` : ""}${problem.issue}`;
          const current = artifactReviewFeedback.value.trim();
          artifactReviewFeedback.value = current ? `${current}\n${line}` : line;
          artifactReviewFeedback.focus();
        });
        row.append(adopt);
        return row;
      }),
    );
  }

  async function scanTakeFindings() {
    if (!state.runId || takeFindingsScanning) return;
    takeFindingsScanning = true;
    if (takeFindingsScan) takeFindingsScan.disabled = true;
    if (takeFindingsSummary) {
      takeFindingsSummary.textContent = "模型正在看片…（一般十几秒）";
    }
    try {
      const findings = await api(`/api/runs/${state.runId}/take-findings`);
      takeFindingsCache.set(state.runId, findings);
    } catch (error) {
      if (takeFindingsSummary) {
        takeFindingsSummary.textContent = error.message || "看片失败";
      }
    } finally {
      takeFindingsScanning = false;
      if (takeFindingsScan) takeFindingsScan.disabled = false;
      if (state.view) renderTakeFindings(state.view);
    }
  }

  if (takeFindingsScan) {
    takeFindingsScan.addEventListener("click", () => {
      scanTakeFindings();
    });
  }

  /** 复制当前文档名（局域网 http 下没有 clipboard API，退回 execCommand）。 */
  async function copyCurrentDocumentTitle() {
    const title = String(
      state.view?.approval?.document_title
      || byId("document-title")?.textContent
      || "",
    ).trim();
    if (!title) return;
    let ok = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(title);
        ok = true;
      }
    } catch (error) {
      ok = false;
    }
    if (!ok) {
      try {
        const helper = document.createElement("textarea");
        helper.value = title;
        helper.setAttribute("readonly", "");
        helper.style.position = "fixed";
        helper.style.opacity = "0";
        document.body.append(helper);
        helper.select();
        ok = document.execCommand("copy");
        helper.remove();
      } catch (error) {
        ok = false;
      }
    }
    if (!copyDocumentTitle) return;
    if (!ok) {
      // 两条路都失败（浏览器禁了剪贴板）：直接把标题选中，用户 Ctrl+C 就行。
      const titleNode = byId("document-title");
      if (titleNode && globalThis.getSelection) {
        const range = document.createRange();
        range.selectNodeContents(titleNode);
        const selection = globalThis.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
    }
    // 图标按钮不能改 textContent（会抹掉 SVG），反馈走 class + title/aria-label，
    // 外加旁边一条文字回执 —— 只变色用户会以为「点了没反应」。
    const feedback = ok ? "已复制" : "已选中，按 Ctrl+C 复制";
    copyDocumentTitle.title = feedback;
    copyDocumentTitle.setAttribute("aria-label", feedback);
    copyDocumentTitle.classList.toggle("is-copied", ok);
    copyDocumentTitle.classList.toggle("is-fallback", !ok);
    if (copyDocumentTitleFeedback) {
      copyDocumentTitleFeedback.textContent = feedback;
      copyDocumentTitleFeedback.classList.toggle("is-fallback", !ok);
    }
    globalThis.setTimeout(() => {
      if (copyDocumentTitle) {
        copyDocumentTitle.title = "复制文档名";
        copyDocumentTitle.setAttribute("aria-label", "复制文档名");
        copyDocumentTitle.classList.remove("is-copied", "is-fallback");
      }
      if (copyDocumentTitleFeedback) {
        copyDocumentTitleFeedback.textContent = "";
        copyDocumentTitleFeedback.classList.remove("is-fallback", "is-error");
      }
    }, 1800);
  }

  if (copyDocumentTitle) {
    copyDocumentTitle.addEventListener("click", () => {
      copyCurrentDocumentTitle();
    });
  }

  function renderProviderStatus() {
    const bar = byId("provider-status-bar");
    if (!bar) return;
    const providers = state.providers;
    const defaults = state.providerDefaults;
    if (!providers || !defaults) {
      bar.hidden = true;
      return;
    }
    const video = (providers.video || []).find((p) => p.name === defaults.video_provider);
    const videoText = video && video.mode !== "local"
      ? `视频：${video.label || "Seedance 2.5"}（付费）`
      : (video?.reachable === false ? "视频：本地 MiniMax H3（离线）" : "视频：本地 MiniMax H3（免费）");
    const imageText = defaults.image_provider === "aiport"
      ? "图片：本地 Qwen（免费）"
      : "图片：云模型（付费）";
    const realPersonText = defaults.video_provider === "aiport"
      ? "真人类视频：本地 MiniMax H3"
      : "真人类视频：火山方舟真人模型";
    bar.textContent = `默认生成来源 · ${videoText} · ${imageText} · ${realPersonText}`;
    bar.hidden = false;
  }

  // 与后端 domain/plan.py 的 IMAGE_ASPECT_RATIOS 保持一致：
  // 生成模型（seedream/banana/gpt-image2）只接受这些离散比例，
  // 文档里的 1700*2500 是交付尺寸，不是比例参数。
  const IMAGE_ASPECT_RATIOS = ["16:9", "9:16", "2:3", "3:2", "1:1", "4:3", "3:4", "21:9", "9:21"];

  function ratioPicker(task) {
    const control = document.createElement("select");
    control.className = "task-control";
    const capabilities = task.task_type === "image_to_video"
      ? videoCapabilities(task)
      : null;
    const ratios = capabilities?.aspect_ratios?.length
      ? capabilities.aspect_ratios
      : IMAGE_ASPECT_RATIOS;
    ratios.forEach((ratio) => {
      const option = element("option", "", ratio === "adaptive" ? "自适应" : ratio);
      option.value = ratio;
      option.selected = task.aspect_ratio === ratio;
      control.append(option);
    });
    if (!ratios.includes(task.aspect_ratio)) {
      const option = element("option", "", task.aspect_ratio || "未选择");
      option.value = task.aspect_ratio || "";
      option.selected = true;
      control.append(option);
    }
    control.addEventListener("change", () => {
      updateTask(task.task_id, { aspect_ratio: control.value });
    });
    return control;
  }

  function resolutionPicker(task) {
    const capabilities = videoCapabilities(task);
    const control = document.createElement("select");
    control.className = "task-control";
    const resolutions = capabilities?.resolutions?.length
      ? capabilities.resolutions
      : ["720p", "1080p"];
    resolutions.forEach((resolution) => {
      const option = element("option", "", resolutionLabel(resolution));
      option.value = resolution;
      option.selected = task.resolution === resolution;
      control.append(option);
    });
    if (!resolutions.includes(task.resolution)) {
      const option = element("option", "", task.resolution || "未选择");
      option.value = task.resolution || "";
      option.selected = true;
      control.append(option);
    }
    control.title = `可选：${resolutions.map(resolutionLabel).join("、")}`;
    control.addEventListener("change", () => {
      updateTask(task.task_id, { resolution: control.value });
    });
    return control;
  }

  function durationInput(task) {
    const capabilities = videoCapabilities(task);
    return boundedNumberInput(
      task.duration ?? capabilities.default_duration,
      {
        min: capabilities.duration_min,
        max: capabilities.duration_max,
        hint: `${capabilities.duration_min}-${capabilities.duration_max} 秒`,
      },
      (value) => updateTask(task.task_id, { duration: value }),
    );
  }

  function outputCountInput(task) {
    const capabilities = videoCapabilities(task);
    const maximum = capabilities.max_output_count || 1;
    return boundedNumberInput(
      task.output_count ?? 1,
      {
        min: 1,
        max: maximum,
        hint: `1-${maximum} 条候选`,
      },
      (value) => updateTask(task.task_id, { output_count: value }),
    );
  }
  function deliveryCropToggle(task) {
    const wrapper = element("label", "task-crop-toggle", "");
    const control = document.createElement("input");
    control.type = "checkbox";
    control.checked = Boolean(task.delivery_crop);
    const variants = task.size_variants || [];
    const first = variants[0] || "";
    const match = String(first).match(/^(\d+)[x×*](\d+)$/i);
    let ratioLabel = "交付尺寸";
    if (match) {
      const a = Number(match[1]);
      const b = Number(match[2]);
      const gcd = (x, y) => (y ? gcd(y, x % y) : x);
      const divisor = gcd(a, b) || 1;
      ratioLabel = `${a / divisor}:${b / divisor}`;
    }
    const caption = document.createElement("span");
    caption.textContent = `裁剪为 ${ratioLabel} 比例（居中，保持中心点不变）`;
    control.disabled = !variants.length;
    control.title = variants.length ? "" : "任务没有输出尺寸，无法裁剪";
    control.addEventListener("change", () => {
      updateTask(task.task_id, { delivery_crop: control.checked });
    });
    wrapper.append(control, caption);
    return wrapper;
  }

  function stylePresets(task) {
    const wrapper = element("div", "task-style-presets");
    STYLE_PRESETS.forEach(([label, fragment]) => {
      const button = element("button", "task-style-preset", label);
      button.type = "button";
      button.title = fragment;
      button.addEventListener("click", () => {
        const current = (task.prompt || "").trim();
        if (current.includes(fragment)) return;
        updateTask(task.task_id, {
          prompt: current ? `${current}，${fragment}` : fragment,
        });
        render(state.view, { refreshTasks: true });
      });
      wrapper.append(button);
    });
    return wrapper;
  }

  /**
   * 图片放大预览：任意图片**双击**即全屏看细节。
   *
   * 用户 2026-09-23：「参考图无法看到放大预览，没办法确认细节」—— 分镜候选图只有
   * 6.5rem 见方，看不出水印/细节。这里用**事件委托**挂在 document 上，所以分镜候选、
   * 文档参考图、成片缩略图全部自动覆盖，不用逐个渲染点去接。
   */
  function openImageLightbox(url) {
    const overlay = byId("image-lightbox");
    const image = byId("image-lightbox-img");
    if (!overlay || !image || !url) return;
    image.src = url;
    overlay.hidden = false;
  }

  function closeImageLightbox() {
    const overlay = byId("image-lightbox");
    const image = byId("image-lightbox-img");
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    if (image) image.removeAttribute("src");
  }

  // 前端测试用的是极简假 DOM（没有 addEventListener / closest），所以这里全部做能力
  // 探测 —— 挂不上就没有放大预览，绝不能因此让整个 app.js 抛异常（CLAUDE.local.md
  // 记过同类坑：假 DOM 缺实现导致一批用例一起挂）。
  if (typeof document.addEventListener === "function") {
    document.addEventListener("dblclick", (event) => {
      const target = event && event.target;
      const image = target && typeof target.closest === "function"
        ? target.closest("img")
        : null;
      if (image && image.src) openImageLightbox(image.src);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeImageLightbox();
    });
  }
  const lightboxOverlay = byId("image-lightbox");
  if (lightboxOverlay && typeof lightboxOverlay.addEventListener === "function") {
    lightboxOverlay.addEventListener("click", closeImageLightbox);
  }

  /**
   * ① 参考素材的**单击**预览入口（用户 2026-10-10：「新增参考素材也能点击预览的功能」）。
   *
   * 页面里原本只有「任意 <img> 双击放大」的事件委托 —— 用户不知道要双击，等于没有入口。
   * 这里给缩略图补上明确的单击入口，**复用同一个 lightbox**（不另造一套）：
   *   - `attachSingleClickPreview(node, url)`：把单击挂到缩略图**本身**。只用在
   *     「缩略图本身没有任何点击语义」的地方 —— 参考素材行（那行的操作是替换/删除/
   *     改序号，都在旁边的按钮上）、排除素材与未使用素材行、成片卡片。
   *   - `previewButton(url, label)`：返回一个独立的「预览」小按钮。用在**点缩略图
   *     已经有别的含义**的地方 —— 分镜候选的缩略图点一下是「选中这张关键帧」，
   *     所以那里叠加一个不抢事件的按钮，而不是改缩略图的单击语义。
   *
   * 能力探测：前端测试跑在极简假 DOM 上（没有 addEventListener / closest），挂不上就
   * 静默跳过，绝不能让整个 app.js 抛异常（CLAUDE.local.md 记过同类坑）。
   */
  function attachSingleClickPreview(node, url) {
    if (!node || !url || typeof node.addEventListener !== "function") return node;
    if (node.classList && typeof node.classList.add === "function") {
      node.classList.add("previewable");
    }
    if (typeof node.setAttribute === "function") {
      node.setAttribute("role", "button");
      node.setAttribute("tabindex", "0");
      node.setAttribute("aria-label", `${node.alt || "图片"}（单击放大预览）`);
    }
    if (!node.title) node.title = "单击放大预览（Esc 或点空白处关闭）";
    node.addEventListener("click", (event) => {
      // 缩略图可能落在 label/可拖拽容器里：不让单击顺带触发别的语义。
      if (event && typeof event.preventDefault === "function") event.preventDefault();
      if (event && typeof event.stopPropagation === "function") event.stopPropagation();
      openImageLightbox(url);
    });
    node.addEventListener("keydown", (event) => {
      if (!event || (event.key !== "Enter" && event.key !== " ")) return;
      if (typeof event.preventDefault === "function") event.preventDefault();
      openImageLightbox(url);
    });
    return node;
  }

  function previewButton(url, label) {
    const button = element("button", "preview-button", "预览");
    button.type = "button";
    button.title = "单击放大预览（Esc 或点空白处关闭）";
    if (typeof button.setAttribute === "function") {
      button.setAttribute("aria-label", `预览${label || ""}大图`);
    }
    if (url && typeof button.addEventListener === "function") {
      button.addEventListener("click", (event) => {
        // 分镜候选的外层就是「点一下 = 选中这张」的按钮：务必别冒泡过去。
        if (event && typeof event.stopPropagation === "function") event.stopPropagation();
        if (event && typeof event.preventDefault === "function") event.preventDefault();
        openImageLightbox(url);
      });
    } else {
      button.disabled = true;
    }
    return button;
  }

  function updateTask(taskId, patch) {
    try {
      state.review = ReviewState.patchTask(state.review, taskId, patch);
      state.view = ReviewState.draftView(state.review);
      updateActionAvailability();
      scheduleHotPatch(taskId, patch);
    } catch (error) {
      showError(error);
    }
  }

  const hotPatchTimers = new Map();

  function scheduleHotPatch(taskId, patch) {
    // 热修改：编辑即时持久化到服务端草稿，不再停留在浏览器本地。
    // 文本输入按 600ms 防抖；空提示词/非法数字是输入过程的中间态，跳过发送。
    if (Object.prototype.hasOwnProperty.call(patch, "prompt") && !String(patch.prompt).trim()) return;
    for (const field of ["output_count", "duration"]) {
      if (Object.prototype.hasOwnProperty.call(patch, field) && !Number.isFinite(patch[field])) return;
    }
    if (
      Object.prototype.hasOwnProperty.call(patch, "reference_images")
      || Object.prototype.hasOwnProperty.call(patch, "reference_mode")
    ) {
      const task = currentTask(taskId);
      if (!task) return;
      scheduleHotReferences(task);
      return;
    }
    const key = `${taskId}:${Object.keys(patch).join(",")}`;
    clearTimeout(hotPatchTimers.get(key));
    hotPatchTimers.set(key, setTimeout(() => {
      hotPatchTimers.delete(key);
      api(`/api/runs/${state.runId}/tasks/${encodeURIComponent(taskId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ patch }),
      }).then(() => {
        // 这是我们自己刚保存的：告诉下一次轮询"这个变化是自己造成的"，
        // 别当成别人改了而弹冲突/重建任务列表。
        selfSavedPending = true;
        return poll(false);
      }).catch((error) => showError(error));
    }, 600));
  }

  const hotReferenceTimers = new Map();

  function scheduleHotReferences(task) {
    const taskId = task.task_id;
    clearTimeout(hotReferenceTimers.get(taskId));
    hotReferenceTimers.set(taskId, setTimeout(() => {
      hotReferenceTimers.delete(taskId);
      patchReferences(task).catch((error) => showError(error));
    }, 600));
  }

  function currentTask(taskId) {
    return state.view?.approval?.tasks.find((task) => task.task_id === taskId) || null;
  }

  function updateReference(taskId, assetId, patch) {
    const task = currentTask(taskId);
    if (!task) return;
    const references = task.reference_images.map((reference) => (
      reference.asset_id === assetId ? { ...reference, ...patch } : reference
    ));
    updateTask(taskId, { reference_images: references });
  }

  function updateReferenceMode(taskId, referenceMode) {
    try {
      state.review = ReviewState.setReferenceMode(state.review, taskId, referenceMode);
      state.view = ReviewState.draftView(state.review);
      updateActionAvailability();
      // render(view) 的第一个参数是必须的：内部直接读 view.status，
      // 无参调用会抛 "Cannot read properties of undefined (reading 'status')"，
      // 一旦触发，同一事件处理器后续逻辑全部中断（表现之一是参考素材拖拽失效）。
      // 本文件其它 4 处调用都显式传了 state.view，这里此前漏了。2026-09-29 修。
      if (state.view) render(state.view, { refreshTasks: true });
    } catch (error) {
      showError(error);
      if (state.view) render(state.view, { refreshTasks: true });
    }
  }

  async function prepareReferenceMutation(task) {
    const directive = typeof ReviewState.referenceMutationDirective === "function"
      ? ReviewState.referenceMutationDirective(state.review, task.task_id)
      : !ReviewState.hasDirty(state.review)
        ? "proceed"
        : ReviewState.canSaveReferences(state.review, task.task_id)
          ? "save_then_proceed"
          : "blocked";
    if (directive === "proceed") return true;
    if (directive === "save_then_proceed") return patchReferences(task);
    showError(new Error("正在提交审批或检测到数据冲突，请稍后重试"));
    return false;
  }

  async function patchReferences(task) {
    if (!ReviewState.canSaveReferences(state.review, task.task_id)) {
      showError(new Error("请先处理其他本地任务编辑，再保存参考图片用途与顺序"));
      return false;
    }
    const current = currentTask(task.task_id);
    const references = current?.reference_images || [];
    return mutate(`/api/runs/${state.runId}/tasks/${encodeURIComponent(task.task_id)}/references`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        references,
        reference_mode: current?.reference_mode || "multi_reference",
      }),
    }, true);
  }

  async function uploadReference(task, file, role, order, replacesAssetId = null) {
    if (!await prepareReferenceMutation(task)) return false;
    if (!file) {
      showError(new Error("请选择图片文件"));
      return false;
    }
    if (task.reference_mode === "first_last_frame" && !replacesAssetId) {
      showError(new Error("首尾帧模式只能保留两张图片；请先切换到多参考模式再增添图片"));
      return false;
    }
    const body = new FormData();
    body.append("file", file);
    body.append("task_id", task.task_id);
    body.append("role", role);
    body.append("order", String(order));
    if (replacesAssetId) body.append("replaces_asset_id", replacesAssetId);
    return mutate(`/api/runs/${state.runId}/references`, { method: "POST", body }, true);
  }

  async function unlinkReference(task, assetId) {
    if (!await prepareReferenceMutation(task)) return false;
    return mutate(
      `/api/runs/${state.runId}/tasks/${encodeURIComponent(task.task_id)}/references/${encodeURIComponent(assetId)}`,
      { method: "DELETE" },
      true,
    );
  }

  async function mutate(url, options, resetDraft = false) {
    if (state.busy) return false;
    setBusy(true);
    clearError();
    // 参考图增删替换会整页刷新草稿；任务勾选是本地状态，刷掉用户会
    // 丢选择。快照后恢复，只恢复非空选择（空选择保持默认全选）。
    const selection = [...ReviewState.selectedTaskIds(state.review)];
    try {
      await api(url, options);
      await poll(true, resetDraft);
      if (resetDraft && selection.length) {
        let review = state.review;
        const current = ReviewState.selectedTaskIds(review);
        for (const taskId of [...new Set([...current, ...selection])]) {
          const shouldSelect = selection.includes(taskId);
          if (current.includes(taskId) !== shouldSelect) {
            try {
              review = ReviewState.setTaskSelected(review, taskId, shouldSelect);
            } catch {
              // 任务可能已随刷新消失
            }
          }
        }
        state.review = review;
        state.view = ReviewState.draftView(review);
        updateActionAvailability();
      }
      return true;
    } catch (error) {
      showError(error);
      return false;
    } finally {
      setBusy(false);
    }
  }

  function referenceRow(task, reference, labels) {
    const asset = assetFor(reference.asset_id);
    const description = descriptionFor(reference.asset_id);
    const row = element("div", "reference-row");
    row.dataset.referenceTask = task.task_id;
    row.dataset.assetId = reference.asset_id;

    const isVideo = reference.role === "reference_video";
    const isAudio = reference.role === "reference_audio";
    const image = document.createElement(isVideo ? "video" : isAudio ? "audio" : "img");
    image.alt = `参考素材 ${reference.order}`;
    if (isVideo) {
      image.muted = true;
      image.controls = true;
      image.preload = "metadata";
      image.playsInline = true;
    }
    if (isAudio) {
      image.controls = true;
      image.preload = "metadata";
    }
    if (asset?.preview_url) image.src = agentUrl(asset.preview_url);
    // ① 参考素材缩略图**单击即预览**（用户 2026-10-10）。只挂图片：视频/音频元素本身
    // 有播放控件，单击是播控语义，抢过来会破坏原有交互。
    if (!isVideo && !isAudio && asset?.preview_url) {
      attachSingleClickPreview(image, agentUrl(asset.preview_url));
    }

    const descriptionText = description
      ? [description.subjects?.join("、"), description.scene, description.probable_role]
          .filter(Boolean)
          .join(" · ")
      : "本地新增图片，尚无视觉描述";
    const descriptionNode = element("div", "reference-description", descriptionText);

    const role = element(
      "div",
      "reference-role",
      // 2026-09-30（用户）：「前端图片、视频这种类型不同的素材，可以分别 123 的顺序来，
      // 图片1 和 视频1 自然区分开了」。这里显示的就是**提示词里会用的号**（`@图片1` /
      // `@视频1`），而不是全局提交序号、也不再是「参考视频」这种没有号的说法。
      labels?.get(reference.asset_id)
        || (reference.role === "first_frame"
          ? "首帧"
          : reference.role === "last_frame"
            ? "尾帧"
            : isVideo ? "参考视频" : isAudio ? "参考音频" : "普通参考图"),
    );

    const order = document.createElement("input");
    order.type = "number";
    order.min = "1";
    //: 这个框里填的是**该类型内的序号**（图片1 的 1、视频1 的 1），不再是全局 order。
    //: 改号只在**同类型内部**重排，全局 order 会被自动重编成 1..N 连续 —— 后端要求
    //: 连续（`integrations/seedance.py` 的 order_mismatch 校验），按类型填号会撞车。
    order.value = ReviewState.referenceTypeIndex(
      task.reference_images,
      reference.asset_id,
    ) || 1;
    order.setAttribute("aria-label", "同类型内的顺序");
    order.title = "同类型内的顺序（决定提示词里的 @图片N / @视频N）";
    order.addEventListener("input", () => {
      const next = ReviewState.reorderReferenceType(
        task.reference_images,
        reference.asset_id,
        Number(order.value),
      );
      updateTask(task.task_id, { reference_images: next });
    });

    const actions = element("div", "reference-actions");
    const replaceInput = document.createElement("input");
    replaceInput.type = "file";
    replaceInput.accept = "image/*,video/mp4,video/webm,audio/mpeg,audio/wav,audio/ogg,audio/aac";
    replaceInput.id = `reference-file-${task.task_id}-${reference.asset_id}`;
    replaceInput.className = "reference-file-input";
    const replace = element(
      "label",
      "quiet-button reference-replace-label",
      "替换",
    );
    replace.htmlFor = replaceInput.id;
    const remove = element(
      "button",
      "quiet-button reference-delete-button",
      "删除",
    );
    remove.type = "button";
    const feedback = ReferenceMutationState.rowFeedback(
      state.referenceMutations,
      task.task_id,
      reference.asset_id,
    );
    const mutationFeedback = element(
      "p",
      `reference-mutation-feedback${feedback ? ` is-${feedback.phase}` : ""}`,
      feedback?.message || "",
    );
    mutationFeedback.setAttribute("aria-live", "polite");

    function showRowFeedback(nextFeedback) {
      mutationFeedback.className = (
        `reference-mutation-feedback${nextFeedback ? ` is-${nextFeedback.phase}` : ""}`
      );
      mutationFeedback.textContent = nextFeedback?.message || "";
      const busy = ReferenceMutationState.isBusy(
        state.referenceMutations,
        task.task_id,
        reference.asset_id,
      );
      replaceInput.disabled = busy;
      replace.className = (
        `quiet-button reference-replace-label${busy ? " is-disabled" : ""}`
      );
      remove.disabled = busy;
      remove.textContent = nextFeedback?.phase === "deleting" ? "删除中…" : "删除";
    }

    replaceInput.addEventListener("change", async () => {
      const file = replaceInput.files[0];
      if (!file || ReferenceMutationState.isBusy(
        state.referenceMutations,
        task.task_id,
        reference.asset_id,
      )) return;
      state.referenceMutations = ReferenceMutationState.start(
        state.referenceMutations,
        task.task_id,
        reference.asset_id,
        "replace",
        file.name,
      );
      showRowFeedback(ReferenceMutationState.rowFeedback(
        state.referenceMutations,
        task.task_id,
        reference.asset_id,
      ));
      const succeeded = await uploadReference(
        task,
        file,
        reference.role,
        Number(order.value),
        reference.asset_id,
      );
      state.referenceMutations = succeeded
        ? ReferenceMutationState.succeed(
          state.referenceMutations,
          task.task_id,
          reference.asset_id,
          "参考素材已替换",
        )
        : ReferenceMutationState.fail(
          state.referenceMutations,
          task.task_id,
          reference.asset_id,
          errorMessage.textContent || "参考素材替换失败，请重试",
        );
      render(ReviewState.draftView(state.review));
    });
    remove.addEventListener("click", async () => {
      if (ReferenceMutationState.isBusy(
        state.referenceMutations,
        task.task_id,
        reference.asset_id,
      )) return;
      state.referenceMutations = ReferenceMutationState.start(
        state.referenceMutations,
        task.task_id,
        reference.asset_id,
        "delete",
      );
      showRowFeedback(ReferenceMutationState.rowFeedback(
        state.referenceMutations,
        task.task_id,
        reference.asset_id,
      ));
      const succeeded = await unlinkReference(task, reference.asset_id);
      state.referenceMutations = succeeded
        ? ReferenceMutationState.succeed(
          state.referenceMutations,
          task.task_id,
          reference.asset_id,
          "参考素材已删除并重新编号",
        )
        : ReferenceMutationState.fail(
          state.referenceMutations,
          task.task_id,
          reference.asset_id,
          errorMessage.textContent || "参考素材删除失败，请重试",
        );
      render(ReviewState.draftView(state.review));
    });
    actions.append(replaceInput, replace, remove);
    row.append(image, descriptionNode, role, order, actions, mutationFeedback);
    showRowFeedback(feedback);
    return row;
  }

  function referenceSection(task) {
    const section = element("section", "reference-section");
    const heading = element("div", "panel-heading");
    heading.append(element("h3", "", "参考素材"));
    const referenceMode = task.reference_mode || "multi_reference";
    // 2026-09-29：参考素材的增删改**只能在「等待审批」阶段**做。其它阶段后端一律 409
    // （实测 `only waiting_approval`：`_persist_draft` 会把运行推回审批中断、状态改回
    // waiting_approval，在"审核成片"的阶段根本落不了地）。所以这里把编辑控件收起来、
    // 只留只读展示 + 一句"怎么办" —— 否则用户看到能点、一点就报错。
    const referenceEditable = !state.view || state.view.status === "waiting_approval";
    const mode = document.createElement("select");
    mode.setAttribute("aria-label", "参考模式");
    [
      ["multi_reference", "多参考模式"],
      ["first_last_frame", "首尾帧模式"],
    ].forEach(([value, label]) => {
      const option = element("option", "", label);
      option.value = value;
      option.selected = value === referenceMode;
      mode.append(option);
    });
    mode.addEventListener("change", () => updateReferenceMode(task.task_id, mode.value));
    heading.append(mode);
    const save = element("button", "quiet-button", "保存用途与顺序");
    save.type = "button";
    save.addEventListener("click", () => patchReferences(task));
    heading.append(save);
    if (!referenceEditable) {
      mode.disabled = true;
      save.disabled = true;
      mode.title = "参考素材只能在审批页修改";
      save.title = "参考素材只能在审批页修改";
    }
    if (referenceMode === "multi_reference") {
      // 明确的落点提示（用户 2026-09-29：拖了半天不知道往哪拖）——放在节的标题行，
      // 不用滚到底部那一行才看得到。
      heading.append(element("span", "reference-drag-hint", "可把文件直接拖进本节"));
    }
    const list = element("div", "reference-list");
    //: 编号按**类型**各从 1 起（与提示词里的 @图片N/@视频N 一致），算一次给整列复用。
    const referenceLabels = ReviewState.referenceLabels(task.reference_images);
    [...task.reference_images]
      .sort((a, b) => a.order - b.order)
      .forEach((reference) => {
        const row = referenceRow(task, reference, referenceLabels);
        if (!referenceEditable) {
          // 非审批阶段：行内的「替换 / 删除」点了只会 409 —— 直接收起来，只读展示。
          row.querySelectorAll(
            ".reference-delete-button, .reference-replace-label, input[type='file']",
          ).forEach((node) => node.remove());
        }
        list.append(row);
      });

    const modeHint = element(
      "p",
      "mode-message",
      referenceMode === "first_last_frame"
        ? "首尾帧模式仅提交两张图片：首帧和尾帧。"
        : "多参考模式支持图片、视频和音频；首尾效果请在提示词中描述。",
    );
    const sectionFeedback = ReferenceMutationState.taskFeedback(
      state.referenceMutations,
      task.task_id,
    );
    const mutationFeedback = element(
      "p",
      `reference-section-feedback${sectionFeedback ? ` is-${sectionFeedback.phase}` : ""}`,
      sectionFeedback?.message || "",
    );
    mutationFeedback.setAttribute("aria-live", "polite");
    section.append(heading, modeHint, list, mutationFeedback);
    if (!referenceEditable) {
      section.append(element(
        "p",
        "mode-message reference-readonly-note",
        "参考素材只能在审批页增删：先点下方「退回修改计划」回到审批页，"
          + "加好或换好素材后再「批准并开始生成」。",
      ));
    }
    if (referenceEditable && referenceMode === "multi_reference") {
      const upload = element("div", "upload-row");
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = "image/*,video/mp4,video/webm,audio/mpeg,audio/wav,audio/ogg,audio/aac";
      const feedback = ReferenceUploadState.feedback(state.referenceUploads, task.task_id);
      const uploadFeedback = element(
        "p",
        `upload-feedback${feedback ? ` is-${feedback.phase}` : ""}`,
        feedback?.message || "请选择图片、视频或音频后再上传（也可以把文件直接拖到本节任意位置）。",
      );
      uploadFeedback.setAttribute("aria-live", "polite");
      const order = document.createElement("input");
      order.type = "number";
      order.min = "1";
      order.value = String(task.reference_images.length + 1);
      const add = element("button", "secondary", "增添素材");
      add.type = "button";
      fileInput.addEventListener("change", () => {
        const file = fileInput.files[0];
        if (!file) return;
        state.referenceUploads = ReferenceUploadState.fileSelected(
          state.referenceUploads,
          task.task_id,
          file,
        );
        uploadFeedback.className = "upload-feedback is-selected";
        uploadFeedback.textContent = ReferenceUploadState.feedback(
          state.referenceUploads,
          task.task_id,
        ).message;
      });
      // 「增添素材」按钮与**拖入即上传**共用这一段（见下面的 drop 处理器）。
      const submitPendingReference = async () => {
        const file = ReferenceUploadState.pendingFile(state.referenceUploads, task.task_id);
        if (!file) {
          const message = "请先选择图片、视频或音频文件";
          state.referenceUploads = ReferenceUploadState.uploadFailed(
            state.referenceUploads, task.task_id, message,
          );
          uploadFeedback.className = "upload-feedback is-error";
          uploadFeedback.textContent = message;
          showError(new Error(message));
          return;
        }
        state.referenceUploads = ReferenceUploadState.uploadStarted(state.referenceUploads, task.task_id);
        uploadFeedback.className = "upload-feedback is-uploading";
        uploadFeedback.textContent = ReferenceUploadState.feedback(
          state.referenceUploads, task.task_id,
        ).message;
        add.disabled = true;
        add.textContent = "正在添加…";
        let succeeded = false;
        try {
          const role = file.type.startsWith("video/") ? "reference_video" : file.type.startsWith("audio/") ? "reference_audio" : "reference_image";
          succeeded = await uploadReference(task, file, role, Number(order.value));
        } catch (error) {
          showError(error);
        }
        state.referenceUploads = succeeded
          ? ReferenceUploadState.uploadSucceeded(state.referenceUploads, task.task_id)
          : ReferenceUploadState.uploadFailed(
            state.referenceUploads,
            task.task_id,
            errorMessage.textContent || "图片添加失败，请重试",
          );
        render(ReviewState.draftView(state.review));
      };
      add.addEventListener("click", submitPendingReference);
      upload.append(fileInput, order, add, uploadFeedback);
      // 拖文件到「参考素材」这一**节** = 等价于「选择文件」（用户 2026-09-29）。
      //
      // 为什么挂在整节上而不是那一行：2026-09-29 用户报「拖动上传还是不行」，实测
      // （真实页面 + elementFromPoint 命中测试）那一行只有 76px 高、位于页面 y≈5100，
      // 而**节内任意位置都是有效落点**（命中路径 INPUT < DIV.upload-row <
      // SECTION.reference-section）。用户的直觉落点是"素材列表/这一节"，拖到缩略图或
      // 标题上旧实现毫无反应 —— 看起来就是"拖不了"。挂到整节，哪里都能接。
      //
      // 只把文件填进选择器并触发 change，**不**直接上传 —— 仍然要点「增添素材」
      // （方案 A：避免拖错一个文件就消耗一次上传）。
      //
      // 另外两个细节也是实测踩出来的：
      //  1) 指针从节内移到**子元素**上时，浏览器会补一对 dragleave/dragenter ——
      //     直接按 dragleave 取消高亮会闪，用户以为"没反应"。用进入计数判断真离开。
      //  2) 拖入的内容里没有文件（从别的网页拖图片、拖文字）时**必须说话**，
      //     静默 return 会让用户完全不知道发生了什么。
      let dragDepth = 0;
      const setDragging = (on) => {
        section.classList.toggle("isDragging", on);
        upload.classList.toggle("isDragging", on);
      };
      const dropFeedback = (phase, message) => {
        state.referenceUploads = phase === "error"
          ? ReferenceUploadState.uploadFailed(state.referenceUploads, task.task_id, message)
          : ReferenceUploadState.uploadStarted(state.referenceUploads, task.task_id);
        uploadFeedback.className = `upload-feedback is-${phase}`;
        uploadFeedback.textContent = message;
      };
      const defaultHint = "请选择图片、视频或音频后再上传（也可以把文件直接拖到本节任意位置）。";
      // 拖动过程中**实时**给反馈：这是"事件到底有没有到这一页"的唯一肉眼判据。
      // 浏览器安全限制不允许在 drop 之前读文件名，但 `types` 里有没有 "Files" 可读 ——
      // 用户 2026-09-29 反复报"拖不了"，有这一行就能一眼分辨是"没收到"还是"收到了但没处理"。
      const restoreFeedback = () => {
        const current = ReferenceUploadState.feedback(state.referenceUploads, task.task_id);
        uploadFeedback.className = `upload-feedback${current ? ` is-${current.phase}` : ""}`;
        uploadFeedback.textContent = current?.message || defaultHint;
      };
      section.addEventListener("dragenter", () => {
        dragDepth += 1;
        setDragging(true);
      });
      section.addEventListener("dragover", (event) => {
        event.preventDefault();
        setDragging(true);
        const hasFiles = Array.from(event.dataTransfer?.types || []).includes("Files");
        uploadFeedback.className = hasFiles ? "upload-feedback is-selected" : "upload-feedback is-error";
        uploadFeedback.textContent = hasFiles
          ? "松开鼠标即可添加（已检测到文件）"
          : "拖入的不是文件：请拖图片/视频/音频文件（文件夹、网页里的图片都不行）";
      });
      section.addEventListener("dragleave", () => {
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) {
          setDragging(false);
          restoreFeedback();
        }
      });
      section.addEventListener("drop", async (event) => {
        event.preventDefault();
        dragDepth = 0;
        setDragging(false);
        const dropped = ReferenceUploadState.droppedFile(event.dataTransfer);
        if (!dropped) {
          dropFeedback("error", "拖入的内容里没有文件，请从资源管理器拖图片/视频/音频");
          return;
        }
        // 文件夹 / 压缩包 / 未知类型：`type` 是空串。**必须在这里拦住并说清楚** ——
        // 塞给 `input.files` 会静默失败，change 里读到空就直接 return，界面上一点反馈
        // 都没有（用户 2026-09-29：「拖到未选择文件那一栏没反应」就是这个）。
        if (!ReferenceUploadState.accepts(dropped)) {
          const name = dropped.name || "未知内容";
          dropFeedback(
            "error",
            dropped.type
              ? `不支持的类型（${dropped.type}）：请拖图片、视频或音频文件`
              : `「${name}」是文件夹或没有类型，请拖单个图片/视频/音频文件`,
          );
          return;
        }
        // 直接写进应用状态，**不依赖** `input.files` 赋值（那条路会静默失败）。
        state.referenceUploads = ReferenceUploadState.fileSelected(
          state.referenceUploads,
          task.task_id,
          dropped,
        );
        // 顺便同步到可见的文件框；失败也不影响（状态里已经有了）。
        try {
          const transfer = new DataTransfer();
          transfer.items.add(dropped);
          fileInput.files = transfer.files;
        } catch (error) {
          /* 忽略：可见性同步失败不影响上传 */
        }
        // **拖入即上传**（2026-09-29）。原来是方案 A：拖入只填进选择器，还要再点一次
        // 「增添素材」——用户那边的表现就是"拖了没反应"。访问日志证实了这个判断：经
        // Portal 的 29 次参考素材上传里 28 次是 201（全部来自点按钮那条路），而他报告
        // "传不了" 的那几次**根本没发出 POST**，也就是那一步点击没发生。
        // 参考素材上传不花钱（只落盘 + 重进审批草稿），所以省掉这一步、拖完就进去。
        await submitPendingReference();
      });
      section.append(upload);
    }
    return section;
  }

  function renderCoverage(view) {
    const coverage = ReviewState.assetCoverage(view);
    byId("coverage-label").textContent = ReviewState.coverageLabel(view);
    const details = [
      `已排除 ${coverage.excluded_count} 张`,
      `未覆盖 ${coverage.uncovered_count} 张`,
      `读取失败 ${coverage.failed_count} 张`,
    ];
    byId("coverage-detail").textContent = details.join(" · ");
    const rows = ReviewState.excludedAssetRows(view).map((item) => {
      const row = element("div", "excluded-asset-row");
      let preview;
      if (item.media_kind === "image") {
        preview = document.createElement("img");
        preview.alt = `排除素材 ${item.asset_id}`;
      } else if (item.media_kind === "video") {
        preview = document.createElement("video");
        preview.controls = true;
        preview.preload = "metadata";
        preview.muted = true;
        preview.playsInline = true;
        preview.setAttribute("aria-label", `排除视频 ${item.asset_id}`);
      } else if (item.media_kind === "audio") {
        preview = document.createElement("audio");
        preview.controls = true;
        preview.preload = "metadata";
        preview.setAttribute("aria-label", `排除音频 ${item.asset_id}`);
      } else {
        preview = element(
          "div",
          "excluded-asset-placeholder",
          item.mime_type || "附件",
        );
      }
      if (item.preview_url && item.media_kind !== "file") {
        preview.src = agentUrl(item.preview_url);
      }
      // ① 排除素材的缩略图同样单击预览（这一行的操作是「排除」，在按钮上，不冲突）。
      if (item.media_kind === "image" && item.preview_url) {
        attachSingleClickPreview(preview, agentUrl(item.preview_url));
      }
      const content = element("div", "excluded-asset-copy");
      content.append(
        element("strong", "", item.asset_id),
        element("p", "", item.reason),
      );
      row.append(preview, content);
      return row;
    });
    if (!rows.length) {
      rows.push(element("p", "mode-message", "暂无排除素材。"));
    }
    byId("excluded-asset-list").replaceChildren(...rows);
    // 未覆盖的素材：给一个「排除」入口 —— 覆盖门要求「每个素材要么被引用、要么被
    // 排除」，而以前界面没有排除入口，用户只能手改提示词才能批准（2026-09-17）。
    const uncovered = ReviewState.uncoveredAssetRows(view).map((item) => {
      const row = element("div", "excluded-asset-row");
      const content = element("div", "excluded-asset-copy");
      const exclude = element("button", "quiet-button", "排除");
      exclude.type = "button";
      exclude.disabled = state.busy;
      exclude.addEventListener("click", () => excludeAsset(item.asset_id, exclude));
      content.append(
        element("strong", "", item.asset_id),
        element("p", "", "计划没有用到它：排除掉才能批准；要用就把它加进某个任务的参考图。"),
        exclude,
      );
      // ⚠️ 刻意**不**给这一行加缩略图：这一列用的是单列模板
      // （`.excluded-asset-list--uncovered`，2026-10-08 修「逐字换行 + 右侧空白」时
      // 定下来的），挂上预览元素就必须改回两列模板 —— 那等于回退那次排版修复。
      // 用户口径是「凡是有缩略图的地方」：这一行本来就没有缩略图。
      row.append(content);
      return row;
    });
    byId("uncovered-asset-list").replaceChildren(
      ...(uncovered.length
        ? uncovered
        : [element("p", "mode-message", "没有未使用素材。")]),
    );
  }

  /** 把用不到的素材排除掉（覆盖门要人做这个决定，界面得给人入口）。 */
  async function excludeAsset(assetId, button) {
    if (state.busy || !state.runId) return;
    setBusy(true);
    clearError();
    button.disabled = true;
    try {
      await api(`/api/runs/${state.runId}/excluded-assets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_id: assetId }),
      });
      await poll(true);
    } catch (error) {
      showError(error);
      button.disabled = false;
    } finally {
      setBusy(false);
    }
  }

  /** 返工对比：这条新提示词怎么来的、跟改前比动了哪几句。 */
  function reworkComparisonPanel(task) {
    const diffApi =
      typeof globalThis.PromptDiff === "object" ? globalThis.PromptDiff : null;
    if (!diffApi) return null;
    const comparison = diffApi.reworkComparison(task);
    if (!comparison.hasRework) return null;

    const box = element("div", "rework-compare");
    const head = element("div", "rework-compare-head");
    head.append(
      element("strong", "", "返工对比"),
      element(
        "span",
        "rework-compare-stats",
        `新增 ${comparison.stats.added} 句 · 删除 ${comparison.stats.removed} 句 · 保留 ${comparison.stats.unchanged} 句`,
      ),
    );
    box.append(head);

    if (comparison.requirements.length) {
      box.append(
        element(
          "div",
          "rework-compare-label",
          "历次返工要求（只累积、不覆盖）",
        ),
      );
      const list = element("ol", "rework-compare-requirements");
      comparison.requirements.forEach((item) => {
        list.append(element("li", "", item));
      });
      box.append(list);
    }

    box.append(
      element("div", "rework-compare-label", "改前（上一版提示词）"),
      element("pre", "rework-compare-base", comparison.basePrompt),
    );

    if (comparison.hasChanges) {
      box.append(element("div", "rework-compare-label", "改后差异"));
      const diff = element("div", "rework-compare-diff");
      comparison.segments.forEach((segment) => {
        if (segment.type === "added") {
          diff.append(element("span", "diff-added", segment.text));
        } else if (segment.type === "removed") {
          diff.append(element("span", "diff-removed", segment.text));
        } else {
          diff.append(element("span", "diff-same", segment.text));
        }
      });
      box.append(diff);
    } else {
      // 光说「一致」等于没说：用户会以为是坏了。这里要讲清真实原因，并给出
      // 下一步。**不要再建议"写成禁止项"** —— 用户口径（2026-09-18）是
      // 「不要在约束里加东西，直接描述在正文就行」，禁止项堆多了反而加深坏印象。
      box.append(
        element("div", "rework-compare-label", "本次返工没有改变提示词正文"),
        element(
          "p",
          "bitable-task-meta",
          "原因通常是这条要求**上一版正文里已经写进去了**（融合器核对后判定无需改动），"
            + "也可能是它本身不构成对画面的新约束。"
            + "如果你的意思是「上一版写了但没做到」，重复同一句不会更有效 —— "
            + "建议把它**改写成更可判定、更可执行的画面描述**"
            + "（例如「自然一点」→「手部动作放慢，每次只做一个动作」），"
            + "也可以直接手动改提示词正文。",
        ),
      );
    }

    const wrapper = element("div", "field field-wide");
    wrapper.append(element("label", "", "返工对比"), box);
    return wrapper;
  }

  function renderTask(task) {
    const card = element("article", "task-card");
    const titleRow = element("div", "task-title-row");
    const selected = document.createElement("input");
    selected.type = "checkbox";
    selected.checked = ReviewState.selectedTaskIds(state.review).includes(task.task_id);
    selected.dataset.taskId = task.task_id;
    selected.setAttribute("aria-label", `选择任务 ${task.title}`);
    selected.addEventListener("change", () => {
      try {
        state.review = ReviewState.setTaskSelected(state.review, task.task_id, selected.checked);
        state.view = ReviewState.draftView(state.review);
        updateActionAvailability();
      } catch (error) {
        showError(error);
      }
    });
    const title = element("div", "");
    title.append(
      element("h3", "", task.title),
      element("span", "task-type", `${task.task_type} · 置信度 ${task.confidence ?? "—"}`),
    );
    titleRow.append(selected, title);

    const grid = element("div", "task-grid");
    grid.append(
      field("提示词", textArea(task.prompt, (value) => {
        updateTask(task.task_id, { prompt: value });
      }, 10, "task-prompt-editor"), true),
      field(
        "负面约束",
        textArea((task.negative_constraints || []).join("\n"), (value) => {
          updateTask(task.task_id, {
            negative_constraints: value.split("\n").map((item) => item.trim()).filter(Boolean),
          });
        }, 5, "task-negative-editor"),
        true,
      ),
    );
    const reworkPanel = reworkComparisonPanel(task);
    if (reworkPanel) grid.append(reworkPanel);
    if (task.task_type === "image_to_image") {
      grid.append(
        field("画面比例", ratioPicker(task)),
        field("生成数量", textInput(task.output_count, (value) => {
          updateTask(task.task_id, { output_count: Number(value) });
        }, "number")),
      );
      grid.append(field("裁剪交付", deliveryCropToggle(task), true));
      grid.append(field("图片尺寸", textInput(task.image_size, (value) => {
        updateTask(task.task_id, { image_size: value });
      })));
      grid.append(field("出图模型", providerPicker(task)));
      grid.append(
        field(
          "输出尺寸（每行一个，如 1700x2500）",
          textArea((task.size_variants || []).join("\n"), (value) => {
            updateTask(task.task_id, {
              size_variants: value
                .split("\n")
                .map((item) => item.trim())
                .filter(Boolean),
            });
          }, 3, "task-size-variants-editor"),
        ),
      );
      grid.append(field("安全区", textInput(task.safe_area, (value) => {
        updateTask(task.task_id, { safe_area: value || null });
      })));
      grid.append(field("画风预设", stylePresets(task), true));
    } else {
      const capabilities = videoCapabilities(task);
      const modelOption = videoOptionFor(task);
      const section = element("section", "video-param-section");
      const heading = element("div", "video-param-heading");
      heading.append(
        element("span", "video-param-title", "生成参数"),
        element("span", "video-param-model", modelOption?.label || task.video_provider || ""),
      );

      const audio = document.createElement("select");
      audio.className = "task-control";
      // 声音默认开启：未填写（旧计划里是 null）与 true 都显示「开启」，
      // 跟提交时 `generate_audio is not False` 的判定保持一致，避免界面骗人。
      const audioEnabled = task.generate_audio !== false;
      [["true", "开启"], ["false", "关闭"]].forEach(([value, label]) => {
        const option = element("option", "", label);
        option.value = value;
        option.selected = String(audioEnabled) === value;
        audio.append(option);
      });
      audio.addEventListener("change", () => {
        updateTask(task.task_id, { generate_audio: audio.value === "true" });
      });

      const parameterGrid = element("div", "video-param-grid");
      parameterGrid.append(
        videoField("视频模型", videoProviderPicker(task), "切换后自动校正参数"),
        videoField(
          "画面比例",
          ratioPicker(task),
          `支持 ${capabilities.aspect_ratios.length} 种比例`,
        ),
        videoField(
          "分辨率",
          resolutionPicker(task),
          `可选 ${capabilities.resolutions.map(resolutionLabel).join(" / ")}`,
        ),
        videoField(
          "视频时长",
          durationInput(task),
          `${capabilities.duration_min}-${capabilities.duration_max} 秒`,
        ),
        videoField(
          "生成数量",
          outputCountInput(task),
          `1-${capabilities.max_output_count} 条候选`,
        ),
        videoField("声音", audio, "默认开启，可手动关闭"),
      );

      const resolutionText = capabilities.resolutions.map(resolutionLabel).join(" / ");
      section.append(heading, parameterGrid);
      section.append(
        element(
          "p",
          "task-model-capability",
          `当前模型能力：${capabilities.duration_min}-${capabilities.duration_max} 秒 · ${resolutionText} · 最多 ${capabilities.max_output_count} 条候选`,
        ),
      );
      grid.append(section);
    }

    const notes = element("div", "task-notes");
    (task.assumptions || []).forEach((text) => notes.append(element("span", "note", `假设：${text}`)));
    (task.warnings || []).forEach((text) => notes.append(element("span", "note", `警告：${text}`)));
    (task.blocking_issues || []).forEach((text) => notes.append(element("span", "note blocking", `阻塞：${text}`)));
    card.append(titleRow, grid, storyboardSection(task), notes, referenceSection(task));
    return card;
  }

  /**
   * 选中某个时间段的关键帧候选。
   *
   * **不走 updateTask**：`updateTask` 会把补丁直接盖进本地草稿，而选图只需要回传
   * `selected`；用 `{index, selected}` 覆盖草稿里的 `storyboard_frames` 会把
   * start/end/prompt/candidates 全弄丢 —— 批准时 StoryboardFrame 缺字段，报
   * 「Field required；Field required…」（2026-09-22 实跑踩到，2 帧 × 2 字段）。
   * 这里改成：本地只改高亮（乐观），服务端按 index 合并候选，随后 poll 拉回权威视图。
   */
  function selectStoryboardCandidate(task, frame, assetId) {
    frame.candidates.forEach((item) => {
      item.selected = item.asset_id === assetId;
    });
    const frames = task.storyboard_frames || [];
    const patch = {
      storyboard_frames: frames.map((item) => ({
        index: item.index,
        start: item.start,
        end: item.end,
        prompt: item.prompt || "",
        // 其它段原样回传**当前高亮的那张**；一张都没高亮就回传 null。
        // 以前这里兜底成 `candidates[0]`：用户删掉过某段的关键帧（那一段不再高亮
        // 任何候选）之后，随便在别的段点一下，就会把删掉的那一段又补上一张
        // 他没挑过的关键帧 —— 还是「删了等于没删」。
        selected: item.index === frame.index
          ? assetId
          : (item.candidates.find((one) => one.selected)?.asset_id || null),
      })),
    };
    api(`/api/runs/${state.runId}/tasks/${encodeURIComponent(task.task_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch }),
    }).then(() => poll(false)).catch((error) => reportActionFailure(error));
  }

  /**
   * 分镜关键帧选图区（用户 2026-09-22：先生成分镜图供用户选择，再生成视频）。
   *
   * 每个时间段一行、横排 K 张候选；点一张即选中（默认第一张）。选中结果通过
   * 热修改 PATCH 回服务端，服务端会把它变成任务的参考图 + 提示词里的关键帧点名。
   */
  function storyboardSection(task) {
    // 候选被删光的段**也要保留一行**：用户在「参考素材」里删掉该段唯一的关键帧后，
    // 服务端会把这一段的候选清空（`unlink_reference`）；如果这里按「候选非空」过滤，
    // 整行（连「重新生成这一张」）会一起消失，用户就再也点不到了。
    const frames = Array.isArray(task.storyboard_frames)
      ? task.storyboard_frames.filter((frame) => Array.isArray(frame?.candidates))
      : [];
    if (!frames.length) return null;
    const section = element("section", "storyboard-section");
    section.append(
      element(
        "p",
        "storyboard-hint",
        "分镜关键帧：每个时间段挑一张（默认第一张）。选中的图会作为关键帧参考一起提交，"
          + "画面会严格对齐它 —— 比只给文字稳定得多。",
      ),
    );
    frames.forEach((frame) => {
      const row = element("div", "storyboard-row");
      // 没有候选就**不highlight任何一张**（以前会兜底高亮第一张，删空的段会显示成
      // 「选了第一张」，与「这一段没有关键帧」矛盾）。
      const selectedId = frame.candidates.find((item) => item.selected)?.asset_id
        || null;
      row.append(
        element(
          "span",
          "storyboard-when",
          `第 ${frame.index} 段 · ${frame.start}-${frame.end}s`,
        ),
      );
      const strip = element("div", "storyboard-strip");
      frame.candidates.forEach((candidate) => {
        const choice = document.createElement("button");
        choice.type = "button";
        choice.className = candidate.asset_id === selectedId
          ? "storyboard-candidate is-selected"
          : "storyboard-candidate";
        choice.title = `选这张作为第 ${frame.index} 段的关键帧`;
        choice.addEventListener("click", () => {
          selectStoryboardCandidate(task, frame, candidate.asset_id);
        });
        const image = document.createElement("img");
        image.loading = "lazy";
        image.alt = `第 ${frame.index} 段候选`;
        image.src = agentUrl(candidate.preview_url);
        choice.append(image);
        // ① 分镜候选的缩略图**单击已经有语义了**（= 选这张关键帧），所以这里不抢它的
        // 单击，而是并排给一个独立的「预览」小按钮（放在按钮外层，避免 button 套
        // button 的非法结构）。用户要的是「有明确的点击预览入口」，语义冲突的地方
        // 用独立入口才不破坏原有交互。
        const cell = element("div", "storyboard-candidate-cell");
        cell.append(
          choice,
          previewButton(
            candidate.preview_url ? agentUrl(candidate.preview_url) : "",
            `第 ${frame.index} 段候选`,
          ),
        );
        strip.append(cell);
      });
      if (!frame.candidates.length) {
        strip.append(
          element(
            "span",
            "storyboard-empty",
            "本段关键帧已删除，不会再作为参考图提交；需要的话重新生成这一张。",
          ),
        );
      }
      row.append(strip);
      // 每段行尾一个「重新生成这一张」：同一段描述、同一参考图重新采样 K 张，
      // 不重新规划、不改提示词、不动其它段（用户 2026-09-22 选的方案 A）。
      const regenerate = document.createElement("button");
      regenerate.type = "button";
      regenerate.className = "storyboard-regenerate secondary";
      regenerate.textContent = "重新生成这一张";
      regenerate.title = "同一段描述重新出图，只替换这一段的候选";
      regenerate.addEventListener("click", () => {
        regenerateStoryboardFrame(task, frame, regenerate);
      });
      row.append(regenerate);
      section.append(row);
    });
    return section;
  }

  /** 只重跑某一段的分镜图（方案 A）。 */
  function regenerateStoryboardFrame(task, frame, button) {
    if (!state.runId || button.disabled) return;
    button.disabled = true;
    const label = button.textContent;
    button.textContent = "生成中…";
    api(
      `/api/runs/${state.runId}/tasks/${encodeURIComponent(task.task_id)}`
        + `/storyboard/${frame.index}/regenerate`,
      { method: "POST" },
    )
      .then(() => poll(false))
      .catch((error) => reportActionFailure(error))
      .finally(() => {
        button.disabled = false;
        button.textContent = label;
      });
  }

  // 同一条需求历次尝试的成片（run_id → artifacts）。取过一次就缓存，避免
  // 成片预览每秒轮询时反复拉同一批数据。
  const artifactHistoryCache = new Map();
  //: 拉取失败的版本 → 失败时间戳。**失败绝不能写成空数组**：那会让卡片永远显示
  //: 「这一版没有成片。」而且再也不重试（缓存命中了）。用户 2026-09-22「有些卡片
  //: 没有预览」，实测就是服务重启期间的失败被永久缓存了 —— 同一批版本在干净页面
  //: 上全都能出预览。
  const artifactHistoryFailedAt = new Map();
  const ARTIFACT_HISTORY_RETRY_MS = 30000;
  //: 每次生成**自己的**时间（来自 /api/runs/{id}）。
  //:
  //: 不能用 recent-runs 的 updated_at —— 那是**多维表格记录行**的更新时间，一条记录
  //: 一行，同一记录的历次尝试全是同一个时间（用户 2026-09-18：「所有日期都一样，
  //: 没有按照顺序来，跟生成的时机完全不一样」）。
  const runTimeCache = new Map();
  //: 失败尝试的**可读原因**（run_id → 文字），与 artifacts 同一次请求取回。
  //: 拿不到就空串 —— 卡片上如实写「未记录失败原因」，绝不编一个理由出来。
  const artifactHistoryFailureReason = new Map();

  //: 打开某条运行时按记录拉回的**完整**历次尝试（record_id → runs）。
  //:
  //: 「最近任务」列表是窗口化的：最多 10 条任务、后端只扫最近 800 条 run。
  //: 更老记录的历次尝试因此永远进不了那份列表（2026-09-24 实测：89 条失败尝试里
  //: 45 条属于窗口外的老记录）。用户点开某条记录时要的是**它自己**的完整历史，
  //: 所以另走 `/api/bitable/record-runs`（按 record_id 查，不受窗口限制），
  //: 拉回来的结果只喂给「往次成片」滑条 —— 全局任务记录列表仍用原来那份窗口数据。
  const recordRunsCache = new Map();
  //: 拉取失败的运行 → 失败时间戳（退避重试，避免「重画 → 再拉 → 再失败」死循环）。
  const recordRunsFailedAt = new Map();
  //: run_id → record_id：前端手上不一定有 record_id（运行详情里没这个字段），
  //: 由这一次请求的响应带回来。
  const recordIdByRun = new Map();
  const RECORD_RUNS_RETRY_MS = 30000;

  /** 当前运行属于哪条记录（列表里有就用列表的，没有就用刚拉回来的）。 */
  function currentRecordIdFor(runId) {
    const known = (state.bitable.recentRuns || []).find(
      (run) => run && run.run_id === runId,
    );
    return (known && known.record_id) || recordIdByRun.get(runId) || "";
  }

  /**
   * 拉这条运行所属记录的**全部**历次尝试。成功返回 true。
   *
   * 失败**不写空数组**（同 artifactHistoryCache 的教训）：写空会让滑条永远只有
   * 窗口里那几条，且再也不重试。失败只记时间戳，退避后再试。
   */
  async function ensureRecordRuns(runId) {
    if (!runId) return false;
    const failedAt = recordRunsFailedAt.get(runId);
    if (failedAt !== undefined && Date.now() - failedAt <= RECORD_RUNS_RETRY_MS) {
      return false;
    }
    try {
      const payload = await api(
        `/api/bitable/record-runs?run_id=${encodeURIComponent(runId)}`,
      );
      const runs = Array.isArray(payload?.runs) ? payload.runs : [];
      const recordId = payload?.record_id
        || (runs.find((run) => run && run.record_id) || {}).record_id
        || "";
      if (!recordId) {
        recordRunsFailedAt.set(runId, Date.now());
        return false;
      }
      recordIdByRun.set(runId, recordId);
      recordRunsCache.set(recordId, runs);
      recordRunsFailedAt.delete(runId);
      return true;
    } catch (error) {
      recordRunsFailedAt.set(runId, Date.now());
      return false;
    }
  }

  /** 该记录的完整历史是否还没拉过（拉失败的在退避窗口内不算「要拉」）。 */
  function needsRecordRuns(runId) {
    if (!runId) return false;
    const recordId = currentRecordIdFor(runId);
    if (recordId && recordRunsCache.has(recordId)) return false;
    const failedAt = recordRunsFailedAt.get(runId);
    return failedAt === undefined || Date.now() - failedAt > RECORD_RUNS_RETRY_MS;
  }

  /**
   * 给「往次成片」用的运行列表：窗口里的最近任务 **∪** 当前这条记录的完整历史。
   *
   * 完整历史按 run_id 覆盖窗口切片（同一版本以完整那份为准，字段更全：多了
   * `runtime_status`）。这样老记录的失败尝试也能出现在滑条上，而全局列表的
   * 数据一个字段都没动。
   */
  function runsForArtifactHistory() {
    const list = state.bitable.recentRuns || [];
    const recordId = currentRecordIdFor(state.runId);
    const full = recordId ? recordRunsCache.get(recordId) : null;
    if (!Array.isArray(full) || !full.length) return list;
    const seen = new Set(full.map((run) => run && run.run_id));
    return [
      ...full,
      ...list.filter((run) => run && !seen.has(run.run_id)),
    ];
  }

  /**
   * 从一次运行的详情里挑出**能给人看**的失败原因。
   *
   * 优先级按生产数据定的（记录 recvvWdIXpk2uE 的 4 条失败尝试实测）：
   *  1. `last_error.message` —— 运行时自己记的原因，最准；只有通用占位
   *     （"The workflow node could not be completed"）时按类别换成中文，再往下找；
   *  2. 该次运行第一条 `status=failed` 事件的摘要；
   *  3. `execution_records[].error.message` —— **生产上大多数失败只有这一条**
   *     （last_error 是 null、事件里也没有 failed），例如
   *     `seedance2.5：生成参数不符合供应商要求`；
   *  4. 都没有 → 按**运行时状态**说一句实话：`cancelled` 就是「这次被取消了」
   *     （多维表格任务状态把它和 failed 一起压成「失败」，只能看运行时状态）；
   *     认不出状态就返回空串，界面显示「未记录失败原因」，**绝不编具体错误**。
   *
   * 极简假 DOM 环境只读普通字段，不碰任何 DOM API。
   */
  function failureReasonFromRun(view) {
    if (!view || typeof view !== "object") return "";
    const lastError = view.last_error;
    const rawMessage = typeof lastError === "string"
      ? lastError
      : (lastError && typeof lastError.message === "string" ? lastError.message : "");
    const message = rawMessage.trim();
    if (message && message !== GENERIC_FAILURE_MESSAGE) return message;
    if (message === GENERIC_FAILURE_MESSAGE) {
      const category = lastError && lastError.category;
      const readable = FAILURE_CATEGORY_FALLBACK[category]
        || (lastError && lastError.technical_detail)
        || "";
      if (String(readable).trim()) return String(readable).trim();
    }
    const events = Array.isArray(view.events) ? view.events : [];
    const failedEvent = events.find((event) => (
      event
      && /fail/i.test(String(event.status || ""))
      && String(event.summary || "").trim()
    ));
    if (failedEvent) return String(failedEvent.summary).trim();
    const records = Array.isArray(view.execution_records)
      ? view.execution_records
      : [];
    const failedRecord = records.find((record) => (
      record && record.error && String(record.error.message || "").trim()
    ));
    if (failedRecord) {
      const provider = failedRecord.provider ? `${failedRecord.provider}：` : "";
      return `${provider}${failedRecord.error.message}`.trim();
    }
    // 具体原因一条都没有时，至少把「被取消」和「真的没记原因」分开说。
    // 运行详情里的 `status` 就是运行时状态；payload 路径另给了 `runtime_status`。
    return failureFallbackByStatus(view.runtime_status || view.status);
  }

  /** 这条 run 是不是**失败的尝试**（滑条上要打失败徽标、不给下载入口）。 */
  function isFailedAttempt(run) {
    return Boolean(run) && run.status === "失败";
  }

  /**
   * 失败尝试的原因：先用手上就有的（recent-runs 下发的 last_error），
   * 再用逐条拉回来的运行详情。都拿不到时按**运行时状态**给一句实话
   * （「这次被取消了」），认不出来才返回空串。
   */
  function failureReasonForRun(run) {
    const cached = artifactHistoryFailureReason.get(run.run_id);
    if (cached) return cached;
    const payloadError = typeof run.last_error === "string" ? run.last_error.trim() : "";
    return payloadError || failureFallbackByStatus(run.runtime_status);
  }

  async function ensureArtifactHistory(runIds) {
    const now = Date.now();
    const missing = runIds.filter((runId) => {
      if (artifactHistoryCache.has(runId)) return false;
      const failedAt = artifactHistoryFailedAt.get(runId);
      // 失败过就退避一段时间再试，避免「重画 → 再拉 → 再失败」的死循环。
      return failedAt === undefined || now - failedAt > ARTIFACT_HISTORY_RETRY_MS;
    });
    if (!missing.length) return false;
    await Promise.all(missing.map(async (runId) => {
      try {
        const sibling = await api(`/api/runs/${runId}`);
        artifactHistoryCache.set(
          runId,
          Array.isArray(sibling?.artifacts) ? sibling.artifacts : [],
        );
        runTimeCache.set(runId, {
          created_at: sibling?.created_at,
          updated_at: sibling?.updated_at,
        });
        artifactHistoryFailureReason.set(
          runId,
          failureReasonFromRun(sibling),
        );
        artifactHistoryFailedAt.delete(runId);
      } catch (error) {
        // 某一版拉不到不影响其它版本，也不该弹全局错误 —— 但**也不能假装它没有
        // 成片**：以前这里写空数组，卡片会永远显示「这一版没有成片。」且不再重试。
        artifactHistoryFailedAt.set(runId, Date.now());
      }
    }));
  }

  /** 历次尝试的展示/排序时间：优先**每次生成自己的**时间，取不到才退回表格行时间。 */
  function siblingDisplayTime(run) {
    const cached = runTimeCache.get(run.run_id);
    const value =
      (cached && (cached.updated_at || cached.created_at)) || run.updated_at;
    const parsed = BitableState.parseServerTime(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  /**
   * 成片预览里的「历史生成」快照：同一条需求**往次生成**的成片。
   *
   * 任务记录一条记录一行；历次版本在这里（成片与结果那一栏的横向滑条里）看和切，
   * 不另开往下的分区 —— 那样页面会挤。产物取过一次就缓存：成片预览每秒轮询。
   *
   * 运行列表用 `runsForArtifactHistory()`：窗口里的最近任务 ∪ 当前记录的完整历史
   * （后者按 record_id 单独拉，老记录的失败尝试也能看到）。
   */
  function artifactHistorySnapshot() {
    const siblings = BitableState.siblingRuns(
      runsForArtifactHistory(),
      state.runId,
    )
      // 按**每次生成自己的**时间倒序：表格行的时间对同一记录全都一样，排不出先后。
      .map((run, index) => ({ run, index }))
      .sort((left, right) => {
        const leftMs = siblingDisplayTime(left.run);
        const rightMs = siblingDisplayTime(right.run);
        if (leftMs !== rightMs) return rightMs - leftMs;
        return left.index - right.index;
      })
      .map((entry) => entry.run);
    return {
      siblings,
      signature: JSON.stringify(
        siblings.map((run) => [
          run.run_id,
          run.status,
          run.updated_at,
          artifactHistoryCache.has(run.run_id),
          artifactHistoryFailedAt.has(run.run_id),
          runTimeCache.get(run.run_id)?.updated_at || "",
        ]),
      ),
    };
  }

  /** 一个「往次版本」卡片：和当前成片并排在同一栏，能直接播、能切过去看。 */
  function artifactHistoryCard(run) {
    const failed = isFailedAttempt(run);
    const card = element(
      "figure",
      failed
        ? "artifact-card artifact-history-card is-failed"
        : "artifact-card artifact-history-card",
    );
    card.dataset.runId = run.run_id;
    // 失败版要一眼看出来：灰化 + 「失败」徽标 + 卡片上直接写失败原因
    //（用户 2026-09-24：「我失败过几次、为什么失败」看不到）。
    if (failed) card.dataset.failed = "1";
    const cachedTime = runTimeCache.get(run.run_id);
    const displayTime =
      (cachedTime && (cachedTime.updated_at || cachedTime.created_at))
      || run.updated_at;
    const caption = element(
      "figcaption",
      "",
      `往次 · ${statusUi(run.status).label}`
        + (displayTime ? ` · ${formatRecentTime(displayTime)}` : ""),
    );
    if (failed) {
      caption.prepend(element("span", "artifact-history-badge", "失败"));
    }
    const open = element("button", "quiet-button", "查看这一版");
    open.type = "button";
    // 刻意**不**按 state.busy 置灰：它是纯导航，而终态运行不会再轮询 ——
    // 置灰后就没有下一次重绘把它恢复，用户看到的就是「点不了」
    // （2026-09-17 实测踩到）。busy 期间的保护由 viewRecentRun 内部负责。
    open.addEventListener("click", () => viewRecentRun(run.run_id));
    caption.append(open);
    card.append(caption);
    const artifacts = artifactHistoryCache.get(run.run_id) || [];
    const artifact = artifacts.find(
      (item) => item && item.preview_url && item.kind !== "file",
    );
    // 失败尝试**只给看/切过去看**，不给下载、不给「送结果表」入口 ——
    // 它没有可交付的成片，给了会让人以为这一版能直接用。
    // （下载/导出入口在主面板，由 run 状态决定；历史格子这里一个都不加。）
    if (failed) {
      const reason = failureReasonForRun(run);
      const reasonText = reason
        || (artifactHistoryFailedAt.has(run.run_id)
          // 「这次运行的详情没拉回来」和「真的没记原因」是两件事，分开说。
          ? "暂时没取到，点「查看这一版」打开。"
          : "未记录失败原因");
      card.append(element(
        "p",
        "artifact-history-reason",
        `失败原因：${reasonText}`,
      ));
      // 悬停也能看到（title 是普通属性，极简假 DOM 下同样安全）。
      card.title = `失败原因：${reasonText}`;
    }
    if (!artifact) {
      if (!failed) {
        // 「拉失败」和「真的没有成片」是两件事，不能都说成「这一版没有成片。」
        card.append(element(
          "p",
          "bitable-task-meta",
          artifactHistoryFailedAt.has(run.run_id)
            ? "预览暂时没取到，点「查看这一版」打开。"
            : "这一版没有成片。",
        ));
      }
      return card;
    }
    if (artifact.kind === "video") {
      const video = document.createElement("video");
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.muted = true;
      video.src = agentUrl(artifact.preview_url);
      card.prepend(video);
    } else {
      const image = document.createElement("img");
      image.alt = artifact.artifact_id;
      image.loading = "lazy";
      image.src = agentUrl(artifact.preview_url);
      // ① 往次成片的缩略图同样单击预览。
      attachSingleClickPreview(image, agentUrl(artifact.preview_url));
      card.prepend(image);
    }
    return card;
  }

  function renderArtifactReview(view) {
    // 历史成片独立于当前这条的成片状态：即使这次失败/还没出片，也要能看到往次的。
    const history = artifactHistorySnapshot();
    // 当前 run 可能**还没进最近列表**（刚重跑出来的新版）—— siblingRuns 需要先在
    // 列表里找到自己、拿到 record_id，否则直接返回空 → "往次成片"整块消失
    //（用户 2026-09-18：「为什么在审核界面看不到往次成片」）。
    // 补拉一次列表并重绘；每个 run 只补一次，避免列表里始终没有它时死循环。
    const knownRuns = state.bitable.recentRuns || [];
    const currentKnown = knownRuns.some((run) => run?.run_id === state.runId);
    // 列表为空时**也要**补拉（那正是最需要的时候）；每个 run 只补一次，避免死循环。
    if (!currentKnown && state.historyRefetchRunId !== state.runId) {
      state.historyRefetchRunId = state.runId;
      loadRecentRuns({ silent: true }).then(() => {
        state.artifactPreviewSignature = null;
        if (state.view) render(state.view);
      });
    }
    // 这条记录**完整**的历次尝试：窗口（最近 10 条任务 / 800 条 run）之外的老记录，
    // 失败尝试只有这里才看得到。拉回来只喂「往次成片」滑条，全局任务记录列表仍用
    // 原来那份窗口数据；每个 run 只拉一次，失败的在退避窗口内不再重试。
    // 只对多维表格运行拉：直接创建的自由运行不在生产表里，问了必然是 404。
    if (state.runMode === "bitable" && needsRecordRuns(state.runId)) {
      ensureRecordRuns(state.runId).then((loaded) => {
        if (!loaded) return;
        state.artifactPreviewSignature = null;
        if (state.view) renderArtifactReview(state.view);
      });
    }
    // 只在**真的有东西要拉**时才补数据并重画一次：否则会变成
    // 「重画 → 缓存已命中 → 再重画」的微任务死循环（实测把测试跑挂了）。
    // 失败过的版本在退避窗口内不算「要拉」，否则同样是死循环。
    const historyRetryAt = Date.now() - ARTIFACT_HISTORY_RETRY_MS;
    const missingHistory = history.siblings.filter((run) => {
      if (artifactHistoryCache.has(run.run_id)) return false;
      const failedAt = artifactHistoryFailedAt.get(run.run_id);
      return failedAt === undefined || failedAt <= historyRetryAt;
    });
    if (missingHistory.length) {
      ensureArtifactHistory(missingHistory.map((run) => run.run_id)).then(() => {
        state.artifactPreviewSignature = null;
        if (state.view) renderArtifactReview(state.view);
      });
    }
    const artifacts = Array.isArray(view.artifacts) ? view.artifacts : [];
    const canReviewArtifacts = Boolean(
      ARTIFACT_REVIEWABLE_STATUSES.has(view.status) && !view.delivery
    );
    // 结果表链接常驻在「成片与结果」面板：只要运行已有成片或已交付，
    // 就把共享结果表地址展示出来，避免导出后链接一闪而过。
    const resultTableUrl = (artifacts.length > 0 || view.delivery)
      ? (view.result_table_url || view.delivery?.result_table_url || "")
      : "";
    if (resultTableUrl) {
      artifactResultLink.href = resultTableUrl;
      artifactResultLink.hidden = false;
    } else {
      artifactResultLink.removeAttribute("href");
      artifactResultLink.hidden = true;
    }
    const validArtifactTaskIds = new Set(
      artifacts.map((artifact) => artifact.task_id)
    );
    if (
      canReviewArtifacts
      && artifacts.length > 0
      && state.artifactReviewRunId !== view.run_id
    ) {
      const decision = view.artifact_review?.decision;
      const restoredTaskIds = Array.isArray(decision?.task_ids)
        ? decision.task_ids.filter((taskId) => validArtifactTaskIds.has(taskId))
        : [];
      state.artifactReviewRunId = view.run_id;
      state.artifactRetryTaskIds = new Set(restoredTaskIds);
      const restoredFeedback = view.artifact_review?.feedback;
      if (typeof restoredFeedback === "string") {
        artifactReviewFeedback.value = restoredFeedback.trim();
      }
    }
    if (canReviewArtifacts && artifacts.length > 0) {
      for (const taskId of [...state.artifactRetryTaskIds]) {
        if (!validArtifactTaskIds.has(taskId)) {
          state.artifactRetryTaskIds.delete(taskId);
        }
      }
    } else if (view.delivery || TERMINAL_RUN_STATUSES.has(view.status)) {
      state.artifactRetryTaskIds.clear();
    }
    // 成片预览里的 <video> 重建成本高，而审批页每 1 秒轮询一次。若成片与
    // 状态都没变就跳过重绘，否则视频元素会被反复销毁重建，导致卡顿/一直加载。
    const signature = JSON.stringify({
      status: view.status,
      // busy 必须进签名：卡片上的按钮按 busy 置灰，而重绘只在签名变化时发生 ——
      // 少了它，「查看这一版」会在选中运行的瞬间被置灰后再也回不来（实测踩到）。
      busy: state.busy,
      artifacts: artifacts.map((artifact) => [
        artifact.artifact_id,
        artifact.preview_url,
        artifact.kind,
        artifact.size,
      ]),
      history: history.signature,
    });
    if (signature === state.artifactPreviewSignature) {
      return;
    }
    state.artifactPreviewSignature = signature;

    // 预览面板**常驻**：本次还没成片（审批中 / 生成中 / 失败）也留着，
    // 往次成片继续看得到 —— 用户明确要求「重跑的时候也能看到预览」。
    artifactReview.hidden = false;
    artifactReviewFeedbackBox.hidden = !canReviewArtifacts;
    // 失败/取消没有成片可勾选，但**重跑入口只在预览页**（任务记录里已删掉），
    // 所以这里必须给它一个「重跑这一版」，否则失败的任务没地方重跑。
    const canRerunThisVersion = ["failed", "cancelled"].includes(view.status);
    artifactReviewActions.hidden = !(canReviewArtifacts || canRerunThisVersion);
    confirmArtifactsButton.hidden = !canReviewArtifacts;
    adjustArtifactsButton.hidden = !canReviewArtifacts;
    rerunArtifactsButton.hidden = !canRerunThisVersion;
    if (artifacts.length > 0) {
      artifactReviewMessage.textContent = canReviewArtifacts
        ? "查看生成素材，确认满意后导出到多维表格「结果」列。"
        : view.status === "delivery_failed"
          ? "素材已生成但结果表写入失败，可继续查看素材并在底部重新写入。"
          : "视频已生成完成，可继续查看；如需回写飞书，请点击下方「导出到结果表」。";
    } else if (["failed", "cancelled"].includes(view.status)) {
      artifactReviewMessage.textContent = view.status === "cancelled"
        ? "本次运行已取消，未生成成片。"
        : "本次运行未生成成片，请在下方的失败原因中查看详情。";
    } else {
      artifactReviewMessage.textContent =
        "本次运行还没有成片（审批或生成中），下面是往次成片。";
    }
    const artifactNodes = artifacts.map((artifact) => {
      const card = element("figure", "artifact-card");
      const label = artifact.kind === "video" ? "视频" : "图片";
      const size = typeof artifact.size === "number"
        ? `${(artifact.size / 1024 / 1024).toFixed(1)} MB`
        : "—";
      // 卡片必须标出「这是第几镜的第几条」：以前只写「视频 · 5.4 MB」，
      // 多条成片长得一模一样，用户根本不知道该勾哪一条重跑
      //（2026-09-23：「卡片顺序对不上分镜顺序，不知道该勾哪一条」）。
      const shotLabel = artifactShotLabel(artifact);
      const caption = element("figcaption", "", `${label} · ${size}`);
      if (shotLabel) {
        caption.prepend(element("span", "artifact-shot-label", shotLabel));
      }
      if (canReviewArtifacts) {
        const choice = element("label", "artifact-retry-choice", "");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = state.artifactRetryTaskIds.has(artifact.task_id);
        checkbox.setAttribute(
          "aria-label",
          shotLabel
            ? `选择重跑 ${shotLabel}（${artifact.task_id}）`
            : `选择重跑任务 ${artifact.task_id}`,
        );
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) state.artifactRetryTaskIds.add(artifact.task_id);
          else state.artifactRetryTaskIds.delete(artifact.task_id);
          updateActionAvailability();
        });
        choice.append(checkbox, element("span", "", "选择此条重跑"));
        caption.append(choice);
      }
      const downloadButton = document.createElement("button");
      downloadButton.type = "button";
      downloadButton.className = "artifact-download-button";
      downloadButton.textContent = artifact.kind === "video" ? "\u4e0b\u8f7d\u89c6\u9891" : "\u4e0b\u8f7d\u56fe\u7247";
      downloadButton.addEventListener("click", () => downloadArtifact(artifact, downloadButton));
      caption.append(downloadButton);
      card.append(caption);
      if (artifact.kind === "video") {
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "metadata";
        video.playsInline = true;
        video.muted = true;
        video.src = agentUrl(artifact.preview_url);
        card.prepend(video);
      } else {
        const image = document.createElement("img");
        image.alt = artifact.artifact_id;
        image.loading = "lazy";
        image.src = agentUrl(artifact.preview_url);
        // ① 成片缩略图单击预览（卡片上的操作是勾选/下载，都在 figcaption 里，不冲突）。
        attachSingleClickPreview(image, agentUrl(artifact.preview_url));
        card.prepend(image);
      }
      return card;
    });
    const historyNodes = history.siblings.length
      ? [
          element("div", "artifact-history-divider", "历史生成"),
          ...history.siblings.map((run) => artifactHistoryCard(run)),
        ]
      : [];
    artifactList.replaceChildren(...artifactNodes, ...historyNodes);
  }

  //: 审查员风险提示默认只铺前几条：审查员的工作方式是「逐条列举遗漏」，提示词里
  //: 没有任何条数上限（AuditReport.issues 是无界数组，_normalize_audit_report
  //: 只规整措辞、不去重不截断），实测 11~53 条 —— 文档分镜表列越多、通用要求越
  //: 细，条目越多（用户 2026-09-22：「为什么有时候能显示 50 多条」）。
  //: 全铺出来会把审批页刷成一堵墙，所以默认 5 条 + 「展开全部」。
  const AUDIT_PREVIEW_COUNT = 5;
  let auditIssuesExpanded = false;
  let auditIssuesRunId = null;

  function renderAuditIssues(view) {
    const box = byId("audit-issues");
    const list = byId("audit-issues-list");
    const count = byId("audit-issues-count");
    if (!box || !list) return;
    const all = Array.isArray(view?.approval?.audit_issues)
      ? view.approval.audit_issues.filter(
        (issue) => typeof issue === "string" && issue.trim(),
      )
      : [];
    // 换一条运行就收起，别把上一条的展开状态带过去。
    if (auditIssuesRunId !== (view?.run_id ?? null)) {
      auditIssuesRunId = view?.run_id ?? null;
      auditIssuesExpanded = false;
    }
    const overflow = Math.max(0, all.length - AUDIT_PREVIEW_COUNT);
    const visible = auditIssuesExpanded || !overflow
      ? all
      : all.slice(0, AUDIT_PREVIEW_COUNT);
    list.replaceChildren(...visible.map((issue) => element("li", "", issue)));
    if (count) count.textContent = `（${all.length} 条）`;
    box.hidden = all.length === 0;
    // 默认展开：它的价值就在于「批准之前看见」，整块藏起来等于没提示。
    if (all.length) box.open = true;
    if (auditIssuesMore) {
      auditIssuesMore.hidden = overflow === 0;
      auditIssuesMore.textContent = auditIssuesExpanded
        ? `只看前 ${AUDIT_PREVIEW_COUNT} 条`
        : `展开全部 ${all.length} 条`;
    }
  }

  if (auditIssuesMore) {
    auditIssuesMore.addEventListener("click", () => {
      auditIssuesExpanded = !auditIssuesExpanded;
      if (state.view) renderAuditIssues(state.view);
    });
  }

  function render(view, { refreshTasks = true } = {}) {
    state.view = view;
    renderRecentRuns();
    const statusInfo = statusUi(view.status);
    const statusLabel = statusLabelWithProgress(view, statusInfo.label);
    const statusBadge = byId("status-badge");
    statusBadge.textContent = statusLabel;
    statusBadge.dataset.tone = statusInfo.tone;
    byId("run-status").textContent = statusLabel;
    runGuidance.dataset.tone = statusInfo.tone;
    runGuidance.replaceChildren(
      element("strong", "", statusLabel),
      element("span", "", statusInfo.action),
    );
    byId("thread-id").textContent = view.thread_id;
    const latestEvent = (view.events || []).at(-1);
    byId("current-node").textContent = BitableState.runStage(view) || latestEvent?.node || "—";
    const elapsed = BitableState.runElapsedBreakdown(view);
    if (elapsed.systemMs === null) {
      byId("run-duration").textContent = "—";
    } else {
      const human =
        elapsed.humanMs > 0
          ? ` + 你审阅 ${formatDuration(elapsed.humanMs)}`
          : "";
      byId("run-duration").textContent = `${formatDuration(elapsed.systemMs)}${human}`;
    }
    byId("document-title").textContent = view.approval.document_title || "未命名文档";
    byId("source-link").href = view.source_url;
    byId("document-revision").textContent = view.approval.revision ?? "—";
    byId("document-summary").textContent = view.approval.document_summary || "";
    const deliveryTarget = byId("delivery-target");
    const delivery = view.delivery || {};
    deliveryTarget.replaceChildren();
    if (delivery.target_type === "production_result_record" && delivery.result_table_url) {
      const link = element("a", "", "打开结果表");
      link.href = delivery.result_table_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      deliveryTarget.append("生成结果已写入：", link);
      deliveryTarget.hidden = false;
    } else {
      deliveryTarget.hidden = true;
    }
    byId("langsmith-warning").hidden = !view.privacy?.langsmith_tracing;
    renderEvents(view.events);

        const providerNames = {
          "seedance2.0": "Seedance 2.0",
          "seedance2.5": "Seedance 2.5",
          seedance: "Seedance",
          chiyun: "Chiyun",
          volcengine_portrait: "真人视频",
          aiport: "本地模型",
        };
    const executionErrors = (view.execution_records || [])
      .filter((record) => record?.error?.message || record?.status === "timed_out")
      .map((record) => {
        const provider = providerNames[record.provider] || record.provider || "生成服务";
        if (record.status === "timed_out" && !record.error?.message) {
          return `${provider}：生成服务等待超时，请稍后重新运行`;
        }
        const code = record.error.code ? `（${record.error.code}）` : "";
        return `${provider}：${record.error.message}${code}`;
      });
    const executionErrorBox = byId("execution-errors");
    executionErrorBox.textContent = executionErrors.length
      ? `生成失败：${executionErrors.join("；")}`
      : "";
    executionErrorBox.hidden = executionErrors.length === 0;

    const ingestIssueRecords = view.approval.ingest_issue_records || [];
    const blockingIngestIssues = ingestIssueRecords
      .filter((record) => record.severity === "blocking")
      .map((record) => record.display_message);
    let issues = (view.approval.validation_issues || [])
      .filter((issue) => !blockingIngestIssues.includes(issue));
    const lastError = view.last_error;
    if (lastError && lastError.message) {
      // 早期持久化的失败记录可能只有通用占位文案，这里按错误类别补齐可读原因，
      // 让历史任务点进去能直接看懂为什么没有成片。
      const genericMessage = lastError.message === GENERIC_FAILURE_MESSAGE;
      const categoryFallback = FAILURE_CATEGORY_FALLBACK;
      const message = genericMessage
        ? (categoryFallback[lastError.category]
          || lastError.technical_detail
          || lastError.message)
        : lastError.message;
      issues = [
        message,
        ...issues.filter(
          (issue) => issue !== "审批校验状态无效，请重新读取后再审批",
        ),
      ];
    }
    const issueBox = byId("validation-issues");
    issueBox.textContent = issues.join("；");
    issueBox.hidden = issues.length === 0;
    renderAuditIssues(view);
    // 文档读取权限不足时，给出可执行的授权步骤，而不是只丢一句“权限不足”。
    const permissionError = lastError && lastError.category === "permission_error";
    if (permissionError) {
      const isWiki = /\/wiki\//.test(view.source_url || "");
      permissionGuide.hidden = false;
      permissionGuideIntro.textContent = isWiki
        ? "请把飞书机器人「飞书任务agent」添加为该知识库的协作者（分享 → 管理协作者 → 添加协作者），权限设为「可阅读」，然后重新运行。"
        : "请把飞书机器人「飞书任务agent」添加为该文档的协作者（分享 → 管理协作者 → 添加协作者），权限设为「可阅读」，然后重新运行。";
    } else {
      permissionGuide.hidden = true;
    }
    const blockingIngestBox = byId("blocking-ingest-issues");
    blockingIngestBox.textContent = blockingIngestIssues.length
      ? `文档读取阻塞：${blockingIngestIssues.join("；")}`
      : "";
    blockingIngestBox.hidden = blockingIngestIssues.length === 0;
    const assetIngestIssues = ingestIssueRecords
      .filter((record) => record.severity === "asset");
    const assetIngestBox = byId("asset-ingest-issues");
    const assetIngestList = byId("asset-ingest-issue-list");
    const kindLabel = { image: "图片", video: "视频", file: "文件" };
    const reasonText = {
      temporary: "暂时下载失败，可点击重新读取",
      permission: "无权读取，请检查飞书素材权限",
      unavailable: "不存在或已失效",
      invalid: "格式或内容无法读取",
      save_failed: "已下载但本地保存失败",
      unknown: "下载失败",
    };
    assetIngestList.replaceChildren(...assetIngestIssues.map((record) => {
      if (!record.asset_kind || !record.failure_reason) {
        return element("div", "asset-issue-row", record.display_message);
      }
      const label = kindLabel[record.asset_kind] || "素材";
      const asset = record.asset_id ? `${record.asset_id}：` : "";
      return element(
        "div",
        "asset-issue-row",
        `${label} ${asset}${reasonText[record.failure_reason] || reasonText.unknown}`,
      );
    }));
    const recoveredFailedAssets = assetIngestIssues.length === 0
      && (view.events || []).some(
        (event) => event.node === "retry_failed_assets" && event.status === "completed",
      );
    retryFailedAssetsFeedback.textContent = recoveredFailedAssets
      ? "失败素材已全部恢复，无需重新读取"
      : "";
    assetIngestBox.hidden = assetIngestIssues.length === 0 && !recoveredFailedAssets;
    const visionIssues = view.approval.vision_issues || [];
    const visionIssueBox = byId("vision-issues");
    visionIssueBox.textContent = visionIssues.length
      ? `素材识别失败（不影响其他素材）：${visionIssues.join("；")}`
      : "";
    visionIssueBox.hidden = visionIssues.length === 0;
    renderCoverage(view);
    if (refreshTasks) {
      // 正在编辑提示词/素材时**绝不重建**任务列表：replaceChildren 会销毁输入框，
      // 正在敲的内容和光标都会丢（用户 2026-09-18：「改提示词会被经常打断」）。
      // 挂起这次刷新，等失焦后再补上。
      if (taskEditorFocused()) {
        pendingTaskRefresh = true;
      } else {
        taskList.replaceChildren(...(view.approval.tasks || []).map(renderTask));
        pendingTaskRefresh = false;
      }
    }
    renderArtifactReview(view);
    renderTakeFindings(view);
    updateActionAvailability();
  }

  /**
   * 服务端数据的"有意义签名"：只取会改变界面的字段。
   *
   * 刻意**不含** updated_at / 运行时长这类每秒都在变的字段 —— 那些由
   * `renderRunDuration` 单独更新，否则每秒都会判定为"变了"、白白重绘整页
   * （用户 2026-09-18：「前端轮询效率太低了，就改成变化的时候才更新不行吗」）。
   */
  function runViewSignature(view) {
    if (!view) return "";
    return JSON.stringify({
      // 带上 run 身份：切换任务时一定重绘（不用在每处切任务的地方手动重置）。
      run: view.run_id || view.thread_id || "",
      status: view.status,
      events: (view.events || []).map(
        (event) => [event.node, event.status, event.summary, event.created_at],
      ),
      approval: view.approval,
      artifacts: view.artifacts,
      execution_records: view.execution_records,
      delivery: view.delivery,
      last_error: view.last_error,
      vision_issues: view.vision_issues,
      artifact_review_decision: view.artifact_review_decision,
    });
  }

  /** 数据没变时唯一要动的界面元素：运行时长。 */
  function renderRunDuration(view) {
    const target = byId("run-duration");
    if (!target) return;
    const elapsed = BitableState.runElapsedBreakdown(view);
    if (elapsed.systemMs === null) {
      target.textContent = "—";
      return;
    }
    const human =
      elapsed.humanMs > 0 ? ` + 人工等待 ${formatDuration(elapsed.humanMs)}` : "";
    target.textContent = `${formatDuration(elapsed.systemMs)}${human}`;
  }

  async function poll(force = false, resetDraft = false) {
    // 任务停在"等人操作"（待审批 / 待审核）时没什么可等的，降频到每 5 秒请求一次；
    // 生成中仍保持每秒。配合上面的签名判断，停在审批页时界面几乎完全静止。
    pollTicks += 1;
    if (
      !force &&
      isParkedStatus(state.view?.status) &&
      pollTicks % PARKED_POLL_EVERY !== 0
    ) {
      return;
    }
    if (!state.runId || (state.busy && !force)) return;
    const requestedRunId = state.runId;
    try {
      const serverView = await api(`/api/runs/${requestedRunId}`);
      if (state.runId !== requestedRunId) return;
      // 正在看的这条运行状态变了 —— 这就是「状态更新的时候刷新」：立刻对一次
      // 任务记录（同一状态反复轮询时什么都不做）。
      const previous = lastViewedRun;
      const statusChanged =
        previous.runId === requestedRunId && previous.status !== serverView.status;
      lastViewedRun = { runId: requestedRunId, status: serverView.status };
      // 自己刚保存的变化：先认领（对齐服务端身份），这样下面的身份比对相等、
      // 不会弹冲突也不会重建任务列表。
      if (selfSavedPending) {
        selfSavedPending = false;
        state.review = ReviewState.adoptSelfSavedView(state.review, serverView);
      }
      const previousReview = state.review;
      const nextReview = resetDraft
        ? ReviewState.mergeServerView(ReviewState.createReviewState(), serverView)
        : ReviewState.mergeServerView(state.review, serverView);
      const refreshTasks = resetDraft || ReviewState.shouldRefreshTaskEditor(
        previousReview,
        nextReview,
        taskList.childElementCount > 0,
      );
      state.review = nextReview;
      // 只有服务端数据**真的变了**才整体重绘；没变就只更新运行时长。
      // （以前每秒无条件 render，既浪费又把用户正在编辑的内容卷进去。）
      const signature = runViewSignature(serverView);
      if (resetDraft || signature !== lastRenderedSignature) {
        lastRenderedSignature = signature;
        render(ReviewState.draftView(state.review), { refreshTasks });
      } else {
        renderRunDuration(serverView);
      }
      if (TERMINAL_RUN_STATUSES.has(serverView.status)) {
        stopPolling();
        pollingNote.textContent = "任务已结束，可开始下一任务或重跑。";
        await loadRecentRuns({ silent: true });
      } else {
        if (statusChanged) await loadRecentRuns({ silent: true });
        pollingNote.textContent = statusUi(serverView.status).action;
      }
    } catch (error) {
      if (state.runId === requestedRunId) showError(error);
    }
  }

  async function viewRecentRun(runId) {
    if (state.busy) return;
    stopPolling();
    setBusy(true);
    clearError();
    try {
      state.runId = runId;
      state.runMode = "bitable";
      state.review = ReviewState.createReviewState();
      state.referenceMutations = ReferenceMutationState.createState();
      state.artifactPreviewSignature = null;
          state.artifactRetryTaskIds = new Set();
      await poll(true);
      startPolling();
      renderRecentRuns();
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
      renderRecentRuns();
    }
  }

  /** 重跑成功后的文案：必须如实说明这次有没有改写提示词。 */
  function rerunToastMessage(feedback) {
    return feedback
      ? "已按返工要求优化提示词，请审核后批准"
      : "未填返工要求，提示词原样保留，请审核后批准";
  }

  /** 返工要求输入框里现在的内容（没有输入框就当作空 = 原样重跑）。 */
  function currentReworkFeedback() {
    return artifactReviewFeedback ? artifactReviewFeedback.value.trim() : "";
  }

  /**
   * 「重新运行」与「重跑选中任务」**共用**的重跑逻辑（用户口径 2026-09-24）。
   *
   * 状态机（两条入口完全一致）：
   *   ① 复用上一版**已批准的计划** —— 不重新规划，省一次规划；
   *   ② 返工要求有内容 → 后端调融合把要求并进提示词；**没内容就一个字都不改**
   *      （也不调模型、不产生费用）；
   *   ③ 新运行停在**待审批**，用户能看到改后的提示词/参数，还能继续编辑；
   *   ④ 点「批准并开始生成」之后才真正提交生成。
   *
   * 与「退回修改计划」（整份计划**重新规划**）分工不同；以前这里走的是
   * `clone_approved_plan` 之后直接进执行，用户没有确认机会。
   */
  async function rerunBitableTask(runId = state.runId, options = {}) {
    // 以前这里是静默 return —— 按钮可见却点不动时用户只会说「点了没反应」。
    if (!runId) {
      showActionToast("当前没有选中的任务，先在上方任务列表里选一条再重跑。");
      return;
    }
    if (state.busy || rerunRequesting) {
      showActionToast("上一次操作还在提交中，请稍候再点「重新运行」。", { timeout: 4000 });
      return;
    }
    const taskIds = Array.isArray(options.taskIds)
      ? options.taskIds.filter(Boolean)
      : [...state.artifactRetryTaskIds];
    const feedback = options.feedback === undefined
      ? currentReworkFeedback()
      : String(options.feedback || "").trim();
    // 勾了成片 → 只重跑勾中的那几条（`rerun-selected`）；没勾 → 整份计划（`rerun`）。
    // 两条后端入口都会「融合改写（有要求时）→ 停在待审批」。
    const endpoint = taskIds.length
      ? `/api/bitable/runs/${encodeURIComponent(runId)}/rerun-selected`
      : `/api/bitable/runs/${encodeURIComponent(runId)}/rerun`;
    const body = taskIds.length
      ? { action: "adjust", feedback, task_ids: taskIds }
      : { feedback };
    setRerunRequesting(true);
    clearError();
    try {
      const created = await api(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      state.runId = created.run_id;
      state.runMode = "bitable";
      state.review = ReviewState.createReviewState();
      state.referenceUploads = ReferenceUploadState.createState();
      state.referenceMutations = ReferenceMutationState.createState();
      state.artifactPreviewSignature = null;
      state.artifactRetryTaskIds = new Set();
      state.artifactReviewRunId = null;
      if (artifactReviewFeedback) artifactReviewFeedback.value = "";
      await poll(true);
      startPolling();
      await loadRecentRuns();
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
      showActionToast(rerunToastMessage(feedback), { tone: "info", timeout: 12000 });
    } catch (error) {
      reportActionFailure(error);
      await loadRecentRuns();
    } finally {
      setRerunRequesting(false);
      renderRecentRuns();
    }
  }

  async function submitDecision(action) {
    if (!state.runId || state.busy) return;
    let body = { action };
    if (action === "reject") body.feedback = byId("reject-feedback").value;
    try {
      if (action === "approve") {
        const submission = ReviewState.beginApprovalSubmit(state.review);
        state.review = submission.state;
        body = submission.payload;
      }
      setBusy(true);
      clearError();
      await api(`/api/runs/${state.runId}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (action === "approve") {
        state.review = ReviewState.completeApprovalSubmit(state.review);
      } else {
        state.review = ReviewState.createReviewState();
      }
      await poll(true);
    } catch (error) {
      if (action === "approve" && ReviewState.isSubmitting(state.review)) {
        state.review = ReviewState.failApprovalSubmit(state.review);
        state.view = ReviewState.draftView(state.review);
      }
      reportActionFailure(error);
    } finally {
      setBusy(false);
    }
  }

  /**
   * 审批页**原地**用 AI 重写所选任务的提示词。
   *
   * 与「退回修改计划」分工不同：不重新规划、不新建运行、不动分镜结构，只把勾中的
   * 那几条任务的提示词按这次要求改写一遍，改完仍停在审批页给用户过目。
   */
  async function optimizeSelectedPrompts() {
    const taskIds = [...ReviewState.selectedTaskIds(state.review)];
    const feedback = promptOptimizeFeedback.value.trim();
    if (!taskIds.length) {
      reportActionFailure(new Error("请先在上方任务列表里勾选要优化的任务"));
      return;
    }
    if (!feedback) {
      reportActionFailure(new Error("请填写优化要求"));
      return;
    }
    setRerunRequesting(true);
    clearError();
    try {
      await api(`/api/runs/${state.runId}/optimize-prompts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_ids: taskIds, feedback }),
      });
      promptOptimizeFeedback.value = "";
      // 服务端已经改写了这几条提示词：丢掉它们在本地未保存的草稿，否则界面会被
      // 旧草稿遮住新提示词。勾选**保留**（用户多半要接着批准所选任务）。
      const remainingEdits = { ...state.review.editsByTaskId };
      for (const taskId of taskIds) delete remainingEdits[taskId];
      state.review = { ...state.review, editsByTaskId: remainingEdits };
      // 这是我们自己刚改的：告诉下一次轮询「这个变化是自己造成的」，
      // 别当成别人改了而弹「服务端计划已更新」。
      selfSavedPending = true;
      await poll(false);
    } catch (error) {
      reportActionFailure(error);
    } finally {
      setRerunRequesting(false);
    }
  }

  async function submitArtifactReview(action) {
    if (
      !state.runId
      || state.busy
      || !state.view
      || !ARTIFACT_REVIEWABLE_STATUSES.has(state.view.status)
      || state.view.delivery
    ) return;
    if (action === "adjust") {
      // 「重跑选中任务」= 与底部「重新运行」**同一条逻辑**：复用已批准计划 →
      // （有返工要求就融合改写提示词，没有就一个字不改）→ 停在审批页等用户确认。
      //
      // 以前这里走 `/api/runs/{id}/artifact-review`（**原地**重跑）：提示词改完
      // 立刻开始生成，用户根本没机会确认（用户 2026-09-24：「我说的是我的返工
      // 要求重跑选中任务应该是优化我的提示词然后让我确认啊」）。
      if (!state.artifactRetryTaskIds.size) {
        reportActionFailure(new Error("请至少选择一条需要重跑的任务"));
        return;
      }
      await rerunBitableTask(state.runId, {
        taskIds: [...state.artifactRetryTaskIds],
        feedback: currentReworkFeedback(),
      });
      return;
    }
    const body = { action };
    setRerunRequesting(true);
    clearError();
    try {
      await api(`/api/runs/${state.runId}/artifact-review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await poll(true);
    } catch (error) {
      reportActionFailure(error);
    } finally {
      setRerunRequesting(false);
    }
  }

  async function cancelActiveRun() {
    if (!state.runId || state.busy) return;
    setBusy(true);
    clearError();
    try {
      await api(`/api/runs/${state.runId}/cancel`, { method: "POST" });
      await poll(true);
    } catch (error) {
      reportActionFailure(error);
    } finally {
      setBusy(false);
    }
  }

  byId("reject-button").addEventListener("click", () => submitDecision("reject"));
  byId("cancel-button").addEventListener("click", () => {
    if (CANCELLABLE_RUN_STATUSES.has(state.view?.status)) {
      cancelActiveRun();
    } else {
      submitDecision("cancel");
    }
  });
  byId("approve-button").addEventListener("click", () => submitDecision("approve"));
  confirmArtifactsButton.addEventListener("click", () => submitArtifactReview("confirm"));
  adjustArtifactsButton.addEventListener("click", () => submitArtifactReview("adjust"));
  rerunArtifactsButton.addEventListener("click", () => rerunBitableTask());
  artifactReviewFeedback.addEventListener("input", updateActionAvailability);
  if (optimizePromptsButton) {
    optimizePromptsButton.addEventListener("click", () => optimizeSelectedPrompts());
  }
  if (promptOptimizeFeedback) {
    promptOptimizeFeedback.addEventListener("input", updateActionAvailability);
  }
  rerunButton.addEventListener("click", () => rerunBitableTask());
  retryFailedAssetsButton.addEventListener("click", async () => {
    if (!state.runId || state.busy || state.view?.status !== "waiting_approval") return;
    setBusy(true);
    clearError();
    retryFailedAssetsFeedback.textContent = "正在重新读取失败素材…";
    try {
      const result = await api(`/api/runs/${state.runId}/retry-failed-assets`, {
        method: "POST",
      });
      retryFailedAssetsFeedback.textContent = result.remaining_count
        ? `已恢复 ${result.recovered_count} 个，仍有 ${result.remaining_count} 个读取失败`
        : `已恢复 ${result.recovered_count} 个素材`;
      await poll(true);
    } catch (error) {
      retryFailedAssetsFeedback.textContent = "重新读取失败";
      reportActionFailure(error);
    } finally {
      setBusy(false);
    }
  });
  retryDeliveryButton.addEventListener("click", async () => {
    if (!state.runId || state.busy) return;
    const url = state.runMode === "bitable"
      ? `/api/bitable/runs/${state.runId}/retry-delivery`
      : `/api/runs/${state.runId}/retry-delivery`;
    if (state.runMode !== "bitable") {
      const started = await mutate(url, { method: "POST" });
      if (started && state.view?.status === "delivering") startPolling();
      return;
    }
    state.bitable = BitableState.retryStarted(state.bitable, state.runId);
    setBusy(true);
    clearError();
    try {
      await api(url, { method: "POST" });
      state.bitable = BitableState.retrySucceeded(state.bitable);
      await poll(true);
      if (state.view?.status === "delivering") startPolling();
    } catch (error) {
      state.bitable = BitableState.retryFailed(state.bitable, error.message);
      reportActionFailure(error);
    } finally {
      setBusy(false);
    }
  });
  discardButton.addEventListener("click", () => {
    state.review = ReviewState.discardLocalChanges(state.review);
    clearError();
    render(ReviewState.draftView(state.review));
  });
  if (PlannerPromptState && plannerPromptButton) {
    plannerPromptButton.addEventListener("click", () => {
      state.plannerPrompt = PlannerPromptState.openPromptEditor(state.plannerPrompt);
      renderPlannerPrompt();
      renderAdvancedSettings();
      plannerPromptText.focus();
    });
    byId("planner-prompt-close").addEventListener("click", closePlannerPromptEditor);
    plannerPromptModal.addEventListener("click", (event) => {
      if (event.target === plannerPromptModal) closePlannerPromptEditor();
    });
    plannerPromptText.addEventListener("input", () => {
      state.plannerPrompt = PlannerPromptState.markPromptDirty(
        state.plannerPrompt, plannerPromptText.value,
      );
      renderPlannerPrompt();
    });
    plannerPromptSave.addEventListener("click", savePlannerPrompt);
    plannerPromptReset.addEventListener("click", resetPlannerPrompt);
    if (advancedSettingsSave) advancedSettingsSave.addEventListener("click", saveProviderPreferences);
  }
  [
    ["bitable-tasks-toggle", "bitable-tasks-body"],
    ["recent-runs-toggle", "recent-run-list"],
  ].forEach(([toggleId, bodyId]) => {
    const toggle = byId(toggleId);
    const body = byId(bodyId);
    // 页面结构与脚本版本对不上时（浏览器缓存了旧 HTML）宁可不折叠，也别抛错。
    if (!toggle || !body) return;
    toggle.addEventListener("click", () => {
      body.hidden = !body.hidden;
      toggle.setAttribute("aria-expanded", String(!body.hidden));
    });
  });
  currentRunSwitcher.addEventListener("change", async () => {
    const runId = currentRunSwitcher.value;
    if (runId && runId !== state.runId) await viewRecentRun(runId);
  });
  trashButton.addEventListener("click", openTrash);
  trashClose.addEventListener("click", closeTrash);
  trashSearch.addEventListener("input", () => {
    trashState.query = trashSearch.value;
    trashState.page = 1;
    renderArchivedRuns();
  });
  trashStatusFilter.addEventListener("change", () => {
    trashState.status = trashStatusFilter.value;
    trashState.page = 1;
    renderArchivedRuns();
  });
  trashPrev.addEventListener("click", () => {
    if (trashState.page <= 1) return;
    trashState.page -= 1;
    renderArchivedRuns();
  });
  trashNext.addEventListener("click", () => {
    const pageCount = Math.max(1, Math.ceil(filteredArchivedRuns().length / trashState.pageSize));
    if (trashState.page >= pageCount) return;
    trashState.page += 1;
    renderArchivedRuns();
  });
  trashModal.addEventListener("click", (event) => {
    if (event.target === trashModal) closeTrash();
  });
  scanBitableButton.addEventListener("click", scanBitableTasks);
  directRunButton.addEventListener("click", startDirectRun);
  directRunUrl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") startDirectRun();
  });
  categoryTabs.forEach((tab) => {
    tab.addEventListener("click", () => selectBitableCategory(tab.dataset.category));
  });
  updateActionAvailability();
  if (PlannerPromptState) {
    renderPlannerPrompt();
    loadPlannerPrompt();
  }
  // 拖放防误伤（用户 2026-09-29）：把文件拖到「参考素材」节**以外**时，浏览器的默认
  // 行为是**导航到那个本地文件** —— 页面被顶掉，用户的体感就是"拖不了 / 页面坏了"。
  // 在 document 上兜住：拖到哪都不许导航。节内那一套自己 preventDefault，不受影响。
  const carriesFiles = (event) =>
    Array.from(event.dataTransfer?.types || []).includes("Files");
  document.addEventListener("dragover", (event) => {
    if (carriesFiles(event)) event.preventDefault();
  });
  document.addEventListener("drop", (event) => {
    if (carriesFiles(event)) event.preventDefault();
  });
  configureModes();
  startBitableRefresh();
})();
