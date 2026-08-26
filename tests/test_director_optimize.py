import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "director"))

import app as director  # noqa: E402


def test_optimize_empty_text_rejected():
    result = director.optimize_prompt("   ", "refine")
    assert result["ok"] is False
    assert "输入提示词" in result["error"]


def test_optimize_missing_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "SKILL_PATH", tmp_path / "none.md")
    monkeypatch.setattr(director, "_load_deepseek_key", lambda: "sk-test")
    result = director.optimize_prompt("一只猫", "refine")
    assert result["ok"] is False
    assert "SKILL" in result["error"]


def test_optimize_calls_deepseek_and_returns_prompt(tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text("你是提示词专家。", encoding="utf-8")
    monkeypatch.setattr(director, "SKILL_PATH", skill)
    monkeypatch.setattr(director, "_load_deepseek_key", lambda: "sk-test")

    captured: dict = {}

    def fake_request_json(method, url, api_key, body=None, timeout=None):
        captured.update(method=method, url=url, body=body)
        return {"choices": [{"message": {"content": "优化后的提示词"}}]}

    monkeypatch.setattr(director, "request_json", fake_request_json)
    result = director.optimize_prompt("一只猫", "refine")
    assert result["ok"] is True
    assert result["prompt"] == "优化后的提示词"
    assert captured["url"].startswith("https://api.deepseek.com")
    assert "提示词专家" in captured["body"]["messages"][0]["content"]
    assert "优化" in captured["body"]["messages"][1]["content"]
