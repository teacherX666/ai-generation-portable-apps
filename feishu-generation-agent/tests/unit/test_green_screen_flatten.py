"""绿幕归一接进流水线的守门测试（2026-10-14）。

**背景**：用户报「刮奶油的视频绿幕会出现阴影等影响抠图的东西」。提示词侧补了物理约束
（`planner._ensure_green_screen_constraints`）+ 长期口径种子之后，**带着约束新生成的那条
依然有阴影**（实测绿幕绿通道只有 117、标准差 29.5）—— 文字改不动模型怎么渲染「被照亮的
表面」。所以改成成片侧**确定性**归一：落盘前把绿幕区域刷成同一纯色。

这里守三件事：
1. 只在**绿幕/抠图**镜头上生效（普通镜头不许被后处理）；
2. 走的是**落盘前**的 `transform`（落盘后改文件会破坏 `{sha256}.{ext}` 产物校验）；
3. **任何失败都保留原片**（后处理绝不能把产物搞坏或让 run 失败）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from feishu_generation_agent.integrations.green_screen import (
    flatten_video_bytes,
    is_green_screen_prompt,
    resolve_python,
)


def _settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    script = tmp_path / "flatten_green_screen.py"
    if not script.exists():
        script.write_text("# 假脚本\n", encoding="utf-8")
    base = {
        "green_screen_flatten_enabled": True,
        "green_screen_flatten_script": script,
        "green_screen_flatten_python": "",
        "green_screen_target_color": "00B140",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_is_green_screen_prompt() -> None:
    assert is_green_screen_prompt("刮过处露出纯色绿幕，边缘平整")
    assert is_green_screen_prompt("这一镜要方便抠图")
    assert not is_green_screen_prompt("小黄鸭在月夜小道上行走")


def test_flatten_returns_none_when_script_missing(tmp_path: Path) -> None:
    """脚本不在就保留原片（部署不完整不该让产物变坏）。"""
    settings = _settings(tmp_path, green_screen_flatten_script=tmp_path / "missing.py")
    assert flatten_video_bytes(b"fake-mp4", settings=settings) is None


def test_flatten_returns_none_when_python_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.green_screen.resolve_python",
        lambda configured="": None,
    )
    assert flatten_video_bytes(b"fake-mp4", settings=_settings(tmp_path)) is None


def test_flatten_returns_none_on_failure(tmp_path: Path, monkeypatch) -> None:
    """脚本报错 → 保留原片（绝不让后处理把 run 搞坏）。"""
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.green_screen.resolve_python",
        lambda configured="": "python",
    )
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.green_screen.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"boom"),
    )
    assert flatten_video_bytes(b"fake-mp4", settings=_settings(tmp_path)) is None


def test_flatten_returns_flattened_bytes(tmp_path: Path, monkeypatch) -> None:
    """成功时返回**归一后的字节**（调用方把它写进产物）。"""
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.green_screen.resolve_python",
        lambda configured="": "python",
    )

    def fake_run(command, **kwargs):
        target = Path(command[command.index("--out") + 1])
        target.write_bytes(b"flattened-mp4")
        return subprocess.CompletedProcess(command, 0, b"ok", b"")

    monkeypatch.setattr(
        "feishu_generation_agent.integrations.green_screen.subprocess.run", fake_run
    )
    assert (
        flatten_video_bytes(b"fake-mp4", settings=_settings(tmp_path))
        == b"flattened-mp4"
    )


def test_flatten_returns_none_when_output_identical(tmp_path: Path, monkeypatch) -> None:
    """脚本没改动内容（等于没归一）→ 返回 None，避免无意义地换掉产物。"""
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.green_screen.resolve_python",
        lambda configured="": "python",
    )

    def fake_run(command, **kwargs):
        target = Path(command[command.index("--out") + 1])
        target.write_bytes(b"fake-mp4")
        return subprocess.CompletedProcess(command, 0, b"ok", b"")

    monkeypatch.setattr(
        "feishu_generation_agent.integrations.green_screen.subprocess.run", fake_run
    )
    assert flatten_video_bytes(b"fake-mp4", settings=_settings(tmp_path)) is None


def test_resolve_python_skips_interpreters_without_cv2(monkeypatch) -> None:
    """**实测**解释器能力：venv 没有 cv2 时不能选它（2026-10-14 端到端回放踩到过）。

    原来只判「文件存在」→ 永远选中 `sys.executable`（服务的 venv）→ 每次归一都失败、
    静默保留原片（安全但功能等于没做）。
    """
    import sys

    from feishu_generation_agent.integrations import green_screen

    green_screen._PROBE_CACHE.clear()
    good = green_screen._KNOWN_PYTHONS[0]
    monkeypatch.setattr(
        green_screen, "_can_import_cv2", lambda python: python == good
    )
    assert green_screen.resolve_python("") == good
    assert green_screen.resolve_python("") != sys.executable


def test_resolve_python_returns_none_when_nothing_has_cv2(monkeypatch) -> None:
    from feishu_generation_agent.integrations import green_screen

    green_screen._PROBE_CACHE.clear()
    monkeypatch.setattr(green_screen, "_can_import_cv2", lambda python: False)
    assert green_screen.resolve_python("") is None


def test_resolve_python_prefers_configured_existing(tmp_path: Path, monkeypatch) -> None:
    """显式配置的解释器**存在且能跑**就用它（部署可通过 .env 覆盖）。"""
    from feishu_generation_agent.integrations import green_screen

    green_screen._PROBE_CACHE.clear()
    configured = tmp_path / "python.exe"
    configured.write_bytes(b"")
    monkeypatch.setattr(green_screen, "_can_import_cv2", lambda python: True)
    assert green_screen.resolve_python(str(configured)) == str(configured)


def test_resolve_python_ignores_configured_path_that_does_not_exist(
    tmp_path: Path, monkeypatch
) -> None:
    """配了不存在的路径 → 不许拿它去跑（回退到真正能跑的解释器）。"""
    from feishu_generation_agent.integrations import green_screen

    green_screen._PROBE_CACHE.clear()
    missing = tmp_path / "nope.exe"
    monkeypatch.setattr(
        green_screen, "_can_import_cv2", lambda python: python == green_screen._KNOWN_PYTHONS[0]
    )
    assert green_screen.resolve_python(str(missing)) == green_screen._KNOWN_PYTHONS[0]


def test_video_transform_only_for_green_screen_video(tmp_path: Path) -> None:
    """接线守门：只有「开关开 + 绿幕镜头」的视频任务才挂 transform。"""
    from feishu_generation_agent.graph.nodes import _video_transform_for_task

    settings = _settings(tmp_path)
    services = SimpleNamespace(settings=settings)

    green = SimpleNamespace(prompt="刮过处露出纯色绿幕")
    plain = SimpleNamespace(prompt="小黄鸭在月夜小道上行走")

    assert _video_transform_for_task(green, services) is not None
    assert _video_transform_for_task(plain, services) is None
    assert (
        _video_transform_for_task(
            green, SimpleNamespace(settings=SimpleNamespace(green_screen_flatten_enabled=False))
        )
        is None
    ), "开关关掉时不许后处理"
