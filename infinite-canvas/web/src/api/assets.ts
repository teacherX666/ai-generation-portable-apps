import { apiFetch, assetUrl, csrfTokenForRequest, safeApiPath } from "./client";
import type { AssetRef, OwnedMediaAsset } from "./contracts";
import type { GraphMediaType } from "@/features/graph/contracts";

type UploadResponse = {
    asset_id?: unknown;
    kind?: unknown;
    status?: unknown;
    media_type?: unknown;
    mime_type?: unknown;
    size_bytes?: unknown;
    upstream_asset_id?: unknown;
};

function assetFromResponse(value: unknown): AssetRef {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("媒体上传响应无效，请重试。");
    const response = value as UploadResponse;
    const id = response.asset_id;
    if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(id)
        || (response.kind !== "reference" && response.kind !== "portrait" && response.kind !== "library")
        || (response.status !== "processing" && response.status !== "active" && response.status !== "failed")
        || (response.media_type !== "image" && response.media_type !== "video" && response.media_type !== "audio")
        || typeof response.mime_type !== "string" || !response.mime_type.startsWith(`${response.media_type}/`)
        || typeof response.size_bytes !== "number" || !Number.isSafeInteger(response.size_bytes) || response.size_bytes < 1) {
        throw new Error("媒体上传响应无效，请重试。");
    }
    return {
        id,
        kind: response.kind,
        status: response.status,
        media_type: response.media_type,
        mime_type: response.mime_type,
        size_bytes: response.size_bytes,
        content_url: safeApiPath(`${assetUrl(id)}/content`),
        ...(typeof response.upstream_asset_id === "string" && response.upstream_asset_id
            ? { upstream_asset_id: response.upstream_asset_id }
            : {}),
    };
}

function ownedAssetFromResponse(value: unknown, expectedMediaType: GraphMediaType): OwnedMediaAsset {
    const asset = assetFromResponse(value);
    if (asset.kind !== "reference" || asset.status !== "active" || asset.media_type !== expectedMediaType
        || typeof asset.size_bytes !== "number" || typeof asset.content_url !== "string") {
        throw new Error("媒体上传响应无效，请重试。");
    }
    return asset as OwnedMediaAsset;
}

export async function fetchAsset(id: string) {
    const response = await apiFetch<unknown>(`/api/v1/assets/${encodeURIComponent(id)}`);
    return assetFromResponse(response);
}

export async function deleteMediaAsset(id: string): Promise<void> {
    await apiFetch<void>(assetUrl(id), { method: "DELETE" });
}

function uploadAsset(
    file: File,
    mediaType: GraphMediaType,
    kind: "reference" | "library",
    onProgress: (percent: number) => void,
    signal?: AbortSignal,
    groupId?: string,
): Promise<AssetRef> {
    if (signal?.aborted) return Promise.reject(new DOMException("The upload was cancelled.", "AbortError"));
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        const abortRequest = () => request.abort();
        const cleanup = () => signal?.removeEventListener("abort", abortRequest);
        request.open("POST", safeApiPath("/api/v1/assets"));
        request.withCredentials = true;
        request.setRequestHeader("Accept", "application/json");
        const csrf = csrfTokenForRequest();
        if (csrf) request.setRequestHeader("X-CSRF-Token", csrf);
        request.upload.addEventListener("progress", (event) => {
            if (!event.lengthComputable || event.total <= 0) return;
            onProgress(Math.max(0, Math.min(99, Math.round(event.loaded / event.total * 100))));
        });
        request.addEventListener("load", () => {
            if (request.status < 200 || request.status >= 300) {
                cleanup();
                reject(new Error("媒体上传失败，请重试。"));
                return;
            }
            try {
                const asset = kind === "library"
                    ? libraryAssetFromResponse(JSON.parse(request.responseText) as unknown, mediaType)
                    : ownedAssetFromResponse(JSON.parse(request.responseText) as unknown, mediaType);
                onProgress(100);
                cleanup();
                resolve(asset);
            } catch {
                cleanup();
                reject(new Error("媒体上传响应无效，请重试。"));
            }
        });
        request.addEventListener("error", () => { cleanup(); reject(new Error("媒体上传失败，请检查网络后重试。")); });
        request.addEventListener("abort", () => { cleanup(); reject(new DOMException("The upload was cancelled.", "AbortError")); });
        const body = new FormData();
        body.append("kind", kind);
        body.append("media_type", mediaType);
        if (groupId) body.append("group_id", groupId);
        body.append("file", file, file.name);
        signal?.addEventListener("abort", abortRequest, { once: true });
        request.send(body);
    });
}

export function uploadMediaAsset(file: File, mediaType: GraphMediaType, onProgress: (percent: number) => void = () => undefined, signal?: AbortSignal): Promise<OwnedMediaAsset> {
    return uploadAsset(file, mediaType, "reference", onProgress, signal) as Promise<OwnedMediaAsset>;
}

function libraryAssetFromResponse(value: unknown, expectedMediaType: GraphMediaType): AssetRef {
    const asset = assetFromResponse(value);
    if (asset.kind !== "library" || asset.media_type !== expectedMediaType || typeof asset.content_url !== "string") {
        throw new Error("资产库响应无效，请重试。");
    }
    return asset;
}

/** 素材库只收人像图（与后端一致）；groupId 缺省时落到默认组。 */
export function uploadLibraryAsset(file: File, groupId?: string, onProgress: (percent: number) => void = () => undefined, signal?: AbortSignal): Promise<AssetRef> {
    return uploadAsset(file, "image", "library", onProgress, signal, groupId);
}

export async function fetchLibraryAssets(): Promise<AssetRef[]> {
    const response = await apiFetch<{ assets: unknown[] }>("/api/v1/library-assets");
    if (!response || !Array.isArray(response.assets)) throw new Error("资产库响应无效，请重试。");
    return response.assets.map((item) => {
        const asset = assetFromResponse(item);
        if (asset.kind !== "library") throw new Error("资产库响应无效，请重试。");
        return asset;
    });
}

// ------------------------------------------------------------ 分组管理
// 与后端 /api/v1/library-groups* 对应。响应校验严格，不信任后端 ——
// 组 id / 素材 id 都要过白名单，非法数据直接抛错而不是渲染脏值。

export type LibraryGroup = { group_id: string; name: string; created_at?: string };

/** 组内素材的本地行 join（9645ab1：只有 kind=library 的行才有上游 id）。 */
export type LibraryGroupLocalRow = {
    asset_id: string;
    kind: "library";
    status: "processing" | "active" | "failed";
    media_type: "image" | "video" | "audio";
    mime_type: string;
    size_bytes: number;
    content_url: string;
    upstream_asset_id?: string;
};

export type LibraryGroupAsset = {
    asset_id: string;
    name: string;
    status: "processing" | "active" | "failed";
    media_type: "image" | "video" | "audio";
    error_message: string | null;
    local: LibraryGroupLocalRow | null;
};

const GROUP_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const ASSET_ID_PATTERN = /^asset-[A-Za-z0-9_-]{1,100}$/;
const STATUSES = new Set(["processing", "active", "failed"]);
const MEDIA_TYPES = new Set(["image", "video", "audio"]);

function groupFromResponse(value: unknown): LibraryGroup {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("资产库分组响应无效，请重试。");
    const group = value as Record<string, unknown>;
    if (typeof group.group_id !== "string" || !GROUP_ID_PATTERN.test(group.group_id)
        || typeof group.name !== "string" || !group.name) {
        throw new Error("资产库分组响应无效，请重试。");
    }
    const created_at = typeof group.created_at === "string" && group.created_at ? group.created_at : undefined;
    return { group_id: group.group_id, name: group.name, created_at };
}

function groupLocalFromResponse(value: unknown): LibraryGroupLocalRow {
    const asset = assetFromResponse(value);
    if (asset.kind !== "library" || typeof asset.size_bytes !== "number" || typeof asset.content_url !== "string") {
        throw new Error("资产库分组内容响应无效，请重试。");
    }
    return {
        asset_id: asset.id,
        kind: "library",
        status: asset.status,
        media_type: asset.media_type ?? "image",
        mime_type: asset.mime_type,
        size_bytes: asset.size_bytes,
        content_url: asset.content_url,
        upstream_asset_id: asset.upstream_asset_id,
    };
}

function groupAssetFromResponse(value: unknown): LibraryGroupAsset {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("资产库分组内容响应无效，请重试。");
    const asset = value as Record<string, unknown>;
    if (typeof asset.asset_id !== "string" || !ASSET_ID_PATTERN.test(asset.asset_id)
        || typeof asset.name !== "string" || !asset.name
        || typeof asset.status !== "string" || !STATUSES.has(asset.status)
        || typeof asset.media_type !== "string" || !MEDIA_TYPES.has(asset.media_type)) {
        throw new Error("资产库分组内容响应无效，请重试。");
    }
    const error_message = typeof asset.error_message === "string" && asset.error_message ? asset.error_message : null;
    const local = asset.local === null || asset.local === undefined
        ? null
        : groupLocalFromResponse(asset.local);
    return {
        asset_id: asset.asset_id,
        name: asset.name,
        status: asset.status as LibraryGroupAsset["status"],
        media_type: asset.media_type as LibraryGroupAsset["media_type"],
        error_message,
        local,
    };
}

export async function listLibraryGroups(): Promise<LibraryGroup[]> {
    const response = await apiFetch<{ groups: unknown[] }>("/api/v1/library-groups");
    if (!response || !Array.isArray(response.groups)) throw new Error("资产库分组响应无效，请重试。");
    return response.groups.map(groupFromResponse);
}

export async function createLibraryGroup(name: string): Promise<LibraryGroup> {
    const response = await apiFetch<unknown>("/api/v1/library-groups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
    });
    return groupFromResponse({ ...(response as Record<string, unknown>), name });
}

export async function renameLibraryGroup(groupId: string, name: string): Promise<void> {
    if (!GROUP_ID_PATTERN.test(groupId)) throw new Error("素材组标识无效。");
    await apiFetch(`/api/v1/library-groups/${encodeURIComponent(groupId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
    });
}

export async function deleteLibraryGroup(groupId: string): Promise<void> {
    if (!GROUP_ID_PATTERN.test(groupId)) throw new Error("素材组标识无效。");
    await apiFetch(`/api/v1/library-groups/${encodeURIComponent(groupId)}`, { method: "DELETE" });
}

export async function fetchGroupAssets(groupId: string): Promise<LibraryGroupAsset[]> {
    if (!GROUP_ID_PATTERN.test(groupId)) throw new Error("素材组标识无效。");
    const response = await apiFetch<{ assets: unknown[] }>(`/api/v1/library-groups/${encodeURIComponent(groupId)}/assets`);
    if (!response || !Array.isArray(response.assets)) throw new Error("资产库分组内容响应无效，请重试。");
    return response.assets.map(groupAssetFromResponse);
}

export async function renameGroupAsset(groupId: string, assetId: string, name: string): Promise<void> {
    if (!GROUP_ID_PATTERN.test(groupId) || !ASSET_ID_PATTERN.test(assetId)) throw new Error("素材标识无效。");
    await apiFetch(`/api/v1/library-groups/${encodeURIComponent(groupId)}/assets/${encodeURIComponent(assetId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
    });
}

export async function deleteGroupAsset(groupId: string, assetId: string): Promise<void> {
    if (!GROUP_ID_PATTERN.test(groupId) || !ASSET_ID_PATTERN.test(assetId)) throw new Error("素材标识无效。");
    await apiFetch(`/api/v1/library-groups/${encodeURIComponent(groupId)}/assets/${encodeURIComponent(assetId)}`, { method: "DELETE" });
}

/** 组内上游素材的同源内容代理地址（每次请求后端重新取预签名 URL）。 */
export function groupAssetContentUrl(groupId: string, assetId: string): string {
    return safeApiPath(`/api/v1/library-groups/${encodeURIComponent(groupId)}/assets/${encodeURIComponent(assetId)}/content`);
}

/** 物化上游独有素材为本地行（7c25820），返回可直接入画布的 AssetRef。 */
export async function importGroupAsset(groupId: string, assetId: string): Promise<AssetRef> {
    if (!GROUP_ID_PATTERN.test(groupId) || !ASSET_ID_PATTERN.test(assetId)) throw new Error("素材标识无效。");
    const response = await apiFetch<unknown>(`/api/v1/library-groups/${encodeURIComponent(groupId)}/assets/${encodeURIComponent(assetId)}/import`, {
        method: "POST",
    });
    const asset = assetFromResponse(response);
    if (asset.kind !== "library") throw new Error("资产库响应无效，请重试。");
    return asset;
}
