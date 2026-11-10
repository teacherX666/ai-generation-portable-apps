#!/usr/bin/env python3
"""Markdown → Word(.docx) 转换器（交接文档专用，除 python-docx 外无依赖）。

为什么要自己写：本仓库的文档大量使用「表格 + 行内代码 + 容器提示 + 多行列表项 + 代码块」，
现成的简易转换脚本会：
  · 丢掉转义竖线 `\\|` 与链接；
  · 把列表项的多行续行拆成普通段落；
  · **让全文的有序列表共用同一个编号序列（第二个列表从 6 开始）**；
  · 把 Markdown 的 `##` 章标题映射成 Word「标题 2」，导致大纲/目录整体缩进一级。

本脚本修掉了以上全部，并保证：
  · 每个有序列表**独立重新编号**（每个列表块一个独立的 numbering 实例）；
  · 标题层级对齐（`##` → 标题 1，`###` → 标题 2，`####` → 标题 3）；
  · 列表项的缩进续行并入同一项；
  · `- [ ]` 清单渲染成 ☐。

用法：
    python tools/md_to_docx.py <输入.md> [输出.docx] [--title "封面标题"] [--subtitle "副标题"]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

try:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor
except ImportError:  # pragma: no cover
    print("需要 python-docx：pip install python-docx", file=sys.stderr)
    raise SystemExit(2)

BODY_FONT = "Microsoft YaHei"
MONO_FONT = "Consolas"
CODE_COLOR = RGBColor(0xC7, 0x25, 0x4E)
QUOTE_COLOR = RGBColor(0x55, 0x55, 0x55)
LINK_COLOR = RGBColor(0x1D, 0x4E, 0xD8)
HEADING_COLORS = {
    1: RGBColor(0x11, 0x18, 0x27),
    2: RGBColor(0x1D, 0x4E, 0xD8),
    3: RGBColor(0x0F, 0x76, 0x6E),
    4: RGBColor(0x7C, 0x2D, 0x12),
}
CODE_SHADING = "F4F4F5"
HEADER_SHADING = "EFF2F7"

INLINE_RE = re.compile(
    r"(\*\*.+?\*\*|`[^`]+`|\*[^*\n]+?\*|\[[^\]\n]+\]\([^)\n]+\))"
)
MARKER_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")


# ─────────────────────────── 底层小工具 ───────────────────────────


def set_run_font(run, name: str = BODY_FONT) -> None:
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), name)


def shade_paragraph(paragraph, fill: str) -> None:
    pr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pr.append(shd)


def shade_cell(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def left_border(paragraph, color: str = "D0D5DD") -> None:
    pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), "12")
    left.set(qn("w:space"), "6")
    left.set(qn("w:color"), color)
    borders.append(left)
    pr.append(borders)


def set_numbering(paragraph, num_id: int, ilvl: int) -> None:
    """把 <w:numPr> 插到正确位置（必须在 pStyle 之后）。"""
    pPr = paragraph._p.get_or_add_pPr()
    numPr = OxmlElement("w:numPr")
    il = OxmlElement("w:ilvl")
    il.set(qn("w:val"), str(ilvl))
    ni = OxmlElement("w:numId")
    ni.set(qn("w:val"), str(num_id))
    numPr.append(il)
    numPr.append(ni)
    pStyle = pPr.find(qn("w:pStyle"))
    if pStyle is not None:
        pStyle.addnext(numPr)
    else:
        pPr.insert(0, numPr)


def add_inline(paragraph, text: str) -> None:
    for part in INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = paragraph.add_run(part[2:-2])
            run.bold = True
            set_run_font(run)
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            set_run_font(run, MONO_FONT)
            run.font.size = Pt(9)
            run.font.color.rgb = CODE_COLOR
        elif part.startswith("[") and "](" in part and part.endswith(")"):
            label, _, url = part[1:-1].partition("](")
            run = paragraph.add_run(label)
            set_run_font(run)
            run.font.color.rgb = LINK_COLOR
            run.underline = True
            if url and url != label:
                tail = paragraph.add_run(f"（{url}）")
                set_run_font(tail)
                tail.font.size = Pt(8)
                tail.font.color.rgb = QUOTE_COLOR
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            run.italic = True
            set_run_font(run)
        else:
            run = paragraph.add_run(part)
            set_run_font(run)


def add_code_block(doc, lines: list[str]) -> None:
    for raw in lines:
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        pf.left_indent = Inches(0.18)
        pf.line_spacing = 1.0
        shade_paragraph(p, CODE_SHADING)
        left_border(p)
        run = p.add_run(raw if raw.strip() else " ")
        set_run_font(run, MONO_FONT)
        run.font.size = Pt(8.5)
    tail = doc.add_paragraph()
    tail.paragraph_format.space_before = Pt(0)
    tail.paragraph_format.space_after = Pt(4)


# ─────────────────────────── 编号实例管理 ───────────────────────────


class Numbering:
    """为每个列表创建独立的 numbering 实例，保证各自从 1 开始、且各层级都有定义。

    背景（两个都必须自己解决，否则 Word 里编号是错的）：

    1. Word 的 `List Number` 样式绑定**同一个** numId，全文共用一条计数序列 →
       第二个列表会从上一个列表的末尾继续编号（症状：第二个列表从 6 开始）。
       解法：每个列表块新建一个 <w:num> 并显式挂到段落上（各实例计数独立）。

    2. python-docx 自带模板里的 abstractNum **每个只有 1 级**（只有 ilvl=0），
       而 Markdown 里缩进两格的"二级列表"要 ilvl=1 → Word 找不到该级定义，
       表现为**同一列表里所有条目显示同一个数字**（症状：易错点后面全是 3）。
       解法：自建一个 3 级的 abstractNum（decimal）与 3 级的 bullet abstractNum，
       完全不依赖模板定义。
    """

    def __init__(self, doc):
        self.ok = False
        self._next_num = 1
        self._next_abs = 1
        try:
            self.numbering = doc.part.numbering_part.element
        except Exception:
            return
        self._next_num = self._max_num_id() + 1
        self._next_abs = self._max_abstract_id() + 1
        # 只自建"有序列表"的多级定义；项目符号沿用内置样式（本来就正常，少动少错）
        self.decimal_abs = self._make_abstract("decimal")
        self.ok = True

    # ---- 现有 id 统计 ----
    def _max_num_id(self) -> int:
        ids = []
        for n in self.numbering.findall(qn("w:num")):
            try:
                ids.append(int(n.get(qn("w:numId"))))
            except (TypeError, ValueError):
                pass
        return max(ids) if ids else 0

    def _max_abstract_id(self) -> int:
        ids = []
        for a in self.numbering.findall(qn("w:abstractNum")):
            try:
                ids.append(int(a.get(qn("w:abstractNumId"))))
            except (TypeError, ValueError):
                pass
        return max(ids) if ids else 0

    # ---- 自建多级定义 ----
    @staticmethod
    def _make_lvl(ilvl: int, num_fmt: str, lvl_text: str, left: int, hanging: int):
        lvl = OxmlElement("w:lvl")
        lvl.set(qn("w:ilvl"), str(ilvl))
        start = OxmlElement("w:start")
        start.set(qn("w:val"), "1")
        fmt = OxmlElement("w:numFmt")
        fmt.set(qn("w:val"), num_fmt)
        txt = OxmlElement("w:lvlText")
        txt.set(qn("w:val"), lvl_text)
        jc = OxmlElement("w:lvlJc")
        jc.set(qn("w:val"), "left")
        pPr = OxmlElement("w:pPr")
        ind = OxmlElement("w:ind")
        ind.set(qn("w:left"), str(left))
        ind.set(qn("w:hanging"), str(hanging))
        pPr.append(ind)
        for el in (start, fmt, txt, jc, pPr):
            lvl.append(el)
        return lvl

    def _make_abstract(self, kind: str) -> int:
        """建一个 3 级 abstractNum；返回它的 abstractNumId。"""
        abs_id = self._next_abs
        self._next_abs += 1
        an = OxmlElement("w:abstractNum")
        an.set(qn("w:abstractNumId"), str(abs_id))
        nsid = OxmlElement("w:nsid")
        nsid.set(qn("w:val"), f"{abs_id:08X}")
        mlt = OxmlElement("w:multiLevelType")
        mlt.set(qn("w:val"), "multilevel")
        an.append(nsid)
        an.append(mlt)
        # 三级：bullet 用三种不同符号，decimal 统一 %N.
        for ilvl in range(3):
            if kind == "decimal":
                lvl = self._make_lvl(ilvl, "decimal", f"%{ilvl + 1}.", 720 + 360 * ilvl, 360)
            else:
                glyph = ["\uf0b7", "o", "\uf0a7"][ilvl]
                lvl = self._make_lvl(ilvl, "bullet", glyph, 720 + 360 * ilvl, 360)
            an.append(lvl)
        # schema 要求 abstractNum 全部排在 num 之前
        nums = self.numbering.findall(qn("w:num"))
        if nums:
            nums[0].addprevious(an)
        else:
            self.numbering.append(an)
        return abs_id

    def new_instance(self, levels) -> int:
        """为「一个有序列表块」建一个独立编号实例（各实例计数互不影响）。"""
        num_id = self._next_num
        self._next_num += 1
        num = OxmlElement("w:num")
        num.set(qn("w:numId"), str(num_id))
        a = OxmlElement("w:abstractNumId")
        a.set(qn("w:val"), str(self.decimal_abs))
        num.append(a)
        for lv in sorted(set(levels)):
            ov = OxmlElement("w:lvlOverride")
            ov.set(qn("w:ilvl"), str(lv))
            so = OxmlElement("w:startOverride")
            so.set(qn("w:val"), "1")
            ov.append(so)
            num.append(ov)
        self.numbering.append(num)
        return num_id


# ─────────────────────────── 表格 ───────────────────────────


def split_table_row(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells, buf, esc = [], [], False
    for ch in row:
        if esc:
            buf.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    cells.append("".join(buf).strip())
    return cells


def is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells)


def add_table(doc, rows: list[str]) -> None:
    header = split_table_row(rows[0])
    body = []
    for r in rows[1:]:
        cells = split_table_row(r)
        if is_separator(cells):
            continue
        body.append(cells)
    ncols = max([len(header)] + [len(r) for r in body]) if body else len(header)
    if ncols == 0:
        return
    table = doc.add_table(rows=1, cols=ncols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    for j in range(ncols):
        cell = table.rows[0].cells[j]
        cell.text = ""
        p = cell.paragraphs[0]
        add_inline(p, header[j] if j < len(header) else "")
        for run in p.runs:
            run.bold = True
        shade_cell(cell, HEADER_SHADING)
    for row_data in body:
        cells = table.add_row().cells
        for j in range(ncols):
            p = cells[j].paragraphs[0]
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            add_inline(p, row_data[j] if j < len(row_data) else "")
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


# ─────────────────────────── 列表块 ───────────────────────────


def peek_is_list(lines: list[str], i: int) -> bool:
    """跳过空行，看下一非空行还是不是列表项。"""
    j = i
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j < len(lines) and bool(MARKER_RE.match(lines[j]))


def collect_list(lines: list[str], i: int):
    """吃掉一个列表块（含缩进续行与内部空行）。返回 (items, next_i)。

    item = {"level": int, "ordered": bool, "parts": [str, ...]}
    """
    items: list[dict] = []
    cur: dict | None = None
    n = len(lines)
    while i < n:
        raw = lines[i]
        if not raw.strip():
            if cur is not None and peek_is_list(lines, i + 1):
                i += 1
                continue
            break
        m = MARKER_RE.match(raw)
        if m:
            indent, marker, text = m.group(1), m.group(2), m.group(3)
            ordered = bool(re.match(r"^\d+\.$", marker))
            level = 0 if len(indent) < 2 else (1 if len(indent) < 4 else 2)
            cur = {"level": level, "ordered": ordered, "parts": [text]}
            items.append(cur)
            i += 1
            continue
        # 缩进续行 → 并入当前项
        if cur is not None and len(raw) - len(raw.lstrip()) >= 2:
            cur["parts"].append(raw.strip())
            i += 1
            continue
        break
    return items, i


def render_list(doc, items: list[dict], numbering: Numbering) -> None:
    ord_levels = {it["level"] for it in items if it["ordered"]}
    dec_id = None
    if ord_levels and numbering.ok:
        dec_id = numbering.new_instance(ord_levels)

    for it in items:
        level = it["level"]
        ordered = it["ordered"]
        text = it["parts"][0]
        if text.strip().startswith("[ ]"):
            text = "☐ " + text.strip()[3:].strip()
        elif text.strip().startswith("[x]"):
            text = "☑ " + text.strip()[3:].strip()

        style = (["List Number", "List Number 2", "List Number 3"] if ordered
                 else ["List Bullet", "List Bullet 2", "List Bullet 3"])[level]
        try:
            p = doc.add_paragraph(style=style)
        except KeyError:
            p = doc.add_paragraph()
            if not ordered:
                text = "• " + text

        if ordered and dec_id is not None:
            # 显式挂编号：缩进交给编号定义里的 w:ind（左缩进 + 悬挂），
            # 这样数字与正文的悬挂对齐正确；项目符号不覆盖，沿用内置样式。
            set_numbering(p, dec_id, level)
        else:
            p.paragraph_format.left_indent = Inches(0.25 + 0.28 * level)
        add_inline(p, text)

        # 缩进续行：与该项正文左边缘对齐（0.5" + 0.25"×level）
        for extra in it["parts"][1:]:
            cp = doc.add_paragraph()
            cp.paragraph_format.left_indent = Inches(0.5 + 0.25 * level)
            cp.paragraph_format.space_before = Pt(0)
            add_inline(cp, extra)


# ─────────────────────────── 目录域 / 封面 ───────────────────────────


def add_toc(doc) -> None:
    p = doc.add_paragraph()
    run = p.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = r'TOC \o "1-3" \h \z \u'
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    hint = OxmlElement("w:t")
    hint.text = "（在 Word 中右键此处 → 更新域，生成带页码的目录）"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for el in (begin, instr, sep, hint, end):
        run._element.append(el)


def add_cover(doc, title: str, subtitle: str) -> None:
    for _ in range(3):
        doc.add_paragraph()
    h = doc.add_heading(level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = h.add_run(title)
    set_run_font(r)
    r.font.size = Pt(30)
    r.font.color.rgb = HEADING_COLORS[1]
    if subtitle:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        rr = p.add_run(subtitle)
        set_run_font(rr)
        rr.font.size = Pt(13)
        rr.font.color.rgb = QUOTE_COLOR
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rr = p.add_run("生成时间：" + dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    set_run_font(rr)
    rr.font.size = Pt(9)
    rr.font.color.rgb = QUOTE_COLOR
    doc.add_page_break()
    # 「目录」用普通段落，不进 Heading 大纲（否则会出现在自己的目录里）
    tp = doc.add_paragraph()
    tr = tp.add_run("目录")
    set_run_font(tr)
    tr.bold = True
    tr.font.size = Pt(16)
    tr.font.color.rgb = HEADING_COLORS[1]
    add_toc(doc)
    doc.add_page_break()


# ─────────────────────────── 主流程 ───────────────────────────


def convert(md_path: Path, docx_path: Path, title: str, subtitle: str) -> None:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(4)
    for lvl, color in HEADING_COLORS.items():
        try:
            st = doc.styles[f"Heading {lvl}"]
        except KeyError:
            continue
        st.font.color.rgb = color
        st.font.name = BODY_FONT

    numbering = Numbering(doc)

    add_cover(doc, title, subtitle)

    lines = md_path.read_text(encoding="utf-8").splitlines()
    i, n = 0, len(lines)
    skipped_title = False
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("<!--") and stripped.endswith("-->"):
            i += 1
            continue
        if not stripped:
            i += 1
            continue

        # 代码块
        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            add_code_block(doc, buf)
            continue

        # 表格
        if stripped.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            add_table(doc, rows)
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            hashes = len(m.group(1))
            if not skipped_title and hashes == 1:
                skipped_title = True
                if m.group(2).strip() == title.strip():
                    i += 1
                    continue
            # Markdown `##` 是本文的"章" → Word 标题 1（对齐大纲层级）
            level = min(max(hashes - 1, 1), 4)
            h = doc.add_heading(level=level)
            add_inline(h, m.group(2))
            for run in h.runs:
                set_run_font(run)
                run.font.color.rgb = HEADING_COLORS[level]
            i += 1
            continue

        # 列表块（必须在"引用块"之前判断，因为引用里也可能有列表）
        if MARKER_RE.match(line):
            items, i = collect_list(lines, i)
            render_list(doc, items, numbering)
            continue

        # 引用块
        if stripped.startswith(">"):
            body = stripped.lstrip(">").strip()
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.3)
            bullet = re.match(r"^[-*]\s+", body)
            if bullet:
                p.paragraph_format.left_indent = Inches(0.5)
                body = re.sub(r"^[-*]\s+", "• ", body)
            add_inline(p, body)
            for run in p.runs:
                run.italic = True
                if run.font.color.rgb is None:
                    run.font.color.rgb = QUOTE_COLOR
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        add_inline(p, stripped)
        i += 1

    doc.save(str(docx_path))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Markdown → Word(.docx)")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--title", default="")
    ap.add_argument("--subtitle", default="")
    args = ap.parse_args(argv[1:])

    md = Path(args.input)
    if not md.exists():
        print(f"找不到输入文件：{md}", file=sys.stderr)
        return 2
    out = Path(args.output) if args.output else md.with_suffix(".docx")
    title = args.title or md.stem
    convert(md, out, title, args.subtitle)
    print(f"OK {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
