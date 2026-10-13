"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const ReferenceUploadState = require(
  "../../src/feishu_generation_agent/web/static/reference-upload-state.js",
);

test("selected reference file survives status updates until upload succeeds", () => {
  const file = { name: "toast-reference.png" };
  let state = ReferenceUploadState.createState();
  state = ReferenceUploadState.fileSelected(state, "task-1", file);
  state = ReferenceUploadState.uploadStarted(state, "task-1");

  assert.equal(ReferenceUploadState.pendingFile(state, "task-1"), file);
  assert.deepEqual(ReferenceUploadState.feedback(state, "task-1"), {
    phase: "uploading",
    message: "正在上传参考素材…",
  });

  state = ReferenceUploadState.uploadSucceeded(state, "task-1");
  assert.equal(ReferenceUploadState.pendingFile(state, "task-1"), null);
  assert.deepEqual(ReferenceUploadState.feedback(state, "task-1"), {
    phase: "success",
    message: "参考素材已加入列表",
  });
});

test("failed reference upload keeps the selected file for retry", () => {
  const file = { name: "retry-reference.png" };
  let state = ReferenceUploadState.createState();
  state = ReferenceUploadState.fileSelected(state, "task-1", file);
  state = ReferenceUploadState.uploadFailed(state, "task-1", "图片格式不支持");

  assert.equal(ReferenceUploadState.pendingFile(state, "task-1"), file);
  assert.deepEqual(ReferenceUploadState.feedback(state, "task-1"), {
    phase: "error",
    message: "图片格式不支持",
  });
});

// 用户 2026-09-29：「参考素材那一行拖不进文件」。飞书模块原先只能点文件选择器，
// 这里补上拖入 —— 取第一个文件后走**同一条** `fileSelected` 路径，仍要点「增添素材」。
test("dropped file is taken from the drag payload", () => {
  const file = { name: "dropped.png", type: "image/png", size: 3 };

  assert.equal(ReferenceUploadState.droppedFile({ files: [file] }), file);
  // 一次拖多个只取第一个（与文件选择器行为一致：input 只读 files[0]）
  assert.equal(
    ReferenceUploadState.droppedFile({ files: [file, { name: "b.png" }] }),
    file,
  );
});

test("dropped payload without files yields nothing", () => {
  assert.equal(ReferenceUploadState.droppedFile({ files: [] }), null);
  assert.equal(ReferenceUploadState.droppedFile({}), null);
  assert.equal(ReferenceUploadState.droppedFile(undefined), null);
  assert.equal(ReferenceUploadState.droppedFile(null), null);
});

// 用户 2026-09-29：「拖到未选择文件那一栏没反应」—— 实拖的是**文件夹**：
// `type` 为空、塞给 input.files 会静默失败，change 读到空就 return，界面上毫无反馈。
test("only image / video / audio drops are accepted", () => {
  assert.equal(ReferenceUploadState.accepts({ name: "a.png", type: "image/png" }), true);
  assert.equal(ReferenceUploadState.accepts({ name: "a.mp4", type: "video/mp4" }), true);
  assert.equal(ReferenceUploadState.accepts({ name: "a.mp3", type: "audio/mpeg" }), true);
});

test("folders and untyped payloads are rejected", () => {
  // 文件夹：Chrome 给的是没有 type 的 File 条目
  assert.equal(ReferenceUploadState.accepts({ name: "素材文件夹", type: "" }), false);
  assert.equal(ReferenceUploadState.accepts({ name: "a.zip", type: "application/zip" }), false);
  assert.equal(ReferenceUploadState.accepts({ name: "a.txt", type: "text/plain" }), false);
  assert.equal(ReferenceUploadState.accepts({ name: "no-type-field" }), false);
  assert.equal(ReferenceUploadState.accepts(null), false);
  assert.equal(ReferenceUploadState.accepts(undefined), false);
});
