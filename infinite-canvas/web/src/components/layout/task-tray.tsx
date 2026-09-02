import { useState } from "react";
import { Ban, CircleCheck, CircleX, LoaderCircle, X } from "lucide-react";

import { CancelJobDialog } from "@/components/cancel-job-dialog";
import { cancelGenerationTask, dismissGenerationTask, useGenerationTasks } from "@/features/generation/use-generation-job";


const statusLabel = { idle: "等待", submitting: "提交中", queued: "排队中", running: "生成中", succeeded: "已完成", failed: "失败" } as const;

export function TaskTray() {
    const tasks = useGenerationTasks((state) => state.tasks);
    const [cancelJobId, setCancelJobId] = useState<string | null>(null);
    return (
        <aside data-testid="task-tray" aria-label="任务托盘" className="fixed inset-x-0 bottom-0 z-40 flex h-[var(--task-tray-height)] items-center gap-3 overflow-x-auto border-t border-border bg-card/98 px-4 text-sm text-muted-foreground backdrop-blur md:left-56">
            <span className="shrink-0 font-medium text-foreground">运行任务</span>
            {tasks.length === 0 ? <span>暂无运行任务</span> : tasks.map((task) => {
                const terminal = task.status === "succeeded" || task.status === "failed";
                const Icon = task.status === "succeeded" ? CircleCheck : task.status === "failed" ? CircleX : LoaderCircle;
                return <div key={task.jobId} className="flex shrink-0 items-center gap-2 rounded-md border border-border bg-muted px-2.5 py-1 text-xs text-foreground" title={task.title}><Icon className={`size-3.5 ${terminal ? "" : "animate-spin"}`} /><span className="max-w-32 truncate">{task.title || task.jobId}</span><span className="text-primary">{statusLabel[task.status]}</span>{task.status === "queued" ? <button aria-label={`取消任务 ${task.jobId}`} title="取消排队" onClick={() => void cancelGenerationTask(task.jobId).catch(() => undefined)}><Ban className="size-3" /></button> : null}{task.status === "running" ? <button aria-label={`取消任务 ${task.jobId}`} title="取消生成（已计费）" onClick={() => setCancelJobId(task.jobId)}><Ban className="size-3" /></button> : null}{terminal ? <button aria-label={`移除任务 ${task.jobId}`} onClick={() => dismissGenerationTask(task.jobId)}><X className="size-3" /></button> : null}</div>;
            })}
            <CancelJobDialog open={cancelJobId !== null} onClose={() => setCancelJobId(null)} onConfirm={() => { const id = cancelJobId; setCancelJobId(null); if (id) void cancelGenerationTask(id).catch(() => undefined); }} />
        </aside>
    );
}
