#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包门禁：扫「本机绝对路径」与「真实密钥泄漏」。

用途：发版前对 **工作区** 与 **包本体** 各扫一遍，任一处非 0 命中就不许发包。

★ 五条判据（每条都是实测踩出来的）
  1. **凭证指纹运行时读取，绝不硬编码** —— 否则扫描器自己成了泄漏源。
  2. **只把 `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` 当密钥**；
     `*_BASE` / `*_URL` / `*_ENDPOINT` 是**公开地址，不报**。
     （实测误报：把 `SEEDANCE_API_BASE=https://api.seedance.nz` 当密钥报了。）
  3. **泛化占位符放行**（`C:/Users/<用户名>/...`）；但**真用户名一出现就硬拦**，
     即使它被尖括号包着 —— 占位符不能成为泄漏的免罪符。
  4. **不回显密钥本体** —— 命中只打印掩码，因为**报告本身也会被转发**。
  5. **样本要运行时拼装** —— 本文件与其测试都在被扫的目录里，
     字面量写样本会自指命中；解法是运行时拼，**不是**给扫描器加白名单。

用法：
  python tools/scan_package.py                          # 扫 skill 目录 + 同级 zip
  python tools/scan_package.py --path <目录> --zip <包>
  exit 0 = 干净；exit 3 = 有命中
"""

import argparse
import os
import re
import sys
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SKILL_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME = SKILL_ROOT.name

CRED_FILES = [
    Path.home() / ".workbuddy" / "credentials" / "keys.env",
    Path.home() / ".workbuddy" / "credentials" / "mv-auto-pipeline" / "keys.env",
]
CRED_SUFFIX = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD")

# 拼装敏感片段：扫描器就在被扫目录里，写死字面量会导致**自指命中**。
# 判据：解法是运行时拼装，**不是**给扫描器加白名单（加白名单＝给自己开后门）。
_MSYS_SEG = "/" + "c" + "/" + "Users" + "/"
PATH_RULES = [
    ("Windows 用户目录", re.compile(r"[A-Za-z]:[\\/]" + "Users" + r"[\\/]", re.I)),
    ("MSYS 用户目录",    re.compile(re.escape(_MSYS_SEG), re.I)),
    ("POSIX 家目录",     re.compile("/" + "(?:Users|home)" + "/", re.I)),
]

SECRET_PREFIX_RULES = [
    ("sk- 前缀",   re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("ghp_ 前缀",  re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("Bearer 串",  re.compile(r"Bearer\s+[A-Za-z0-9\-_.]{24,}")),
    ("私钥块",     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# 泛化占位符（这些词出现在尖括号里 = 文档模板，不是真路径）
PLACEHOLDER_WORDS = ("用户名", "你的", "user", "name", "your", "某人", "xxx", "...", "runtime", "路径", "path")
SKIP_SUFFIX = (".zip", ".png", ".jpg", ".jpeg", ".gif", ".pyc", ".pyo", ".ico", ".pdf")
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}


def mask(s):
    return (s[:4] + "*" * 8 + s[-4:]) if len(s) > 12 else "***"


def load_credentials():
    """返回 [(键名, 值)]，只含**看着像凭证**的键（排除 *_BASE/*_URL/*_ENDPOINT）。"""
    out = []
    for cand in CRED_FILES:
        if not cand.exists():
            continue
        for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if not v or len(v) < 12:
                continue
            if not k.upper().endswith(CRED_SUFFIX):
                continue          # ★ 判据 2：BASE/URL 是公开地址，跳过
            out.append((k, v))
    return out


def has_placeholder(line):
    """行内是否含占位符（尖括号/大括号/百分号/星号/省略号）。"""
    if re.search(r"[<>{}]|%[A-Za-z_]+%|\*{2,}|\.\.\.", line):
        return True
    return False


def is_generic_placeholder_path(line):
    """`C:/Users/<用户名>/x` 这类 → 放行。判据 3：尖括号里是泛化词才算占位符。"""
    if not has_placeholder(line):
        return False
    for m in re.finditer(r"[<{]([^>}]{1,40})[>}]", line):
        inner = m.group(1).strip().lower()
        if any(w.lower() in inner for w in PLACEHOLDER_WORDS) or not inner:
            return True
    return False


def scan_text(label, text, creds, username):
    hits = []
    for lno, line in enumerate(text.splitlines(), 1):
        # ---- 真用户名：任何情况下都硬拦（占位符也救不了）----
        if username and re.search(r"\b" + re.escape(username) + r"\b", line):
            hits.append(("PATH", "本机用户名", label, lno, line.strip()[:90]))

        # ---- 绝对路径 ----
        for name, rx in PATH_RULES:
            if rx.search(line) and not is_generic_placeholder_path(line):
                hits.append(("PATH", name, label, lno, line.strip()[:90]))

        # ---- 真实凭证值 ----
        for k, v in creds:
            if v in line:
                hits.append(("SECRET(real)", k, label, lno, mask(v)))

        # ---- 凭证前缀 ----
        for name, rx in SECRET_PREFIX_RULES:
            m = rx.search(line)
            if m:
                hits.append(("SECRET(prefix)", name, label, lno, mask(m.group())))
    return hits


def _iter_files(root):
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            p = Path(r) / f
            if p.suffix.lower() in SKIP_SUFFIX or f.startswith("."):
                continue
            yield p


def scan_dir(root, creds, username):
    hits, n = [], 0
    for p in _iter_files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        n += 1
        hits += scan_text("dir:" + str(p.relative_to(root)), text, creds, username)
    return n, hits


def scan_zip(path, creds, username):
    hits, n = [], 0
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith("/") or Path(name).suffix.lower() in SKIP_SUFFIX:
                continue
            try:
                text = zf.read(name).decode("utf-8")
            except Exception:
                continue
            n += 1
            hits += scan_text("zip:" + name, text, creds, username)
    return n, hits


def main():
    ap = argparse.ArgumentParser(description="打包门禁：本机路径 + 密钥泄漏扫描")
    ap.add_argument("--path", default=str(SKILL_ROOT), help="要扫的目录（默认本 skill）")
    ap.add_argument("--zip", dest="zip_path", default=None, help="要扫的 zip（默认同级同名包）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = Path(args.path)
    creds = load_credentials()
    username = os.environ.get("USERNAME") or os.environ.get("USER") or ""

    total_hits = []
    n1, h1 = scan_dir(root, creds, username)
    total_hits += h1
    if not args.quiet:
        print(f"工作区：{root.name} —— {n1} 个文本文件，{len(h1)} 处命中")

    zpath = Path(args.zip_path) if args.zip_path else (root.parent / f"{root.name}-v1.0-20260926.zip")
    if zpath.exists():
        n2, h2 = scan_zip(zpath, creds, username)
        total_hits += h2
        if not args.quiet:
            print(f"包本体：{zpath.name} —— {n2} 个文件，{len(h2)} 处命中")
    elif not args.quiet:
        print(f"包本体：{zpath.name} 不存在，跳过")

    if not args.quiet:
        print(f"凭证指纹库：{len(creds)} 条（运行时读自 keys.env，不回显本体）")

    if not total_hits:
        print("\n✅ 0 命中 —— 无本机绝对路径、无密钥泄漏")
        return 0
    print(f"\n❌ {len(total_hits)} 处命中：")
    for kind, name, label, lno, snip in total_hits:
        print(f"  [{kind}] {name} @ {label}:{lno}  {snip}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
