#!/usr/bin/env python3
"""把 shared-ui/ 的共享资源同步到各应用。

为什么需要它：shared-ui/ 里既有 CSS 也有 JS，但旧的 sync-ui.sh 只复制 CSS，
于是放在共享目录里的 JS 从来没有真正同步过。

在 Windows 上可直接运行（旧 .sh 需要 Git Bash），因此作为主力脚本；.sh 保留给其它机器。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "shared-ui"

#: 共享 JS：文件名 -> 目标目录（相对仓库根）。
#:
#: model-capabilities.js 于 2026-09-28 纳入同步。此前它躺在共享目录里却没人同步，
#: nano-banana 的本机副本悄悄比共享源多了 4 行能力校验（modes / response_formats）
#: 谁也没发现 —— 这正是「共享机制只覆盖 CSS」留下的洞。那 4 行现已回迁进共享源，
#: 三方统一。
#: 注：seedance 的 providers.json 不声明这两个字段，而校验逻辑遇到未声明的能力会
#: 自动跳过，所以回迁**不会改变 seedance 的行为**。
JS_TARGETS = {
    "job-cards.js": ["seedance/static", "nano-banana/static", "portal/static"],
    "model-capabilities.js": ["seedance/static", "nano-banana/static"],
}

#: 共享 CSS 的目标（与旧 sync-ui.sh 保持一致）
CSS_TARGETS = [
    "portal/static/ui", "seedance/static/ui", "nano-banana/static/ui",
    "dreamina/static/ui", "rag-assistant/static/ui",
    "volcengine-portrait/static/ui",
    "feishu-generation-agent/src/feishu_generation_agent/web/static/ui",
]


def sync_js() -> int:
    count = 0
    for name, dirs in JS_TARGETS.items():
        source = SRC / name
        if not source.exists():
            print(f"  WARN 共享源缺失: {source}", file=sys.stderr)
            continue
        for rel in dirs:
            dest_dir = REPO / rel
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest_dir / name)
            print(f"  JS   {name} -> {rel}")
            count += 1
    return count


def sync_css() -> int:
    count = 0
    core = SRC / "portal-ui-core.css"
    for rel in CSS_TARGETS:
        dest = REPO / rel
        dest.mkdir(parents=True, exist_ok=True)
        if core.exists():
            shutil.copyfile(core, dest / "portal-ui-core.css")
        for sub in ("tokens", "styles"):
            src_sub = SRC / sub
            if src_sub.is_dir():
                (dest / sub).mkdir(parents=True, exist_ok=True)
                for f in src_sub.iterdir():
                    if f.is_file():
                        shutil.copyfile(f, dest / sub / f.name)
        print(f"  CSS  portal-ui-core.css + tokens/ + styles/ -> {rel}")
        count += 1
    return count


def main() -> int:
    print("同步 shared-ui →")
    js = sync_js()
    css = sync_css()
    print(f"完成：{js} 个 JS 目标，{css} 个 CSS 目标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
