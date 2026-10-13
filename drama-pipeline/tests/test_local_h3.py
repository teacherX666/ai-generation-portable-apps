"""本地 MiniMax H3 出片链路的单元测试（stdlib unittest，零网络）。

覆盖：镜头解析容错、参数推导、报错人话化、提交/同步编排、参考图落盘复用。
"""
from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parent
for _p in (str(APP_ROOT), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipeline import generator as G  # noqa: E402
from pipeline import local_h3 as H  # noqa: E402
from pipeline import shots as S  # noqa: E402
from pipeline.store import RunStore  # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode("ascii")
PNG_URL = "data:image/png;base64," + PNG


class TestShotParser(unittest.TestCase):
    def test_markdown_headings(self):
        text = (
            "# 08 视频提示词\n\n"
            "## 镜 1\n时长：4 秒\n景别：中景\n主体与动作：人物抬头微笑\n\n"
            "## 镜 2\n时长：6 秒\n主体与动作：人物转身走出画面\n"
        )
        shots, ok = S.parse_shots(text)
        self.assertTrue(ok)
        self.assertEqual([s.index for s in shots], [1, 2])
        self.assertEqual(shots[0].seconds, 4.0)
        self.assertEqual(shots[1].seconds, 6.0)
        self.assertIn("抬头微笑", shots[0].prompt)

    def test_bold_and_numbered_forms(self):
        for text in (
            "**镜头 1**\n内容 A 足够长足够长\n\n**镜头 2**\n内容 B 足够长足够长\n",
            "### Shot 1\ncontent A long enough\n\n### Shot 2\ncontent B long enough\n",
            "镜 1：\n内容 A 足够长足够长\n\n镜 2：\n内容 B 足够长足够长\n",
            "第 1 镜\n内容 A 足够长足够长\n\n第 2 镜\n内容 B 足够长足够长\n",
            "1. 镜头\n内容 A 足够长足够长\n\n2. 镜头\n内容 B 足够长足够长\n",
        ):
            shots, ok = S.parse_shots(text)
            self.assertTrue(ok, text[:20])
            self.assertEqual(len(shots), 2, text[:20])

    def test_prefers_h3_section_over_seedance(self):
        text = (
            "## 镜 1\n"
            "时长：4 秒\n"
            "Seedance 2.5 口径：written for seedance natural language here\n"
            "MiniMax H3 口径：<Subject 1> walks in, camera pushes slowly\n"
        )
        shots, ok = S.parse_shots(text)
        self.assertTrue(ok)
        self.assertIn("<Subject 1>", shots[0].prompt)

    def test_fallback_when_no_structure(self):
        shots, ok = S.parse_shots("这是一段没有任何镜号结构的整篇提示词，用来兜底。")
        self.assertFalse(ok)
        self.assertEqual(len(shots), 1)
        self.assertIn("兜底", shots[0].prompt)

    def test_empty_text(self):
        shots, ok = S.parse_shots("   ")
        self.assertEqual(shots, [])
        self.assertFalse(ok)

    def test_duration_clamped(self):
        shots, _ = S.parse_shots("## 镜 1\n时长：999 秒\n正文内容足够长足够长\n")
        self.assertLessEqual(shots[0].seconds, S.MAX_SHOT_SECONDS)

    def test_refs_extracted(self):
        shots, _ = S.parse_shots(
            "## 镜 1\n引用素材：@图片1 与 <Picture 3>，主体 <Subject 2>\n正文足够长足够长\n"
        )
        self.assertEqual(shots[0].refs, [1, 2, 3])

    def test_duplicate_numbers_get_unique_index(self):
        shots, ok = S.parse_shots("## 镜 1\nAAAAAAAAAA\n\n## 镜 1\nBBBBBBBBBB\n")
        self.assertTrue(ok)
        self.assertEqual(len({s.index for s in shots}), len(shots))

    def test_seconds_to_frames_hits_17k_plus_5(self):
        for seconds in (1, 4, 6, 8, 12):
            frames = S.seconds_to_frames(seconds, 24)
            self.assertEqual(frames % 17, 5 % 17, f"{seconds}s -> {frames}")
            self.assertGreaterEqual(frames, 5)


# ── 真实产物形态（2026-10-08 一次真机 08 阶段输出，已删减） ──────────────

REAL_SAMPLE = """# 08-video-prompts.md

## 1. 分镜总表（4 秒 · 单段 · 3 镜）

| 镜号 | 时长 | 景别与机位 |
|---|---|---|
| SH01 | 0.0–1.4s | 中近景 |
| SH02 | 1.4–2.8s | 特写 |
| SH03 | 2.8–4.0s | 中景 |

## 2. Seedance 2.5 口径 · 逐镜提示词

### SH01（0.0–1.4s）

现代都市悬疑短剧，真人写实电影质感……（Seedance 散文体）

### SH02（1.4–2.8s）

现代都市悬疑短剧……（Seedance 散文体）

### SH03（2.8–4.0s）

现代都市悬疑短剧……（Seedance 散文体）

## 3. MiniMax H3 口径 · 逐镜提示词

### 图片上传顺序

- @图片1：CH01 林小满主参考
- @图片2：SC01 便利店主参考

### SH01（0.0–1.4s）

```text
subject_definitions:
<Subject 1>：林小满（Lin Xiaoman），22 岁女性……
<Subject 2>：深夜便利店……

summary:
1.4 秒一镜到底，场景是深夜便利店收银台内侧。

detailed_description:
[一镜到底] 0–1.4s，机位在收银台内侧……

Negative: no other people, no text,
```

### SH02（1.4–2.8s）

```text
subject_definitions:
<Subject 3>：监控显示屏……

summary:
1.4 秒一镜到底，监控屏特写。

detailed_description:
[一镜到底] 1.4–2.8s，屏幕内右上格站着一个人形……

Negative: no other people,
```

### SH03（2.8–4.0s）

```text
subject_definitions:
<Subject 1>：林小满……

summary:
1.2 秒一镜到底，她猛地转身。

detailed_description:
[一镜到底] 2.8–4.0s，越肩朝货架通道拍摄……

Negative: no other people,
```

## 4. 素材准备清单（补图用）

- 林小满三视图
"""


class TestRealOutputFormat(unittest.TestCase):
    def test_three_shots_not_six(self):
        """同一镜在 Seedance 节与 H3 节各出现一次 —— 不分节就会解析成 6 个。"""
        shots, ok = S.parse_shots(REAL_SAMPLE)
        self.assertTrue(ok)
        self.assertEqual([s.index for s in shots], [1, 2, 3])

    def test_duration_from_range_in_heading(self):
        shots, _ = S.parse_shots(REAL_SAMPLE)
        self.assertEqual([s.seconds for s in shots], [1.4, 1.4, 1.2])

    def test_prompt_comes_from_h3_section_and_fence_stripped(self):
        shots, _ = S.parse_shots(REAL_SAMPLE)
        first = shots[0].prompt
        self.assertIn("subject_definitions:", first)
        self.assertIn("detailed_description:", first)
        self.assertNotIn("```", first)
        self.assertNotIn("Seedance 散文体", first)

    def test_upload_order_heading_ignored(self):
        shots, _ = S.parse_shots(REAL_SAMPLE)
        self.assertNotIn("图片上传顺序", [s.title for s in shots])

    def test_refs_come_from_subject_tags(self):
        shots, _ = S.parse_shots(REAL_SAMPLE)
        self.assertEqual(shots[0].refs, [1, 2])
        self.assertEqual(shots[1].refs, [3])

    def test_h3_section_chosen_even_if_seedance_comes_last(self):
        text = (
            "## 3. MiniMax H3 口径 · 逐镜提示词\n\n"
            "### SH01（0–2.0s）\n\n```text\ndetailed_description:\nH3 正文内容\n```\n\n"
            "## 9. 附录 · Seedance 备注\n\n"
            "### SH01（0–9.9s）\n\n这段不该被选中\n"
        )
        shots, ok = S.parse_shots(text)
        self.assertTrue(ok)
        self.assertIn("H3 正文内容", shots[0].prompt)
        self.assertEqual(shots[0].seconds, 2.0)

    def test_offsets_in_table_do_not_create_shots(self):
        """分镜总表里的 | SH01 | 是表格行，不是标题，不该被当成镜头块。"""
        text = "| SH01 | 0.0–1.4s |\n| SH02 | 1.4–2.8s |\n" + REAL_SAMPLE
        shots, ok = S.parse_shots(text)
        self.assertTrue(ok)
        self.assertEqual(len(shots), 3)


class TestLocalH3Params(unittest.TestCase):
    def test_normalize_mode_by_image_count(self):
        self.assertEqual(H.normalize_mode("", 0), "t2v")
        self.assertEqual(H.normalize_mode("", 1), "i2v")
        self.assertEqual(H.normalize_mode("", 2), "flf2v")
        self.assertEqual(H.normalize_mode("", 3), "ref2v")
        self.assertEqual(H.normalize_mode("", 9), "ref2v")

    def test_explicit_mode_downgraded_when_refs_missing(self):
        self.assertEqual(H.normalize_mode("i2v", 0), "t2v")
        self.assertEqual(H.normalize_mode("flf2v", 1), "i2v")
        self.assertEqual(H.normalize_mode("flf2v", 0), "t2v")
        self.assertEqual(H.normalize_mode("ref2v", 0), "ref2v")

    def test_values_are_all_strings_and_provider_locked(self):
        values = H.build_values(prompt="一个人回头", mode="t2v")
        self.assertTrue(all(isinstance(v, str) for v in values.values()))
        self.assertEqual(values["provider"], H.LOCAL_PROVIDER)
        self.assertEqual(values["model_kind"], H.H3_MODEL_KIND)
        self.assertEqual(values["steps"], "8")
        self.assertEqual(values["cfg"], "1.0")
        self.assertEqual(values["attention_mode"], "sageattn")
        self.assertEqual(values["h3_second_stage_enabled"], "true")

    def test_duration_and_ratio_presets(self):
        values = H.build_values(prompt="x", mode="t2v", duration_id="12s",
                                ratio="16:9 (Widescreen)")
        self.assertEqual(values["num_frames"], "288")
        self.assertEqual(values["h3_aspect_ratio"], "16:9 (Widescreen)")

    def test_provider_cannot_be_overridden(self):
        values = H.build_values(prompt="x", mode="t2v",
                                overrides={"provider": "ark", "model_kind": "gpt", "steps": 4})
        self.assertEqual(values["provider"], H.LOCAL_PROVIDER)
        self.assertEqual(values["model_kind"], H.H3_MODEL_KIND)
        self.assertEqual(values["steps"], "4")   # 允许调加速步数

    def test_empty_prompt_rejected(self):
        with self.assertRaises(H.LocalH3Error):
            H.build_values(prompt="   ", mode="t2v")

    def test_build_files_only_accepts_data_urls(self):
        files = H.build_files([
            {"data_url": PNG_URL, "filename": "a.png"},
            {"url": "https://example.com/b.png"},      # AI Port 其实不认 url，必须丢掉
            {"data_url": "not-a-data-url"},
        ])
        self.assertEqual(list(files), ["h3_image_0"])
        self.assertEqual(files["h3_image_0"]["filename"], "a.png")

    def test_build_files_caps_at_nine(self):
        files = H.build_files([{"data_url": PNG_URL}] * 20)
        self.assertEqual(len(files), H.MAX_REF_IMAGES)

    def test_request_key_is_stable_and_scoped(self):
        a = H.request_key("run1", 1, "h")
        self.assertEqual(a, H.request_key("run1", 1, "h"))
        self.assertNotEqual(a, H.request_key("run1", 2, "h"))
        self.assertNotEqual(a, H.request_key("run2", 1, "h"))


class TestHumanizeError(unittest.TestCase):
    def test_socks_proxy_is_explained(self):
        msg = H.humanize_error(
            "RuntimeError: ComfyUI 未启动 (http://127.0.0.1:8188): "
            "<urlopen error unknown url type: socks4>"
        )
        self.assertIn("代理", msg)
        self.assertNotIn("Traceback", msg)

    def test_comfyui_down(self):
        self.assertIn("ComfyUI", H.humanize_error("Connection refused"))

    def test_oom_mentions_downgrade(self):
        self.assertIn("显存", H.humanize_error("torch.cuda.OutOfMemoryError: CUDA out of memory"))

    def test_traceback_noise_stripped(self):
        raw = "Traceback (most recent call last):\n  File \"x\", line 1\nValueError: 坏参数"
        self.assertEqual(H.humanize_error(raw), "ValueError: 坏参数")


class _BaseGen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.tmp.name))
        self.gen = G.LocalGenerator(self.store, interval=3600)
        self.run = self.store.create(
            topic="t", mode="short_drama", stage_ids=["video_prompts"]
        )
        self.store.write_artifact(
            self.run["id"],
            "08-video-prompts.md",
            "## 镜 1\n时长：4 秒\n主体：人物抬头微笑，镜头缓慢推近\n\n"
            "## 镜 2\n时长：6 秒\n主体：人物转身走出画面，跟拍\n",
        )
        self.store.mark_stage(self.run["id"], "video_prompts", status="done", chars=80)

    def tearDown(self):
        self.tmp.cleanup()


class TestGeneratorSubmit(_BaseGen):
    def test_submit_uses_parsed_prompt_and_locks_local(self):
        calls = {}

        def fake_submit(**kwargs):
            calls.update(kwargs)
            return {"job_id": "job-1", "deduplicated": False, "values": kwargs.get("values") or {}, "images": 0}

        with mock.patch.object(G.local_h3, "submit", side_effect=fake_submit):
            result = self.gen.submit(self.run["id"], 2, {})

        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(calls["mode"], "t2v")
        self.assertIn("转身走出画面", calls["prompt"])
        self.assertTrue(calls["request_id"].startswith("drama-"))
        view = self.gen.shots_view(self.run["id"])
        shot2 = [s for s in view["shots"] if s["index"] == 2][0]
        self.assertEqual(shot2["status"], "submitted")
        self.assertEqual(shot2["job_id"], "job-1")

    def test_submit_honours_prompt_and_mode_override(self):
        calls = {}
        with mock.patch.object(G.local_h3, "submit",
                               side_effect=lambda **kw: (calls.update(kw), {"job_id": "j", "deduplicated": False, "values": {}, "images": 0})[1]):
            self.gen.submit(self.run["id"], 1, {"prompt": "自定义提示词内容", "mode": "ref2v"})
        self.assertEqual(calls["prompt"], "自定义提示词内容")
        self.assertEqual(calls["mode"], "ref2v")

    def test_submit_rejects_unknown_shot(self):
        with mock.patch.object(G.local_h3, "submit", side_effect=AssertionError("不该被调用")):
            with self.assertRaises(H.LocalH3Error):
                self.gen.submit(self.run["id"], 99, {"prompt": ""})
        # 没有提示词也不该炸出未捕获异常
        with mock.patch.object(G.local_h3, "submit", return_value={"job_id": "j", "deduplicated": False, "values": {}, "images": 0}):
            self.gen.submit(self.run["id"], 99, {"prompt": "手动给的"})

    def test_submit_failure_is_recorded(self):
        with mock.patch.object(G.local_h3, "submit", side_effect=H.LocalH3Error("本地网关连不上")):
            with self.assertRaises(H.LocalH3Error):
                self.gen.submit(self.run["id"], 1, {})
        entry = self.store.read_shots(self.run["id"])["1"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("连不上", entry["error"])

    def test_reference_images_persist_and_reuse(self):
        images = [{"data_url": PNG_URL, "filename": "a.png"}]
        with mock.patch.object(G.local_h3, "submit",
                               side_effect=lambda **kw: {"job_id": "j1", "deduplicated": False, "values": {}, "images": len(kw.get("images") or [])}) as m:
            self.gen.submit(self.run["id"], 1, {"images": images})
        first = m.call_args.kwargs
        self.assertEqual(len(first["images"]), 1)
        self.assertEqual(first["mode"], "i2v")          # 1 张图 → 首帧图生视频
        # 第二次不带图，应复用落盘的参考图
        with mock.patch.object(G.local_h3, "submit",
                               side_effect=lambda **kw: {"job_id": "j2", "deduplicated": False, "values": {}, "images": len(kw.get("images") or [])}) as m2:
            self.gen.submit(self.run["id"], 1, {})
        self.assertEqual(len(m2.call_args.kwargs["images"]), 1)
        self.assertTrue((self.store.run_dir(self.run["id"]) / "shots" / "shot-01-ref0.bin").exists())

    def test_nearest_duration_mapping(self):
        self.assertEqual(self.gen._nearest_duration(4.0), "4s")
        self.assertEqual(self.gen._nearest_duration(5.5), "6s")
        self.assertEqual(self.gen._nearest_duration(11.9), "12s")


class TestGeneratorSync(_BaseGen):
    def _seed_job(self, job_id="job-x"):
        with mock.patch.object(G.local_h3, "submit",
                               return_value={"job_id": job_id, "deduplicated": False, "values": {}, "images": 0}):
            self.gen.submit(self.run["id"], 1, {})

    def test_sync_downloads_video_and_marks_succeeded(self):
        self._seed_job()
        job = {
            "status": "succeeded", "done": True, "ok": True, "events": [], "error": "",
            "raw": {"results": [{"ok": True, "download_url": "/api/download/tok",
                                "filename": "out.mp4", "seed": 42,
                                "local_path": "E:/x/out.mp4"}]},
        }

        def fake_download(url, dest, timeout=600):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"mp4-bytes")
            return dest.stat().st_size

        with mock.patch.object(G.local_h3, "job_status", return_value=job), \
             mock.patch.object(G.local_h3, "download_to", side_effect=fake_download):
            out = self.gen.sync_run(self.run["id"])

        self.assertEqual(out["changed"], 1)
        entry = self.store.read_shots(self.run["id"])["1"]
        self.assertEqual(entry["status"], "succeeded")
        self.assertEqual(entry["seed"], "42")
        self.assertEqual(entry["bytes"], 9)
        view = self.gen.shots_view(self.run["id"])
        self.assertTrue(view["shots"][0]["has_video"])

    def test_sync_records_failure_with_human_message(self):
        self._seed_job()
        job = {"status": "failed", "done": True, "ok": False, "events": [],
               "error": "显存不够", "raw": {}}
        with mock.patch.object(G.local_h3, "job_status", return_value=job):
            self.gen.sync_run(self.run["id"])
        entry = self.store.read_shots(self.run["id"])["1"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("显存", entry["error"])

    def test_sync_keeps_running_state(self):
        self._seed_job()
        job = {"status": "running", "done": False, "ok": False, "events": [], "error": "", "raw": {}}
        with mock.patch.object(G.local_h3, "job_status", return_value=job):
            self.gen.sync_run(self.run["id"])
        entry = self.store.read_shots(self.run["id"])["1"]
        self.assertEqual(entry["status"], "running")
        self.assertIn(1, self.store.shots_in_flight(self.run["id"]))

    def test_download_failure_does_not_lose_state(self):
        self._seed_job()
        job = {"status": "succeeded", "done": True, "ok": True, "events": [], "error": "",
               "raw": {"results": [{"ok": True, "download_url": "/api/download/tok"}]}}
        with mock.patch.object(G.local_h3, "job_status", return_value=job), \
             mock.patch.object(G.local_h3, "download_to", side_effect=H.LocalH3Error("下载成片失败：HTTP 500")):
            self.gen.sync_run(self.run["id"])
        entry = self.store.read_shots(self.run["id"])["1"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("下载成片失败", entry["error"])

    def test_cancel_requires_active_job(self):
        self.assertEqual(self.gen.cancel(self.run["id"], 1)["ok"], False)
        self._seed_job()
        with mock.patch.object(G.local_h3, "cancel", return_value={"ok": True}):
            self.assertEqual(self.gen.cancel(self.run["id"], 1)["ok"], True)

    def test_runs_with_shots_in_flight(self):
        self.assertEqual(self.store.runs_with_shots_in_flight(), [])
        self._seed_job()
        self.assertEqual(self.store.runs_with_shots_in_flight(), [self.run["id"]])


class TestShotStoreSafety(_BaseGen):
    def test_shot_paths_reject_bad_index(self):
        for bad in ("0", "-1", "abc", "1000", "", None):
            with self.assertRaises(ValueError):
                self.store.shot_video_path(self.run["id"], bad)

    def test_shot_video_path_inside_run_dir(self):
        path = self.store.shot_video_path(self.run["id"], 3)
        self.assertTrue(str(path).startswith(str(self.store.run_dir(self.run["id"]))))
        self.assertEqual(path.name, "shot-03.mp4")

    def test_update_shot_is_read_modify_write(self):
        self.store.update_shot(self.run["id"], 1, status="submitted", job_id="a")
        self.store.update_shot(self.run["id"], 1, elapsed=12)
        self.store.update_shot(self.run["id"], 2, status="idle")
        data = self.store.read_shots(self.run["id"])
        self.assertEqual(data["1"]["status"], "submitted")
        self.assertEqual(data["1"]["job_id"], "a")
        self.assertEqual(data["1"]["elapsed"], 12)
        self.assertEqual(data["2"]["status"], "idle")


if __name__ == "__main__":
    unittest.main(verbosity=2)
