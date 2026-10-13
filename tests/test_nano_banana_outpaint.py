"""扩图（outpaint）模式的契约测试。

背景（2026-10-10）：图片生成模块的「模式」以前只影响云端走哪条 API；本地通道的请求体是
白名单，`flux2_mode` / `zimage_mode` 从来没下发出去 —— 实测选「本地 Z-Image + 参考图 +
模式=img2img」时 AI Port 收到的仍是 `zimage_t2i_api`（参考图被忽略）。同一处也是扩图模式
能不能生效的前提，所以这里把两件事一起钉住。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OutpaintModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nano = load_module("nano_outpaint_test", ROOT / "nano-banana" / "app.py")

    # ---- 纯逻辑 ----

    def test_cloud_outpaint_prompt_uses_chosen_ratio_not_hardcoded_16_9(self):
        prompt = self.nano.outpaint_prompt("一只猫", "3:4", include_ratio=True)
        self.assertIn("一只猫", prompt)
        self.assertIn("3:4", prompt)
        self.assertNotIn("16:9", prompt)

    def test_cloud_outpaint_prompt_leaves_ratio_open_on_auto(self):
        prompt = self.nano.outpaint_prompt("一只猫", "auto", include_ratio=True)
        self.assertIn("画面内容自然延展决定", prompt)
        self.assertNotIn("16:9", prompt)

    def test_local_outpaint_prompt_never_mentions_ratio(self):
        prompt = self.nano.outpaint_prompt("一只猫", "16:9", include_ratio=False)
        self.assertNotIn("16:9", prompt)
        self.assertIn("扩图", prompt)

    def test_user_supplied_outpaint_wording_is_kept_as_is(self):
        original = "请对这张图做扩图，向左右延展背景"
        self.assertEqual(
            self.nano.outpaint_prompt(original, "16:9", include_ratio=True), original
        )

    def test_local_mode_values_outpaint_and_passthrough(self):
        # 扩图 → 各模型自己的真扩图分支
        self.assertEqual(
            self.nano.local_mode_values("flux2_klein_allinone", "outpaint", True),
            {"flux2_mode": "outpaint"},
        )
        self.assertEqual(
            self.nano.local_mode_values("zimage_multifunction", "outpaint", True),
            {"zimage_mode": "outpaint"},
        )
        # 非扩图：Klein 保持 auto（AI Port 按参考图数量自己挑分支）
        self.assertEqual(
            self.nano.local_mode_values("flux2_klein_allinone", "img2img", True),
            {"flux2_mode": "auto"},
        )
        # 非扩图：Z-Image 按界面上的模式如实映射（这正是以前漏掉的那一步）
        self.assertEqual(
            self.nano.local_mode_values("zimage_multifunction", "img2img", True),
            {"zimage_mode": "img2img"},
        )
        self.assertEqual(
            self.nano.local_mode_values("zimage_multifunction", "text2img", False),
            {"zimage_mode": "t2i"},
        )
        # 没有扩图分支的本地模型不下发模式字段
        self.assertEqual(self.nano.local_mode_values("qwen2511", "outpaint", True), {})

    def test_local_outpaint_padding_defaults_and_clamps(self):
        padding = self.nano.local_outpaint_padding({}, "zimage_multifunction")
        self.assertEqual(
            padding,
            {
                "outpaint_left": "256",
                "outpaint_top": "0",
                "outpaint_right": "256",
                "outpaint_bottom": "0",
                "outpaint_feather": "50",
            },
        )
        padding = self.nano.local_outpaint_padding(
            {"outpaint_left": "-5", "outpaint_right": "99999", "outpaint_feather": "abc"},
            "flux2_klein_allinone",
        )
        self.assertEqual(padding["outpaint_left"], "0")
        self.assertEqual(padding["outpaint_right"], "4096")
        self.assertEqual(padding["outpaint_feather"], "50")

    def test_local_auto_candidate_prefers_zimage_for_outpaint(self):
        self.assertEqual(
            self.nano._local_auto_candidates("auto", 1, "outpaint"),
            ["zimage_multifunction"],
        )
        # 非扩图时保持原来的链路（1 张参考图 → qwen2511）
        self.assertEqual(self.nano._local_auto_candidates("auto", 1, "img2img"), ["qwen2511"])

    # ---- 端到端（打桩拦截出站请求） ----

    def _run_one_local(self, values, files, captured):
        job_id = "job-outpaint-test"
        self.nano.JOBS[job_id] = {"events": [], "results": []}

        def fake_request_json(method, url, api_key, payload=None, timeout=None, **kwargs):
            captured.append({"method": method, "url": url, "payload": payload})
            raise RuntimeError("stop-after-capture")

        with mock.patch.object(self.nano, "local_gateway_available", lambda *a, **k: True), \
             mock.patch.object(self.nano, "request_json", fake_request_json), \
             mock.patch.dict(self.nano.JOBS, {job_id: {"events": [], "results": []}}):
            with self.assertRaises(RuntimeError):
                self.nano.run_one(job_id, 1, values, files)
        return captured[0]

    def test_local_outpaint_payload_carries_mode_and_padding(self):
        values = {
            "api_key": "",
            "provider": "comfyui_local",
            "model": "zimage_multifunction",
            "mode": "outpaint",
            "prompt": "把背景延展出去",
            "aspect_ratio": "auto",
            "image_size": "1K",
            "outpaint_left": "300",
            "outpaint_right": "300",
            "outpaint_feather": "60",
        }
        captured = []
        call = self._run_one_local(values, {"image_1": ("a.png", b"blob")}, captured)
        payload = call["payload"]["values"]
        self.assertEqual(payload["model_kind"], "zimage_multifunction")
        self.assertEqual(payload["zimage_mode"], "outpaint")
        self.assertEqual(payload["outpaint_left"], "300")
        self.assertEqual(payload["outpaint_right"], "300")
        self.assertEqual(payload["outpaint_top"], "0")
        self.assertEqual(payload["outpaint_feather"], "60")
        # 本地扩图不该把比例写进提示词（画幅由扩展像素决定）
        self.assertNotIn("画幅", payload["prompt"])

    def test_local_img2img_now_passes_zimage_mode(self):
        values = {
            "api_key": "",
            "provider": "comfyui_local",
            "model": "zimage_multifunction",
            "mode": "img2img",
            "prompt": "洗图",
            "aspect_ratio": "auto",
            "image_size": "1K",
        }
        captured = []
        call = self._run_one_local(values, {"image_1": ("a.png", b"blob")}, captured)
        self.assertEqual(call["payload"]["values"]["zimage_mode"], "img2img")

    def test_cloud_outpaint_prompt_carries_ratio(self):
        job_id = "job-cloud-outpaint-test"
        captured = {}

        def fake_multipart_post(url, api_key, fields, files_payload):
            captured.update({"url": url, "fields": fields, "files": files_payload})
            raise RuntimeError("stop-after-capture")

        values = {
            "api_key": "sk-test",
            "provider": "t8star",
            "base_url": "https://ai.t8star.org",
            "model": "gpt-image-2.5-flare-2k",
            "mode": "outpaint",
            "prompt": "把背景延展出去",
            "aspect_ratio": "3:4",
            "image_size": "2K",
        }
        with mock.patch.dict(self.nano.JOBS, {job_id: {"events": [], "results": []}}), \
             mock.patch.object(self.nano, "multipart_post", fake_multipart_post):
            with self.assertRaises(RuntimeError):
                self.nano.run_one(job_id, 1, values, {"image_1": ("a.png", b"blob")})
        prompt = captured["fields"]["prompt"]
        self.assertIn("3:4", prompt)
        self.assertNotIn("16:9", prompt)

    def test_outpaint_requires_a_reference_image(self):
        job_id = "job-outpaint-no-ref"
        values = {
            "api_key": "sk-test",
            "provider": "t8star",
            "base_url": "https://ai.t8star.org",
            "model": "gpt-image-2.5-flare-2k",
            "mode": "outpaint",
            "prompt": "扩图",
            "aspect_ratio": "auto",
            "image_size": "2K",
        }
        with mock.patch.dict(self.nano.JOBS, {job_id: {"events": [], "results": []}}):
            with self.assertRaises(ValueError) as caught:
                self.nano.run_one(job_id, 1, values, {})
        self.assertIn("扩图", str(caught.exception))

    def test_local_outpaint_rejects_models_without_outpaint_branch(self):
        job_id = "job-outpaint-bad-model"
        values = {
            "api_key": "",
            "provider": "comfyui_local",
            "model": "klein_true_v3_assets",
            "mode": "outpaint",
            "prompt": "扩图",
            "aspect_ratio": "auto",
            "image_size": "1K",
        }
        with mock.patch.dict(self.nano.JOBS, {job_id: {"events": [], "results": []}}):
            with self.assertRaises(ValueError) as caught:
                self.nano.run_one(job_id, 1, values, {"image_1": ("a.png", b"blob")})
        self.assertIn("Klein", str(caught.exception))

    # ---- 界面/配置契约 ----

    def test_frontend_and_providers_expose_outpaint(self):
        html = (ROOT / "nano-banana" / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "nano-banana" / "static" / "app.js").read_text(encoding="utf-8")
        providers = (ROOT / "nano-banana" / "providers.json").read_text(encoding="utf-8")
        self.assertIn('value="outpaint"', html)
        self.assertIn('name="outpaint_left"', html)
        self.assertIn("outpaint_feather", html)
        self.assertIn("mode === 'outpaint'", html)
        self.assertIn("mode: 'img2img'", app_js)
        # 没有扩图分支的本地模型要禁用「扩图」选项
        self.assertIn('"modes": ["img2img"]', providers)
        self.assertIn("outpaint_left", self.nano.VALUE_FIELDS)
        self.assertIn("outpaint_feather", self.nano.VALUE_FIELDS)


if __name__ == "__main__":
    unittest.main()
