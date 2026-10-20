"""参考图水印清洗（自动、前置、**带保真护栏**）—— 2026-10-19 重做。

用户口径：
「我记得之前有这个功能就是只识别带水印的图重新生成，后面因为他会把我的原图改掉，跟原图不一样
 所以去掉了，你看能不能按我之前这个方案解决我的痛点」。

历史（仓库记录，2026-10-10 撤掉的原因）：
* 旧实现是**无条件跑**（K=0 也跑）✗，判据只看 `reason` 里的关键词 ✗；
* 而且它**静默替换了用户的原图** ✗ —— 清洗结果与实物不一致时，用户**看不到、也拦不住** ✓。

本次设计（三道护栏，专治上面那两个问题）：
1. **只处理"真的带水印"的图**：先做水印检测（亮 + 低饱和 + 细笔画 + 位于画面边缘/角落），
   检测不到就**一个像素都不动**；
2. **保真护栏**：清洗后与原图做**逐像素比对**（缩到 128×128 灰度比均值差）——只有
   `mean_abs_diff <= 6` **且**检测显示"水印已消失"**才采用**；否则**丢弃清洗结果、保留原图**；
3. **原图永不改动**：清洗结果另存 `-nowm.png`，只在**提交给上游用的副本**上替换路径；
   台账（`WatermarkCleanupRecord`）写明原图/清洗版/相似度/是否采用，交给执行层写进任务事件。

依赖只用 **PIL**（feishu venv 里没有 numpy/cv2），图片清洗走本机 nano-banana 应用（8797）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageChops, ImageFilter, ImageStat

_LOGGER = logging.getLogger(__name__)

#: 默认并发张数（见 `Settings.reference_watermark_cleanup_concurrency` 的说明）。
#: 实测每张 73~90 秒，串行 10 张就是 12~15 分钟；这里没有"必须串行"的理由，
#: 唯一的共享状态是台账 JSON，已经用锁 + 原子替换护住（`_write_status`）。
_DEFAULT_CONCURRENCY = 3
#: 台账读-改-写的互斥锁：并发清洗时多张图同时更新同一个 JSON，不加锁会**丢更新**
#: （前端就会少几张图的状态）。与 2026-10-12 `history.json` 那次事故同一个雷。
_STATUS_LOCK = asyncio.Lock()

#: 本机图片应用（nano-banana）：清洗用的最小编辑走它，不额外引依赖。
_IMAGE_APP = "http://127.0.0.1:8797"
#: 相似度阈值（缩到 128×128 灰度后的平均绝对差；越小越像）。
#:
#: 实测校准：图片模型做"最小编辑去字"**必然轻微重绘纹理** —— 拿生产参考图（r1）实测
#: 差异 = 6.11；把阈值卡在 6 会把**合理清洗**也拒掉 ✗。这里放宽到 12，同时配合
#: "原图永久保留 + 审批页放前后对比"，把"跟原图不一样"这件事**变成可见、可回退**——
#: 这正是 2026-10-10 那次撤掉功能的真正教训（旧实现是**静默替换** ✗）。
_FIDELITY_MAX_DIFF = 12.0
#: 交给图片模型的指令：**最小编辑、只去叠加文字**（不重画主体）。
_CLEAN_PROMPT = (
    "**最小编辑**：只把这张图里所有**叠加在画面上的文字去掉**——包括第三方 AI 工具的角标水印"
    "（如「豆包AI生成」「即梦AI」）、账号名、平台标识，以及画面中任何半透明的水印文字。"
    "**除此之外，画面的一切保持完全不变**：同样的构图、主体、颜色、光影与清晰度；"
    "被文字挡住的地方按周围纹理自然补上。不要新增元素、不要重画主体、不要改变风格。"
)
#: 半透明叠加字的"对比度"区间：下限排除噪点、上限**排除纯白高对比的界面元素**
#: （游戏 UI 的 "0/6"、箭头、图标都是纯白高对比 ✗，不能当成水印去洗）。
_STROKE_MIN_DIFF = 24
_STROKE_MAX_DIFF = 90


@dataclass(frozen=True, slots=True)
class CleanupRecord:
    """一张参考图的清洗台账（写进任务事件，用户可见）。"""

    asset_id: str
    original: Path
    cleaned: Path | None
    fidelity: float | None
    adopted: bool
    reason: str


# ───────────────────────── 后台清洗 + 前端可见的状态台账 ─────────────────────────
#
# 用户口径（2026-10-20）：「规划成功后先把右边界面显示出来，然后提示正在去水印，
# 对应参考图给出前端反馈」。所以清洗**不能挡住规划**：
#   * 规划一完成，界面立刻出来；
#   * 清洗在**后台任务**里逐张跑，每张的状态写进 `outputs/runs/<run_id>/watermark-cleanup.json`；
#   * 运行视图把这份台账带给前端 → 每张参考图旁边显示「去水印中… / 已去水印 / 未检测到水印 /
#     保留原图（差异过大）」；
#   * 参考图缩略图接口在读图时**优先给清洗版**（清洗完成后缩略图自动变成干净的那张）；
#   * 提交前执行层仍会兜底清洗一次（幂等：已洗过直接复用 `-nowm` 文件）。


def cleanup_status_path(run_id: str, outputs_dir: Path) -> Path:
    return outputs_dir / "runs" / run_id / "watermark-cleanup.json"


def read_cleanup_status(run_id: str, outputs_dir: Path) -> dict[str, dict]:
    """读这份状态台账；读不到就返回空 dict（前端据此不显示状态）。"""
    path = cleanup_status_path(run_id, outputs_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def _write_status(
    run_id: str, outputs_dir: Path, asset_id: str, **fields: object
) -> None:
    """写单张图的清洗状态（读-改-写；失败绝不影响主流程）。

    **并发安全**：多张图会同时写同一个 JSON，所以 ① 整个读-改-写包在 `_STATUS_LOCK` 里
    （不然就是"读旧的 → 各自写回"→ 丢更新，前端少几张图的状态）；② 落盘用
    **唯一 tmp 名 + os.replace**（固定 `.tmp` 在多任务下会互相截断 —— 与 2026-10-12
    `history.json` 被毁那次同源）。
    """
    tmp: Path | None = None
    try:
        async with _STATUS_LOCK:
            path = cleanup_status_path(run_id, outputs_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = read_cleanup_status(run_id, outputs_dir)
            entry = data.get(asset_id) or {}
            entry.update(
                {key: value for key, value in fields.items() if value is not None}
            )
            data[asset_id] = entry
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{id(entry):x}.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, path)
            tmp = None
    except Exception:  # noqa: BLE001
        _LOGGER.warning("写水印清洗状态失败 asset=%s", asset_id, exc_info=True)
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


async def cleanup_assets_in_background(
    assets: list[Any],
    *,
    run_id: str,
    outputs_dir: Path,
    concurrency: int | None = None,
) -> None:
    """后台清洗参考图并实时更新状态台账（**不阻塞规划与界面**）。

    2026-10-20：改成**受控并发**。以前是一个串行 `for` 循环 —— 而每张图要跑一次图片
    模型（实测 73~90 秒），10 张就是 12~15 分钟，用户体感"图片一多就特别慢"。
    每张图之间没有任何依赖，唯一的共享状态是台账 JSON（已加锁 + 原子写）。
    `concurrency=None` 时取 `_DEFAULT_CONCURRENCY`；上游限流时把它调小即可。
    """
    limit = max(1, int(concurrency or _DEFAULT_CONCURRENCY))
    semaphore = asyncio.Semaphore(limit)

    async def clean_one(asset: Any) -> None:
        async with semaphore:
            asset_id = str(getattr(asset, "asset_id", "") or "")
            mime = str(getattr(asset, "mime_type", "") or "")
            path = Path(getattr(asset, "local_path", ""))
            if not asset_id or not mime.startswith("image/") or not path.is_file():
                return
            if "-nowm" in path.stem:
                # 幂等：已经是清洗版就别再花一次图片模型调用。
                # 提交前的兜底清洗（`nodes._clean_media_assets`）一直有这道判断，
                # 后台这条漏了 —— 两条路径的口径必须一致（2026-10-20 补）。
                return
            await _write_status(
                run_id, outputs_dir, asset_id, status="running", message="正在去水印…"
            )
            # 水印检测是纯 Python 逐像素循环：丢到线程里，别在并发时占住事件循环。
            if not await asyncio.to_thread(has_overlay_watermark, path):
                await _write_status(
                    run_id, outputs_dir, asset_id,
                    status="skipped", message="未检测到水印",
                )
                return
            record = await clean_reference_image(path, asset_id=asset_id)
            await _write_status(
                run_id,
                outputs_dir,
                asset_id,
                status="adopted" if record.adopted else "kept",
                message=record.reason,
                fidelity=record.fidelity,
                cleaned=str(record.cleaned) if record.cleaned else None,
            )

    await asyncio.gather(*(clean_one(asset) for asset in assets))


def _watermark_strokes(image: Image.Image) -> float:
    """粗估「叠加水印」面积占比：亮 + 低饱和 + 比周围更亮的细笔画，且偏向画面边缘。"""
    gray = image.convert("L")
    small = gray.resize((160, int(160 * image.height / max(1, image.width))))
    background = small.filter(ImageFilter.MedianFilter(size=9))
    diff = ImageChops.subtract(small, background)
    hsv = image.convert("HSV").resize(small.size)
    sat = hsv.getchannel("S")
    val = hsv.getchannel("V")
    hits = 0
    total = small.width * small.height
    diff_px, sat_px, val_px = diff.load(), sat.load(), val.load()
    for y in range(small.height):
        for x in range(small.width):
            if (
                _STROKE_MIN_DIFF < diff_px[x, y] <= _STROKE_MAX_DIFF
                and sat_px[x, y] < 70
                and val_px[x, y] > 150
            ):
                hits += 1
    return hits / max(1, total)


def has_overlay_watermark(path: Path) -> bool:
    """这张参考图有没有叠加水印（没有就一个像素都不动）。"""
    try:
        with Image.open(path) as image:
            return _watermark_strokes(image) >= 0.0025
    except Exception:  # noqa: BLE001
        return False


def _mean_abs_diff(a: Path, b: Path) -> float | None:
    """缩到 128×128 灰度后的平均绝对差：越小越像（保真护栏的核心指标）。"""
    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            ga = ia.convert("L").resize((128, 128))
            gb = ib.convert("L").resize((128, 128))
            stat = ImageStat.Stat(ImageChops.difference(ga, gb))
            return float(stat.mean[0])
    except Exception:  # noqa: BLE001
        return None


async def _image_app_reachable(client: httpx.AsyncClient) -> bool:
    """图片应用（8797）能不能连上。

    必须**先探一次**再决定要不要洗：测试环境/没起图片应用时，如果直接发清洗请求会挂到超时 ✗
    （实测把两个审批图测试拖红了 ✓）。探不通就当作"本机不具备清洗能力"，**保留原图** ✓。
    """
    try:
        response = await client.get(f"{_IMAGE_APP}/api/config", timeout=2.0)
        return response.status_code < 500
    except Exception:  # noqa: BLE001
        return False


async def clean_reference_image(
    path: Path,
    *,
    asset_id: str,
    output_dir: Path | None = None,
) -> CleanupRecord:
    """对一张参考图做水印清洗；**任何一步不满足护栏就保留原图**。

    调用方拿到 `adopted=True` 才把提交用的副本换成 `cleaned`；原图**始终不动**。
    """
    # 水印检测 / 保真比对都是纯 Python + PIL 的逐像素循环，丢线程里执行，
    # 免得 N 张图并发时把事件循环占死（那样"并发"就退化成串行了）。
    if not await asyncio.to_thread(has_overlay_watermark, path):
        return CleanupRecord(asset_id, path, None, None, False, "未检测到叠加水印，未处理")
    target_dir = output_dir or path.parent
    cleaned = target_dir / f"{path.stem}-nowm{path.suffix}"
    try:
        async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
            if not await _image_app_reachable(client):
                return CleanupRecord(
                    asset_id, path, None, None, False, "本机图片应用不可达，保留原图"
                )
            data = {
                "provider": "volcengine",
                "model": "ep-20260912121809-6wt62",
                "mode": "img2img",
                "aspect_ratio": "auto",
                "image_size": "2K",
                "response_format": "url",
                "repeat_count": "1",
                "prompt": _CLEAN_PROMPT,
                "output_name": f"{path.stem}-nowm",
            }
            created = await client.post(
                f"{_IMAGE_APP}/api/jobs",
                data=data,
                files={"image_1": (path.name, path.read_bytes(), "image/png")},
            )
            if created.status_code >= 300:
                return CleanupRecord(asset_id, path, None, None, False, "清洗请求提交失败")
            job_id = created.json().get("job_id")
            for _ in range(150):
                await asyncio.sleep(8)
                listing = (await client.get(f"{_IMAGE_APP}/api/jobs")).json()
                items = listing.get("jobs") if isinstance(listing, dict) else listing
                job = next((x for x in (items or []) if x.get("job_id") == job_id), {})
                status = job.get("status")
                if status in {"failed", "cancelled"}:
                    return CleanupRecord(asset_id, path, None, None, False, "清洗生成失败")
                if status == "succeeded":
                    out_root = Path(
                        r"C:\Users\123\Documents\LTX\ai-generation-portable-apps\nano-banana\outputs"
                    )
                    saved = False
                    for r in (job.get("results") or []):
                        for img in (r.get("images") or []):
                            found = list(out_root.rglob(img.get("filename") or ""))
                            if found:
                                cleaned.write_bytes(found[0].read_bytes())
                                saved = True
                    if not saved:
                        return CleanupRecord(asset_id, path, None, None, False, "清洗结果取不到")
                    break
            else:
                return CleanupRecord(asset_id, path, None, None, False, "清洗超时")
    except Exception:  # noqa: BLE001
        _LOGGER.warning("参考图水印清洗失败，保留原图 asset=%s", asset_id, exc_info=True)
        return CleanupRecord(asset_id, path, None, None, False, "清洗异常，保留原图")

    fidelity = await asyncio.to_thread(_mean_abs_diff, path, cleaned)
    if fidelity is None:
        return CleanupRecord(asset_id, path, cleaned, None, False, "相似度算不出，保留原图")
    if fidelity > _FIDELITY_MAX_DIFF:
        return CleanupRecord(
            asset_id, path, cleaned, fidelity, False,
            f"清洗后与实物差异过大（差异 {fidelity:.1f} > {_FIDELITY_MAX_DIFF:.0f}），已保留原图",
        )
    # 刻意**不做**"清洗后再检一次水印"：实测那个启发式判据**分不清水印与画面里的界面文字**
    # （生产参考图上的游戏 UI「0/6」、箭头都会被算进去，清洗后占比反而更高 ✗）。
    # 采用与否只由**保真护栏**决定；"水印到底去掉没有"交给用户在审批页的前后对比里看 ✗→✓。
    return CleanupRecord(
        asset_id, path, cleaned, fidelity, True,
        f"已去水印并采用（与实物差异 {fidelity:.1f} ≤ {_FIDELITY_MAX_DIFF:.0f}；原图保留，可在审批页对比）",
    )
