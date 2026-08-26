# 报错问答助手（rag-assistant）

一个面向公司内部同事的报错问答服务：用户粘贴报错文字或截图，助手先从飞书知识库检索答案；知识库没有时，再现场扫描项目源码尝试定位，并把有价值的结论写入飞书「待审核池」。

## 目录结构

```text
rag-assistant/
├── app_fastapi.py            网页入口（FastAPI），Portal 反向代理到本服务
├── portal_identity.py        校验 Portal 注入的 HMAC 身份
├── secrets.example.json      配置模板（复制并填写后另存为 state/secrets.json）
├── static/index.html         聊天式网页 UI
└── rag_agent/
    ├── config.py             从 state/secrets.json 加载配置
    ├── query/                预处理、检索、Prompt、语义闸门、日志
    ├── llm/                  截图摘要（视觉）、DeepSeek 生成
    ├── sync/                 飞书知识库拉取、切分、向量化、蓝绿索引
    ├── self_learn/           源码打包、现场分析、候选知识写回
    └── lark/                 飞书 API 封装
```

## 首次初始化

1. 复制配置模板并填写真实密钥：

   ```bash
   cp secrets.example.json state/secrets.json
   chmod 600 state/secrets.json
   ```

   需要填写的字段：

   | 字段 | 说明 |
   |---|---|
   | `deepseek_api_key` | DeepSeek 生成模型的密钥 |
   | `embedding_api_key` | Embedding 服务（默认 T8Star）的密钥 |
   | `embedding_base_url` | Embedding 服务的 OpenAI 兼容地址 |
   | `lark_app_id` / `lark_app_secret` | 飞书自建应用的凭证 |
   | `lark_kb_doc_id` | 飞书知识库文档 ID 或 wiki token |
   | `lark_kb_pending_doc_id` | 「待审核池」文档 ID 或 wiki token |
   | `code_scan_root` | 源码扫描的仓库根目录，留空则默认取本仓库上级目录 |

2. 构建知识库索引（会从飞书拉取知识库、向量化并切换到新索引）：

   ```bash
   curl -X POST http://127.0.0.1:8900/admin/reindex \
     -H 'Content-Type: application/json' \
     -d '{"dry_run": false}'
   ```

   部署到 Portal 后，管理接口需要管理员身份；本地直连调试时可省略身份头。

## 运行

由 Portal 统一拉起（设置 `RAG_ASSISTANT_ENGINE=fastapi`），默认端口 `8900`。也可以本地单独调试：

```bash
cd rag-assistant
../.venv/bin/uvicorn app_fastapi:app --host 127.0.0.1 --port 8900
```

## 关键接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 网页 UI |
| `GET` | `/health` | 存活检查 |
| `POST` | `/api/ask` | 问答接口（普通用户可用） |
| `GET` | `/admin/status` | 当前索引状态（管理员） |
| `POST` | `/admin/reindex` | 重建知识库索引（管理员） |
| `GET` | `/admin/query-log` | 问答日志（管理员） |

## 问答流程

1. 对输入做本地规则预筛，明显无关的问题直接拒绝、明显报错直接放行，两者都不额外调用模型；
2. 规则判断不了的，走语义闸门，进一步避免无关输入触发昂贵操作；
3. 有截图时先做视觉摘要，摘要同时参与检索和最终生成；
4. 检索飞书知识库，用 DeepSeek 判断「完全命中 / 部分命中 / 未命中」；
5. 未命中时扫描源码现场分析，中高置信度的结论写入飞书待审核池，人工审核后才进入正式知识库。

## 数据与安全

- `state/`、`data/` 均为运行时数据，已 gitignore，不会提交密钥、日志或索引；
- `state/secrets.json` 含密钥，务必保持权限 `600`；
- 源码扫描会把项目源码发给 DeepSeek，上线公网前应审查外发范围和敏感信息脱敏策略。
