"""drama-pipeline 单元测试（stdlib unittest，无第三方依赖）。

跑法：
    cd ai-generation-portable-apps/drama-pipeline
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parent
for _p in (str(APP_ROOT), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipeline import stages as S  # noqa: E402
from pipeline import runner as R  # noqa: E402
from pipeline.store import RunStore, resolve_stage_ids, next_pending_stage, upstream_texts  # noqa: E402


class TestStages(unittest.TestCase):
    def test_eight_stages_in_order(self):
        self.assertEqual(len(S.STAGES), 8)
        self.assertEqual([s.order for s in S.STAGES], list(range(1, 9)))
        self.assertEqual(
            [s.id for s in S.STAGES],
            ["concept", "character_bible", "worldview", "outline", "script", "assets", "performance", "video_prompts"],
        )

    def test_artifacts_unique_and_safe(self):
        names = [s.artifact for s in S.STAGES]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
            self.assertTrue(name.endswith(".md"))

    def test_every_referenced_skill_exists_in_manifest(self):
        manifest = {s.id for s in S.load_skills()}
        self.assertTrue(manifest, "skills/SKILLS.json 应该能读出技能")
        for stage in S.STAGES:
            for sid in stage.skills:
                self.assertIn(sid, manifest, f"{stage.id} 引用了未登记的技能 {sid}")

    def test_manifest_files_exist_on_disk(self):
        missing = [s.rel_path for s in S.load_skills() if not s.path.exists()]
        self.assertEqual(missing, [], f"技能文件缺失：{missing}")

    def test_skill_texts_budgeted(self):
        """预算契约：首个技能最多吃满 total_budget，其余每个至少保留 2000 字下限，
        所以真实上限是 total_budget + (n-1)*2000。"""
        stage = S.STAGE_BY_ID["video_prompts"]
        picked = S.skill_texts_for_stage(stage, total_budget=8000)
        self.assertTrue(picked)
        total = sum(len(t) for _s, t in picked)
        self.assertLessEqual(total, 8000 + (len(picked) - 1) * 2000)

    def test_default_budget_stays_within_documented_ceiling(self):
        for stage in S.STAGES:
            picked = S.skill_texts_for_stage(stage)
            total = sum(len(t) for _s, t in picked)
            ceiling = S.TOTAL_BUDGET + max(0, len(picked) - 1) * 2000
            self.assertLessEqual(total, ceiling, f"{stage.id} 超出提示词预算")
            self.assertGreater(total, 0, f"{stage.id} 没取到任何技能正文")

    def test_stage_prompt_contains_topic_mode_upstream_and_artifact(self):
        stage = S.STAGE_BY_ID["script"]
        msgs = S.stage_prompt(
            stage,
            topic="外卖小哥读心术",
            mode="short_drama",
            upstream={"concept": "概念正文", "outline": "大纲正文"},
        )
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        system, user = msgs[0]["content"], msgs[1]["content"]
        self.assertIn("非交互", system)
        self.assertIn("技能规范", system)
        self.assertIn("外卖小哥读心术", user)
        self.assertIn("短剧", user)
        self.assertIn("概念正文", user)
        self.assertIn("大纲正文", user)
        self.assertIn(stage.artifact, user)

    def test_stage_prompt_skips_missing_upstream(self):
        stage = S.STAGE_BY_ID["concept"]
        msgs = S.stage_prompt(stage, topic="x", mode="narrated", upstream={"outline": "不该出现"})
        self.assertNotIn("不该出现", msgs[1]["content"])

    def test_unknown_mode_falls_back_to_raw_value(self):
        msgs = S.stage_prompt(S.STAGE_BY_ID["concept"], topic="x", mode="weird")
        self.assertIn("weird", msgs[1]["content"])


class TestResolveStageIds(unittest.TestCase):
    def test_default_is_all_eight(self):
        self.assertEqual(resolve_stage_ids(None), list(S.STAGE_ORDER))
        self.assertEqual(resolve_stage_ids([]), list(S.STAGE_ORDER))

    def test_subset_keeps_canonical_order(self):
        self.assertEqual(resolve_stage_ids(["script", "concept"]), ["concept", "script"])

    def test_unknown_ids_ignored(self):
        self.assertEqual(resolve_stage_ids(["nope", "outline"]), ["outline"])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_load_list(self):
        run = self.store.create(topic="测试主题", mode="short_drama", stage_ids=["concept", "outline"])
        self.assertTrue(run["id"])
        self.assertEqual(set(run["stages"]), {"concept", "outline"})
        loaded = self.store.load(run["id"])
        self.assertEqual(loaded["topic"], "测试主题")
        listed = self.store.list_runs()
        self.assertEqual([r["id"] for r in listed], [run["id"]])
        self.assertEqual(listed[0]["total"], 2)

    def test_artifact_roundtrip_and_counts(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        n = self.store.write_artifact(run["id"], "01-concept.md", "内容")
        self.assertEqual(n, 2)
        self.assertEqual(self.store.read_artifact(run["id"], "01-concept.md"), "内容")
        self.store.mark_stage(run["id"], "concept", status="done", chars=n)
        self.assertEqual(self.store.load(run["id"])["stages"]["concept"]["status"], "done")
        self.assertEqual(self.store.list_runs()[0]["done"], 1)

    def test_rejects_path_traversal(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        for bad in ("../evil.md", "..\\evil.md", "sub/dir.md", "", "x" * 200):
            with self.assertRaises(ValueError):
                self.store.artifact_path(run["id"], bad)
        with self.assertRaises(ValueError):
            self.store.artifact_path("../etc", "a.md")

    def test_artifact_write_stays_inside_run_dir(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        with self.assertRaises(ValueError):
            self.store.write_artifact(run["id"], "../escape.md", "x")
        self.assertFalse((Path(self.tmp.name).parent / "escape.md").exists())

    def test_delete_run(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        self.store.write_artifact(run["id"], "01-concept.md", "x")
        self.assertTrue(self.store.delete(run["id"]))
        self.assertIsNone(self.store.load(run["id"]))

    def test_next_pending_stage(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept", "outline"])
        self.assertEqual(next_pending_stage(run), "concept")
        self.store.mark_stage(run["id"], "concept", status="done")
        self.assertEqual(next_pending_stage(self.store.load(run["id"])), "outline")

    def test_upstream_texts_reads_only_existing_artifacts(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept", "outline"])
        self.store.write_artifact(run["id"], "01-concept.md", "概念")
        got = upstream_texts(self.store, run["id"], ("concept", "outline"))
        self.assertEqual(got, {"concept": "概念"})

    def test_events_are_capped(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        for i in range(250):
            self.store.log_event(run["id"], f"e{i}")
        self.assertLessEqual(len(self.store.load(run["id"])["events"]), 200)

    # ---- 2026-09-30 真实故障的回归测试 ----

    def test_no_lost_update_between_two_writers(self):
        """两个写者各改各的字段，任何一方的改动都不能被另一方覆盖。"""
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        barrier = threading.Barrier(2)

        def a():
            barrier.wait()
            for _ in range(30):
                self.store.mark_stage(run["id"], "concept", provider="A")

        def b():
            barrier.wait()
            for _ in range(30):
                self.store.log_event(run["id"], "B")

        t1, t2 = threading.Thread(target=a), threading.Thread(target=b)
        t1.start(); t2.start(); t1.join(); t2.join()
        final = self.store.load(run["id"])
        self.assertEqual(final["stages"]["concept"]["provider"], "A")
        self.assertTrue(any(ev.endswith("B") for ev in final["events"]))

    def test_no_permission_error_under_concurrent_read_and_write(self):
        """复现过的故障：读者持句柄时 os.replace 抛 PermissionError(13)。

        修法是把读也纳入同一把锁；这里用真并发压 300 轮，任何异常都算失败。
        """
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        errors: list[str] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    self.store.load(run["id"])
                    self.store.list_runs()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"read: {exc!r}")

        def writer():
            try:
                for i in range(300):
                    self.store.mark_stage(run["id"], "concept", chars=i)
                    self.store.log_event(run["id"], f"n{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"write: {exc!r}")

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        w = threading.Thread(target=writer)
        w.start()
        w.join(timeout=60)
        stop.set()
        t.join(timeout=5)
        self.assertEqual(errors, [], f"并发读写出现异常：{errors[:3]}")
        self.assertEqual(self.store.load(run["id"])["stages"]["concept"]["chars"], 299)

    def test_reconcile_marks_interrupted_runs(self):
        stale = self.store.create(topic="stale", mode="short_drama", stage_ids=["concept", "outline"])
        self.store.mark_stage(stale["id"], "concept", status="running")
        self.store.set_fields(stale["id"], status="running", current_stage="concept")
        done = self.store.create(topic="done", mode="short_drama", stage_ids=["concept"])
        self.store.mark_stage(done["id"], "concept", status="done")
        self.store.set_fields(done["id"], status="succeeded")

        self.assertEqual(self.store.reconcile_on_start(), 1)

        fixed = self.store.load(stale["id"])
        self.assertEqual(fixed["status"], "interrupted")
        self.assertEqual(fixed["stages"]["concept"]["status"], "failed")
        self.assertIn("中断", fixed["stages"]["concept"]["error"])
        # 已完成的运行不受影响，且可以对账后再续跑
        self.assertEqual(self.store.load(done["id"])["status"], "succeeded")
        self.assertEqual(next_pending_stage(fixed), "concept")


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.tmp.name))
        self.runner = R.PipelineRunner(self.store, provider="deepseek", local_first=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_execute_writes_artifacts_and_marks_done(self):
        run = self.store.create(topic="主题", mode="short_drama", stage_ids=["concept", "outline"])
        calls = []

        def fake_call_llm(messages, **kwargs):
            calls.append(messages)
            return {"ok": True, "content": "产物正文", "provider": "deepseek", "model": "m"}

        with mock.patch.object(R.model_gateway, "call_llm", side_effect=fake_call_llm):
            result = self.runner.execute(run["id"])

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(calls), 2)
        loaded = self.store.load(run["id"])
        self.assertEqual(loaded["stages"]["concept"]["status"], "done")
        self.assertEqual(loaded["stages"]["outline"]["status"], "done")
        self.assertEqual(self.store.read_artifact(run["id"], "01-concept.md"), "产物正文")
        self.assertEqual(self.store.read_artifact(run["id"], "04-outline.md"), "产物正文")

    def test_execute_stops_at_first_required_failure(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept", "outline"])

        def fake_call_llm(messages, **kwargs):
            return {"ok": False, "error": "上游挂了"}

        with mock.patch.object(R.model_gateway, "call_llm", side_effect=fake_call_llm):
            result = self.runner.execute(run["id"])

        self.assertEqual(result["status"], "failed")
        loaded = self.store.load(run["id"])
        self.assertEqual(loaded["stages"]["concept"]["status"], "failed")
        self.assertEqual(loaded["stages"]["outline"]["status"], "pending")
        self.assertIn("上游挂了", loaded["stages"]["concept"]["error"])

    def test_optional_stage_failure_is_skipped_not_fatal(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["worldview", "outline"])
        seen = []

        def fake_call_llm(messages, **kwargs):
            seen.append(messages[1]["content"])
            if "世界观" in messages[1]["content"]:
                return {"ok": False, "error": "boom"}
            return {"ok": True, "content": "ok", "provider": "p", "model": "m"}

        with mock.patch.object(R.model_gateway, "call_llm", side_effect=fake_call_llm):
            result = self.runner.execute(run["id"])

        self.assertEqual(result["status"], "succeeded")
        loaded = self.store.load(run["id"])
        self.assertEqual(loaded["stages"]["worldview"]["status"], "skipped")
        self.assertEqual(loaded["stages"]["outline"]["status"], "done")

    def test_empty_content_is_a_failure(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        with mock.patch.object(R.model_gateway, "call_llm", return_value={"ok": True, "content": "  "}):
            result = self.runner.execute(run["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("空内容", self.store.load(run["id"])["stages"]["concept"]["error"])

    def test_rerun_stage_resets_tail(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept", "outline"])
        self.store.mark_stage(run["id"], "concept", status="done")
        self.store.mark_stage(run["id"], "outline", status="done")
        ok, _msg = self.runner.rerun_stage(run["id"], "concept")
        self.assertTrue(ok)
        loaded = self.store.load(run["id"])
        self.assertEqual(loaded["stages"]["concept"]["status"], "pending")
        self.assertEqual(loaded["stages"]["outline"]["status"], "pending")
        # 清空队列，避免后台线程真的去跑
        while not self.runner._queue.empty():
            self.runner._queue.get_nowait()

    def test_rerun_unknown_stage_is_rejected(self):
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])
        ok, msg = self.runner.rerun_stage(run["id"], "video_prompts")
        self.assertFalse(ok)
        self.assertIn("不在本次运行", msg)

    def test_manual_edit_survives_concurrent_runner_writes(self):
        """回归：runner 曾持有陈旧快照，会把人工保存的产物状态覆盖回旧值。"""
        run = self.store.create(topic="t", mode="short_drama", stage_ids=["concept"])

        def fake_call_llm(messages, **kwargs):
            # 在「阶段执行中」模拟 HTTP 线程人工保存
            self.store.mark_stage(run["id"], "concept", provider="manual", model="manual")
            return {"ok": True, "content": "自动产物", "provider": "deepseek", "model": "m"}

        with mock.patch.object(R.model_gateway, "call_llm", side_effect=fake_call_llm):
            self.runner.execute(run["id"])

        loaded = self.store.load(run["id"])
        self.assertEqual(loaded["stages"]["concept"]["status"], "done")
        self.assertEqual(loaded["status"], "succeeded")
        # 人工写入的 provider 不应被 runner 的收尾写入抹掉（runner 用重读-改-写）
        self.assertEqual(loaded["stages"]["concept"]["provider"], "deepseek")

    def test_load_llm_key_prefers_env(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "ark-env-key"}, clear=False):
            key, source = R.load_llm_key()
        self.assertEqual(key, "ark-env-key")
        self.assertEqual(source, "env:DEEPSEEK_API_KEY")

    def test_load_llm_key_falls_back_to_own_state_file(self):
        env = {k: "" for k in R.KEY_ENV_NAMES}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(R, "LOCAL_KEY_FILE", Path(self.tmp.name) / "deepseek.key") as _p:
                (Path(self.tmp.name) / "deepseek.key").write_text("ark-own", encoding="utf-8")
                key, source = R.load_llm_key()
        self.assertEqual(key, "ark-own")
        self.assertEqual(source, "state/deepseek.key")

    def test_load_llm_key_returns_empty_when_nothing_available(self):
        env = {k: "" for k in R.KEY_ENV_NAMES}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(R, "LOCAL_KEY_FILE", Path(self.tmp.name) / "nope.key"):
                with mock.patch.object(R, "SIBLING_KEY_FILES", (Path(self.tmp.name) / "also-nope.key",)):
                    key, source = R.load_llm_key()
        self.assertEqual((key, source), ("", ""))


class TestSkillsManifest(unittest.TestCase):
    def test_manifest_is_valid_json_with_required_fields(self):
        raw = json.loads((APP_ROOT / "skills" / "SKILLS.json").read_text(encoding="utf-8"))
        skills = raw["skills"]
        self.assertGreaterEqual(len(skills), 20)
        for item in skills:
            for field in ("id", "title", "file", "stages"):
                self.assertIn(field, item, f"{item.get('id')} 缺字段 {field}")
        ids = [s["id"] for s in skills]
        self.assertEqual(len(ids), len(set(ids)), "技能 id 必须唯一")

    def test_free_pack_has_21_files_and_4_extracted_packs(self):
        free = list((APP_ROOT / "skills" / "free").iterdir())
        self.assertEqual(len(free), 21)
        extracted = [d for d in (APP_ROOT / "skills" / "extracted").iterdir() if d.is_dir()]
        self.assertEqual(len(extracted), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
