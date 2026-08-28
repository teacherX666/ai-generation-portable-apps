import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import ActivityAssetsPage from "@/pages/assets/activity";
import * as assetsApi from "@/api/assets";


afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

const activeItem = {
    asset_id: "asset-abc123", name: "人像1", status: "active" as const,
    media_type: "image" as const, error_message: null, local: null,
};

it("renders groups with proxied thumbnails, renames group and asset, and uploads to the selected group", async () => {
    vi.spyOn(assetsApi, "listLibraryGroups").mockResolvedValue([
        { group_id: "g1", name: "人像组", created_at: "2026-01-01" },
    ]);
    vi.spyOn(assetsApi, "fetchGroupAssets").mockResolvedValue([activeItem]);
    const renameGroup = vi.spyOn(assetsApi, "renameLibraryGroup").mockResolvedValue();
    const renameAsset = vi.spyOn(assetsApi, "renameGroupAsset").mockResolvedValue();
    const upload = vi.spyOn(assetsApi, "uploadLibraryAsset").mockResolvedValue({} as never);

    render(<MemoryRouter><ActivityAssetsPage /></MemoryRouter>);

    await screen.findByText("人像组");
    const thumb = await screen.findByTestId("library-thumb-asset-abc123");
    expect(thumb.getAttribute("src")).toBe("/api/v1/library-groups/g1/assets/asset-abc123/content");
    await screen.findByText("人像1");

    // group rename
    fireEvent.click(screen.getByLabelText("重命名素材组"));
    fireEvent.change(screen.getByLabelText("素材组新名字"), { target: { value: "新组名" } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(renameGroup).toHaveBeenCalledWith("g1", "新组名"));

    // asset rename
    fireEvent.click(screen.getByLabelText("重命名 人像1"));
    fireEvent.change(screen.getByLabelText("素材新名字"), { target: { value: "改名人像" } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(renameAsset).toHaveBeenCalledWith("g1", "asset-abc123", "改名人像"));

    // upload to the selected group
    const file = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "portrait.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("选择素材文件"), { target: { files: [file] } });
    await waitFor(() => expect(upload).toHaveBeenCalledWith(file, "g1", expect.any(Function)));
});

it("shows the backend message when deleting the protected default group", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.spyOn(assetsApi, "listLibraryGroups").mockResolvedValue([
        { group_id: "g1", name: "默认组", created_at: "2026-01-01" },
    ]);
    vi.spyOn(assetsApi, "fetchGroupAssets").mockResolvedValue([activeItem]);
    const deleteGroup = vi.spyOn(assetsApi, "deleteLibraryGroup")
        .mockRejectedValue(new Error("当前默认素材组不可删除。"));

    render(<MemoryRouter><ActivityAssetsPage /></MemoryRouter>);

    fireEvent.click(await screen.findByLabelText("删除素材组"));
    await screen.findByText("当前默认素材组不可删除。");
    expect(deleteGroup).toHaveBeenCalledWith("g1");
    // 组还在列表里
    expect(screen.getByLabelText("选择素材组")).toHaveValue("g1");
});

it("creates a new group and shows failed asset reasons", async () => {
    vi.spyOn(assetsApi, "listLibraryGroups").mockResolvedValue([
        { group_id: "g1", name: "人像组", created_at: "2026-01-01" },
    ]);
    vi.spyOn(assetsApi, "fetchGroupAssets").mockResolvedValue([
        { ...activeItem, status: "failed" as const, error_message: "宽高比超出范围" },
    ]);
    const createGroup = vi.spyOn(assetsApi, "createLibraryGroup")
        .mockResolvedValue({ group_id: "g2", name: "新组" });

    render(<MemoryRouter><ActivityAssetsPage /></MemoryRouter>);

    await screen.findByText("宽高比超出范围");
    fireEvent.click(screen.getByLabelText("新建素材组"));
    fireEvent.change(screen.getByLabelText("新素材组名字"), { target: { value: "新组" } });
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => expect(createGroup).toHaveBeenCalledWith("新组"));
    expect(screen.getByLabelText("选择素材组")).toHaveValue("g2");
});

it("deletes an asset after confirmation", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.spyOn(assetsApi, "listLibraryGroups").mockResolvedValue([
        { group_id: "g1", name: "人像组", created_at: "2026-01-01" },
    ]);
    const fetchAssets = vi.spyOn(assetsApi, "fetchGroupAssets")
        .mockResolvedValueOnce([activeItem])
        .mockResolvedValueOnce([]);
    const deleteAsset = vi.spyOn(assetsApi, "deleteGroupAsset").mockResolvedValue();

    render(<MemoryRouter><ActivityAssetsPage /></MemoryRouter>);

    fireEvent.click(await screen.findByLabelText("删除 人像1"));
    await waitFor(() => expect(deleteAsset).toHaveBeenCalledWith("g1", "asset-abc123"));
    await screen.findByText("这个组还没有素材，选择图片上传即可。");
    expect(fetchAssets).toHaveBeenCalledTimes(2);
});
