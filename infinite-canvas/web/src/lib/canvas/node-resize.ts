export const MIN_NODE_SIZE = Object.freeze({ width: 120, height: 80 });
export const MAX_NODE_SIZE = Object.freeze({ width: 8192, height: 8192 });

/** Rounds and clamps a free-resize size to the supported node bounds. */
export function clampNodeSize(width: number, height: number): { width: number; height: number } {
    const clamp = (value: number, minimum: number, maximum: number) => {
        if (!Number.isFinite(value)) return minimum;
        return Math.max(minimum, Math.min(maximum, Math.round(value)));
    };
    return {
        width: clamp(width, MIN_NODE_SIZE.width, MAX_NODE_SIZE.width),
        height: clamp(height, MIN_NODE_SIZE.height, MAX_NODE_SIZE.height),
    };
}
