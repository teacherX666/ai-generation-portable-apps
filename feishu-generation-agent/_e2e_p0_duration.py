"""②/③ 真实端到端（8765 真实接口，零视频生成）。

流程：
1. `POST /api/runs` 用「刮太空沙」的真实飞书文档建 run（真实规划，1 次文本模型调用）；
2. 轮询到 `waiting_approval`，记录**规划产出的提示词**（③ 的判据：镜头数 / 分镜指令）；
3. 用审批页真实接口 `PATCH /api/runs/{id}/tasks/{task_id}` 把它改成 r5 的形态
   （正文写 d 秒、任务时长 d+2）→ 这正是「毛线1」r5 的手改轨迹；
4. `GET /api/runs/{id}` 读审批页视图：`approval.validation_issues`（改前为空 = 放行；
   改后应有可读 issue）；
5. 只有 `--approve` 时才真的点批准（改后必然在 `revalidate_approval` 就被拦，
   执行前抛错 → 零生成费用；改前**绝不**点，否则会真的出片）。

用法：
    python _e2e_p0_duration.py --tag before           # 只读基线（不点批准）
    python _e2e_p0_duration.py --tag after --approve  # 改后：读 issue + 证明执行前被拦
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee"}
SAND_DOC = "https://redcqchina.feishu.cn/wiki/AZQhws4jyicD8ekZWI7celKPn2f"
OUT_DIR = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\yarn-fix")

_DIRECTIVE_PATTERNS = {
    "镜头2+": re.compile(r"镜头\s*(?:[2-9]|\d{2,})"),
    "第二镜": re.compile(r"第\s*[二三四五六七八九十\d]+\s*个?\s*镜头"),
    "N段镜头": re.compile(r"(?:[2-9]|\d{2,}|[两二三四五六七八九十])\s*(?:段|个)\s*镜头"),
    "分N段": re.compile(r"(?:分|切|拆|划)\s*(?:成)?\s*(?:[2-9]|\d{2,}|[两二三四五六七八九十])\s*段"),
    "快速剪辑": re.compile(r"(?:快速剪辑|连续剪辑|剪辑节奏|剪辑点|多段剪辑|加速剪辑)"),
    "硬切": re.compile(r"硬切"),
}
_SEGMENT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:秒|s|S)?\s*[-–—~～至到]\s*(\d+(?:\.\d+)?)\s*(?:秒|s|S)"
)


def _record(tag: str, run_id: str, payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"e2e_{tag}.json"
    existing = []
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    existing.append({"run_id": run_id, **payload})
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _wait_approval(client: httpx.Client, run_id: str, timeout: float = 420.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        response = client.get(f"/api/runs/{run_id}", headers=HEADERS)
        response.raise_for_status()
        last = response.json()
        status = last.get("status")
        if status == "waiting_approval":
            return last
        if status in {"failed", "cancelled"}:
            raise SystemExit(f"run 未进入待审批：{status} / {last.get('last_error')}")
        time.sleep(4)
    raise SystemExit(f"等待待审批超时，最后状态 {last.get('status')}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()

    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        created = client.post(
            "/api/runs", json={"source_url": SAND_DOC}, headers=HEADERS
        )
        created.raise_for_status()
        run_id = created.json()["run_id"]
        print(f"[{args.tag}] run_id={run_id} 规划中…")

        view = _wait_approval(client, run_id)
        tasks = view["approval"]["tasks"]
        summary = []
        for task in tasks:
            prompt = task.get("prompt") or ""
            segments = [(float(a), float(b)) for a, b in _SEGMENT.findall(prompt)]
            summary.append(
                {
                    "task_id": task["task_id"],
                    "duration": task.get("duration"),
                    "prompt_chars": len(prompt),
                    "shot_markers": [int(m) for m in re.findall(r"镜头\s*(\d+)", prompt)],
                    "timeline_segments": segments,
                    "directives": {
                        name: pattern.findall(prompt)
                        for name, pattern in _DIRECTIVE_PATTERNS.items()
                        if pattern.search(prompt)
                    },
                    "planning_validation_issues": view["approval"].get(
                        "validation_issues"
                    ),
                    "prompt": prompt,
                }
            )
        print(f"[{args.tag}] 规划完成：{len(tasks)} 个任务")
        for item in summary:
            print(
                f"  {item['task_id']} dur={item['duration']} "
                f"segments={item['timeline_segments']} directives={list(item['directives'])}"
            )

        # ③ 的判据：规划产出的提示词里有没有分镜指令 / 是不是 1 个镜头
        planning_ok = all(
            not item["directives"] and len(set(item["shot_markers"])) <= 1
            for item in summary
        )
        print(f"[{args.tag}] ③ 规划阶段单镜头检查：{'PASS' if planning_ok else 'FAIL'}")

        # --- ② 构造 r5 形态：正文写 d 秒 + 任务时长 d+2 -------------------------
        task = tasks[0]
        prompt = task.get("prompt") or ""
        duration = int(task.get("duration") or 8)
        stale = duration + 2
        # 把正文里的时长表述钉死在 d 秒（同时把时间轴头也写成 0—d 秒，模拟用户手改）
        retimed = re.sub(
            r"(?:预计|共|总计|总时长|全片时长|成片时长)(\s*[:：为]?\s*)\d+(?:\.\d+)?",
            lambda m: f"{m.group(0)[: len(m.group(0)) - len(m.group(0).lstrip())]}",
            prompt,
        )
        retimed = re.sub(
            r"(预计|共|总计|总时长|全片时长|成片时长)(\s*[:：为]?\s*)\d+(?:\.\d+)?\s*(秒|[sS]\b)",
            lambda m: f"{m.group(1)}{m.group(2)}{duration}\u79d2",
            prompt,
        )
        if f"预计{duration}秒" not in retimed:
            retimed = f"正面近景、固定机位、单镜头一镜到底，预计{duration}秒。{retimed}"
        patch = {"prompt": retimed, "duration": stale}
        response = client.patch(
            f"/api/runs/{run_id}/tasks/{task['task_id']}",
            json={"patch": patch},
            headers=HEADERS,
        )
        print(f"[{args.tag}] PATCH 改时长 {duration}→{stale}（正文仍写 {duration} 秒）"
              f" → {response.status_code}")
        response.raise_for_status()

        after = client.get(f"/api/runs/{run_id}", headers=HEADERS).json()
        issues = after["approval"].get("validation_issues") or []
        task_after = after["approval"]["tasks"][0]
        print(f"[{args.tag}] 审批页 validation_issues={issues}")
        print(f"[{args.tag}] 任务 blocking_issues={task_after.get('blocking_issues')}")
        print(
            f"[{args.tag}] ② 改后是否被拦："
            f"{'YES（有可读 issue）' if issues else 'NO（审批页没有任何提示 = 放行）'}"
        )

        payload = {
            "planning_ok": planning_ok,
            "planning": summary,
            "duration_before": duration,
            "duration_stale": stale,
            "validation_issues_after_patch": issues,
            "blocking_issues_after_patch": task_after.get("blocking_issues"),
            "status_after_patch": after.get("status"),
        }

        if args.approve:
            decision = client.post(
                f"/api/runs/{run_id}/decision",
                json={
                    "action": "approve",
                    "selected_task_ids": [task["task_id"]],
                },
                headers=HEADERS,
            )
            print(f"[{args.tag}] 点批准 → HTTP {decision.status_code} {decision.text[:200]}")
            payload["decision_status"] = decision.status_code
            payload["decision_body"] = decision.text[:600]
            # 等它落地：被拦时 run 会 failed，执行记录必须为空（= 一次生成都没有）
            for _ in range(30):
                time.sleep(2)
                final = client.get(f"/api/runs/{run_id}", headers=HEADERS).json()
                if final.get("status") in {"failed", "succeeded", "cancelled"}:
                    break
            payload["final_status"] = final.get("status")
            payload["final_last_error"] = final.get("last_error")
            payload["execution_records"] = len(final.get("execution_records") or [])
            payload["artifacts"] = len(final.get("artifacts") or [])
            print(
                f"[{args.tag}] 最终 status={final.get('status')} "
                f"execution_records={payload['execution_records']} "
                f"artifacts={payload['artifacts']}"
            )
            print(f"[{args.tag}] last_error={json.dumps(final.get('last_error'), ensure_ascii=False)[:400]}")

        _record(args.tag, run_id, payload)
        print(f"[{args.tag}] run_id 记录到 {OUT_DIR / f'e2e_{args.tag}.json'}")


if __name__ == "__main__":
    sys.exit(main())
