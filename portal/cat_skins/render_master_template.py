#!/usr/bin/env python3
"""Render Master Template V1 debug views as a dependency-free SVG."""
from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT / "master-template-v1.json"
OUTPUT_PATH = ROOT / "master-template-v1-debug.svg"

PART_COLORS = {
    "background": "#F5F7FA", "ear": "#A855F7", "inner_ear": "#E879F9",
    "head_outline": "#6D28D9", "head": "#C4B5FD", "eye": "#FDE047",
    "pupil": "#111827", "nose": "#FB7185", "mouth_corner": "#F472B6",
    "mouth_center": "#EC4899", "chin": "#8B5CF6", "body_outline": "#1D4ED8",
    "body": "#93C5FD", "rump_boundary": "#2563EB", "tail": "#22C55E",
    "tail_root": "#15803D", "leg": "#F59E0B", "paw": "#B45309",
}
PERMISSION_COLORS = {
    "locked_occupancy": "#EF4444", "color_only": "#F97316",
    "patternable": "#22C55E", "shape_optional": "#A855F7",
    "transparent_only": "#E5E7EB",
}
OVERLAY_COLORS = {
    "headwear": "#FACC15", "wing": "#38BDF8", "cape": "#F472B6",
    "face_costume": "#A78BFA",
}


def rect(x, y, w, h, fill, stroke="#CBD5E1", sw=1, opacity=1):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" opacity="{opacity}"/>'


def text(x, y, value, size=14, fill="#111827", weight="400", anchor="start"):
    return f'<text x="{x}" y="{y}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="{size}" fill="{fill}" font-weight="{weight}" text-anchor="{anchor}">{html.escape(str(value))}</text>'


def grid_panel(title, subtitle, ox, oy, cells, fill_fn, label_fn=None, cell=28):
    out = [text(ox, oy - 38, title, 24, weight="700"), text(ox, oy - 14, subtitle, 13, "#64748B")]
    for i in range(17):
        if i < 16:
            out.append(text(ox + i * cell + cell / 2, oy - 5, i, 10, "#64748B", anchor="middle"))
            out.append(text(ox - 9, oy + i * cell + cell * .66, i, 10, "#64748B", anchor="middle"))
    by_xy = {(c["x"], c["y"]): c for c in cells}
    for y in range(16):
        for x in range(16):
            c = by_xy[(x, y)]
            fill = fill_fn(c)
            out.append(rect(ox + x * cell, oy + y * cell, cell, cell, fill, "#FFFFFF", 1))
            if label_fn:
                label = label_fn(c)
                if label:
                    out.append(text(ox + x * cell + cell / 2, oy + y * cell + cell * .68, label, 9, "#0F172A", "600", "middle"))
    out.append(rect(ox, oy, cell * 16, cell * 16, "none", "#0F172A", 2))
    return out


def main():
    data = json.loads(TEMPLATE_PATH.read_text("utf-8"))
    cells = data["cells"]
    width, height = 1540, 1420
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', rect(0, 0, width, height, "#FFFFFF", "none")]
    svg += [text(54, 58, "Classic Black Cat · Master Template V1", 34, weight="800"),
            text(54, 88, "16×16 全坐标部位、修改权限与装饰覆盖区调试图（坐标从 0 开始）", 16, "#475569")]

    def base_fill(c):
        code = c["base_code"]
        if code == ".": return "#F8FAFC"
        return data.get("preview_palette", {}).get(code, {"O":"#14161B","F":"#30343D","I":"#64E49A","P":"#141416","N":"#DB5369","S":"#5B6271"}.get(code,"#94A3B8"))

    svg += grid_panel("A. 最终经典黑猫母版", "冻结轮廓；以后不得被生成器擅自改写", 54, 154, cells, base_fill)
    svg += grid_panel("B. 每格基础部位", "颜色代表解剖角色；透明格也明确标记为 background", 532, 154, cells, lambda c: PART_COLORS[c["base_part"]])
    svg += grid_panel("C. 每格修改权限", "红=锁定占用，绿=可画花纹，紫=高稀有轮廓候选", 1010, 154, cells, lambda c: PERMISSION_COLORS[c["permission"]], lambda c: {"locked_occupancy":"L","color_only":"C","patternable":"P","shape_optional":"S","transparent_only":""}[c["permission"]])

    # Overlay union view: show base in gray, then stripe cells by highest-priority zone.
    priority = ["headwear", "wing", "cape", "face_costume"]
    def overlay_fill(c):
        zones=c["overlay_zones"]
        return OVERLAY_COLORS[next((z for z in reversed(priority) if z in zones), "headwear")] if zones else "#E5E7EB"
    svg += grid_panel("D. 装饰允许覆盖区", "黄=头饰，蓝=翅膀，粉=披风，紫=面罩；重叠格按后层显示", 54, 704, cells, overlay_fill)

    # Legends and stats.
    x0, y0 = 548, 708
    svg += [text(x0, y0, "图例与执行约定", 26, weight="800")]
    y=y0+36
    for key,label in data["permission_legend"].items():
        svg.append(rect(x0,y-15,18,18,PERMISSION_COLORS[key],"none")); svg.append(text(x0+28,y,label,14)); y+=30
    y+=12
    svg.append(text(x0,y,"渲染图层顺序",18,weight="700")); y+=27
    svg.append(text(x0,y," → ".join(data["render_layers"]),14,"#334155")); y+=42
    svg.append(text(x0,y,"花纹职责分工",18,weight="700")); y+=27
    svg.append(text(x0,y,"程序决定：稀有度、花纹家族、色板角色、密度、对称倾向、随机种子、装饰家族",14,"#334155")); y+=24
    svg.append(text(x0,y,"AI决定：合法坐标内的具体色块布局、受控不对称和主题标记",14,"#334155")); y+=42
    svg.append(text(x0,y,"核心规则",18,weight="700")); y+=27
    for line in [
        "• 未规定的基础像素不画；透明格只有被 overlay zone 授权时才可使用。",
        "• 五官和下巴位于最终前景层，面罩/皇冠不能让脸失去可读性。",
        "• 翅膀必须连接背部 attachment zone，且不能与尾巴共用像素。",
        "• AI只提交坐标操作；程序渲染最终16×16并派生第二帧腿部动画。",
    ]:
        svg.append(text(x0,y,line,14,"#334155")); y+=25

    pc=Counter(c["permission"] for c in cells); bc=Counter(c["base_part"] for c in cells)
    sx, sy=1050, 708
    svg += [text(sx,sy,"模板统计",26,weight="800")]; sy+=38
    for k in ["locked_occupancy","color_only","patternable","shape_optional","transparent_only"]:
        svg.append(rect(sx,sy-15,18,18,PERMISSION_COLORS[k],"none")); svg.append(text(sx+28,sy,f"{k}: {pc[k]} 格",14)); sy+=30
    sy+=12
    svg.append(text(sx,sy,f"256 格已完整分类：{sum(pc.values())} / 256",16,"#0F766E","700")); sy+=34
    svg.append(text(sx,sy,f"有像素：{256-bc['background']} 格；透明背景：{bc['background']} 格",14,"#334155")); sy+=26
    svg.append(text(sx,sy,"状态：FROZEN · classic-black-master-v1",14,"#334155","700"))

    svg.append(text(54,1378,"生成日期：2026-08-28  ·  文件：portal/cat_skins/master-template-v1.json",13,"#64748B"))
    svg.append('</svg>')
    OUTPUT_PATH.write_text(''.join(svg), 'utf-8')
    print(OUTPUT_PATH)

if __name__ == "__main__":
    main()
