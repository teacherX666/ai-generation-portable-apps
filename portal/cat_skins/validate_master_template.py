#!/usr/bin/env python3
"""Validate the frozen 16×16 master cell map against the classic skin."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MASTER_PATH = ROOT / "master-template-v1.json"
CLASSIC_PATH = ROOT / "classic-black-v1.json"

VALID_PERMISSIONS = {"locked_occupancy", "color_only", "patternable", "shape_optional", "transparent_only"}
VALID_PARTS = {
    "background", "ear", "inner_ear", "head_outline", "head", "eye", "pupil",
    "nose", "mouth_corner", "mouth_center", "chin", "body_outline", "body",
    "rump_boundary", "tail", "tail_root", "leg", "paw",
}
VALID_OVERLAYS = {"headwear", "wing", "cape", "face_costume"}


def load(path: Path):
    return json.loads(path.read_text("utf-8"))


def validate_master(master: dict, classic: dict) -> list[str]:
    errors: list[str] = []
    if master.get("schema_version") != "cat-master-template-v1":
        errors.append("schema_version 必须是 cat-master-template-v1")
    if master.get("status") != "frozen":
        errors.append("Master Template V1 必须处于 frozen 状态")
    if master.get("base_frame") != classic.get("frames", {}).get("a"):
        errors.append("base_frame 必须与经典黑猫 frame_a 完全一致")

    cells = master.get("cells")
    if not isinstance(cells, list) or len(cells) != 256:
        return errors + ["cells 必须恰好包含 256 格"]

    seen: set[tuple[int, int]] = set()
    base = classic["frames"]["a"]
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            errors.append(f"cells[{index}] 不是对象")
            continue
        x, y = cell.get("x"), cell.get("y")
        if not isinstance(x, int) or not isinstance(y, int) or not (0 <= x < 16 and 0 <= y < 16):
            errors.append(f"cells[{index}] 坐标非法: ({x},{y})")
            continue
        if (x, y) in seen:
            errors.append(f"坐标重复: ({x},{y})")
        seen.add((x, y))
        if cell.get("base_code") != base[y][x]:
            errors.append(f"({x},{y}) base_code 与经典母版不一致")
        if cell.get("base_part") not in VALID_PARTS:
            errors.append(f"({x},{y}) base_part 非法")
        permission = cell.get("permission")
        if permission not in VALID_PERMISSIONS:
            errors.append(f"({x},{y}) permission 非法")
        if cell.get("pattern_allowed") != (permission in {"patternable", "shape_optional"}):
            errors.append(f"({x},{y}) pattern_allowed 与 permission 不一致")
        overlays = cell.get("overlay_zones")
        if not isinstance(overlays, list) or set(overlays) - VALID_OVERLAYS:
            errors.append(f"({x},{y}) overlay_zones 非法")
        if base[y][x] == "." and permission != "transparent_only":
            errors.append(f"透明基础格 ({x},{y}) 必须是 transparent_only")
        if base[y][x] != "." and permission == "transparent_only":
            errors.append(f"占用基础格 ({x},{y}) 不能是 transparent_only")

    expected = {(x, y) for y in range(16) for x in range(16)}
    if seen != expected:
        errors.append("cells 未完整且唯一地覆盖 16×16 网格")

    regions = master.get("regions") or {}
    for permission in VALID_PERMISSIONS:
        coords = {tuple(v) for v in regions.get(permission, [])}
        actual = {(c["x"], c["y"]) for c in cells if c.get("permission") == permission}
        if coords != actual:
            errors.append(f"regions.{permission} 与 cells 不一致")

    overlays = master.get("overlay_zones") or {}
    for name in VALID_OVERLAYS:
        declared = {tuple(v) for v in (overlays.get(name) or {}).get("allowed", [])}
        actual = {(c["x"], c["y"]) for c in cells if name in c.get("overlay_zones", [])}
        if declared != actual:
            errors.append(f"overlay_zones.{name}.allowed 与 cells 不一致")

    face = {tuple(v) for v in regions.get("face_foreground", [])}
    actual_face = {(c["x"], c["y"]) for c in cells if c.get("final_face_foreground")}
    if face != actual_face:
        errors.append("regions.face_foreground 与 cells 不一致")
    return errors


def main() -> int:
    errors = validate_master(load(MASTER_PATH), load(CLASSIC_PATH))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OK: Master Template V1 完整覆盖 256 格，并与经典黑猫母版一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
