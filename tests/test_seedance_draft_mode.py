"""Seedance 2.5 样片模式（Draft）的两段式接线。

官方口径（《创建视频生成任务》+《Doubao Seedance 2.5 教程 · 样片模式》，2026-10-15 核对）：
  * `draft: true`  开启样片模式，**只允许 480p**（传其他分辨率直接报错）；
                   返回的任务 id 即 Draft 任务 ID（7 天有效）。
  * `content: [{"type": "draft_task", "draft_task": {"id": <Draft 任务 id>}}]`
                   基于样片出正式成片；提示词、参考素材、随机种子、宽高比、时长由模型
                   **自动复用**，可重新指定的是目标分辨率。id 必须是 Step1 返回的 `id`，
                   **不是** `upstreamTaskId`。
  * 仅 Seedance 2.5 支持。

这里守的是**我们这一侧的接线**（钉 480p、第二段只发最小体、不白跑一次素材上传、
非 2.5 模型在本地就被拒），不是方舟本身的行为。

写法说明：本机没装 pytest，`tests/run_tests_local.py` 只收**模块级** test_* 函数，
所以这里不用 unittest.TestCase（两种 runner 都能跑）。
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

_MOD = None


def _mod():
    global _MOD
    if _MOD is None:
        spec = importlib.util.spec_from_file_location("seedance_app_draft_test", ROOT / "seedance" / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _MOD = module
    return _MOD


def _providers():
    return json.loads((ROOT / "seedance" / "providers.json").read_text(encoding="utf-8"))


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


def _draft_form(**extra):
    values = {"provider": "volcengine", "model": "doubao-seedance-2-5-260628",
              "prompt": "女孩抱着狐狸，镜头缓缓拉出", "draft": "1",
              "duration": "8", "ratio": "16:9", "resolution": "720p"}
    values.update(extra)
    return make_form(**values)


# --- 第一段：样片 ---------------------------------------------------------


def test_draft_forces_480p():
    """界面把分辨率锁成 480p，但手写 API 调用可能带 720p —— 就地改对，别吃异步报错。"""
    payload = _payload(_draft_form(resolution="720p"))
    assert payload["draft"] is True
    assert payload["resolution"] == "480p"


def test_draft_note_explains_the_coercion():
    notes = []
    _payload(_draft_form(resolution="1080p"), notes=notes)
    assert any("样片模式只允许 480p" in note for note in notes), notes


def test_draft_at_480p_needs_no_note():
    notes = []
    _payload(_draft_form(resolution="480p"), notes=notes)
    assert not [note for note in notes if "480p" in note], notes


def test_plain_generation_has_no_draft_key():
    """没开样片模式时绝不能带上 draft —— 带了就变成 480p 样片，白烧一次生成。"""
    form = make_form(provider="volcengine", model="doubao-seedance-2-5-260628",
                     prompt="让画面动起来", duration="8", ratio="16:9", resolution="720p")
    payload = _payload(form)
    assert "draft" not in payload
    assert payload["resolution"] == "720p"


def test_draft_false_is_not_sent():
    payload = _payload(_draft_form(draft="0"))
    assert "draft" not in payload


def test_draft_keeps_prompt_and_reference_media():
    """第一段要真素材：样片的意义就是把提示词/参考图一起验证掉。"""
    form = _draft_form()
    form["ref_image_1"] = FakeField(filename="a.png", blob=b"\x00" * 32)
    payload = _payload(form)
    assert payload["content"][0]["type"] == "text"
    assert payload["content"][0]["text"]
    assert payload["content"][1]["role"] == "reference_image"
    assert payload["draft"] is True


# --- 第二段：基于样片出成片 ------------------------------------------------


def _final_form(**extra):
    values = {"provider": "volcengine", "model": "doubao-seedance-2-5-260628",
              "prompt": "基于样片生成正式成片", "draft_task_id": "cgt-draft-abc",
              "resolution": "1080p", "duration": "-1", "ratio": "adaptive"}
    values.update(extra)
    return make_form(**values)


def test_draft_task_sends_only_the_draft_reference():
    payload = _payload(_final_form())
    assert payload["content"] == [{"type": "draft_task", "draft_task": {"id": "cgt-draft-abc"}}]


def test_draft_task_does_not_re_send_reused_params():
    """提示词/素材/比例/时长/种子/有声/任务类型由模型自动复用，官方明令**禁止传入**
    （「即使传入值与 Draft 阶段使用的一致，也会触发报错」）—— 一个都不发。"""
    payload = _payload(_final_form())
    for key in ("duration", "ratio", "generate_audio", "watermark", "prompt", "seed",
                "omni_reference_task_type", "tools", "draft", "return_last_frame"):
        assert key not in payload, f"{key} 不该出现在第二段的请求体里"


def test_draft_task_pins_resolution_to_1080p():
    """官方：成片阶段 `resolution` 必须传入且**仅支持 1080p**（2026-10-15 实测发 720p 被拒）。"""
    payload = _payload(_final_form(resolution=""))
    assert payload["resolution"] == "1080p"
    assert _payload(_final_form(resolution="1080p"))["resolution"] == "1080p"


def test_draft_task_rejects_other_resolutions_loudly():
    """不能悄悄把用户要的 720p 抬成 1080p —— 分辨率直接决定计费档位。

    实测原文（2026-10-15，真机）：
        the parameter resolution specified in the request is not valid for model
        doubao-seedance-2-5 in draft_task final stage; received: "720p"
    """
    for bad in ("720p", "480p", "4k"):
        try:
            _payload(_final_form(resolution=bad))
        except RuntimeError as exc:
            assert "1080p" in str(exc), exc
            continue
        raise AssertionError(f"成片阶段分辨率 {bad} 必须就地报错，而不是静默改成 1080p 加价")


def test_draft_task_does_not_upload_media():
    """第二段短路返回，素材一个都不该挂 —— 挂了会白跑一遍本地/TOS 上传。"""
    mod = _mod()
    form = _final_form()
    form["ref_video_1"] = FakeField(filename="src.mp4", blob=b"\x00" * 32)
    with mock.patch.object(mod, "media_reference") as media_ref, \
         mock.patch.object(mod, "tos_upload") as tos:
        payload = mod.build_payload(form, "ark-test", "https://ark.example/api/v3", 0, "ws")
    media_ref.assert_not_called()
    tos.assert_not_called()
    assert payload["content"][0]["type"] == "draft_task"


def test_draft_task_note_says_params_are_reused():
    notes = []
    _payload(_final_form(), notes=notes)
    assert any("样片" in note and "1080p" in note for note in notes), notes


def test_content_counts_reports_draft_task():
    """第二段的 content 里只有 draft_task；计数漏了它，事件会写成「素材全 0」吓人。"""
    counts = _mod().content_counts({"content": [{"type": "draft_task", "draft_task": {"id": "x"}}]})
    assert "draft_task:1" in counts, counts


# --- 能力前置校验（本地拒，不等方舟异步报错） ------------------------------


def _validate(**values):
    body = {"provider": "volcengine", "model": "doubao-seedance-2-5-260628",
            "resolution": "480p", "ratio": "16:9", "duration": "8"}
    body.update(values)
    _mod().validate_model_capabilities(body, {})


def _expect_error(**values):
    try:
        _validate(**values)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"这批参数应当被拒：{values}")


def test_two_five_declares_draft_capability():
    """能力声明是这条链路的唯一真源（providers.json），掉了就整套静默失效。"""
    models = _providers()["providers"]["volcengine"]["models"]
    declared = {m["id"]: bool((m.get("capabilities") or {}).get("draft")) for m in models}
    assert declared.get("doubao-seedance-2-5-260628") is True, declared
    assert [mid for mid, ok in declared.items() if ok] == ["doubao-seedance-2-5-260628"], declared


def test_two_five_declares_1080p():
    """成片固定 1080p，而 `validate_model_capabilities` 按 providers.json 的声明校验 ——
    2.5 的声明里没有 1080p 的话，**我们自己的前置校验**会把合法的成片请求拦下来
    （2026-10-15 之前就漏了这个档位，官方能力表里 2.5 是 480p/720p/1080p）。"""
    models = _providers()["providers"]["volcengine"]["models"]
    resolutions = next(m["resolutions"] for m in models if m["id"] == "doubao-seedance-2-5-260628")
    assert "1080p" in resolutions, resolutions
    assert "4k" not in resolutions, "4k 是 2.0 独有，2.5 不声明"


def test_validate_allows_draft_on_2_5():
    _validate(draft="1")


def test_validate_rejects_draft_on_2_0():
    message = _expect_error(model="doubao-seedance-2-0-260128", draft="1")
    assert "样片模式" in message, message


def test_validate_rejects_draft_task_id_on_2_0():
    message = _expect_error(model="doubao-seedance-2-0-260128", draft_task_id="cgt-draft-abc")
    assert "样片模式" in message, message


def test_validate_allows_draft_task_id_on_2_5():
    _validate(draft_task_id="cgt-draft-abc", resolution="1080p", duration="-1", ratio="adaptive")


def test_validate_rejects_non_1080p_final_resolution_at_submit_time():
    """提交时就 400，别建了作业再在 run 里失败 —— 用户少等一轮、也少一条失败记录。"""
    message = _expect_error(draft_task_id="cgt-draft-abc", resolution="720p", duration="-1", ratio="adaptive")
    assert "1080p" in message, message


def test_draft_flows_through_json_value_fields():
    """自由创作走 /api/jobs/json：字段不在 VALUE_FIELDS 里就会被静默丢掉（功能全正常、只是没生效）。"""
    mod = _mod()
    assert "draft" in mod.VALUE_FIELDS
    assert "draft_task_id" in mod.VALUE_FIELDS


# ── 列表接口必须下发 draft 标志（2026-10-20「窗口无法返回」的根因） ──────────
#
# 卡片是从任务列表 `/api/jobs`（为瘦身不下发 params）和活动列表 `/api/activity`
#（不下发 request）画出来的。列表里没有 draft，前端就判不出样片 → 点卡片只会开普通
# 详情弹窗 → 关掉样片窗口后**再也回不去**。


def test_values_are_draft_reads_the_flag_in_all_shapes():
    mod = _mod()
    assert mod.values_are_draft({"draft": "1"}) is True
    assert mod.values_are_draft({"draft": "on"}) is True
    assert mod.values_are_draft({"draft": True}) is True
    assert mod.values_are_draft({"draft": ""}) is False
    assert mod.values_are_draft({"resolution": "480p"}) is False
    assert mod.values_are_draft(None) is False


def test_activity_record_draft_checks_request_and_restore():
    mod = _mod()
    assert mod.activity_record_draft({"request": {"values": {"draft": "1"}}}) is True
    assert mod.activity_record_draft({"request": {"parsed": {"values": {"draft": "1"}}}}) is True
    assert mod.activity_record_draft({"restore": {"values": {"draft": "1"}}}) is True
    assert mod.activity_record_draft({"request": {"values": {"resolution": "720p"}}}) is False
    assert mod.activity_record_draft({}) is False


def test_activity_list_summary_carries_draft_flag():
    """活动列表每条要带 draft —— 卡片靠它决定点开进样片窗口还是普通详情。"""
    mod = _mod()
    records = [
        {"id": "a1", "job_id": "j1", "status": "succeeded", "title": "样片",
         "request": {"values": {"draft": "1"}}, "workspace_id": "ws-x"},
        {"id": "a2", "job_id": "j2", "status": "succeeded", "title": "普通",
         "request": {"values": {"resolution": "720p"}}, "workspace_id": "ws-x"},
    ]
    with mock.patch.object(mod, "read_activity_log", return_value=records):
        payload = mod.activity_list(ws_id="ws-x")
    by_id = {item["id"]: item for item in payload["records"]}
    assert by_id["a1"]["draft"] is True, "样片记录必须被标出来"
    assert by_id["a2"]["draft"] is False
