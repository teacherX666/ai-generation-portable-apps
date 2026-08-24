#!/usr/bin/env python3
"""每日定时清理（launchd: com.ai-portal-cleanup，每日 03:47）。

默认 dry-run（只报告，不动任何文件）；加 --apply 才真执行。四个职责：

  1. outputs 产物按龄清理（--outputs-retention，默认 14 天）：
     文件已在飞书多维表格同步过的（feishu-output-sync 的 synced 表，
     fingerprint = sha1(相对路径|size|mtime)，与 sync.py 同口径）→ 直接删除；
     未同步的 → 移到回收站兜底（~/.Trash/ai-portable-cleanup-<ts>/）。
     宁可多进回收站，不可误删没有飞书副本的文件。
  2. workspaces 草稿参考素材按龄清理（--workspace-days，默认 30 天）：
     只删「preset.json 超过 N 天未编辑」的 workspace 的 media/ 内容；
     preset.json（草稿提示词）与 archives 保留。参考图/视频重传即可恢复，
     此语义与 2026-07-22 数据迁移一致。
  3. download_files.json 失效 token 剪枝：路径不存在的条目移除。
     写入用 CAS（读→滤→重读比对→原子替换），避免与运行中子应用的
     并发写互相覆盖；比对不一致就跳过本轮。
  4. 日志截断：feishu agent access log > 100MB、portal 子应用日志 > 50MB
     直接截断。日志由进程持有 append fd（launchd 重定向/Popen stdout），
     截断后继续写入不受影响（O_APPEND 每次写都落在文件末尾）。

统计保护：usage.json / activity_log.json / users.json 一律不碰；
state/workspaces 之外的其他 state 内容不碰；cloudflared 隧道日志
（com.ai-portal-tunnel 属于其他应用）不碰。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()

# ── 1. outputs 白名单（相对 REPO_ROOT）。任何未列出的目录一律跳过。 ──
OUTPUT_DIRS = [
    "seedance/outputs",
    "seedance/视频生成合集",
    "seedance/图片生成合集",
    "seedance/图",
    "seedance/浏览器下载",
    "seedance/Pictures",
    "seedance/0706",
    "seedance/AI Tool",
    "seedance/【公交车】7.2 ai片头需求 黄敏",
    "seedance/【水排序】7.3 ai片头需求 饶津毓",
    "seedance/【水排序】7.3 ai片头需求2黄淼",
    "seedance/鸟排序:7.6日 汪洪秀 AI需求:2个需求",
    "nano-banana/outputs",
    "nano-banana/图片生成合集",
    "nano-banana/视频生成合集",
    "nano-banana/图",
    "nano-banana/浏览器下载",
    "nano-banana/AI Tool",
    "nano-banana/0706",
    "dreamina/outputs",
    "dreamina/uploads",
    "volcengine-portrait/outputs",
    "volcengine-portrait/视频生成合集",
    "volcengine-portrait/uploads",
]

EXTS = {".mp4", ".png", ".jpg", ".jpeg", ".webp"}

FORBIDDEN_PARTS = {"state", "portal", ".git", "static"}

# ── 2. workspaces（草稿）根 ──
WORKSPACE_DIRS = [
    "seedance/state/workspaces",
    "nano-banana/state/workspaces",
]

# ── 3. download_files 映射 ──
DOWNLOAD_MAP_PATHS = [
    REPO_ROOT / "seedance/state/download_files.json",
    REPO_ROOT / "volcengine-portrait/state/download_files.json",
]

# ── 4. 日志截断阈值 ──
LOG_TRUNCATE_RULES = [
    (HOME / "Library/Logs/feishu-generation-agent.log", 100 * 1024 * 1024),
]
PORTAL_LOGS_DIR = REPO_ROOT / "portal/state/logs"
PORTAL_LOG_MAX = 50 * 1024 * 1024

SYNC_DB = REPO_ROOT / "feishu-output-sync/state/sync.sqlite3"


def human_size(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:.1f}{unit}"
        n /= step
    return f"{n:.1f}PB"


# ── synced 表 ──────────────────────────────────────────────────────────────

def load_synced_fingerprints() -> set[str]:
    """已搬进飞书多维表格的文件指纹集合。DB 不存在/损坏 → 空集
    （后果只是所有文件走回收站而非直接删，方向安全）。"""
    if not SYNC_DB.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{SYNC_DB}?mode=ro", uri=True)
        rows = conn.execute("SELECT fingerprint FROM synced").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def fingerprint(path: Path, size: int, mtime: int) -> str:
    """与 feishu-output-sync 同口径的指纹：registry.fingerprint 用
    art.path（scanner 里是 f.resolve() 绝对路径）+ size + int(mtime)。
    实测已验证与 synced 表精确命中。失配的后果只是多进回收站，方向安全。"""
    raw = f"{path}|{size}|{mtime}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ── 1. outputs ─────────────────────────────────────────────────────────────

def collect_outputs(before: dt.date) -> list[tuple[Path, int, dt.date]]:
    hits = []
    for rel in OUTPUT_DIRS:
        root = REPO_ROOT / rel
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in EXTS:
                continue
            if any(part in FORBIDDEN_PARTS for part in p.parts):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            mdate = dt.date.fromtimestamp(st.st_mtime)
            if mdate < before:
                hits.append((p, st.st_size, mdate))
    hits.sort(key=lambda x: x[2])
    return hits


def run_outputs(hits: list[tuple[Path, int, dt.date]], synced: set[str],
                apply: bool) -> tuple[int, int, int, int]:
    """返回 (删除数, 回收站数, 今日跳过数, 回收字节数)。"""
    deleted = trashed = skipped_today = 0
    trash_bytes = 0
    today = dt.date.today()
    trash_files: list[Path] = []
    for p, size, mdate in hits:
        if mdate == today:
            skipped_today += 1
            continue
        st = p.stat()
        if fingerprint(p, st.st_size, int(st.st_mtime)) in synced:
            if apply:
                try:
                    p.unlink()
                    deleted += 1
                except OSError as e:
                    print(f"  删除失败 {p}: {e}", file=sys.stderr)
            else:
                deleted += 1
        else:
            trash_files.append(p)
            trash_bytes += size
    if trash_files:
        if apply:
            ok, root = move_to_trash(trash_files)
            trashed = ok
            print(f"  已移回收站 {ok}/{len(trash_files)} → {root}")
        else:
            trashed = len(trash_files)
    return deleted, trashed, skipped_today, trash_bytes


def move_to_trash(files: list[Path]) -> tuple[int, Path]:
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = HOME / ".Trash" / f"ai-portable-cleanup-{ts}"
    trash_root.mkdir(parents=True, exist_ok=True)
    ok = 0
    for src in files:
        try:
            rel = src.relative_to(REPO_ROOT)
        except ValueError:
            rel = Path(src.name)
        dst = trash_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            ok += 1
        except OSError as e:
            print(f"  移回收站失败 {src}: {e}", file=sys.stderr)
    return ok, trash_root


# ── 2. workspaces media ────────────────────────────────────────────────────

def collect_workspace_media(ws_days: int) -> list[tuple[Path, int]]:
    """preset.json mtime 早于 ws_days 天的 workspace 的 media/ 全部文件。
    返回 (file_path, size)。preset.json 与 archives 永不动。"""
    hits: list[tuple[Path, int]] = []
    cutoff = dt.date.today() - dt.timedelta(days=ws_days)
    for rel in WORKSPACE_DIRS:
        ws_root = REPO_ROOT / rel
        if not ws_root.exists():
            continue
        for preset in ws_root.glob("*/preset.json"):
            try:
                if dt.date.fromtimestamp(preset.stat().st_mtime) >= cutoff:
                    continue
            except OSError:
                continue
            media_dir = preset.parent / "media"
            if not media_dir.is_dir():
                continue
            for f in media_dir.rglob("*"):
                if f.is_file():
                    try:
                        hits.append((f, f.stat().st_size))
                    except OSError:
                        continue
    hits.sort(key=lambda x: x[0])
    return hits


def run_workspace_media(hits: list[tuple[Path, int]], apply: bool) -> int:
    """直接删除（草稿参考素材，用户可重传——2026-07-22 迁移已定义此语义）。"""
    if not apply:
        return len(hits)
    deleted = 0
    for p, _ in hits:
        try:
            p.unlink()
            deleted += 1
        except OSError as e:
            print(f"  删除失败 {p}: {e}", file=sys.stderr)
    # 清掉空的子目录，保持目录树整洁
    for rel in WORKSPACE_DIRS:
        ws_root = REPO_ROOT / rel
        if not ws_root.exists():
            continue
        for media_dir in ws_root.glob("*/media"):
            try:
                for d in sorted(media_dir.rglob("*"), reverse=True):
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
            except OSError:
                pass
    return deleted


# ── 3. download_files.json 剪枝 ────────────────────────────────────────────

def prune_download_maps(apply: bool) -> int:
    pruned = 0
    for map_path in DOWNLOAD_MAP_PATHS:
        if not map_path.exists():
            continue
        try:
            first = json.loads(map_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(first, dict):
            continue
        keep = {tok: p for tok, p in first.items() if Path(str(p)).exists()}
        removed = len(first) - len(keep)
        pruned += removed
        if removed:
            print(f"  {map_path.name}: 移除 {removed} 个失效 token（剩 {len(keep)} 个）")
            if apply:
                # CAS：重读比对，运行中子应用若在窗口内写了新条目则放弃本轮
                try:
                    second = json.loads(map_path.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if second != first:
                    print("  ⚠️ 文件在读取窗口内被改动，跳过本轮写入", file=sys.stderr)
                    continue
                tmp = map_path.with_suffix(map_path.suffix + ".tmp")
                tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=2), "utf-8")
                tmp.replace(map_path)
    return pruned


# ── 4. 日志截断 ────────────────────────────────────────────────────────────

def truncate_logs(apply: bool) -> list[str]:
    done: list[str] = []
    targets: list[tuple[Path, int]] = list(LOG_TRUNCATE_RULES)
    if PORTAL_LOGS_DIR.exists():
        for p in PORTAL_LOGS_DIR.glob("*.log"):
            targets.append((p, PORTAL_LOG_MAX))
    for p, max_bytes in targets:
        try:
            if p.is_file() and p.stat().st_size > max_bytes:
                size_before = p.stat().st_size
                if apply:
                    # 进程持 append fd（launchd 重定向 / Popen stdout），
                    # 截断后 O_APPEND 写入仍落文件末尾，无数据损坏。
                    p.write_text("")
                done.append(f"{p} ({human_size(size_before)})")
        except OSError:
            continue
    return done


# ── main ───────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真的执行；不加则只预览")
    ap.add_argument("--outputs-retention", type=int, default=14,
                    help="outputs 保留天数（默认 14）")
    ap.add_argument("--workspace-days", type=int, default=30,
                    help="workspace preset.json 超过该天数未编辑才清 media/（默认 30）")
    args = ap.parse_args()

    mode = "执行" if args.apply else "dry-run"
    print(f"=== ai-portal 每日清理（{mode}） {dt.datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"outputs 保留 {args.outputs_retention} 天；workspace 参考素材阈值 {args.workspace_days} 天")
    print()

    synced = load_synced_fingerprints()
    print(f"[1/4] outputs 产物（> {args.outputs_retention} 天）")
    print(f"      飞书已同步指纹表：{len(synced)} 条（命中 → 直接删；未命中 → 回收站）")
    hits = collect_outputs(dt.date.today() - dt.timedelta(days=args.outputs_retention))
    if hits:
        total = sum(sz for _, sz, _ in hits)
        deleted, trashed, skipped_today, trash_bytes = run_outputs(hits, synced, args.apply)
        print(f"      命中 {len(hits)} 个文件 {human_size(total)}："
              f"直接删 {deleted}、回收站 {trashed}（{human_size(trash_bytes)}）、"
              f"今日跳过 {skipped_today}")
    else:
        print("      无超龄文件")
    print()

    print(f"[2/4] workspaces 草稿参考素材（preset.json 超 {args.workspace_days} 天未编辑）")
    ws_hits = collect_workspace_media(args.workspace_days)
    if ws_hits:
        ws_total = sum(sz for _, sz in ws_hits)
        ws_deleted = run_workspace_media(ws_hits, args.apply)
        print(f"      命中 {len(ws_hits)} 个文件 {human_size(ws_total)}"
              + (f"，已删 {ws_deleted}" if args.apply else "（preset.json 与 archives 保留）"))
    else:
        print("      无超龄 workspace")
    print()

    print("[3/4] download_files.json 失效 token 剪枝")
    pruned = prune_download_maps(args.apply)
    print(f"      共移除 {pruned} 个失效条目")
    print()

    print("[4/4] 日志截断")
    truncated = truncate_logs(args.apply)
    if truncated:
        for t in truncated:
            print(f"      截断: {t}")
    else:
        print("      无超限日志")
    print()

    print(f"=== 完成（{mode}）===")
    if not args.apply:
        print("提示：本次为 dry-run，未改动任何文件。加 --apply 真执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
