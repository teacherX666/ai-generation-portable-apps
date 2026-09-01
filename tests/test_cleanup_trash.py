import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "cleanup_daily_trash", ROOT / "tools" / "cleanup_daily.py"
)
cleanup = importlib.util.module_from_spec(_spec)
sys.modules["cleanup_daily_trash"] = cleanup
_spec.loader.exec_module(cleanup)


def _make_trash_tree(root: Path):
    old = root / "ai-portable-cleanup-20260801_034700"
    old.mkdir(parents=True)
    (old / "video.mp4").write_bytes(b"x" * 100)
    recent = root / "ai-portable-cleanup-20260828_034700"
    recent.mkdir()
    (recent / "video.mp4").write_bytes(b"y" * 50)
    # 名字模式不符的目录与文件永远不碰
    other = root / "unrelated-folder"
    other.mkdir()
    (other / "keep.mp4").write_bytes(b"z" * 10)
    (root / "ai-portable-cleanup-20260701_034700").write_bytes(b"not-a-dir")
    os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    return old, recent, other


def test_collect_trash_dirs_only_matches_exact_pattern(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "TRASH_ROOT", tmp_path)
    old, recent, other = _make_trash_tree(tmp_path)
    hits = cleanup.collect_trash_dirs(30)
    assert [str(d) for d, _ in hits] == [str(old)]
    assert sum(size for _, size in hits) == 100


def test_run_trash_purge_dry_run_leaves_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "TRASH_ROOT", tmp_path)
    old, recent, other = _make_trash_tree(tmp_path)
    hits = cleanup.collect_trash_dirs(30)
    freed = cleanup.run_trash_purge(hits, apply=False)
    assert freed == 100
    assert old.is_dir() and recent.is_dir() and other.is_dir()


def test_run_trash_purge_apply_deletes_only_expired_matching_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "TRASH_ROOT", tmp_path)
    old, recent, other = _make_trash_tree(tmp_path)
    hits = cleanup.collect_trash_dirs(30)
    freed = cleanup.run_trash_purge(hits, apply=True)
    assert freed == 100
    assert not old.exists()
    assert recent.is_dir() and (recent / "video.mp4").exists()
    assert other.is_dir() and (other / "keep.mp4").exists()
    # 非目录的伪装文件也不动
    assert (tmp_path / "ai-portable-cleanup-20260701_034700").is_file()
