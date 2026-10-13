"""上游出站 HTTP：绕开桌面（注册表）系统代理。

2026-09-28 实测：这台机器的 Windows 交互式代理是
``ProxyEnable=1 / ProxyServer=socks=127.0.0.1:1080``（Jamjams.exe）。
stdlib ``urllib`` 会通过 ``getproxies()`` 读到它，但**不支持 SOCKS** ——
于是它把这个 SOCKS 口当 HTTP/HTTPS 代理，对着 SOCKS 服务端讲 TLS，
连接在握手中途被掐断，表现为：

    URLError: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] ...>

后果：nano-banana 的图生图（参考图）每条重试 6 次、耗约 670 秒后失败；
同一批 t8star 请求走代理光 TLS 就要 21 秒，直连 0.6 秒。

本项目所有上游（chiyun / t8star / ark / 飞书 / uguu …）从这台机器都能直连，
所以**默认不经任何代理**。确有供应商必须经代理时，设置环境变量
``AI_PORT_UPSTREAM_PROXY=http://127.0.0.1:7890`` 即可全局 opt back in
（nano-banana / seedance 另支持各自的 ``*_UPSTREAM_PROXY``）。

用法::

    from shared.net import UPSTREAM
    with UPSTREAM.open(req, timeout=timeout) as resp:
        ...

注意：只影响**本进程发起的出站调用**。带 ``context=`` 的 ``urlopen``（各
应用对 Portal 的本地探活）刻意不动 —— 目标是 127.0.0.1，注册表
ProxyOverride 已包含 ``127.*``，本来就绕过代理。
"""

from __future__ import annotations

import os
import urllib.request

ENV_VAR = "AI_PORT_UPSTREAM_PROXY"


def upstream_opener(env_var: str = ENV_VAR) -> urllib.request.OpenerDirector:
    """An opener that ignores the OS proxy unless ``env_var`` is set."""
    override = os.environ.get(env_var, "").strip()
    if override:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": override, "https": override})
        )
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


UPSTREAM = upstream_opener()
