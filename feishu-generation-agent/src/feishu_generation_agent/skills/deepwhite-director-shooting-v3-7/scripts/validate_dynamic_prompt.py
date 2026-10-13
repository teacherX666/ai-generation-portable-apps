#!/usr/bin/env python3
"""Mechanical checks for Seedance dynamic prompts.

This validator checks structure and common risk phrases. It does not judge choreography.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REQUIRED_SECTIONS = [
    "【一句话概述】",
    "【全局设定】",
    "【初始位置关系】",
    "【详细情节】",
    "【全局连续性与结尾约束】",
]

LEGACY_END_SECTION = "【全局补充 / 结尾约束】"

SLOWDOWN_RISKS = [
    "绝对静止",
    "固定锁死",
    "无呼吸起伏",
    "禁止改变焦段",
    "突然稳定并停住",
    "保持当前姿势不动",
    "完成动作后停住",
    "绝对静止",
]

ABSTRACT_ACTIONS = [
    "激烈战斗",
    "激烈打斗",
    "高速战斗",
    "展开搏斗",
    "展开激战",
    "速度感很强",
    "冲击感十足",
]

PHYSICAL_MARKERS = [
    "接触",
    "命中",
    "挡开",
    "格开",
    "凹陷",
    "后滑",
    "击退",
    "掀飞",
    "飞散",
    "碎裂",
    "崩散",
    "冲击",
    "余势",
    "惯性",
    "改变方向",
]

TIME_RE = re.compile(r"(?m)^\s*(\d+(?:\.\d+)?)\s*[—–-]\s*(\d+(?:\.\d+)?)\s*秒")


def read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def detailed_section(text: str) -> str:
    start = text.find("【详细情节】")
    if start < 0:
        return text
    end = text.find("【全局连续性与结尾约束】", start)
    if end < 0:
        end = text.find(LEGACY_END_SECTION, start)
    return text[start:] if end < 0 else text[start:end]


def validate(text: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    positions = []
    for section in REQUIRED_SECTIONS:
        index = text.find(section)
        if section == "【全局连续性与结尾约束】" and index < 0:
            index = text.find(LEGACY_END_SECTION)
            if index >= 0:
                warnings.append("使用旧版【全局补充 / 结尾约束】标题；正式v3.6请改为【全局连续性与结尾约束】")
        if index < 0:
            errors.append(f"缺少必需区块：{section}")
        positions.append(index)
    present_positions = [p for p in positions if p >= 0]
    if len(present_positions) == len(REQUIRED_SECTIONS) and present_positions != sorted(present_positions):
        errors.append("必需区块顺序不符合提示词契约")

    if "@图片" in text or "@视频" in text or "@素材" in text:
        if "【素材说明】" not in text:
            warnings.append("发现素材句柄但缺少【素材说明】")
        if "不参考" not in text:
            warnings.append("素材说明可能缺少“不参考什么”的边界")

    detail = detailed_section(text)
    for phrase in SLOWDOWN_RISKS:
        if phrase in detail:
            warnings.append(f"详细情节中出现潜在减速/冻结措辞：{phrase}")

    abstract_hits = [phrase for phrase in ABSTRACT_ACTIONS if phrase in detail]
    if abstract_hits and not any(marker in detail for marker in PHYSICAL_MARKERS):
        warnings.append("存在抽象战斗描述，但未发现明显接触、受力或结果语言")

    if "镜头" in detail and "衔接：" not in detail and "节点衔接：" not in detail:
        warnings.append("多镜头/路径内容可能缺少显式衔接状态")

    if "关键接触与反馈：" in detail and not any(marker in detail for marker in PHYSICAL_MARKERS):
        warnings.append("关键接触字段可能缺少可见结果或物理后果")

    ranges = [(float(a), float(b)) for a, b in TIME_RE.findall(detail)]
    for start, end in ranges:
        if end <= start:
            errors.append(f"无效时间段：{start:g}—{end:g}秒")
    for (start_a, end_a), (start_b, end_b) in zip(ranges, ranges[1:]):
        if start_b < end_a:
            errors.append(f"时间段重叠：{start_a:g}—{end_a:g} 与 {start_b:g}—{end_b:g}")
        elif start_b > end_a:
            warnings.append(f"时间轴存在空档：{end_a:g}—{start_b:g}秒")

    if len(ranges) > 12:
        warnings.append("时间段超过12个，检查是否把连续动作过度拆成微节拍")

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Seedance fight prompt")
    parser.add_argument("prompt", help="UTF-8 prompt file, or - for stdin")
    args = parser.parse_args()

    try:
        text = read_text(args.prompt)
    except (OSError, UnicodeError) as exc:
        print(f"ERROR: 无法读取提示词：{exc}")
        return 2

    errors, warnings = validate(text)
    for item in errors:
        print(f"ERROR: {item}")
    for item in warnings:
        print(f"WARNING: {item}")

    if errors:
        print(f"FAIL: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"PASS: 0 error(s), {len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
