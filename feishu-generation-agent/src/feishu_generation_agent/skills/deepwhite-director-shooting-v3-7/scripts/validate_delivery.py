#!/usr/bin/env python3
"""Validate deterministic parts of a Deepwhite director delivery."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ID_PATTERNS = {
    "events": re.compile(r"^EV-\d+$"),
    "dialogues": re.compile(r"^DLG-\d+$"),
    "locks": re.compile(r"^LOCK-\d+$"),
}

REQUIRED_SECTIONS = (
    "剧本锁定",
    "资产",
    "故事板",
    "时长",
    "覆盖",
    "空间",
    "文字分镜",
    "Seedance 2.5",
    "连续性",
)

TIME_RANGE_RE = re.compile(r"(?P<start>\d+)\s*(?:—|–|-|至|~)\s*(?P<end>\d+)\s*秒")
PANEL_COUNT_RE = re.compile(r"(?:实际格数|故事板格数|共)\s*[:：]?\s*(\d+)\s*格")
FENCE_RE = re.compile(r"^```(?:text|markdown)?\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\{[^{}\n]+\}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查导演分镜交付中的机械约束，不替代剧情语义和返图审查。",
        epilog=(
            "manifest JSON 格式："
            '{"events":[{"id":"EV-01","text":"事件"}],'
            '"dialogues":[{"id":"DLG-01","speaker":"角色","text":"逐字台词"}],'
            '"locks":[{"id":"LOCK-01","text":"连续性事实"}]} '
        ),
    )
    parser.add_argument("delivery", type=Path, help="最终 Markdown 交付文件")
    parser.add_argument("--manifest", type=Path, help="剧本锁定 JSON 清单")
    parser.add_argument("--max-segment-seconds", type=int, default=30)
    parser.add_argument("--max-panels", type=int, default=16)
    return parser.parse_args()


def read_utf8(path: Path, label: str, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append(f"{label}不存在：{path}")
    except UnicodeDecodeError:
        errors.append(f"{label}不是有效 UTF-8：{path}")
    return ""


def load_manifest(path: Path, errors: list[str]) -> dict[str, list[dict[str, str]]]:
    raw = read_utf8(path, "manifest", errors)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        errors.append(f"manifest JSON 无法解析：第{exc.lineno}行第{exc.colno}列")
        return {}
    if not isinstance(data, dict):
        errors.append("manifest 顶层必须是对象")
        return {}

    normalized: dict[str, list[dict[str, str]]] = {}
    seen: set[str] = set()
    for category, pattern in ID_PATTERNS.items():
        items = data.get(category, [])
        if not isinstance(items, list):
            errors.append(f"manifest.{category} 必须是数组")
            continue
        normalized[category] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                errors.append(f"manifest.{category}[{index}] 必须是对象")
                continue
            item_id = str(item.get("id", ""))
            if not pattern.fullmatch(item_id):
                errors.append(f"manifest.{category}[{index}] 的ID无效：{item_id or '空'}")
                continue
            if item_id in seen:
                errors.append(f"manifest ID重复：{item_id}")
                continue
            seen.add(item_id)
            normalized[category].append({key: str(value) for key, value in item.items()})
    return normalized


def find_prompt_blocks(text: str) -> list[str]:
    blocks = []
    for match in FENCE_RE.finditer(text):
        block = match.group(1).strip()
        signals = sum(
            token in block
            for token in ("【详细情节】", "画面动作概述", "画面构图", "机位：", "动作/表演", "对白：", "声音：")
        )
        if signals >= 2:
            blocks.append(block)
    return blocks


def validate_manifest_coverage(
    text: str,
    manifest: dict[str, list[dict[str, str]]],
    errors: list[str],
    warnings: list[str],
) -> None:
    for category in ("events", "dialogues", "locks"):
        for item in manifest.get(category, []):
            item_id = item["id"]
            if item_id not in text:
                errors.append(f"交付中缺少锁定ID：{item_id}")
            item_text = item.get("text", "").strip()
            if category == "dialogues" and item_text and item_text not in text:
                errors.append(f"交付中缺少逐字台词：{item_id} {item_text}")
            if category == "dialogues" and item_text:
                speaker = item.get("speaker", "").strip()
                pos = text.find(item_text)
                nearby = text[max(0, pos - 120) : pos + len(item_text) + 120] if pos >= 0 else ""
                if speaker and speaker not in nearby:
                    warnings.append(f"台词附近未找到说话人，请人工确认：{item_id} {speaker}")

    dialogue_positions = [text.find(item["id"]) for item in manifest.get("dialogues", [])]
    present_positions = [pos for pos in dialogue_positions if pos >= 0]
    if present_positions != sorted(present_positions):
        errors.append("DLG 首次出现顺序与 manifest 不一致")


def validate_timestamps(
    block: str,
    block_number: int,
    max_seconds: int,
    errors: list[str],
) -> None:
    ranges = [(int(m.group("start")), int(m.group("end"))) for m in TIME_RANGE_RE.finditer(block)]
    if not ranges:
        errors.append(f"提示词{block_number}缺少连续时间戳")
        return
    if ranges[0][0] != 0:
        errors.append(f"提示词{block_number}的首个时间戳必须从0秒开始")
    for index, (start, end) in enumerate(ranges):
        if end <= start:
            errors.append(f"提示词{block_number}存在无效时间段：{start}—{end}秒")
        if index and start != ranges[index - 1][1]:
            previous_end = ranges[index - 1][1]
            errors.append(f"提示词{block_number}时间戳不连续：{previous_end}秒后接{start}秒")
    if max(end for _, end in ranges) > max_seconds:
        errors.append(f"提示词{block_number}超过单段时长上限{max_seconds}秒")


def validate_delivery(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    text = read_utf8(args.delivery, "交付文件", errors)
    if not text:
        return errors, warnings

    for section in REQUIRED_SECTIONS:
        if section not in text:
            warnings.append(f"未找到建议交付章节：{section}")

    if args.manifest:
        manifest = load_manifest(args.manifest, errors)
        validate_manifest_coverage(text, manifest, errors, warnings)
    else:
        warnings.append("未提供 --manifest，无法机械核对EV/DLG/LOCK与逐字台词")

    prompt_blocks = find_prompt_blocks(text)
    if not prompt_blocks:
        errors.append("未识别到正式视频提示词代码块")
    for number, block in enumerate(prompt_blocks, start=1):
        if PLACEHOLDER_RE.search(block):
            errors.append(f"提示词{number}仍含未替换占位符")
        validate_timestamps(block, number, args.max_segment_seconds, errors)
        if "需要BGM" in block and "不要BGM" in block:
            warnings.append(f"提示词{number}同时包含需要BGM与不要BGM")
        if "需要字幕" in block and "不要字幕" in block:
            warnings.append(f"提示词{number}同时包含需要字幕与不要字幕")

    for match in PANEL_COUNT_RE.finditer(text):
        count = int(match.group(1))
        if count > args.max_panels:
            errors.append(f"故事板格数{count}超过上限{args.max_panels}")

    no_storyboard = "不使用故事板" in text or "故事板方式：不使用" in text
    if no_storyboard and any(re.search(r"SB-\d+", block) for block in prompt_blocks):
        errors.append("不使用故事板分支的视频提示词仍包含SB面板引用")

    return errors, warnings


def main() -> int:
    args = parse_args()
    errors, warnings = validate_delivery(args)
    for message in errors:
        print(f"ERROR: {message}")
    for message in warnings:
        print(f"WARNING: {message}")
    if errors:
        print(f"RESULT: FAIL ({len(errors)} errors, {len(warnings)} warnings)")
        return 1
    print(f"RESULT: PASS (0 errors, {len(warnings)} warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
