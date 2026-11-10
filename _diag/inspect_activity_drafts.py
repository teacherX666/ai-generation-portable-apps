"""看看活动记录里哪些是「样片」，以及列表/详情接口各自能不能认出来。

用法：python _diag/inspect_activity_drafts.py [ws]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "seedance" / "state" / "activity_log.json"
BASE = "http://127.0.0.1:8787"
NO_FLAG = "(无)"


def get(path: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:120].decode("utf-8", "replace")


def main() -> int:
    ws = sys.argv[1] if len(sys.argv) > 1 else ""
    items = json.loads(LOG.read_text(encoding="utf-8"))
    print(f"活动记录 {len(items)} 条；标出样片：")
    drafts = []
    for item in items[-12:]:
        request = item.get("request") or {}
        values = (request.get("parsed") or {}).get("values") or request.get("values") or {}
        restore = (item.get("restore") or {}).get("values") or {}
        draft = values.get("draft", restore.get("draft"))
        if draft:
            drafts.append(item)
        print(
            f"  {item.get('created_at')} ws={item.get('workspace_id')} "
            f"user={item.get('username')!r} status={item.get('status')} "
            f"job={item.get('job_id')} draft={draft!r}"
        )
    if not drafts:
        print("最近 12 条里没有样片记录")
        return 0
    target = drafts[-1]
    run_ws = ws or str(target.get("workspace_id") or "")
    print(f"\n拿最后一条样片试接口（ws={run_ws}）：")
    status, listing = get(f"/api/activity?ws={run_ws}")
    records = (listing or {}).get("records") if isinstance(listing, dict) else None
    print(f"  列表 GET /api/activity?ws=… -> HTTP {status}，{len(records or [])} 条")
    hit = next((r for r in (records or []) if r.get("id") == target.get("id")), None)
    print(f"    列表里能找到这条吗：{'能' if hit else '不能'}")
    if hit:
        print(f"    列表字段：{sorted(hit.keys())}")
    detail_status, detail = get(f"/api/activity/{target.get('id')}?ws={run_ws}")
    print(f"  详情 GET /api/activity/<id> -> HTTP {detail_status}")
    if detail_status == 200 and isinstance(detail, dict):
        request = detail.get("request") or {}
        values = (request.get("parsed") or {}).get("values") or request.get("values") or {}
        restore = (detail.get("restore") or {}).get("values") or {}
        print(f"    详情里 draft = {values.get('draft', restore.get('draft'))!r}（前端靠它判断是不是样片）")
    else:
        print(f"    ⚠ 详情取不到 → 前端只能拿列表字段判断，而列表**没有** draft → 判成普通记录 → 不走二级窗口")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
