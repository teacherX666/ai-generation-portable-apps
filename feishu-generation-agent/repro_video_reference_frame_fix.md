# 复现记录：参考视频被摘掉 → 零产物静默完成（2026-09-24）

用户原话：「现在上传参考视频但是却生成失败了」。

## 一、改之前的现场（真实失败运行）

### 1. 数据库里两条失败运行

`a27a00a1-08a5-4f33-bbf2-fb7000a9876d` / `59bbfafc-4275-423d-9cf3-ef4e5f185cf4`：

- 任务的 `reference_images`：
  `[{image-1, reference_image, 1}, {image-2, reference_image, 2}, {upload-8127ef4d…, reference_video, 3}]`
- 事件里只有一条：
  `execute_selected_tasks/running 参考素材已自动整理：视频不能作为参考图（Seedance 不接受视频输入）：upload-8127ef4d…`
- 之后 `execute_selected_tasks/completed`、`verify_and_download_artifacts/completed` —— **节点全绿**。

### 2. `GET /api/runs/a27a00a1…` 的视图（改之前的用户视角）

```
status: failed
last_error: null                     ← 用户看不到任何原因
artifacts: 0
execution_records: [{"task_id": "task-01", "provider": "seedance2.5",
  "provider_task_id": null, "status": "failed",
  "error": {"category": "validation_error", "message": "生成参数不符合供应商要求",
            "retryable": false, "code": "generation_asset_mapping_mismatch"}}]
operations: [{"task_id": "task-01", "phase": "failed"}]
events[-3:]: execute_selected_tasks running 参考素材已自动整理：视频不能作为参考图…
             execute_selected_tasks completed
             verify_and_download_artifacts completed
```

### 3. 根因链

1. 执行前 `_without_unusable_references()` 把**视频素材**从素材列表里摘掉（只记一条事件）；
2. 但任务本身**还引用着那个视频**；
3. `integrations/seedance.py::_validate_submission` 要求
   `set(asset_ids) == set(reference_ids)` → 抛 `generation_asset_mapping_mismatch`
   （`ErrorCategory.VALIDATION` → 被判成本地预检失败 → 运营方**根本没收到请求**，所以没花钱，
   但用户也什么都没看到）；
4. `execute_selected_tasks` 正常返回 → `status: failed`、零产物、`last_error: null`。

## 二、改之后的现场（免费活体验证，**未批准、未花钱**）

复现脚本：`repro_video_reference_frame_fix.py`（建运行 → 真实接口传参考视频 → 跑执行前流水线 → 删运行）

```
$env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe repro_video_reference_frame_fix.py
```

实测输出（运行 `2774d327-c639-4316-80b0-6a129e4aa66b`，参考视频 14MB mp4）：

```
=== 上传参考视频之后（改之前的现场）===
reference_images: [{image-1, reference_image, 1}, {image-2, reference_image, 2},
                   {image-3, reference_image, 3},
                   {upload-33630a12…, reference_video, 4}]

=== 改之后：执行前流水线 ===
events: ["[execute_selected_tasks/running] 参考视频已改用抽帧画面：
          upload-33630a12… → upload-33630a12…-frame"]
reference_images: [{image-1, reference_image, 1}, {image-2, reference_image, 2},
                   {image-3, reference_image, 3},
                   {upload-33630a12…-frame, reference_image, 4}]   ← 换成抽帧产物
orders: [1, 2, 3, 4]                                              ← 连续、无重复
assets handed to provider: [image-1(png), image-2(png), image-3(png), upload-33630a12…-frame(jpeg)]
references == assets: True                                        ← 供应商适配层的不变量恢复
zero-reference failure reason: None
frame asset: upload-33630a12…-frame image/jpeg 312249 …/inputs/e04fb9e8….jpg exists: True
DELETE run: {"run_id": "2774d327-…", "status": "deleted"}
```

另一条真实运行（`59bbfafc` 里用户那次上传的 3.4MB 原视频）单独跑抽帧：
`upload-8127ef4d… → upload-8127ef4d…-frame`，217674 字节、JPEG magic `FF D8`，
提示词里的 `参考视频1` 被重映射成 `参考图2`（用户原提示词里确实写了「参考视频1，将该视频1定义为运动参考」）。

## 三、未做的部分（明确声明）

- **没有点「批准」**：批准后是真的调一次 Seedance 生成（花钱），按任务要求跳过。
  因此「改之后真的出了一条成片」这一步**没有活体证据**，只有「执行前引用已落到抽帧产物上
  + 供应商侧不变量（references == assets）成立」的证据。
- 「抽帧失败 → 零参考图 → 运行明确失败」这条**只有单测覆盖**
  （`tests/unit/test_video_frame_references.py::test_run_fails_visibly_when_no_reference_survives`），
  没有做付费活体；要活体验证得批准一条只有参考视频的任务（抽帧失败时会走失败分支、不调用供应商）。
- 抽帧产物的素材 id 用契约里的**单帧命名** `{video_id}-frame`（不带序号）：
  planner 的 `_asset_base_id` / `_register_video_frame_aliases` 与前端 `review-state.js`
  都只认 `-frame` 后缀，换成 `-frame-1` 会让覆盖统计与前端别名同时认不出来。
