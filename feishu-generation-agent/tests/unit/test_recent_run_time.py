"""历史记录的时间：裸 UTC 串要转成带时区的 ISO，否则前端显示早 8 小时。"""

from feishu_generation_agent.web.app import _iso_utc_string


def test_naive_utc_string_gets_a_timezone() -> None:
    """用户报 2026-09-18：「时间是乱的」—— 多维表格返回的是裸 UTC 串。"""
    assert _iso_utc_string("2026-09-18 06:07:37") == (
        "2026-09-18T06:07:37+00:00"
    )


def test_string_with_microseconds_keeps_precision() -> None:
    assert _iso_utc_string("2026-09-18 06:07:37.123456").startswith(
        "2026-09-18T06:07:37.123456"
    )


def test_already_aware_string_is_kept() -> None:
    assert _iso_utc_string("2026-09-18T06:07:37+00:00") == (
        "2026-09-18T06:07:37+00:00"
    )


def test_empty_and_garbage_are_tolerated() -> None:
    assert _iso_utc_string("") == ""
    assert _iso_utc_string("不是时间") == "不是时间"
