"""活体验证：在跑着的 8765 上真实删一次分镜关键帧，看它会不会被批准路径加回来。

流程（全部走真实 HTTP 接口，用给定的 Portal 用户身份）：
  ① 读 / 保存高级设置（K），临时把 storyboard_candidates 设成 2；
  ② POST /api/runs 建一条独立运行（真实飞书文档链接），等它到 waiting_approval；
  ③ GET 视图，记下第 1 段选中的关键帧；
  ④ DELETE 那张参考图 → 再 GET，检查帧的 selected / candidates / selection_cleared；
  ⑤ 清理：DELETE 这条运行，并把 K 改回原值。

只读 + 一次真实删除，不触发视频生成（不批准），所以不花视频的钱；
分镜候选图是真实生成的（这是验证必需的开销）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
USER = "6b34d9e8-3058-41b6-89b2-4cedac2362ee"
SOURCE_URL = "https://t7le908iko.feishu.cn/wiki/FEnYwJiKRiDBu1kwsdFcd7X6nae"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "X-Portal-User-Id": USER,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def prefs() -> dict:
    return call("GET", "/api/provider-preferences")


def put_prefs(payload: dict) -> dict:
    return call("PUT", "/api/provider-preferences", payload)


def frames_of(view: dict) -> list[dict]:
    out: list[dict] = []
    for task in (view.get("approval") or {}).get("tasks") or []:
        for frame in task.get("storyboard_frames") or []:
            out.append({"task_id": task.get("task_id"), **frame})
    return out


def main() -> None:
    original = prefs()
    print("① 原高级设置:", original, flush=True)
    run_id = None
    try:
        put_prefs({**original, "storyboard_candidates": 2})
        print("   临时 K=2:", prefs(), flush=True)

        created = call("POST", "/api/runs", {"source_url": SOURCE_URL})
        run_id = created["run_id"]
        print("② 建运行:", run_id, flush=True)

        deadline = time.time() + 900
        view = {}
        while time.time() < deadline:
            view = call("GET", f"/api/runs/{run_id}")
            status = view.get("status")
            if status == "waiting_approval":
                break
            if status in {"failed", "cancelled"}:
                raise SystemExit(f"运行未到审批态：{status} {view.get('last_error')}")
            time.sleep(5)
        print("   状态:", view.get("status"), flush=True)

        frames = frames_of(view)
        print("③ 分镜帧:", json.dumps(frames, ensure_ascii=False)[:1200], flush=True)
        target = next(
            (f for f in frames if f.get("selected") and len(f.get("candidates") or []) >= 1),
            None,
        )
        if target is None:
            print("   ⚠ 没有可删的分镜关键帧（本次没生成出来），跳过删除验证")
            return
        task_id = target["task_id"]
        deleted = target["selected"]
        print(
            f"   目标：task={task_id} index={target['index']} 选中={deleted} "
            f"候选={[c.get('asset_id') for c in target['candidates']]}",
            flush=True,
        )

        call(
            "DELETE",
            f"/api/runs/{run_id}/tasks/{task_id}/references/{deleted}",
        )
        after = call("GET", f"/api/runs/{run_id}")
        frame = next(
            f
            for f in frames_of(after)
            if f["task_id"] == task_id and f["index"] == target["index"]
        )
        refs = [
            r["asset_id"]
            for t in (after.get("approval") or {}).get("tasks") or []
            if t.get("task_id") == task_id
            for r in t.get("reference_images") or []
        ]
        print("④ 删除后：", flush=True)
        print("   reference_images =", refs, flush=True)
        print("   还含被删的图 ?   =", deleted in refs, flush=True)
        print("   frame.selected   =", frame.get("selected"), flush=True)
        print("   selection_cleared=", frame.get("selection_cleared"), flush=True)
        print(
            "   candidates       =",
            [
                (c.get("asset_id"), c.get("selected"))
                for c in frame.get("candidates") or []
            ],
            flush=True,
        )
        print(
            "   视图高亮         =",
            [c["asset_id"] for c in frame.get("candidates") or [] if c.get("selected")],
            flush=True,
        )
    finally:
        if run_id:
            try:
                call("DELETE", f"/api/runs/{run_id}")
                print("⑤ 已清理运行", run_id, flush=True)
            except Exception as exc:  # noqa: BLE001
                print("   ⚠ 清理运行失败:", exc, flush=True)
        put_prefs(original)
        print("   已恢复高级设置:", prefs(), flush=True)


main()
