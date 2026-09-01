import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { nanoid } from "nanoid";

import {
    createLibraryGroup,
    deleteGroupAsset as deleteGroupAssetApi,
    deleteLibraryGroup,
    fetchAsset as fetchAssetApi,
    fetchGroupAssets as fetchGroupAssetsApi,
    groupAssetContentUrl,
    importGroupAsset as importGroupAssetApi,
    listLibraryGroups,
    renameLibraryGroup,
    renameGroupAsset as renameGroupAssetApi,
    uploadLibraryAsset,
} from "@/api/assets";
import type { LibraryGroup, LibraryGroupAsset, LibraryGroupLocalRow } from "@/api/assets";
import type { AssetRef } from "@/api/contracts";
import type { GraphMediaItem } from "@/features/graph/contracts";
import { safeMediaDisplayName } from "@/features/graph/media-collection";


type LibraryTarget = { nodeId: string; label: string; itemCount: number };

type Props = {
    targets: readonly LibraryTarget[];
    onClose: () => void;
    upload?: (file: File, groupId: string | undefined, onProgress: (percent: number) => void, signal?: AbortSignal) => Promise<AssetRef>;
    fetchAsset?: (id: string) => Promise<AssetRef>;
    fetchGroups?: () => Promise<LibraryGroup[]>;
    createGroup?: (name: string) => Promise<LibraryGroup>;
    renameGroup?: (groupId: string, name: string) => Promise<void>;
    deleteGroup?: (groupId: string) => Promise<void>;
    fetchGroupAssets?: (groupId: string) => Promise<LibraryGroupAsset[]>;
    renameGroupAsset?: (groupId: string, assetId: string, name: string) => Promise<void>;
    deleteGroupAsset?: (groupId: string, assetId: string) => Promise<void>;
    importGroupAsset?: (groupId: string, assetId: string) => Promise<AssetRef>;
    addToCollection?: (nodeId: string, items: GraphMediaItem[]) => void;
    pollIntervalMs?: number;
};

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

function isPortraitImage(file: File): boolean {
    return file.type.startsWith("image/") && (file.type === "image/png" || file.type === "image/jpeg" || file.type === "image/webp");
}

function errorMessage(error: unknown, fallback: string): string {
    return error instanceof Error && error.message ? error.message : fallback;
}

function localRowToRef(row: LibraryGroupLocalRow): AssetRef {
    return {
        id: row.asset_id,
        kind: "library",
        status: row.status,
        media_type: row.media_type,
        mime_type: row.mime_type,
        size_bytes: row.size_bytes,
        content_url: row.content_url,
        upstream_asset_id: row.upstream_asset_id,
    };
}

function importedToLocal(asset: AssetRef): LibraryGroupLocalRow {
    return {
        asset_id: asset.id,
        kind: "library",
        status: asset.status,
        media_type: asset.media_type ?? "image",
        mime_type: asset.mime_type,
        size_bytes: asset.size_bytes ?? 1,
        content_url: asset.content_url ?? "",
        upstream_asset_id: asset.upstream_asset_id,
    };
}

function statusLabel(status: string): string {
    return status === "active" ? "已就绪" : status === "processing" ? "审核处理中…" : "处理失败";
}

export function AssetLibraryPanel({
    targets,
    onClose,
    upload = uploadLibraryAsset,
    fetchAsset = fetchAssetApi,
    fetchGroups = listLibraryGroups,
    createGroup = createLibraryGroup,
    renameGroup = renameLibraryGroup,
    deleteGroup = deleteLibraryGroup,
    fetchGroupAssets = fetchGroupAssetsApi,
    renameGroupAsset = renameGroupAssetApi,
    deleteGroupAsset = deleteGroupAssetApi,
    importGroupAsset = importGroupAssetApi,
    addToCollection = () => undefined,
    pollIntervalMs = 5000,
}: Props) {
    const [groups, setGroups] = useState<LibraryGroup[]>([]);
    const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);
    const [assets, setAssets] = useState<LibraryGroupAsset[]>([]);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const [importingId, setImportingId] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [newGroupName, setNewGroupName] = useState("");
    const [renamingGroup, setRenamingGroup] = useState(false);
    const [renameValue, setRenameValue] = useState("");
    // 上游 ea58271：多目标时按素材行弹「添加到哪里」选择器；记住上次选择，
    // 下次同类素材默认高亮上次的节点。选择器按上游素材 id 定位行，
    // 真正要入画布的 AssetRef 暂存在 pendingRef（无本地行时先 import 物化）。
    const [pickingFor, setPickingFor] = useState<string | null>(null);
    const pendingRef = useRef<{ ref: AssetRef; displayName: string } | null>(null);
    const lastTargetId = useRef<string>(targets[0]?.nodeId ?? "");
    const fileInputRef = useRef<HTMLInputElement>(null);

    // client-side join：显示状态以本地行（localByUpstream，取自 assets 里的
    // item.local）为准，避免 settleProcessing 与分组轮询同 tick 覆盖状态。
    const localByUpstream = useMemo(() => {
        const map = new Map<string, LibraryGroupLocalRow>();
        for (const item of assets) {
            if (item.local) map.set(item.asset_id, item.local);
        }
        return map;
    }, [assets]);

    const refreshGroups = useCallback(async () => {
        try {
            const next = await fetchGroups();
            setGroups(next);
            return next;
        } catch (err) {
            setError(errorMessage(err, "素材组加载失败，请重试。"));
            return null;
        }
    }, [fetchGroups]);

    useEffect(() => {
        let cancelled = false;
        void (async () => {
            const next = await refreshGroups();
            if (cancelled) return;
            setLoading(false);
            if (next && next.length) {
                setSelectedGroupId((previous) =>
                    previous && next.some((group) => group.group_id === previous) ? previous : next[0].group_id);
            }
        })();
        return () => { cancelled = true; };
    }, [refreshGroups]);

    const refreshGroupAssets = useCallback(async (groupId: string) => {
        if (!groupId) return;
        try {
            setError(null);
            setAssets(await fetchGroupAssets(groupId));
        } catch (err) {
            setError(errorMessage(err, "素材列表加载失败，请重试。"));
        }
    }, [fetchGroupAssets]);

    useEffect(() => {
        if (!selectedGroupId) {
            setAssets([]);
            return;
        }
        let cancelled = false;
        void (async () => {
            try {
                setError(null);
                const next = await fetchGroupAssets(selectedGroupId);
                if (!cancelled) setAssets(next);
            } catch (err) {
                if (!cancelled) setError(errorMessage(err, "素材列表加载失败，请重试。"));
            }
        })();
        return () => { cancelled = true; };
    }, [selectedGroupId, fetchGroupAssets]);

    useEffect(() => {
        if (!pickingFor) return;
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") setPickingFor(null);
        };
        window.addEventListener("keydown", onKeyDown);
        return () => window.removeEventListener("keydown", onKeyDown);
    }, [pickingFor]);

    // 轮询 1：本地行审核状态（GET /api/v1/assets/{id} 会顺带刷新 DB 状态）。
    const settleProcessing = useCallback(async () => {
        const pending = assets.filter((item) => (localByUpstream.get(item.asset_id) ?? item.local)?.status === "processing");
        if (!pending.length) return;
        for (const item of pending) {
            const localRow = localByUpstream.get(item.asset_id) ?? item.local;
            if (!localRow) continue;
            try {
                const updated = await fetchAsset(localRow.asset_id);
                setAssets((current) => current.map((candidate) =>
                    candidate.asset_id === item.asset_id && candidate.local
                        ? { ...candidate, local: { ...candidate.local, status: updated.status } }
                        : candidate));
            } catch {
                // 网络抖动保留原状，下个周期再试。
            }
        }
    }, [assets, localByUpstream, fetchAsset]);

    useEffect(() => {
        const hasPendingLocal = assets.some((item) => (localByUpstream.get(item.asset_id) ?? item.local)?.status === "processing");
        if (!hasPendingLocal) return undefined;
        const timer = window.setInterval(() => { void settleProcessing(); }, Math.max(200, pollIntervalMs));
        return () => window.clearInterval(timer);
    }, [assets, localByUpstream, pollIntervalMs, settleProcessing]);

    // 轮询 2：上游独有素材（无本地行）还在 Processing 时整组刷新。
    useEffect(() => {
        if (!selectedGroupId) return undefined;
        const hasUpstreamProcessing = assets.some((item) =>
            (localByUpstream.get(item.asset_id) ?? item.local) === null && item.status === "processing");
        if (!hasUpstreamProcessing) return undefined;
        const timer = window.setInterval(() => { void refreshGroupAssets(selectedGroupId); }, Math.max(200, pollIntervalMs));
        return () => window.clearInterval(timer);
    }, [assets, localByUpstream, selectedGroupId, pollIntervalMs, refreshGroupAssets]);

    const submitFile = async (file: File) => {
        if (!isPortraitImage(file) || file.size > MAX_IMAGE_BYTES) {
            setError("只支持 10MB 以内的 PNG/JPEG/WebP 人像图。");
            return;
        }
        setUploading(true);
        setError(null);
        try {
            await upload(file, selectedGroupId ?? undefined, () => undefined);
            // 上传落组后刷新组资产；没有组时后端会创建默认组，一并刷新分组。
            const next = await refreshGroups();
            if (selectedGroupId) {
                await refreshGroupAssets(selectedGroupId);
            } else if (next && next.length) {
                setSelectedGroupId(next[0].group_id);
            }
        } catch (err) {
            setError(errorMessage(err, "上传失败，请重试。"));
        } finally {
            setUploading(false);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const buildItem = (asset: AssetRef, displayName: string): GraphMediaItem => {
        if (typeof asset.size_bytes !== "number") throw new Error(`library asset ${asset.id} has no size_bytes`);
        return {
            id: nanoid(),
            assetId: asset.id,
            displayName,
            mimeType: asset.mime_type,
            bytes: asset.size_bytes,
            kind: "library",
        };
    };

    const addToTarget = (target: LibraryTarget) => {
        const pending = pendingRef.current;
        if (!pending) return;
        lastTargetId.current = target.nodeId;
        addToCollection(target.nodeId, [buildItem(pending.ref, pending.displayName)]);
    };

    // 上游独有素材（无本地行）先 import 物化再走现有目标选择流程。
    const handleAdd = async (item: LibraryGroupAsset) => {
        if (!selectedGroupId) return;
        const localRow = localByUpstream.get(item.asset_id) ?? item.local;
        let ref: AssetRef;
        if (localRow) {
            ref = localRowToRef(localRow);
        } else {
            setImportingId(item.asset_id);
            setError(null);
            try {
                ref = await importGroupAsset(selectedGroupId, item.asset_id);
                setAssets((current) => current.map((candidate) =>
                    candidate.asset_id === item.asset_id ? { ...candidate, local: importedToLocal(ref) } : candidate));
            } catch (err) {
                setError(errorMessage(err, "素材导入失败，请重试。"));
                return;
            } finally {
                setImportingId(null);
            }
        }
        if (ref.status !== "active" || typeof ref.size_bytes !== "number" || typeof ref.media_type !== "string") return;
        pendingRef.current = { ref, displayName: safeMediaDisplayName(item.name, "image") };
        if (targets.length === 1) {
            addToTarget(targets[0]);
            return;
        }
        setPickingFor(item.asset_id);
    };

    const orderedTargets = [...targets].sort((left, right) => {
        const leftRemembered = lastTargetId.current === left.nodeId ? 0 : 1;
        const rightRemembered = lastTargetId.current === right.nodeId ? 0 : 1;
        return leftRemembered - rightRemembered;
    });

    const removeAsset = async (item: LibraryGroupAsset) => {
        if (!selectedGroupId) return;
        if (!window.confirm(`确认删除素材「${item.name}」？方舟与本地副本一并删除，画布里已添加的引用不受影响。`)) return;
        try {
            await deleteGroupAsset(selectedGroupId, item.asset_id);
            await refreshGroupAssets(selectedGroupId);
        } catch (err) {
            setError(errorMessage(err, "删除失败，请重试。"));
        }
    };

    const createNewGroup = async () => {
        const name = newGroupName.trim();
        if (!name) return;
        setError(null);
        try {
            const created = await createGroup(name);
            setNewGroupName("");
            await refreshGroups();
            setSelectedGroupId(created.group_id);
        } catch (err) {
            setError(errorMessage(err, "新建分组失败，请重试。"));
        }
    };

    const commitRenameGroup = async () => {
        if (!selectedGroupId) return;
        const name = renameValue.trim();
        if (!name) return;
        setError(null);
        try {
            await renameGroup(selectedGroupId, name);
            setRenamingGroup(false);
            await refreshGroups();
        } catch (err) {
            setError(errorMessage(err, "分组改名失败，请重试。"));
        }
    };

    const handleDeleteGroup = async () => {
        if (!selectedGroupId) return;
        const group = groups.find((candidate) => candidate.group_id === selectedGroupId);
        if (!window.confirm(`确认删除分组「${group?.name ?? selectedGroupId}」？组内素材会一并删除，画布里已添加的引用不受影响。`)) return;
        setError(null);
        try {
            await deleteGroup(selectedGroupId);
            const next = await refreshGroups();
            if (next && next.length) {
                setSelectedGroupId((previous) =>
                    previous && next.some((candidate) => candidate.group_id === previous) ? previous : next[0].group_id);
            } else {
                setSelectedGroupId(null);
                setAssets([]);
            }
        } catch (err) {
            setError(errorMessage(err, "删除分组失败，请重试。"));
        }
    };

    const currentGroup = groups.find((group) => group.group_id === selectedGroupId);

    return (
        <aside className="fixed right-6 top-1/2 z-40 flex h-[60vh] w-1/4 min-w-[380px] -translate-y-1/2 flex-col rounded-xl border border-[#d9e0ea] bg-[#f8fafc] p-4 shadow-2xl" aria-label="人像资产库">
            <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold">人像资产库</h2>
                <button type="button" onClick={onClose} aria-label="关闭人像资产库" className="text-xs text-[#687386] hover:text-[#172033]">关闭</button>
            </div>
            <p className="mt-1 text-xs text-[#687386]">上传的人像会进入火山方舟私域资产库，生成视频时以资产引用方式使用。</p>
            {targets.length === 0 ? (
                <p className="mt-3 rounded border border-[#d9e0ea] bg-[#f3f6fa] p-2 text-xs text-[#687386]">先在画布中添加一个图片素材节点，再从这里添加人像。</p>
            ) : null}
            <div className="mt-3 flex items-center gap-2">
                <select
                    value={selectedGroupId ?? ""}
                    onChange={(event) => setSelectedGroupId(event.target.value || null)}
                    aria-label="选择素材组"
                    disabled={loading}
                    className="min-w-0 flex-1 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1.5 text-xs text-[#172033] disabled:opacity-50"
                >
                    {groups.length === 0 ? <option value="">暂无分组</option> : null}
                    {groups.map((group) => (
                        <option key={group.group_id} value={group.group_id}>{group.name}</option>
                    ))}
                </select>
                <button
                    type="button"
                    disabled={!selectedGroupId}
                    onClick={() => {
                        setRenameValue(currentGroup?.name ?? "");
                        setRenamingGroup(true);
                    }}
                    aria-label="改名当前素材组"
                    className="shrink-0 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1.5 text-xs text-[#235fd6] hover:bg-[#eef2f7] disabled:opacity-50"
                >
                    改名
                </button>
                <button
                    type="button"
                    disabled={!selectedGroupId}
                    onClick={() => void handleDeleteGroup()}
                    aria-label="删除当前素材组"
                    className="shrink-0 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1.5 text-xs text-[#c2410c] hover:bg-[#fdf0e8] disabled:opacity-50"
                >
                    删组
                </button>
            </div>
            {renamingGroup && selectedGroupId ? (
                <div className="mt-2 flex items-center gap-2">
                    <input
                        value={renameValue}
                        onChange={(event) => setRenameValue(event.target.value)}
                        onKeyDown={(event) => {
                            if (event.key === "Enter") void commitRenameGroup();
                            if (event.key === "Escape") setRenamingGroup(false);
                        }}
                        aria-label="分组新名字"
                        maxLength={64}
                        className="min-w-0 flex-1 rounded border border-[#d9e0ea] bg-[#ffffff] px-2 py-1 text-xs text-[#172033]"
                    />
                    <button type="button" onClick={() => void commitRenameGroup()} className="shrink-0 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1 text-xs text-[#235fd6] hover:bg-[#eef2f7]">保存</button>
                    <button type="button" onClick={() => setRenamingGroup(false)} className="shrink-0 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1 text-xs text-[#687386] hover:bg-[#eef2f7]">取消</button>
                </div>
            ) : null}
            <div className="mt-2 flex items-center gap-2">
                <input
                    value={newGroupName}
                    onChange={(event) => setNewGroupName(event.target.value)}
                    onKeyDown={(event) => {
                        if (event.key === "Enter") void createNewGroup();
                    }}
                    placeholder="新分组名字"
                    aria-label="新分组名字"
                    maxLength={64}
                    className="min-w-0 flex-1 rounded border border-[#d9e0ea] bg-[#ffffff] px-2 py-1 text-xs text-[#172033]"
                />
                <button
                    type="button"
                    onClick={() => void createNewGroup()}
                    disabled={!newGroupName.trim()}
                    aria-label="新建素材组"
                    className="shrink-0 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1 text-xs text-[#235fd6] hover:bg-[#eef2f7] disabled:opacity-50"
                >
                    新建组
                </button>
            </div>
            <div className="mt-3 flex items-center gap-2">
                <input
                    ref={fileInputRef}
                    aria-label="选择人像图片"
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    disabled={uploading}
                    onChange={(event) => {
                        const selected = event.target.files?.[0];
                        if (selected) void submitFile(selected);
                    }}
                    className="block max-w-full text-xs file:mr-3 file:rounded file:border file:border-[#d9e0ea] file:bg-[#f3f6fa] file:px-3 file:py-2 file:text-[#235fd6]"
                />
                <button
                    type="button"
                    onClick={() => { if (selectedGroupId) void refreshGroupAssets(selectedGroupId); }}
                    disabled={uploading || !selectedGroupId}
                    aria-label="刷新资产库"
                    className="shrink-0 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1 text-xs text-[#235fd6] hover:bg-[#eef2f7] disabled:opacity-50"
                >
                    刷新
                </button>
            </div>
            {error ? <p className="mt-2 text-xs text-[#92400e]" role="alert">{error}</p> : null}
            <div className="mt-3 flex-1 overflow-y-auto" aria-label="资产库列表">
                {loading ? <p className="text-xs text-[#687386]">加载中…</p> : null}
                {!loading && !groups.length ? <p className="text-xs text-[#687386]">还没有素材组：新建一个分组，或直接上传（自动进入默认组）。</p> : null}
                {!loading && groups.length > 0 && !selectedGroupId ? <p className="text-xs text-[#687386]">选择一个素材组查看素材。</p> : null}
                {!loading && selectedGroupId && !assets.length ? <p className="text-xs text-[#687386]">这个分组还没有素材，上传或从方舟工具添加。</p> : null}
                <ul className="space-y-2">
                    {assets.map((item) => {
                        const localRow = localByUpstream.get(item.asset_id) ?? item.local;
                        const effectiveStatus = localRow?.status ?? item.status;
                        const failed = effectiveStatus === "failed";
                        const isImporting = importingId === item.asset_id;
                        return (
                            <li key={item.asset_id} className="relative flex items-center justify-between gap-2 rounded border border-[#d9e0ea] bg-[#f3f6fa] p-2">
                                <div className="flex min-w-0 items-center gap-2">
                                    {item.media_type === "image" ? (
                                        <img
                                            src={localRow?.content_url ?? (selectedGroupId ? groupAssetContentUrl(selectedGroupId, item.asset_id) : "")}
                                            alt=""
                                            loading="lazy"
                                            className="h-10 w-10 shrink-0 rounded border border-[#d9e0ea] bg-[#eef2f7] object-cover"
                                        />
                                    ) : (
                                        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded border border-[#d9e0ea] bg-[#eef2f7] text-[10px] text-[#687386]">
                                            {item.media_type === "video" ? "视频" : "音频"}
                                        </span>
                                    )}
                                    <div className="min-w-0">
                                        <p className="truncate text-xs text-[#172033]" title={item.name}>{item.name}</p>
                                        <p className={`text-[10px] ${failed ? "text-[#92400e]" : "text-[#687386]"}`}>
                                            {statusLabel(effectiveStatus)}
                                        </p>
                                        {failed && item.error_message ? (
                                            <p className="truncate text-[10px] text-[#92400e]" title={item.error_message}>{item.error_message}</p>
                                        ) : null}
                                    </div>
                                </div>
                                <div className="flex shrink-0 items-center gap-1">
                                    <button
                                        type="button"
                                        disabled={effectiveStatus !== "active" || !targets.length || isImporting}
                                        onClick={() => void handleAdd(item)}
                                        aria-label={`添加 ${item.name} 到素材节点`}
                                        className="rounded border border-[#d9e0ea] bg-[#f8fafc] px-2 py-1 text-xs text-[#235fd6] hover:bg-[#eef2f7] disabled:cursor-not-allowed disabled:opacity-40"
                                    >
                                        {isImporting ? "导入中…" : "添加"}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => void removeAsset(item)}
                                        aria-label={`删除素材 ${item.name}`}
                                        className="rounded border border-[#d9e0ea] bg-[#f8fafc] px-2 py-1 text-xs text-[#c2410c] hover:bg-[#fdf0e8]"
                                    >
                                        删除
                                    </button>
                                </div>
                                {pickingFor === item.asset_id && targets.length > 1 ? (
                                    <div className="absolute right-0 top-full z-10 mt-1 w-64 rounded-lg border border-[#d9e0ea] bg-[#f8fafc] p-1 shadow-xl" role="menu" aria-label="选择目标素材节点">
                                        <p className="px-2 py-1 text-[10px] tracking-wide text-[#687386]">添加到哪个素材节点</p>
                                        {orderedTargets.map((target) => (
                                            <button
                                                key={target.nodeId}
                                                type="button"
                                                role="menuitem"
                                                onClick={() => {
                                                    addToTarget(target);
                                                    setPickingFor(null);
                                                }}
                                                className={`block w-full rounded px-2 py-1 text-left text-xs hover:bg-[#eef2f7] ${lastTargetId.current === target.nodeId ? "font-semibold text-[#235fd6]" : "text-[#172033]"}`}
                                            >
                                                {target.label}（{target.itemCount} 项）
                                            </button>
                                        ))}
                                    </div>
                                ) : null}
                            </li>
                        );
                    })}
                </ul>
            </div>
        </aside>
    );
}
