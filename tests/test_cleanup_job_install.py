import importlib.util
import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "portal_app_cleanup_job", ROOT / "portal" / "app.py"
)
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_cleanup_job"] = portal
_spec.loader.exec_module(portal)

CFG = {"enabled": True, "output_days": 7, "trash_days": 1, "hour": 3, "minute": 47}


def test_build_cleanup_plist_is_valid_and_uses_current_deploy_paths(tmp_path):
    text = portal.build_cleanup_plist(
        CFG, python="/usr/bin/python3", script=tmp_path / "cleanup_daily.py", home=tmp_path
    )
    data = plistlib.loads(text.encode("utf-8"))
    assert data["Label"] == "com.ai-portal-cleanup"
    args = data["ProgramArguments"]
    assert args[0] == "/usr/bin/python3"
    assert args[1].endswith("cleanup_daily.py")
    assert "--apply" in args
    assert args[args.index("--outputs-retention") + 1] == "7"
    assert args[args.index("--trash-days") + 1] == "1"
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 47}
    assert data["WorkingDirectory"] == str(ROOT)  # 仓库根目录（portal/app.py 里是 ROOT.parent）
    assert data["StandardOutPath"] == str(tmp_path / "Library" / "Logs" / "ai-portal-cleanup.log")


def test_ensure_cleanup_job_installs_then_is_idempotent(tmp_path, monkeypatch):
    calls = []
    target = tmp_path / "LaunchAgents" / "com.ai-portal-cleanup.plist"
    script = tmp_path / "cleanup_daily.py"
    script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(portal, "CLEANUP_SCRIPT", script)

    first = portal.ensure_cleanup_job(
        plist_path=target, runner=calls.append, config=CFG, home=tmp_path
    )
    assert first == "installed"
    assert target.exists()
    assert [c[1] for c in calls] == ["bootout", "bootstrap"]

    calls.clear()
    second = portal.ensure_cleanup_job(
        plist_path=target, runner=calls.append, config=CFG, home=tmp_path
    )
    assert second == "unchanged"
    assert calls == []


def test_ensure_cleanup_job_rewrites_when_retention_changes(tmp_path, monkeypatch):
    target = tmp_path / "LaunchAgents" / "com.ai-portal-cleanup.plist"
    script = tmp_path / "cleanup_daily.py"
    script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(portal, "CLEANUP_SCRIPT", script)
    portal.ensure_cleanup_job(plist_path=target, runner=lambda a: None, config=CFG, home=tmp_path)
    changed = dict(CFG, output_days=3)
    assert portal.ensure_cleanup_job(
        plist_path=target, runner=lambda a: None, config=changed, home=tmp_path
    ) == "installed"
    data = plistlib.loads(target.read_bytes())
    args = data["ProgramArguments"]
    assert args[args.index("--outputs-retention") + 1] == "3"


def test_ensure_cleanup_job_can_be_disabled(tmp_path):
    target = tmp_path / "LaunchAgents" / "com.ai-portal-cleanup.plist"
    assert portal.ensure_cleanup_job(
        plist_path=target, runner=lambda a: None,
        config={"enabled": False}, home=tmp_path,
    ) == "disabled"
    assert not target.exists()
