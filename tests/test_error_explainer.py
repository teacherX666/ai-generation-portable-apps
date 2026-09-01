import importlib.util
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location(
    "portal_error_explainer", ROOT / "portal" / "error_explainer.py"
)
explainer = importlib.util.module_from_spec(_spec)
sys.modules["portal_error_explainer"] = explainer
_spec.loader.exec_module(explainer)


def _fresh_state():
    explainer._CACHE.clear()
    explainer._LOCKS.clear()
    explainer._NEGATIVE_UNTIL.clear()


def _ok_response():
    body = b'{"choices": [{"message": {"content": "\\u60a8\\u7684\\u7d20\\u6750\\u5f15\\u7528\\u5df2\\u5931\\u6548\\uff0c\\u8bf7\\u91cd\\u65b0\\u4e0a\\u4f20\\u3002"}}]}'
    resp = mock.Mock()
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    resp.read.return_value = body
    return resp


def test_explain_returns_none_for_invalid_inputs():
    _fresh_state()
    with mock.patch.object(explainer.urllib.request, "urlopen") as urlopen:
        assert explainer.explain_error("j1", "", "detail", "key-12345678") is None
        assert explainer.explain_error("j1", "code", "", "key-12345678") is None
        assert explainer.explain_error("j1", "c" * 65, "d", "key-12345678") is None
        assert explainer.explain_error("j1", "code", "d" * 501, "key-12345678") is None
        assert explainer.explain_error("j1", "code", "detail", "short") is None
        urlopen.assert_not_called()


def test_explain_calls_ark_and_caches_per_job():
    _fresh_state()
    with mock.patch.object(explainer.urllib.request, "urlopen", return_value=_ok_response()) as urlopen:
        result = explainer.explain_error("j1", "InvalidParameter", "ref not found", "key-12345678", timeout=5)
        assert isinstance(result, str) and len(result) > 0
        # 同 job 二次调用走缓存，不再发请求
        again = explainer.explain_error("j1", "InvalidParameter", "ref not found", "key-12345678", timeout=5)
        assert again == result
        assert urlopen.call_count == 1
        # 请求体带模型与中文指令
        request = urlopen.call_args[0][0]
        payload = __import__("json").loads(request.data)
        assert payload["messages"][0]["role"] == "system"
        assert "错误码：InvalidParameter" in payload["messages"][1]["content"]


def test_explain_negative_caches_failures():
    _fresh_state()
    with mock.patch.object(explainer.urllib.request, "urlopen",
                           side_effect=__import__("urllib").error.URLError("boom")) as urlopen:
        assert explainer.explain_error("j1", "Code", "detail", "key-12345678", timeout=5) is None
        assert explainer.explain_error("j1", "Code", "detail", "key-12345678", timeout=5) is None
        assert urlopen.call_count == 1
    # TTL 过后允许重试
    explainer._NEGATIVE_UNTIL[("Code", "detail")] = time.time() - 1
    with mock.patch.object(explainer.urllib.request, "urlopen", return_value=_ok_response()) as urlopen:
        assert explainer.explain_error("j1", "Code", "detail", "key-12345678", timeout=5) is not None
        assert urlopen.call_count == 1


def test_explain_rejects_garbage_response():
    _fresh_state()
    resp = mock.Mock()
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    resp.read.return_value = b"not json"
    with mock.patch.object(explainer.urllib.request, "urlopen", return_value=resp):
        assert explainer.explain_error("j1", "Code", "detail", "key-12345678", timeout=5) is None
