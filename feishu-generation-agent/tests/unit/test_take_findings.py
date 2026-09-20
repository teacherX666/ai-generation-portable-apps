"""模型审片（自动找穿帮）：解析、汇总、无分析器兜底。"""

from pathlib import Path

import pytest

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.integrations import video_insight
from feishu_generation_agent.integrations.video_insight import (
    DeepSeekVideoInsight,
    analyze_artifacts,
)


@pytest.fixture(autouse=True)
def _clear_take_cache():
    """看片缓存按 sha256 跨调用复用，测试之间必须清掉，否则互相污染。"""
    video_insight._TAKE_CACHE.clear()
    yield
    video_insight._TAKE_CACHE.clear()


class _TakeAnalyzer:
    """只实现审片模式的替身。"""

    def __init__(self, findings: dict) -> None:
        self._findings = findings
        self.calls = 0

    async def analyze_take(self, asset: MediaAsset) -> dict:
        self.calls += 1
        return self._findings


def _artifact(tmp_path: Path) -> dict:
    path = tmp_path / "take.mp4"
    path.write_bytes(b"video-bytes")
    return {
        "artifact_id": "artifact-1",
        "task_id": "task-1",
        "kind": "video",
        "mime_type": "video/mp4",
        "local_path": str(path),
        "size": path.stat().st_size,
        "sha256": "sha-1",
    }


async def test_same_take_is_only_watched_once(tmp_path: Path) -> None:
    """同一段成片只让模型看一次（看一次 ≈ 40k token，2026-09-18 用户要求省额度）。

    审片和返工融合看的是同一段视频；以前各看一遍，用户点完审片再重跑就白烧 40k。
    """
    analyzer = _TakeAnalyzer({"summary": "画面", "problems": [], "uncertainties": []})
    artifact = _artifact(tmp_path)

    await analyze_artifacts(analyzer, [artifact])
    await analyze_artifacts(analyzer, [artifact])

    assert analyzer.calls == 1  # 第二次命中缓存


async def test_take_cache_not_used_when_prompt_changes(tmp_path: Path) -> None:
    """带提示词逐条核对的结果**不缓存** —— 换了提示词结论会变。"""

    class _Recorder:
        def __init__(self) -> None:
            self.calls = 0

        async def analyze_take(self, asset, prompt: str = "") -> dict:
            self.calls += 1
            return {"summary": "画面", "problems": [], "uncertainties": []}

    analyzer = _Recorder()
    artifact = _artifact(tmp_path)

    await analyze_artifacts(analyzer, [artifact], prompt="要求A")
    await analyze_artifacts(analyzer, [artifact], prompt="要求B")

    assert analyzer.calls == 2


async def test_analyze_artifacts_returns_problems(tmp_path: Path) -> None:
    analyzer = _TakeAnalyzer({
        "summary": "一名男子在厨房拆纸箱",
        "problems": [
            {"at": "0:03", "issue": "手部穿模", "why": "手指穿过纸箱壁"},
        ],
        "uncertainties": ["结尾看不清"],
    })

    findings = await analyze_artifacts(analyzer, [_artifact(tmp_path)])

    assert findings["available"] is True
    assert findings["artifact_id"] == "artifact-1"
    assert findings["problems"][0]["issue"] == "手部穿模"
    assert findings["uncertainties"] == ["结尾看不清"]
    assert analyzer.calls == 1


async def test_analyze_artifacts_without_analyzer_is_not_fatal() -> None:
    findings = await analyze_artifacts(None, [])

    assert findings["available"] is False
    assert findings["problems"] == []
    assert "没有配置" in findings["reason"]


async def test_analyze_artifacts_without_video_says_so(tmp_path: Path) -> None:
    findings = await analyze_artifacts(_TakeAnalyzer({}), [])

    assert findings["available"] is False
    assert "没有可分析的成片" in findings["reason"]


async def test_analyze_artifacts_survives_analyzer_failure(tmp_path: Path) -> None:
    class _Broken:
        async def analyze_take(self, asset: MediaAsset) -> dict:
            raise RuntimeError("看片炸了")

    findings = await analyze_artifacts(_Broken(), [_artifact(tmp_path)])

    assert findings["available"] is False
    assert "看片失败" in findings["reason"]


async def test_analyze_artifacts_passes_requirement_prompt(tmp_path: Path) -> None:
    """审片要把当初的提示词一起给模型，否则抓不到「要求了但没做到」。"""

    class _Recorder:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def analyze_take(self, asset, prompt: str = "") -> dict:
            self.prompts.append(prompt)
            return {"summary": "画面", "problems": [], "uncertainties": []}

    analyzer = _Recorder()
    await analyze_artifacts(
        analyzer, [_artifact(tmp_path)], prompt="第 1 根枝桠上有三只绿色小鸟"
    )

    assert analyzer.prompts == ["第 1 根枝桠上有三只绿色小鸟"]


def test_parse_keeps_violation_kind() -> None:
    """「要求了但没做到」要能和普通穿帮区分开（用户最在意的就是这类）。"""
    raw = (
        '{"summary":"画面", "problems":['
        '{"at":"0:03","issue":"要求第 1 根枝桠三只绿鸟，实际只有两只",'
        '"why":"数量不对","kind":"违背要求"}]}'
    )

    data = DeepSeekVideoInsight._parse("artifact-1", raw)

    assert data["problems"][0]["kind"] == "违背要求"


def test_parse_cleans_and_caps_problems() -> None:
    raw = (
        '{"summary":" 画面 ", "problems":['
        '{"at":"0:03","issue":" 手部穿模 ","why":" 手指穿过纸箱 "},'
        '{"issue":""},{"at":"0:05"}, "不是对象"],'
        '"uncertainties":[" 结尾 ", ""]}'
    )

    data = DeepSeekVideoInsight._parse("artifact-1", raw)

    assert data["summary"] == "画面"
    assert data["problems"] == [
        {
            "at": "0:03",
            "issue": "手部穿模",
            "why": "手指穿过纸箱",
            "kind": "穿帮",
        },
    ]
    assert data["uncertainties"] == ["结尾"]
