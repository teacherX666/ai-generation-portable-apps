"""Regression guards for issues repeatedly exposed by repository syncs.

These tests intentionally cover the cross-module seams that ordinary provider
unit tests miss:
- old media stored under the raw workspace must still resolve after user
  isolation changes;
- Feishu must be launched with its own interpreter because its dependencies
  (LangGraph) are not installed in the shared Portal venv.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_module(relative_path: str, module_name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_nano_saved_media_falls_back_to_raw_workspace(tmp_path: Path, monkeypatch):
    nano = _load_module("nano-banana/app.py", "nano_sync_regression")
    raw_ws = "ws-old"
    scoped_ws = "u_mm_ws-old"
    stored = "reference.png"
    payload = b"reference-image-bytes"

    def media_dir(scope: str) -> Path:
        return tmp_path / "workspaces" / scope / "media"

    monkeypatch.setattr(nano, "_ws_media_dir", media_dir)
    media_dir(raw_ws).mkdir(parents=True)
    (media_dir(raw_ws) / stored).write_bytes(payload)

    assert nano._resolve_media_path(stored, scoped_ws, raw_ws) == media_dir(raw_ws) / stored

    class Item:
        filename = None

        def __init__(self, value: str):
            self.value = value

    form = {
        "saved_media": Item(json.dumps({
            "image_1": {"filename": "reference.png", "stored": stored},
        }, ensure_ascii=False)),
    }
    assert nano.get_file_or_saved(form, "image_1", scoped_ws, raw_ws) == ("reference.png", payload)

    restored = nano._files_from_restore(
        {"media": {"image_1": {"filename": "reference.png", "stored": stored}}},
        scoped_ws,
        raw_ws,
    )
    assert restored == {"image_1": ("reference.png", payload)}


def test_feishu_uses_its_own_venv_interpreter():
    app_spec = _load_module("portal/app_spec.py", "portal_app_spec_sync_regression")
    specs = app_spec.load_specs(ROOT / "portal" / "apps.json", ROOT)
    feishu = next(item for item in specs if item.name == "feishu-generation-agent")
    assert feishu.interpreter == "feishu-generation-agent/.venv/bin/python"
