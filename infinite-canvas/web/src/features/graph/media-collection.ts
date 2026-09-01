import type { GraphMediaItem, GraphMediaType } from "@/features/graph/contracts";

const mediaLabels: Readonly<Record<GraphMediaType, string>> = {
    image: "图片",
    video: "视频",
    audio: "音频",
};

export function mediaItemLabel(mediaType: GraphMediaType, index: number) {
    return `@${mediaLabels[mediaType]}${index + 1}`;
}

export function moveMediaItem(items: readonly GraphMediaItem[], itemId: string, offset: -1 | 1): GraphMediaItem[] | readonly GraphMediaItem[] {
    const index = items.findIndex((item) => item.id === itemId);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= items.length) return items;
    const next = [...items];
    const [item] = next.splice(index, 1);
    next.splice(target, 0, item);
    return next;
}

export function moveMediaItemTo(items: readonly GraphMediaItem[], itemId: string, targetId: string): GraphMediaItem[] | readonly GraphMediaItem[] {
    const index = items.findIndex((item) => item.id === itemId);
    const target = items.findIndex((item) => item.id === targetId);
    if (index < 0 || target < 0 || index === target) return items;
    const next = [...items];
    const [item] = next.splice(index, 1);
    next.splice(target, 0, item);
    return next;
}

export function safeMediaDisplayName(name: string, mediaType: GraphMediaType) {
    const basename = name.replace(/[\u0000-\u001f\u007f]/g, "").split(/[\\/]/).at(-1) ?? "";
    const cleaned = basename.trim().slice(0, 120);
    return cleaned || `${mediaLabels[mediaType]}文件`;
}

export const DEFAULT_MEDIA_COLLECTION_TITLES: Readonly<Record<GraphMediaType, string>> = {
    image: "参考图片",
    video: "参考视频",
    audio: "参考音频",
};

/** 标题为「族名+正整数编号」时返回编号，否则 null。 */
export function mediaCollectionTitleNumber(title: string, mediaType: GraphMediaType): number | null {
    const base = DEFAULT_MEDIA_COLLECTION_TITLES[mediaType];
    if (!title.startsWith(base)) return null;
    const rest = title.slice(base.length);
    if (!/^\d+$/.test(rest)) return null;
    const number = Number(rest);
    return Number.isSafeInteger(number) && number >= 1 ? number : null;
}

/** 标题恰好等于族名（未编号）时为 true。 */
export function isBareDefaultMediaCollectionTitle(title: string, mediaType: GraphMediaType): boolean {
    return title === DEFAULT_MEDIA_COLLECTION_TITLES[mediaType];
}

/** 顺延编号：从既有编号最大值的下一个开始（至少 1）。自定义标题不参与计数。 */
export function nextMediaCollectionTitle(titles: readonly string[], mediaType: GraphMediaType): string {
    const largest = Math.max(0, ...titles.map((title) => mediaCollectionTitleNumber(title, mediaType) ?? 0));
    return `${DEFAULT_MEDIA_COLLECTION_TITLES[mediaType]}${largest + 1}`;
}
