"""发送 / 更新飞书消息。M1 提供 reply_text;M5 追加 send_placeholder_card / patch_card。"""
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


def reply_text(client: lark.Client, message_id: str, text: str) -> None:
    """回复一条文本消息到指定 message_id 的会话上下文。"""
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .msg_type("text")
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        raise RuntimeError(
            f"lark reply failed: code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
        )


def _card(text: str, title: str = "RAG Agent") -> dict:
    """构造一张最简卡片,只有一段可动态更新的文本。"""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
        ],
    }


def send_placeholder_card(
    client: lark.Client,
    chat_id: str,
    text: str = "收到,正在查 KB...",
) -> str:
    """向指定 chat_id 发送占位卡片,返回该卡片的 message_id(用于后续 patch)。"""
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(_card(text), ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        raise RuntimeError(
            f"send placeholder card failed: code={resp.code} msg={resp.msg} "
            f"log_id={resp.get_log_id()}"
        )
    return resp.data.message_id


def patch_card(client: lark.Client, message_id: str, text: str) -> None:
    """更新已发送的卡片内容为新文本。"""
    req = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(json.dumps(_card(text), ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.patch(req)
    if not resp.success():
        raise RuntimeError(
            f"patch card failed: code={resp.code} msg={resp.msg} "
            f"log_id={resp.get_log_id()}"
        )
