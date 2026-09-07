"""错误翻译规则表测试：ark_errors 新条目 / dreamina CLI 表 / nano-banana 中文提示。

数据来源：2026-09 各子应用 activity_log.json 的真实失败样本。
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(mod_path, name):
    spec = importlib.util.spec_from_file_location(name, mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class ArkErrorsTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ark = _load(ROOT / "portal" / "ark_errors.py", "ark_errors_for_test")

    def test_download_image_failure(self):
        zh = self.ark.translate_ark_error(
            "InvalidParameter", "Error while downloading image, error: http status 403")
        self.assertIn("参考图网络地址下载失败", zh)

    def test_existing_entries_still_match(self):
        self.assertIsNotNone(self.ark.translate_ark_error(
            "InvalidParameter", "reference asset is not found"))
        self.assertIsNotNone(self.ark.translate_ark_error(
            "OutputVideoSensitiveContentDetected", "content policy"))

    def test_unknown_returns_none(self):
        self.assertIsNone(self.ark.translate_ark_error("WeirdCode", "whatever"))


class DreaminaTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dm = _load(ROOT / "dreamina" / "app.py", "dreamina_app_for_test")

    def test_tns_check(self):
        self.assertIn("审核", self.dm.translate_cli_error(
            "[1] generation failed: pre-TNS check did not pass"))

    def test_upload_file_missing(self):
        self.assertIn("重新上传", self.dm.translate_cli_error(
            '[1] upload resource "": read file : open : no such file or directory'))

    def test_vip_resolution(self):
        self.assertIn("VIP", self.dm.translate_cli_error(
            "video_resolution 1080p requires model_version seedance2.0_vip"))

    def test_upload_token_502_not_misread_as_login(self):
        zh = self.dm.translate_cli_error("get upload token: get upload token status 502: <html>")
        self.assertIn("502", zh)
        self.assertNotIn("登录", zh)

    def test_balance_still_covered(self):
        self.assertIn("余额", self.dm.translate_cli_error(
            "api error: ret=1006, message=CreditPreDeductNotEnough"))


class NanoTranslateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nb = _load(ROOT / "nano-banana" / "app.py", "nano_app_for_test")

    def test_auth_failed_hint(self):
        zh = self.nb._translate_nano_error("auth_failed", '{"error":{"code":"invalid_request"}}')
        self.assertIn("密钥", zh)

    def test_ark_code_extracted_from_json_message(self):
        # nano-banana 的 client_error 把 OpenAI 风格 JSON 原样放进 message
        zh = self.nb._translate_nano_error(
            "client_error",
            '{"error":{"code":"InvalidEndpointOrModel.NotFound","message":"The model or endpoint g..."}}')
        self.assertIn("模型", zh)

    def test_unknown_returns_empty(self):
        self.assertEqual(self.nb._translate_nano_error("unknown", "something else"), "")


if __name__ == "__main__":
    unittest.main()
