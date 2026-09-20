"""轮询进度文案：必须区分本地与云端，不能一律说「本地模型」。"""

from feishu_generation_agent.graph.nodes import _provider_progress_message


def test_cloud_provider_running_is_not_called_local() -> None:
    message = _provider_progress_message("volcengine_portrait", "running")
    assert message is not None
    assert "本地模型" not in message
    assert "volcengine_portrait" in message


def test_cloud_provider_queueing_is_not_called_local() -> None:
    message = _provider_progress_message("seedance2.5", "queued")
    assert message is not None
    assert "本地模型" not in message


def test_local_provider_running_mentions_local_model() -> None:
    message = _provider_progress_message("aiport", "running")
    assert message is not None
    assert "本地模型" in message


def test_local_provider_queueing_mentions_gpu() -> None:
    message = _provider_progress_message("aiport", "submitted")
    assert message is not None
    assert "本地模型" in message
    assert "GPU" in message


def test_unknown_status_emits_nothing() -> None:
    assert _provider_progress_message("seedance2.5", "succeeded") is None


def test_missing_provider_still_reads_sensibly() -> None:
    message = _provider_progress_message(None, "running")
    assert message is not None
    assert "本地模型" not in message