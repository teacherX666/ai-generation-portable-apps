from __future__ import annotations

import importlib.util
import sys
import tempfile
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


class Field:
    def __init__(self, value: str = "", filename: str | None = None):
        self.value = value
        self.filename = filename


class MediaRequirementGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seedance = load_module(
            "seedance_media_guard_test", ROOT / "seedance" / "app.py"
        )

    def test_cloud_prompt_only_is_text_to_video(self):
        form = {
            "provider": Field("volcengine"),
            "model": Field("doubao-seedance-2-0-260128"),
            "prompt": Field("a small robot walking through a market"),
            "duration": Field("8"),
            "ratio": Field("16:9"),
            "resolution": Field("720p"),
            "generate_audio": Field("false"),
            "watermark": Field("false"),
            "return_last_frame": Field("false"),
            "web_search": Field("false"),
            "seed": Field(""),
            "vary_seed": Field("false"),
        }
        payload = self.seedance.build_payload(
            form, "unused-key", "https://ark.example/api/v3", 0, "localhost"
        )

        self.assertEqual(
            payload["content"],
            [{"type": "text", "text": "a small robot walking through a market"}],
        )

    def test_local_h3_empty_media_submits_t2v(self):
        captured = {}

        def capture_submit(method, url, api_key, payload, timeout):
            captured.update(
                {"method": method, "url": url, "payload": payload, "timeout": timeout}
            )
            raise RuntimeError("stop after capturing local submit")

        form = {
            "provider": Field("comfyui_local"),
            "model": Field("minimax_h3_all_reference"),
            "prompt": Field("a red panda walking through snow"),
            "duration": Field("8"),
            "ratio": Field("16:9"),
            "resolution": Field("720p"),
            "seed": Field(""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.seedance.STATE_DIR = Path(tmp)
            with mock.patch.object(self.seedance, "request_json", side_effect=capture_submit), \
                    mock.patch.object(self.seedance, "add_event"):
                with self.assertRaisesRegex(RuntimeError, "stop after capturing local submit"):
                    self.seedance._run_local_video(
                        "job-local",
                        0,
                        form,
                        {"timeout": 1, "poll_interval": 2, "output_dir": tmp},
                        {},
                        "localhost",
                        "http://127.0.0.1:8801",
                    )

        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/api/video_local/jobs/json"))
        self.assertEqual(captured["payload"]["values"]["h3_task_mode"], "t2v")
        self.assertEqual(captured["payload"]["files"], {})

    def test_seedance_frontend_does_not_require_reference_media(self):
        source = (ROOT / "seedance" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const usesLocalH3 =", source)
        self.assertIn("if (!usesLocalH3) {", source)
        self.assertIn("await validateReferenceVideoDurations();", source)
        self.assertNotIn("if (this.taskMode === 'reference' &&", source)
        self.assertNotIn("请先上传首帧、参考图或参考视频", source)

    def test_nano_banana_keeps_reference_guard_for_qwen_edit_model(self):
        source = (ROOT / "nano-banana" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn(
            "selectedProvider === 'comfyui_local' && selectedModel === 'qwen2511'",
            source,
        )

    def test_infinite_canvas_video_reference_ports_are_optional(self):
        source = (ROOT / "infinite-canvas" / "translate.py").read_text(encoding="utf-8")

        self.assertIn('"port_id": "first_frame", "media_type": "image", "min_items": 0', source)
        self.assertIn('"port_id": "reference_images", "media_type": "image"', source)
        self.assertIn('"port_id": "reference_video", "media_type": "video"', source)
        self.assertNotIn('"port_id": "reference_images", "media_type": "image", "min_items": 1', source)

    def test_feishu_agent_seedance_supports_empty_video_references(self):
        source = (
            ROOT
            / "feishu-generation-agent"
            / "src"
            / "feishu_generation_agent"
            / "integrations"
            / "seedance.py"
        ).read_text(encoding="utf-8")

        self.assertIn("if not references and not assets:", source)
        self.assertIn("Seedance 的文生视频模式", source)

    def test_portrait_local_ref_requirement_is_an_explicit_ref2v_contract(self):
        source = (ROOT / "volcengine-portrait" / "app.py").read_text(encoding="utf-8")

        self.assertIn("local model requires at least one reference asset or uploaded file", source)
        self.assertIn('"h3_task_mode": "ref2v"', source)

    def test_dreamina_text2video_is_prompt_only(self):
        dreamina = load_module(
            "dreamina_media_guard_test", ROOT / "dreamina" / "app.py"
        )
        args = dreamina.Handler.build_cli_args(
            None,
            "text2video",
            {
                "prompt": "a lighthouse at sunrise",
                "duration": "5",
                "ratio": "16:9",
                "video_resolution": "720p",
                "model_version": "seedance2.0fast_vip",
            },
            {},
            {},
        )

        self.assertIn("--prompt=a lighthouse at sunrise", args)
        self.assertNotIn("--image", args)
        self.assertNotIn("--first", args)


if __name__ == "__main__":
    unittest.main()