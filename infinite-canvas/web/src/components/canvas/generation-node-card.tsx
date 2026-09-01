import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { CircleCheck, CircleX, Download, LoaderCircle, Trash2 } from "lucide-react";

import type { CanvasNodeData } from "@/types/canvas";

function ResultPreviewDialog({ node, onClose }: { node: CanvasNodeData; onClose: () => void }) {
    useEffect(() => {
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") onClose();
        };
        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, [onClose]);

    // Portal + no-zoom 标记：与素材预览对话框同一契约，画布不会把对话框内的
    // pointerdown 当成背景平移捕获（上游 cb1f3c9）。
    return createPortal(
        <div
            data-canvas-no-zoom
            className="fixed inset-0 z-[90] flex items-center justify-center bg-black/70 p-6"
            onPointerDown={(event) => {
                event.stopPropagation();
                if (event.target === event.currentTarget) onClose();
            }}
        >
            <section role="dialog" aria-modal="true" aria-label="生成结果大图预览" className="flex h-full w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-[#d9e0ea] bg-[#f8fafc] text-[#172033] shadow-2xl">
                <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[#e2e8f0] px-4 py-2.5">
                    <h2 className="min-w-0 truncate text-sm font-semibold">{node.title}</h2>
                    <button type="button" aria-label="关闭预览" onClick={onClose} className="shrink-0 rounded-lg border border-[#c3ccd9] px-2.5 py-1 text-xs text-[#687386] hover:bg-[#eef2f7]">关闭</button>
                </header>
                <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-black/40 p-3">
                    {node.type === "video" ? (
                        <video aria-label="生成视频结果预览" className="max-h-full max-w-full rounded" controls src={node.metadata?.content}>
                            当前浏览器无法播放该视频。
                        </video>
                    ) : (
                        <img src={node.metadata?.content} alt="生成结果大图" className="max-h-full max-w-full rounded" />
                    )}
                </div>
                <footer className="shrink-0 border-t border-[#e2e8f0] px-4 py-2.5 text-xs text-[#687386]">
                    {node.metadata?.requestId ? <p>请求编号：{node.metadata.requestId}</p> : null}
                    <p>双击画面可放大查看；点击画面外或按 Esc 关闭。</p>
                </footer>
            </section>
        </div>,
        document.body,
    );
}

export function GenerationNodeCard({ node, onRetry, onDelete }: { node: CanvasNodeData; onRetry?: (token: string) => void; onDelete?: () => void }) {
    const status = node.metadata?.status || "idle";
    const result = node.metadata?.sourceJobId;
    const [zoomed, setZoomed] = useState(false);
    return (
        <>
        <article data-testid={result ? `result-node-${result}` : `generation-node-${node.id}`} className="flex h-full flex-col overflow-hidden rounded-xl border border-[#d9e0ea] bg-[#f8fafc] text-xs text-[#172033] shadow-xl">
            <header className="flex shrink-0 items-center gap-2 border-b border-[#e2e8f0] px-3 py-2">
                <span className="text-[#235fd6]">{status === "success" ? <CircleCheck className="size-4" /> : status === "error" ? <CircleX className="size-4 text-[#ff8c82]" /> : <LoaderCircle className="size-4 animate-spin" />}</span>
                <strong>{node.title}</strong>
            </header>
            {status === "success" && node.type === "image" && node.metadata?.content ? (
                <div
                    title="双击放大查看"
                    className="media-surface m-3 flex min-h-0 flex-1 cursor-zoom-in items-center justify-center overflow-hidden rounded-lg"
                    onDoubleClick={() => setZoomed(true)}
                >
                    <img src={node.metadata.content} alt="生成结果" className="block max-h-full max-w-full object-contain" />
                </div>
            ) : status === "success" && node.type === "video" && node.metadata?.content ? (
                <div
                    title="双击放大查看"
                    className="media-surface m-3 flex min-h-0 flex-1 cursor-zoom-in items-center justify-center overflow-hidden rounded-lg bg-black"
                    onDoubleClick={() => setZoomed(true)}
                >
                    <video aria-label="生成视频结果" className="block max-h-full max-w-full object-contain" controls preload="metadata" src={node.metadata.content}>
                        当前浏览器无法播放该视频。
                    </video>
                </div>
            ) : (
                <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
                    <p className="whitespace-pre-wrap text-[#465267]">{status === "error" ? node.metadata?.errorDetails : node.metadata?.prompt || node.metadata?.content}</p>
                    {node.metadata?.requestId ? <p className="text-[#8b95a7]">请求编号：{node.metadata.requestId}</p> : null}
                    {status === "error" && node.metadata?.idempotencyKey && onRetry ? (
                        <button className="rounded border border-[#c3ccd9] px-2 py-1 text-[#2f6bdd]" onClick={() => onRetry(node.metadata!.idempotencyKey!)}>
                            重试
                        </button>
                    ) : null}
                </div>
            )}
            {status === "success" && node.metadata?.content ? (
                <footer className="flex shrink-0 gap-2 border-t border-[#e2e8f0] p-2">
                    <a href={node.metadata.content} download={`生成结果-${node.metadata.sourceResultIndex ?? 0}.${node.type === "video" ? "mp4" : "png"}`} className="flex items-center gap-1 rounded border border-[#c3ccd9] px-2 py-1 text-[#2f6bdd]">
                        <Download className="size-3" />
                        下载
                    </a>
                    {onDelete ? (
                        <button type="button" onClick={onDelete} className="flex items-center gap-1 rounded border border-[#6b3535] px-2 py-1 text-[#ff9b92]">
                            <Trash2 className="size-3" />
                            删除
                        </button>
                    ) : null}
                </footer>
            ) : null}
        </article>
        {zoomed && status === "success" && node.metadata?.content ? <ResultPreviewDialog node={node} onClose={() => setZoomed(false)} /> : null}
        </>
    );
}
