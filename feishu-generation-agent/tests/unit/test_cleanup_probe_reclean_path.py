"""探针在**重洗**那条路上的两个真实故障（2026-10-28）。

用户报「重新清洗了中间字还是没了」。日志实锤：

    清洗范围探针：素材准备失败 AttributeError("'types.SimpleNamespace'
    object has no attribute 'download_error'")

→ 重洗时探针每次都抛异常、返回 None → 没有保护区 → 守卫形同不存在 → 字照旧被洗掉。
（入库那条路用真实 MediaAsset，所以探针是好的 —— 只有重洗坏。）

另外日志里还有一类失败：公开图床的 presigned 链接被上游拒
（`Error while downloading: …tos-cn-beijing.volces.com/refmedia/…`），
所以探针必须有**不依赖图床**的兜底通道（内联缩图 base64）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from PIL import Image

from feishu_generation_agent.integrations import reference_watermark_clean as module
from feishu_generation_agent.integrations.cleanup_scope import (
    DeepSeekCleanupScopeProbe,
    _inline_image_part,
)


def _png(path: Path, size=(600, 800)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (150, 110, 130)).save(path)
    return path


def test_reclean_asset_stub_has_every_field_the_probe_reads() -> None:
    """**核心回归**：替身字段缺一个，重洗的探针就整条失效（守卫白装）。"""
    stub = module._asset_stub("image-2", Path("ref.png"))
    for field in ("asset_id", "mime_type", "local_path", "sha256", "download_error"):
        assert hasattr(stub, field), f"探针素材替身缺字段 {field}（重洗会整条失效）"


class _Model:
    """记录收到的消息，回一份合法的范围 JSON。"""

    def __init__(self) -> None:
        self.messages: list[object] = []

    async def ainvoke(self, messages: object, config: object = None) -> object:  # noqa: ANN001
        self.messages.append(messages)
        return json.dumps(
            {
                "watermarks": [{"label": "右下角角标", "bbox": [0.8, 0.95, 0.99, 1.0]}],
                "keep": [{"label": "品牌字 Jewel Loop", "bbox": [0.3, 0.42, 0.72, 0.6]}],
            }
        )


class _BrokenHost:
    async def upload(self, data: bytes, filename: str, mime: str) -> str:
        raise RuntimeError("图床挂了")


def test_probe_falls_back_to_inline_base64_when_host_fails(tmp_path: Path) -> None:
    """图床不可用 → 探针改走内联缩图，仍然把范围问出来（否则守卫等于没装）。"""
    source = _png(tmp_path / "inputs" / "ref.png")
    model = _Model()
    probe = DeepSeekCleanupScopeProbe(
        model, public_media_host=_BrokenHost(), data_dir=tmp_path / "data"
    )
    asset = module._asset_stub("image-2", source)

    scope = asyncio.run(probe.probe(asset))

    assert scope is not None, "图床挂了也必须能探到范围"
    assert scope.keep and scope.keep[0][0] == "品牌字 Jewel Loop"
    payload = json.dumps(model.messages[0], ensure_ascii=False)
    assert "data:image/jpeg;base64," in payload, "兜底要走内联 base64"


def test_inline_part_is_downscaled(tmp_path: Path) -> None:
    """内联图必须限长边（5.7MB 原图直接塞进请求体会被上游拒）。"""
    big = _png(tmp_path / "big.png", (1600, 2400))
    part = _inline_image_part(big, max_side=1024)
    assert part is not None
    encoded = part["image_url"]["url"].split(",", 1)[1]
    import base64

    raw = base64.b64decode(encoded)
    assert len(raw) < 400_000, f"缩图后仍太大：{len(raw)} 字节"
    with Image.open(__import__("io").BytesIO(raw)) as image:
        assert max(image.size) <= 1024


def test_probe_result_is_cached_by_sha(tmp_path: Path) -> None:
    """同一张图只问一次模型（重洗 + 后续运行都命中缓存）。"""
    source = _png(tmp_path / "inputs" / "ref.png")
    model = _Model()
    probe = DeepSeekCleanupScopeProbe(
        model, public_media_host=_BrokenHost(), data_dir=tmp_path / "data"
    )
    asset = module._asset_stub("image-2", source)

    async def scenario() -> None:
        first = await probe.probe(asset)
        second = await probe.probe(asset)
        assert first is not None and second is not None
        assert len(model.messages) == 1, "第二次应该命中缓存"

    asyncio.run(scenario())


def test_wiring_passes_the_probe_into_the_reclean_path() -> None:
    """接线守门：重洗必须带探针（这条路上次就是因为探针崩了才没护住）。"""
    import inspect

    from feishu_generation_agent.graph import runtime

    source = inspect.getsource(runtime)
    assert "scope_probe" in source
    assert "cleanup_scope_probe" in source
