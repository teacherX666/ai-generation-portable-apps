import { createPortal } from "react-dom";

/** 运行中任务强制取消确认：提示已开始计费，取消后结果丢失但费用可能仍产生。 */
export function CancelJobDialog({ open, onClose, onConfirm }: { open: boolean; onClose: () => void; onConfirm: () => void }) {
    if (!open) return null;
    return createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
            <div role="alertdialog" aria-label="确认取消任务" className="w-full max-w-sm rounded-xl border border-border bg-card p-5 shadow-2xl" onClick={(event) => event.stopPropagation()}>
                <h3 className="text-sm font-semibold text-foreground">取消正在生成的任务？</h3>
                <p className="mt-2 text-xs leading-5 text-muted-foreground">
                    任务已开始计费。取消后本次生成结果将丢失，已产生的费用可能仍然需要支付。
                </p>
                <p className="mt-2 text-[11px] leading-5 text-muted-foreground">取消后即可重新发起新的生成任务。</p>
                <div className="mt-4 flex justify-end gap-2">
                    <button type="button" onClick={onClose} className="rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground hover:border-[#f59e0b] hover:text-foreground">继续生成</button>
                    <button type="button" onClick={onConfirm} className="rounded-lg border border-[#f59e0b] bg-[#fffbeb] px-3 py-1.5 text-xs font-medium text-[#b45309] hover:bg-[#fef3c7]">确定取消</button>
                </div>
            </div>
        </div>,
        document.body,
    );
}
