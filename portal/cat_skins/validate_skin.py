#!/usr/bin/env python3
"""Validate a generated 16×16 cat skin without third-party dependencies."""

from __future__ import annotations

import json
import re
import sys
from collections import deque
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ANATOMY_PATH = ROOT / "cat-anatomy-v1.json"
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def occupied(frame: list[str], x: int, y: int) -> bool:
    return frame[y][x] != "."


def validate_data(skin: dict, anatomy: dict | None = None) -> list[str]:
    """Validate an already parsed skin object.

    The HTTP generation path uses this function directly so rejected model
    output never has to be persisted to disk first.
    """
    errors: list[str] = []
    anatomy = anatomy or load_json(ANATOMY_PATH)

    if not isinstance(skin, dict):
        return ["皮肤数据必须是 JSON 对象"]

    if skin.get("schema_version") != "cat-skin-v1":
        errors.append("schema_version 必须是 cat-skin-v1")

    rarity = skin.get("rarity")
    rarity_limits = anatomy["rarity_limits"]
    if rarity not in rarity_limits:
        errors.append("rarity 必须是 common/rare/epic/legendary")

    palette = skin.get("palette")
    if not isinstance(palette, dict) or not palette:
        errors.append("palette 必须是非空对象")
        palette = {}
    for key, color in palette.items():
        if not isinstance(key, str) or len(key) != 1 or key == ".":
            errors.append(f"非法 palette 键: {key!r}")
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            errors.append(f"palette[{key!r}] 必须是 #RRGGBB")
    if rarity in rarity_limits and len(palette) > rarity_limits[rarity]["max_palette_colors"]:
        errors.append(f"{rarity} 最多允许 {rarity_limits[rarity]['max_palette_colors']} 种颜色")

    frames = skin.get("frames") or {}
    frame_a = frames.get("a")
    frame_b = frames.get("b")
    for frame_name, frame in (("a", frame_a), ("b", frame_b)):
        if not isinstance(frame, list) or len(frame) != 16:
            errors.append(f"frame_{frame_name} 必须恰好 16 行")
            continue
        for y, row in enumerate(frame):
            if not isinstance(row, str) or len(row) != 16:
                errors.append(f"frame_{frame_name} 第 {y} 行必须恰好 16 个字符")
                continue
            unknown = sorted(set(row) - set(palette) - {"."})
            if unknown:
                errors.append(f"frame_{frame_name} 第 {y} 行含未声明字符: {unknown}")

    if errors or not isinstance(frame_a, list) or not isinstance(frame_b, list):
        return errors

    # 动画帧只能改变腿部区域。
    animation_box = anatomy["anatomy"]["legs"]["animation_box"]
    for y in range(16):
        for x in range(16):
            if frame_a[y][x] == frame_b[y][x]:
                continue
            inside = (
                animation_box["x_min"] <= x <= animation_box["x_max"]
                and animation_box["y_min"] <= y <= animation_box["y_max"]
            )
            if not inside:
                errors.append(f"两帧在腿部动画区外存在差异: ({x},{y})")

    # 固定的面部和身体锚点。
    required_groups = {
        "左耳": anatomy["anatomy"]["ears"]["left_required"],
        "右耳": anatomy["anatomy"]["ears"]["right_required"],
        "左眼": anatomy["anatomy"]["eyes"]["left_eye_box"],
        "右眼": anatomy["anatomy"]["eyes"]["right_eye_box"],
        "鼻子": anatomy["anatomy"]["muzzle"]["nose"],
        "猫嘴": anatomy["anatomy"]["muzzle"]["mouth_corners"],
        "下巴": anatomy["anatomy"]["head"]["flat_chin"],
        "身体核心": anatomy["anatomy"]["torso"]["required_core"],
        "封闭臀部": anatomy["anatomy"]["torso"]["closed_rump_boundary"],
        "尾巴根部": anatomy["anatomy"]["tail"]["root"]
    }
    for label, coords in required_groups.items():
        for x, y in coords:
            if not occupied(frame_a, x, y):
                errors.append(f"{label}缺少必需像素 ({x},{y})")

    # 头部各横行必须连续占满，确保闭环内部不会破洞。
    for y_text, ranges in anatomy["anatomy"]["head"]["required_occupied_rows"].items():
        y = int(y_text)
        for x_min, x_max in ranges:
            for x in range(x_min, x_max + 1):
                if not occupied(frame_a, x, y):
                    errors.append(f"头部闭环区域出现透明缺口 ({x},{y})")

    # 双瞳必须各自竖直连续、颜色一致，且中间隔离列不能使用瞳孔色。
    left_pupil = anatomy["anatomy"]["eyes"]["left_pupil"]
    right_pupil = anatomy["anatomy"]["eyes"]["right_pupil"]
    separator = anatomy["anatomy"]["eyes"]["separator"]
    left_codes = {frame_a[y][x] for x, y in left_pupil}
    right_codes = {frame_a[y][x] for x, y in right_pupil}
    pupil_codes = left_codes | right_codes
    if "." in pupil_codes or len(left_codes) != 1 or len(right_codes) != 1:
        errors.append("左右竖瞳必须各自使用一种连续的非透明颜色")
    if any(frame_a[y][x] in pupil_codes for x, y in separator):
        errors.append("双眼中间隔离列不能使用瞳孔颜色")

    # parts 坐标必须真实落在对应帧的非透明像素上。
    parts = skin.get("parts")
    if not isinstance(parts, dict):
        errors.append("parts 必须是对象")
        parts = {}
    required_part_names = {
        "eyes", "pupils", "nose", "mouth", "chin", "head_outline",
        "torso", "rump_boundary", "tail", "legs_a", "legs_b", "wing",
        "head_accessory"
    }
    for missing_name in sorted(required_part_names - set(parts)):
        errors.append(f"parts 缺少必需字段: {missing_name}")
    for part_name, coords in parts.items():
        if not isinstance(coords, list):
            errors.append(f"parts.{part_name} 必须是坐标数组")
            continue
        target = frame_b if part_name == "legs_b" else frame_a
        for coord in coords:
            if (
                not isinstance(coord, list)
                or len(coord) != 2
                or not all(isinstance(value, int) for value in coord)
            ):
                errors.append(f"parts.{part_name} 含非法坐标: {coord!r}")
                continue
            x, y = coord
            if not (0 <= x < 16 and 0 <= y < 16):
                errors.append(f"parts.{part_name} 坐标越界: ({x},{y})")
            elif not occupied(target, x, y):
                errors.append(f"parts.{part_name} 坐标对应透明像素: ({x},{y})")

    # 尾巴必须待在左侧设计区、避开地面，并从根部连续延伸。
    tail_coords = {
        tuple(coord)
        for coord in parts.get("tail", [])
        if isinstance(coord, list) and len(coord) == 2
    }
    tail_box = anatomy["anatomy"]["tail"]["design_box"]
    tail_forbidden = {tuple(coord) for coord in anatomy["anatomy"]["tail"]["forbidden"]}
    for x, y in sorted(tail_coords):
        if not (
            tail_box["x_min"] <= x <= tail_box["x_max"]
            and tail_box["y_min"] <= y <= tail_box["y_max"]
        ):
            errors.append(f"尾巴像素超出设计区: ({x},{y})")
        if (x, y) in tail_forbidden:
            errors.append(f"尾巴不得拖到地面区域: ({x},{y})")
    if not tail_coords:
        errors.append("tail 坐标不能为空")
    else:
        tail_seen = set()
        tail_queue = deque([next(iter(tail_coords))])
        while tail_queue:
            point = tail_queue.popleft()
            if point in tail_seen:
                continue
            tail_seen.add(point)
            x, y = point
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    neighbor = (x + dx, y + dy)
                    if neighbor in tail_coords and neighbor not in tail_seen:
                        tail_queue.append(neighbor)
        if tail_coords - tail_seen:
            errors.append("tail 坐标自身不连续")
        root = {tuple(coord) for coord in anatomy["anatomy"]["tail"]["root"]}
        touches_root = any(
            abs(tx - rx) <= 1 and abs(ty - ry) <= 1
            for tx, ty in tail_coords
            for rx, ry in root
        )
        if not touches_root:
            errors.append("尾巴没有从臀部根区伸出")

    wing_coords = {
        tuple(coord)
        for coord in parts.get("wing", [])
        if isinstance(coord, list) and len(coord) == 2
    }
    if tail_coords & wing_coords:
        errors.append("尾巴与翅膀不能共用像素")

    # 除声明的悬浮区域外，所有非透明像素必须与猫主体连通。
    floating = {
        tuple(coord)
        for region in skin.get("floating_regions", [])
        if isinstance(region, dict)
        for coord in region.get("pixels", [])
        if isinstance(coord, list) and len(coord) == 2
    }
    pixels = {
        (x, y)
        for y, row in enumerate(frame_a)
        for x, code in enumerate(row)
        if code != "." and (x, y) not in floating
    }
    if pixels:
        seen = set()
        queue = deque([next(iter(pixels))])
        while queue:
            point = queue.popleft()
            if point in seen:
                continue
            seen.add(point)
            x, y = point
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    neighbor = (x + dx, y + dy)
                    if neighbor in pixels and neighbor not in seen:
                        queue.append(neighbor)
        disconnected = sorted(pixels - seen)
        if disconnected:
            errors.append(f"存在未声明的悬空/断开像素: {disconnected[:8]}")

    return errors


def validate(path: Path) -> list[str]:
    return validate_data(load_json(path))


def main() -> int:
    if len(sys.argv) != 2:
        print(f"用法: {Path(sys.argv[0]).name} <cat-skin.json>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1]).resolve()
    try:
        errors = validate(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取失败: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("校验失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"校验通过: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
