"""lark-oapi 客户端封装:构造 Client + WsClient(长连接)。"""
import json
from typing import Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.core.const import UTF_8
from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.http import Transport
from lark_oapi.core.model import BaseRequest, RequestOption
from lark_oapi.core.token.auth import verify as _verify_auth

from rag_agent.config import Settings


def build_api_client(settings: Settings) -> lark.Client:
    """构造用于 REST 调用的 lark client(发消息、拉文档、下载图片等)。"""
    return (
        lark.Client.builder()
        .app_id(settings.lark_app_id)
        .app_secret(settings.lark_app_secret)
        .log_level(lark.LogLevel.INFO)
        .build()
    )


def build_ws_client(
    settings: Settings,
    on_message: Callable[[P2ImMessageReceiveV1], None],
) -> lark.ws.Client:
    """构造长连接 WsClient,把 im.message.receive_v1 事件路由到 on_message。"""
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    return lark.ws.Client(
        settings.lark_app_id,
        settings.lark_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )


import base64

from lark_oapi.api.im.v1 import GetMessageResourceRequest


def download_image_as_data_url(
    client: lark.Client,
    message_id: str,
    image_key: str,
) -> str:
    """从飞书拉取消息里的图片,返回 data URL(供 Claude vision 使用)。"""
    req = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(image_key)
        .type("image")
        .build()
    )
    resp = client.im.v1.message_resource.get(req)
    if not resp.success():
        raise RuntimeError(
            f"download image failed: code={resp.code} msg={resp.msg} "
            f"log_id={resp.get_log_id()} image_key={image_key}"
        )
    # resp.file 是一个 stream / bytes 对象
    raw: bytes = resp.file.read() if hasattr(resp.file, "read") else bytes(resp.file)
    # 简单粗暴用 PNG(飞书图片一般是 png/jpeg,vision 都接受;若失败可后续加 mimetype 探测)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def fetch_bot_open_id(client: lark.Client, app_id: str) -> str:
    """获取本机器人自身的 open_id(群 @ 判定用)。

    lark-oapi 1.7.x 没有生成 `bot.v3` 资源,但 `/open-apis/bot/v3/info` 接口
    仍然是拿 bot open_id 的最直接方式,所以这里走 Transport 原生调用。
    如果拿不到就抛异常,由上层退回环境变量 LARK_BOT_OPEN_ID。
    """
    req = BaseRequest()
    req.http_method = HttpMethod.GET
    req.uri = "/open-apis/bot/v3/info"
    req.token_types = {AccessTokenType.TENANT}

    option = RequestOption()
    # Transport.execute 走的是原始通道,不会经过 Chain 里的 token 注入,
    # 所以显式补一下 tenant token,否则会带 `Bearer None`。
    _verify_auth(client._config, req, option)
    resp = Transport.execute(client._config, req, option)

    if resp is None or resp.content is None:
        raise RuntimeError("fetch_bot_open_id: empty response")

    try:
        body = json.loads(resp.content.decode(UTF_8))
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError(f"fetch_bot_open_id: bad json body: {e}") from e

    code = body.get("code")
    if code and code != 0:
        raise RuntimeError(
            f"fetch_bot_open_id: api error code={code} msg={body.get('msg')}"
        )

    # /bot/v3/info 的返回体:{"code":0,"msg":"","bot":{"open_id":"ou_...", ...}}
    # 有些环境会包一层 data,兼容两种形状。
    bot = body.get("bot")
    if bot is None:
        data = body.get("data") or {}
        bot = data.get("bot") or data
    if not isinstance(bot, dict):
        raise RuntimeError(f"fetch_bot_open_id: no bot payload: {body}")

    open_id = bot.get("open_id") or bot.get("openid") or ""
    if not open_id:
        raise RuntimeError(f"fetch_bot_open_id: no open_id in response: {body}")

    _ = app_id  # 保留参数以便未来切到 application.get 时使用
    return open_id
