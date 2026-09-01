"""素材库分组体系 —— ark_library 分组管理函数的单元测试。

mock 掉 _openapi_call（签名与网络都不碰），验证：
- list_groups 翻页 cap 200、不满页即停、created_at 提取
- list_group_assets 翻页 cap 250、Failed 项 error_message 提取、media_type 推导
- delete_group / rename_group / update_asset / create_group 的调用形状与校验
- get_asset_url 空 URL 抛 LibraryInvalid
- upload_image 显式 group_id 校验（不触发任何网络）

导入方式参照 tests/test_platform_thumb.py 的 spec_from_file_location。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "infinite-canvas"))

_spec = importlib.util.spec_from_file_location(
    "ark_library_groups", ROOT / "infinite-canvas" / "ark_library.py"
)
ark = importlib.util.module_from_spec(_spec)
sys.modules["ark_library_groups"] = ark
_spec.loader.exec_module(ark)

CFG = {"ark_access_key": "ak", "ark_secret_key": "sk",
       "tos_access_key": "tak", "tos_secret_key": "tsk",
       "tos_bucket": "bucket", "tos_region": "cn-beijing",
       "project_name": "Seedance2.0"}


def _group_item(i: int) -> dict:
    return {"Id": f"group-20260101000000-g{i:03d}", "Name": f"组{i}"}


def _asset_item(i: int, status: str = "Active", *, error: dict | None = None,
                media_type: str = "Image") -> dict:
    item = {"Id": f"asset-{i:06d}", "Name": f"素材{i}", "Status": status,
            "URL": f"https://bucket.tos-cn-beijing.volces.com/refmedia/x-{i}.png",
            "AssetType": media_type}
    if error is not None:
        item["Error"] = error
    return item


# ------------------------------------------------------------- list_groups

def test_list_groups_paginates_and_caps_at_200(monkeypatch):
    calls = []

    def fake_call(cfg, action, payload):
        calls.append((action, payload))
        return {"Result": {"Items": [_group_item(i) for i in range(100)]}}

    monkeypatch.setattr(ark, "_openapi_call", fake_call)
    groups = ark.list_groups(CFG)

    assert len(groups) == 200
    assert len(calls) == 2  # 第二页后达 cap，不再翻第三页
    assert calls[0][0] == "ListAssetGroups"
    assert calls[0][1]["Filter"] == {"GroupType": "AIGC"}
    assert calls[0][1]["PageSize"] == 100
    assert calls[1][1]["PageNumber"] == 2
    assert groups[0]["group_id"] == "group-20260101000000-g000"
    assert groups[0]["name"] == "组0"
    assert groups[0]["created_at"] == "2026-01-01"  # 组 id 内嵌日期提取


def test_list_groups_stops_when_page_not_full(monkeypatch):
    calls = []

    def fake_call(cfg, action, payload):
        calls.append(action)
        return {"Result": {"Items": [_group_item(i) for i in range(60)]}}

    monkeypatch.setattr(ark, "_openapi_call", fake_call)
    groups = ark.list_groups(CFG)

    assert len(groups) == 60
    assert len(calls) == 1


def test_list_groups_rejects_invalid_items(monkeypatch):
    monkeypatch.setattr(ark, "_openapi_call", lambda cfg, action, payload: {
        "Result": {"Items": [{"Id": "../evil", "Name": "x"}]}})
    with pytest.raises(ark.LibraryInvalid):
        ark.list_groups(CFG)


# ------------------------------------------------------- list_group_assets

def test_list_group_assets_paginates_and_caps_at_250(monkeypatch):
    calls = []

    def fake_call(cfg, action, payload):
        calls.append((action, payload))
        return {"Result": {"Items": [_asset_item(i) for i in range(50)]}}

    monkeypatch.setattr(ark, "_openapi_call", fake_call)
    items = ark.list_group_assets(CFG, "group-abc")

    assert len(items) == 250
    assert len(calls) == 5  # 5 页 × 50 = 250，不再翻第六页
    assert calls[0][0] == "ListAssets"
    assert calls[0][1]["Filter"]["GroupIds"] == ["group-abc"]
    assert calls[0][1]["Filter"]["Statuses"] == ["Processing", "Active", "Failed"]
    assert calls[0][1]["PageSize"] == 50
    assert items[0] == {"asset_id": "asset-000000", "name": "素材0",
                        "status": "active", "media_type": "image",
                        "error_message": None}


def test_list_group_assets_extracts_failed_error_message(monkeypatch):
    page = [
        _asset_item(1, "Failed", error={"Message": "宽高比超出范围"}),
        _asset_item(2, "Failed"),  # 无 Error.Message → error_message 保持 None
        _asset_item(3, "Active"),
        _asset_item(4, "Processing", media_type="Video"),
        _asset_item(5, "Failed", error={"Code": "X"}),  # Message 非字符串
    ]
    monkeypatch.setattr(ark, "_openapi_call",
                        lambda cfg, action, payload: {"Result": {"Items": page}})
    items = ark.list_group_assets(CFG, "group-abc")

    assert [i["status"] for i in items] == ["failed", "failed", "active", "processing", "failed"]
    assert items[0]["error_message"] == "宽高比超出范围"
    assert items[1]["error_message"] is None
    assert items[3]["media_type"] == "video"  # AssetType 推导
    assert items[4]["error_message"] is None


def test_list_group_assets_stops_when_page_not_full(monkeypatch):
    calls = []

    def fake_call(cfg, action, payload):
        calls.append(action)
        return {"Result": {"Items": [_asset_item(i) for i in range(17)]}}

    monkeypatch.setattr(ark, "_openapi_call", fake_call)
    items = ark.list_group_assets(CFG, "group-abc")

    assert len(items) == 17
    assert len(calls) == 1


# ------------------------------------------------------------ 增删改调用

def test_delete_group_passes_payload_and_validates(monkeypatch):
    calls = []

    def fake_call(cfg, action, payload):
        calls.append((action, payload))
        return {}

    monkeypatch.setattr(ark, "_openapi_call", fake_call)
    ark.delete_group(CFG, "group-abc")
    assert calls == [("DeleteAssetGroup",
                      {"Id": "group-abc", "ProjectName": "Seedance2.0"})]

    with pytest.raises(ValueError):
        ark.delete_group(CFG, "bad id!")
    assert len(calls) == 1  # 非法 id 不发请求


def test_rename_group_and_update_asset_shapes(monkeypatch):
    calls = []
    monkeypatch.setattr(ark, "_openapi_call",
                        lambda cfg, action, payload: calls.append((action, payload)) or {})
    ark.rename_group(CFG, "group-abc", "  新名字  ")
    assert calls[-1] == ("UpdateAssetGroup",
                         {"Id": "group-abc", "Name": "新名字", "ProjectName": "Seedance2.0"})
    ark.update_asset(CFG, "asset-123", "新人像")
    assert calls[-1] == ("UpdateAsset",
                         {"Id": "asset-123", "Name": "新人像", "ProjectName": "Seedance2.0"})

    with pytest.raises(ValueError):
        ark.rename_group(CFG, "bad id!", "名字")
    with pytest.raises(ValueError):
        ark.update_asset(CFG, "asset-123", "   ")


def test_create_group_cleans_name_and_returns_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(ark, "_openapi_call",
                        lambda cfg, action, payload: seen.update(payload) or {"Result": {"Id": "group-new"}})
    assert ark.create_group(CFG, "  人像 A  ") == "group-new"
    assert seen == {"Name": "人像 A", "ProjectName": "Seedance2.0", "GroupType": "AIGC"}

    monkeypatch.setattr(ark, "_openapi_call",
                        lambda cfg, action, payload: seen.update(payload) or {"Result": {"Id": "group-new"}})
    ark.create_group(CFG, "x" * 100)
    assert len(seen["Name"]) == 64  # 超长截断

    with pytest.raises(ValueError):
        ark.create_group(CFG, "   ")
    with pytest.raises(ValueError):
        ark.create_group(CFG, "  \x00\x1f  ")  # 控制字符清洗后为空


# --------------------------------------------------------------- get_asset_url

def test_get_asset_url_requires_nonempty_url(monkeypatch):
    monkeypatch.setattr(ark, "_openapi_call",
                        lambda cfg, action, payload: {"Result": {"Id": "asset-1", "URL": ""}})
    with pytest.raises(ark.LibraryInvalid):
        ark.get_asset_url(CFG, "asset-1")

    monkeypatch.setattr(ark, "_openapi_call",
                        lambda cfg, action, payload: {"Result": {"Id": "asset-1", "URL": "https://x/y.png"}})
    assert ark.get_asset_url(CFG, "asset-1") == "https://x/y.png"

    with pytest.raises(ValueError):
        ark.get_asset_url(CFG, "bad id")


# ----------------------------------------------------------- upload_image

def test_upload_image_validates_explicit_group_id(monkeypatch, tmp_path):
    path = tmp_path / "t.png"
    path.write_bytes(b"\x89PNG" + b"x" * 16)
    size = path.stat().st_size
    # 非法组 id 在发任何请求之前就拒绝
    with pytest.raises(ValueError):
        ark.upload_image(CFG, str(path), "image/png", size, "t.png", group_id="bad id!")
