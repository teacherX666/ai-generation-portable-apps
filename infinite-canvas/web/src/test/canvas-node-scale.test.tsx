import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { DraggableCanvasNode } from "@/components/canvas/draggable-canvas-node";
import { MAX_NODE_SIZE, MIN_NODE_SIZE } from "@/lib/canvas/node-resize";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    document.body.style.cursor = "";
});

function nodeAt(overrides: Partial<CanvasNodeData> = {}): CanvasNodeData {
    return { id: "node-a", type: CanvasNodeType.Text, title: "Node A", position: { x: 10, y: 20 }, width: 200, height: 100, ...overrides };
}

it("scales the node box and its content proportionally", () => {
    render(
        <DraggableCanvasNode node={nodeAt({ scale: 1.5 })} scale={1} onPositionChange={vi.fn()}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    const box = screen.getByTestId("draggable-node-node-a");
    expect(box.style.width).toBe("300px");
    expect(box.style.height).toBe("150px");
    const inner = screen.getByTestId("node-content-node-a");
    expect(inner.style.transform).toBe("scale(1.5)");
    expect(inner.style.transformOrigin).toBe("top left");
    expect(inner.style.width).toBe("200px");
    expect(inner.style.minHeight).toBe("100px");
});

it("reports the scaled measured size for connections and ports", () => {
    let resizeCallback!: ResizeObserverCallback;
    vi.stubGlobal(
        "ResizeObserver",
        class {
            constructor(callback: ResizeObserverCallback) { resizeCallback = callback; }
            observe = vi.fn();
            disconnect = vi.fn();
        },
    );
    const onMeasuredSize = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt({ scale: 1.5 })} scale={1} onPositionChange={vi.fn()} onMeasuredSize={onMeasuredSize}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    act(() => resizeCallback([{ contentRect: { width: 200, height: 100 } }] as ResizeObserverEntry[], {} as ResizeObserver));
    expect(onMeasuredSize).toHaveBeenCalledWith("node-a", { width: 300, height: 150 });
});

it("keeps port overlays outside the scaled content wrapper so their stacking is not trapped", () => {
    render(
        <DraggableCanvasNode
            node={nodeAt({ scale: 2 })}
            scale={1}
            onPositionChange={vi.fn()}
            overlays={<button type="button" data-testid="port-overlay">port</button>}
        >
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    const box = screen.getByTestId("draggable-node-node-a");
    const content = screen.getByTestId("node-content-node-a");
    const overlay = screen.getByTestId("port-overlay");
    expect(box.contains(overlay)).toBe(true);
    expect(content.contains(overlay)).toBe(false);
});

it("shows a resize handle on a selected node and hides it otherwise", () => {
    const { rerender } = render(
        <DraggableCanvasNode node={nodeAt()} scale={1} selected onPositionChange={vi.fn()} onResize={vi.fn()}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    expect(screen.getByLabelText("拖拽调整节点大小")).toBeInTheDocument();
    rerender(
        <DraggableCanvasNode node={nodeAt()} scale={1} onPositionChange={vi.fn()} onResize={vi.fn()}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    expect(screen.queryByLabelText("拖拽调整节点大小")).toBeNull();
    rerender(
        <DraggableCanvasNode node={nodeAt()} scale={1} selected onPositionChange={vi.fn()}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    expect(screen.queryByLabelText("拖拽调整节点大小")).toBeNull();
});

it("resizes the node freely from the bottom-right corner handle", () => {
    const onResize = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt()} scale={1} selected onPositionChange={vi.fn()} onResize={onResize}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    const handle = screen.getByLabelText("拖拽调整节点大小");
    fireEvent.pointerDown(handle, { button: 0, pointerId: 3, clientX: 300, clientY: 200 });
    fireEvent.pointerMove(window, { pointerId: 3, clientX: 450, clientY: 320 });
    fireEvent.pointerUp(window, { pointerId: 3 });
    expect(onResize).toHaveBeenLastCalledWith("node-a", { width: 350, height: 220 });
    // The pointer gesture must not move the node.
});

it("divides the resize delta by the viewport zoom", () => {
    const onResize = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt()} scale={2} selected onPositionChange={vi.fn()} onResize={onResize}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    fireEvent.pointerDown(screen.getByLabelText("拖拽调整节点大小"), { button: 0, pointerId: 4, clientX: 300, clientY: 200 });
    fireEvent.pointerMove(window, { pointerId: 4, clientX: 500, clientY: 440 });
    fireEvent.pointerUp(window, { pointerId: 4 });
    expect(onResize).toHaveBeenLastCalledWith("node-a", { width: 300, height: 220 });
});

it("does not move the node while dragging the resize handle", () => {
    const onPositionChange = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt()} scale={1} selected onPositionChange={onPositionChange} onResize={vi.fn()}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    const handle = screen.getByLabelText("拖拽调整节点大小");
    fireEvent.pointerDown(handle, { button: 0, pointerId: 5, clientX: 300, clientY: 200 });
    fireEvent.pointerMove(window, { pointerId: 5, clientX: 400, clientY: 300 });
    fireEvent.pointerUp(window, { pointerId: 5 });
    expect(onPositionChange).not.toHaveBeenCalled();
});

it("clamps the resize to the minimum and maximum node sizes", () => {
    const onResize = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt()} scale={1} selected onPositionChange={vi.fn()} onResize={onResize}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    const handle = screen.getByLabelText("拖拽调整节点大小");
    fireEvent.pointerDown(handle, { button: 0, pointerId: 6, clientX: 300, clientY: 200 });
    fireEvent.pointerMove(window, { pointerId: 6, clientX: -9000, clientY: -9000 });
    fireEvent.pointerUp(window, { pointerId: 6 });
    expect(onResize).toHaveBeenLastCalledWith("node-a", { width: MIN_NODE_SIZE.width, height: MIN_NODE_SIZE.height });
    fireEvent.pointerDown(handle, { button: 0, pointerId: 7, clientX: 300, clientY: 200 });
    fireEvent.pointerMove(window, { pointerId: 7, clientX: 30000, clientY: 30000 });
    fireEvent.pointerUp(window, { pointerId: 7 });
    expect(onResize).toHaveBeenLastCalledWith("node-a", { width: MAX_NODE_SIZE.width, height: MAX_NODE_SIZE.height });
});

it("uses a fixed clipped box once the node has been resized", () => {
    render(
        <DraggableCanvasNode node={nodeAt({ resized: true })} scale={1} onPositionChange={vi.fn()}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    const inner = screen.getByTestId("node-content-node-a");
    expect(inner.style.height).toBe("100px");
    expect(inner.style.minHeight).toBe("");
    expect(inner.style.overflow).toBe("hidden");
});
