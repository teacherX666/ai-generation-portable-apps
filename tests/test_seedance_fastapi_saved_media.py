"""Regression: Seedance FastAPI production path must honor saved_media refs."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_fastapi_module():
    path = ROOT / "seedance" / "app_fastapi.py"
    spec = importlib.util.spec_from_file_location("seedance_fastapi_saved_media_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_seedance_fastapi_resolves_saved_reference_images(tmp_path: Path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "secrets.json").write_text(
        json.dumps({"volcengine_api_key": "test-key"}), encoding="utf-8"
    )
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    module = _load_fastapi_module()
    ws_id = "ws-seedance-test"
    media_dir = tmp_path / "workspaces" / ws_id / "media"
    media_dir.mkdir(parents=True)
    first = b"first-reference"
    second = b"second-reference"
    (media_dir / "first.png").write_bytes(first)
    (media_dir / "second.png").write_bytes(second)
    monkeypatch.setattr(module.legacy, "_ws_media_dir", lambda _ws: media_dir)
    values = {
        "saved_media": json.dumps({
            "ref_image_1": {"filename": "1.png", "stored": "first.png", "mime": "image/png"},
            "ref_image_2": {"filename": "2.png", "stored": "second.png", "mime": "image/png"},
        }, ensure_ascii=False),
    }
    resolved = module._resolve_submit_files(values, {}, ws_id)
    assert resolved == {
        "ref_image_1": ("1.png", first),
        "ref_image_2": ("2.png", second),
    }
