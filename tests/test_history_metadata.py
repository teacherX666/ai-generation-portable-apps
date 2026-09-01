import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location(
    "portal_app_meta", ROOT / "portal" / "app.py"
)
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_meta"] = portal
_spec.loader.exec_module(portal)


def test_json_body_whitelist_and_key_exclusion():
    meta = portal.extract_job_metadata(
        "application/json",
        json.dumps({"prompt": "一只猫", "aspect_ratio": "1:1",
                    "count": 2, "api_key": "sk-secret", "model": "m1"}).encode(),
    )
    assert meta["prompt"] == "一只猫"
    assert meta["params"] == {"aspect_ratio": "1:1", "count": 2}
    assert "api_key" not in json.dumps(meta["params"])
    assert meta["model"] == "m1"


def test_multipart_body_prompt_extraction():
    boundary = "----test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        "雨夜霓虹街头\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="aspect_ratio"\r\n\r\n'
        "9:16\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    ctype = f"multipart/form-data; boundary={boundary}"
    meta = portal.extract_job_metadata(ctype, body)
    assert meta["prompt"] == "雨夜霓虹街头"
    assert meta["params"] == {"aspect_ratio": "9:16"}


def test_oversize_body_skips_prompt():
    meta = portal.extract_job_metadata("application/json", b"x" * (5 * 1024 * 1024 + 1))
    assert meta["prompt"] == ""
    assert meta["params"] == {}


def test_unparsable_body_returns_empty():
    meta = portal.extract_job_metadata("application/json", b"{not json")
    assert meta["prompt"] == "" and meta["params"] == {}
