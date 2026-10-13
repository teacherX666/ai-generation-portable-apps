#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
`tools/scan_package.py` 的对抗测试。

★ 为什么必须有这个文件
  「它现在没响」永远不能证明「它不会响」。一个恒不报警的门禁看起来和「环境干净」一模一样。
  所以每条规则都要有一个**该响的样本**（证明会响）和一个**不该响的样本**（证明不误报）。

★ 两条工程约束
  1. **样本一律运行时拼装** —— 本文件就在被扫的目录里，直接写字面量会自指命中。
     解法是拼装字符串，**不是**给扫描器加白名单。
  2. **误报也是故障** —— 每修一个真实误报（如把公开端点 URL 当密钥），就固化成一条反向断言。

用法：python tools/test_scan_package.py
"""

import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_package as sp  # noqa: E402

# ---------------- 样本（全部运行时拼装，源码里不出现真字面量） ----------------
BS = chr(92)                                        # 反斜杠
U_SEG = "/" + "Users" + "/"                         # 拼装：源码里不出现该字面量
H_SEG = "/" + "home" + "/"
FAKE_USER = "pro" + "beuser"                        # 假用户名（非本机）
FAKE_WIN = "C" + ":" + BS + "Users" + BS + FAKE_USER + BS + "x.png"
FAKE_POSIX = H_SEG + FAKE_USER + "/x.png"
GEN_PLACEHOLDER_1 = "C:" + U_SEG + "<" + "用户名" + ">/x.png"
GEN_PLACEHOLDER_2 = H_SEG + "<" + "your" + "_name" + ">/x.png"
ENDPOINT_LINE = "SEEDANCE_API_BASE=" + "https" + "://" + "api.seedance.nz"
SK_PLACEHOLDER = "sk-" + "x" * 8                    # 长度不足，是占位
SK_REAL_SHAPE = "sk-" + "a" * 30                    # 形状像真密钥
GHP_SHAPE = "ghp_" + "b" * 30
BEARER_SHAPE = "Bearer " + "c" * 30
PRIVKEY = "-" * 5 + "BEGIN " + "RSA PRIVATE KEY" + "-" * 5
ENV_VARNAME = 'os.environ["' + "X_API" + '_KEY"]'

REAL_USER = os.environ.get("USERNAME") or os.environ.get("USER") or ""
USER_IN_ANGLE = ("C:" + U_SEG + "<" + REAL_USER + ">/x.png") if REAL_USER else ""

creds = sp.load_credentials()

CASES = [
    # (说明, 文本, 期望命中)
    ("真·Windows 用户目录",              FAKE_WIN,                     True),
    ("真·POSIX 家目录",                  FAKE_POSIX,                   True),
    ("真·本机用户名裸出现",              "作者机是 " + (REAL_USER or "nobody"), bool(REAL_USER)),
    ("泛化占位符 <用户名>",              GEN_PLACEHOLDER_1,            False),
    ("泛化占位符 <your_name>",           GEN_PLACEHOLDER_2,            False),
    ("公开端点 URL（误报来源）",          ENDPOINT_LINE,                False),
    ("sk- 占位（太短）",                 SK_PLACEHOLDER,               False),
    ("环境变量名（不是值）",              ENV_VARNAME,                  False),
    ("sk- 真实形状",                     SK_REAL_SHAPE,                True),
    ("ghp_ 真实形状",                    GHP_SHAPE,                    True),
    ("Bearer 长串",                      BEARER_SHAPE,                 True),
    ("私钥块",                           PRIVKEY,                      True),
    # 防削弱：加了「占位符放行」之后，尖括号里写**真用户名**仍必须拦
    ("防削弱：<真用户名> 仍须拦",         USER_IN_ANGLE,                bool(REAL_USER)),
]


def main():
    print(f"凭证指纹库：{len(creds)} 条（运行时读自 keys.env）")
    print(f"本机用户名：{REAL_USER or '(未获取)'}")
    print(f"用例数：{len(CASES)}\n")

    # 真密钥值用例（读不到就跳过，不算失败）
    if creds:
        k0, v0 = creds[0]
        CASES.append((f"真密钥值泄露（{k0}）", "token = " + v0, True))

    passed = failed = skipped = 0
    for desc, text, expect in CASES:
        if desc.startswith("真密钥值泄露") and not creds:
            skipped += 1
            continue
        if expect and text == "":
            skipped += 1
            print(f"  ⏭  {desc}（环境不具备，跳过）")
            continue
        hits = sp.scan_text("probe", text, creds, REAL_USER)
        got = bool(hits)
        if got == expect:
            passed += 1
            flag = "✅" if expect else "✅"
            print(f"  {flag} {desc}：{'命中' if got else '未命中'}（符合预期）")
        else:
            failed += 1
            print(f"  ❌ {desc}：期望 {'命中' if expect else '不命中'}，实际 {'命中' if got else '不命中'}")
            for h in hits[:3]:
                print(f"        {h[0]} / {h[1]} / {h[4][:70]}")

    print(f"\n结果：PASS {passed} / FAIL {failed} / SKIP {skipped}")
    if failed:
        print("❌ 门禁测试未通过 —— 修扫描器或修预期，不许放行")
        return 1
    print("✅ 全部通过：该报的会报，不该报的不报")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
