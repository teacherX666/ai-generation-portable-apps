"""Regression: FastAPI production path must honor saved_media references."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_fastapi_module():
    path = ROOT / "nano-banana" / "app_fastapi.py"
    spec = importlib.util.spec_from_file_location("nano_fastapi_saved_media_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fastapi_resolves_saved_media_without_new_upload(tmp_path: Path, monkeypatch):
    module = _load_fastapi_module()
    ws_id = "ws-fastapi-test"
    stored = "saved-reference.png"
    payload = b"fastapi-saved-media-bytes"
    media_dir = tmp_path / "workspaces" / ws_id / "media"
    media_dir.mkdir(parents=True)
    (media_dir / stored).write_bytes(payload)
    monkeypatch.setattr(module.legacy, "_ws_media_dir", lambda _ws: media_dir)
    values = {
        "saved_media": json.dumps({
            "image_1": {
                "filename": "reference.png",
                "stored": stored,
                "mime": "image/png",
            }
        }, ensure_ascii=False),
    }
    resolved = module._resolve_submit_files(values, {}, ws_id)
    assert resolved == {"image_1": ("reference.png", payload)}
