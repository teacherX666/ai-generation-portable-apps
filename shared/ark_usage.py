#!/usr/bin/env python3
"""从上游（火山方舟）生成响应里取 token 用量。

为什么需要它：方舟**视频**任务是异步的，`GET /contents/generations/tasks/{id}`
的响应里带

    "usage": {"completion_tokens": 540900, "total_tokens": 540900}

而**图片** `POST /images/generations` 是同步的，响应里带

    "usage": {"input_images": 1, "generated_images": 8,
              "output_tokens": 23107, "total_tokens": 23107}

两种形状不同，取数口径必须唯一 —— 否则同一个用户在两个应用里的「token」不可比。
本模块是唯一口径：`total_tokens` → `completion_tokens` → `output_tokens`，
都没有就返回 0。

注意 `generated_images` / `input_images` **不是** token，绝不能拿它们顶替，
否则「图片张数」会被当成 token 混进统计。
"""
from __future__ import annotations

from typing import Any, Mapping

# 取值优先级：total 最权威；completion 是视频的口径；output 是老版图片的口径。
_TOKEN_KEYS = ("total_tokens", "completion_tokens", "output_tokens")


def as_token_int(value: Any) -> int:
    """把上游字段转成非负整数；布尔、非数字、负数一律当 0。"""
    if isinstance(value, bool):  # bool 是 int 的子类，必须先挡掉
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        try:
            parsed = int(float(value.strip()))
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0
    return 0


def extract_tokens(payload: Any) -> int:
    """返回该响应对应的 token 用量；缺失或不认识就返回 0。"""
    if not isinstance(payload, Mapping):
        return 0
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    for key in _TOKEN_KEYS:
        tokens = as_token_int(usage.get(key))
        if tokens > 0:
            return tokens
    return 0
