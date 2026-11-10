#!/usr/bin/env python3
"""按 activity_log 的真实附件回放，修正 seedance 历史明细里「有/无输入视频」的档位。

**为什么**（2026-10-29 用户报「计算费用应该按挂没挂参考视频判断单价」）：
Portal 从 multipart 请求体推断「这次挂了参考视频」时，把 `filename=""` 的**空槽位**也算成
了附件 —— 浏览器会把没选中的文件输入也塞进请求（实测 seedance 表单里 ref_video_1..3 全在，
值都是 `b''`）。于是**每一次**生成都被标成 `has_input_video=True`，单价串到「有输入视频」
档（Seedance 2.5：40.29 元/百万 token，而无输入视频是 67.17，差 1.6 倍）。

判据本身已在 `portal/app.py` 修好（`_disposition_filename`：空 filename 不算文件），
这个脚本只负责**回放历史行**。

**真值来源**：seedance 自己的 `state/activity_log.json` —— 每个作业的
`restore.media`（重试/重启恢复用的素材清单，带文件名与 mime）与 `values.saved_media`
（历史素材复用那条路），记的是**当时真的上传了什么**，不是从 Portal 的推断反推。
⚠️ 该文件只保留最近 100 个作业，所以本脚本**只能覆盖那个窗口**里的明细行。

用法：
    python _diag/repair_seedance_input_video_tier.py            # dry-run（默认，只打印）
    python _diag/repair_seedance_input_video_tier.py --apply    # 写回（先备份）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ACTIVITY = ROOT / "seedance" / "state" / "activity_log.json"
GENERATIONS = ROOT / "portal" / "state" / "generations.jsonl"
FILE_FIELD_HINTS = ("ref_video", "ref_image", "ref_audio", "first_frame", "last_frame")


def log(message: str) -> None:
    """降级打印：GBK 控制台遇到 emoji 会抛 UnicodeEncodeError，把脚本整死（本仓库踩过）。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        # 注意别再 decode("ascii","replace") —— 那会产出 U+FFFD，GBK 同样编不出来
        print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _mime_of(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("mime") or value.get("mime_type") or "")
    return ""


def real_media_by_job() -> dict[str, dict[str, str]]:
    """`{job_id: {字段名: mime}}` —— 当时**真的**带上的素材（空槽位不算）。"""
    try:
        entries = json.loads(ACTIVITY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"读不到 activity_log：{exc}")
        return {}
    out: dict[str, dict[str, str]] = {}
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("job_id") or "")
        if not job_id:
            continue
        media: dict[str, str] = {}
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        # 1) 恢复清单：重启/重试时重建素材用的那份，字段名 + mime 最全
        restore = item.get("restore") if isinstance(item.get("restore"), dict) else {}
        for field, meta in (restore.get("media") or {}).items():
            media[str(field)] = _mime_of(meta)
        # 2) 「历史素材」复用那条路：真实引用在 values.saved_media 里
        values = request.get("values") if isinstance(request.get("values"), dict) else {}
        saved = values.get("saved_media")
        if isinstance(saved, str) and saved.strip():
            try:
                parsed = json.loads(saved)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                for field, meta in parsed.items():
                    media.setdefault(str(field), _mime_of(meta))
        # 3) 直接上传：values 里的文件字段（空槽位是 "b''"，跳过）
        for field, value in values.items():
            name = str(field)
            if name == "saved_media" or not any(h in name for h in FILE_FIELD_HINTS):
                continue
            if isinstance(value, str) and value not in ("", "b''"):
                media.setdefault(name, "")
        out[job_id] = media
    return out


def has_reference_video(media: dict[str, str]) -> bool:
    for field, mime in media.items():
        text = f"{field} {mime}".lower()
        if "video" in text:
            return True
    return False


def load_quote():
    """借 Portal 自己的算价函数（口径只有一处，别在这里复制一份单价逻辑）。"""
    from tests.portal_isolation import load_isolated

    module = load_isolated("portal_app_repair_media_tier", ROOT / "portal" / "app.py")
    # 用**生产**那份单价表（隔离目录里没有 pricing.json，会回落到内置表）
    module.PRICING_PATH = ROOT / "portal" / "state" / "pricing.json"
    module._PRICING_CACHE["mtime"] = -1.0
    return module


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    media_by_job = real_media_by_job()
    log(f"activity_log 覆盖作业：{len(media_by_job)} 个")
    if not media_by_job:
        log("没有可用的真值来源，什么都不做。")
        return 2
    if not GENERATIONS.exists():
        log(f"找不到 {GENERATIONS}")
        return 2

    mod = load_quote()
    lines = GENERATIONS.read_text(encoding="utf-8").splitlines()
    changed: list[dict] = []
    skipped_no_truth = 0
    kept_rows: list[str] = []
    delta_total = 0.0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            kept_rows.append(line)
            continue
        if str(row.get("app") or "") != "seedance" or not isinstance(row.get("job_id"), str):
            kept_rows.append(line)
            continue
        media = media_by_job.get(str(row["job_id"]))
        if media is None:
            skipped_no_truth += 1
            kept_rows.append(line)
            continue
        correct = has_reference_video(media)
        if bool(row.get("has_input_video")) == correct:
            kept_rows.append(line)
            continue
        quote = mod.quote_generation(
            model=str(row.get("model") or ""),
            resolution=str(row.get("resolution") or ""),
            image_size=str(row.get("image_size") or ""),
            has_input_video=correct,
            tokens=int(row.get("tokens") or 0),
            images=int(row.get("images") or 0),
            app="seedance",
        )
        old_amount = row.get("amount")
        new_amount = quote.get("amount")
        if not isinstance(new_amount, (int, float)):
            # 认不出族/档位：宁可不动，也不要写个假数字
            kept_rows.append(line)
            continue
        row = dict(row)
        row["has_input_video"] = correct
        row["media_fields"] = sorted(media)
        row["unit_price"] = quote.get("unit_price")
        row["unit"] = quote.get("unit")
        row["amount"] = new_amount
        if quote.get("label") and not row.get("price_family"):
            row["price_family"] = quote.get("label")
        changed.append({
            "job_id": row.get("job_id"), "time": row.get("time"), "model": row.get("model"),
            "resolution": row.get("resolution"), "tokens": row.get("tokens"),
            "old_unit": None, "new_unit": quote.get("unit_price"),
            "old_amount": old_amount, "new_amount": new_amount,
        })
        if isinstance(old_amount, (int, float)):
            delta_total += float(new_amount) - float(old_amount)
        kept_rows.append(json.dumps(row, ensure_ascii=False))

    log(f"扫描明细 {len(lines)} 行（不在 activity_log 窗口内、无从判断的 seedance 行：{skipped_no_truth} 行）")
    log(f"需要修正：{len(changed)} 行")
    for item in changed[:40]:
        log("  {time} {model} {res} tokens={tok} {oa} -> {na} (unit {nu})".format(
            time=item["time"], model=item["model"], res=item["resolution"],
            tok=item["tokens"], oa=item["old_amount"], na=item["new_amount"],
            nu=item["new_unit"]))
    if len(changed) > 40:
        log(f"  …… 另外 {len(changed) - 40} 行")
    log(f"金额合计变化：{delta_total:+.4f} 元")
    if not apply:
        log("\n--dry-run：没有写任何文件；确认无误后加 --apply")
        return 0
    if not changed:
        log("没有需要修正的行，未写文件。")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = GENERATIONS.with_name(f"generations.jsonl.bak-before-media-tier-{stamp}")
    shutil.copy2(GENERATIONS, backup)
    tmp = GENERATIONS.with_name(GENERATIONS.name + f".tmp{os.getpid()}")
    tmp.write_text("\n".join(kept_rows) + "\n", encoding="utf-8")
    os.replace(tmp, GENERATIONS)
    log(f"已写回 {GENERATIONS}（修正 {len(changed)} 行），备份：{backup.name}")
    log("Portal 每次请求都重读这份明细文件，所以无需重启即可看到修正。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
