"""「视频编辑」必须把任务类型钉死成 edit，否则方舟会静默返回原片。

2026-09-22 生产事故（用户原话：「视频编辑完全就是返回的原视频，我说的内容他是一点没改」）：
自由创作的「Seedance 2.5 · 视频编辑」入口下发了 task_mode=edit / duration=-1 /
ratio=adaptive / role=reference_video，**唯独没下发 omni_reference_task_type**。
官方文档《Doubao Seedance 2.5 教程》写明任务类型由模型根据素材+提示词意图判断，
视频编辑的推荐配置第 1 条就是 `omni_reference_task_type = edit`。

实测（同素材同提示词，唯一变量是该参数）：
    不下发     → 同帧差 mean 3.91 / max  7.21（241 帧全同，等于没改）
    下发 edit  → 同帧差 mean 17.65 / max 51.85（局部真的改了）

写法说明：本机没装 pytest，`tests/run_tests_local.py` 只收**模块级** test_* 函数，
所以这里不用 unittest.TestCase（两种 runner 都能跑）。
"""

import importlib.util
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

_MOD = None


def _mod():
    global _MOD
    if _MOD is None:
        spec = importlib.util.spec_from_file_location("seedance_app_omni_test", ROOT / "seedance" / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _MOD = module
    return _MOD


class FakeField:
    def __init__(self, value=None, filename=None, blob=b""):
        self.value = value
        self.filename = filename
        self.file = _Blob(blob)


class _Blob:
    def __init__(self, blob):
        self._blob = blob

    def read(self, *args):
        return self._blob


class FakeForm(dict):
    """build_payload 只用到 in / [] / .value / .filename / .file.read()。"""


def make_form(**values):
    form = FakeForm()
    for key, value in values.items():
        form[key] = FakeField(value=str(value))
    return form


def _payload(form, notes=None):
    mod = _mod()
    with mock.patch.object(mod, "media_reference", return_value={"url": "https://example.test/ref.mp4"}), \
         mock.patch.object(mod, "tos_upload", return_value="https://example.test/ref.mp4"):
        return mod.build_payload(form, "ark-test", "https://ark.example/api/v3", 0, "ws", notes=notes)


def _edit_form(**extra):
    values = {"provider": "volcengine", "model": "doubao-seedance-2-5-260628",
              "prompt": "编辑视频：把红球改成蓝球", "task_mode": "edit",
              "duration": "-1", "ratio": "adaptive"}
    values.update(extra)
    form = make_form(**values)
    form["ref_video_1"] = FakeField(filename="src.mp4", blob=b"\x00" * 32)
    return form


def test_edit_task_pins_omni_reference_task_type():
    payload = _payload(_edit_form())
    assert payload["omni_reference_task_type"] == "edit", payload.get("omni_reference_task_type")
    assert payload["duration"] == -1
    assert payload["ratio"] == "adaptive"
    assert payload["content"][1]["role"] == "reference_video"


def test_edit_task_coerces_bad_duration_and_ratio():
    """编辑任务 duration 必须 -1、ratio 必须 adaptive（官方硬约束），就地改对。"""
    payload = _payload(_edit_form(duration="8", ratio="16:9"))
    assert payload["duration"] == -1
    assert payload["ratio"] == "adaptive"
    assert payload["omni_reference_task_type"] == "edit"


def test_plain_reference_task_stays_auto():
    """普通「参考生视频」不能被打上 edit —— 那会让方舟按编辑校验参数。"""
    form = make_form(provider="volcengine", model="doubao-seedance-2-5-260628",
                     prompt="让画面动起来", task_mode="reference",
                     duration="8", ratio="16:9")
    form["ref_video_1"] = FakeField(filename="src.mp4", blob=b"\x00" * 32)
    payload = _payload(form)
    assert "omni_reference_task_type" not in payload
    assert payload["duration"] == 8
    assert payload["ratio"] == "16:9"


def test_explicit_extend_is_honoured():
    payload = _payload(_edit_form(task_mode="reference", omni_reference_task_type="extend",
                                  duration="12", ratio="16:9"))
    assert payload["omni_reference_task_type"] == "extend"
    assert payload["ratio"] == "adaptive", "延长任务 ratio 必须 adaptive"
    assert payload["duration"] == 12, "延长任务时长由用户定，不该被改写"


def test_explicit_auto_is_passed_through():
    payload = _payload(_edit_form(omni_reference_task_type="auto"))
    assert "omni_reference_task_type" not in payload, "auto 是方舟默认行为，没必要显式下发"


def test_edit_without_reference_video_is_not_pinned():
    """edit 要求至少一个 reference_video；没有就别塞，否则方舟同步 400。"""
    form = make_form(provider="volcengine", model="doubao-seedance-2-5-260628",
                     prompt="编辑视频", task_mode="edit", duration="-1", ratio="adaptive")
    form["ref_image_1"] = FakeField(filename="a.png", blob=b"\x00" * 32)
    payload = _payload(form)
    assert "omni_reference_task_type" not in payload


def test_invalid_value_is_rejected_loudly():
    try:
        _payload(_edit_form(omni_reference_task_type="rewrite"))
    except RuntimeError:
        return
    raise AssertionError("非法 omni_reference_task_type 必须报错，而不是静默忽略")


def test_notes_explain_the_coercion():
    notes = []
    _payload(_edit_form(duration="8", ratio="16:9"), notes=notes)
    assert any("视频编辑任务时长" in note for note in notes), notes
    assert any("视频编辑任务比例" in note for note in notes), notes


def test_noop_detector_returns_none_on_garbage():
    """顺手提醒不能反过来把任务搞挂：读不出来就返回 None。"""
    assert _mod().edit_output_looks_unchanged(b"not a video", ROOT / "nope.mp4") is None


def test_noop_threshold_sits_above_reencode_noise():
    # 重编码噪声实测 3~5/255，真实编辑 ≥15/255；阈值必须落在两者之间。
    assert 5.0 < _mod().EDIT_NOOP_DIFF < 15.0
