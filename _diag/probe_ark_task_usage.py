#!/usr/bin/env python3
"""只读探针：回查一个历史方舟视频生成任务，确认响应里有没有 usage（token 用量）。

零成本：GET 已存在的任务，不创建新任务。
不打印 API Key。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from shared.net import UPSTREAM
except ImportError:  # pragma: no cover
    UPSTREAM = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SECRETS = ROOT / "seedance" / "state" / "secrets.json"
TASK_ID = sys.argv[1] if len(sys.argv) > 1 else "cgt-20261010161242-moeh8"
BASE = "https://ark.cn-beijing.volces.com/api/v3"


def main() -> int:
    key = (json.loads(SECRETS.read_text("utf-8")).get("volcengine_api_key") or "").strip()
    if not key:
        print("no key", file=sys.stderr)
        return 2
    print(f"key present: len={len(key)} prefix={key[:4]}***")
    url = f"{BASE}/contents/generations/tasks/{TASK_ID}"
    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {key}"})
    try:
        with UPSTREAM.open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            print(f"HTTP {resp.status}")
    except urllib.error.HTTPError as exc:  # noqa: F821
        raw = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERR {type(exc).__name__}: {exc}")
        return 1

    data = json.loads(raw)
    print("=== top-level keys ===")
    print(sorted(data.keys()))
    print("=== usage ===")
    print(json.dumps(data.get("usage"), ensure_ascii=False, indent=2))
    print("=== duration / model / status ===")
    print(json.dumps({k: data.get(k) for k in ("id", "model", "status", "duration", "created_at", "updated_at")},
                     ensure_ascii=False, indent=2))
    print("=== FULL (content urls truncated) ===")
    trimmed = json.loads(raw)
    content = trimmed.get("content")
    if isinstance(content, dict):
        for k, v in list(content.items()):
            if isinstance(v, str) and len(v) > 80:
                content[k] = v[:80] + "..."
    print(json.dumps(trimmed, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
