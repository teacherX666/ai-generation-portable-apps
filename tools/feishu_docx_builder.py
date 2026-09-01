"""飞书 docx 使用说明文档构建器（共享模块）。

所有子应用的使用说明文档都走这套（API 实测要点见 feishu_docx_probe.py 与
记忆 feishu-docx-api）。用法见各 build_*_manual_doc.py。

内容 item 元组：
  ("h2"|"h3", text) / ("p", text) / ("quote", text)
  ("bullet"|"ordered", [text, ...])
  ("callout", emoji, color, text)   # 单行文本！emoji∈{bulb,info,warning,loudspeaker,pushpin}, color 1-7
  ("table", [表头], [[行], ...])
  ("image", filename)               # 相对 assets 目录；>1200px 自动 sips 缩宽
  ("divider",)
inline 支持 **粗体** 与 `行内代码`；p/quote/bullet/ordered 支持 \n 换行。
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "feishu-output-sync"))
from feishu import FeishuClient  # noqa: E402

INLINE_RE = re.compile(r"(\*\*.+?\*\*|`.+?`)")


def load_client() -> FeishuClient:
    cfg = json.loads((HERE / "feishu-output-sync" / "config.json").read_text("utf-8"))
    return FeishuClient(cfg["app_id"], cfg["app_secret"], folder_token=cfg.get("folder_token", ""))


def parse_inline(text: str) -> list[dict]:
    runs = []
    for part in INLINE_RE.split(text):
        if not part:
            continue
        style = {}
        content = part
        if part.startswith("**") and part.endswith("**"):
            style["bold"] = True
            content = part[2:-2]
        elif part.startswith("`") and part.endswith("`"):
            style["inline_code"] = True
            content = part[1:-1]
        runs.append({"text_run": {"content": content, "text_element_style": style}})
    return runs


def text_elements(text: str) -> list[dict]:
    lines = [l for l in text.split("\n") if l]
    if not lines:
        return [{"text_run": {"content": "", "text_element_style": {}}}]
    elements = []
    for l in lines:
        elements.extend(parse_inline(l))
        elements.append({"text_run": {"content": "", "text_element_style": {}}})
    elements.pop()  # 去掉最后一个多余换行
    return elements


def png_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        f.read(16)
        w, h = struct.unpack(">II", f.read(8))
    return w, h


def prep_image(src: Path) -> tuple[Path, int, int]:
    w, h = png_size(src)
    if w <= 1200:
        return src, w, h
    dst = Path(tempfile.gettempdir()) / f"sd-manual-{src.stem}.png"
    subprocess.run(
        ["sips", "--resampleWidth", "1200", str(src), "--out", str(dst)],
        check=True, capture_output=True)
    nw, nh = png_size(dst)
    return dst, nw, nh


class DocBuilder:
    def __init__(self, client: FeishuClient, doc_id: str):
        self.client = client
        self.doc = doc_id
        self.root = doc_id
        self.stats = {"blocks": 0, "images": 0, "tables": 0}

    def _children(self, blocks: list[dict]) -> None:
        self.client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{self.root}/children",
            {"children": blocks})
        self.stats["blocks"] += len(blocks)

    def h2(self, text: str) -> None:
        self._children([{"block_type": 4, "heading2": {"elements": parse_inline(text)}}])

    def h3(self, text: str) -> None:
        self._children([{"block_type": 5, "heading3": {"elements": parse_inline(text)}}])

    def p(self, text: str) -> None:
        self._children([{"block_type": 2, "text": {"elements": text_elements(text), "style": {}}}])

    def quote(self, text: str) -> None:
        self._children([{"block_type": 15, "quote": {"elements": text_elements(text)}}])

    def bullet(self, items: list[str]) -> None:
        for it in items:
            self._children([{"block_type": 12, "bullet": {"elements": text_elements(it)}}])

    def ordered(self, items: list[str]) -> None:
        for it in items:
            self._children([{"block_type": 13, "ordered": {"elements": text_elements(it)}}])

    def divider(self) -> None:
        self._children([{"block_type": 22, "divider": {}}])

    def callout(self, emoji: str, color: int, text: str) -> None:
        r = self.client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{self.root}/children",
            {"children": [{"block_type": 19, "callout": {
                "background_color": color, "emoji_id": emoji}}]})
        co = r["data"]["children"][0]
        self.stats["blocks"] += 1
        kid = co["children"][0]
        self.client._json_call(
            "PATCH", f"/open-apis/docx/v1/documents/{self.doc}/blocks/batch_update",
            {"requests": [{"block_id": kid, "update_text_elements": {
                "elements": text_elements(text)}}]})

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        md = "| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) + "\n"
        md += "\n".join("| " + " | ".join(r) + " |" for r in rows)
        conv = self.client._json_call(
            "POST", "/open-apis/docx/v1/documents/blocks/convert",
            {"content_type": "markdown", "content": md})
        data = conv["data"]
        descendants = []
        for b in data["blocks"]:
            b = dict(b)
            b.pop("parent_id", None)
            if b.get("block_type") == 31 and isinstance(b.get("table"), dict):
                prop = dict(b["table"].get("property") or {})
                prop.pop("merge_info", None)
                b["table"]["property"] = prop
            descendants.append(b)
        self.client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{self.root}/descendant",
            {"children_id": data["first_level_block_ids"], "descendants": descendants, "index": -1})
        self.stats["tables"] += 1

    def image(self, assets_dir: Path, fname: str) -> None:
        src = assets_dir / fname
        path, w, h = prep_image(src)
        r = self.client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{self.root}/children",
            {"children": [{"block_type": 27, "image": {"width": w, "height": h}}]})
        bid = r["data"]["children"][0]["block_id"]
        blob = path.read_bytes()
        up = self.client._multipart(
            "/open-apis/drive/v1/medias/upload_all",
            {
                "file_name": fname,
                "parent_type": "docx_image",
                "parent_node": bid,
                "size": str(len(blob)),
                "extra": json.dumps({"drive_route_token": self.doc}),
            },
            "file", fname, blob, "image/png")
        self.client._json_call(
            "PATCH", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{bid}",
            {"replace_image": {"token": up["data"]["file_token"]}})
        self.stats["images"] += 1
        self.stats["blocks"] += 1


def build_doc(client: FeishuClient, title: str, content: list, assets_dir: Path) -> str:
    """建文档、灌内容、开组织共享，返回 document_id。"""
    cfg = json.loads((HERE / "feishu-output-sync" / "config.json").read_text("utf-8"))
    r = client._json_call(
        "POST", "/open-apis/docx/v1/documents",
        {"title": title, "folder_token": cfg.get("folder_token", "")})
    doc_id = r["data"]["document"]["document_id"]
    b = DocBuilder(client, doc_id)
    for item in content:
        kind = item[0]
        if kind == "h2":
            b.h2(item[1])
        elif kind == "h3":
            b.h3(item[1])
        elif kind == "p":
            b.p(item[1])
        elif kind == "quote":
            b.quote(item[1])
        elif kind == "bullet":
            b.bullet(item[1])
        elif kind == "ordered":
            b.ordered(item[1])
        elif kind == "callout":
            b.callout(item[1], item[2], item[3])
        elif kind == "table":
            b.table(item[1], item[2])
        elif kind == "image":
            b.image(assets_dir, item[1])
        elif kind == "divider":
            b.divider()
    client._json_call(
        "PATCH", f"/open-apis/drive/v1/permissions/{doc_id}/public?type=docx",
        {"link_share_entity": "tenant_editable"})
    print("统计:", json.dumps(b.stats, ensure_ascii=False))
    return doc_id
