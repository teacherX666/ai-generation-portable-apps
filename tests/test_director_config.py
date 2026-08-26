import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "director"))

import app as director  # noqa: E402


def test_seedream_size_mapping():
    assert director.seedream_size("2K", "1:1") == "2048x2048"
    assert director.seedream_size("2K", "9:16") == "1584x2816"
    assert director.seedream_size("1K", "4:3") == "1152x864"


def test_seedream_size_rejects_unknown_ratio():
    try:
        director.seedream_size("2K", "5:4")
    except ValueError as exc:
        assert "比例" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_config_payload_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PORT", "8895")
    monkeypatch.setenv("CORS", "1")
    monkeypatch.setenv("VOLCENGINE_ARK_API_KEY", "test-ark-key")
    import importlib

    importlib.reload(director)
    payload = director.config_payload()
    assert payload["aspect_ratios"] == list(director.ASPECT_RATIOS)
    assert payload["resolutions"] == ["1K", "1.5K", "2K"]
    assert payload["ark_ready"] is True
    assert payload["model"] == "doubao-seedream-5-0-pro-260628"
