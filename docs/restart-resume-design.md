# 重启续跑（resume-first recovery）设计 — 2026-10-14

## 问题

Portal 收到 SIGTERM 会连带 SIGTERM 掉所有托管子应用（`portal/app.py:1440`、`5517`）；
子应用启动时 `recover_backlog()` 把 backlog 里**每一条**都重新 `run_job`：一个跑了一半的
视频任务会被整条重投 → **同一件事付两次钱**。

而事实上上游任务根本没死：方舟的 `contents/generations/tasks/{id}` 在它自己服务器上继续跑、
继续计费（实测：seedance 作业 `81a56eb2…` 的方舟任务 `cgt-20261014132713-75iu4` 在本地进程
之外持续 running）。我们只是**把 task_id 丢了**：

- `provider_tasks`（task_id / status_url）只在内存 `JOBS` 里（`seedance/app.py:3426`、
  `nano-banana/app.py:3002`），从不落盘；
- `jobs_backlog.json` 只写 `stage`/`activity_id`/`ws_id`/`username`，而且 `stage` **只写不读**
  ——文档里写的「started → 标记中断」从未实现（`test_job_backlog_recovery.py:61` 那条测试
  一直是红的）。

## 口径（用户 2026-10-14 选定）

**不浪费 token 优先**：宁可让用户偶尔点一次「重试」，也绝不自动重投可能已经计费的任务。

## run 级状态机（写进 `backlog[job_id]["runs"]["<index>"]`）

| phase | 含义 | 重启后 |
|---|---|---|
| （无记录） | 线程还没发起付费请求（本地模型永远停在这里：免费） | **重投**（不花钱，行为同旧版） |
| `submitting` | 付费 POST 在途 / 已返回但 task_id 尚未落盘 | **不自动重投**：状态未确认，标失败 + `retryable`，写明原因 |
| `submitted` | 已拿到 `task_id` + `status_url` | **续跑**：只 GET 状态 → 下载，**0 元** |
| `done` | 已拿到结果（含 `tokens`） | **回放**：直接当该 run 成功，**0 网络调用** |

- **付费前**只有云端 run 会写 `submitting`；本地模型（comfyui_workflow / `_LOCAL_MODEL_IDS`）
  在付费调用之前不写任何记录 → 被打断就照旧重跑（免费）。
- 任何 run **成功返回后**由 `run_job` 统一写 `done` + result（本地 run 也写）：再断一次连本地
  那份产物都直接回放，不占 GPU。
- 存量条目（`stage=started` 且完全没有 `runs`）：无法判断是否已计费 → 按「标记中断、可手动重试」
  处理（正是那条红测试期望的行为）。
- `stage=queued`：从未开跑 → 照旧自动重入队。

## 落点

| 文件 | 改动 |
|---|---|
| `seedance/app.py` | `_backlog_set_run(_locked)` / `_backlog_runs`；`run_one(resume=…)` 跳过 build_payload + POST；`run_one_with_fallback` 透传；`run_job(recovery=…)` 回放 done、跳过 unknown、只对未提交的 index 派 worker，成功后写 `done`；`recover_backlog()` 分流 |
| `nano-banana/app.py` | 同款；async 分支（`?async=true` → `/v1/images/tasks/{id}`）可续跑，同步分支（seedream / gpt-image / gemini）无断点 → 只写 `submitting`，恢复时不盲投 |

## 验证

### 单测

`tests/test_restart_resume.py`（16 条，含 2 条「正常路径不许被破坏」守卫）+ 既有
`test_job_backlog_recovery.py`（6 条，含那条一直红的 `test_started_job_marked_interrupted`，
现在转绿）+ `test_restart_recovery_keeps_reference_media.py`（10 条）= **32 passed / 0 failed**。

关键判别力：`test_run_one_resume_never_posts`（断言上游调用序列里只有 GET）、
`test_run_job_replays_done_runs_and_only_dispatches_rest`（回放的 tokens 必须原样计入、只对
未提交的 index 派 worker）、`test_nano_marks_submitting_before_paid_call`（同步通道靠提交前
标记兜底）。

### 真机回放（0 元，真实方舟任务）

`_diag/live_resume_replay.py <task_id>`：在临时 state 目录里伪造「重启前正在跑」的现场
（backlog stage=started + runs[1]={phase:submitted}），再调用产物里真正的 `recover_backlog()`。

用 10 分钟前真实成功的方舟任务 `cgt-20261014133635-a6r1z`（高大王）实测：

```
recovered=1 interrupted=0
Run 1: 续跑上游任务 cgt-20261014133635-a6r1z（服务重启，不重新提交）
Run 1: succeeded → Run 1: downloaded 20261014_135300_run1_cgt-...mp4
终态=succeeded tokens=648900 done=1
产物 bytes=23287481（23MB 真实成片）
上游调用序列：[('GET', '.../contents/generations/tasks/cgt-...')]   ← 0 次 POST
```

即：**已付费的成片被接回来了，全程没有第二次生成**。这是本次修复的核心证据。

