import { describe, expect, it } from "vitest";

import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import {
    DEFAULT_MEDIA_COLLECTION_TITLES,
    isBareDefaultMediaCollectionTitle,
    mediaCollectionTitleNumber,
    nextMediaCollectionTitle,
} from "@/features/graph/media-collection";
import { normalizeCanvasProject, type CanvasNodeInput, type CanvasProjectInput } from "@/features/graph/normalize-project";
import { CanvasNodeType } from "@/types/canvas";

function mediaNode(id: string, title: string, mediaType: "image" | "video" | "audio", type = CanvasNodeType.Image) {
    return {
        id,
        type,
        title,
        position: { x: 0, y: 0 },
        width: 360,
        height: 300,
        metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "media-collection", mediaType, outputPortId: "media", items: [] } },
    };
}

function project(nodes: CanvasNodeInput[], connections: CanvasProjectInput["connections"] = []): CanvasProjectInput {
    return {
        id: "project",
        title: "Project",
        createdAt: "2026-08-17T00:00:00.000Z",
        updatedAt: "2026-08-17T00:00:00.000Z",
        nodes,
        connections,
        chatSessions: [],
        activeChatId: null,
        backgroundMode: "lines",
        showImageInfo: false,
        viewport: { x: 0, y: 0, k: 1 },
    };
}

describe("media collection node titles", () => {
    it("parses numbered default titles per media type", () => {
        expect(mediaCollectionTitleNumber("参考图片2", "image")).toBe(2);
        expect(mediaCollectionTitleNumber("参考视频1", "video")).toBe(1);
        expect(mediaCollectionTitleNumber("参考图片", "image")).toBeNull();
        expect(mediaCollectionTitleNumber("参考图片x", "image")).toBeNull();
        expect(mediaCollectionTitleNumber("参考视频2", "image")).toBeNull();
        expect(mediaCollectionTitleNumber("女主脸", "image")).toBeNull();
        expect(mediaCollectionTitleNumber("参考图片0", "image")).toBeNull();
        expect(mediaCollectionTitleNumber("参考图片99999999999999999999", "image")).toBeNull();
    });

    it("detects bare default titles", () => {
        expect(isBareDefaultMediaCollectionTitle("参考图片", "image")).toBe(true);
        expect(isBareDefaultMediaCollectionTitle("参考图片2", "image")).toBe(false);
        expect(isBareDefaultMediaCollectionTitle("女主脸", "image")).toBe(false);
        expect(DEFAULT_MEDIA_COLLECTION_TITLES.audio).toBe("参考音频");
    });

    it("continues the sequence from the largest existing number, ignoring custom titles", () => {
        expect(nextMediaCollectionTitle([], "image")).toBe("参考图片1");
        expect(nextMediaCollectionTitle(["女主脸", "参考图片2"], "image")).toBe("参考图片3");
        expect(nextMediaCollectionTitle(["参考图片10", "参考视频3"], "image")).toBe("参考图片11");
    });
});

describe("legacy bare title numbering on load", () => {
    it("numbers bare titles after the existing sequence, keeps custom and numbered titles, and is idempotent", () => {
        const input = project([
            mediaNode("a", "参考图片", "image"),
            mediaNode("b", "参考图片", "image"),
            mediaNode("c", "女主脸", "image"),
            mediaNode("d", "参考图片2", "image"),
            mediaNode("e", "参考视频", "video", CanvasNodeType.Video),
            mediaNode("f", "参考音频", "audio", CanvasNodeType.Audio),
        ]);
        const once = normalizeCanvasProject(input);
        expect(once.nodes.map((node) => node.title)).toEqual(["参考图片3", "参考图片4", "女主脸", "参考图片2", "参考视频1", "参考音频1"]);
        const twice = normalizeCanvasProject(once);
        expect(twice.nodes.map((node) => node.title)).toEqual(once.nodes.map((node) => node.title));
    });

    it("starts numbering at 1 when no numbered title exists", () => {
        const input = project([mediaNode("a", "参考图片", "image"), mediaNode("b", "参考图片", "image"), mediaNode("c", "参考视频", "video", CanvasNodeType.Video)]);
        expect(normalizeCanvasProject(input).nodes.map((node) => node.title)).toEqual(["参考图片1", "参考图片2", "参考视频1"]);
    });

    it("leaves a project with no bare titles completely untouched", () => {
        const input = project([
            mediaNode("a", "参考图片1", "image"),
            mediaNode("b", "女主脸", "image"),
            mediaNode("c", "参考视频5", "video", CanvasNodeType.Video),
        ]);
        expect(normalizeCanvasProject(input).nodes.map((node) => node.title)).toEqual(["参考图片1", "女主脸", "参考视频5"]);
    });

    it("keeps a foreign default title as custom and numbers the same-type bare title from 1", () => {
        const input = project([
            mediaNode("a", "参考视频", "image"),
            mediaNode("b", "参考视频", "video", CanvasNodeType.Video),
        ]);
        expect(normalizeCanvasProject(input).nodes.map((node) => node.title)).toEqual(["参考视频", "参考视频1"]);
    });

    it("does not crash on non-string titles from corrupted or imported projects", () => {
        const broken = { ...mediaNode("a", "参考图片", "image"), title: 42 as unknown as string };
        const input = project([broken, mediaNode("b", "参考图片", "image")]);
        expect(normalizeCanvasProject(input).nodes.map((node) => node.title)).toEqual([42, "参考图片1"]);
    });
});
