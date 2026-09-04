from __future__ import annotations

import json
import os
import socket
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parent
CONFIG_PATH = REPO_ROOT / "config" / "local_ai.env"
ENV_NAME = "AIPORT_BASE_URL"
DEFAULT_URL = "http://127.0.0.1:8801"
HEALTH_PATH = "/api/modules"

_source = "environment" if os.environ.get(ENV_NAME) else "default"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_config() -> None:
    """Load the machine-local gateway URL from config/local_ai.env.

    An explicitly exported AIPORT_BASE_URL always wins. This file is the only
    deployment-specific local-model setting in the repository.
    """
    global _source

    if os.environ.get(ENV_NAME):
        _source = "environment"
        return
    if not CONFIG_PATH.exists():
        _source = "default"
        return
    try:
        for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            key, sep, value = line.partition("=")
            if not sep or not key.strip():
                continue
            if key.strip().upper() == ENV_NAME and not os.environ.get(ENV_NAME):
                os.environ[ENV_NAME] = _unquote(value)
                _source = "config/local_ai.env"
                return
    except OSError:
        _source = "default"


def raw_configured_url() -> str:
    load_config()
    return (os.environ.get(ENV_NAME) or DEFAULT_URL).strip().rstrip("/")


def configured_url() -> str:
    value = raw_configured_url()
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return DEFAULT_URL.rstrip("/")
    return value


def force_ipv4(url: str) -> str:
    """Resolve .local/mDNS names to the first IPv4 address.

    Python's urllib tries every getaddrinfo result sequentially and can burn a
    long timeout on an unreachable IPv6 link-local result before reaching IPv4.
    """
    if not url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    host = parts.hostname or ""
    if (
        not host
        or host.count(".") == 3
        or host.lower() in {"localhost", "::1"}
    ):
        return url
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        first = socket.getaddrinfo(
            host, port, socket.AF_INET, socket.SOCK_STREAM
        )[0][4][0]
    except (OSError, IndexError):
        return url
    netloc = first if parts.port is None else f"{first}:{parts.port}"
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


def resolved_url() -> str:
    return force_ipv4(configured_url())


def probe(timeout: float = 1.5) -> tuple[bool, str]:
    url = f"{resolved_url()}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500, ""
    except Exception as exc:
        return False, str(exc)


def ready(timeout: float = 1.5) -> bool:
    ok, _ = probe(timeout)
    return ok


def snapshot(timeout: float = 1.5) -> dict[str, Any]:
    load_config()
    raw = raw_configured_url()
    configured = configured_url()
    configured_error = "" if raw == configured else f"invalid AIPORT_BASE_URL: {raw!r}"
    resolved = force_ipv4(configured)
    ok, error = probe(timeout)
    return {
        "configured_url": configured,
        "configured_error": configured_error,
        "resolved_url": resolved,
        "health_url": f"{resolved}{HEALTH_PATH}",
        "ready": ok,
        "error": error,
        "source": _source,
        "config_file": str(CONFIG_PATH) if CONFIG_PATH.exists() else None,
    }


def diagnostic_text(timeout: float = 1.5) -> str:
    info = snapshot(timeout)
    lines = [
        f"configured_url : {info['configured_url']}",
        f"resolved_url   : {info['resolved_url']}",
        f"health_url     : {info['health_url']}",
        f"source         : {info['source']}",
        f"config_file    : {info['config_file']}",
        f"ready          : {info['ready']}",
    ]
    if info.get("configured_error"):
        lines.append(f"configured_error: {info["configured_error"]}")
    if info["error"]:
        lines.append(f"error          : {info['error']}")
    return "\n".join(lines)


def as_json(timeout: float = 1.5) -> str:
    return json.dumps(snapshot(timeout), ensure_ascii=False, indent=2)