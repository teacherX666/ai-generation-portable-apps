import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { GenerationNodeCard } from "@/components/canvas/generation-node-card";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";

afterEach(() => cleanup());

const successImageNode: CanvasNodeData = {
    id: "result-a",
    type: CanvasNodeType.Image,
    title: "生成结果",
    position: { x: 0, y: 0 },
    width: 320,
    height: 180,
    metadata: { status: "success", sourceJobId: "job-a", content: "/api/v1/results/job-a/0", requestId: "req-1" },
};

it("opens a full preview on double click and closes via button, backdrop and Escape", () => {
    render(<GenerationNodeCard node={successImageNode} />);
    const surface = screen.getByTitle("双击放大查看");
    fireEvent.doubleClick(surface);
    expect(screen.getByRole("dialog", { name: "生成结果大图预览" })).toBeInTheDocument();
    expect(screen.getByAltText("生成结果大图")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("关闭预览"));
    expect(screen.queryByRole("dialog", { name: "生成结果大图预览" })).not.toBeInTheDocument();

    fireEvent.doubleClick(surface);
    fireEvent.pointerDown(document.querySelector(".fixed.inset-0")!);
    expect(screen.queryByRole("dialog", { name: "生成结果大图预览" })).not.toBeInTheDocument();

    fireEvent.doubleClick(surface);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "生成结果大图预览" })).not.toBeInTheDocument();
});
