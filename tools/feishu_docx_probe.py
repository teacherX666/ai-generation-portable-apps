#!/usr/bin/env python3
"""飞书 docx API 探测（第 2 轮）：convert 表格 / 空图片块 / replace_image / descendant 插入 / callout 变体。

上一轮结论：children API 支持 2/3/4/5/12/13/15/22；图片块建块时不能带 token；
表格块 = 31 / 单元格 = 32，只能走 descendant API；组织共享 PATCH 已验证。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "feishu-output-sync"))
from feishu import FeishuClient, FeishuError  # noqa: E402

CFG = json.loads((HERE / "feishu-output-sync" / "config.json").read_text("utf-8"))
client = FeishuClient(CFG["app_id"], CFG["app_secret"], folder_token=CFG.get("folder_token", ""))
ASSETS = HERE / "docs" / "seedance-manual" / "assets"

# 上一轮遗留的探测文档（创建成功但 DELETE docx 404），用 drive files 接口删
LEFTOVER_IDS = [
    "ZthtdqMZJoMDtSxZAD0cIT10n6b",  # 第 1 轮
    "XXN9dmUjxoLLQRxGShncLvArnag",  # 第 2 轮
    "OvmJdBXOwo5qGUxyr0DczKxUnRd",  # 第 3 轮
]


def p(label: str, fn):
    try:
        out = fn()
        print(f"  OK   {label}: {json.dumps(out, ensure_ascii=False)[:220]}")
        return out
    except FeishuError as e:
        print(f"  FAIL {label}: {e}")
        print(f"        detail={e.detail[:400]}")
        return None


def main() -> int:
    # 0. 清理遗留探测文档（drive files 删除接口）
    for doc_id in LEFTOVER_IDS:
        p(f"清理遗留文档 {doc_id[:8]}", lambda d=doc_id: client._json_call(
            "DELETE", f"/open-apis/drive/v1/files/{d}?type=docx"))

    # 1. 建临时探测文档
    r = client._json_call("POST", "/open-apis/docx/v1/documents", {"title": "【临时探测·可删】round2"})
    doc_id = r["data"]["document"]["document_id"]
    print("文档:", doc_id)

    # 2. convert API：markdown（含 GFM 表格）→ blocks
    md = """# 一级标题

正文**加粗**文字。

| 列A | 列B |
| --- | --- |
| 值1 | 值2 |
| 值3 | 值4 |

- 无序1
- 无序2
"""
    conv = p("convert markdown→blocks", lambda: client._json_call(
        "POST", "/open-apis/docx/v1/documents/blocks/convert",
        {"content_type": "markdown", "content": md}))
    types = []
    if conv:
        for b in conv.get("data", {}).get("blocks", []):
            types.append(b.get("block_type"))
        print("  convert 返回 block_types:", types)
        print("  first_level ids:", conv["data"].get("first_level_block_ids"))
        # 存一份给 descendant 用
        with open("/tmp/convert_blocks.json", "w") as f:
            json.dump(conv["data"], f, ensure_ascii=False)

    # 3. 空图片块（带 width/height）能否创建
    img_block_id = None
    r = p("空图片块(带尺寸)", lambda: client._json_call(
        "POST", f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
        {"children": [{"block_type": 27, "image": {"width": 1200, "height": 563}}]}))
    if r:
        img_block_id = r["data"]["children"][0]["block_id"]
        print("  image block id:", img_block_id)

    # 4. 图片媒体上传（parent_node=图片块）+ replace_image
    if img_block_id:
        png = (ASSETS / "I-taskmode.png").read_bytes()
        up = p("图片上传(parent_node=image block)", lambda: client._multipart(
            "/open-apis/drive/v1/medias/upload_all",
            {
                "file_name": "probe.png",
                "parent_type": "docx_image",
                "parent_node": img_block_id,
                "size": str(len(png)),
                "extra": json.dumps({"drive_route_token": doc_id}),
            },
            "file", "probe.png", png, "image/png"))
        if up:
            ft = up["data"]["file_token"]
            p("replace_image PATCH", lambda: client._json_call(
                "PATCH", f"/open-apis/docx/v1/documents/{doc_id}/blocks/{img_block_id}",
                {"replace_image": {"token": ft}}))

    # 5. callout 变体（两个空块，不挂子块）
    for name, co in [
        ("callout bg色", {"background_color": 1}),
        ("callout emoji+bg", {"emoji_id": "💡", "background_color": 1}),
    ]:
        p(name, lambda co=co: client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            {"children": [{"block_type": 19, "callout": co}]}))

    # 6. descendant 插入 convert 出来的块（清洗 parent_id / merge_info）
    try:
        data = json.load(open("/tmp/convert_blocks.json"))
        blocks = data["blocks"]
        first_ids = data["first_level_block_ids"]
        cleaned = []
        for b in blocks:
            b = dict(b)
            b.pop("parent_id", None)
            if b.get("block_type") == 31 and isinstance(b.get("table"), dict):
                prop = dict(b["table"].get("property") or {})
                prop.pop("merge_info", None)
                b["table"]["property"] = prop
            cleaned.append(b)
        r = client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant",
            {"children_id": first_ids, "descendants": cleaned, "index": -1})
        print("  OK   descendant 插入 convert 块, created:", len(r.get("data", {}).get("children", [])))
        for c in r["data"]["children"]:
            print("    created:", c.get("block_type"), c.get("block_id"))
    except FeishuError as e:
        print("  FAIL descendant 插入:", e)
        print("        detail:", e.detail[:500])

    # 7. 共享
    p("组织内共享", lambda: client._json_call(
        "PATCH", f"/open-apis/drive/v1/permissions/{doc_id}/public?type=docx",
        {"link_share_entity": "tenant_editable"}))

    print("PROBE2-DONE", doc_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
