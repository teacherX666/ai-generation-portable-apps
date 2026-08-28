import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AssetLibraryPanel } from "@/components/canvas/asset-library-panel";
import type { AssetRef } from "@/api/contracts";
import type { LibraryGroup, LibraryGroupAsset, LibraryGroupLocalRow } from "@/api/assets";
import type { GraphMediaItem } from "@/features/graph/contracts";


afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

const groupA: LibraryGroup = { group_id: "group-a", name: "默认组", created_at: "2026-01-01" };

const localRow = (overrides: Partial<LibraryGroupLocalRow> = {}): LibraryGroupLocalRow => ({
    asset_id: "lib-1", kind: "library", status: "active", media_type: "image",
    mime_type: "image/png", size_bytes: 16,
    content_url: "/api/v1/assets/lib-1/content", upstream_asset_id: "asset-up1",
    ...overrides,
});

const groupItem = (overrides: Partial<LibraryGroupAsset> = {}): LibraryGroupAsset => ({
    asset_id: "asset-up1", name: "portrait", status: "active", media_type: "image",
    error_message: null, local: localRow(), ...overrides,
});

const targets = [{ nodeId: "node-images", label: "参考图片", itemCount: 0 }];

const groupStubs = (items: LibraryGroupAsset[] = []) => {
    const fetchGroups = vi.fn(async () => [groupA]);
    const fetchGroupAssets = vi.fn(async () => items);
    const fetchAsset = vi.fn(async () => localRowToRef(items[0]?.local));
    return { fetchGroups, fetchGroupAssets, fetchAsset };
};

function localRowToRef(row: LibraryGroupLocalRow | undefined | null): AssetRef {
    if (!row) throw new Error("no local row");
    return {
        id: row.asset_id, kind: "library", status: row.status, media_type: row.media_type,
        mime_type: row.mime_type, size_bytes: row.size_bytes,
        content_url: row.content_url, upstream_asset_id: row.upstream_asset_id,
    };
}

it("uploads a portrait into the group, polls it active, and adds it to the media collection", async () => {
    const calls: string[] = [];
    const added: { nodeId: string; items: GraphMediaItem[] }[] = [];
    const uploaded: AssetRef = {
        id: "lib-1", kind: "library", status: "processing", media_type: "image",
        mime_type: "image/png", size_bytes: 16, content_url: "/api/v1/assets/lib-1/content",
        upstream_asset_id: "asset-up1",
    };
    const upload = vi.fn(async () => { calls.push("upload:library"); return { ...uploaded }; });
    const fetchGroups = vi.fn(async () => { calls.push("list:groups"); return [groupA]; });
    const fetchGroupAssets = vi.fn(async (groupId: string) => {
        calls.push(`list:${groupId}`);
        return [groupItem({ status: "processing", local: localRow({ status: "processing" }) })];
    });
    const fetchAsset = vi.fn(async () => { calls.push("poll:lib-1"); return { ...uploaded, status: "active" as const }; });
    const addToCollection = vi.fn((nodeId: string, items: GraphMediaItem[]) => { added.push({ nodeId, items }); });

    render(<AssetLibraryPanel targets={targets} onClose={() => undefined} upload={upload} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={fetchAsset} addToCollection={addToCollection} pollIntervalMs={10} />);

    const file = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "portrait.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("选择人像图片"), { target: { files: [file] } });

    await waitFor(() => expect(calls).toContain("upload:library"));
    await waitFor(() => expect(calls).toContain("poll:lib-1"));
    const addButton = await screen.findByLabelText("添加 portrait 到素材节点");
    await waitFor(() => expect(addButton).not.toBeDisabled());
    fireEvent.click(addButton);

    expect(added).toHaveLength(1);
    expect(added[0].nodeId).toBe("node-images");
    expect(added[0].items).toEqual([{
        id: expect.any(String) as string,
        assetId: "lib-1",
        displayName: "portrait",
        mimeType: "image/png",
        bytes: 16,
        kind: "library",
    }]);
});

it("rejects non-image files before any upload and disables adding failed assets with the upstream error", async () => {
    const upload = vi.fn();
    const { fetchGroups, fetchGroupAssets } = groupStubs([
        groupItem({ status: "failed", local: null, error_message: "宽高比超出范围" }),
    ]);

    render(<AssetLibraryPanel targets={targets} onClose={() => undefined} upload={upload} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={vi.fn()} />);

    const file = new File([new Uint8Array(8)], "clip.mp4", { type: "video/mp4" });
    fireEvent.change(screen.getByLabelText("选择人像图片"), { target: { files: [file] } });
    await screen.findByText("只支持 10MB 以内的 PNG/JPEG/WebP 人像图。");
    expect(upload).not.toHaveBeenCalled();

    await screen.findByText("处理失败");
    await screen.findByText("宽高比超出范围");
    expect(screen.getByLabelText("添加 portrait 到素材节点")).toBeDisabled();
});

it("shows a hint instead of an add button when no image media node exists", async () => {
    const { fetchGroups, fetchGroupAssets } = groupStubs([groupItem()]);

    render(<AssetLibraryPanel targets={[]} onClose={() => undefined} upload={vi.fn()} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={vi.fn()} />);

    const addButton = await screen.findByLabelText("添加 portrait 到素材节点");
    expect(addButton).toBeDisabled();
    await screen.findByText("先在画布中添加一个图片素材节点，再从这里添加人像。");
    expect(screen.queryByRole("menu", { name: "选择目标素材节点" })).not.toBeInTheDocument();
});

it("offers a remembered-choice picker when several image media nodes exist", async () => {
    const added: { nodeId: string; items: GraphMediaItem[] }[] = [];
    const addToCollection = vi.fn((nodeId: string, items: GraphMediaItem[]) => { added.push({ nodeId, items }); });
    const { fetchGroups, fetchGroupAssets } = groupStubs([groupItem({ asset_id: "asset-up2", name: "portrait-2", local: localRow({ asset_id: "lib-2", upstream_asset_id: "asset-up2" }) })]);
    const twoTargets = [
        { nodeId: "node-a", label: "参考图A", itemCount: 2 },
        { nodeId: "node-b", label: "参考图B", itemCount: 0 },
    ];

    render(<AssetLibraryPanel targets={twoTargets} onClose={() => undefined} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={vi.fn()} addToCollection={addToCollection} />);

    fireEvent.click(await screen.findByLabelText("添加 portrait-2 到素材节点"));
    const menu = await screen.findByRole("menu", { name: "选择目标素材节点" });
    expect(menu).toBeInTheDocument();
    fireEvent.click(screen.getByText("参考图B（0 项）"));

    expect(addToCollection).toHaveBeenCalledWith("node-b", expect.anything());
    expect(added).toHaveLength(1);
    expect(added[0].nodeId).toBe("node-b");

    // 记住选择：再次打开选择器，上次目标排在最前
    fireEvent.click(screen.getByLabelText("添加 portrait-2 到素材节点"));
    const menuItems = await screen.findAllByRole("menuitem");
    expect(menuItems[0]).toHaveTextContent("参考图B");
});

it("deletes an asset after confirmation and refreshes the group assets", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const deleteGroupAsset = vi.fn(async () => undefined);
    const { fetchGroups, fetchGroupAssets } = groupStubs([groupItem()]);

    render(<AssetLibraryPanel targets={[]} onClose={() => undefined} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={vi.fn()} deleteGroupAsset={deleteGroupAsset} />);

    fireEvent.click(await screen.findByLabelText("删除素材 portrait"));
    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(deleteGroupAsset).toHaveBeenCalledWith("group-a", "asset-up1"));
    // 删除成功后整组刷新
    expect(fetchGroupAssets).toHaveBeenCalledWith("group-a");
});

it("switches the current group and lists that group's assets", async () => {
    const groupB: LibraryGroup = { group_id: "group-b", name: "B组", created_at: "2026-02-01" };
    const fetchGroups = vi.fn(async () => [groupA, groupB]);
    const fetchGroupAssets = vi.fn(async (groupId: string) => {
        if (groupId === "group-a") return [groupItem()];
        return [groupItem({ asset_id: "asset-b1", name: "B组素材", local: localRow({ asset_id: "lib-b", upstream_asset_id: "asset-b1" }) })];
    });

    render(<AssetLibraryPanel targets={targets} onClose={() => undefined} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={vi.fn()} />);

    await screen.findByText("portrait");
    fireEvent.change(screen.getByLabelText("选择素材组"), { target: { value: "group-b" } });
    await screen.findByText("B组素材");
    expect(screen.queryByText("portrait")).not.toBeInTheDocument();
});

it("creates a new group and switches the selector to it", async () => {
    const created = { group_id: "group-new", name: "新分组" };
    const createGroup = vi.fn(async (name: string) => {
        expect(name).toBe("新分组");
        return created;
    });
    const fetchGroups = vi.fn()
        .mockResolvedValueOnce([groupA])
        .mockResolvedValue([groupA, created]);
    const fetchGroupAssets = vi.fn(async () => []);

    render(<AssetLibraryPanel targets={targets} onClose={() => undefined} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={vi.fn()} createGroup={createGroup} />);

    fireEvent.change(await screen.findByLabelText("新分组名字"), { target: { value: "新分组" } });
    fireEvent.click(screen.getByLabelText("新建素材组"));

    await waitFor(() => expect(createGroup).toHaveBeenCalledWith("新分组"));
    const selector = screen.getByLabelText("选择素材组") as HTMLSelectElement;
    await waitFor(() => expect(selector.value).toBe("group-new"));
});

it("shows the backend message when deleting the protected default group", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const deleteGroup = vi.fn(async () => { throw new Error("当前默认素材组不可删除。"); });
    const { fetchGroups, fetchGroupAssets } = groupStubs([groupItem()]);

    render(<AssetLibraryPanel targets={[]} onClose={() => undefined} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={vi.fn()} deleteGroup={deleteGroup} />);

    fireEvent.click(await screen.findByLabelText("删除当前素材组"));
    await screen.findByText("当前默认素材组不可删除。");
    expect(screen.getByLabelText("选择素材组")).toHaveValue("group-a");
});

it("imports an upstream-only asset before adding it to the canvas", async () => {
    const added: { nodeId: string; items: GraphMediaItem[] }[] = [];
    const addToCollection = vi.fn((nodeId: string, items: GraphMediaItem[]) => { added.push({ nodeId, items }); });
    const imported: AssetRef = {
        id: "lib-imported", kind: "library", status: "active", media_type: "image",
        mime_type: "image/jpeg", size_bytes: 64,
        content_url: "/api/v1/assets/lib-imported/content", upstream_asset_id: "asset-up2",
    };
    const importGroupAsset = vi.fn(async (groupId: string, assetId: string) => {
        expect(groupId).toBe("group-a");
        expect(assetId).toBe("asset-up2");
        return { ...imported };
    });
    const { fetchGroups, fetchGroupAssets } = groupStubs([
        groupItem({ asset_id: "asset-up2", name: "远景图", local: null }),
    ]);

    render(<AssetLibraryPanel targets={targets} onClose={() => undefined} fetchGroups={fetchGroups} fetchGroupAssets={fetchGroupAssets} fetchAsset={vi.fn()} importGroupAsset={importGroupAsset} addToCollection={addToCollection} />);

    fireEvent.click(await screen.findByLabelText("添加 远景图 到素材节点"));

    await waitFor(() => expect(importGroupAsset).toHaveBeenCalledWith("group-a", "asset-up2"));
    expect(added).toHaveLength(1);
    expect(added[0].items).toEqual([{
        id: expect.any(String) as string,
        assetId: "lib-imported",
        displayName: "远景图",
        mimeType: "image/jpeg",
        bytes: 64,
        kind: "library",
    }]);
});
