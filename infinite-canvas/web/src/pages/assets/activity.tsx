import { useCallback, useEffect, useRef, useState } from "react";

import {
    createLibraryGroup,
    deleteGroupAsset,
    deleteLibraryGroup,
    fetchGroupAssets,
    groupAssetContentUrl,
    listLibraryGroups,
    renameGroupAsset,
    renameLibraryGroup,
    uploadLibraryAsset,
    type LibraryGroup,
    type LibraryGroupAsset,
} from "@/api/assets";


const ACCEPT = "image/png,image/jpeg,image/webp";
const MEDIA_LABELS: Record<string, string> = { image: "图片", video: "视频", audio: "音频" };

function errorMessage(error: unknown, fallback: string): string {
    return error instanceof Error && error.message ? error.message : fallback;
}

function statusLabel(status: string): string {
    return status === "active" ? "已就绪" : status === "processing" ? "审核处理中…" : "处理失败";
}

export default function ActivityAssetsPage() {
    const [groups, setGroups] = useState<LibraryGroup[]>([]);
    const [groupId, setGroupId] = useState("");
    const [groupAssets, setGroupAssets] = useState<LibraryGroupAsset[]>([]);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [renamingGroup, setRenamingGroup] = useState<string | null>(null);
    const [groupRenameValue, setGroupRenameValue] = useState("");
    const [creatingGroup, setCreatingGroup] = useState(false);
    const [newGroupName, setNewGroupName] = useState("");
    const [renamingAsset, setRenamingAsset] = useState<string | null>(null);
    const [assetRenameValue, setAssetRenameValue] = useState("");
    const fileInputRef = useRef<HTMLInputElement>(null);

    const refreshGroups = useCallback(async () => {
        try {
            setError(null);
            const listed = await listLibraryGroups();
            setGroups(listed);
            setGroupId((current) => current && listed.some((group) => group.group_id === current) ? current : (listed[0]?.group_id ?? ""));
        } catch (err) {
            setError(errorMessage(err, "资产库分组加载失败，请重试。"));
        }
    }, []);

    const refreshGroupAssets = useCallback(async (targetGroupId: string) => {
        if (!targetGroupId) {
            setGroupAssets([]);
            setLoading(false);
            return;
        }
        setLoading(true);
        try {
            setGroupAssets(await fetchGroupAssets(targetGroupId));
        } catch (err) {
            setError(errorMessage(err, "分组素材加载失败，请重试。"));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void refreshGroups();
    }, [refreshGroups]);

    useEffect(() => {
        // 回到页面时刷新，拾取别处（其他用户/面板）的增删。
        const onVisible = () => {
            if (document.visibilityState === "visible") void refreshGroups();
        };
        document.addEventListener("visibilitychange", onVisible);
        return () => document.removeEventListener("visibilitychange", onVisible);
    }, [refreshGroups]);

    useEffect(() => {
        void refreshGroupAssets(groupId);
    }, [groupId, refreshGroupAssets]);

    useEffect(() => {
        if (!groupAssets.some((asset) => asset.status === "processing")) return;
        const timer = window.setInterval(() => {
            void refreshGroupAssets(groupId);
        }, 5000);
        return () => window.clearInterval(timer);
    }, [groupAssets, groupId, refreshGroupAssets]);

    const submitFile = async (file: File) => {
        if (!ACCEPT.split(",").includes(file.type) || file.size > 10 * 1024 * 1024) {
            setError("只支持 10MB 以内的 PNG/JPEG/WebP 人像图。");
            return;
        }
        if (!groupId) {
            setError("请先选择或创建素材组。");
            return;
        }
        setUploading(true);
        setError(null);
        try {
            await uploadLibraryAsset(file, groupId, () => undefined);
            await refreshGroupAssets(groupId);
        } catch (err) {
            setError(errorMessage(err, "上传失败，请重试。"));
        } finally {
            setUploading(false);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const removeCurrentGroup = async () => {
        const group = groups.find((item) => item.group_id === groupId);
        if (!group) return;
        const assetCount = groupAssets.length;
        if (!window.confirm(assetCount
            ? `确定删除素材组「${group.name}」及其 ${assetCount} 个素材？此操作不可恢复。`
            : `确定删除素材组「${group.name}」？此操作不可恢复。`)) return;
        setError(null);
        try {
            await deleteLibraryGroup(group.group_id);
            await refreshGroups();
        } catch (err) {
            setError(errorMessage(err, "删除素材组失败，请重试。"));
        }
    };

    const submitNewGroup = async () => {
        const name = newGroupName.trim();
        setCreatingGroup(false);
        setNewGroupName("");
        if (!name) return;
        setError(null);
        try {
            const created = await createLibraryGroup(name);
            setGroups((current) => [...current, created]);
            setGroupId(created.group_id);
            await refreshGroupAssets(created.group_id);
        } catch (err) {
            setError(errorMessage(err, "新建素材组失败，请重试。"));
        }
    };

    const submitGroupRename = async (targetGroupId: string) => {
        const group = groups.find((item) => item.group_id === targetGroupId);
        const name = groupRenameValue.trim();
        setRenamingGroup(null);
        if (!group || !name || name === group.name) return;
        setError(null);
        try {
            await renameLibraryGroup(targetGroupId, name);
            setGroups((current) => current.map((item) => item.group_id === targetGroupId ? { ...item, name } : item));
        } catch (err) {
            setError(errorMessage(err, "组改名失败，请重试。"));
        }
    };

    const submitAssetRename = async (asset: LibraryGroupAsset) => {
        const name = assetRenameValue.trim();
        setRenamingAsset(null);
        if (!name || name === asset.name) return;
        setError(null);
        try {
            await renameGroupAsset(groupId, asset.asset_id, name);
            setGroupAssets((current) => current.map((item) => item.asset_id === asset.asset_id ? { ...item, name } : item));
        } catch (err) {
            setError(errorMessage(err, "素材改名失败，请重试。"));
        }
    };

    const removeAsset = async (asset: LibraryGroupAsset) => {
        if (!window.confirm(`确定删除素材「${asset.name}」？方舟与本地副本一并删除。`)) return;
        setError(null);
        try {
            await deleteGroupAsset(groupId, asset.asset_id);
            await refreshGroupAssets(groupId);
        } catch (err) {
            setError(errorMessage(err, "素材删除失败，请重试。"));
        }
    };

    const selectedGroup = groups.find((group) => group.group_id === groupId);

    return (
        <section className="mx-auto max-w-5xl px-5 py-8 text-[#172033] [color-scheme:light]">
            <p className="text-xs tracking-[0.2em] text-[#235fd6]">ASSET LIBRARY</p>
            <h1 className="mt-2 text-3xl font-semibold text-[#172033]">资产库</h1>
            <p className="mt-2 text-sm text-[#687386]">素材按组分类管理；上传后经方舟审核即可在画布中引用。</p>

            <div className="mt-6 flex flex-wrap items-center gap-3">
                <label className="text-xs text-[#687386]">
                    素材组
                    <select value={groupId} onChange={(event) => setGroupId(event.target.value)} aria-label="选择素材组" disabled={loading && !groups.length} className="mt-1 block w-64 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1.5 text-sm text-[#172033]">
                        {groups.length === 0 ? <option value="">暂无分组</option> : null}
                        {groups.map((group) => (
                            <option key={group.group_id} value={group.group_id} title={group.created_at ? `创建于 ${group.created_at}` : undefined}>{group.name}</option>
                        ))}
                    </select>
                </label>
                <button type="button" onClick={() => void refreshGroups()} disabled={uploading} aria-label="刷新素材组" className="mt-4 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-3 py-1.5 text-sm text-[#235fd6] disabled:opacity-50">刷新</button>
                {selectedGroup ? (
                    <button type="button" onClick={() => { setGroupRenameValue(selectedGroup.name); setRenamingGroup(selectedGroup.group_id); }} aria-label="重命名素材组" className="mt-4 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-3 py-1.5 text-sm text-[#235fd6]">改名</button>
                ) : null}
                <button type="button" onClick={() => setCreatingGroup(true)} aria-label="新建素材组" className="mt-4 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-3 py-1.5 text-sm text-[#235fd6]">新建组</button>
                {selectedGroup ? (
                    <button type="button" onClick={() => void removeCurrentGroup()} aria-label="删除素材组" className="mt-4 rounded border border-[#e6c8bd] bg-[#f3f6fa] px-3 py-1.5 text-sm text-[#c2410c]">删除</button>
                ) : null}
                <label className="mt-4 text-sm text-[#687386]">
                    上传到当前组
                    <input
                        ref={fileInputRef}
                        aria-label="选择素材文件"
                        type="file"
                        accept={ACCEPT}
                        disabled={uploading || !groupId}
                        onChange={(event) => {
                            const selected = event.target.files?.[0];
                            if (selected) void submitFile(selected);
                        }}
                        className="mt-1 block max-w-full text-xs text-[#172033] file:mr-3 file:rounded file:border file:border-[#d9e0ea] file:bg-[#f3f6fa] file:px-3 file:py-2 file:text-[#235fd6]"
                    />
                </label>
            </div>
            {renamingGroup ? (
                <div className="mt-3 flex items-center gap-2">
                    <input aria-label="素材组新名字" value={groupRenameValue} onChange={(event) => setGroupRenameValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void submitGroupRename(renamingGroup); if (event.key === "Escape") setRenamingGroup(null); }} className="w-64 rounded border border-[#c3ccd9] bg-[#ffffff] px-2 py-1.5 text-sm text-[#172033]" />
                    <button type="button" onClick={() => void submitGroupRename(renamingGroup)} className="rounded bg-[#235fd6] px-3 py-1.5 text-sm font-medium text-[#ffffff]">保存</button>
                    <button type="button" onClick={() => setRenamingGroup(null)} className="rounded border border-[#c3ccd9] px-3 py-1.5 text-sm text-[#687386]">取消</button>
                </div>
            ) : null}
            {creatingGroup ? (
                <div className="mt-3 flex items-center gap-2">
                    <input aria-label="新素材组名字" value={newGroupName} onChange={(event) => setNewGroupName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void submitNewGroup(); if (event.key === "Escape") setCreatingGroup(false); }} placeholder="输入新组名" className="w-64 rounded border border-[#c3ccd9] bg-[#ffffff] px-2 py-1.5 text-sm text-[#172033]" />
                    <button type="button" onClick={() => void submitNewGroup()} className="rounded bg-[#235fd6] px-3 py-1.5 text-sm font-medium text-[#ffffff]">保存</button>
                    <button type="button" onClick={() => { setCreatingGroup(false); setNewGroupName(""); }} className="rounded border border-[#c3ccd9] px-3 py-1.5 text-sm text-[#687386]">取消</button>
                </div>
            ) : null}
            {error ? <p className="mt-3 text-sm text-[#92400e]" role="alert">{error}</p> : null}

            <div className="mt-6 overflow-hidden rounded-xl border border-[#d9e0ea] bg-[#ffffff]">
                {loading ? <p className="p-8 text-sm text-[#687386]">正在加载素材…</p> : null}
                {!loading && !groupAssets.length ? <p className="p-8 text-sm text-[#687386]">这个组还没有素材，选择图片上传即可。</p> : null}
                <ul className="grid grid-cols-2 gap-3 p-4 sm:grid-cols-3 lg:grid-cols-4">
                    {groupAssets.map((asset) => {
                        const local = asset.local;
                        const proxied = groupAssetContentUrl(groupId, asset.asset_id);
                        const thumbnail = local?.content_url ?? proxied;
                        return (
                            <li key={asset.asset_id} className="overflow-hidden rounded-lg border border-[#d9e0ea] bg-[#f3f6fa]">
                                <div className="aspect-square w-full overflow-hidden bg-[#eef2f7]" aria-hidden="true">
                                    {asset.media_type === "image" ? (
                                        <img src={thumbnail} alt={asset.name} loading="lazy" data-testid={`library-thumb-${asset.asset_id}`} className="h-full w-full object-cover" />
                                    ) : asset.media_type === "video" ? (
                                        <video src={thumbnail} muted playsInline preload="metadata" data-testid={`library-thumb-${asset.asset_id}`} className="h-full w-full object-cover" />
                                    ) : (
                                        <span data-testid={`library-thumb-${asset.asset_id}`} className="flex h-full w-full items-center justify-center text-xl text-[#687386]">♪</span>
                                    )}
                                </div>
                                <div className="p-2">
                                    {renamingAsset === asset.asset_id ? (
                                        <div className="flex items-center gap-1">
                                            <input aria-label="素材新名字" value={assetRenameValue} onChange={(event) => setAssetRenameValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void submitAssetRename(asset); if (event.key === "Escape") setRenamingAsset(null); }} className="min-w-0 flex-1 rounded border border-[#c3ccd9] bg-[#ffffff] px-1.5 py-1 text-xs text-[#172033]" />
                                            <button type="button" onClick={() => void submitAssetRename(asset)} className="rounded bg-[#235fd6] px-2 py-1 text-xs font-medium text-[#ffffff]">保存</button>
                                            <button type="button" onClick={() => setRenamingAsset(null)} className="rounded border border-[#c3ccd9] px-2 py-1 text-xs text-[#687386]">取消</button>
                                        </div>
                                    ) : (
                                        <p className="truncate text-sm text-[#172033]" title={asset.name}>{asset.name}</p>
                                    )}
                                    <p className="mt-0.5 text-xs text-[#687386]">
                                        {MEDIA_LABELS[asset.media_type] ?? "素材"} · {statusLabel(asset.status)}
                                    </p>
                                    {asset.status === "failed" && asset.error_message ? (
                                        <p className="mt-0.5 line-clamp-2 text-xs text-[#c2410c]" title={asset.error_message}>{asset.error_message}</p>
                                    ) : null}
                                    {renamingAsset !== asset.asset_id ? (
                                        <div className="mt-1.5 flex gap-1">
                                            <button type="button" onClick={() => { setAssetRenameValue(asset.name); setRenamingAsset(asset.asset_id); }} aria-label={`重命名 ${asset.name}`} className="rounded border border-[#d9e0ea] bg-[#ffffff] px-2 py-1 text-xs text-[#687386] hover:border-[#235fd6]">
                                                改名
                                            </button>
                                            <button type="button" onClick={() => void removeAsset(asset)} aria-label={`删除 ${asset.name}`} className="rounded border border-[#d9e0ea] bg-[#ffffff] px-2 py-1 text-xs text-[#c2410c] hover:border-[#c2410c]">
                                                删除
                                            </button>
                                        </div>
                                    ) : null}
                                </div>
                            </li>
                        );
                    })}
                </ul>
            </div>
        </section>
    );
}
