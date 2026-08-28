import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "director" / "assets"


def test_gpt_image_templates_schema():
    d = json.loads((ASSETS / "gpt_image_templates.json").read_text(encoding="utf-8"))
    assert d["source"].startswith("https://github.com/")
    assert len(d["categories"]) >= 10
    assert len(d["cases"]) >= 400
    case = d["cases"][0]
    for key in ("id", "title", "prompt"):
        assert key in case and case[key]
    total = (ASSETS / "gpt_image_templates.json").stat().st_size
    assert total < 600 * 1024, f"体积超限: {total}"


def test_nano_banana_styles_schema():
    d = json.loads((ASSETS / "nano_banana_styles.json").read_text(encoding="utf-8"))
    assert len(d["styles"]) >= 15
    s = d["styles"][0]
    assert s["name"] and s["prompt"]
    assert (ASSETS / "nano_banana_styles.json").stat().st_size < 150 * 1024


def test_shortcut_inspirations_schema():
    d = json.loads((ASSETS / "shortcut_inspirations.json").read_text(encoding="utf-8"))
    assert 100 <= len(d["items"]) <= 500
    it = d["items"][0]
    assert it["title"] and it["prompt"]
    assert (ASSETS / "shortcut_inspirations.json").stat().st_size < 300 * 1024


def test_negative_tags_schema():
    d = json.loads((ASSETS / "negative_tags.json").read_text(encoding="utf-8"))
    assert len(d["negative"]) >= 30
    assert len(d["styles"]) >= 30
    assert (ASSETS / "negative_tags.json").stat().st_size < 80 * 1024
