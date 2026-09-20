"""任务记录外面的「已成片 N 条」靠 Repository.count_artifacts_by_run 取数。"""

from feishu_generation_agent.domain import Artifact
from feishu_generation_agent.storage.repository import Repository


def _artifact(artifact_id: str, run_id: str) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        task_id="task-1",
        kind="video",
        local_path=f"/tmp/{artifact_id}.mp4",
        mime_type="video/mp4",
        size=1024,
        sha256=f"sha-{artifact_id}",
        status="ready",
    )


async def test_count_artifacts_by_run_aggregates_per_run(tmp_path) -> None:
    repository = await Repository.open(tmp_path / "repo.sqlite3")
    try:
        await repository.save_artifact("run-a", _artifact("a1", "run-a"))
        await repository.save_artifact("run-a", _artifact("a2", "run-a"))
        await repository.save_artifact("run-b", _artifact("b1", "run-b"))

        counts = await repository.count_artifacts_by_run(
            ["run-a", "run-b", "run-c"]
        )
        empty = await repository.count_artifacts_by_run([])
    finally:
        await repository.close()

    # 没有成片的 run 不出现在结果里（调用方按 0 处理）。
    assert counts == {"run-a": 2, "run-b": 1}
    assert empty == {}
