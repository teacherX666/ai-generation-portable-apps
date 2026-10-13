#!/usr/bin/env python3
"""只读探测（带密钥）：new-api 中转站能不能拉到**按请求的用量/额度**。

两家都是 new-api（one-api 系）：chiyun.work / ai.t8star.org。
new-api 常见只读接口（都不需要登录，只要令牌）：
  POST/GET /v1/dashboard/billing/usage?start_date&end_date   → 区间额度（美分）
  GET      /api/log/token?key=<sk>&p=0&page_size=50          → **该令牌的按请求日志**
                                                                （模型 / 额度 / 时间 / 用时）
  GET      /api/log/self?...                                 → 需要 access token（没有，跳过）
只打印状态码与字段名，**不打印密钥**。
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "relay_usage_probe2.txt"


def keys() -> dict:
    path = ROOT / "nano-banana" / "state" / "secrets.json"
    return (json.loads(path.read_text(encoding="utf-8")).get("provider_keys") or {})


def call(url: str, key: str = "") -> tuple[int, str]:
    headers = {"Accept": "application/json", "User-Agent": "portal-usage-probe/1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25, context=ssl.create_default_context()) as resp:
            return resp.status, resp.read(4000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(600).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


buf: list[str] = []
ks = keys()
today = "2026-10-13"
for name, base in (("gemini(chiyun.work)", "https://chiyun.work"), ("t8star", "https://ai.t8star.org")):
    slot = "gemini" if name.startswith("gemini") else "t8star"
    key = str(ks.get(slot) or "").strip()
    buf.append(f"===== {name}  密钥{'已读到' if key else '缺失'} =====")
    if not key:
        buf.append("  跳过（没有密钥）\n")
        continue
    probes = [
        ("额度-区间", f"{base}/v1/dashboard/billing/usage?start_date=2026-10-01&end_date={today}"),
        ("额度-订阅", f"{base}/v1/dashboard/billing/subscription"),
        ("令牌日志(new-api)", f"{base}/api/log/token?key={urllib.parse.quote(key)}&p=0&page_size=20"),
        ("用量汇总(new-api)", f"{base}/api/log/self?p=0&page_size=5"),
        ("模型列表", f"{base}/v1/models"),
    ]
    for label, url in probes:
        status, body = call(url, key)
        buf.append(f"  [{label}] {url.split('?')[0]} -> HTTP {status}")
        text = " ".join(body.split())
        buf.append(f"      {text[:400]}")
        # 若是 JSON 且有 data 数组，打印首条的字段名，看有没有「用量/额度」
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            items = data.get("data")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                keys_seen = sorted(items[0].keys())
                buf.append(f"      条数={len(items)} 首条字段={keys_seen}")
            elif isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                buf.append(f"      data 字段={sorted(data['data'].keys())[:20]}")
    buf.append("")

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
