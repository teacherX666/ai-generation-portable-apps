# Portal 全局历史记录设计（方案二·媒体卡片）

日期：2026-08-27
状态：待用户审阅
参考设计：`~/ai-creation-canvas/docs/design/task-center-options.html` 方案二（媒体卡片：缩略图网格 + 点击弹窗）

## 目标

在 Portal 新增一个「历史记录」页签，聚合**所有子应用**（Seedance / 图像生成 / Dreamina / 人像 / 无限画布 / 导演台…）的任务历史，界面采用方案二形态：

- 顶部筛选栏：用户（管理员可见，普通用户只看自己）、全部/图片/视频、全部状态/成功/失败/生成中、搜索（提示词/任务编号）、近 7/30/90 天、刷新
- **媒体卡片网格**：缩略图优先（视频带播放徽标 + 时长角标，图片直接见缩略图，无产出的失败/排队任务用渐变占位），左上角「图片/视频」种类徽标，卡片主体为提示词一行省略，底部「用户 · 时间」+ 状态药丸（已成功/已失败/生成中/排队中）
- **点卡片出详情弹窗**：左侧大预览，右侧键值表（用户/应用/模型/参数/任务编号/提交时间/完成时间）+「请求」「返回」两个页签——请求展示提示词与参数（完整 JSON），返回展示状态/耗时/产出数量/错误原因（失败任务红色错误块）/**结果清单**；**结果清单每条（图片、视频）都带「下载」按钮，可直接在弹窗内下载**，主预览区同样带下载按钮（复用 `_blobDownload` 自签证书下载模式）；**任何密钥字段永不展示**

## 现状与缺口

- 子应用任务状态是**内存态**（JOBS dict，重启即丢）；portal 只有聚合计数（usage.json：jobs/张/秒），**没有提示词、参数、结果 URL 的任务级记录**
- portal `_proxy` 转发 POST 时已把 body 读进内存（`rfile.read(length)`）→ 提取提示词**零额外开销**
- portal `_job_poll_loop` 每 15s 轮询 `/api/jobs/{id}`，终态时已能拿到 `status/done/duration/results`（含结果 URL）→ 终态信息在既有循环里顺手落库

## 架构

```
portal/app.py（后端，唯一改动点）
├── _proxy 中 is_job POST：解析 body（JSON / multipart），提取白名单字段
│     → register_job 增参 prompt/params/model
├── register_job：写 history 记录（status=pending，注册时落库 → 生成中卡片可实时可见）
├── _job_poll_loop 终态分支：更新 history 记录（status/done/results[0].url/时长/error）
│     404 分支：更新为 failed
└── GET /api/platform/history：分页查询 + 权限过滤（admin 全量，普通用户仅本人）

portal/state/history.json（gitignored）
└── {"<app>:<job_id>": {…记录…}}；写入时顺带剪枝：
      超过 30 天的条目删除 + 总量上限 10000 条（超限删最旧）
      ——每日清理服务 tools/cleanup_daily.py 补一条兜底剪枝（统计文件一律不碰，新增 history 剪枝不影响统计）

portal 前端
├── index.html：新 tab 按钮「历史记录」+ tab-panel（硬编码，符合 Portal 惯例）
├── app.js：HistoryApp() 组件（方案二样式，PetiteVue，注册进 createApp 根上下文）
└── styles.css：卡片网格/药丸/弹窗样式（沿用 portal 现有 --surface/--border 主题令牌，明暗双主题自适应）
```

## 数据模型（history.json 单条记录）

```json
{
  "app": "nano-banana", "job_id": "xxxx",
  "username": "王露悦",
  "kind": "image",
  "prompt": "…提示词…",
  "model": "doubao-seedream-5-0-pro-260628",
  "params": {"aspect_ratio": "1:1", "count": 2},
  "status": "done",
  "submitted_at": 1784599999, "completed_at": 1784600123,
  "duration": 0,
  "thumb_url": "/outputs/job-0.png",
  "results": [{"url": "/outputs/job-0.png", "kind": "image"},
              {"url": "/outputs/job-1.png", "kind": "image"}],
  "error": ""
}
```

**body 解析规则（防泄露）**：
- JSON body：直接取白名单字段
- multipart（dreamina 等 FormData）：`cgi.FieldStorage` 解析，只取文本字段、**丢弃文件字节**
- 白名单字段名：`prompt, text, content, aspect_ratio, ratio, duration, resolution, image_size, count, mode, style, negative_prompt, seed, generate_audio`
- **含 `key/token/secret/password` 的字段名一律不采集**；采集包在 try/except 里——历史采集失败绝不影响统计与代理（核心红线）
- body 超过 5MB 时跳过 prompt 提取（只记 params 可安全拿到的部分），避免大内存开销

**thumb_url 规则**：终态 results 第一条的 url 原样存（各子应用相对路径不同），前端统一前缀 `/{app}` 经 portal 代理访问；视频缩略图用 `<video preload="metadata" muted playsinline>`（CLAUDE.md 已知：默认 preload=none 不拉元数据、画不出首帧）。

**状态归一**：子应用状态五花八门，portal 归一为四档：`succeeded/done/completed` → 已成功；`failed/cancelled/error` → 已失败；`running/processing/generating/uploading` → 生成中；`queued/waiting/pending` → 排队中。

## 后端 API

`GET /api/platform/history?days=30&kind=all&status=all&q=&limit=60&offset=0`
- 鉴权：复用 portal session；**admin 看全部（可选 `user=` 过滤），普通用户强制只看本人**（与现有统计权限一致）
- 返回：`{ok, total, items: [...记录 + display_name...]}`，按 submitted_at 倒序；不含任何密钥
- 搜索 `q`：匹配提示词或任务编号（子串）
- 分页：limit/offset + 前端「加载更多」

## 前端（HistoryApp，方案二样式）

- 筛选栏：用户下拉（仅 admin 渲染）、`全部/图片/视频` 分段、`全部状态/成功/失败/生成中` 分段、搜索框、`7/30/90 天` 分段、刷新按钮
- 卡片网格：`repeat(auto-fill, minmax(220px, 1fr))`；媒体区高 140px（img `object-fit: cover` / video 首帧）；状态药丸沿用设计稿配色（绿成功/红失败/蓝生成中/琥珀排队）；卡片 hover 边框高亮 + 上浮 2px
- 弹窗：左侧 300px 预览（视频带播放控件），右侧信息区 + 请求/返回页签；失败任务在返回页签显示红色错误块；结果清单每条可点击下载（复用 `_blobDownload` 自签证书下载模式）
- 空态：「暂无历史」；加载更多按钮
- 明暗双主题：全部颜色走 portal 现有 CSS 变量体系，不新造主题系统

## 错误处理

- 历史采集/落库全部 try/except 包裹，失败仅打日志，**不影响统计登记、不影响代理转发**
- 子应用已重启导致 404 → 历史记录标为 failed（与统计回滚路径一致）
- 前端所有请求检查 `res.ok`；弹窗加载失败显示错误提示

## 测试

1. **单元测试**（pytest，fake request_json/子应用响应）：register_job 写 pending 记录；终态更新（done/failed/404）；body 白名单解析（JSON + multipart 各一例）；**密钥字段名不采集**；body>5MB 跳过 prompt；权限过滤（admin 全量/普通用户仅本人）；30 天剪枝与 10000 条上限；统计计数器不受 history 写入影响（回归断言 X-Job-Id 登记路径不变）
2. **前端**：node 冒烟（DOM 桩，断言 HistoryApp 暴露 + 卡片/弹窗函数存在），沿用 test_director_sidebar.mjs 模式
3. **live 实测**（部署后）：真实跑一次出图任务 → 历史页出现卡片（生成中→已成功）、缩略图可显示、弹窗请求/返回正确；普通用户账号只能看到自己的记录；统计页数字不变

## 部署

- 前端三件套 + portal/app.py；**后端改动需重启 portal**（与用户协调、确认无进行中任务；plist 不变，kickstart 即可）
- history.json 在 state/（gitignored）；cleanup_daily.py 增加 history 剪枝（只剪 history，不碰 usage.json 等统计文件——核心红线）
- 兼容性：历史记录从上线时刻开始累积（此前任务无记录），页面空态文案说明

## 统计核对（核心红线）

- 本次改动**只新增** history 采集与查询；`_is_job_request` 白名单、X-Job-Id 登记、daily/by_user 计数、finalize 回滚逻辑一律不动
- history 写入失败静默降级，绝不抛出影响代理/统计
- cleanup_daily 对 usage.json / by_user / job_owners 的既有处理保持原样
