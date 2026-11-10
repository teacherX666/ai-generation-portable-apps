"""低磁盘水位自动收紧（2026-10-30）。

背景：清理器原来只认年龄、不看剩余空间。C 盘只剩 19.5GB 时它照常按 7 天清理，
而产物被挪进**同一块盘**的回收站、8 天后才真删 → 净腾不出空间。
这组测试锁住三件事：低水位确实收紧、盘不紧张/探测失败时**绝不**收紧、
以及 main() 真的用了收紧后的值（判别力自检）。
"""

import contextlib
import datetime as dt
import importlib.util
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "cleanup_daily_lowdisk", ROOT / "tools" / "cleanup_daily.py"
)
cleanup = importlib.util.module_from_spec(_spec)
sys.modules["cleanup_daily_lowdisk"] = cleanup
_spec.loader.exec_module(cleanup)


# ── 判据本身 ──────────────────────────────────────────────────────────────

def test_low_disk_tightens_both_retentions():
    assert cleanup.tighten_for_low_disk(19.5, 7, 7) == (3, 1, True)


def test_healthy_disk_keeps_requested_retention():
    assert cleanup.tighten_for_low_disk(100.0, 7, 7) == (7, 7, False)


def test_threshold_boundary_is_not_low():
    # 恰好等于阈值不算紧张（避免在边界上反复横跳）
    assert cleanup.tighten_for_low_disk(25.0, 7, 7) == (7, 7, False)


def test_probe_failure_never_tightens():
    # ⚠️ 探测失败绝不能读成「盘满了」——那会在未知情况下加速删数据
    assert cleanup.tighten_for_low_disk(None, 7, 7) == (7, 7, False)


def test_tighten_only_shrinks_never_grows():
    # 命令行传了更短的值 → 低水位不许把它放宽回下限
    assert cleanup.tighten_for_low_disk(1.0, 1, 0) == (1, 0, True)
    assert cleanup.tighten_for_low_disk(1.0, 5, 2) == (3, 1, True)


def test_free_gb_for_returns_none_on_oserror(monkeypatch):
    def boom(_):
        raise OSError("probe failed")

    monkeypatch.setattr(cleanup.shutil, "disk_usage", boom)
    assert cleanup.free_gb_for(cleanup.REPO_ROOT) is None


def test_free_gb_for_reads_the_real_volume():
    got = cleanup.free_gb_for(cleanup.REPO_ROOT)
    assert isinstance(got, float) and got > 0


# ── main() 真的用了收紧后的值 ──────────────────────────────────────────────

def _stub_pipeline(monkeypatch, record):
    """把六个步骤全换成只记录参数的桩，避免真的去扫盘/删文件。"""
    monkeypatch.setattr(cleanup, "load_synced_fingerprints", lambda: {})

    def fake_collect_outputs(before):
        record["outputs_before"] = before
        return []

    monkeypatch.setattr(cleanup, "collect_outputs", fake_collect_outputs)
    monkeypatch.setattr(cleanup, "collect_workspace_media", lambda days: [])
    monkeypatch.setattr(cleanup, "prune_download_maps", lambda apply: 0)
    monkeypatch.setattr(cleanup, "truncate_logs", lambda apply: [])
    monkeypatch.setattr(cleanup, "prune_history", lambda apply: 0)

    def fake_collect_trash(retention_days):
        record["trash_days"] = retention_days
        return []

    monkeypatch.setattr(cleanup, "collect_trash_dirs", fake_collect_trash)


def _run_main(argv):
    """跑 main() 并回收 stdout（不依赖 pytest 的 capsys，本机只有自带 runner）。"""
    buf = io.StringIO()
    monkeypatch_argv = list(sys.argv)
    sys.argv = ["cleanup_daily.py"] + argv
    try:
        with contextlib.redirect_stdout(buf):
            rc = cleanup.main()
    finally:
        sys.argv = monkeypatch_argv
    return rc, buf.getvalue()


def test_main_actually_uses_tightened_retention(monkeypatch):
    """判别力自检：旧代码只看 args，这里 outputs 会是「今天-7」→ 必须红。"""
    record = {}
    _stub_pipeline(monkeypatch, record)
    monkeypatch.setattr(cleanup, "free_gb_for", lambda path: 12.0)

    rc, out = _run_main(["--outputs-retention", "7", "--trash-days", "7"])
    assert rc == 0
    assert "低磁盘水位" in out
    assert record["outputs_before"] == dt.date.today() - dt.timedelta(days=3)
    assert record["trash_days"] == 1


def test_main_keeps_requested_retention_when_no_auto_tighten(monkeypatch):
    record = {}
    _stub_pipeline(monkeypatch, record)
    monkeypatch.setattr(cleanup, "free_gb_for", lambda path: 1.0)

    rc, out = _run_main(["--outputs-retention", "7", "--trash-days", "7", "--no-auto-tighten"])
    assert rc == 0
    assert "已关闭" in out
    assert record["outputs_before"] == dt.date.today() - dt.timedelta(days=7)
    assert record["trash_days"] == 7


def test_main_reports_probe_failure_without_tightening(monkeypatch):
    record = {}
    _stub_pipeline(monkeypatch, record)
    monkeypatch.setattr(cleanup, "free_gb_for", lambda path: None)

    rc, out = _run_main(["--outputs-retention", "7", "--trash-days", "7"])
    assert rc == 0
    assert "探测失败" in out
    assert record["outputs_before"] == dt.date.today() - dt.timedelta(days=7)
    assert record["trash_days"] == 7
