import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, ChevronUp, GripVertical, Plus, Trash2, X } from "lucide-react";
import { nanoid } from "nanoid";

import { deleteMediaAsset, uploadMediaAsset } from "@/api/assets";
import { safeApiPath } from "@/api/client";
import type { OwnedMediaAsset } from "@/api/contracts";
import type { GraphMediaItem, GraphMediaType } from "@/features/graph/contracts";
import { mediaItemLabel, moveMediaItem, moveMediaItemTo, safeMediaDisplayName } from "@/features/graph/media-collection";
import { sharedMediaUploadScheduler } from "@/features/graph/media-upload-scheduler";
import { formatBytes } from "@/lib/image-utils";
import type { CanvasNodeData } from "@/types/canvas";

type UploadFunction = (file: File, mediaType: GraphMediaType, onProgress: (percent: number) => void, signal: AbortSignal) => Promise<OwnedMediaAsset>;
export type MediaItemsUpdater = (current: readonly GraphMediaItem[]) => GraphMediaItem[];

type MediaCollectionNodeProps = {
    node: CanvasNodeData;
    readOnly?: boolean;
    onItemsChange: (update: MediaItemsUpdater) => boolean | void;
    upload?: UploadFunction;
    removeAsset?: (assetId: string) => Promise<void>;
};

type PendingUpload = {
    id: string;
    name: string;
    progress: number;
    failed: boolean;
};

type QueuedUpload = {
    id: string;
    file: File;
    name: string;
    previewUrl: string | null;
    controller: AbortController;
    readonly scope: UploadScope;
    readonly order: number;
};

type UploadScope = Readonly<{ nodeId: string; mediaType: GraphMediaType; generation: number; disabled: boolean }>;
const MAX_MEDIA_ITEMS = 30;

function sameScope(left: UploadScope, right: UploadScope) {
    return left.nodeId === right.nodeId && left.mediaType === right.mediaType && left.generation === right.generation && !right.disabled;
}

const copyByType: Readonly<Record<GraphMediaType, { noun: string; accept: string }>> = {
    image: { noun: "图片", accept: "image/png,image/jpeg,image/webp" },
    video: { noun: "视频", accept: "video/mp4,video/webm" },
    audio: { noun: "音频", accept: "audio/mpeg,audio/wav" },
};

function MediaPreview({ mediaType, item, label, nodeWidth, onView }: { mediaType: GraphMediaType; item: GraphMediaItem; label: string; nodeWidth: number; onView?: (item: GraphMediaItem) => void }) {
    const source = safeApiPath(`/api/v1/assets/${encodeURIComponent(item.assetId)}/content`);
    const accessibleName = `${label} ${item.displayName}`;
    // 缩略图随节点宽度缩放，自由拖拽节点时预览跟着变（上游 47dc69a）
    const thumbWidth = Math.max(48, Math.min(160, Math.round(nodeWidth * 0.22)));
    if (mediaType === "image") {
        return (
            <button type="button" aria-label={`查看 ${label} 详情`} title="点击查看大图" onClick={() => onView?.(item)} className="shrink-0 cursor-zoom-in rounded-md border border-[#d9e0ea] p-0 hover:border-[#235fd6]">
                <img src={source} alt={accessibleName} style={{ width: thumbWidth, height: Math.round(thumbWidth * 0.8) }} className="rounded-md object-cover" />
            </button>
        );
    }
    if (mediaType === "video") return <video src={source} aria-label={accessibleName} controls preload="metadata" style={{ width: Math.round(thumbWidth * 1.2), height: Math.round(thumbWidth * 0.8) }} className="rounded-md border border-[#d9e0ea] bg-black object-cover" />;
    return <audio src={source} aria-label={accessibleName} controls preload="metadata" className="h-9 w-40 max-w-full" />;
}

function MediaPreviewDialog({ item, label, onClose }: { item: GraphMediaItem; label: string; onClose: () => void }) {
    const source = safeApiPath(`/api/v1/assets/${encodeURIComponent(item.assetId)}/content`);
    const [dimensions, setDimensions] = useState<{ width: number; height: number } | null>(item.width && item.height ? { width: item.width, height: item.height } : null);

    useEffect(() => {
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") onClose();
        };
        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, [onClose]);

    // Portal 到 body（画布 transform 层内 fixed 会被缩放裁剪，长图被截断）；
    // data-canvas-no-zoom + stopPropagation 防止画布把对话框点击当成背景平移
    // 捕获指针（上游 cb1f3c9）。
    return createPortal(
        <div
            className="fixed inset-0 z-[90] flex items-center justify-center bg-black/65 p-6"
            data-canvas-no-zoom
            onPointerDown={(event) => {
                event.stopPropagation();
                if (event.target === event.currentTarget) onClose();
            }}
        >
            <section role="dialog" aria-modal="true" aria-label={`${label} 大图预览`} className="flex max-h-full w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-[#d9e0ea] bg-[#f8fafc] text-[#172033] shadow-2xl">
                <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[#e2e8f0] px-4 py-2.5">
                    <h2 className="min-w-0 truncate text-sm font-semibold">{item.displayName}</h2>
                    <button type="button" aria-label="关闭预览" onClick={onClose} className="shrink-0 rounded-lg border border-[#c3ccd9] px-2.5 py-1 text-xs text-[#687386] hover:bg-[#eef2f7]">关闭</button>
                </header>
                <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-black/40 p-4">
                    <img
                        src={source}
                        alt={`${label} ${item.displayName}`}
                        className="max-h-full max-w-full rounded-md"
                        onLoad={(event) => {
                            const image = event.currentTarget;
                            if (!dimensions && image.naturalWidth > 0 && image.naturalHeight > 0) setDimensions({ width: image.naturalWidth, height: image.naturalHeight });
                        }}
                    />
                </div>
                <footer className="shrink-0 space-y-1 border-t border-[#e2e8f0] px-4 py-2.5 text-xs text-[#687386]">
                    <p>素材编号：{label}</p>
                    <p>格式：{item.mimeType}</p>
                    {item.bytes > 0 ? <p>大小：{formatBytes(item.bytes)}</p> : null}
                    {dimensions ? <p>尺寸：{dimensions.width} × {dimensions.height}</p> : null}
                </footer>
            </section>
        </div>,
        document.body,
    );
}

function isAbortError(error: unknown) {
    return error instanceof DOMException && error.name === "AbortError";
}

export function MediaCollectionNode({ node, readOnly = false, onItemsChange, upload = uploadMediaAsset, removeAsset = deleteMediaAsset }: MediaCollectionNodeProps) {
    const graph = node.metadata?.graph;
    if (graph?.role !== "media-collection") return null;
    const { mediaType, items } = graph;
    const details = copyByType[mediaType];
    const [pending, setPending] = useState<PendingUpload[]>([]);
    const [selectionError, setSelectionError] = useState<string | null>(null);
    const [viewing, setViewing] = useState<{ item: GraphMediaItem; label: string } | null>(null);
    const mountedRef = useRef(true);
    const activeRef = useRef(!readOnly);
    const generationRef = useRef(0);
    const scopeRef = useRef<UploadScope>({ nodeId: node.id, mediaType, generation: 0, disabled: readOnly });
    const uploadRef = useRef(upload);
    const removeAssetRef = useRef(removeAsset);
    const onItemsChangeRef = useRef(onItemsChange);
    const queueRef = useRef<QueuedUpload[][]>([]);
    const processingRef = useRef(false);
    const entriesRef = useRef(new Map<string, QueuedUpload>());
    const cancelledRef = useRef(new Set<string>());
    const objectUrlsRef = useRef(new Set<string>());
    const draggedItemRef = useRef<string | null>(null);
    const itemOrderRef = useRef(new Map<string, number>());
    const nextOrderRef = useRef(0);
    const drainQueueRef = useRef<() => Promise<void>>(async () => undefined);
    uploadRef.current = upload;
    removeAssetRef.current = removeAsset;
    onItemsChangeRef.current = onItemsChange;
    for (const item of items) {
        if (!itemOrderRef.current.has(item.id)) {
            itemOrderRef.current.set(item.id, nextOrderRef.current);
            nextOrderRef.current += 1;
        }
    }

    const releaseEntry = (entry: QueuedUpload) => {
        if (entry.previewUrl && objectUrlsRef.current.delete(entry.previewUrl)) URL.revokeObjectURL(entry.previewUrl);
        entriesRef.current.delete(entry.id);
        cancelledRef.current.delete(entry.id);
    };

    const cancelEntry = (id: string, updateUi = true) => {
        const entry = entriesRef.current.get(id);
        if (!entry) return;
        cancelledRef.current.add(id);
        entry.controller.abort();
        releaseEntry(entry);
        if (updateUi && mountedRef.current) setPending((current) => current.filter((candidate) => candidate.id !== id));
    };

    const cancelAll = (updateUi = true) => {
        activeRef.current = false;
        for (const id of [...entriesRef.current.keys()]) cancelEntry(id, false);
        queueRef.current = [];
        cancelledRef.current.clear();
        if (updateUi && mountedRef.current) {
            setPending([]);
            setSelectionError(null);
        }
    };

    const discardAsset = (assetId: string) => {
        void removeAssetRef.current(assetId).catch(() => undefined);
    };

    drainQueueRef.current = async () => {
        if (processingRef.current) return;
        processingRef.current = true;
        try {
            while (queueRef.current.length) {
                const batch = queueRef.current.shift() ?? [];
                const results = await Promise.all(batch.map(async (entry) => {
                    if (cancelledRef.current.has(entry.id) || entry.controller.signal.aborted) return { entry, asset: null, cancelled: true } as const;
                    if (mountedRef.current) setPending((current) => current.map((candidate) => candidate.id === entry.id ? { ...candidate, progress: 0 } : candidate));
                    try {
                        const asset = await sharedMediaUploadScheduler.schedule(() => uploadRef.current(entry.file, entry.scope.mediaType, (progress) => {
                            if (mountedRef.current && !cancelledRef.current.has(entry.id)) setPending((current) => current.map((candidate) => candidate.id === entry.id ? { ...candidate, progress } : candidate));
                        }, entry.controller.signal), entry.controller.signal);
                        if (entry.controller.signal.aborted || cancelledRef.current.has(entry.id) || !activeRef.current || !mountedRef.current || !sameScope(entry.scope, scopeRef.current)) {
                            discardAsset(asset.id);
                            return { entry, asset: null, cancelled: true } as const;
                        }
                        if (asset.status !== "active" || asset.media_type !== entry.scope.mediaType) {
                            discardAsset(asset.id);
                            return { entry, asset: null, cancelled: true } as const;
                        }
                        return { entry, asset, cancelled: false } as const;
                    } catch (error) {
                        return { entry, asset: null, cancelled: entry.controller.signal.aborted || isAbortError(error) } as const;
                    }
                }));
                const accepted = results.flatMap(({ entry, asset, cancelled }) => asset && !cancelled ? [{ item: {
                    id: nanoid(), assetId: asset.id, displayName: entry.name, mimeType: asset.mime_type, bytes: asset.size_bytes,
                }, order: entry.order }] : []);
                if (accepted.length) {
                    if (activeRef.current && mountedRef.current) {
                        let persisted: boolean | void = false;
                        try {
                            for (const value of accepted) itemOrderRef.current.set(value.item.id, value.order);
                            persisted = onItemsChangeRef.current((current) => {
                                const next = [...current];
                                for (const value of accepted) {
                                    const index = next.findIndex((candidate) => (itemOrderRef.current.get(candidate.id) ?? Number.MAX_SAFE_INTEGER) > value.order);
                                    if (index < 0) next.push(value.item);
                                    else next.splice(index, 0, value.item);
                                }
                                return next;
                            });
                        } catch {
                            persisted = false;
                        }
                        if (persisted === false) for (const value of accepted) discardAsset(value.item.assetId);
                    } else {
                        for (const value of accepted) discardAsset(value.item.assetId);
                    }
                }
                const failures = new Set(results.filter((result) => !result.asset && !result.cancelled).map((result) => result.entry.id));
                for (const result of results) if (!failures.has(result.entry.id)) releaseEntry(result.entry);
                for (const entry of batch) cancelledRef.current.delete(entry.id);
                if (mountedRef.current) setPending((current) => current.flatMap((candidate) => {
                    if (failures.has(candidate.id)) return [{ ...candidate, failed: true }];
                    return results.some((result) => result.entry.id === candidate.id) ? [] : [candidate];
                }));
            }
        } finally {
            processingRef.current = false;
        }
    };

    useLayoutEffect(() => {
        const previous = scopeRef.current;
        if (previous.nodeId !== node.id || previous.mediaType !== mediaType || readOnly) {
            generationRef.current += 1;
            cancelAll();
            if (previous.nodeId !== node.id || previous.mediaType !== mediaType) {
                itemOrderRef.current.clear();
                nextOrderRef.current = 0;
                for (const item of items) {
                    itemOrderRef.current.set(item.id, nextOrderRef.current);
                    nextOrderRef.current += 1;
                }
            }
        }
        scopeRef.current = { nodeId: node.id, mediaType, generation: generationRef.current, disabled: readOnly };
        activeRef.current = !readOnly;
    }, [node.id, mediaType, readOnly]);

    useEffect(() => () => {
        mountedRef.current = false;
        cancelAll(false);
    }, []);

    const handleFiles = (files: File[]) => {
        if (readOnly || files.length === 0) return;
        const available = Math.max(0, MAX_MEDIA_ITEMS - items.length - entriesRef.current.size);
        const selected = files.slice(0, available);
        setSelectionError(files.length > available ? `每个集合最多 ${MAX_MEDIA_ITEMS} 个媒体文件，已忽略 ${files.length - available} 个。` : null);
        if (!selected.length) return;
        const scope = Object.freeze({ ...scopeRef.current });
        const batch = selected.map((file) => {
            const previewUrl = typeof URL.createObjectURL === "function" ? URL.createObjectURL(file) : null;
            if (previewUrl) objectUrlsRef.current.add(previewUrl);
            const entry: QueuedUpload = { id: nanoid(), file, name: safeMediaDisplayName(file.name, mediaType), previewUrl, controller: new AbortController(), scope, order: nextOrderRef.current++ };
            entriesRef.current.set(entry.id, entry);
            return entry;
        });
        setPending((current) => [...current, ...batch.map((entry) => ({ id: entry.id, name: entry.name, progress: 0, failed: false }))]);
        queueRef.current.push(batch);
        void drainQueueRef.current();
    };

    const retryEntry = (id: string) => {
        const entry = entriesRef.current.get(id);
        if (!entry || readOnly || !sameScope(entry.scope, scopeRef.current)) return;
        entry.controller = new AbortController();
        setPending((current) => current.map((candidate) => candidate.id === id ? { ...candidate, failed: false, progress: 0 } : candidate));
        queueRef.current.push([entry]);
        void drainQueueRef.current();
    };

    const overflowing = items.length + pending.length > 8;
    const resized = node.resized === true;
    const viewingDialog = viewing && items.some((candidate) => candidate.id === viewing.item.id) ? viewing : null;

    return <> <article className="flex h-full flex-col overflow-hidden rounded-xl border border-[#d9e0ea] bg-white text-[#172033] shadow-[0_12px_36px_rgba(0,0,0,0.36)]">
        <header className="flex shrink-0 items-center justify-between border-b border-[#232b3e] px-3 py-2">
            <div><p className="text-[10px] tracking-[0.16em] text-[#235fd6]">MEDIA INPUT</p><h2 className="text-sm font-semibold">{node.title}</h2></div>
            {!readOnly ? <label className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-[#c3ccd9] bg-[#eef5ff] px-2 py-1 text-xs text-[#465267] hover:border-[#235fd6]">
                <Plus className="size-3.5" />添加
                <input className="sr-only" type="file" multiple accept={details.accept} aria-label={`添加${details.noun}`} onChange={(event) => {
                    const selected = Array.from(event.currentTarget.files ?? []);
                    event.currentTarget.value = "";
                    handleFiles(selected);
                }} />
            </label> : null}
        </header>
        <ol data-overflowing={String(overflowing)} className={`${resized ? "min-h-0 flex-1 overflow-y-auto" : overflowing ? "max-h-80 overflow-y-auto" : ""} space-y-2 p-2`}>
            {selectionError ? <li role="alert" className="rounded-lg border border-[#f3c5c0] bg-[#fee2e2] px-3 py-2 text-xs text-[#b91c1c]">{selectionError}</li> : null}
            {items.map((item, index) => {
                const label = mediaItemLabel(mediaType, index);
                return <li key={item.id} data-testid={`media-item-${item.id}`} draggable={!readOnly || undefined}
                    onDragStart={() => { if (!readOnly) draggedItemRef.current = item.id; }}
                    onDragOver={(event) => { if (!readOnly) event.preventDefault(); }}
                    onDrop={(event) => {
                        if (readOnly) return;
                        event.preventDefault();
                        const dragged = draggedItemRef.current;
                        draggedItemRef.current = null;
                        if (dragged) onItemsChange((current) => [...moveMediaItemTo(current, dragged, item.id)]);
                    }}
                    className="flex items-center gap-2 rounded-lg border border-[#21283a] bg-[#eef2f7] p-2">
                    {!readOnly ? <GripVertical className="size-4 shrink-0 text-[#666d7b]" aria-hidden="true" /> : null}
                    <MediaPreview mediaType={mediaType} item={item} label={label} nodeWidth={node.width} onView={(mediaItem) => setViewing({ item: mediaItem, label })} />
                    <div className="min-w-0 flex-1"><p className="text-xs font-medium text-[#465267]">{label}</p>{readOnly
                        ? <p className="truncate text-[11px] text-[#687386]">{item.displayName}</p>
                        : <input key={`${item.id}:${item.displayName}`} aria-label={`重命名 ${label}`} defaultValue={item.displayName} onBlur={(event) => {
                            const displayName = safeMediaDisplayName(event.currentTarget.value, mediaType);
                            onItemsChange((current) => current.map((candidate) => candidate.id === item.id ? { ...candidate, displayName } : candidate));
                        }} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} className="w-full rounded border border-transparent bg-transparent text-[11px] text-[#687386] outline-none focus:border-[#c3ccd9] focus:bg-[#ffffff]" />}</div>
                    {!readOnly ? <div className="flex shrink-0 items-center gap-1">
                        <button type="button" aria-label={`上移 ${label}`} disabled={index === 0} onClick={() => onItemsChange((current) => [...moveMediaItem(current, item.id, -1)])} className="rounded p-1 text-[#687386] hover:bg-[#eef2f7] disabled:opacity-30"><ChevronUp className="size-3.5" /></button>
                        <button type="button" aria-label={`下移 ${label}`} disabled={index === items.length - 1} onClick={() => onItemsChange((current) => [...moveMediaItem(current, item.id, 1)])} className="rounded p-1 text-[#687386] hover:bg-[#eef2f7] disabled:opacity-30"><ChevronDown className="size-3.5" /></button>
                        <button type="button" aria-label={`移除 ${label}`} onClick={() => onItemsChange((current) => current.filter((candidate) => candidate.id !== item.id))} className="rounded p-1 text-[#b91c1c] hover:bg-[#f3c5c0]"><Trash2 className="size-3.5" /></button>
                    </div> : null}
                </li>;
            })}
            {pending.map((entry) => <li key={entry.id} role={entry.failed ? "alert" : "status"} className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-xs ${entry.failed ? "border-[#f3c5c0] bg-[#fee2e2] text-[#b91c1c]" : "border-[#c3d6f7] bg-[#eef5ff] text-[#465267]"}`}>
                <span className="min-w-0 flex-1 truncate">{entry.failed ? `${entry.name} 上传失败，请重试。` : `${entry.name} · ${entry.progress}%`}</span>
                {!readOnly && entry.failed ? <><button type="button" aria-label={`重试 ${entry.name}`} onClick={() => retryEntry(entry.id)} className="rounded px-2 py-1 hover:bg-[#fee2e2]">重试</button><button type="button" aria-label={`移除 ${entry.name}`} onClick={() => cancelEntry(entry.id)} className="rounded px-2 py-1 hover:bg-[#fee2e2]">移除错误</button></> : null}
                {!entry.failed && !readOnly ? <button type="button" aria-label={`取消上传 ${entry.name}`} onClick={() => cancelEntry(entry.id)} className="rounded p-1 hover:bg-[#eef5ff]"><X className="size-3.5" /></button> : null}
            </li>)}
            {items.length === 0 && pending.length === 0 ? <li className="rounded-lg border border-dashed border-[#343d52] px-4 py-7 text-center text-xs text-[#687386]">添加一个或多个{details.noun}，顺序会决定 @引用编号。</li> : null}
        </ol>
    </article>
    {viewingDialog ? <MediaPreviewDialog item={viewingDialog.item} label={viewingDialog.label} onClose={() => setViewing(null)} /> : null}
</>;
}
