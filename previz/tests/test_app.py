"""previz 后端 unittest：直接调 Handler 级函数 + 内存态，不起真实端口。

约定：与 director 一样，路由逻辑放 Handler 方法、存储逻辑放模块级函数，
测试只 import 模块（app.py 被 import 时不得启动服务器——server 启动放
`if __name__ == "__main__` 块内）。
"""
import cgi
import io
import json
import shutil
import tempfile
import unittest
import zipfile
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


FIXTURE = Path(__file__).resolve().parent / "_render_fixture.png"


def _mk_shot(pid="p_test01", sid="shot1"):
    p = _mk(pid)
    p["shots"].append({
        "id": sid, "name": "镜头1", "order": 0, "aspect": "16:9",
        "camera": previz.DEFAULT_CAMERA, "characters": [], "props": [],
        "thumbnail": "", "render": "", "notes": "",
    })
    previz.save_project(p)
    return p


class _FakeHandler:
    """json_response / _handle_export 落点：记录状态码与响应头，body 进 BytesIO。"""

    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = 0
        self.headers_out = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.headers_out[k] = v

    def end_headers(self):
        pass


def _make_form(shot_id="shot1", with_render=True, with_thumb=True):
    """构造 multipart FieldStorage（走 cgi 标准解析，与真实请求同路径）。"""
    boundary = "----previz-boundary-42"

    def field(name, filename, ctype, data):
        return (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"

    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="shot_id"\r\n\r\n{shot_id}\r\n').encode()
    if with_render:
        body += field("render", "render.png", "image/png", FIXTURE.read_bytes())
    if with_thumb:
        body += field("thumb", "thumb.png", "image/png", FIXTURE.read_bytes())
    body += f"--{boundary}--\r\n".encode()
    env = {"REQUEST_METHOD": "POST",
           "CONTENT_TYPE": f"multipart/form-data; boundary={boundary}",
           "CONTENT_LENGTH": str(len(body))}
    # 不能传 headers=env：headers 给定后 cgi 不再从 environ 反填，且它按
    # 小写 content-type 查字典（普通 dict 大小写敏感）→ multipart 被当整包
    # 单字段吞掉。省略 headers 让 cgi 自行反填小写键，与 do_POST 同解析路径。
    return cgi.FieldStorage(fp=io.BytesIO(body), environ=env)


class TestRenderAndFiles(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(PROJECTS_DIR, ignore_errors=True)
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.handler = _FakeHandler()

    def _handle(self, pid, form):
        previz.Handler._handle_render(self.handler, pid, form)
        return self.handler

    def test_render_saves_png_and_updates_shot(self):
        _mk_shot()
        self._handle("p_test01", _make_form())
        assert (PROJECTS_DIR / "p_test01" / "s_shot1_render.png").exists()
        assert (PROJECTS_DIR / "p_test01" / "s_shot1_thumb.png").exists()
        p = previz.load_project("p_test01")
        assert p["shots"][0]["render"] == "s_shot1_render.png"
        assert p["shots"][0]["thumbnail"] == "s_shot1_thumb.png"

    def test_render_unknown_shot_400(self):
        _mk_shot()
        self._handle("p_test01", _make_form(shot_id="nope"))
        assert self.handler.status == 400

    def test_render_missing_file_400(self):
        _mk_shot()
        self._handle("p_test01", _make_form(with_render=False))
        assert self.handler.status == 400

    def test_render_unknown_project_404(self):
        self._handle("p_missing", _make_form())
        assert self.handler.status == 404

    def test_export_zip_contains_named_pngs(self):
        _mk_shot()
        self._handle("p_test01", _make_form())
        previz.Handler._handle_export(self.handler, "p_test01")
        assert self.handler.status == 200
        assert "filename*=UTF-8''" in self.handler.headers_out["Content-Disposition"]
        with zipfile.ZipFile(io.BytesIO(self.handler.wfile.getvalue())) as zf:
            assert zf.namelist() == ["00-镜头1.png"]

    def test_render_over_limit_413(self):
        _mk_shot()
        orig = previz.MAX_RENDER_BYTES
        previz.MAX_RENDER_BYTES = 10  # fixture 70 字节 → 限长读入 11 字节即触发
        try:
            self._handle("p_test01", _make_form())
        finally:
            previz.MAX_RENDER_BYTES = orig
        assert self.handler.status == 413
        # 限长拒绝必须发生在任何落盘之前
        assert not (PROJECTS_DIR / "p_test01" / "s_shot1_render.png").exists()

    def test_json_response_no_store(self):
        previz.json_response(self.handler, 200, {"ok": True})
        assert self.handler.headers_out["Cache-Control"] == "no-store"

    def test_put_garbage_content_length_400(self):
        # 与 _handle_render 同款鸭子类型调用：只测 do_PUT 的 CL 容错路径，
        # 不需要真实 socket（n=0 时不读 rfile）
        h = _FakeHandler()
        h.path = "/api/projects/p_test01"
        h.headers = {"Content-Length": "abc"}  # 垃圾 CL → _read_json_body 容错返回 None
        previz.Handler.do_PUT(h)
        assert h.status == 400

    def test_file_regex_rejects_traversal(self):
        assert previz._FILE_RE.fullmatch("../p_test01/project.json") is None
        assert previz._FILE_RE.fullmatch("s_a_render.png") is not None
        assert previz._FILE_RE.fullmatch("project.json") is None

    def test_zip_entry_name_sanitizes(self):
        assert previz._zip_entry_name("../../evil") == "evil"
        assert previz._zip_entry_name("a/b\\c") == "a_b_c"
        assert previz._zip_entry_name("") == "shot"
