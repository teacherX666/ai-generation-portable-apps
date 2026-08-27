import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "cleanup_daily", ROOT / "tools" / "cleanup_daily.py"
)
cleanup = importlib.util.module_from_spec(_spec)
sys.modules["cleanup_daily"] = cleanup
_spec.loader.exec_module(cleanup)


def test_prune_history_removes_old_and_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "HISTORY_PATH", tmp_path / "history.json")
    now = time.time()
    data = {
        "a:old": {"submitted_at": now - 40 * 86400},
        "a:new": {"submitted_at": now},
    }
    (tmp_path / "history.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    removed = cleanup.prune_history(apply=True)
    assert removed == 1
    saved = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert "a:old" not in saved and "a:new" in saved


def test_prune_history_dry_run_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "HISTORY_PATH", tmp_path / "history.json")
    now = time.time()
    (tmp_path / "history.json").write_text(
        json.dumps({"a:old": {"submitted_at": now - 40 * 86400}}, ensure_ascii=False),
        encoding="utf-8",
    )
    removed = cleanup.prune_history(apply=False)
    assert removed == 1
    saved = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert "a:old" in saved  # dry-run 不改文件


def test_prune_history_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "HISTORY_PATH", tmp_path / "none.json")
    assert cleanup.prune_history(apply=True) == 0
