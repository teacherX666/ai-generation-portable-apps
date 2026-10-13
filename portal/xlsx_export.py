"""极简 XLSX 生成器（零依赖）。

为什么手写而不装 openpyxl：本项目「第三方库按需使用，引入前先确认」，
而这里只需要「表头加粗 + 冻结首行 + 自动筛选 + 按列指定宽度」四件事，
用 zipfile + 几个 XML 就能做到，不必为此在本机解释器里加依赖。

CSV 的硬伤是**没有列宽信息** —— WPS/Excel 打开后要么全按默认窄列（长内容被截断），
要么一键「最适合列宽」把 200 字的提示词那列拉成一大条、其它列显得离得很远。
xlsx 由我们把宽度定死，打开即好看。

只支持：字符串/数字单元格、列宽、表头样式、冻结首行、自动筛选。
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

# XML 1.0 不允许的控制字符（除 \t \n \r），出现会让 Excel 直接报文件损坏
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# styles：0=普通，1=表头（加粗 + 浅底 + 细边框）
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
  <font><sz val="11"/><name val="等线"/></font>
  <font><b/><sz val="11"/><color rgb="FF1F2937"/><name val="等线"/></font>
</fonts>
<fills count="3">
  <fill><patternFill patternType="none"/></fill>
  <fill><patternFill patternType="gray125"/></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FFF1F5F9"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
  <border><left/><right/><top/><bottom/><diagonal/></border>
  <border><left style="thin"><color rgb="FFD8DEE9"/></left><right style="thin"><color rgb="FFD8DEE9"/></right>
    <top style="thin"><color rgb="FFD8DEE9"/></top><bottom style="thin"><color rgb="FFD8DEE9"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
  <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
  <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="常规" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _col_letter(idx: int) -> str:
    """0-based 列号 → A、B、…、Z、AA…"""
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _text(value) -> str:
    return _ILLEGAL_XML.sub("", str(value))


def _cell(ref: str, value, style: int | None = None) -> str:
    style_attr = f' s="{style}"' if style else ""
    if isinstance(value, bool) or value is None or value == "":
        return f'<c r="{ref}"{style_attr}/>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t xml:space="preserve">{escape(_text(value))}</t></is></c>'


def build_xlsx(headers: list[str], rows: list[list], widths: list[float] | None = None,
               sheet_name: str = "Sheet1", freeze_header: bool = True,
               autofilter: bool = True) -> bytes:
    """把表头 + 行数据打成 xlsx 字节流。widths 单位为「字符数」（Excel 的列宽单位）。"""
    ncols = len(headers)
    cols_xml = ""
    if widths:
        parts = []
        for i in range(ncols):
            w = widths[i] if i < len(widths) else 14
            parts.append(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>')
        cols_xml = "<cols>" + "".join(parts) + "</cols>"

    pane = ('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>') if freeze_header else ""
    view = f'<sheetViews><sheetView workbookViewId="0" tabSelected="1">{pane}</sheetView></sheetViews>'

    body = [f'<row r="1" ht="22" customHeight="1">' +
            "".join(_cell(f"{_col_letter(i)}1", h, 1) for i, h in enumerate(headers)) + "</row>"]
    for r, row in enumerate(rows, start=2):
        cells = "".join(_cell(f"{_col_letter(i)}{r}", v) for i, v in enumerate(row[:ncols]))
        body.append(f'<row r="{r}">{cells}</row>')

    filter_xml = (f'<autoFilter ref="A1:{_col_letter(ncols - 1)}{len(rows) + 1}"/>') if (autofilter and rows) else ""
    dim = f"A1:{_col_letter(ncols - 1)}{max(1, len(rows) + 1)}"
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dim}"/>{view}<sheetFormatPr defaultRowHeight="16"/>'
        f'{cols_xml}<sheetData>{"".join(body)}</sheetData>{filter_xml}'
        '</worksheet>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK.format(sheet=escape(sheet_name)))
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()
