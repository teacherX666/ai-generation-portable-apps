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

    def test_aspect_ratio_not_misreported_as_download_failure(self):
        # 2026-09-21 生产实锤：方舟把「比例校验失败」也包在 "Error while downloading
        # image" 前缀里，泛化条目会误报成「链接失效」，用户换图换链接都修不好。
        msg = ("Error while downloading image, error: expected the aspect ratio to be "
               "between 0.39 and 2.50, but received image with aspect ratio: 2.77 instead "
               "Request id: 0217899611817000e23c04e8b740c94d8512f0597aa5fa6126bf4")
        zh = self.ark.translate_ark_error("InvalidParameter", msg)
        self.assertIn("2.77", zh)
        self.assertIn("竖图", zh)
        self.assertNotIn("下载失败", zh)

    def test_aspect_ratio_too_wide(self):
        msg = ("Error while downloading image, error: expected the aspect ratio to be "
               "between 0.39 and 2.50, but received image with aspect ratio: 0.31 instead")
        zh = self.ark.translate_ark_error("InvalidParameter", msg)
        self.assertIn("横图", zh)
        self.assertIn("0.31", zh)

    def test_real_download_failure_survives_dynamic_branch(self):
        # 动态比例分支不能把真正的下载失败（没有数字）也吃掉
        zh = self.ark.translate_ark_error(
            "InvalidParameter", "Error while downloading image, error: http status 404")
        self.assertIn("下载失败", zh)

    def test_body_helper_translates_aspect_ratio_json(self):
        raw = ('{"error":{"code":"InvalidParameter","message":"Error while downloading image, '
               'error: expected the aspect ratio to be between 0.39 and 2.50, but received '
               'image with aspect ratio: 2.66 instead","param":"image_url","type":"BadRequest"}}')
        self.assertIn("2.66", self.ark.translate_ark_error_body(raw))

    def test_existing_entries_still_match(self):
        self.assertIsNotNone(self.ark.translate_ark_error(
            "InvalidParameter", "reference asset is not found"))
        self.assertIsNotNone(self.ark.translate_ark_error(
            "OutputVideoSensitiveContentDetected", "content policy"))

    def test_unknown_returns_none(self):
        self.assertIsNone(self.ark.translate_ark_error("WeirdCode", "whatever"))

    def test_real_person_privacy_information_translated(self):
        # 2026-09 高频：真人素材未认证 → 方舟 400 PrivacyInformation
        zh = self.ark.translate_ark_error(
            "InputVideoSensitiveContentDetected.PrivacyInformation",
            "The request failed because the input video 'content[1]' may contain real person.",
        )
        self.assertIn("含真人", zh)
        self.assertIn("本地", zh)

    def test_private_info_entry_wins_over_generic_prefix(self):
        # 前缀匹配先命中先返回：PrivacyInformation 必须排在通用条目之前，
        # 否则用户拿到的只是"审核未通过"这种没有下一步动作的泛化提示。
        generic = self.ark.translate_ark_error("InputVideoSensitiveContentDetected", "x")
        specific = self.ark.translate_ark_error(
            "InputVideoSensitiveContentDetected.PrivacyInformation", "x")
        self.assertIsNotNone(generic)
        self.assertIsNotNone(specific)
        self.assertNotEqual(generic, specific)
        self.assertIn("真人认证", specific)

    def test_body_helper_parses_raw_ark_error_json(self):
        # 提交阶段拿不到 (code, message) 对：seedance 的 APIError.message 与
        # volcengine-portrait 的 detail 都是原始 body，必须能解析出来。
        raw = ('{"error":{"code":"InputVideoSensitiveContentDetected.PrivacyInformation",'
               '"message":"The request failed because the input video \'content[3]\' may contain real person.",'
               '"param":"content[3]","type":"BadRequest"}}')
        self.assertIn("含真人", self.ark.translate_ark_error_body(raw))
        # dict 形态（{"error": {...}} 或内层 error dict）同样支持
        self.assertIn("含真人", self.ark.translate_ark_error_body(
            {"error": {"code": "InputVideoSensitiveContentDetected.PrivacyInformation"}}))
        # 未命中/空 body 返回 None，调用方继续展示原文
        self.assertIsNone(self.ark.translate_ark_error_body("not json at all"))
        self.assertIsNone(self.ark.translate_ark_error_body(None))


class SeedanceReferenceImageRatioTests(unittest.TestCase):
    """提交前拦掉方舟必定会拒的参考图比例，并点名是哪张图。

    真实样本：609x1687（高÷宽 2.77）、872x2318（2.66）被方舟 400 拒；方舟报错里
    只有比例数字，不说哪张图、也不给动作，多图任务尤其难查。
    """

    @classmethod
    def setUpClass(cls):
        cls.sd = _load(ROOT / "seedance" / "app.py", "seedance_app_for_ratio_test")

    @staticmethod
    def _image(width, height):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (width, height), (200, 200, 200)).save(buf, format="JPEG")
        return buf.getvalue()

    def test_tall_reference_image_flagged_with_filename_and_size(self):
        problem = self.sd.ark_image_aspect_problem("image-2.jpg", self._image(609, 1687))
        self.assertIn("image-2.jpg", problem)
        self.assertIn("609x1687", problem)
        self.assertIn("竖图", problem)

    def test_wide_reference_image_flagged(self):
        problem = self.sd.ark_image_aspect_problem("wide.png", self._image(2560, 800))
        self.assertIn("横图", problem)

    def test_supported_ratios_pass(self):
        for width, height in ((816, 816), (720, 1280), (1920, 1080), (872, 2180)):
            with self.subTest(size=(width, height)):
                self.assertEqual("", self.sd.ark_image_aspect_problem("ok.png", self._image(width, height)))

    def test_undecodable_blob_is_not_blocked(self):
        # 量不出尺寸就不拦：绝不用本地探测能力不足挡掉一次本来能跑的任务
        self.assertEqual("", self.sd.ark_image_aspect_problem("x.png", b"not an image at all"))

    # --- 自动补边 ------------------------------------------------------

    def _padded(self, width, height, fmt="JPEG"):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (width, height), (180, 120, 90)).save(buf, format=fmt)
        return self.sd.pad_image_to_ark_ratio("ref.jpg", buf.getvalue())

    def test_slightly_tall_image_is_padded_instead_of_rejected(self):
        # 真实生产样本 609x1687（2.77）：只需左右各补 33px，不该让用户去改图
        blob, note = self._padded(609, 1687)
        self.assertTrue(note, "应当补边而不是原样返回")
        self.assertIn("609x1687", note)
        self.assertIn("补边", note)
        self.assertEqual("", self.sd.ark_image_aspect_problem("ref.jpg", blob))
        import io

        from PIL import Image

        with Image.open(io.BytesIO(blob)) as img:
            self.assertEqual((675, 1687), img.size)

    def test_second_real_sample_also_fixed(self):
        # 872x2318（2.66）→ 928x2318
        blob, note = self._padded(872, 2318)
        self.assertTrue(note)
        self.assertEqual("", self.sd.ark_image_aspect_problem("ref.jpg", blob))

    def test_wide_image_padded_top_bottom(self):
        # 2560x900（0.35）只差 99px 高度 → 上下补边；2560x800 要补 25% 就该拒绝
        blob, note = self._padded(2560, 900, fmt="PNG")
        self.assertTrue(note)
        self.assertIn("上下", note)
        self.assertEqual("", self.sd.ark_image_aspect_problem("ref.png", blob))
        wide, wide_note = self._padded(2560, 800, fmt="PNG")
        self.assertEqual("", wide_note)
        self.assertIn("横图", self.sd.ark_image_aspect_problem("ref.png", wide))

    def test_compliant_image_returned_untouched(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (720, 1280), (10, 10, 10)).save(buf, format="JPEG")
        original = buf.getvalue()
        blob, note = self.sd.pad_image_to_ark_ratio("ok.jpg", original)
        self.assertEqual(original, blob)
        self.assertEqual("", note)

    def test_extreme_ratio_left_to_explicit_error(self):
        # 1:5 的长图要补一倍宽度 —— 补边等于凭空造半个画面，交给显式报错
        blob, note = self._padded(400, 2000)
        self.assertEqual("", note)
        self.assertIn("竖图", self.sd.ark_image_aspect_problem("ref.jpg", blob))

    def test_alpha_and_format_are_preserved(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (600, 1800), (255, 0, 0, 0)).save(buf, format="PNG")
        blob, note = self.sd.pad_image_to_ark_ratio("cut.png", buf.getvalue())
        self.assertTrue(note)
        with Image.open(io.BytesIO(blob)) as img:
            self.assertEqual("PNG", img.format)
            self.assertEqual("RGBA", img.mode)

    def test_undecodable_blob_returned_untouched(self):
        blob, note = self.sd.pad_image_to_ark_ratio("x.png", b"not an image")
        self.assertEqual(b"not an image", blob)
        self.assertEqual("", note)


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
