import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AssetLibraryPanel } from "@/components/canvas/asset-library-panel";
import type { AssetRef } from "@/api/contracts";
import type { GraphMediaItem } from "@/features/graph/contracts";


afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

const activeAsset: AssetRef = {
    id: "lib-1", kind: "library", status: "active", media_type: "image", mime_type: "image/png",
    size_bytes: 16, content_url: "/api/v1/assets/lib-1/content",
};
const processingAsset: AssetRef = { ...activeAsset, status: "processing" };

const targets = [{ nodeId: "node-images", label: "参考图片", itemCount: 0 }];

it("uploads a portrait into the library, polls it active, and adds it to the media collection", async () => {
    const calls: string[] = [];
    const added: { nodeId: string; items: GraphMediaItem[] }[] = [];
    const upload = vi.fn(async () => { calls.push("upload:library"); return { ...processingAsset } satisfies AssetRef; });
    const fetchAsset = vi.fn(async () => { calls.push("poll:lib-1"); return { ...activeAsset } satisfies AssetRef; });
    const fetchAssets = vi.fn(async () => { calls.push("list"); return [] satisfies AssetRef[]; });
    const addToCollection = vi.fn((nodeId: string, items: GraphMediaItem[]) => { added.push({ nodeId, items }); });

    render(<AssetLibraryPanel targets={targets} onClose={() => undefined} upload={upload} fetchAssets={fetchAssets} fetchAsset={fetchAsset} addToCollection={addToCollection} pollIntervalMs={10} />);
    await screen.findByText("资产库还没有人像，选择图片上传即可。");

    const file = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "portrait.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("选择人像图片"), { target: { files: [file] } });

    await waitFor(() => expect(calls).toContain("poll:lib-1"));
    const addButton = await screen.findByLabelText("添加 lib-1 到素材节点");
    fireEvent.click(addButton);

    expect(added).toHaveLength(1);
    expect(added[0].nodeId).toBe("node-images");
    expect(added[0].items).toEqual([{
        id: expect.any(String) as string,
        assetId: "lib-1",
        displayName: "lib-1",
        mimeType: "image/png",
        bytes: 16,
        kind: "library",
    }]);
});

it("rejects non-image files before any upload and disables adding failed assets", async () => {
    const upload = vi.fn();
    const fetchAssets = vi.fn(async () => [{ ...activeAsset, status: "failed" } satisfies AssetRef]);
    const fetchAsset = vi.fn();

    render(<AssetLibraryPanel targets={targets} onClose={() => undefined} upload={upload} fetchAssets={fetchAssets} fetchAsset={fetchAsset} />);

    const file = new File([new Uint8Array(8)], "clip.mp4", { type: "video/mp4" });
    fireEvent.change(screen.getByLabelText("选择人像图片"), { target: { files: [file] } });
    await screen.findByText("只支持 10MB 以内的 PNG/JPEG/WebP 人像图。");
    expect(upload).not.toHaveBeenCalled();

    await screen.findByText("处理失败");
    expect(screen.getByLabelText("添加 lib-1 到素材节点")).toBeDisabled();
});

it("shows a hint instead of an add button when no image media node exists", async () => {
    const fetchAssets = vi.fn(async () => [] satisfies AssetRef[]);

    render(<AssetLibraryPanel targets={[]} onClose={() => undefined} upload={vi.fn()} fetchAssets={fetchAssets} fetchAsset={vi.fn()} />);

    await screen.findByText("先在画布中添加一个图片素材节点，再从这里添加人像。");
    expect(screen.queryByRole("menu", { name: "选择目标素材节点" })).not.toBeInTheDocument();
});

it("offers a remembered-choice picker when several image media nodes exist", async () => {
    const added: { nodeId: string; items: GraphMediaItem[] }[] = [];
    const addToCollection = vi.fn((nodeId: string, items: GraphMediaItem[]) => { added.push({ nodeId, items }); });
    const fetchAssets = vi.fn(async () => [{ ...activeAsset, id: "lib-2" } satisfies AssetRef]);
    const twoTargets = [
        { nodeId: "node-a", label: "参考图A", itemCount: 2 },
        { nodeId: "node-b", label: "参考图B", itemCount: 0 },
    ];

    render(<AssetLibraryPanel targets={twoTargets} onClose={() => undefined} fetchAssets={fetchAssets} fetchAsset={vi.fn()} addToCollection={addToCollection} />);

    fireEvent.click(await screen.findByLabelText("添加 lib-2 到素材节点"));
    const menu = await screen.findByRole("menu", { name: "选择目标素材节点" });
    expect(menu).toBeInTheDocument();
    fireEvent.click(screen.getByText("参考图B（0 项）"));

    expect(addToCollection).toHaveBeenCalledWith("node-b", expect.anything());
    expect(added).toHaveLength(1);
    expect(added[0].nodeId).toBe("node-b");

    // 记住选择：再次打开选择器，上次目标排在最前
    fireEvent.click(screen.getByLabelText("添加 lib-2 到素材节点"));
    const menuItems = await screen.findAllByRole("menuitem");
    expect(menuItems[0]).toHaveTextContent("参考图B");
});

it("deletes an asset after confirmation and refreshes the list", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const deleteAsset = vi.fn(async () => undefined);
    const fetchAssets = vi.fn(async () => [{ ...activeAsset } satisfies AssetRef]);

    render(<AssetLibraryPanel targets={[]} onClose={() => undefined} fetchAssets={fetchAssets} fetchAsset={vi.fn()} deleteAsset={deleteAsset} />);

    fireEvent.click(await screen.findByLabelText("删除素材 lib-1"));
    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(deleteAsset).toHaveBeenCalledWith("lib-1"));
});
