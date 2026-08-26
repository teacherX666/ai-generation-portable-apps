"""飞书消息事件路由:过滤 + 抽取 query。"""
import json
import logging
from dataclasses import dataclass, field

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

logger = logging.getLogger(__name__)


@dataclass
class ExtractedQuery:
    """从飞书事件中抽取出的查询。"""
    message_id: str
    chat_id: str = ""
    text: str = ""
    image_keys: list[str] = field(default_factory=list)


def extract_query(
    event: P2ImMessageReceiveV1,
    bot_open_id: str,
) -> ExtractedQuery | None:
    """从事件中抽取 query;不属于机器人处理范围则返回 None。

    处理规则:
    - 私聊(p2p):全部接收
    - 群聊(group):必须 @ 了机器人才接收
    - 消息类型 text / image / post 分别解析
    """
    msg = event.event.message
    message_id = msg.message_id
    chat_id = msg.chat_id
    msg_type = msg.message_type
    chat_type = msg.chat_type
    logger.debug("raw msg: type=%s content=%r", msg_type, msg.content)

    # 群聊必须 @ 机器人
    if chat_type == "group":
        mentions = msg.mentions or []
        if not any(m.id.open_id == bot_open_id for m in mentions):
            logger.debug("skip group msg without mention: %s", message_id)
            return None

    try:
        content = json.loads(msg.content) if msg.content else {}
    except json.JSONDecodeError:
        logger.warning("cannot parse msg content: %s", msg.content)
        return None

    text = ""
    image_keys: list[str] = []

    if msg_type == "text":
        text = content.get("text", "")
        # 去掉群@标记(mention.key,形如 @_user_1)
        for m in msg.mentions or []:
            if m.key:
                text = text.replace(m.key, "")
    elif msg_type == "image":
        image_keys.append(content.get("image_key", ""))
    elif msg_type == "post":
        # post 富文本。两种格式:
        # 1. 主动发送时(SDK):{"post": {"zh_cn": {"content": [[...]]}}}
        # 2. 用户端"图+文"合成消息:{"title": "...", "content": [[...]], "content_v2": [...]}
        # 都是 list[list[seg]] 结构,只是嵌套层级不同
        lines = None
        if "post" in content:
            post = content["post"]
            lang_content = post.get("zh_cn") or next(iter(post.values()), {})
            lines = lang_content.get("content", [])
        else:
            lines = content.get("content", [])

        for line in lines or []:
            for seg in line:
                if seg.get("tag") == "text":
                    text += seg.get("text", "")
                elif seg.get("tag") == "img":
                    image_keys.append(seg.get("image_key", ""))
    else:
        logger.info("unsupported msg_type: %s", msg_type)
        return None

    # 清理空图片 key、trim 文本
    image_keys = [k for k in image_keys if k]
    text = text.strip()

    if not text and not image_keys:
        return None

    return ExtractedQuery(message_id=message_id, chat_id=chat_id, text=text, image_keys=image_keys)
