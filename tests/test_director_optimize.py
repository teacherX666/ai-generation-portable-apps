import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "director"))
sys.path.insert(0, str(ROOT))

from shared import model_gateway  # noqa: E402

# 用唯一模块名加载，避免与套件里其它 `import app` 的测试撞 sys.modules
_spec = importlib.util.spec_from_file_location(
    "director_app_optimize", ROOT / "director" / "app.py"
)
director = importlib.util.module_from_spec(_spec)
sys.modules["director_app_optimize"] = director
_spec.loader.exec_module(director)


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

    # optimize_prompt 走 shared.model_gateway.call_llm（不是 director 自己的
    # request_json），所以要拦网关里的那个符号；旧写法会真的发网络请求。
    monkeypatch.setattr(model_gateway, "request_json", fake_request_json)
    result = director.optimize_prompt("一只猫", "refine")
    assert result["ok"] is True
    assert result["prompt"] == "优化后的提示词"
    # 2026-09-15 起 deepseek provider 指向火山方舟（DeepSeek V4.1 Flash），不再是官方 API
    assert captured["url"].startswith("https://ark.cn-beijing.volces.com/api/v3")
    assert "提示词专家" in captured["body"]["messages"][0]["content"]
    assert "优化" in captured["body"]["messages"][1]["content"]
