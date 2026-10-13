"""实测：我方给上游的 `video_url` 到底能不能被匿名取到真实字节。

复现生产链路：用**生产同一套配置**（`.env` 里的 TOS / uguu）构造
`animation_media_host`，上传一个真实 mp4，然后**不带任何鉴权**地 GET 那个 URL，
检查：状态码 / Content-Type / Content-Length / 前 16 字节 magic / Range 支持。

这一步回答「`invalid_media` 是不是因为我们给上游的 URL 取不到有效字节」。
只上传 1 个小文件到 `refmedia/` 前缀（与生产每次生成做的事情一样）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from feishu_generation_agent.config import Settings  # noqa: E402
from feishu_generation_agent.integrations.public_media import (  # noqa: E402
    TosPublicMediaHost,
    UguuPublicMediaHost,
)

H264 = (
    ROOT
    / "data/runs/13268f86-1522-43f4-ada4-35cdfc72b9e6/inputs"
    / "752f4f4197fa196dd669a68f24f3cf1b1ffbf0aff295c02e46a8f59ea41a7bfb.mp4"
)
HEVC = (
    ROOT
    / "data/runs/PmN5dYXLEoa0gAxz3CjcnPXKnKf/inputs"
    / "59149a36afd22f2381c6284cc643241157b7322c694d8bb6b8b6c81e38c10147.mp4"
)


async def main() -> None:
    settings = Settings()
    async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
        host = UguuPublicMediaHost(client)
        kind = "uguu"
        if (
            settings.volcengine_access_key is not None
            and settings.volcengine_secret_key is not None
            and settings.tos_bucket
        ):
            host = TosPublicMediaHost(
                client,
                access_key=settings.volcengine_access_key.get_secret_value(),
                secret_key=settings.volcengine_secret_key.get_secret_value(),
                bucket=settings.tos_bucket,
                region=settings.tos_region,
            )
            kind = f"TOS bucket={settings.tos_bucket} region={settings.tos_region}"
        print("生产实际使用的公开托管：", kind)

        for label, path in (("H.264 720x1280", H264), ("HEVC 2160x3840", HEVC)):
            content = path.read_bytes()
            print("=" * 74)
            print(f"{label}  {path.name}  {len(content)} B")
            try:
                url = await host.upload(content, path.name, "video/mp4")
            except Exception as exc:  # noqa: BLE001
                print("  上传失败：", type(exc).__name__, exc)
                continue
            print("  url:", url[:150] + ("…" if len(url) > 150 else ""))
            # 匿名 GET（不带任何鉴权头）
            probe = await client.get(url)
            head = probe.content[:16] if probe.content else b""
            print(
                f"  匿名 GET -> {probe.status_code} "
                f"content-type={probe.headers.get('content-type')!r} "
                f"content-length={probe.headers.get('content-length')!r} "
                f"实际字节={len(probe.content)}"
            )
            print("  前 16 字节:", head)
            # Range
            ranged = await client.get(url, headers={"Range": "bytes=0-1023"})
            print(
                f"  Range: bytes=0-1023 -> {ranged.status_code} "
                f"实际={len(ranged.content)} "
                f"content-range={ranged.headers.get('content-range')!r}"
            )
            # 完全不带 header 的裸请求（模拟上游下载器）
            bare = await client.get(url, headers={"User-Agent": "curl/8.0"})
            print(
                f"  裸 UA GET -> {bare.status_code} 字节={len(bare.content)} "
                f"ct={bare.headers.get('content-type')!r}"
            )
            print(
                "  与源文件字节一致:",
                len(probe.content) == len(content),
                "| magic=ftyp:",
                b"ftyp" in head,
            )


if __name__ == "__main__":
    asyncio.run(main())
    sys.stdout.flush()
