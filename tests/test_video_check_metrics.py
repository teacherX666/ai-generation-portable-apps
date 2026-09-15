"""确定性视频体检的单元测试。

全部用 cv2 合成已知缺陷的视频，不依赖任何真实生成结果、不调用模型、不花钱。

覆盖：正常视频不误报、静止段、黑边、模糊、时长与参数不符、不可解码文件。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


video_check = _load_module("video_check_under_test", ROOT / "portal" / "video_check.py")

FPS = 8
WIDTH = 160
HEIGHT = 120


def _write_video(path: Path, frames: list[np.ndarray], fps: int = FPS) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (WIDTH, HEIGHT))
    assert writer.isOpened(), "cv2 VideoWriter 无法打开，测试环境不支持 mp4v"
    for frame in frames:
        writer.write(frame)
    writer.release()


def _noise_frame(seed: int) -> np.ndarray:
    """高对比度随机噪声：清晰度极高、且每帧都不一样（不会误判静止）。"""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(HEIGHT, WIDTH, 3), dtype=np.uint8)


def _clean_frames(count: int = 24) -> list[np.ndarray]:
    return [_noise_frame(seed) for seed in range(count)]


class VideoCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _analyze(self, frames, name="clip.mp4", expected=None):
        path = self.tmp / name
        _write_video(path, frames)
        return video_check.analyze(str(path), expected=expected)

    def test_clean_video_reports_no_warnings(self):
        result = self._analyze(_clean_frames())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["findings"], [], f"正常视频不应误报，实际: {result['findings']}")
        self.assertTrue(result["clean"])

    def test_static_segment_is_detected(self):
        # 12 帧全同 = 1.5 秒静止，夹在运动画面中间
        frames = _clean_frames(12) + [_noise_frame(999)] * 12 + _clean_frames(12)
        result = self._analyze(frames)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("freeze", kinds, f"应检测出静止段，实际 findings={result['findings']}")

    def test_letterbox_is_detected(self):
        frames = []
        for seed in range(24):
            frame = _noise_frame(seed)
            frame[:18, :] = 0     # 上黑边
            frame[-18:, :] = 0    # 下黑边
            frames.append(frame)
        result = self._analyze(frames)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("letterbox", kinds, f"应检测出黑边，实际 findings={result['findings']}")
        bar = result["metrics"]["letterbox_px"]
        self.assertGreaterEqual(bar["top"], 2)
        self.assertGreaterEqual(bar["bottom"], 2)

    def test_blur_is_detected(self):
        # 先做粗糙色块再重度模糊：结构仍在移动（不会判静止），但细节被抹掉
        frames = []
        for seed in range(24):
            rng = np.random.default_rng(seed)
            coarse = rng.integers(0, 256, size=(HEIGHT // 24, WIDTH // 24, 3), dtype=np.uint8)
            blocky = cv2.resize(coarse, (WIDTH, HEIGHT), interpolation=cv2.INTER_NEAREST)
            frames.append(cv2.GaussianBlur(blocky, (0, 0), 12))
        result = self._analyze(frames)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("blur", kinds, f"应检测出模糊，实际 findings={result['findings']}")

    def test_duration_mismatch_is_detected(self):
        # 24 帧 @8fps = 3 秒，却申报 12 秒
        result = self._analyze(_clean_frames(24), expected={"duration": 12})
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("duration_mismatch", kinds,
                      f"应检测出时长不符，实际 findings={result['findings']}")
        self.assertAlmostEqual(result["metrics"]["duration_seconds"], 3.0, delta=0.4)

    def test_matching_duration_is_not_flagged(self):
        result = self._analyze(_clean_frames(24), expected={"duration": 3})
        kinds = [f["kind"] for f in result["findings"]]
        self.assertNotIn("duration_mismatch", kinds)

    def test_undecodable_file_returns_error(self):
        path = self.tmp / "not-a-video.mp4"
        path.write_bytes(b"this is not a video" * 10)
        result = video_check.analyze(str(path))
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class VideoCheckRouteTests(unittest.TestCase):
    """路由接线检查。

    这里的断言是**源码文本级**的：端点要等 Portal 重启才生效（重启会杀掉所有
    子应用和运行中的任务），所以不能靠"起服务打一发"来验证。文本级断言至少能
    保证接线不会被静默删掉。
    """

    def setUp(self):
        self.source = (ROOT / "portal" / "app.py").read_text(encoding="utf-8")

    def test_route_is_dispatched_to_the_handler(self):
        self.assertIn('"/api/video-check"', self.source)
        self.assertIn("self._platform_video_check(user)", self.source)

    def test_handler_method_exists(self):
        self.assertIn("def _platform_video_check(self, user: dict):", self.source)

    def test_handler_is_reachable_only_after_auth(self):
        # 路由必须落在 _require_auth() 之后的 elif 链里
        auth_index = self.source.index("user = self._require_auth(path)")
        route_index = self.source.index('elif path == "/api/video-check":')
        self.assertGreater(route_index, auth_index)

    def test_cv2_import_failure_cannot_stop_the_portal(self):
        # video_check 依赖 cv2；缺失时必须降级而不是让 Portal 起不来
        self.assertIn("try:\n    import video_check", self.source)
        self.assertIn("video_check = None", self.source)

    def test_input_url_is_whitelisted_like_the_thumbnail_endpoint(self):
        # 复用缩略图端点的路径白名单，避免路径穿越
        self.assertIn('re.fullmatch(r"/[A-Za-z0-9._/-]+", url)', self.source)
        self.assertIn("MAX_VIDEO_CHECK_BYTES", self.source)


if __name__ == "__main__":
    unittest.main()
