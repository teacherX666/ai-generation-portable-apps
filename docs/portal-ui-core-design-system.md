# Portal UI Core：企业 AI 创作工作台前端骨架

状态：第一版基座已落地，页面迁移按模块逐步进行。

## 1. 目标与边界

这套骨架服务于公司内部 AI 创作聚合平台。它统一视觉语言、页面结构和交互状态，但不强迫所有模块使用同一个前端框架：

- Portal、Seedance、Nano Banana、Dreamina standalone、RAG、飞书 Agent 继续使用浏览器原生 HTML/CSS/JS 或 PetiteVue，保持客户端零构建依赖。
- 无限画布继续使用 React + Vite + TypeScript + Tailwind + shadcn/Radix；只做语义 token 映射，不把工作台降级成普通后台页面。
- 不新增全站 npm UI 库。现有代码已能覆盖静态部署和 iframe 代理场景，基座以可复制的 CSS 为主要分发形式。

正式风格名：**克制型企业 AI 创作工作台（Light Productivity Studio）**。

## 1.1 技术方案决策

当前采用“**无构建共享 CSS Core + 按模块保留现有运行时**”的方案：

| 层级 | 当前方案 | 原因 |
| --- | --- | --- |
| Portal 壳层 | 原生 HTML/CSS/JS + PetiteVue | 已在服务机静态部署，改样式无需重新构建；保持登录、代理、统计和原生管理页稳定 |
| Seedance / Nano Banana | 原生 HTML/CSS/JS + PetiteVue | 工作区、任务轮询、草稿和文件上传已经成熟；引入组件库会增加重复状态和构建成本 |
| Dreamina / RAG / 飞书 Agent | 原生 HTML/CSS/JS（部分 PetiteVue） | 继续兼容独立入口和 Portal 代理，不改变现有 API 与权限守卫 |
| 无限画布 | React + Vite + TypeScript + Tailwind + shadcn/Radix | 画布拖拽、缩放、节点连接和快捷键需要组件状态与自由布局；它本来就已经是独立 React 工作台 |
| 统一视觉层 | `shared-ui/portal-ui-core.css` | CSS 变量、布局契约、状态徽标和无障碍规则可直接复制到 iframe，不需要客户端安装依赖 |

本阶段**不新增全站 UI 组件库**。Ant Design、Element Plus、Naive UI 等库都假设存在 npm 构建链，并且会把一套管理后台组件带进每个 iframe；这与当前“服务机静态部署、客户端零环境依赖、子应用可独立运行”的约束不匹配。

如果未来迁移到统一 React 构建链，推荐只把 **Radix primitives（无样式交互原语）+ 现有 Token + 自有组件**作为演进方向：这样可以复用 Dialog、Popover、Tabs、Tooltip 等行为，同时保留当前克制的视觉语言，不把页面变成模板化后台。

## 1.2 前端骨架分层

骨架不是单一组件文件，而是四层契约：

1. **Foundation**：颜色、字号、间距、圆角、阴影、动效、z-index（`tokens/core.css`）。
2. **Semantic**：页面和组件只引用语义变量，例如画布、表面、主动作、文本和焦点环（`tokens/semantic.css`）。
3. **Primitives**：壳层、侧栏、工作区、Panel、Button、Field、Badge、Alert、Toolbar、KPI、Result Card、Task Row、Data Table（`styles/layout.css`、`styles/components.css`）。
4. **Page contracts**：创作工作台、审批工作台、管理控制台、问答诊断工作台、无限画布五种页面类型。

业务模块只负责数据、任务状态和领域交互；视觉迁移优先改 Token 或 Primitive，不直接在业务 CSS 中继续堆新的颜色和间距。

## 2. 原版代码已经完成的工作

原版不是“没有骨架”，而是已经有一套分散但有效的基础：

1. `portal/static/index.html` 提供统一 Portal Shell：标题栏、LAN 状态、用户信息、模块 Tab、全局横幅、iframe/native 混合挂载。
2. `portal/static/styles.css`、`seedance/static/styles.css`、`nano-banana/static/styles.css`、`dreamina/static/styles.css` 已有 `DESIGN TOKENS v1`，确定了 `#f3f6fa` 浅色蓝灰画布、白色表面、`#235fd6` 主动作、边框、语义色、间距、圆角、阴影、焦点态和滚动条。
3. Portal 与多个子应用已经采用“唯一主滚动容器”原则：根容器隐藏溢出、内容区显式滚动、iframe 使用 `height:100%`，这是双滑动条修复的结构基础。
4. Seedance/Nano Banana 已有工作区 Tab、草稿恢复、任务状态轮询和结果区；Dreamina 已有表单、账号管理和结果展示；RAG 已恢复为对话 + 诊断结果页面；无限画布已有最完整的现代 React 工作台实现。

本次新增的 UI Core 不替换这些能力，而是把它们提炼成跨模块可复用的稳定 API。

## 3. Token 分层

`shared-ui/tokens/` 是唯一源：

- `core.css`：原始颜色、字号、间距、圆角、阴影、动效和 z-index。
- `semantic.css`：新代码使用的语义变量，如 `--ui-bg-canvas`、`--ui-surface`、`--ui-action-primary`、`--ui-text-primary`。
- `legacy-aliases.css`：把原版 `--page-bg`、`--surface`、`--accent` 等变量映射到语义层，避免一次性大规模重写。

当前值保持原版不变。任何视觉升级先修改 token，再评估组件和页面，不在业务 CSS 中散落新颜色。

## 4. 组件与页面契约

第一批基础类已在 `shared-ui/styles/` 提供：

- 布局：`.ui-shell`、`.ui-shell__body`、`.ui-sidebar`、`.ui-main`、`.ui-workbench`、`.ui-console`。
- 容器：`.ui-panel`、`.ui-panel__header`、`.ui-panel__body`。
- 动作：`.ui-btn--primary`、`.ui-btn--secondary`、`.ui-btn--ghost`。
- 表单：`.ui-field`、`.ui-field__label`、`.ui-input`、`.ui-select`、`.ui-textarea`。
- 状态：`.ui-badge--neutral/info/success/warning/danger`、`.ui-alert`。
- 空态与无障碍：`.ui-empty`、`.ui-visually-hidden`、`.ui-scroll`。

页面模板统一为五类：

1. 创作工作台：输入/参数 + 素材 + 任务 + 结果，结果优先。
2. 审批工作台：任务列表 + 内容/时间线 + 决策和反馈。
3. 管理控制台：指标 + 筛选 + 表格/详情抽屉 + 日志。
4. 问答诊断工作台：对话流 + 结论/原因/操作/引用/是否解决。
5. 无限画布：自由画布 + 浮动工具栏 + 侧边上下文，不套固定两栏表单。

## 5. 状态词典

跨模块统一使用以下语义，不用各自发明颜色和文案：

`idle` 未开始、`queued` 排队中、`running` 处理中、`succeeded` 已完成、`failed` 失败可重试、`cancelled` 已取消、`review_required` 待审核、`archived` 已归档。

状态颜色只用于徽标、边框、图标或局部提示；不使用大面积高饱和色覆盖工作区。

## 6. iframe 分发策略

Portal CSS 不会穿透 iframe，因此 UI Core 同步到以下本地入口：

`portal/static/ui/`、`seedance/static/ui/`、`nano-banana/static/ui/`、`dreamina/static/ui/`、`rag-assistant/static/ui/`、`feishu-generation-agent/.../static/ui/`。

从仓库根目录执行 `sh shared-ui/sync-ui.sh` 发布。它不安装依赖、不构建浏览器 bundle；每个应用仍可独立直连运行，Portal 代理下也能加载相同的本地 CSS。

## 7. 迁移顺序

1. 先替换重复 token 块为 Core 引用，保留 legacy alias。
2. 迁移按钮、输入框、Panel、Badge、Alert、Dialog、Toast、Tabs。
3. Seedance/Nano Banana 先统一结果卡片、任务行和上传区。
4. 飞书 Agent 统一审批三栏骨架和状态。
5. RAG 统一结构化答案、引用来源、复制和反馈。
6. 管理功能最后迁移表格、筛选、抽屉、指标卡。

每次只迁一个页面区域，保留现有 API、任务状态和 localStorage 键，不把视觉迁移和业务重构混在一个提交里。

## 8. 验收清单

- Portal 和每个 iframe 在独立直连、Portal 代理两种路径都能加载 UI Core。
- 浏览器端没有新增 npm 构建依赖。
- 根容器和主内容区只有一个垂直滚动责任，iframe 不再使用错误的固定视口高度。
- Seedance / Nano Banana 的页面级工作区 Tab 栏与两栏 `.app` 共享同一个 100% 高度视口；页面根节点隐藏溢出，侧栏和结果区只承担局部滚动，避免 body + workspace 嵌套双滚动条。
- 键盘 Tab、`:focus-visible`、减少动态效果偏好可用。
- 颜色、状态、按钮尺寸和面板间距来自 token，不在业务页面新增随意值。
- 无限画布的快捷键、暗色模式和自由布局保持不变。

## 9. 原版与本次补充的边界

### 原版已经提供

- Portal 顶部栏、LAN 信息、模块 Tab 和 iframe/native 混合挂载。
- 四个主要页面中的 DESIGN TOKENS v1：`#f3f6fa`、`#ffffff`、`#235fd6`，以及边框、语义色、圆角、间距、阴影、焦点态。
- Seedance/Nano Banana 的工作区 Tab、草稿恢复、任务轮询、活动记录和结果渲染。
- Dreamina 的账号池、生成表单、历史记录、存档 CRUD 和管理员权限守卫。
- RAG 的对话流、图片粘贴入口；飞书 Agent 的审批工作流；无限画布的 React 交互模型。

### 本次补充

- 把分散 Token 提炼成共享 UI Core，并通过 `sync-ui.sh` 分发到每个 iframe 入口。
- 用 legacy alias 兼容旧变量，避免一次性改写所有业务 CSS。
- 给 Portal、Seedance、Nano Banana、Dreamina、RAG、飞书 Agent、人像入口接入统一壳层类和页面类型类。
- 恢复 Portal 中报错问答助手、飞书 Agent、无限画布的本地 fallback，注册表暂时不可用时不再白屏。
- 收紧 Portal 与 iframe 的滚动责任；Seedance/Nano Banana 根节点隐藏溢出，侧栏和结果区承担局部滚动，修复双滑动条复发路径。
- 补充统一的 Toolbar、KPI、Result Card、Task Row、Data Table、Badge、Alert 和窄屏规则，为后续逐区域迁移提供稳定类名。

## 10. 后续实施顺序

后续仍按“一个页面区域一个小步”推进：

1. Seedance/Nano Banana：结果卡、上传素材区、任务状态和活动行。
2. Dreamina：账号池、任务卡、历史结果和管理员动作。
3. RAG：结构化回答、原因/操作/引用/是否解决反馈。
4. 飞书 Agent：审批三栏、时间线、素材覆盖和结果预览。
5. Portal 管理页：统计、密钥库、人像页的 KPI、筛选、表格和详情抽屉。

每一步都保留现有接口、任务状态、localStorage 键和权限边界；如果需要后端重启，必须单独确认，因为内存任务会被终止。
