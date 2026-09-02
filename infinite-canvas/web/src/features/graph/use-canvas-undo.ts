import { useCallback, useRef, useState } from "react";

import { useCanvasStore } from "@/stores/canvas/use-canvas-store";
import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

const MAX_HISTORY = 100;

export type CanvasHistoryEntry = { nodes: CanvasNodeData[]; connections: CanvasConnection[] };

export function useCanvasUndo(projectId: string) {
    const undoStackRef = useRef<CanvasHistoryEntry[]>([]);
    const redoStackRef = useRef<CanvasHistoryEntry[]>([]);
    const [, bump] = useState(0);
    const refresh = () => bump((value) => value + 1);

    const snapshot = useCallback((): CanvasHistoryEntry | null => {
        const current = useCanvasStore.getState().openProject(projectId);
        if (!current) return null;
        return { nodes: current.nodes, connections: current.connections };
    }, [projectId]);

    const capture = useCallback(() => {
        const entry = snapshot();
        if (!entry) return;
        undoStackRef.current.push(entry);
        if (undoStackRef.current.length > MAX_HISTORY) undoStackRef.current.shift();
        redoStackRef.current = [];
        refresh();
    }, [snapshot]);

    const undo = useCallback((): boolean => {
        const entry = undoStackRef.current.pop();
        if (!entry) return false;
        const current = snapshot();
        if (!current) {
            undoStackRef.current.push(entry);
            return false;
        }
        redoStackRef.current.push(current);
        useCanvasStore.getState().updateProject(projectId, { nodes: entry.nodes, connections: entry.connections });
        refresh();
        return true;
    }, [projectId, snapshot]);

    const redo = useCallback((): boolean => {
        const entry = redoStackRef.current.pop();
        if (!entry) return false;
        const current = snapshot();
        if (!current) {
            redoStackRef.current.push(entry);
            return false;
        }
        undoStackRef.current.push(current);
        useCanvasStore.getState().updateProject(projectId, { nodes: entry.nodes, connections: entry.connections });
        refresh();
        return true;
    }, [projectId, snapshot]);

    return {
        capture,
        undo,
        redo,
        canUndo: undoStackRef.current.length > 0,
        canRedo: redoStackRef.current.length > 0,
    };
}
