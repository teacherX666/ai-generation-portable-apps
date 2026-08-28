"""Plain-language failure explanations through the Ark seed chat model.

``ark_errors.py`` 的本地规则表未命中时，用 doubao-seed 对话模型把技术性
错误改写为 ≤100 字可操作中文。三级降级：本地规则 → 模型 → 原文。

同步实现（urllib + 线程锁）——两个子应用后端都是 stdlib 线程模型。
去重与负面缓存都在进程内存：同一 job 只解释一次（并发轮询共享一次模型
调用），同一 (code, detail) 解释失败后 10 分钟内不再重试，避免模型未开通
时每次失败都白等 15 秒超时。

模型与超时可配：环境变量 ``ERROR_EXPLAINER_MODEL``（默认
doubao-seed-1-6-flash-250615）。前置条件：所用 Ark 账号需开通该对话模型；
未开通时全部失败静默降级，不影响现有行为。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

_SYSTEM_INSTRUCTION = (
    "你是 AI 视频/图像生成平台的技术支持助手。用户收到平台返回的错误，"
    "请把错误码和原始信息翻译成一句普通人能看懂的中文：说明发生了什么、"
    "大概率是什么原因、用户下一步该怎么做。不超过 100 字，不要道歉，不要编造细节。"
)

_MAX_EXPLANATION_CHARS = 300
_MAX_CODE_CHARS = 64
_MAX_DETAIL_CHARS = 500
_NEGATIVE_CACHE_TTL = 600.0

_LOCKS: dict = {}
_CACHE: dict = {}
_NEGATIVE_UNTIL: dict = {}


def explain_error(job_id: str, code: str, detail: str, api_key: str,
                  model: Optional[str] = None, timeout: float = 15.0) -> Optional[str]:
    """把 (code, detail) 解释成中文建议。任何失败（无 key / 网络 / 非 200 /
    解析失败 / 模型未开通）都返回 None，调用方保留原文即可。"""
    code, detail = (code or "").strip(), (detail or "").strip()
    key = (api_key or "").strip()
    if not code or not detail or len(code) > _MAX_CODE_CHARS or len(detail) > _MAX_DETAIL_CHARS or len(key) < 8:
        return None
    existing = _CACHE.get(job_id)
    if existing is not None:
        return existing

    cache_key = (code, detail)
    if _NEGATIVE_UNTIL.get(cache_key, 0.0) > time.time():
        return None

    lock = _LOCKS.setdefault(job_id, threading.Lock())
    try:
        with lock:
            if job_id in _CACHE:
                return _CACHE[job_id]
            model_id = model or os.environ.get("ERROR_EXPLAINER_MODEL") or "doubao-seed-1-6-flash-250615"
            if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", model_id) is None:
                return None
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": _SYSTEM_INSTRUCTION},
                    {"role": "user", "content": f"错误码：{code}\n原始信息：{detail}"},
                ],
                "temperature": 0.2,
                "max_tokens": 400,
            }
            request = urllib.request.Request(
                _ENDPOINT,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read(16384)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
                _NEGATIVE_UNTIL[cache_key] = time.time() + _NEGATIVE_CACHE_TTL
                return None
            try:
                data = json.loads(body)
                content = data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                _NEGATIVE_UNTIL[cache_key] = time.time() + _NEGATIVE_CACHE_TTL
                return None
            if not isinstance(content, str) or not content.strip():
                _NEGATIVE_UNTIL[cache_key] = time.time() + _NEGATIVE_CACHE_TTL
                return None
            explanation = content.strip()[:_MAX_EXPLANATION_CHARS]
            _CACHE[job_id] = explanation
            return explanation
    finally:
        _LOCKS.pop(job_id, None)
