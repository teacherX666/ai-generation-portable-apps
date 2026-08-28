import { useCallback, useEffect, useRef, useState } from "react";
import { nanoid } from "nanoid";

import { deleteMediaAsset, fetchAsset as fetchAssetApi, fetchLibraryAssets, uploadLibraryAsset } from "@/api/assets";
import type { AssetRef } from "@/api/contracts";
import type { GraphMediaItem } from "@/features/graph/contracts";


type LibraryTarget = { nodeId: string; label: string; itemCount: number };

type Props = {
    targets: readonly LibraryTarget[];
    onClose: () => void;
    upload?: (file: File, onProgress: (percent: number) => void, signal?: AbortSignal) => Promise<AssetRef>;
    fetchAssets?: () => Promise<AssetRef[]>;
    fetchAsset?: (id: string) => Promise<AssetRef>;
    addToCollection?: (nodeId: string, items: GraphMediaItem[]) => void;
    deleteAsset?: (id: string) => Promise<void>;
    pollIntervalMs?: number;
};

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

function isPortraitImage(file: File): boolean {
    return file.type.startsWith("image/") && (file.type === "image/png" || file.type === "image/jpeg" || file.type === "image/webp");
}

export function AssetLibraryPanel({
    targets,
    onClose,
    upload = uploadLibraryAsset,
    fetchAssets = fetchLibraryAssets,
    fetchAsset = fetchAssetApi,
    addToCollection = () => undefined,
    deleteAsset = deleteMediaAsset,
    pollIntervalMs = 5000,
}: Props) {
    const [assets, setAssets] = useState<AssetRef[]>([]);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    // 上游 ea58271：多目标时按素材行弹「添加到哪里」选择器；记住上次选择，
    // 下次同类素材默认高亮上次的节点。我们的人像库目标全是图片节点，
    // 单个 remembered id 即等价于上游按 mediaType 的记忆。
    const [pickingFor, setPickingFor] = useState<string | null>(null);
    const lastTargetId = useRef<string>(targets[0]?.nodeId ?? "");
    const fileInputRef = useRef<HTMLInputElement>(null);

    const refresh = useCallback(async () => {
        try {
            setError(null);
            setAssets(await fetchAssets());
        } catch {
            setError("资产库加载失败，请重试。");
        } finally {
            setLoading(false);
        }
    }, [fetchAssets]);

    useEffect(() => {
        void refresh();
    }, [refresh]);

    useEffect(() => {
        if (!pickingFor) return;
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") setPickingFor(null);
        };
        window.addEventListener("keydown", onKeyDown);
        return () => window.removeEventListener("keydown", onKeyDown);
    }, [pickingFor]);

    const settleProcessing = useCallback(async () => {
        const pending = assets.filter((asset) => asset.status === "processing");
        if (!pending.length) return;
        const settled: AssetRef[] = [];
        for (const asset of pending) {
            try {
                const updated = await fetchAsset(asset.id);
                settled.push(updated);
            } catch {
                settled.push(asset);
            }
        }
        setAssets((current) => current.map((item) => settled.find((candidate) => candidate.id === item.id) ?? item));
    }, [assets, fetchAsset]);

    useEffect(() => {
        if (assets.some((asset) => asset.status === "processing")) {
            const timer = window.setInterval(() => {
                void settleProcessing();
            }, Math.max(200, pollIntervalMs));
            return () => window.clearInterval(timer);
        }
        return undefined;
    }, [assets, pollIntervalMs, settleProcessing]);

    const submitFile = async (file: File) => {
        if (!isPortraitImage(file) || file.size > MAX_IMAGE_BYTES) {
            setError("只支持 10MB 以内的 PNG/JPEG/WebP 人像图。");
            return;
        }
        setUploading(true);
        setError(null);
        try {
            const asset = await upload(file, () => undefined);
            setAssets((current) => [asset, ...current.filter((item) => item.id !== asset.id)]);
        } catch {
            setError("上传失败，请重试。");
        } finally {
            setUploading(false);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const buildItem = (asset: AssetRef): GraphMediaItem => {
        if (typeof asset.size_bytes !== "number") throw new Error(`library asset ${asset.id} has no size_bytes`);
        return {
            id: nanoid(),
            assetId: asset.id,
            displayName: asset.id,
            mimeType: asset.mime_type,
            bytes: asset.size_bytes,
            kind: "library",
        };
    };

    const addToTarget = (target: LibraryTarget, asset: AssetRef) => {
        if (asset.status !== "active" || typeof asset.size_bytes !== "number" || typeof asset.media_type !== "string") return;
        lastTargetId.current = target.nodeId;
        addToCollection(target.nodeId, [buildItem(asset)]);
    };

    const chooseAddTarget = (asset: AssetRef) => {
        if (targets.length === 1) {
            addToTarget(targets[0], asset);
            return;
        }
        if (targets.length > 1) setPickingFor(asset.id);
    };

    const orderedTargets = [...targets].sort((left, right) => {
        const leftRemembered = lastTargetId.current === left.nodeId ? 0 : 1;
        const rightRemembered = lastTargetId.current === right.nodeId ? 0 : 1;
        return leftRemembered - rightRemembered;
    });

    const removeAsset = async (asset: AssetRef) => {
        if (!window.confirm(`确认删除素材 ${asset.id} ？画布里已添加的引用不受影响。`)) return;
        setError(null);
        try {
            await deleteAsset(asset.id);
            setAssets((current) => current.filter((item) => item.id !== asset.id));
        } catch {
            setError("删除失败，请重试。");
        }
    };

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
                <button type="button" onClick={() => void refresh()} disabled={uploading} aria-label="刷新资产库" className="shrink-0 rounded border border-[#d9e0ea] bg-[#f3f6fa] px-2 py-1 text-xs text-[#235fd6] hover:bg-[#eef2f7] disabled:opacity-50">刷新</button>
            </div>
            {error ? <p className="mt-2 text-xs text-[#92400e]" role="alert">{error}</p> : null}
            <div className="mt-3 flex-1 overflow-y-auto" aria-label="资产库列表">
                {loading ? <p className="text-xs text-[#687386]">加载中…</p> : null}
                {!loading && !assets.length ? <p className="text-xs text-[#687386]">资产库还没有人像，选择图片上传即可。</p> : null}
                <ul className="space-y-2">
                    {assets.map((asset) => (
                        <li key={asset.id} className="relative flex items-center justify-between rounded border border-[#d9e0ea] bg-[#f3f6fa] p-2">
                            <div className="min-w-0">
                                <p className="truncate text-xs text-[#172033]">{asset.id}</p>
                                <p className="text-[10px] text-[#687386]">
                                    {asset.status === "active" ? "已就绪" : asset.status === "processing" ? "审核处理中…" : "处理失败"}
                                </p>
                            </div>
                            <div className="flex shrink-0 items-center gap-1">
                                <button
                                    type="button"
                                    disabled={asset.status !== "active" || !targets.length}
                                    onClick={() => chooseAddTarget(asset)}
                                    aria-label={`添加 ${asset.id} 到素材节点`}
                                    className="rounded border border-[#d9e0ea] bg-[#f8fafc] px-2 py-1 text-xs text-[#235fd6] hover:bg-[#eef2f7] disabled:cursor-not-allowed disabled:opacity-40"
                                >
                                    添加
                                </button>
                                <button
                                    type="button"
                                    onClick={() => void removeAsset(asset)}
                                    aria-label={`删除素材 ${asset.id}`}
                                    className="rounded border border-[#d9e0ea] bg-[#f8fafc] px-2 py-1 text-xs text-[#c2410c] hover:bg-[#fdf0e8]"
                                >
                                    删除
                                </button>
                            </div>
                            {pickingFor === asset.id && targets.length > 1 ? (
                                <div className="absolute right-0 top-full z-10 mt-1 w-64 rounded-lg border border-[#d9e0ea] bg-[#f8fafc] p-1 shadow-xl" role="menu" aria-label="选择目标素材节点">
                                    <p className="px-2 py-1 text-[10px] tracking-wide text-[#687386]">添加到哪个素材节点</p>
                                    {orderedTargets.map((target) => (
                                        <button
                                            key={target.nodeId}
                                            type="button"
                                            role="menuitem"
                                            onClick={() => {
                                                addToTarget(target, asset);
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
                    ))}
                </ul>
            </div>
        </aside>
    );
}
