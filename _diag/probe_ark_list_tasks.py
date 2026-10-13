#!/usr/bin/env python3
"""只读探针：方舟「查询视频生成任务列表」是否能一次性回捞历史 usage（用于补历史统计）。

零成本：只 GET 列表。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from shared.net import UPSTREAM
except ImportError:  # pragma: no cover
    UPSTREAM = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SECRETS = ROOT / "seedance" / "state" / "secrets.json"
BASE = "https://ark.cn-beijing.volces.com/api/v3"


def get(url: str, key: str) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {key}"})
    try:
        with UPSTREAM.open(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    key = (json.loads(SECRETS.read_text("utf-8")).get("volcengine_api_key") or "").strip()
    for label, url in [
        ("plain page_size=3", f"{BASE}/contents/generations/tasks?page_size=3"),
        ("with page_num", f"{BASE}/contents/generations/tasks?page_size=3&page_num=1"),
    ]:
        status, raw = get(url, key)
        print(f"\n=== {label} -> HTTP {status} ===")
        if status != 200:
            print(raw[:600])
            continue
        data = json.loads(raw)
        print("top-level keys:", sorted(data.keys()))
        items = data.get("items") or data.get("data") or []
        print("item count:", len(items))
        for it in items[:3]:
            print(json.dumps({
                "id": it.get("id"),
                "model": it.get("model"),
                "status": it.get("status"),
                "duration": it.get("duration"),
                "created_at": it.get("created_at"),
                "usage": it.get("usage"),
            }, ensure_ascii=False))
        print("has_more / total:", data.get("has_more"), data.get("total"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
