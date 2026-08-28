# 导演台 v2 设计：接入 GitHub 高星 AIGC skill

日期：2026-08-28
状态：待用户审阅

## 目标

导演台从 3 个 skill（优化/扩写/文生图）扩到 **8 个**，接入 6 个 GitHub 高星项目的精华（用户已全选）：

| # | 来源（⭐） | 新 skill | 形态 |
|---|---|---|---|
| 1 | awesome-gpt-image-2（23.4k） | 工业模板 | 词库拼装，无 LLM |
| 2 | LangGPT（12.5k） | 结构化提示词 | LLM（DeepSeek + 框架约束） |
| 3 | awesome-nanobanana-pro（10.2k） | 风格参考 | 词库 + LLM 融合（可选） |
| 4 | ChatGPT-Shortcut（8.7k） | 场景灵感 | 词库检索，无 LLM |
| 5 | sd-webui-prompt-all-in-one（3.2k） | 负面词生成 | 词库拼装 + LLM 扩展（可选） |
| 6 | Prompt-Engineering-Guide（77.9k） | —（知识底座） | 融进现有优化/扩写 SKILL.md |

## 架构

全部沿用现有 director 子应用（8895），零新端口、零 portal 改动（除前端 skill 列表）：

```
director/
├── assets/                    ← 新：词库数据（committed，头部注释注明来源仓库与 license）
│   ├── gpt_image_templates.json   ← #1：cases.json/style-library.json 精选（≤500KB）
│   ├── nano_banana_styles.json    ← #3：README 章节提取的风格提示词片段（≤100KB）
│   ├── shortcut_inspirations.json ← #4：按分类精选的现成提示词（≤500 条）
│   └── negative_tags.json         ← #5：负面词表 + 风格标签（≤50KB）
├── SKILL.md                  ← #6：追加「提示词工程通用技巧」段（角色/约束/输出格式/少样本/思维链）
├── app.py                    ← /api/assets 一次性下发词库（浏览器端缓存）+ optimize-prompt 新 mode
└── static/（无独立前端，导演台 UI 在 portal）
```

portal 前端（portal/static/app.js DirectorApp）：
- skills 列表扩到 8 个；按 skill 切换参数区（模板下拉 / 风格多选 / 场景分类 / 主题选择）
- 词库类 skill 本地拼装输出（零 LLM 成本、秒回）；结构化/融合类走 `/director/api/optimize-prompt` 新 mode
- 输出沿用现有「复制 / 填入文生图」串联

## 数据流

1. 工业模板：选模板（摄影/电商/海报/人物…）→ 填变量（主体/风格/环境）→ 前端按模板槽位拼装 → 输出提示词
2. 结构化提示词：输入一句话 → mode=langgpt → DeepSeek 按 LangGPT 框架（# Role/## Profile/## Rules/## Workflow）输出
3. 风格参考：选风格（如 Hyper-Realistic Crowd/2000s Mirror Selfie…）→ 输出该风格的提示词片段，可一键并入输入框让 LLM 融合
4. 场景灵感：选分类（写作/编程/营销/设计…）→ 展示该分类现成提示词列表，点选填入输入框
5. 负面词生成：选主题/风格 → 词库拼出负面词串（水印/低质量/畸形…）→ 输出「负面词：」段，可并入文生图
6. 指南底座：SKILL.md 加通用技巧段，现有 optimize/expand 出词质量随之提升（零新端点）

## 关键决策（已定）

- **词库下发到浏览器、前端拼装**：模板/风格/场景/负面词都无 LLM 调用，快且零成本；只有「结构化提示词」「风格融合」需要 LLM
- **词库瘦身**：ChatGPT-Shortcut 全量 7.7MB 太大——按 weight/tags 精选到 ≤500 条中文提示词；gpt-image-2 cases.json 精选核心类别
- **来源与 license**：每份 assets 头部注释来源仓库 URL；均为公开可用的提示词/模板数据，内部工具使用无碍
- **/api/assets 体积**：总量控制在 ≤700KB，一次性拉取后 localStorage 缓存（版本号失效）
- **统计红线**：词库类 skill 不产生任务；langgpt/融合走 DeepSeek 不计用量（与现有 optimize 一致）；出图仍走 /api/jobs + X-Job-Id 正常计数

## 测试

- 后端：/api/assets 结构与版本号、optimize mode=langgpt 契约
- 前端：词库拼装函数（模板槽位替换、负面词拼接）、node 冒烟扩展
- live：每个 skill 实测一次（词库类零成本；langgpt 真实 DeepSeek 一次）

## 部署

- director 子应用重启（杀进程让 portal 看门狗拉起）+ 前端刷新；无 plist 改动
