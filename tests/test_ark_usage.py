"""`shared/ark_usage.extract_tokens` 的口径测试。

真实响应形状来自 2026-10-12 的零成本回查：
- 视频任务 GET /contents/generations/tasks/{id} → {"usage": {"completion_tokens": 540900, "total_tokens": 540900}}
- 图片 POST /images/generations → {"usage": {"input_images": 1, "generated_images": 8,
                                            "output_tokens": 23107, "total_tokens": 23107}}
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "shared" / "ark_usage.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ark_usage_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ExtractTokensTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def test_video_usage_shape(self):
        payload = {"id": "cgt-x", "status": "succeeded",
                   "usage": {"completion_tokens": 540900, "total_tokens": 540900}}
        self.assertEqual(self.mod.extract_tokens(payload), 540900)

    def test_image_usage_shape(self):
        payload = {"data": [{"url": "x"}],
                   "usage": {"input_images": 1, "generated_images": 8,
                             "output_tokens": 23107, "total_tokens": 23107}}
        self.assertEqual(self.mod.extract_tokens(payload), 23107)

    def test_prefers_total_over_completion(self):
        payload = {"usage": {"completion_tokens": 10, "total_tokens": 42}}
        self.assertEqual(self.mod.extract_tokens(payload), 42)

    def test_falls_back_to_output_tokens(self):
        payload = {"usage": {"output_tokens": 777}}
        self.assertEqual(self.mod.extract_tokens(payload), 777)

    def test_generated_images_is_not_a_token_count(self):
        """图片张数不是 token —— 取错了会让「8 张图」变成「8 token」。"""
        payload = {"usage": {"input_images": 3, "generated_images": 8}}
        self.assertEqual(self.mod.extract_tokens(payload), 0)

    def test_missing_usage_is_zero(self):
        for payload in ({}, {"status": "running"}, {"usage": None}, None, [], "nope"):
            self.assertEqual(self.mod.extract_tokens(payload), 0, msg=repr(payload))

    def test_running_task_has_null_usage(self):
        """实测：running 的任务 usage 是 null（列表 API 里就是这样）。"""
        self.assertEqual(self.mod.extract_tokens({"status": "running", "usage": None}), 0)

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(self.mod.extract_tokens({"usage": {"total_tokens": "12345"}}), 12345)

    def test_booleans_and_negatives_are_rejected(self):
        self.assertEqual(self.mod.extract_tokens({"usage": {"total_tokens": True}}), 0)
        self.assertEqual(self.mod.extract_tokens({"usage": {"total_tokens": -5}}), 0)

    def test_zero_tokens_is_zero(self):
        self.assertEqual(self.mod.extract_tokens({"usage": {"total_tokens": 0}}), 0)


if __name__ == "__main__":
    unittest.main()
