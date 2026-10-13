#!/usr/bin/env python3
"""只读探测：两个云代理（中转站）到底能不能查用量。

nano-banana 的代码注释写着「t8star / gemini 响应里没有 usage → 0」，但那是**响应体**层面；
中转站通常还有**用量/账单查询接口**（one-api / new-api 系）。这里只发只读 GET，
**不打印任何密钥**，只打印状态码与响应片段。

用法：python _diag/probe_relay_usage.py
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "relay_usage_probe.txt"


def load_keys() -> dict:
    path = ROOT / "nano-banana" / "state" / "secrets.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def call(url: str, key: str = "", extra: dict | None = None) -> tuple[int, str]:
    headers = {"Accept": "application/json", "User-Agent": "portal-usage-probe/1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, headers=headers, method="GET")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            body = resp.read(1200).decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(400).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


buf: list[str] = []
keys = load_keys()
buf.append(f"secrets.json 里的 provider 键：{sorted(keys.keys())}")
buf.append("（密钥值不打印）")
buf.append("")

# 各家中转站常见的只读用量/账单端点（one-api / new-api / OpenAI 兼容）
PATHS = [
    "/v1/dashboard/billing/usage?start_date=2026-10-01&end_date=2026-10-13",
    "/v1/dashboard/billing/subscription",
    "/api/user/self",
    "/api/status",
    "/v1/models",
]
HOSTS = {"chiyun": "https://chiyun.work", "t8star": "https://ai.t8star.org"}

for name, base in HOSTS.items():
    key = str(keys.get(name) or keys.get("gemini" if name == "chiyun" else name) or "").strip()
    buf.append(f"== {name}（{base}）密钥是否读到：{'是' if key else '否'} ==")
    for path in PATHS:
        status, body = call(base + path, key)
        snippet = " ".join(body.split())[:150]
        buf.append(f"  GET {path:<62} -> {status}  {snippet}")
    buf.append("")

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
