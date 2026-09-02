import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CanvasNodeContextMenu } from "@/components/canvas/canvas-context-menu";
import { CanvasCreateContextMenu } from "@/components/canvas/canvas-create-context-menu";
import { useCanvasUndo } from "@/features/graph/use-canvas-undo";
import { useCanvasStore, type CanvasProject } from "@/stores/canvas/use-canvas-store";

function seedProject(): CanvasProject {
    const project: CanvasProject = {
        id: "project-1",
        title: "测试画布",
        createdAt: "2026-08-28T00:00:00.000Z",
        updatedAt: "2026-08-28T00:00:00.000Z",
        nodes: [{ id: "node-a", type: "text", title: "提示词", position: { x: 0, y: 0 }, width: 300, height: 250, metadata: { status: "idle", graph: { schemaVersion: 1, role: "prompt", text: "", outputPortId: "prompt" } } }],
        connections: [],
        chatSessions: [],
        activeChatId: null,
        backgroundMode: "lines",
        showImageInfo: false,
        viewport: { x: 0, y: 0, k: 1 },
        graphSchemaVersion: 1,
    };
    useCanvasStore.setState({ projects: [project], projectSyncMetadata: {}, syncNotice: null, loadError: null, hydrated: true, projectsLoaded: true });
    return project;
}

function Harness({ onReady }: { onReady: (undoApi: ReturnType<typeof useCanvasUndo>) => void }) {
    const undoApi = useCanvasUndo("project-1");
    onReady(undoApi);
    return null;
}

beforeEach(() => {
    vi.useRealTimers();
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

it("captures undo and redo snapshots and restores project state", () => {
    seedProject();
    let api: ReturnType<typeof useCanvasUndo> | null = null;
    render(<Harness onReady={(value) => { api = value; }} />);
    expect(api).not.toBeNull();

    // 记录基线 → 变更 → undo → redo
    api!.capture();
    const store = () => useCanvasStore.getState().openProject("project-1")!;
    useCanvasStore.getState().updateProject("project-1", { nodes: [...store().nodes, { id: "node-b", type: "text", title: "新节点", position: { x: 40, y: 40 }, width: 300, height: 250, metadata: { status: "idle" } }] });
    expect(store().nodes.map((node) => node.id)).toEqual(["node-a", "node-b"]);

    expect(api!.undo()).toBe(true);
    expect(store().nodes.map((node) => node.id)).toEqual(["node-a"]);

    expect(api!.redo()).toBe(true);
    expect(store().nodes.map((node) => node.id)).toEqual(["node-a", "node-b"]);
});

it("undo and redo are no-ops without history", () => {
    seedProject();
    let api: ReturnType<typeof useCanvasUndo> | null = null;
    render(<Harness onReady={(value) => { api = value; }} />);
    expect(api!.undo()).toBe(false);
    expect(api!.redo()).toBe(false);
});

it("history is capped at 100 entries", () => {
    seedProject();
    let api: ReturnType<typeof useCanvasUndo> | null = null;
    render(<Harness onReady={(value) => { api = value; }} />);
    for (let index = 0; index < 120; index += 1) {
        const current = useCanvasStore.getState().openProject("project-1")!;
        api!.capture();
        useCanvasStore.getState().updateProject("project-1", { nodes: [...current.nodes, { id: `node-${index}`, type: "text", title: "x", position: { x: index, y: index }, width: 100, height: 100, metadata: { status: "idle" } }] });
    }
    let undone = 0;
    while (api!.undo()) undone += 1;
    expect(undone).toBeLessThanOrEqual(100);
});

it("node context menu shows undo redo entries with shortcut hints and disabled states", () => {
    const onUndo = vi.fn();
    const onRedo = vi.fn();
    render(
        <CanvasNodeContextMenu
            menu={{ type: "node", nodeId: "node-a", x: 0, y: 0 }}
            onClose={() => undefined}
            onDelete={() => undefined}
            canUndo={true}
            canRedo={false}
            onUndo={onUndo}
            onRedo={onRedo}
        />,
    );
    expect(screen.getByRole("menuitem", { name: /撤销/ })).not.toBeDisabled();
    expect(screen.getByRole("menuitem", { name: /重做/ })).toBeDisabled();
    expect(screen.getByRole("menuitem", { name: /撤销/ })).toHaveTextContent(/⌘Z|Ctrl\+Z/);
    expect(screen.getByRole("menuitem", { name: /重做/ })).toHaveTextContent(/⇧⌘Z|Ctrl\+Shift\+Z/);
});

it("canvas create context menu shows undo redo entries", () => {
    render(
        <CanvasCreateContextMenu
            menu={{ type: "canvas", x: 0, y: 0, worldPosition: { x: 0, y: 0 } }}
            imageModelDisabled={false}
            videoModelDisabled={false}
            onClose={() => undefined}
            onCreate={() => undefined}
            canUndo={true}
            canRedo={true}
            onUndo={() => undefined}
            onRedo={() => undefined}
        />,
    );
    expect(screen.getByRole("menuitem", { name: /撤销/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /重做/ })).toBeInTheDocument();
});
