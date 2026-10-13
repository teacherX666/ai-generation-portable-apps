#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四件套打包器：standalone 单文件 + zip 包。

四件套的另两件：
  · 安装版  = 本目录自身（skill 目录就位即完成安装）
  · 公众号页 = 本目录下的 `fight-fx-anchor-prompter-公众号介绍页.html`（人维护，不进 zip，它是物料不是运行件）

★ 三条铁律
  1. **「我说包含了」≠「真的包含了」** → 拼装后对每份源文件做**行级覆盖校验**，覆盖率 < 98% 直接失败并列出漏掉的行。
     判据：打包这件事最危险的失败模式是「静默漏文件」，所以校验必须是机械的、逐文件的、会失败的。
  2. **打包后必须回读包内容再断言** → 不信 zipfile.write 的返回值，重新打开 zip 读一次 namelist。
  3. **不静默** → 每份源文件都打印 命中行/总行数；被排除的文件也打印原因。

用法：
  python tools/make_deliverables.py --version 1.0            # 日期取今天
  python tools/make_deliverables.py --version 1.0 --date 20260926
  python tools/make_deliverables.py --version 1.0 --dry-run   # 只校验不写盘
"""

import argparse
import datetime
import os
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
STANDALONE_NAME = f"{SKILL_NAME}-standalone.md"

# standalone 的拼装顺序（确定性：改这里就是改文档结构）
PARTS = [
    ("01 · 主控台（原 SKILL.md）",                    "SKILL.md",                          True),
    ("02 · 方法论完整版（原 references/fx-formula.md）", "references/fx-formula.md",          False),
    ("03 · 槽位词库（原 references/fx-vocabulary.md）",  "references/fx-vocabulary.md",       False),
    ("04 · 输出格式模板（原 references/engine-formats.md）", "references/engine-formats.md",   False),
    ("05 · 出图模板（原 references/image-prompts.md）",   "references/image-prompts.md",       False),
    ("06 · 案例库（原 case-library.md）",                "case-library.md",                   False),
    ("07 · 生图脚本（原 scripts/gen_image.py）",          "scripts/gen_image.py",              False),
]

# zip 排除：缓存 / 自身产物 / 凭证 / 版本归档
EXCLUDE_DIRS = {"__pycache__", ".git", "_versions", "tmp", ".tmp"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".zip", ".bak", ".key", ".pem")
EXCLUDE_NAMES = {".env", "keys.env", "token.json", "credentials.json", ".DS_Store"}
EXCLUDE_PREFIX = (".",)          # 隐藏文件一律不进包
COVERAGE_MIN = 0.98


def find_content_files():
    """扫目录里「应该进 standalone」的内容文件。

    ★ 这是本打包器**唯一真正会响**的门禁：PARTS 是手工维护的清单，
      新加一个 references 文件却忘了加进 PARTS → 单文件版静默少一章。
      行级覆盖率**抓不到**这种漏（拼装和校验用的是同一份内存副本，恒 100%）。
    """
    found = set()
    for pat in ("*.md", "references/*.md", "scripts/*.py"):
        for p in SKILL_ROOT.glob(pat):
            name = p.name
            if name == STANDALONE_NAME:          # 自身产物，不自我包含
                continue
            if "公众号" in name:                  # 宣传物料，不是运行内容
                continue
            found.add(p.relative_to(SKILL_ROOT).as_posix())
    return found


def strip_frontmatter(text):
    """去掉 YAML frontmatter，返回 (frontmatter_text, body_text)。"""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    fm = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    return fm, body


def line_coverage(src_body, dst_text):
    """行级覆盖率：源里每个「有意义的行」是否出现在目标里。返回 (命中, 总数, 漏掉的行)。"""
    lines = [l.strip() for l in src_body.splitlines()]
    lines = [l for l in lines if len(l) >= 8]      # 太短的行易误判（如 '---'）
    if not lines:
        return 0, 0, []
    missed = [l for l in lines if l not in dst_text]
    return len(lines) - len(missed), len(lines), missed


def build_standalone(version, date_str):
    header = f"""# {SKILL_NAME} · 单文件独立版（standalone）

> **版本**：V{version}　·　**生成日期**：{date_str}
> **本文件性质**：自包含单文件版 —— 把 `SKILL.md` + `references/` 全部内容 + 生图脚本合并为**一份**，
> 可直接投喂给任何 AI 使用，**不需要安装 skill 目录**。
> **拼装方式**：由 `tools/make_deliverables.py` 自动拼装并做**行级覆盖校验**（末尾附校验表），
> 不是手抄 —— 手抄必漏。
> **安装版路径**：`~/.workbuddy/skills/{SKILL_NAME}/`
> **两版内容一致**：安装版与单文件版是同一份内容的两种形态。

### 本文件包含

| 部分 | 来源 | 作用 |
|---|---|---|
| 一 | SKILL.md | 主控：两条方法论 + 选择卡 + 工作流 + 自检 |
| 二 | references/fx-formula.md | 方法论完整版（教程原文要点 + 实战迭代案例） |
| 三 | references/fx-vocabulary.md | 槽位词库（流体/粒子/锚点/撞击/反馈/光效） |
| 四 | references/engine-formats.md | Seedance 2.0 / 2.5 / H3 三种输出格式 |
| 五 | references/image-prompts.md | 主角图 / 场景图出图提示词模板 |
| 六 | case-library.md | 案例库 |
| 七 | scripts/gen_image.py | 贞贞工坊生图脚本（自包含，可直接保存为 .py 运行） |

---
"""
    chunks = [header]
    report = []
    fm_text = ""

    # ---- 0. 防漏文件门禁（唯一真正会响的那道）----
    declared = {rel for _, rel, _ in PARTS}
    actual = find_content_files()
    orphan = sorted(actual - declared)
    ghost = sorted(declared - actual)
    print("\n=== 内容文件清单核对（防漏章）===", flush=True)
    print(f"  目录内内容文件 {len(actual)} 份 / PARTS 声明 {len(declared)} 份", flush=True)
    if orphan:
        print(f"  ❌ 目录里有、PARTS 没声明 → 单文件版会静默少这些章：{orphan}", flush=True)
    if ghost:
        print(f"  ❌ PARTS 声明了、目录里没有 → 源缺失：{ghost}", flush=True)
    if orphan or ghost:
        print("  → 打包中止：修 PARTS 清单或补回源文件，不许出残包", flush=True)
        raise SystemExit(5)
    print("  ✅ 一一对应，无漏无虚", flush=True)

    for idx, (title, rel, drop_fm) in enumerate(PARTS, start=1):
        src = SKILL_ROOT / rel
        if not src.exists():
            print(f"  ❌ 源文件缺失：{rel} —— 打包中止（不许出残包）", flush=True)
            raise SystemExit(2)
        raw = src.read_text(encoding="utf-8")
        if drop_fm:
            fm_text, body = strip_frontmatter(raw)
        else:
            body = raw
        # 脚本类用围栏包起来，md 类直接嵌入
        if rel.endswith(".py"):
            block = f"## 第 {title} 部分\n\n> 保存为 `gen_image.py` 即可直接运行。\n\n```python\n{body.rstrip()}\n```\n"
        else:
            block = f"## 第 {title} 部分\n\n{body.rstrip()}\n"
        chunks.append("\n---\n\n" + block)
        report.append((rel, body))

    # 来源元信息（frontmatter 不丢，改成引用块）
    if fm_text:
        meta = "\n---\n\n## 附 · 源文件元信息（原 frontmatter）\n\n```yaml\n" + fm_text + "\n```\n"
        chunks.append(meta)

    text = "".join(chunks)

    # ---- 行级覆盖校验（源从**磁盘重读**，不是用拼装时的内存副本）----
    print("\n=== 行级覆盖校验 ===", flush=True)
    ok = True
    rows = []
    for rel, _ in report:
        body = (SKILL_ROOT / rel).read_text(encoding="utf-8")
        if rel == "SKILL.md":
            _, body = strip_frontmatter(body)
        hit, total, missed = line_coverage(body, text)
        ratio = (hit / total) if total else 1.0
        flag = "✅" if ratio >= COVERAGE_MIN else "❌"
        print(f"  {flag} {rel}：{hit}/{total} 行（{ratio:.1%}）", flush=True)
        rows.append((rel, hit, total, ratio))
        if ratio < COVERAGE_MIN:
            ok = False
            for line in missed[:5]:
                print(f"       漏：{line[:80]}", flush=True)
    if not ok:
        print("\n❌ 覆盖率不达标 —— 不写盘（宁可不产出，也不出漏内容的单文件）", flush=True)
        raise SystemExit(3)

    table = "\n---\n\n## 附 · 拼装完整性校验\n\n| 源文件 | 命中行 / 总行 | 覆盖率 |\n|---|---|---|\n"
    for rel, hit, total, ratio in rows:
        table += f"| `{rel}` | {hit} / {total} | {ratio:.1%} |\n"
    table += f"\n**校验器**：`tools/make_deliverables.py`（阈值 {COVERAGE_MIN:.0%}，不达标不出包）\n"
    text += table

    return text, rows


def collect_zip_entries():
    entries, skipped = [], []
    for root, dirs, files in os.walk(SKILL_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            p = Path(root) / f
            rel = p.relative_to(SKILL_ROOT)
            if f.startswith(EXCLUDE_PREFIX) or f in EXCLUDE_NAMES or f.lower().endswith(EXCLUDE_SUFFIX):
                skipped.append((str(rel), "凭证/缓存/自身产物"))
                continue
            if "公众号" in f:
                skipped.append((str(rel), "宣传物料（不是运行件）"))
                continue
            entries.append(rel)
    return sorted(entries), skipped


def make_zip(zip_path, entries, dry_run=False):
    if dry_run:
        return []
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for e in entries:
            src = SKILL_ROOT / e
            if src.is_dir():
                continue
            # arcname 必须用正斜杠：Windows 下 str(Path) 是反斜杠，解压到别的系统会出怪路径
            zf.write(src, arcname=(Path(SKILL_NAME) / e).as_posix())
    # ---- 回读断言：不信任写入动作 ----
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    return [e for e in entries if (Path(SKILL_NAME) / e).as_posix() not in names]


def main():
    ap = argparse.ArgumentParser(description="四件套打包器：standalone + zip")
    ap.add_argument("--version", required=True, help="版本号，如 1.0")
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y%m%d"),
                    help="日期 YYYYMMDD（默认今天）")
    ap.add_argument("--dry-run", action="store_true", help="只校验，不写盘")
    args = ap.parse_args()

    date_str = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
    print(f"skill      : {SKILL_NAME}")
    print(f"版本 / 日期 : V{args.version} / {date_str}")
    print(f"skill 根目录: {SKILL_ROOT}")

    # ---- 1. standalone ----
    text, rows = build_standalone(args.version, date_str)
    standalone_path = SKILL_ROOT / STANDALONE_NAME
    print(f"\n=== standalone ===\n  字符数：{len(text):,}")
    if not args.dry_run:
        standalone_path.write_text(text, encoding="utf-8")
        print(f"  [saved] {standalone_path}")
    else:
        print("  (dry-run，未写盘)")

    # ---- 2. zip ----
    entries, skipped = collect_zip_entries()
    if not args.dry_run and standalone_path.exists() and STANDALONE_NAME not in [str(e) for e in entries]:
        entries = sorted(entries + [Path(STANDALONE_NAME)])
    print(f"\n=== zip ({len(entries)} 项) ===")
    for e in entries:
        print(f"  + {e}")
    for rel, why in skipped:
        print(f"  - {rel}（排除：{why}）")

    zip_path = SKILL_ROOT.parent / f"{SKILL_NAME}-v{args.version}-{args.date}.zip"
    missing = make_zip(zip_path, entries, args.dry_run)
    if args.dry_run:
        print("  (dry-run，未写盘)")
    else:
        if missing:
            print(f"\n❌ 回读断言失败，包内缺 {len(missing)} 项：{missing}")
            raise SystemExit(4)
        size = zip_path.stat().st_size
        print(f"  [saved] {zip_path}（{size:,} 字节）")
        print(f"  ✅ 回读断言通过：{len(entries)} 项全部在包内")

    print("\n=== 完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
