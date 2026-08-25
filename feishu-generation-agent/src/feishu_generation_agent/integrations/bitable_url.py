from collections.abc import Mapping
from typing import Any
from urllib.parse import (
    parse_qs,
    parse_qsl,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.document import SourceType
from feishu_generation_agent.integrations.feishu_source import parse_feishu_url


def parse_bitable_url(url: str, table_id: str, view_id: str) -> BitableLocation:
    parsed = urlsplit(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parts and parts[0] == "base":
        # 独立多维表格 Base（非 wiki 内嵌）：app_token 直接来自 URL，
        # table_id / view_id 由配置提供。
        if len(parts) != 2 or not parts[1].strip():
            raise ValueError("Base 链接缺少 app_token")
        return BitableLocation(
            wiki_token="",
            app_token=parts[1].strip(),
            table_id=table_id,
            view_id=view_id,
            source_url=url,
        )

    source_type, wiki_token = parse_feishu_url(url)
    if source_type is not SourceType.WIKI:
        raise ValueError("多维表格链接必须是 wiki 或 base 链接")

    query = parse_qs(parsed.query, keep_blank_values=True)
    _require_matching_query_value(query, "table", table_id)
    _require_matching_query_value(query, "view", view_id)

    return BitableLocation(
        wiki_token=wiki_token,
        table_id=table_id,
        view_id=view_id,
        source_url=url,
    )


def with_bitable_view(location: BitableLocation, view_id: str) -> BitableLocation:
    if not isinstance(view_id, str) or not view_id.strip():
        raise ValueError("多维表格 view_id 不能为空")
    parsed = urlsplit(location.source_url)
    query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if name not in {"table", "view"}
    ]
    query.extend((("table", location.table_id), ("view", view_id.strip())))
    source_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )
    return location.model_copy(
        update={"view_id": view_id.strip(), "source_url": source_url}
    )


def parse_requirement_source(value: Any) -> str:
    sources = {
        _normalize_document_url(candidate)
        for candidate in _iter_source_candidates(value)
    }
    if len(sources) != 1:
        raise ValueError("需求来源必须恰好一个飞书文档链接")
    return sources.pop()


def _require_matching_query_value(
    query: dict[str, list[str]], name: str, expected: str
) -> None:
    if not expected or query.get(name) != [expected]:
        raise ValueError(f"链接中的 {name} 必须与配置一致")


def _iter_source_candidates(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        if "link" in value:
            yield from _iter_source_candidates(value["link"])
    elif isinstance(value, list):
        for item in value:
            yield from _iter_source_candidates(item)


def _normalize_document_url(url: str) -> str:
    source_type, token = parse_feishu_url(url)
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    authority = hostname if port in {None, 443} else f"{hostname}:{port}"
    return urlunsplit(
        (parsed.scheme, authority, f"/{source_type.value}/{token}", "", "")
    )
