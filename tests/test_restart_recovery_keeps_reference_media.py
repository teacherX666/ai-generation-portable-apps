"""重启恢复不能丢参考素材（用户 2026-10-12：「参考图能看见，但生成时没用上」）。

**真实事故**：Portal / 子应用今天被重启 5 次（13:02:46 / 15:32:54 / 15:47:10 / 15:56:01 /
17:09:24）。每次重启都会把排队中的作业「自动恢复入队」重投，而重投时**参考素材被静默丢掉** ——
作业照旧**成功**，成片却是无视参考图的文生图。

实锤（seedance 活动记录 `2026-10-12 17:06:22` / MM，附了 `ref_image_1` + `ref_image_2`）：

```
17:09:31 服务重启后自动恢复入队
17:09:31 Started 2 run(s) ...
17:09:31 Run 1: creating task (text:1, image_url:0, video_url:0, audio_url:0)   ← 素材是 0
```

**根因**：素材文件落在**用户前缀作用域**目录

    state/workspaces/u_<用户>_<ws>/media/<stored>

而 `recover_backlog()` / `retry_job()` 把**裸 ws**（`jobs_backlog.json` 里存的 `ws_id`，
如 `ws-1790238813605-ec871`）传给 `_files_from_restore()` → `_ws_media_dir(裸ws)` 指向不存在的
目录 → `path.exists()` 为假 → `continue` **静默跳过** → `files` 为空 → 重投无素材。
（`media_scope = _config_scope_for(ws_id, username)` 在同一段代码里**已经算对了**，只是没拿去
找文件。）

这正是本仓库记过的那个坑：**「日志里的 ws」≠「实际 scope」**。
seedance 与 nano-banana 各有一处 `recover_backlog` + 一处 `retry_job`，四处同一错。

注：本仓库的应用测试用 `tests/run_tests_local.py`（本机该解释器没有 pytest），
所以这里**不用任何 pytest 专用特性**，只用 runner 支持的 `tmp_path` / `monkeypatch`。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ("seedance", "nano-banana")


def _load(app: str):
    mod_path = ROOT / app / "app.py"
    name = f"{app.replace('-', '_')}_refmedia_recovery_test"
    spec = importlib.util.spec_from_file_location(name, mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _lock(mod):
    return getattr(mod, "JOBS_LOCK", None) or mod.LOCK


def _isolate(mod, state_dir: Path, monkeypatch) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(mod, "ACTIVITY_PATH", state_dir / "activity_log.json")
    monkeypatch.setattr(mod, "BACKLOG_PATH", state_dir / "jobs_backlog.json")
    mod.JOBS.clear()


def _seed_user_scoped_media(mod, *, scope_ws: str, username: str, stored: str) -> str:
    """按生产布局把素材写到 `workspaces/u_<用户>_<ws>/media/`，返回实际作用域。"""
    scope = mod._config_scope_for(scope_ws, username)
    media_dir = mod._ws_media_dir(scope)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / stored).write_bytes(b"PNGDATA")
    return scope


def _image_field(mod) -> str:
    """各模块的参考图字段名不同：seedance 用 `ref_image_N`，nano-banana 用 `image_N`。"""
    for candidate in ("ref_image_1", "image_1"):
        if candidate in mod.FILE_FIELDS:
            return candidate
    raise AssertionError("找不到参考图字段")


def _write_activity_with_media(
    mod, *, activity_id: str, job_id: str, stored: str, ws: str = "ws1"
) -> None:
    field = _image_field(mod)
    mod.write_activity_log(
        [
            {
                "id": activity_id,
                "job_id": job_id,
                "status": "running",
                "workspace_id": ws,
                "username": "tester",
                "restore": {
                    "values": {"prompt": "p", "duration": 10},
                    "media": {
                        field: {
                            "filename": "ref.png",
                            "stored": stored,
                            "mime": "image/png",
                        }
                    },
                },
            }
        ]
    )


def test_files_from_restore_reads_the_user_scoped_media_dir(tmp_path, monkeypatch) -> None:
    """对照组：作用域传对了就能读回文件（证明文件与 restore 都是好的）。"""
    for app in APPS:
        mod = _load(app)
        _isolate(mod, tmp_path / app, monkeypatch)
        scope = _seed_user_scoped_media(
            mod, scope_ws="ws1", username="tester", stored="abc_ref_image_1.png"
        )
        _write_activity_with_media(
            mod, activity_id="act-1", job_id="job-1", stored="abc_ref_image_1.png"
        )

        # 只给「裸 ws + 用户名」，候选目录由函数内部解析（结构性收口：调用点没机会传错）
        assert mod._config_scope_for("ws1", "tester") == scope
        files = mod._files_from_restore(
            mod.read_activity_log()[0]["restore"], "ws1", "tester"
        )

        assert files.get(_image_field(mod)) == ("ref.png", b"PNGDATA"), app


def test_legacy_bare_ws_media_still_readable(tmp_path, monkeypatch) -> None:
    """旧素材（前缀隔离之前写在**裸 ws 目录**里的）仍要能读回 —— 修 bug 不能修回退。

    实测两种布局在生产上是**并存**的：`workspaces/ws-1789375564109-9ece4/media/`（101 个旧文件）
    与 `workspaces/u_cfy_ws-1789375564109-9ece4/media/`（3 个新文件）同时存在。
    """
    for app in APPS:
        mod = _load(app)
        _isolate(mod, tmp_path / app, monkeypatch)
        legacy_dir = mod._ws_media_dir("ws1")  # 裸 ws 目录
        legacy_dir.mkdir(parents=True, exist_ok=True)
        stored = "legacy_ref_image_1.png"
        (legacy_dir / stored).write_bytes(b"OLD")
        _write_activity_with_media(
            mod, activity_id="act-3", job_id="job-3", stored=stored
        )

        restore = mod.read_activity_log()[0]["restore"]
        # 同一个调用（裸 ws + 用户名）：旧素材写在裸 ws 目录里也要能找到
        files = mod._files_from_restore(restore, "ws1", "tester")

        assert files.get(_image_field(mod)) == ("ref.png", b"OLD"), app


def test_restart_recovery_keeps_reference_media(tmp_path, monkeypatch) -> None:
    """重启「自动恢复入队」必须带着参考素材重投（生产 2026-10-12 的坑）。"""
    for app in APPS:
        mod = _load(app)
        _isolate(mod, tmp_path / app, monkeypatch)
        scope = _seed_user_scoped_media(
            mod, scope_ws="ws1", username="tester", stored="abc_ref_image_1.png"
        )
        _write_activity_with_media(
            mod, activity_id="act-1", job_id="job-1", stored="abc_ref_image_1.png"
        )
        with _lock(mod):
            # backlog 里存的就是**裸 ws**（生产 jobs_backlog.json 的真实形态）
            mod._backlog_set_locked(
                "job-1", stage="queued", activity_id="act-1", ws_id="ws1", username="tester"
            )

        captured: dict = {}

        def _fake_run_job(job_id, values, files, activity_id, media_scope, _c=captured):
            _c.update(job_id=job_id, files=dict(files), media_scope=media_scope)

        monkeypatch.setattr(mod, "run_job", _fake_run_job)
        recovered, _interrupted = mod.recover_backlog()

        assert recovered == 1, app
        # 作用域本来就算对了 —— 问题只在「没拿它去找文件」
        assert captured["media_scope"] == scope, app
        field = _image_field(mod)
        assert field in captured["files"], (
            f"{app}: 重启重投把参考素材丢了（成片会是无视参考图的文生图）"
        )
        assert captured["files"][field] == ("ref.png", b"PNGDATA"), app


def test_recovery_warns_when_reference_media_cannot_be_rebuilt(tmp_path, monkeypatch) -> None:
    """素材**真的**找不回来时必须说出声 —— 静默重投成文生图是最坑的形态。

    2026-10-12 事故里作业照旧 succeeded，界面上一个字都没有，用户只能靠肉眼发现成片没参考。
    """
    for app in APPS:
        mod = _load(app)
        _isolate(mod, tmp_path / app, monkeypatch)
        # 刻意不写任何素材文件
        _write_activity_with_media(
            mod, activity_id="act-4", job_id="job-4", stored="gone.png"
        )
        with _lock(mod):
            mod._backlog_set_locked(
                "job-4", stage="queued", activity_id="act-4", ws_id="ws1", username="tester"
            )

        monkeypatch.setattr(mod, "run_job", lambda *a, **k: None)
        mod.recover_backlog()

        events = " ".join(
            str(event.get("message") or "") for event in mod.JOBS["job-4"]["events"]
        )
        assert "参考素材" in events, f"{app}: 素材没找回却一声不响（用户会以为生成用了参考图）"


def test_no_call_site_rebuilds_media_with_a_bare_workspace_id() -> None:
    """**源码级护栏**：不许再出现「用裸 ws 去重建素材」的调用形态（2026-10-12 事故的形态）。

    这是防「第四次」的防线。运行时已经两个布局都找（读兜底），但调用点仍然必须给出
    `media_scope`：一旦旧素材目录被清理/迁移，只给裸 ws 的调用会**静默**丢素材，回到原点。
    护栏写成扫源码的测试 —— 在这台机器上「靠人记住规则」已经被证明不管用（同一个坑犯了三次）。
    """
    import re

    for app in APPS:
        source = (ROOT / app / "app.py").read_text(encoding="utf-8")
        calls = re.findall(r"_files_from_restore\(([^)]*)\)", source)
        assert calls, f"{app}: 找不到 _files_from_restore 的调用（函数被改名了？同步改这条护栏）"
        for args in calls:
            provided = [item for item in args.split(",") if item.strip()]
            assert len(provided) >= 3, (
                f"{app}: `_files_from_restore({args.strip()})` 必须写成 "
                "`(restore, ws_id, username)` —— 作用域由函数内部解析，调用点不许自己拼"
            )
        assert "_files_from_restore(restore, ws_id)" not in source, (
            f"{app}: 又出现了「裸 ws 找素材」的调用 —— 这正是 2026-10-12 丢参考素材的形态"
        )
        assert "_files_from_restore(restore, media_scope, ws_id)" not in source, (
            f"{app}: 调用点又在自己拼作用域了 —— 2026-10-12 丢参考素材就是这么来的"
        )


def test_submit_time_invariant_flags_declared_but_unattached_references() -> None:
    """**结果侧兜底**：声明了参考素材、上游却一张没挂上 → 必须报警。

    这条不变量**不看原因**（作用域错 / 文件被清理 / 恢复没重建回来 / 模板没挂上…），
    所以它是这个 bug 类的最后一道防线。判据直接取自 2026-10-12 事故那一行事件：

        creating task (text:1, image_url:0, video_url:0, audio_url:0)
    """
    mod = _load("seedance")
    zero = "text:1, image_url:0, video_url:0, audio_url:0"

    warning = mod.reference_attachment_warning(
        ["ref_image_1", "ref_image_2", "ref_video_1"], zero
    )
    assert warning and "参考素材" in warning, "声明了 3 个素材却挂上 0 个，必须报警"

    # 只要挂上任意一个（图/视频/音频），就不算「没用上」
    assert mod.reference_attachment_warning(["ref_image_1"], "text:1, image_url:1, video_url:0, audio_url:0") is None
    assert mod.reference_attachment_warning(["ref_video_1"], "text:1, image_url:0, video_url:1, audio_url:0") is None
    # 本来就没声明参考素材（纯文生视频）不许误报
    assert mod.reference_attachment_warning([], zero) is None
    assert mod.reference_attachment_warning(["", "  "], zero) is None


class _ValueField:
    """最小假字段：模拟 cgi.FieldStorage 里一个普通文本项（无 filename）。"""

    filename = None

    def __init__(self, value: str) -> None:
        self.value = value


class _SavedMediaForm:
    """最小假表单：只带 saved_media（模拟「界面选了历史素材」这一路）。"""

    def __init__(self, saved: dict) -> None:
        import json as _json

        self._fields = {"saved_media": _ValueField(_json.dumps(saved))}

    def __contains__(self, name: str) -> bool:
        return name in self._fields

    def __getitem__(self, name: str):
        return self._fields[name]


def test_nano_banana_flags_declared_but_unreadable_references() -> None:
    """nano-banana 的提交前不变量：声明了参考图、一张都读不到 → 必须报警。

    （字段名与 seedance 不同：这个模块是 `image_N`，seedance 是 `ref_image_N`。）
    """
    mod = _load("nano-banana")
    form = _SavedMediaForm(
        {
            "image_1": {"filename": "a.png", "stored": "gone_1.png", "mime": "image/png"},
            "image_2": {"filename": "b.png", "stored": "gone_2.png", "mime": "image/png"},
        }
    )

    declared = mod.declared_reference_fields(form)
    assert declared == ["image_1", "image_2"]

    warning = mod.reference_attachment_warning(declared, 0)
    assert warning and "参考图" in warning, "声明了 2 张却收到 0 张，必须报警"

    # 挂上了就不报；本来没声明也不许误报
    assert mod.reference_attachment_warning(declared, 1) is None
    assert mod.reference_attachment_warning([], 0) is None


def test_replaced_media_moves_to_trash_and_stays_readable(tmp_path, monkeypatch) -> None:
    """换素材时旧文件**进回收站而不是删除**（用户 2026-10-12 口径）。

    判别力：旧实现是 `old_path.unlink()`，这份素材会被删掉 —— 那时「历史 restore 仍能读到」
    这条断言必红。实测事故：MM 17:41 换参考视频 → 17:06 那条作业的 `ref_video_1` 永久取不到。
    """
    for app in APPS:
        mod = _load(app)
        _isolate(mod, tmp_path / app, monkeypatch)
        scope = _seed_user_scoped_media(
            mod, scope_ws="ws1", username="tester", stored="abc_ref_image_1.png"
        )
        _write_activity_with_media(
            mod, activity_id="act-5", job_id="job-5", stored="abc_ref_image_1.png"
        )
        media_dir = mod._ws_media_dir(scope)
        restore = mod.read_activity_log()[0]["restore"]

        retired = mod.retire_replaced_media(media_dir, "abc_ref_image_1.png")

        assert retired is not None, app
        assert not (media_dir / "abc_ref_image_1.png").exists(), f"{app}: 旧素材没被移走"
        assert (media_dir.parent / "media_trash" / "abc_ref_image_1.png").is_file(), app
        # ★ 关键：历史作业的 restore 仍然读得到（这就是「别删」的意义）
        files = mod._files_from_restore(restore, "ws1", "tester")
        assert files.get(_image_field(mod)) == ("ref.png", b"PNGDATA"), (
            f"{app}: 换素材后历史作业读不回参考素材了"
        )


def test_sweep_media_trash_only_removes_expired(tmp_path, monkeypatch) -> None:
    """回收站轮转：只清超过保留期的，保留期内的一律留着。"""
    import os

    for app in APPS:
        mod = _load(app)
        _isolate(mod, tmp_path / app, monkeypatch)
        trash = mod._ws_trash_dir("u_tester_ws1")
        trash.mkdir(parents=True, exist_ok=True)
        old_file = trash / "old.png"
        new_file = trash / "new.png"
        old_file.write_bytes(b"OLD")
        new_file.write_bytes(b"NEW")
        stale = 30 * 86400
        os.utime(old_file, (old_file.stat().st_atime - stale, old_file.stat().st_mtime - stale))

        stats = mod.sweep_media_trash(max_age_days=14)

        assert stats["removed"] == 1 and stats["kept"] == 1, f"{app}: {stats}"
        assert not old_file.exists(), app
        assert new_file.is_file(), f"{app}: 保留期内的素材被误删"


def test_retry_keeps_reference_media(tmp_path, monkeypatch) -> None:
    """「重试」也要带着参考素材（同一处错，别只修恢复那条路）。"""
    for app in APPS:
        mod = _load(app)
        _isolate(mod, tmp_path / app, monkeypatch)
        scope = _seed_user_scoped_media(
            mod, scope_ws="ws1", username="tester", stored="abc_ref_image_1.png"
        )
        _write_activity_with_media(
            mod, activity_id="act-2", job_id="job-2", stored="abc_ref_image_1.png"
        )

        captured: dict = {}

        def _fake_create_job(values, files, *args, _c=captured, **kwargs):
            _c.update(files=dict(files), kwargs=kwargs)
            return "job-new"

        monkeypatch.setattr(mod, "create_job", _fake_create_job)
        mod.retry_job("job-2")

        field = _image_field(mod)
        assert field in captured["files"], f"{app}: 重试把参考素材丢了"
        assert captured["files"][field] == ("ref.png", b"PNGDATA"), app
        assert captured["kwargs"].get("media_scope") == scope, app
