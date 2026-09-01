"""无限画布子应用 —— FastAPI 入口。

由 Portal 通过共享 .venv 的 uvicorn 启动（portal/app.py:641-657）：
    uvicorn app_fastapi:app --host 127.0.0.1 --port 8893
需要 launchd plist 里设置 INFINITE_CANVAS_ENGINE=fastapi。

边界：只监听回环；身份来自 Portal 的 HMAC 签名头；不做 CSRF
（X-CSRF-Token 不在 Portal 转发白名单内，做了必然全挂）。
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

import ark_library
import comfy_api
import store
import translate
from portal_identity import verify_portal_identity

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# 上限沿用画布上游默认值；Portal 自身上限 200MB（portal/app.py:1293），我们更严格。
# image 10MB→32MB：previz 标注 JPEG 烘焙后（20MB 原图 × JPEG 0.92 保真）避免 413
MAX_UPLOAD = {"image": 32 * 1024 * 1024, "video": 64 * 1024 * 1024, "audio": 32 * 1024 * 1024}
_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
}
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_GROUP_NAME_MAX = 64
_ASSET_NAME_MAX = 64

app = FastAPI(title="infinite-canvas", docs_url=None, redoc_url=None, openapi_url=None)


def _error(status: int, code: str, message: str, *, retryable: bool = False,
           phase: str = "request") -> JSONResponse:
    """错误体形状对齐前端 ApiError（web/src/api/contracts.ts:19）。"""
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "request_id": "canvas", "phase": phase},
    )


# ------------------------------------------------------------ 素材库公用

def _require_library_cfg() -> dict | None:
    """素材库未配置时返回 None，调用方按 503 LIBRARY_ASSETS_UNAVAILABLE 处理。"""
    return ark_library.load_config()


def _library_error(exc: Exception) -> JSONResponse:
    """素材库调用异常 → 对外错误体（沿用既有 upload/delete 分支的映射）。"""
    if isinstance(exc, ValueError):
        return _error(422, "UPSTREAM_UNAVAILABLE", str(exc), phase="library")
    if isinstance(exc, ark_library.LibraryError):
        return _error(502 if exc.retryable else 422, "UPSTREAM_UNAVAILABLE",
                      str(exc), retryable=exc.retryable, phase="library")
    if isinstance(exc, ark_library.LibraryInvalid):
        return _error(502, "UPSTREAM_INVALID", str(exc), phase="library")
    raise exc


def _library_unavailable() -> JSONResponse:
    return _error(503, "LIBRARY_ASSETS_UNAVAILABLE", "素材库未配置。",
                  retryable=True, phase="library")


def _is_default_group(group_id: str) -> bool:
    """当前持久化的默认组不可改名/删除，避免本地 JSON 悬空。"""
    return group_id == ark_library._group_id()


def _library_group_name(value: object) -> str | None:
    """校验组名：必填、strip 后 1..64。合法返回清洗后的名字，否则 None。"""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not (1 <= len(name) <= _GROUP_NAME_MAX):
        return None
    return name


def _library_asset_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not (1 <= len(name) <= _ASSET_NAME_MAX):
        return None
    return name


def _local_payload(row: dict) -> dict:
    """本地行 → 前端 join 用的字段子集（含 content_url 与上游 id）。"""
    return {"asset_id": row["asset_id"], "kind": row["kind"], "status": row["status"],
            "media_type": row["media_type"], "mime_type": row["mime_type"],
            "size_bytes": int(row["size_bytes"]),
            "content_url": f"/api/v1/assets/{row['asset_id']}/content",
            "upstream_asset_id": row.get("upstream_asset_id")}


def _local_by_upstream(user_id: str, upstream_ids: list[str]) -> dict:
    """本人 kind=library 本地行按 upstream_asset_id 索引（9645ab1：只有
    kind=library 的行才有上游 id 可回填，reference/portrait 保持不透明）。"""
    if not upstream_ids:
        return {}
    wanted = set(upstream_ids)
    return {
        row["upstream_asset_id"]: row
        for row in store.list_library_assets(user_id)
        if row.get("upstream_asset_id") in wanted
    }


@app.on_event("startup")
def _startup() -> None:
    # 建表很快；不要在启动路径做重活 —— Portal 看门狗只给约 45 秒
    # （portal/app.py:685-715 每 15s 探活、连续 3 次失败即重启）。
    store.init_schema()


def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


@app.middleware("http")
async def identity_boundary(request: Request, call_next):
    path = request.url.path
    # Portal 的统计轮询是服务端到服务端直连子应用（portal/app.py:986-987 用裸
    # HTTPConnection），**不经代理、不注入任何签名头**。这个兼容路由专供它使用，
    # 因此只能以「来源必须是回环」为边界，不能要求签名。
    # 它返回的字段仅任务状态与计数，不含用户名或素材内容。
    # 画布前端用的是 /api/v1/jobs/{id}，仍然要求签名。
    if path.startswith("/api/jobs/"):
        if not _is_loopback(request):
            return _error(404, "not_found", "资源不存在。")
        return await call_next(request)
    # 静态资源不校验身份：Portal 代理层已要求登录（portal/app.py:1980 检查 use_apps），
    # 这里重复校验只会让 SPA 白屏难排查。
    if path.startswith("/api/"):
        user = verify_portal_identity(request.headers)
        if user is None:
            return _error(401, "unauthorized", "请通过 Portal 访问。", phase="authentication")
        request.state.user = user
    return await call_next(request)


# ------------------------------------------------------------------ session

@app.get("/api/v1/session")
async def get_session(request: Request):
    user = request.state.user
    # user_id 决定前端 IndexedDB 库名 ai-creation-canvas:<env>:<userId>
    # （web/src/storage/scope.ts:25-27），必须稳定 —— 不能用 username 代替。
    return {"user_id": user["user_id"], "username": user["username"], "role": user["role"]}


# ----------------------------------------------------------------- projects

@app.get("/api/v1/projects")
async def api_list_projects(request: Request):
    return {"projects": store.list_projects(request.state.user["user_id"])}


@app.post("/api/v1/projects")
async def api_create_project(request: Request):
    user = request.state.user
    document = await request.json()
    if not isinstance(document, dict) or not _PROJECT_ID_RE.match(str(document.get("id") or "")):
        return _error(400, "invalid_request", "画布标识无效。")
    try:
        envelope, created = store.create_project(user["user_id"], document)
    except store.DocumentTooLarge as exc:
        return _error(413, "document_too_large", str(exc))
    except store.ConflictError:
        return _error(409, "PROJECT_CONFLICT", "画布已存在且内容不同。")
    return JSONResponse(status_code=201 if created else 200, content=envelope)


@app.get("/api/v1/projects/{project_id}")
async def api_get_project(request: Request, project_id: str):
    try:
        return store.get_project(request.state.user["user_id"], project_id)
    except store.NotFoundError:
        return _error(404, "not_found", "画布不存在。")


@app.put("/api/v1/projects/{project_id}")
async def api_update_project(request: Request, project_id: str):
    payload = await request.json()
    if not isinstance(payload, dict):
        return _error(400, "invalid_request", "请求体无效。")
    expected = payload.pop("expected_version", None)
    if not isinstance(expected, int):
        return _error(400, "invalid_request", "缺少版本号。")
    try:
        return store.update_project(request.state.user["user_id"], project_id, payload, expected)
    except store.DocumentTooLarge as exc:
        return _error(413, "document_too_large", str(exc))
    except store.ConflictError:
        # code 必须精确等于 PROJECT_CONFLICT —— 前端靠它触发“冲突副本”分叉
        # （web/src/features/projects/project-sync.ts:62-65,297），拼错会丢改动。
        return _error(409, "PROJECT_CONFLICT", "画布已被其他窗口修改。")


@app.delete("/api/v1/projects/{project_id}", status_code=204)
async def api_delete_project(request: Request, project_id: str):
    store.delete_project(request.state.user["user_id"], project_id)
    return Response(status_code=204)


# ------------------------------------------------------------------- assets

@app.post("/api/v1/assets")
async def api_upload_asset(request: Request, file: UploadFile = File(...),
                           media_type: str = Form(...), kind: str = Form("reference"),
                           group_id: str = Form(None)):
    user = request.state.user
    if media_type not in ("image", "video", "audio"):
        return _error(400, "invalid_request", "媒体类型无效。")
    # 素材库（kind=library）只收人像图：与上游一致（ark_assets._IMAGE_MIMES），
    # 生成的视频要拿它做资产引用，视频/音频进不了 AIGC 素材库。
    if kind not in ("reference", "library"):
        return _error(400, "invalid_request", "资产类型无效。")
    if kind == "library" and media_type != "image":
        return _error(400, "invalid_request", "素材库只支持图片。")
    if kind == "library" and group_id is not None and _GROUP_ID_RE.fullmatch(group_id) is None:
        return _error(400, "invalid_request", "素材组标识无效。")
    payload = await file.read()
    if not payload:
        # 前端要求 size_bytes >= 1（web/src/api/assets.ts:24），空文件必须拒绝。
        return _error(400, "invalid_request", "文件为空。")
    if len(payload) > MAX_UPLOAD[media_type]:
        return _error(413, "file_too_large", "文件超过大小上限。")

    # mime 自行推导：前端严格校验 mime_type 必须以 "<media_type>/" 开头
    # （web/src/api/assets.ts:23），不能直接信客户端的 Content-Type。
    guessed = mimetypes.guess_type(file.filename or "")[0] or ""
    mime = guessed if guessed.startswith(f"{media_type}/") else f"{media_type}/octet-stream"
    if not guessed.startswith(f"{media_type}/"):
        declared = (file.content_type or "")
        mime = declared if declared.startswith(f"{media_type}/") else mime

    asset_id = secrets.token_urlsafe(16).replace("=", "")
    # 绝不用客户端文件名做路径（目录穿越）；扩展名由 mime 白名单反查。
    ext = _EXT_BY_MIME.get(mime, ".bin")
    user_dir = store.UPLOAD_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", user["user_id"])[:64]
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{asset_id}{ext}"
    path.write_bytes(payload)

    if kind == "library":
        if mime not in ("image/png", "image/jpeg", "image/webp"):
            path.unlink(missing_ok=True)
            return _error(400, "invalid_request", "素材库只支持 32MB 以内的 PNG/JPEG/WebP 图片。")
        cfg = ark_library.load_config()
        if cfg is None:
            # code 与上游一致，前端统一走「上传失败，请重试」的提示。
            path.unlink(missing_ok=True)
            return _error(503, "LIBRARY_ASSETS_UNAVAILABLE", "素材库未配置。",
                          retryable=True, phase="library")
        # 传方舟是同步串行（TOS PUT + CreateAsset + GetAsset 轮询），
        # 放到线程池跑，别阻塞事件循环。本地副本无论如何都保留 ——
        # 画布渲染与生成取字节走本地文件，不依赖方舟的可达性。
        try:
            upstream_id, status = await asyncio.to_thread(
                ark_library.upload_image, cfg, str(path), mime, len(payload),
                file.filename or "portrait.png", group_id)
        except ValueError as exc:
            path.unlink(missing_ok=True)
            return _error(400, "invalid_request", str(exc))
        except ark_library.LibraryError as exc:
            path.unlink(missing_ok=True)
            return _error(502 if exc.retryable else 422, "UPSTREAM_UNAVAILABLE",
                          str(exc), retryable=exc.retryable, phase="library")
        except ark_library.LibraryInvalid as exc:
            path.unlink(missing_ok=True)
            return _error(502, "UPSTREAM_INVALID", str(exc), phase="library")
        row = store.insert_asset(user["user_id"], asset_id, media_type, mime,
                                 len(payload), str(path), kind="library",
                                 status=status, service_id="ark-video",
                                 upstream_asset_id=upstream_id)
        # 前端 libraryAssetFromResponse 要求 content_url（web/src/api/assets.ts）。
        return {**row, "content_url": f"/api/v1/assets/{asset_id}/content"}

    row = store.insert_asset(user["user_id"], asset_id, media_type, mime, len(payload), str(path))
    # 前端现在要求上传响应必须带 content_url（web/src/api/assets.ts:37-44
    # ownedAssetFromResponse），存裸路径 —— 挂载前缀由前端 safeApiPath 处理。
    return {**row, "content_url": f"/api/v1/assets/{asset_id}/content"}


@app.get("/api/v1/assets/{asset_id}")
async def api_get_asset(request: Request, asset_id: str):
    if not _ASSET_ID_RE.match(asset_id):
        return _error(400, "invalid_request", "资产标识无效。")
    try:
        row = store.get_asset(request.state.user["user_id"], asset_id)
    except store.NotFoundError:
        return _error(404, "not_found", "资产不存在。")
    # 素材库资产还在方舟侧 Processing 时刷新状态（前端面板 5s 轮询走这里，
    # web/src/components/canvas/asset-library-panel.tsx settleProcessing）。
    if row["kind"] == "library" and row["status"] == "processing":
        cfg = ark_library.load_config()
        if cfg is not None and row.get("upstream_asset_id"):
            try:
                status = await asyncio.to_thread(
                    ark_library.get_asset_status, cfg, row["upstream_asset_id"])
            except (ark_library.LibraryError, ark_library.LibraryInvalid):
                status = "processing"
            updated = store.update_asset_status(request.state.user["user_id"], asset_id, status)
            if updated is not None:
                row = updated
    return {"asset_id": row["asset_id"], "kind": row["kind"], "status": row["status"],
            "media_type": row["media_type"], "mime_type": row["mime_type"],
            "size_bytes": int(row["size_bytes"]),
            "content_url": f"/api/v1/assets/{asset_id}/content",
            "upstream_asset_id": row.get("upstream_asset_id")}


@app.get("/api/v1/library-assets")
async def api_list_library_assets(request: Request):
    rows = store.list_library_assets(request.state.user["user_id"])
    return {"assets": [
        {"asset_id": r["asset_id"], "kind": "library", "status": r["status"],
         "media_type": r["media_type"], "mime_type": r["mime_type"],
         "size_bytes": int(r["size_bytes"]),
         "content_url": f"/api/v1/assets/{r['asset_id']}/content",
         "upstream_asset_id": r.get("upstream_asset_id")}
        for r in rows
    ]}


@app.get("/api/v1/assets/{asset_id}/content")
@app.head("/api/v1/assets/{asset_id}/content")
async def api_asset_content(request: Request, asset_id: str):
    if not _ASSET_ID_RE.match(asset_id):
        return _error(400, "invalid_request", "资产标识无效。")
    try:
        row = store.get_asset(request.state.user["user_id"], asset_id)
    except store.NotFoundError:
        return _error(404, "not_found", "资产不存在。")
    path = Path(row["path"])
    if not path.is_file():
        return _error(404, "not_found", "资产文件缺失。")
    # FileResponse 自动处理 Range —— <video> 需要 Range 拿 metadata 才能画首帧。
    return FileResponse(str(path), media_type=row["mime_type"])


@app.delete("/api/v1/assets/{asset_id}", status_code=204)
async def api_delete_asset(request: Request, asset_id: str):
    try:
        row = store.get_asset(request.state.user["user_id"], asset_id)
    except store.NotFoundError:
        return _error(404, "not_found", "资产不存在。")
    # 素材库资产双删（上游 895068a）：先删方舟侧，再删本地行与文件。
    # 方舟删除失败时不删本地，避免「上游还留着但本地看不到」的悬空素材。
    if row["kind"] == "library" and row.get("upstream_asset_id"):
        cfg = ark_library.load_config()
        if cfg is None:
            return _error(503, "LIBRARY_ASSETS_UNAVAILABLE", "素材库未配置。",
                          retryable=True, phase="library")
        try:
            await asyncio.to_thread(
                ark_library.delete_asset, cfg, row["upstream_asset_id"])
        except ValueError as exc:
            return _error(422, "UPSTREAM_UNAVAILABLE", str(exc), phase="library")
        except ark_library.LibraryError as exc:
            return _error(502 if exc.retryable else 422, "UPSTREAM_UNAVAILABLE",
                          str(exc), retryable=exc.retryable, phase="library")
        except ark_library.LibraryInvalid as exc:
            return _error(502, "UPSTREAM_INVALID", str(exc), phase="library")
    store.delete_asset(request.state.user["user_id"], asset_id)
    return Response(status_code=204)


# -------------------------------------------------------- 素材库分组体系
# 上游 96878f1/dc5cb6a/b59accb/7c25820/d13711d/9645ab1/5ad0d58/141a68c。
# 所有端点都要 request.state.user（identity_boundary 已保证 /api/ 有签名），
# 素材库未配置统一 503 LIBRARY_ASSETS_UNAVAILABLE。

@app.get("/api/v1/library-groups")
async def api_list_library_groups(request: Request):
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        groups = await asyncio.to_thread(ark_library.list_groups, cfg)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)
    # 组 id 再按本地白名单校验一道才输出（防御上游返回脏数据）。
    return {"ok": True, "groups": [
        group for group in groups
        if _GROUP_ID_RE.fullmatch(str(group.get("group_id") or "")) is not None
    ]}


@app.post("/api/v1/library-groups")
async def api_create_library_group(request: Request):
    payload = await request.json()
    name = _library_group_name(payload.get("name") if isinstance(payload, dict) else None)
    if name is None:
        return _error(400, "invalid_request", "素材组名字无效。")
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        group_id = await asyncio.to_thread(ark_library.create_group, cfg, name)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)
    if _GROUP_ID_RE.fullmatch(group_id) is None:
        return _error(502, "UPSTREAM_INVALID", "素材库分组响应无效。", phase="library")
    return {"ok": True, "group_id": group_id}


@app.put("/api/v1/library-groups/{group_id}")
async def api_rename_library_group(request: Request, group_id: str):
    if _GROUP_ID_RE.fullmatch(group_id) is None:
        return _error(400, "invalid_request", "素材组标识无效。")
    payload = await request.json()
    name = _library_group_name(payload.get("name") if isinstance(payload, dict) else None)
    if name is None:
        return _error(400, "invalid_request", "素材组名字无效。")
    if _is_default_group(group_id):
        return _error(409, "GROUP_PROTECTED", "当前默认素材组不可改名。", phase="library")
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        await asyncio.to_thread(ark_library.rename_group, cfg, group_id, name)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)
    return {"ok": True}


@app.delete("/api/v1/library-groups/{group_id}")
async def api_delete_library_group(request: Request, group_id: str):
    if _GROUP_ID_RE.fullmatch(group_id) is None:
        return _error(400, "invalid_request", "素材组标识无效。")
    if _is_default_group(group_id):
        return _error(409, "GROUP_PROTECTED", "当前默认素材组不可删除。", phase="library")
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        await asyncio.to_thread(ark_library.delete_group, cfg, group_id)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)
    # 本地行不动：画布已引用的素材照常渲染/生成（本地字节还在）。
    return {"ok": True}


@app.get("/api/v1/library-groups/{group_id}/assets")
async def api_list_library_group_assets(request: Request, group_id: str):
    if _GROUP_ID_RE.fullmatch(group_id) is None:
        return _error(400, "invalid_request", "素材组标识无效。")
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        assets = await asyncio.to_thread(ark_library.list_group_assets, cfg, group_id)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)
    local_by_up = _local_by_upstream(request.state.user["user_id"],
                                     [a["asset_id"] for a in assets])
    return {"ok": True, "assets": [
        {**asset,
         "local": _local_payload(local_by_up[asset["asset_id"]])
         if asset["asset_id"] in local_by_up else None}
        for asset in assets
    ]}


@app.put("/api/v1/library-groups/{group_id}/assets/{asset_id}")
async def api_rename_library_group_asset(request: Request, group_id: str, asset_id: str):
    if _GROUP_ID_RE.fullmatch(group_id) is None or _ASSET_ID_RE.fullmatch(asset_id) is None:
        return _error(400, "invalid_request", "素材标识无效。")
    payload = await request.json()
    name = _library_asset_name(payload.get("name") if isinstance(payload, dict) else None)
    if name is None:
        return _error(400, "invalid_request", "素材名字无效。")
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        await asyncio.to_thread(ark_library.update_asset, cfg, asset_id, name)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)
    return {"ok": True}


@app.delete("/api/v1/library-groups/{group_id}/assets/{asset_id}")
async def api_delete_library_group_asset(request: Request, group_id: str, asset_id: str):
    if _GROUP_ID_RE.fullmatch(group_id) is None or _ASSET_ID_RE.fullmatch(asset_id) is None:
        return _error(400, "invalid_request", "素材标识无效。")
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        await asyncio.to_thread(ark_library.delete_asset, cfg, asset_id)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)
    # 本人有该上游素材的本地行则一并删（含本地文件）；没有就算了。
    user_id = request.state.user["user_id"]
    for row in store.list_library_assets(user_id):
        if row.get("upstream_asset_id") == asset_id:
            try:
                store.delete_asset(user_id, row["asset_id"])
            except store.NotFoundError:
                pass
            break
    return {"ok": True}


@app.get("/api/v1/library-groups/{group_id}/assets/{asset_id}/content")
async def api_library_group_asset_content(request: Request, group_id: str, asset_id: str):
    """上游素材的同源流式代理（d13711d）。

    方舟返回的 URL 在外部媒体桶，被本应用 CSP（img-src 'self'）与客户
    局域网出网策略挡住；代理让缩略图同源，且每次请求重新取预签名 URL。
    """
    if _GROUP_ID_RE.fullmatch(group_id) is None or _ASSET_ID_RE.fullmatch(asset_id) is None:
        return _error(400, "invalid_request", "素材标识无效。")
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        url = await asyncio.to_thread(ark_library.get_asset_url, cfg, asset_id)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)

    import httpx
    client = httpx.AsyncClient(timeout=httpx.Timeout(120), follow_redirects=True,
                               trust_env=False)
    try:
        stream_context = client.stream("GET", url)
        upstream_response = await stream_context.__aenter__()
    except httpx.HTTPError:
        await client.aclose()
        return _error(502, "UPSTREAM_UNAVAILABLE", "素材库服务不可用。",
                      retryable=True, phase="library")
    if upstream_response.status_code != 200:
        await stream_context.__aexit__(None, None, None)
        await client.aclose()
        return _error(502, "UPSTREAM_UNAVAILABLE", "素材库服务不可用。",
                      retryable=True, phase="library")
    content_type = upstream_response.headers.get(
        "content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
    if not content_type.startswith(("image/", "video/", "audio/", "application/octet-stream")):
        await stream_context.__aexit__(None, None, None)
        await client.aclose()
        return _error(502, "UPSTREAM_INVALID", "素材库返回了无法解析的响应。",
                      phase="library")

    async def body():
        try:
            async for chunk in upstream_response.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await stream_context.__aexit__(None, None, None)
            await client.aclose()

    return StreamingResponse(
        body(),
        media_type=content_type,
        headers={"Cache-Control": "max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/v1/library-groups/{group_id}/assets/{asset_id}/import")
async def api_import_library_group_asset(request: Request, group_id: str, asset_id: str):
    """物化上游独有素材为本地行（7c25820）。

    方舟侧用 TOS Browser 等工具直接传的素材没有本地行，画布加不了；
    这里下载字节落 state/uploads 用户目录，登记 kind=library 本地行，
    之后走既有 addToCollection 流程。
    """
    if _GROUP_ID_RE.fullmatch(group_id) is None or _ASSET_ID_RE.fullmatch(asset_id) is None:
        return _error(400, "invalid_request", "素材标识无效。")
    user = request.state.user
    cfg = _require_library_cfg()
    if cfg is None:
        return _library_unavailable()
    try:
        url = await asyncio.to_thread(ark_library.get_asset_url, cfg, asset_id)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid) as exc:
        return _library_error(exc)
    try:
        status = await asyncio.to_thread(ark_library.get_asset_status, cfg, asset_id)
    except (ValueError, ark_library.LibraryError, ark_library.LibraryInvalid):
        status = "processing"

    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180), follow_redirects=True,
                                     trust_env=False) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return _error(502, "UPSTREAM_UNAVAILABLE", "素材库服务不可用。",
                      retryable=True, phase="library")
    if response.status_code != 200:
        return _error(502, "UPSTREAM_UNAVAILABLE", "素材库服务不可用。",
                      retryable=True, phase="library")
    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    ext = _EXT_BY_MIME.get(mime)
    if ext is None:
        return _error(422, "UPSTREAM_UNAVAILABLE", "素材库内容类型不支持。", phase="library")
    media_type = mime.split("/", 1)[0] if mime.startswith(("image/", "video/", "audio/")) else "image"
    body = response.content
    if not body or len(body) > MAX_UPLOAD.get(media_type, 0):
        return _error(413, "file_too_large", "素材超过大小上限。")

    local_id = secrets.token_urlsafe(16).replace("=", "")
    user_dir = store.UPLOAD_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", user["user_id"])[:64]
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{local_id}{ext}"
    try:
        path.write_bytes(body)
    except OSError:
        return _error(502, "UPSTREAM_UNAVAILABLE", "素材写入失败，请重试。",
                      retryable=True, phase="library")
    row = store.insert_asset(user["user_id"], local_id, media_type, mime, len(body),
                             str(path), kind="library", status=status,
                             service_id="ark-video", upstream_asset_id=asset_id)
    return {**row, "content_url": f"/api/v1/assets/{local_id}/content"}


# ----------------------------------------------------------------- activity

@app.get("/api/v1/activity/assets")
async def api_activity_assets(request: Request):
    rows = store.list_assets(request.state.user["user_id"])
    return {"assets": [
        {"asset_id": r["asset_id"], "media_type": r["media_type"], "mime_type": r["mime_type"],
         "size_bytes": int(r["size_bytes"]), "created_at": r["created_at"]}
        for r in rows
    ]}


@app.get("/api/v1/activity/jobs")
async def api_activity_jobs(request: Request):
    jobs = store.list_jobs(request.state.user["user_id"])
    return {"jobs": [
        {"job_id": j["job_id"], "operation": j["operation"], "status": j["status"],
         "created_at": j["created_at"]}
        for j in jobs
    ]}


@app.get("/api/v1/prompt-skills")
async def api_prompt_skills():
    # 提示词优化 Skill 需要额外的文本模型接入，当前不提供。
    return {"skills": []}


# --------------------------------------------------------- models / jobs

app.include_router(translate.router)
app.include_router(comfy_api.router)


@app.exception_handler(comfy_api._HTTP)
async def _comfy_http_handler(request: Request, exc):
    return JSONResponse(status_code=exc.status, content={
        "code": exc.code, "message": exc.message, "retryable": exc.retryable,
        "request_id": "canvas", "phase": "request"})


# ------------------------------------------------------------ static / SPA

@app.get("/{requested_path:path}")
async def static_or_spa(requested_path: str):
    """静态资源，找不到则回退到 index.html（react-router 是 browser history 模式）。"""
    index = STATIC_DIR / "index.html"
    if requested_path:
        # 拒绝穿越：解析后必须仍在 STATIC_DIR 内，且是常规文件（挡符号链接）。
        candidate = (STATIC_DIR / requested_path).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return _error(404, "not_found", "资源不存在。")
        if candidate.is_file() and not candidate.is_symlink():
            return FileResponse(str(candidate))
    if not index.is_file():
        return _error(404, "not_found", "前端尚未构建，请运行 build.sh。")
    return FileResponse(str(index), media_type="text/html")
