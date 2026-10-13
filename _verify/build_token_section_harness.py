#!/usr/bin/env python3
"""构建「Token 消耗」区块的真实渲染验证页。

登录态拿不到，没法直接看生产门户的统计页，所以这里把**真实**的两样东西拼起来：
  - 从 portal/static/index.html 抽出的那个 <section> 原文（不是我手抄的副本）
  - 从 portal/static/app.js 抽出的那 4 个方法的真实源码（大括号配对提取）
再用仓库里 vendored 的 petite-vue 渲染，喂一份真实形状的区间数据。

跑法：
    python _verify/build_token_section_harness.py
然后在浏览器打开生成的 token_section_harness.html。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "portal" / "static" / "index.html"
APP_JS = ROOT / "portal" / "static" / "app.js"
OUT = Path(__file__).resolve().parent / "token_section_harness.html"

METHODS = ["rangeTokenTotals", "fmtTokens", "tokenAppList", "rangeTokenGrandTotal"]


def extract_section(html: str) -> str:
    marker = "Token 消耗（火山方舟）"
    marker_at = html.index(marker)
    start = html.rindex("<section", 0, marker_at)
    end = html.index("</section>", start) + len("</section>")
    return html[start:end]


def extract_method(js: str, name: str) -> str:
    anchor = js.index(f"\n    {name}(")
    brace = js.index("{", anchor)
    depth = 0
    for i in range(brace, len(js)):
        ch = js[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return js[anchor : i + 1].strip().rstrip(",")
    raise ValueError(f"unbalanced braces while extracting {name}")


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    section = extract_section(html)
    methods = {name: extract_method(js, name) for name in METHODS}

    # 真实形状：/api/platform/stats/range 返回 users[user][app] = {images[], seconds[], tokens[]}
    state = {
        "isAdmin": True,
        "rangeStart": "2026-10-06",
        "rangeEnd": "2026-10-12",
        "rangeLoading": False,
        "appsMeta": {
            "seedance": {"display_name": "Seedance", "color": "#2563eb"},
            "nano-banana": {"display_name": "图像生成模块", "color": "#10b981"},
            "volcengine-portrait": {"display_name": "人像生成", "color": "#f59e0b"},
        },
        "rangeData": {
            "dates": ["2026-10-11", "2026-10-12"],
            "users": {
                "cfy": {"seedance": {"images": [0, 0], "seconds": [0, 140], "tokens": [0, 326947]}},
                "MM": {"seedance": {"images": [0, 0], "seconds": [12, 15], "tokens": [260100, 540900]}},
                "左籽悦": {"nano-banana": {"images": [2, 0], "seconds": [0, 0], "tokens": [23107, 0]}},
                "高大王": {
                    "seedance": {"images": [0, 0], "seconds": [0, 253], "tokens": [0, 0]},
                    "nano-banana": {"images": [25, 0], "seconds": [0, 0], "tokens": [0, 0]},
                },
            },
        },
    }

    body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Token 区块渲染验证</title>
<style>
  body {{ background:#f3f6fa; font-family: system-ui, sans-serif; padding:20px; }}
  table {{ border-collapse: collapse; background:#fff; }}
  th, td {{ border:1px solid #d8e0ec; padding:6px 10px; font-size:13px; text-align:left; }}
  .note {{ font-size:12px; color:#555; margin-bottom:10px; }}
</style>
</head>
<body>
<p class="note">用真实 index.html 模板片段 + 真实 app.js 方法渲染（数据为构造的样例）。</p>
<div id="app">
{section}
</div>
<script src="../portal/static/vendor/petite-vue.iife.js"></script>
<script>
const appLabelStub = (app) => ({{ seedance: 'Seedance', 'nano-banana': '图像生成模块',
  'volcengine-portrait': '人像生成' }}[app] || app);

// app.js 里这些是**对象字面量的简写方法**，必须原样放进对象里，不能当语句粘。
const tokenMethods = {{
{chr(10).join('  ' + src + ',' for src in methods.values())}
}};

const state = {json.dumps(state, ensure_ascii=False, indent=2)};
Object.assign(state, tokenMethods);
state.appLabel = appLabelStub;
PetiteVue.createApp(state).mount('#app');
window.__harnessReady = true;
</script>
</body>
</html>
"""
    OUT.write_text(body, encoding="utf-8")
    print(f"wrote {OUT} ({len(body)} bytes); methods: {', '.join(methods)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
