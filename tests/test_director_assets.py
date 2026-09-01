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


import importlib.util
import sys

sys.path.insert(0, str(ROOT / "director"))
_spec = importlib.util.spec_from_file_location(
    "director_app_assets", ROOT / "director" / "app.py"
)
director = importlib.util.module_from_spec(_spec)
sys.modules["director_app_assets"] = director
_spec.loader.exec_module(director)


def test_assets_payload(monkeypatch):
    monkeypatch.setattr(director, "ASSETS_DIR", ROOT / "director" / "assets")
    payload = director.assets_payload()
    assert payload["version"] == director.ASSETS_VERSION
    for key in ("gpt_image_templates", "nano_banana_styles",
                "shortcut_inspirations", "negative_tags"):
        assert isinstance(payload[key], dict)
    # 体积上限：一次性下发不应超过 1MB
    assert len(json.dumps(payload, ensure_ascii=False)) < 1024 * 1024


def test_optimize_langgpt_mode(monkeypatch, tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("langgpt框架", encoding="utf-8")
    monkeypatch.setattr(director, "SKILL_PATH", skill)
    monkeypatch.setattr(director, "_load_deepseek_key", lambda: "sk-test")
    captured = {}
    monkeypatch.setattr(director, "request_json",
                        lambda m, u, k, body=None, timeout=None:
                        (captured.update(body=body) or
                         {"choices": [{"message": {"content": "结构化结果"}}]}))
    result = director.optimize_prompt("一只猫", "langgpt")
    assert result["ok"] is True
    sys_msg = captured["body"]["messages"][0]["content"]
    assert "langgpt框架" in sys_msg
    user = captured["body"]["messages"][1]["content"]
    assert "结构化" in user
