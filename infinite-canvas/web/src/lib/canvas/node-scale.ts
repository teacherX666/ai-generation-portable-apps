export const MIN_NODE_SCALE = 0.25;
export const MAX_NODE_SCALE = 4;

/** Effective scale of a node; stored values are clamped and missing values default to 1. */
export function nodeScaleOf(node: { scale?: number }): number {
    if (typeof node.scale !== "number" || !Number.isFinite(node.scale)) return 1;
    return Math.min(Math.max(node.scale, MIN_NODE_SCALE), MAX_NODE_SCALE);
}

/** Normalizes a stored scale value; returns undefined when the value is unusable. */
export function normalizeNodeScale(value: unknown): number | undefined {
    if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
    return Math.round(Math.min(Math.max(value, MIN_NODE_SCALE), MAX_NODE_SCALE) * 100) / 100;
}
