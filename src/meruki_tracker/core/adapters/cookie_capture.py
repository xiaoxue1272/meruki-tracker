"""跨平台 Cookie 捕获。

**能力边界（重要，先读这段）**

不同浏览器把 Cookie 存在不同地方，可获取性差别极大：

===================  ==========================================  ==================
浏览器               存储位置                                     能否自动读取
===================  ==========================================  ==================
Chrome / Edge / Brave 加密 SQLite（key 在系统钥匙串）              **不能**（AES-GCM +
                     macOS: Keychain / Win: DPAPI / Linux: 钥匙串 完整性保护，需人工授权）
Firefox              ``cookies.sqlite``，明文                      **能**（需浏览器关闭）
Chrome（调试端口）    ``--remote-debugging-port`` 的 CDP 接口      **能**（需以此方式启动）
===================  ==========================================  ==================

结论：**不存在一个"拉起普通浏览器就能自动拿到 Cookie"的通用办法。**
本模块因此采用"尽力而为 + 明确回退"的策略：

1. 先探测 Chrome 系浏览器的 CDP 调试端口（如果用户按提示那样启动浏览器，
   就能完全自动拿到 Cookie）；
2. 再尝试从 Firefox 的明文库读取；
3. 都不行就返回 :class:`CaptureFailure`，由 UI 层引导用户手动粘贴。

**绝不猜测、绝不半途返回残缺的 Cookie**：拿不到完整的就明确说拿不到，
并且**必须如实说明是哪一步失败的**——失败原因被含糊成一句
"端口上没有调试接口"会让用户永远修不好问题。

关于 CDP 的 WebSocket 客户端：项目不想为"调一次 CDP"引入 websockets 依赖，
因此这里手写了一个只覆盖本场景（ws://127.0.0.1）的最小客户端。它按
RFC 6455 处理握手校验、掩码、16/64 位长度、分片（continuation + FIN）以及
控制帧（ping/pong/close），并做 CDP 的 ``id`` 关联——浏览器会主动推事件帧，
"收到第一帧就当响应"必然出错。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

#: 需要捕获 Cookie 的域名（Cookie 的 domain 字段以此为后缀即视为目标站点）
TARGET_HOST = "meruki.cn"

#: CDP 默认调试端口
DEFAULT_CDP_PORT = 9222

#: CDP 调用（HTTP + WebSocket）的总超时上限（秒）
DEFAULT_CDP_TIMEOUT = 5.0

#: 单个 WebSocket 消息的上限，防止对端灌爆内存
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024

#: 握手响应头的上限
_MAX_HEADER_BYTES = 64 * 1024

#: 连续控制帧的处理上限（防止对端无限发 ping 导致死循环）
_MAX_CONTROL_FRAMES = 64

if TYPE_CHECKING:
    from collections.abc import Callable

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: WebSocket opcode
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """捕获成功。"""

    cookie: str
    #: 捕获来源：cdp / firefox
    source: str
    #: 供 UI 展示的说明
    note: str = ""


@dataclass(frozen=True, slots=True)
class CaptureFailure:
    """捕获失败。"""

    #: 人类可读的原因
    reason: str
    #: 建议的下一步（可展示给用户）
    hints: tuple[str, ...] = ()


# ------------------------------------------------------------------ 共用工具


def _host_matches(domain: str) -> bool:
    """Cookie 的 domain 是否属于目标站点。

    注意前导点：Chrome 与 Firefox 都可能写成 ``.meruki.cn``。
    """
    d = domain.lstrip(".").lower()
    return d == TARGET_HOST or d.endswith("." + TARGET_HOST)


def _build_header(pairs: list[tuple[str, str]]) -> str:
    """把 (name, value) 列表拼成 Cookie 头，同名去重保留第一个。"""
    seen: dict[str, str] = {}
    for name, value in pairs:
        if name and name not in seen:
            seen[name] = value
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def _exc_name(exc: BaseException) -> str:
    """只暴露异常类型名，避免把底层信息（可能含路径/令牌）带到用户面前。"""
    return type(exc).__name__


def _cdp_unreachable(port: int) -> CaptureFailure:
    """**只有**真正连不上调试端口时才用这条。"""
    return CaptureFailure(
        reason=f"连接 {port} 端口失败：该端口上没有可用的调试接口",
        hints=(
            "若要自动获取 Cookie，请用调试端口启动浏览器，例如：",
            f'  Chrome: open -a "Google Chrome" --args --remote-debugging-port={port}',
            "或者使用「手动粘贴 Cookie」方式登录",
        ),
    )


def _cdp_bad_status(port: int, status: int) -> CaptureFailure:
    """端口**有**响应但不是可用的调试接口（如 HTTP 403 / 503）。

    典型来源有两个，都不是"端口没开"：

    - 残留的 Chrome 实例占着该端口但正在退出（DevTools 在关闭过程中返回 503）；
    - Chrome 用**默认**用户配置启动：新版 Chrome 的远程调试要求显式指定
      ``--user-data-dir``，否则调试端口不会真正可用。
    """
    hints: list[str] = []
    if status in (401, 403):
        hints.append("调试接口拒绝了请求：请确认是用 --remote-debugging-port 启动的 Chrome")
    else:
        # 状态码已在 reason 里说过，这里只给"为什么"和"怎么办"
        hints.append("常见原因：残留的 Chrome 实例占着端口，或浏览器正在退出")
    hints.extend(
        [
            "建议：先完全退出 Chrome，再用独立配置目录启动一次：",
            f'  open -a "Google Chrome" --args --remote-debugging-port={port}'
            " --user-data-dir=/tmp/meruki-chrome",
            f"确认端口占用：lsof -nP -iTCP:{port} -sTCP:LISTEN",
            "或者使用「手动粘贴 Cookie」方式登录",
        ]
    )
    return CaptureFailure(
        reason=f"{port} 端口有响应，但调试接口不可用（HTTP {status}）", hints=tuple(hints)
    )


# ------------------------------------------------------------------ 来源 1：CDP


async def wait_for_debug_port(port: int, *, timeout: float = 10.0) -> bool:
    """等待调试端口就绪。

    浏览器进程启动到 DevTools HTTP 服务可用之间有几百毫秒的空档，直接请求会
    得到连接失败。轮询 ``/json/version`` 直到 200 或超时。

    注意 ``trust_env=False``：本机回环请求不能被系统代理接管（否则会拿到
    代理返回的状态码，看起来像"端口不通"）。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
                resp = await client.get(f"http://127.0.0.1:{port}/json/version")
                if resp.status_code == 200:
                    return True
        except (httpx.HTTPError, OSError):
            pass
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.25)


def _url_belongs_to_site(url: str) -> bool:
    """这个 URL 的主机名是不是目标站点（或它的子域）。

    只看 hostname：路径、查询串、片段里出现 ``meruki.cn`` 都不算。解析不出
    主机名（``about:blank``、扩展页、非法 URL）一律返回 False，让调用方回退到
    下一档。
    """
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return bool(host) and _host_matches(host)


def _select_target(targets: object) -> str:
    """从 ``/json/list`` 里挑出 CDP 用的 WebSocket 地址。

    ``/json/list`` 的顺序不可靠：扩展的 ``background_page``、service worker
    往往排在真正的主页面之前。所以要显式筛选：

    1. ``type == "page"`` 且 URL 属于目标站点（最理想）；
    2. 任意 ``type == "page"``；
    3. 都没匹配上才回退到"第一个带 ``webSocketDebuggerUrl`` 的 target"。

    没有可用地址时返回空串。``webSocketDebuggerUrl`` 是空串也视为没有。
    """
    candidates: list[tuple[str, str, str]] = []
    for t in targets if isinstance(targets, list) else []:
        if not isinstance(t, dict):
            continue
        ws_url = t.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        candidates.append((str(t.get("type", "")), str(t.get("url", "")), str(ws_url)))
    if not candidates:
        return ""

    # 注意：目标站点要从 target 的 url 判断，不能从 webSocketDebuggerUrl 判断——
    # 后者永远是 ws://127.0.0.1:9222/devtools/page/<id>，不含站点信息。
    #
    # 判据是**主机名相等或子域**，不是"URL 里含这串字符"：搜索页
    # （?q=meruki.cn）和仿冒域名（meruki.cn.evil.example）都含这串字符，用
    # 子串匹配会让它们抢在真正的拍品页前面被选中。
    for type_, page_url, ws_url in candidates:
        if type_ == "page" and _url_belongs_to_site(page_url):
            return ws_url
    for type_, _, ws_url in candidates:
        if type_ == "page":
            return ws_url
    return candidates[0][2]


async def capture_via_cdp(
    port: int = DEFAULT_CDP_PORT, *, timeout: float = DEFAULT_CDP_TIMEOUT
) -> CaptureResult | CaptureFailure:
    """通过 Chrome DevTools Protocol 读取 Cookie。

    前提：浏览器以 ``--remote-debugging-port=<port>`` 启动。
    这是唯一能"全自动"拿到 Chrome 系 Cookie 的受支持途径——走的是浏览器
    自己提供的调试接口，不需要解密它的存储、不触碰用户密码。

    Args:
        port: 调试端口。
        timeout: 整次 CDP 交互的**总**超时上限（秒），不是每个请求的上限：
            否则"连不上 + 每个请求都慢"会把总耗时放大数倍。

    Returns:
        成功返回 :class:`CaptureResult`，否则 :class:`CaptureFailure`（附建议）。

    失败原因严格区分三种，互不覆盖：

    - 端口不通 → "连接 N 端口失败…"（给启动浏览器的建议）；
    - 连上了但调用失败（握手/超时/CDP 返回 error）→ 原样透传 ``_cdp_get_cookies``
      的原因；
    - 调用成功但没匹配到 Cookie → "可能尚未登录或未在目标页面"。
    """
    base = f"http://127.0.0.1:{port}"
    try:
        async with asyncio.timeout(timeout):
            # trust_env=False **必须**：默认会读取系统/环境代理设置（macOS 上
            # urllib 会去查 System Configuration），于是发往 127.0.0.1 的请求也会
            # 被本地代理接管，代理回一个 503 —— 表现就是"调试端口永远不可用"，
            # 哪怕 Chrome 明明正确启动了。本机回环地址不该经过任何代理。
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                # 1. 找一个可用的 target
                try:
                    resp = await client.get(f"{base}/json/list")
                except (httpx.HTTPError, OSError) as exc:
                    # 连不上（拒绝连接 / 超时 / DNS）：这才叫"端口上没有调试接口"
                    _LOG.debug("CDP /json/list 连接失败: %r", exc)
                    return _cdp_unreachable(port)

                if resp.status_code != 200:
                    # 端口**有**响应，但不是可用的调试接口。绝不能再说"端口不通"，
                    # 否则用户会去做一件已经做过的事（他就是照提示启动的浏览器）。
                    _LOG.debug("CDP /json/list 返回 %s: %r", resp.status_code, resp.text[:200])
                    return _cdp_bad_status(port, resp.status_code)

                try:
                    targets = resp.json()
                except (json.JSONDecodeError, ValueError):
                    _LOG.debug("CDP /json/list 返回非 JSON: %r", resp.text[:200])
                    return CaptureFailure(
                        reason=f"{port} 端口有响应，但返回的不是调试接口的 JSON 列表",
                        hints=(
                            "该端口可能被别的程序占用，或浏览器尚未就绪",
                            f"确认端口：lsof -nP -iTCP:{port} -sTCP:LISTEN",
                            "或者使用「手动粘贴 Cookie」方式登录",
                        ),
                    )

                ws_url = _select_target(targets)
                if not ws_url:
                    return CaptureFailure(
                        reason=(
                            "调试接口可用，但没有找到可操作的页面（请先在浏览器里打开 meruki.cn）"
                        ),
                        hints=(f"在浏览器中打开 https://www.{TARGET_HOST}/ 后重试",),
                    )

                # 2. 通过 HTTP 版的 /json 拿不到 Cookie，需要 WebSocket 调
                #    Network.getCookies（见模块 docstring 里关于手写客户端的说明）。
                pairs = await _cdp_get_cookies(ws_url, timeout)
    except TimeoutError:
        return CaptureFailure(
            reason=f"调试接口在 {timeout:.0f}s 内没有响应（超时）",
            hints=(
                "浏览器可能繁忙或调试接口被占用；请稍后重试",
                "或者使用「手动粘贴 Cookie」方式登录",
            ),
        )
    except OSError as exc:
        # 只可能来自连接阶段（httpx 的网络错误是其子类，已在上面处理）
        _LOG.debug("CDP 连接异常: %r", exc)
        return _cdp_unreachable(port)

    # 连上了但没拿到 → 必须原样透传真实原因，绝不能改写成"端口上没有调试接口"
    if isinstance(pairs, CaptureFailure):
        return pairs
    if not pairs:
        return CaptureFailure(
            reason=(
                "调试接口调用成功，但目标站点没有匹配的 Cookie"
                "（可能尚未登录，或当前未在目标页面完成登录）"
            ),
            hints=(
                f"请先在浏览器里打开并完成 {TARGET_HOST} 的登录，再重试",
                "或者使用「手动粘贴 Cookie」方式登录",
            ),
        )
    header = _build_header(pairs)
    return CaptureResult(cookie=header, source="cdp", note="已通过浏览器调试接口获取 Cookie")


async def _cdp_get_cookies(ws_url: str, timeout: float) -> list[tuple[str, str]] | CaptureFailure:
    """用最小 WebSocket 客户端调用 ``Network.getCookies``。

    Returns:
        目标站点的 ``(name, value)`` 列表（可能为空：调用成功但没有匹配的 Cookie）；
        调用本身失败时返回 :class:`CaptureFailure`，其 ``reason`` 会由
        :func:`capture_via_cdp` 原样透传给用户。
    """
    try:
        payload = {
            "id": 1,
            "method": "Network.getCookies",
            "params": {"urls": ["https://www." + TARGET_HOST]},
        }
        data = await _ws_call(ws_url, payload, timeout)
        cookies = data.get("result", {}).get("cookies", [])
        if not isinstance(cookies, list):
            return CaptureFailure(reason="调试接口返回了非预期的响应结构")
        return [
            (str(c.get("name", "")), str(c.get("value", "")))
            for c in cookies
            if isinstance(c, dict) and _host_matches(str(c.get("domain", "")))
        ]
    except WebSocketError as exc:
        # 自己抛的错误文案本来就面向用户，直接透传
        return CaptureFailure(reason=f"调用调试接口失败：{exc}")
    except (TypeError, ValueError) as exc:
        # JSON 解析失败 / 响应结构不对；类型名足够定位，细节进 debug 日志
        _LOG.debug("解析 CDP 响应失败: %r", exc)
        return CaptureFailure(reason=f"调用调试接口失败：响应无法解析（{_exc_name(exc)}）")
    except Exception as exc:  # 兜底：绝不能把裸异常抛给 UI 层
        _LOG.debug("CDP 调用异常: %r", exc)
        return CaptureFailure(reason=f"调用调试接口失败：{_exc_name(exc)}")


class WebSocketError(RuntimeError):
    """WebSocket 层面的失败（握手/协议/对端关闭）。文案面向用户。"""


async def _ws_call(ws_url: str, request: dict, timeout: float) -> dict:
    """发一条 CDP 请求，返回 ``id`` 匹配的响应对象（已确认不含 ``error``）。

    CDP 在响应之前可能推任意多事件帧（``{"method": ...}``，没有 ``id`` 或
    ``id`` 不同），所以不能"读一帧就当响应"：循环读帧，跳过解析失败/无 id/id
    不匹配的帧，直到拿到 ``id == request["id"]`` 的那一条。

    Raises:
        WebSocketError: 握手失败、对端关闭、超时，或 CDP 返回了 ``error``。
    """
    expected_id = request.get("id")

    def wants(text: str) -> bool:
        try:
            msg = json.loads(text)
        except (ValueError, TypeError):
            return False
        return isinstance(msg, dict) and msg.get("id") == expected_id

    raw = await _ws_simple_call(ws_url, json.dumps(request), timeout, wants=wants)
    msg = json.loads(raw)
    if "error" in msg:
        raise WebSocketError(_format_cdp_error(msg["error"]))
    return msg


def _format_cdp_error(error: object) -> str:
    """把 CDP 的 ``{"code":..,"message":..}`` 转成一句人话。"""
    if not isinstance(error, dict):
        return f"调试接口返回错误：{error!s:.200}"
    code = error.get("code")
    message = str(error.get("message", "")).strip() or "未知错误"
    return f"调试接口返回错误（{code}）：{message}"


async def _ws_simple_call(
    ws_url: str,
    message: str,
    timeout: float,
    *,
    wants: Callable[[str], bool] | None = None,
) -> str:
    """极简 WebSocket 客户端：握手 + 发一帧文本 + 收一条**想要的**完整消息。

    仅支持本场景所需的明文本地连接（ws://127.0.0.1）。整次调用受 ``timeout``
    硬性约束（``asyncio.timeout`` 覆盖连接、握手、收发全过程）。

    Args:
        wants: 谓词，判断某条消息是不是我们要的；默认接受第一条。
            不匹配的消息（CDP 事件帧、别人的响应）会被跳过，继续读下一条。
    """
    parts = urlsplit(ws_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    try:
        async with asyncio.timeout(timeout):
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except OSError as exc:
                raise WebSocketError(
                    f"连接调试接口的 WebSocket 失败（{host}:{port}，{_exc_name(exc)}）"
                ) from exc
            try:
                await _ws_handshake(reader, writer, host, port, path)
                # 发送（客户端帧必须掩码）
                writer.write(_ws_frame(message.encode(), _OP_TEXT))
                await writer.drain()
                return await _ws_read_message(reader, writer, wants=wants)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
    except TimeoutError as exc:
        raise WebSocketError(f"等待调试接口响应超时（{timeout:.0f}s）") from exc


async def _ws_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    path: str,
) -> None:
    """发 HTTP Upgrade 请求并校验 101 与 ``Sec-WebSocket-Accept``。"""
    key = _ws_key()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    writer.write(req.encode())
    await writer.drain()

    head = await _read_headers(reader)
    first_line, _, rest = head.partition("\r\n")
    status_parts = first_line.split()
    # 精确判断状态码，避免 "HTTP/1.1 101" 出现在别的文本里造成误判
    if len(status_parts) < 2 or status_parts[1] != "101":
        raise WebSocketError(f"WebSocket 握手失败（{first_line.strip() or '无响应'}）")
    if not _header_ok(head, "upgrade", "websocket") or not _header_ok(
        head, "connection", "upgrade"
    ):
        raise WebSocketError("WebSocket 握手失败（缺少 Upgrade/Connection 头）")
    expect = _ws_accept(key)
    actual = _header_value(rest, "sec-websocket-accept")
    if actual is not None and actual != expect:
        raise WebSocketError("WebSocket 握手失败（Sec-WebSocket-Accept 不匹配）")


def _header_value(header_block: str, name: str) -> str | None:
    """从响应头块里取某个头（不区分大小写），取不到返回 None。"""
    for line in header_block.split("\r\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == name:
            return value.strip()
    return None


def _header_ok(header_block: str, name: str, expected: str) -> bool:
    """头存在且包含期望值（Connection 可能是逗号分隔的列表）。"""
    value = _header_value(header_block, name)
    if value is None:
        return False
    return expected in [p.strip().lower() for p in value.split(",")]


def _ws_key() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def _ws_accept(key: str) -> str:
    """RFC 6455 定义的握手校验值。"""
    digest = hashlib.sha1((key + _WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


async def _read_headers(reader: asyncio.StreamReader) -> str:
    """读到空行为止，返回完整响应头（含首行）。有大小上限。"""
    lines = []
    total = 0
    while True:
        line = await reader.readline()
        if not line:
            break
        total += len(line)
        if total > _MAX_HEADER_BYTES:
            raise WebSocketError("WebSocket 握手失败（响应头过大）")
        lines.append(line.decode("latin-1"))
        if line in (b"\r\n", b"\n"):
            break
    return "".join(lines)


def _ws_frame(payload: bytes, opcode: int = _OP_TEXT, *, fin: bool = True) -> bytes:
    """构造客户端帧（默认 FIN=1 的文本帧，带掩码）。

    Args:
        payload: 载荷。
        opcode: 帧类型；``0x9`` 是 ping，``0xA`` 是 pong，``0x8`` 是 close。
        fin: FIN 位。控制帧必须为 True，且载荷不得超过 125 字节。
    """
    control = opcode >= _OP_CLOSE
    if control and len(payload) > 125:
        raise ValueError("控制帧的载荷不能超过 125 字节")
    if control and not fin:
        raise ValueError("控制帧不能分片")
    n = len(payload)
    header = bytearray([(0x80 if fin else 0x00) | (opcode & 0x0F)])
    mask_bit = 0x80
    if n < 126:
        header.append(mask_bit | n)
    elif n < 65536:
        header.append(mask_bit | 126)
        header += n.to_bytes(2, "big")
    else:
        header.append(mask_bit | 127)
        header += n.to_bytes(8, "big")
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def _ws_unframe(b1: int, mask: bytes, data: bytes) -> tuple[bool, int, bytes]:
    """解出 (FIN, opcode, 载荷)。服务端帧按 RFC 不应带掩码，带了就解掉。"""
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return fin, opcode, data


async def _ws_read_raw_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """读一个原始帧，返回 (FIN, opcode, 未解码的载荷)。"""
    b1, b2 = await reader.readexactly(2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    if b1 & 0x70:
        raise WebSocketError("WebSocket 协议错误（RSV 位非零，未协商扩展）")
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    if opcode >= _OP_CLOSE and (length > 125 or not fin):
        raise WebSocketError("WebSocket 协议错误（控制帧过长或分片）")
    if length > _MAX_MESSAGE_BYTES:
        raise WebSocketError("WebSocket 消息过大")
    mask = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(length) if length else b""
    return _ws_unframe(b1, mask, data)


async def _ws_read_message(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None = None,
    *,
    wants: Callable[[str], bool] | None = None,
) -> str:
    """读出一条**符合条件的**完整消息（已合并所有分片），处理 ping/pong/close。

    ``wants`` 不匹配的消息会被整条丢弃并继续读下一条——这是 CDP 必需的行为：
    浏览器会在响应之前推事件帧，甚至推送别的请求的响应。

    遵循 RFC 6455 的关键点：

    - 控制帧可以插在分片消息中间，也可能先于业务帧到达 → 必须原地处理完继续读；
    - ping 必须回同样载荷的 pong（FIN=1、≤125 字节）；
    - pong 与未知控制帧忽略；
    - close 视为失败（调用方还没拿到响应）；
    - opcode 0x1/0x2 且 FIN=1 直接返回，FIN=0 进缓冲等 continuation；
    - 分片期间再收到新的数据帧属于协议错误。
    """
    chunks: list[bytes] = []
    buffered_opcode: int | None = None
    controls = 0

    while True:
        fin, opcode, data = await _ws_read_raw_frame(reader)

        if opcode == _OP_PING:
            controls += 1
            if controls > _MAX_CONTROL_FRAMES:
                raise WebSocketError("WebSocket 协议错误（控制帧过多）")
            if writer is not None:
                writer.write(_ws_frame(data, _OP_PONG))
                await writer.drain()
            continue
        if opcode == _OP_PONG:
            continue
        if opcode == _OP_CLOSE:
            raise WebSocketError("对端关闭了 WebSocket 连接（未返回调试结果）")

        if opcode == _OP_CONTINUATION:
            if buffered_opcode is None:
                raise WebSocketError("WebSocket 协议错误（没有起始帧的 continuation）")
            buffered_opcode = _OP_TEXT if buffered_opcode == _OP_TEXT else _OP_BINARY
            if buffered_opcode == _OP_TEXT:
                chunks.append(data)
            else:
                raise WebSocketError("WebSocket 收到二进制消息（应为文本 JSON）")
        elif opcode in (_OP_TEXT, _OP_BINARY):
            if buffered_opcode is not None:
                raise WebSocketError("WebSocket 协议错误（分片消息未结束又来新消息）")
            buffered_opcode = opcode
            if opcode == _OP_BINARY:
                raise WebSocketError("WebSocket 收到二进制消息（应为文本 JSON）")
            chunks.append(data)
        else:
            raise WebSocketError(f"WebSocket 协议错误（未知 opcode 0x{opcode:x}）")

        if fin:
            # 文本帧理论上要验 UTF-8，但 JSON 解析会兜住；这里宽松解码避免
            # 因浏览器侧偶发的替代字符而丢掉整个响应。
            text = b"".join(chunks).decode("utf-8", "replace")
            # 一整条消息结束：重置分片状态，再判断是不是我们要的那条
            chunks.clear()
            buffered_opcode = None
            if wants is None or wants(text):
                return text
            continue


# ------------------------------------------------------------------ 来源 2：Firefox


def firefox_cookie_dbs() -> list[Path]:
    """返回各平台可能存在的 Firefox cookie 数据库路径。

    **按 cookies.sqlite 的 mtime 倒序**：最近用过的 profile 最可能是用户
    真正在登录的那个；按字母序会优先读到早已废弃的 profile。
    """
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support" / "Firefox" / "Profiles"
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA", home)) / "Mozilla" / "Firefox" / "Profiles"
    else:
        root = home / ".mozilla" / "firefox"
    if not root.is_dir():
        return []

    found: list[tuple[float, str, Path]] = []
    for db in root.glob("*/cookies.sqlite"):
        try:
            mtime = db.stat().st_mtime
        except OSError:
            # 读不到 mtime 的排最后，但不要因此丢掉候选
            mtime = 0.0
        found.append((mtime, str(db), db))
    # 路径参与排序，保证 mtime 相同时结果依然确定
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [db for _, _, db in found]


def capture_via_firefox() -> CaptureResult | CaptureFailure:
    """从 Firefox 的明文 cookie 库读取目标站点 Cookie。

    Firefox 的 ``cookies.sqlite`` 不加密，可以直接查。但浏览器运行时该文件可能
    被锁，因此先复制到临时文件再读（只读快照，不改动用户数据）。

    注意：本函数是**同步**的（文件复制 + sqlite 查询）。在协程里调用必须用
    ``asyncio.to_thread``，否则会阻塞事件循环。

    Returns:
        成功返回 :class:`CaptureResult`，否则 :class:`CaptureFailure`。
    """
    dbs = firefox_cookie_dbs()
    if not dbs:
        return CaptureFailure(
            reason="没有找到 Firefox 的 Cookie 数据库",
            hints=("如果你用的是 Chrome，请改用「浏览器登录」中的调试端口方式",),
        )

    errors: list[str] = []
    for db in dbs:
        try:
            pairs = _read_firefox_db(db)
        except (sqlite3.Error, OSError) as exc:
            _LOG.debug("读取 %s 失败: %r", db, exc)
            errors.append(f"{db.parent.name}: {_exc_name(exc)}")
            continue
        if pairs:
            return CaptureResult(
                cookie=_build_header(pairs),
                source="firefox",
                note=f"已从 Firefox 配置「{db.parent.name}」读取 Cookie",
            )

    if errors:
        return CaptureFailure(
            reason="读取 Firefox Cookie 库失败：" + "；".join(errors),
            hints=("请先关闭 Firefox 再重试（运行中会锁住数据库）",),
        )
    return CaptureFailure(
        reason="Firefox 中没有找到目标站点的 Cookie（可能尚未登录）",
        hints=(f"请先在 Firefox 里登录 {TARGET_HOST}",),
    )


def _read_firefox_db(db: Path) -> list[tuple[str, str]]:
    """复制一份再查，避免锁库；返回目标站点的 (name, value)。

    ``moz_cookies`` 各版本表结构略有差异，但 ``name`` / ``value`` / ``host``
    三列长期稳定，因此直接在 SQL 里带条件查询，再在 Python 侧做后缀匹配
    （后缀匹配无法直接在 SQL 里安全表达：``LIKE '%meruki.cn'`` 会把
    ``notmeruki.cn`` 也选进来，所以 SQL 只做粗筛）。

    - 快照用 ``mode=ro`` 只读打开，**不能加 ``immutable=1``**：immutable 会让
      SQLite 直接忽略 ``-wal``，而 Firefox 完全可能把最新的 Cookie 留在 WAL 里
      还没 checkpoint（实测这种库里主文件连 ``moz_cookies`` 表都还没有）。加了
      immutable 的后果是读出一句 ``no such table: moz_cookies``，用户被告知
      「请先关闭 Firefox」，可真正的原因在打开方式。正常关闭浏览器后 WAL 会被
      checkpoint，所以这个问题只在「浏览器还开着」时出现——恰恰是用户点「浏览器
      登录」的典型时刻。
    - ``ORDER BY`` 保证结果确定（同名 Cookie 的保留第一个取决于顺序）。
    - value 为空的 Cookie 直接跳过：空值在 Cookie 头里没有意义，而且常常是
      注销后留下的残渣，拼进头部反而会干扰服务端判断。
    """
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "cookies.sqlite"
        shutil.copyfile(db, snap)
        # 可能有 WAL，一并带上（存在才复制），否则读不到最新写入
        wal = db.with_name(db.name + "-wal")
        if wal.exists():
            shutil.copyfile(wal, Path(td) / (snap.name + "-wal"))
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT name, value, host FROM moz_cookies "
                "WHERE host LIKE ? AND value IS NOT NULL AND value != '' "
                "ORDER BY host, name, creationTime",
                (f"%{TARGET_HOST}",),
            ).fetchall()
        finally:
            con.close()

    pairs: list[tuple[str, str]] = []
    for name, value, host in rows:
        name_s, value_s = str(name or ""), str(value or "")
        if not name_s or not value_s:
            continue
        if not _host_matches(str(host or "")):
            continue
        pairs.append((name_s, value_s))
    return pairs


# ------------------------------------------------------------------ 统一入口


async def capture_cookie(port: int = DEFAULT_CDP_PORT) -> CaptureResult | CaptureFailure:
    """按优先级尝试所有自动捕获方式。

    顺序：CDP 调试端口 → Firefox 明文库。

    :func:`capture_via_firefox` 是同步函数（文件复制 + sqlite 查询），
    放进 ``asyncio.to_thread`` 执行：它可能耗时数百毫秒到数秒，
    直接调用会卡死事件循环（TUI 会整个失去响应）。

    Returns:
        成功返回 :class:`CaptureResult`；全失败返回 :class:`CaptureFailure`，
        其 ``hints`` 应展示给用户以引导回退到手动粘贴。
    """
    cdp = await capture_via_cdp(port)
    if isinstance(cdp, CaptureResult):
        return cdp

    ff = await asyncio.to_thread(capture_via_firefox)
    if isinstance(ff, CaptureResult):
        return ff

    # 两条路都失败：把各自的**具体**原因与可操作建议都带给用户，并标明来源。
    # 否则用户只看到笼统的"未能自动获取 Cookie"，无从判断是端口没开、
    # 端口被占用、还是没找到 Firefox 库。
    hints: list[str] = [f"CDP：{cdp.reason}"]
    # CDP 的针对性建议（如 --user-data-dir / lsof）；"改用粘贴"这类兜底话术
    # 统一放到最后，避免与下面的收尾建议重复。
    hints.extend(h for h in cdp.hints if "手动粘贴" not in h)
    hints.append(f"Firefox：{ff.reason}")
    hints.append("请使用「手动粘贴 Cookie」方式登录（TUI 里按 s 进配置 → 登录，再选粘贴）")
    return CaptureFailure(reason="未能自动获取 Cookie", hints=tuple(hints))
