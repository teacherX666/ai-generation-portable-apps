(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ReferenceUploadState = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createState() {
    return { pendingByTaskId: {}, feedbackByTaskId: {} };
  }

  function withTask(state, taskId, { file, feedback }) {
    const pendingByTaskId = { ...(state.pendingByTaskId || {}) };
    const feedbackByTaskId = { ...(state.feedbackByTaskId || {}) };
    if (file === null) delete pendingByTaskId[taskId];
    else if (file !== undefined) pendingByTaskId[taskId] = file;
    if (feedback) feedbackByTaskId[taskId] = feedback;
    return { pendingByTaskId, feedbackByTaskId };
  }

  function createFeedback(phase, message) {
    return { phase, message };
  }

  function fileSelected(state, taskId, file) {
    const mimeType = typeof file.type === "string" ? file.type : "";
    const category = mimeType.startsWith("video/") ? "视频" : mimeType.startsWith("audio/") ? "音频" : "图片";
    return withTask(state, taskId, {
      file,
      feedback: createFeedback("selected", `已选择${category}：${file.name}，点击“增添素材”上传`),
    });
  }

  function uploadStarted(state, taskId) {
    return withTask(state, taskId, {
      feedback: createFeedback("uploading", "正在上传参考素材…"),
    });
  }

  function uploadSucceeded(state, taskId) {
    return withTask(state, taskId, {
      file: null,
      feedback: createFeedback("success", "参考素材已加入列表"),
    });
  }

  function uploadFailed(state, taskId, message) {
    return withTask(state, taskId, {
      feedback: createFeedback("error", message || "参考素材添加失败，请重试"),
    });
  }

  function pendingFile(state, taskId) {
    return state.pendingByTaskId?.[taskId] || null;
  }

  /**
   * 从 drop 事件的 `dataTransfer` 里取**第一个**文件。
   *
   * 用户 2026-09-29：「参考素材那一行拖不进文件」。飞书模块原先只能点文件选择器
   * （`git log -S dataTransfer` 在该模块全历史 0 命中 —— 拖动从未实现）。现在把拖入
   * 接到**同一条**路径上：取到文件后走 `fileSelected`，仍然要点「增添素材」才真正上传
   * （方案 A：不因为手一滑就消耗一次上传）。
   *
   * 拿不到文件就返回 null，调用方安静退出（不要吞掉页面其它的拖放行为）。
   */
  function droppedFile(dataTransfer) {
    const files = dataTransfer && dataTransfer.files;
    if (!files || !files.length) return null;
    return files[0];
  }

  /**
   * 拖进来的东西能不能直接上传？
   *
   * **文件夹、压缩包等没有 MIME 类型**（`type === ""`），不能收。用户 2026-09-29 报
   * 「拖到未选择文件那一栏没反应」——实测根因就是这类"没有类型的 payload"：把它塞给
   * `input.files` 会**静默失败**，`change` 处理器读到空文件就 `return`，于是界面上
   * 一点反馈都没有，用户只能认为"拖不了"。
   *
   * 所以先在**进状态之前**判一次类型，不合格就给出可操作的提示（而不是沉默）。
   */
  function accepts(file) {
    const type = file && typeof file.type === "string" ? file.type : "";
    return /^(image|video|audio)\//.test(type);
  }

  function feedback(state, taskId) {
    return state.feedbackByTaskId?.[taskId] || null;
  }

  return {
    accepts,
    createState,
    droppedFile,
    feedback,
    fileSelected,
    pendingFile,
    uploadFailed,
    uploadStarted,
    uploadSucceeded,
  };
});
