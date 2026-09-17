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
  // 哪些任务记录展开了「历史版本」（纯界面状态，不参与持久化）。
  const expandedRunGroups = new Set();
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
  const artifactReview = byId("artifact-review");
  const artifactList = byId("artifact-list");
  const artifactReviewMessage = byId("artifact-review-message");
  const artifactResultLink = byId("artifact-result-table-link");
  const artifactReviewFeedbackBox = byId("artifact-review-feedback-box");
  const artifactReviewActions = byId("artifact-review-actions");
  const artifactReviewFeedback = byId("artifact-review-feedback");
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

  function stopPolling() {
    if (state.pollTimer !== null) globalThis.clearInterval(state.pollTimer);
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
    // 同一条多维表格记录的历次尝试叠在同一条任务下（重跑不新开任务记录）。
    const groups = BitableState.groupRecentRuns(runs);
    const signature = JSON.stringify({
      selected: state.runId,
      busy: state.busy,
      expanded: [...expandedRunGroups].sort(),
      groups: groups.map((group) => [
        group.key,
        group.current && group.current.run_id,
        group.current && group.current.status,
        group.current && group.current.active,
        group.current && group.current.result_table_url,
        group.current && group.current.rerunnable,
        group.history.map((run) => [run.run_id, run.status]),
      ]),
    });
    if (recentRunList.dataset.renderSig === signature) return;
    recentRunList.dataset.renderSig = signature;
    const activeCount = runs.filter((run) => run.active).length;
    // 只有真的合并了版本才提「N 个版本」，免得单版本时多一句废话。
    const versionNote =
      runs.length && groups.length !== runs.length
        ? ` · ${runs.length} 个版本`
        : "";
    runHistorySummary.textContent = runs.length
      ? `${activeCount ? `${activeCount} 个进行中 · ` : ""}共 ${groups.length} 条${versionNote}`
      : "进行中与历史任务都在这里";
    const switchOptions = runs.map((run) => {
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

    const nodes = groups.map((group) => {
      const run = group.current;
      if (!run) return element("p", "bitable-empty", "暂无任务记录。");
      const selected = run.run_id === state.runId;
      const row = element("article", `recent-run${selected ? " is-current" : ""}`);
      row.dataset.runId = run.run_id;
      if (group.record_id) row.dataset.recordId = group.record_id;
      const details = element("div", "recent-run-details");
      const versionLabel = group.versions.length > 1
        ? ` · 共 ${group.versions.length} 版`
        : "";
      details.append(
        element("strong", "", group.display_text || run.run_id),
        element(
          "p",
          "bitable-task-meta",
          `${run.active ? "进行中" : "历史"} · ${statusUi(run.status).label}${versionLabel}`,
        ),
      );
      const actions = runActionsFor(run, { selected });
      if (group.history.length) {
        const expanded = expandedRunGroups.has(group.key);
        const toggle = element(
          "button",
          "quiet-button recent-run-history-toggle",
          expanded ? `收起历史（${group.history.length}）` : `历史 ${group.history.length} 次`,
        );
        toggle.type = "button";
        toggle.setAttribute("aria-expanded", String(expanded));
        toggle.addEventListener("click", () => {
          if (expandedRunGroups.has(group.key)) expandedRunGroups.delete(group.key);
          else expandedRunGroups.add(group.key);
          renderRecentRuns();
        });
        actions.append(toggle);
      }
      row.append(details, actions);

      if (group.history.length && expandedRunGroups.has(group.key)) {
        const historyBox = element("div", "recent-run-history");
        group.history.forEach((older, index) => {
          // 版本号从最老的一版数起：最新的一版是「第 N 版」。
          const versionNo = group.versions.length - 1 - index;
          const olderSelected = older.run_id === state.runId;
          const olderRow = element(
            "article",
            `recent-run recent-run-version${olderSelected ? " is-current" : ""}`,
          );
          olderRow.dataset.runId = older.run_id;
          const olderDetails = element("div", "recent-run-details");
          olderDetails.append(
            element(
              "strong",
              "",
              `第 ${versionNo} 版 · ${statusUi(older.status).label}`,
            ),
            element(
              "p",
              "bitable-task-meta",
              formatRecentTime(older.updated_at) || older.run_id,
            ),
          );
          olderRow.append(
            olderDetails,
            runActionsFor(older, { selected: olderSelected }),
          );
          historyBox.append(olderRow);
        });
        row.append(historyBox);
      }
      return row;
    });
    if (!nodes.length) nodes.push(element("p", "bitable-empty", "暂无任务记录。"));
    recentRunList.replaceChildren(...nodes);
  }

  /** 一条运行的按钮组（当前版与历史版共用，保证能力不因分组而缩水）。 */
  function runActionsFor(run, { selected = false } = {}) {
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
    if (run.rerunnable) {
      const rerun = element("button", "quiet-button", "重跑");
      rerun.type = "button";
      rerun.disabled = state.busy;
      rerun.addEventListener("click", () => rerunBitableTask(run.run_id));
      actions.append(rerun);
    }
    if (!run.active) {
      const remove = element("button", "danger", "删除");
      remove.type = "button";
      remove.disabled = state.busy;
      remove.addEventListener("click", () => archiveBitableRun(run.run_id));
      actions.append(remove);
    }
    return actions;
  }

  function formatRecentTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", {
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
      [...(activeRuns || []).slice().reverse().map((run) => ({ ...run, active: true })),
        ...(recentRuns || []).map((run) => ({ ...run, active: false }))]
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
  const RUN_WATCH_INTERVAL_MS = 5000;
  let runWatchTimer = null;

  function needsRunWatch() {
    return (state.bitable.recentRuns || []).some((run) =>
      SELF_PROGRESSING_RUN_STATUSES.has(run.status),
    );
  }

  function stopRunWatch() {
    if (runWatchTimer !== null) globalThis.clearInterval(runWatchTimer);
    runWatchTimer = null;
  }

  /** 有任务在自行推进才起盯守；全都在等人操作就彻底停掉（0 请求）。 */
  function scheduleRunWatch() {
    stopRunWatch();
    if (!needsRunWatch()) return;
    runWatchTimer = globalThis.setInterval(() => {
      if (document.hidden) return;
      if (!needsRunWatch()) {
        stopRunWatch();
        return;
      }
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
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", {
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
      const metadata = [`状态：${archivedStatusLabel(run)}`];
      const updatedAt = formatArchivedTime(run.updated_at);
      if (updatedAt) metadata.push(`删除时间：${updatedAt}`);
      details.append(
        element("strong", "", run.display_text || run.run_id),
        element("p", "bitable-task-meta", metadata.join(" · ")),
      );
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
      && artifactReviewFeedback.value.trim()
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
    adjustArtifactsButton.disabled = state.busy || !canAdjustArtifacts;
    artifactReviewFeedback.disabled = state.busy || !canReviewArtifacts;
    const terminal = TERMINAL_RUN_STATUSES.has(state.view?.status);
    rerunButton.disabled = state.busy
      || state.runMode !== "bitable"
      || !RERUNNABLE_RUN_STATUSES.has(state.view?.status);
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
      }).then(() => poll(false)).catch((error) => showError(error));
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
      render();
    } catch (error) {
      showError(error);
      render();
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

  function referenceRow(task, reference) {
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

    const descriptionText = description
      ? [description.subjects?.join("、"), description.scene, description.probable_role]
          .filter(Boolean)
          .join(" · ")
      : "本地新增图片，尚无视觉描述";
    const descriptionNode = element("div", "reference-description", descriptionText);

    const role = element(
      "div",
      "reference-role",
      reference.role === "first_frame"
        ? "首帧"
        : reference.role === "last_frame"
          ? "尾帧"
          : isVideo ? "参考视频" : isAudio ? "参考音频" : "普通参考图",
    );

    const order = document.createElement("input");
    order.type = "number";
    order.min = "1";
    order.value = reference.order;
    order.setAttribute("aria-label", "图片顺序");
    order.addEventListener("input", () => {
      updateReference(task.task_id, reference.asset_id, { order: Number(order.value) });
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
    const list = element("div", "reference-list");
    [...task.reference_images]
      .sort((a, b) => a.order - b.order)
      .forEach((reference) => list.append(referenceRow(task, reference)));

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
    if (referenceMode === "multi_reference") {
      const upload = element("div", "upload-row");
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = "image/*,video/mp4,video/webm,audio/mpeg,audio/wav,audio/ogg,audio/aac";
      const feedback = ReferenceUploadState.feedback(state.referenceUploads, task.task_id);
      const uploadFeedback = element(
        "p",
        `upload-feedback${feedback ? ` is-${feedback.phase}` : ""}`,
        feedback?.message || "请选择图片、视频或音频后再上传。",
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
      add.addEventListener("click", async () => {
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
      });
      upload.append(fileInput, order, add, uploadFeedback);
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
      box.append(element("div", "rework-compare-label", "改后与改前一致"));
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
      [["true", "开启"], ["false", "关闭"]].forEach(([value, label]) => {
        const option = element("option", "", label);
        option.value = value;
        option.selected = String(Boolean(task.generate_audio)) === value;
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
        videoField("声音", audio, "按需求开启"),
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
    card.append(titleRow, grid, notes, referenceSection(task));
    return card;
  }

  function renderArtifactReview(view) {
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
      artifacts: artifacts.map((artifact) => [
        artifact.artifact_id,
        artifact.preview_url,
        artifact.kind,
        artifact.size,
      ]),
    });
    if (signature === state.artifactPreviewSignature) {
      return;
    }
    state.artifactPreviewSignature = signature;

    const showsArtifacts = [
      "waiting_review", "delivering", "delivery_failed", "succeeded", "completed_with_errors",
    ].includes(view.status) && artifacts.length > 0;
    // 终态但没有任何成片（执行失败/已取消）：不隐藏整块，而是给出明确占位，
    // 避免历史任务点进去后主区域一片空白，让用户误以为「成片预览坏了」。
    const terminalWithoutArtifacts = (
      ["failed", "cancelled"].includes(view.status)
    );
    if (!showsArtifacts && !terminalWithoutArtifacts) {
      artifactReview.hidden = true;
      artifactList.replaceChildren();
      return;
    }
    artifactReview.hidden = false;
    artifactList.replaceChildren();
    if (terminalWithoutArtifacts) {
      artifactReviewFeedbackBox.hidden = true;
      artifactReviewActions.hidden = true;
      artifactReviewMessage.textContent = view.status === "cancelled"
        ? "本次运行已取消，未生成成片。"
        : "本次运行未生成成片，请在下方的失败原因中查看详情。";
      return;
    }
    artifactReviewMessage.textContent = canReviewArtifacts
      ? "查看生成素材，确认满意后导出到多维表格「结果」列。"
      : view.status === "delivery_failed"
        ? "素材已生成但结果表写入失败，可继续查看素材并在底部重新写入。"
        : "视频已生成完成，可继续查看；如需回写飞书，请点击下方「导出到结果表」。";
    artifactReviewFeedbackBox.hidden = !canReviewArtifacts;
    artifactReviewActions.hidden = !canReviewArtifacts;
    artifactList.replaceChildren(...artifacts.map((artifact) => {
      const card = element("figure", "artifact-card");
      const label = artifact.kind === "video" ? "视频" : "图片";
      const size = typeof artifact.size === "number"
        ? `${(artifact.size / 1024 / 1024).toFixed(1)} MB`
        : "—";
      const caption = element("figcaption", "", `${label} · ${size}`);
      if (canReviewArtifacts) {
        const choice = element("label", "artifact-retry-choice", "");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = state.artifactRetryTaskIds.has(artifact.task_id);
        checkbox.setAttribute("aria-label", `选择重跑任务 ${artifact.task_id}`);
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
        card.prepend(image);
      }
      return card;
    }));
  }

  function render(view, { refreshTasks = true } = {}) {
    state.view = view;
    renderRecentRuns();
    const statusInfo = statusUi(view.status);
    const statusBadge = byId("status-badge");
    statusBadge.textContent = statusInfo.label;
    statusBadge.dataset.tone = statusInfo.tone;
    byId("run-status").textContent = statusInfo.label;
    runGuidance.dataset.tone = statusInfo.tone;
    runGuidance.replaceChildren(
      element("strong", "", statusInfo.label),
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
      const genericMessage = lastError.message === "The workflow node could not be completed";
      const categoryFallback = {
        permission_error: "飞书应用没有权限读取该文档或素材，请检查文档分享与应用权限。",
        document_error: "文档读取失败，请检查文档内容或格式。",
        configuration_error: "服务配置缺失，请联系管理员检查飞书应用配置。",
        transient_error: "服务暂时不可用，请稍后重试。",
        provider_terminal_error: "生成服务返回错误，无法继续生成。",
        delivery_error: "结果写入飞书失败。",
        validation_error: "任务参数校验失败。",
      };
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
      taskList.replaceChildren(...(view.approval.tasks || []).map(renderTask));
    }
    renderArtifactReview(view);
    updateActionAvailability();
  }

  async function poll(force = false, resetDraft = false) {
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
      render(ReviewState.draftView(state.review), { refreshTasks });
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

  async function rerunBitableTask(runId = state.runId) {
    if (!runId || state.busy) return;
    setBusy(true);
    clearError();
    try {
      const created = await api(`/api/bitable/runs/${encodeURIComponent(runId)}/rerun`, {
        method: "POST",
      });
      state.runId = created.run_id;
      state.runMode = "bitable";
      state.review = ReviewState.createReviewState();
      state.referenceUploads = ReferenceUploadState.createState();
      state.referenceMutations = ReferenceMutationState.createState();
      state.artifactPreviewSignature = null;
          state.artifactRetryTaskIds = new Set();
      await poll(true);
      startPolling();
      await loadRecentRuns();
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      showError(error);
      await loadRecentRuns();
    } finally {
      setBusy(false);
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
      showError(error);
    } finally {
      setBusy(false);
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
    const body = { action };
    if (action === "adjust") {
      body.feedback = artifactReviewFeedback.value;
      body.task_ids = [...state.artifactRetryTaskIds];
      if (!body.feedback || !body.feedback.trim()) {
        showError(new Error("请填写调整意见"));
        return;
      }
      if (!body.task_ids.length) {
        showError(new Error("请至少选择一条需要重跑的任务"));
        return;
      }
    }
    setBusy(true);
    clearError();
    try {
      if (action === "adjust" && state.runMode === "bitable") {
        const created = await api(
          `/api/bitable/runs/${state.runId}/rerun-selected`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        state.runId = created.run_id;
        state.runMode = "bitable";
        state.review = ReviewState.createReviewState();
        state.referenceUploads = ReferenceUploadState.createState();
        state.referenceMutations = ReferenceMutationState.createState();
        state.artifactPreviewSignature = null;
        state.artifactRetryTaskIds = new Set();
        state.artifactReviewRunId = null;
        artifactReviewFeedback.value = "";
        await poll(true);
        startPolling();
        await loadRecentRuns();
        renderRecentRuns();
        document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
        return;
      }
      await api(`/api/runs/${state.runId}/artifact-review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await poll(true);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
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
      showError(error);
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
  artifactReviewFeedback.addEventListener("input", updateActionAvailability);
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
      showError(error);
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
      showError(error);
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
  configureModes();
  startBitableRefresh();
})();
