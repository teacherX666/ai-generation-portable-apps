#!/usr/bin/env python3
"""Unified model gateway (stdlib only).

This is the single place where every sub-app resolves which provider to use
for a capability, checks local/cloud availability, and (for LLM) dispatches
with "local first, cloud fallback".

Sub-apps are launched with cwd=<app_dir>, so their Python sys.path[0] points at
the sub-app directory rather than the repo root. Before importing this module a
sub-app must add the repo root to sys.path, e.g.:

    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent
    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))
    from shared import model_gateway
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
REGISTRY_PATH = _HERE / "model_registry.json"

# Env overrides so deployment can change endpoints/models without editing JSON.
_ENV_BASE_URL = {
    "local_gateway": "AIPORT_BASE_URL",
    "local_llm": "LOCAL_LLM_BASE_URL",
}
_ENV_MODEL = {
    "local_llm": "LOCAL_LLM_MODEL",
}


class GatewayError(Exception):
    """Raised for upstream/network failures with a user-facing message."""

    def __init__(self, message: str, status_code: int = 502, raw_response: str = ""):
        self.message = message
        self.status_code = status_code
        self.raw_response = raw_response
        super().__init__(message)


def load_registry() -> dict[str, Any]:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


REGISTRY: dict[str, Any] = load_registry()


def providers() -> dict[str, Any]:
    return REGISTRY.get("providers", {})


def provider_config(name: str) -> dict[str, Any]:
    return providers().get(name, {})


def capabilities() -> dict[str, Any]:
    return REGISTRY.get("capabilities", {})


def capability_config(name: str) -> dict[str, Any]:
    return capabilities().get(name, {})


def provider_base_url(name: str) -> str:
    env_name = _ENV_BASE_URL.get(name)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip().rstrip("/")
    return str(provider_config(name).get("base_url", "")).rstrip("/")


def provider_model(name: str) -> str:
    env_name = _ENV_MODEL.get(name)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    return str(provider_config(name).get("model", ""))


def _env_key(name: str) -> str:
    for env_name in provider_config(name).get("env", []):
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()
    return ""


def request_json(
    method: str,
    url: str,
    api_key: str = "",
    body: dict[str, Any] | None = None,
    timeout: float = 120,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """urllib JSON helper; raises GatewayError on non-2xx/network errors."""
    hdrs: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = ""
        try:
            detail = json.loads(raw).get("error", {}).get("message", "") or raw[:200]
        except Exception:
            detail = raw[:200]
        if exc.code == 401:
            raise GatewayError("密钥无效或已过期（InvalidApiKey）", 401, raw)
        if exc.code == 429:
            raise GatewayError("请求过于频繁，稍后重试", 429, raw)
        raise GatewayError(detail or exc.reason or f"上游服务错误（{exc.code}）", exc.code, raw)
    except urllib.error.URLError as exc:
        raise GatewayError(f"网络错误：{exc.reason}", 502, "") from exc
    except (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
        raise GatewayError(f"本地模型请求超时或连接中断：{exc}", 504, "") from exc


def provider_ready(name: str, timeout: float = 1.5) -> bool:
    """Local providers are probed via their health endpoint; cloud is assumed reachable."""
    cfg = provider_config(name)
    if not cfg:
        return False
    if cfg.get("kind") != "local":
        return True
    base = provider_base_url(name)
    health = cfg.get("health_path", "/")
    if not base:
        return False
    try:
        with urllib.request.urlopen(base + health, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


def is_local_gateway_ready(timeout: float = 1.5) -> bool:
    return provider_ready("local_gateway", timeout)


def is_local_llm_ready(timeout: float = 1.5) -> bool:
    return provider_ready("local_llm", timeout)


def resolve_provider(capability: str, preferred: str | None = None, timeout: float = 1.5) -> str | None:
    """Pick the first usable provider for a capability (respecting local_first)."""
    cfg = capability_config(capability)
    ordered = list(cfg.get("providers", []))
    if not ordered:
        return None

    def usable(name: str) -> bool:
        pcfg = provider_config(name)
        if not pcfg:
            return False
        if pcfg.get("kind") == "local":
            return provider_ready(name, timeout)
        return True

    if preferred and preferred in ordered and usable(preferred):
        return preferred

    local = [p for p in ordered if provider_config(p).get("kind") == "local"]
    cloud = [p for p in ordered if provider_config(p).get("kind") != "local"]

    if cfg.get("local_first", True):
        for p in local:
            if provider_ready(p, timeout):
                return p
        if cloud:
            return cloud[0]
    else:
        if cloud:
            return cloud[0]
        for p in local:
            if provider_ready(p, timeout):
                return p
    return ordered[0]


def call_llm(
    messages: list[dict[str, str]],
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    local_first: bool = True,
    enable_thinking: bool = False,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 180,
) -> dict[str, Any]:
    """Dispatch a chat completion with local-first / cloud-fallback.

    Returns {"ok": True, "content", "provider", "model"} on success or
    {"ok": False, "error"} when no provider works. Local Qwen is expected to
    be an OpenAI-compatible endpoint (e.g. Ollama /v1); until it is ready this
    falls back to DeepSeek automatically.
    """
    cfg = capability_config("llm")
    ordered = list(cfg.get("providers", ["local_llm", "deepseek"]))

    def _kind(name: str) -> str:
        return str(provider_config(name).get("kind", ""))

    local = [p for p in ordered if _kind(p) == "local"]
    cloud = [p for p in ordered if _kind(p) != "local"]

    if provider:
        candidates = [provider] if provider in ordered else [provider] + ordered
    elif local_first:
        candidates = local + cloud
    else:
        candidates = cloud + local

    last_error = ""
    for name in candidates:
        pcfg = provider_config(name)
        if not pcfg:
            continue
        if pcfg.get("kind") == "local" and not provider_ready(name):
            last_error = f"{pcfg.get('label', name)} 当前不可用"
            continue
        use_model = model or provider_model(name)
        if not use_model:
            last_error = f"{pcfg.get('label', name)} 未配置模型名"
            continue
        use_key = api_key if api_key is not None else _env_key(name)
        if not use_key and pcfg.get("kind") != "local":
            last_error = f"{pcfg.get('label', name)} 未配置 API Key"
            continue
        base = provider_base_url(name)
        if not base:
            continue
        body = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if pcfg.get("kind") == "local":
            body["enable_thinking"] = enable_thinking
        try:
            result = request_json("POST", f"{base}/chat/completions", use_key, body, timeout=timeout)
            choices = result.get("choices") if isinstance(result, dict) else None
            content = ""
            if choices:
                content = str((choices[0].get("message", {}) or {}).get("content", "") or "")
            if content.strip():
                return {"ok": True, "content": content.strip(), "provider": name, "model": use_model}
            last_error = f"{pcfg.get('label', name)} 返回了空结果"
        except GatewayError as exc:
            last_error = exc.message
    return {"ok": False, "error": last_error or "没有可用的大模型 Provider"}


def status_payload(timeout: float = 1.5) -> dict[str, Any]:
    """Safe, key-free summary for UI config endpoints."""
    provider_summary: dict[str, Any] = {}
    for name, cfg in providers().items():
        provider_summary[name] = {
            "label": cfg.get("label", name),
            "kind": cfg.get("kind"),
            "ready": provider_ready(name, timeout),
            "has_key": bool(_env_key(name)),
            "model": provider_model(name),
        }
    capability_summary: dict[str, Any] = {}
    for name, cfg in capabilities().items():
        capability_summary[name] = {
            "label": cfg.get("label", name),
            "local_first": bool(cfg.get("local_first", True)),
            "providers": list(cfg.get("providers", [])),
            "resolved": resolve_provider(name, timeout=timeout),
        }
    return {"ok": True, "providers": provider_summary, "capabilities": capability_summary}