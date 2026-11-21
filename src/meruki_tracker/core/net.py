"""发 HTTP 请求：本机地址直连，外部地址照常走代理。

**为什么本机地址必须绕开代理**

``urllib.request.urlopen`` 默认尊重系统代理（macOS 上取自「网络」设置里的
代理配置，其它平台取自 ``HTTP_PROXY`` 等环境变量）。macOS 的系统代理排除
列表是用户自己填的，默认只有 ``*.local`` 与 ``169.254/16``——**不含
``127.0.0.1``**，``urllib.request.proxy_bypass("127.0.0.1")`` 在开了代理的
机器上返回 ``False``。

后果不是慢，是错得莫名其妙：OneBot 之类跑在同一台机器上的服务，请求被送去
代理，代理再连 ``127.0.0.1:<端口>``——那是**代理进程自己的回环**，根本没有
那个服务，于是拿到 ``503`` 而不是「连不上」。真机踩过：一个断言「连不上」的
测试在开了系统代理的机器上收到 ``HTTP 503``，一开始还以为是测试写错了。

所以：**回环地址一律直连**（``ProxyHandler({})``），其余地址保持默认行为——
用户在外网确实需要代理，把代理功能一并关掉是另一种错。
"""

from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request
from typing import Any

#: 不走代理的 opener：``ProxyHandler({})`` 的意思是"什么代理都不用"。
#: 模块级缓存，避免每次请求都新建。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 无端口时也算回环的域名。
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def is_loopback_host(host: str) -> bool:
    """``host`` 是否指向本机（含 ``localhost`` 与 ``127.0.0.0/8``、``::1``）。"""
    name = (host or "").strip().strip("[]").lower()
    if not name:
        return False
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        # 主机名不是 IP：只认上面那几个明写本机的名字。
        # 不去做 DNS 解析——解析要联网，发请求前先发一次解析得不偿失。
        return False


def url_for(req: urllib.request.Request) -> str:
    return req.full_url


def bypasses_proxy(url: str) -> bool:
    """这个 URL 是否该直连（本机地址）。"""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    return is_loopback_host(host)


def open_url(req: urllib.request.Request, *, timeout: float) -> Any:
    """``urlopen`` 的替代：本机地址直连，其余走系统/环境代理。

    返回的对象与 ``urlopen`` 一样可当上下文管理器用，因此调用点的
    ``with urlopen(req, timeout=...) as resp:`` 只要换个名字就行。
    """
    if bypasses_proxy(req.full_url):
        # OpenerDirector 的方法叫 open()，没有 urlopen()——两者返回同一个
        # HTTPResponse，调用点的 ``with ... as resp`` 不用改。
        return _DIRECT.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
