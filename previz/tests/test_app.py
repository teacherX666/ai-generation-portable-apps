"""previz 后端 unittest：直接调 Handler 级函数 + 内存态，不起真实端口。

约定：与 director 一样，路由逻辑放 Handler 方法、存储逻辑放模块级函数，
测试只 import 模块（app.py 被 import 时不得启动服务器——server 启动放
`if __name__ == "__main__` 块内）。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import app as previz

PROJECTS_DIR = Path(tempfile.mkdtemp(prefix="previz-test-"))
previz.PROJECTS_DIR = PROJECTS_DIR  # 测试注入数据目录


def _mk(pid="p_test01"):
    p = previz.new_project("测试项目")
    p["id"] = pid
    previz.save_project(p)
    return p


class TestProjectCRUD(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(PROJECTS_DIR, ignore_errors=True)
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    def test_new_project_defaults(self):
        p = previz.new_project("夏日广告")
        assert p["name"] == "夏日广告"
        assert p["id"].startswith("p_")
        assert p["shots"] == []
        assert "created_at" in p and "created_by_ip" in p

    def test_save_and_load_roundtrip(self):
        p = _mk()
        previz.save_project(p)
        loaded = previz.load_project("p_test01")
        assert loaded["name"] == "测试项目"
        assert loaded["id"] == "p_test01"

    def test_list_projects_skips_broken_files(self):
        _mk("p_good")
        (PROJECTS_DIR / "p_bad").mkdir(exist_ok=True)
        (PROJECTS_DIR / "p_bad" / "project.json").write_text("{broken", encoding="utf-8")
        names = [p["name"] for p in previz.list_projects()]
        assert names == ["测试项目"]

    def test_delete_removes_dir(self):
        _mk()
        assert previz.delete_project("p_test01") is True
        assert not (PROJECTS_DIR / "p_test01").exists()
        assert previz.delete_project("p_missing") is False

    def test_load_missing_returns_none(self):
        assert previz.load_project("p_missing") is None

    def test_validate_rejects_non_dict(self):
        assert previz.validate_project("not a dict") is None
        assert previz.validate_project({"id": "p_x"}) is None  # 缺 name/shots

    def test_validate_fills_missing_shot_fields(self):
        p = _mk()
        p["shots"].append({"id": "s_1"})  # 缺相机/人物/道具
        out = previz.validate_project(p)
        s = out["shots"][0]
        assert s["camera"]["fov"] == 50
        assert s["characters"] == [] and s["props"] == [] and s["notes"] == ""

    def test_id_regex(self):
        assert previz.valid_id("p_abc-123_x") is True
        assert previz.valid_id("../etc") is False
        assert previz.valid_id("") is False
