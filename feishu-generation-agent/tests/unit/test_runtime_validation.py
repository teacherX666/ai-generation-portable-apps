import logging

from feishu_generation_agent.config import Settings
from feishu_generation_agent.graph.runtime import GraphRuntime


def test_rebuild_validation_issues_logs_underlying_error_and_fails_closed(
    caplog,
):
    runtime = GraphRuntime.__new__(GraphRuntime)
    runtime.settings = Settings(_env_file=None)

    with caplog.at_level(
        logging.ERROR,
        logger="feishu_generation_agent.graph.runtime",
    ):
        result = runtime._rebuild_validation_issues(
            state={},
            plan=None,
            records=[],
        )

    assert result == ["审批校验状态无效，请重新读取后再审批"]
    assert any(
        "重建审批校验问题失败" in record.message
        for record in caplog.records
    )
