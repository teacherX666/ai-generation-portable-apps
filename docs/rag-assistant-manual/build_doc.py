#!/usr/bin/env python3
"""构建飞书文档《报错问答助手 · 使用说明》。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(HERE / "tools"))
from feishu_docx_builder import build_doc, load_client  # noqa: E402

DOC_TITLE = "报错问答助手 · 使用说明"
ASSETS = Path(__file__).resolve().parent / "assets"

CONTENT = [
    ("quote", "遇到报错别慌，粘贴文字或截图，先问它。"),
    ("divider",),
    ("h2", "一、这是什么？"),
    ("p", "报错问答助手是面向公司同事的**排障问答服务**：把报错文字或截图发给它，助手会先到**飞书知识库**检索现成答案；知识库没有时，会**现场扫描平台源码**帮你定位问题。有价值的结论还会被整理进知识库，越用越聪明。"),
    ("image", "A-rag-portal.png"),
    ("divider",),
    ("h2", "二、怎么用"),
    ("ordered", ["把报错文字粘贴到输入框（**截图也可以**：点「识图」选截图，或直接在页面上 Ctrl+V 粘贴截图）",
                 "点「发送」",
                 "稍等片刻，回答会分小节显示，点「复制」一键带走"]),
    ("image", "B-rag-empty.png"),
    ("divider",),
    ("h2", "三、答案怎么看"),
    ("bullet", ["答案按 **问题原因 / 解决办法** 等小节组织，读起来清楚",
                "回答右上角有 **复制** 按钮，方便转发",
                "知识库里有的问题秒回；需要现场查源码的问题会慢一些（几十秒）"]),
    ("image", "C-rag-chat.png"),
    ("divider",),
    ("h2", "四、常见问题（FAQ）"),
    ("p", "**Q1：答非所问怎么办？**\n换个说法描述报错（带上完整报错原文或截图效果最好），再问一次。"),
    ("p", "**Q2：问了半天没有回复？**\n现场扫描源码的请求耗时较长，请耐心等；持续无响应联系管理员。"),
    ("p", "**Q3：我的提问会被别人看到吗？**\n不会，对话是你的个人会话。有普遍价值的结论才会经审核后进入共享知识库。"),
    ("p", "**Q4：涉及账号、密钥、配额的问题它能解决吗？**\n这类问题它答不了（也没有权限），直接联系管理员。"),
]


def main() -> int:
    client = load_client()
    doc_id = build_doc(client, DOC_TITLE, CONTENT, ASSETS)
    print("DOC_URL=https://redcqchina.feishu.cn/docx/" + doc_id)
    print("DOC_ID=" + doc_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
