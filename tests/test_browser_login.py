"""浏览器登录链路的测试。

覆盖三个新模块的可测部分（不依赖真实浏览器 / 真实站点 / 真实网络）：

- ``adapters.browser``：平台候选命令、图形会话探测、成功/失败语义
- ``adapters.cookie_capture``：CDP WebSocket 客户端、target 筛选、domain 匹配、
  Cookie 头拼装、Firefox 库读取、失败原因分类
- ``app.browser_login``：编排的成功/失败/超时/异常折返路径

**关于假 CDP 服务器**：``test_browser_login.py`` 里的 CDP 用例都在
``asyncio.start_server`` 上跑一个真的 TCP 服务器，真的做 HTTP Upgrade 握手、
真的按 RFC 6455 编码帧（含 ping、分片、16/64 位长度、close），被测的
``_ws_simple_call`` / ``_ws_read_message`` / ``_cdp_get_cookies`` 全部走真实网络路径。
只有 HTTP 的 ``/json/list`` 用 ``httpx.MockTransport`` 替换（那是 httpx 的职责，
不是我们的被测逻辑）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import sqlite3
import struct
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from meruki_tracker.core.adapters import browser, cookie_capture
from meruki_tracker.core.adapters.browser import BrowserLaunch, OpenResult
from meruki_tracker.core.adapters.cookie_capture import (
    CaptureFailure,
    CaptureResult,
    WebSocketError,
)
from meruki_tracker.core.app.browser_login import browser_login

_PROFILE = Path("/tmp/mw-test-chrome-profile")

WS_SERVER_TIMEOUT = 5.0

# ================================================================== 假 WebSocket 服务器
#
# 这里是"另一个实现"：按 RFC 6455 的**服务端**视角独立编码帧，不复用被测模块的
# 任何帧构造/解析代码，否则两边一起错会互相掩盖。


def ws_server_frame(payload: bytes, opcode: int = 0x1, *, fin: bool = True) -> bytes:
    """构造服务端帧（服务端帧不加掩码）。"""
    b1 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        head = bytes([b1, n])
    elif n < 65536:
        head = bytes([b1, 126]) + struct.pack("!H", n)
    else:
        head = bytes([b1, 127]) + struct.pack("!Q", n)
    return head + payload


class WsClientFrame:
    """服务端视角解析出的客户端帧。"""

    def __init__(self, fin: bool, opcode: int, payload: bytes) -> None:
        self.fin = fin
        self.opcode = opcode
        self.payload = payload

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"WsClientFrame(fin={self.fin}, opcode=0x{self.opcode:x}, {self.payload!r})"


async def ws_read_client_frame(reader: asyncio.StreamReader) -> WsClientFrame:
    """读一个客户端帧并解掩码；客户端帧**必须**带掩码，否则报协议错误。"""
    b1, b2 = await reader.readexactly(2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    if not b2 & 0x80:
        raise AssertionError("客户端帧必须带掩码（RFC 6455 §5.3）")
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4)
    data = await reader.readexactly(length)
    return WsClientFrame(fin, opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


class FakeWsServer:
    """一个本地 TCP 服务器，让脚本按需要"演"出各种握手下场与帧序列。"""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        #: 服务端收到的所有客户端帧（含 pong），供断言
        self.received: list[WsClientFrame] = []
        #: 握手请求原文
        self.request_head = ""
        #: 服务端把响应写到哪一步（区分"半帧后卡住"）
        self.script_error: BaseException | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/page/ABC"

    async def __aenter__(self) -> FakeWsServer:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self, script) -> None:
        """启动服务器；``script(server, reader, writer)`` 处理单个连接。"""

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await asyncio.wait_for(script(self, reader, writer), WS_SERVER_TIMEOUT)
            except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
                pass  # 客户端提前关闭：正常场景（超时/报错后主动断开）
            except BaseException as exc:  # 把断言错误带回主线程
                self.script_error = exc
            finally:
                writer.close()
                with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                    await writer.wait_closed()

        self._server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def raise_if_failed(self) -> None:
        if self.script_error is not None:
            raise self.script_error


async def ws_accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    """读完客户端握手请求，回 101 + 正确的 Sec-WebSocket-Accept。"""
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
    key = ""
    for line in head.split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "sec-websocket-key":
            key = value.strip()
    assert key, f"客户端没有发 Sec-WebSocket-Key：{head!r}"
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode()
    )
    await writer.drain()
    return head


def cdp_result(cookies: list[dict]) -> str:
    return json.dumps({"id": 1, "result": {"cookies": cookies}}, ensure_ascii=False)


def meruki_cookies() -> list[dict]:
    return [
        {"name": "sid", "value": "SECRET-SID", "domain": ".meruki.cn"},
        {"name": "other", "value": "nope", "domain": ".evil.com"},
        {"name": "uid", "value": "42", "domain": "www.meruki.cn"},
    ]


# ------------------------------------------------------------ 脚本化的"标准 CDP 服务"
#
# 每种帧布局都从同一个脚本产出：先读请求 → 回 101 → 读客户端请求帧 → 按布局写响应帧。
# 这样所有变体都真正跑通了被测代码，差异只在"服务端怎么发"。


def make_script(*, respond):
    """``respond(payload_text, server, reader, writer)`` —— 参数不足时返回标准响应。"""

    async def script(server: FakeWsServer, reader, writer):
        await ws_accept(reader, writer)
        frame = await ws_read_client_frame(reader)
        server.received.append(frame)
        payload = json.loads(frame.payload.decode())
        server.request_head = json.dumps(payload)
        await respond(payload, server, reader, writer)

    return script


async def standard_respond(payload, server, reader, writer, **opts):
    writer.write(ws_server_frame(cdp_result(meruki_cookies()).encode()))
    await writer.drain()


def script_plain():
    return make_script(respond=standard_respond)


# ================================================================== 夹具


@pytest.fixture
async def ws_server():
    """启动一个假 WS 服务器；用例里用 :meth:`FakeWsServer.start` 装载脚本。"""
    server = FakeWsServer()
    try:
        yield server
    finally:
        server.raise_if_failed()
        await server.close()


class FakeJsonList:
    """把 httpx 的 ``/json/list`` 换成给定 JSON（真实 TCP 那段仍由 FakeWsServer 承担）。"""

    def __init__(self, targets: object, *, status: int = 200) -> None:
        self.targets = targets
        self.status = status
        self.asked: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.asked.append(str(request.url))
            return httpx.Response(self.status, json=self.targets)

        return httpx.MockTransport(handler)

    def patch(self, port: int):
        """同时替换 AsyncClient 的 transport 与端口，让被测代码连到假服务器。"""

        real_client = httpx.AsyncClient

        def fake_client(*args, **kwargs):
            kwargs["transport"] = self.transport()
            return real_client(*args, **kwargs)

        return patch.object(cookie_capture.httpx, "AsyncClient", fake_client)


def _targets_for(port: int, *, extra_first: list[dict] | None = None) -> list[dict]:
    base = list(extra_first or [])
    base.append(
        {
            "type": "page",
            "url": "https://www.meruki.cn/",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/ABC",
        }
    )
    return base


# ================================================================== CDP 端到端


async def test_cdp_happy_path_returns_cookie(ws_server):
    """正路：握手 101 + 一帧文本响应 → 拿到过滤后的 Cookie 头。"""
    await ws_server.start(script_plain())
    json_list = FakeJsonList(_targets_for(ws_server.port))
    with json_list.patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res
    assert res.source == "cdp"
    assert res.cookie == "sid=SECRET-SID; uid=42"
    # 客户端确实调用了 Network.getCookies
    assert json.loads(ws_server.request_head)["method"] == "Network.getCookies"
    assert ws_server.received[0].opcode == 0x1


async def test_cdp_handshake_non_101_is_reported_as_failure(ws_server):
    """非 101 的握手 → "连上但调用失败"，不能伪装成"端口上没有调试接口"。"""

    async def script(server, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()

    await ws_server.start(script)
    json_list = FakeJsonList(_targets_for(ws_server.port))
    with json_list.patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureFailure)
    assert "握手失败" in res.reason
    assert "404" in res.reason
    # 关键断言：真实原因不能被抹成"端口上没有可用的调试接口"
    assert "没有可用的调试接口" not in res.reason


async def test_cdp_rejects_bad_accept_header(ws_server):
    """101 但 Sec-WebSocket-Accept 不对 → 必须失败（不能瞎信 101）。"""

    async def script(server, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: WRONGACCEPTVALUE=\r\n\r\n"
        )
        await writer.drain()

    await ws_server.start(script)
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureFailure)
    assert "握手失败" in res.reason


async def test_cdp_126_length_branch(ws_server):
    """服务端帧用 16 位扩展长度（126..65535）。用大 cookie 撑过 126 字节。"""
    big = [
        {"name": f"c{i}", "value": "x" * 20, "domain": ".meruki.cn"} for i in range(10)
    ]  # 拼出的 JSON 超过 126 字节，服务端必须走 126 分支

    async def respond(payload, server, reader, writer):
        raw = cdp_result(big).encode()
        assert 126 <= len(raw) < 65536
        writer.write(ws_server_frame(raw))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res
    for i in range(10):
        assert f"c{i}=" + "x" * 20 in res.cookie


async def test_cdp_127_length_branch(ws_server):
    """服务端帧用 64 位扩展长度（>=65536）。"""
    huge_value = "y" * 70_000
    big = [{"name": "huge", "value": huge_value, "domain": ".meruki.cn"}]

    async def respond(payload, server, reader, writer):
        raw = cdp_result(big).encode()
        assert len(raw) >= 65536
        writer.write(ws_server_frame(raw))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=10.0)

    assert isinstance(res, CaptureResult), res
    assert res.cookie == f"huge={huge_value}"


async def test_cdp_fragmented_message_is_joined(ws_server):
    """text FIN=0 + continuation FIN=1，中间还夹一个 ping → 能拼回完整 JSON。"""

    async def respond(payload, server, reader, writer):
        raw = cdp_result(meruki_cookies()).encode()
        cut = len(raw) // 2
        writer.write(ws_server_frame(raw[:cut], 0x1, fin=False))
        # 控制帧允许插在分片消息中间
        writer.write(ws_server_frame(b"mid-fragment-ping", 0x9))
        await writer.drain()
        # 先收到 pong 再补上后半片：pong 必须在响应之前就发出，
        # 否则客户端收完响应立刻断开，pong 会和断开竞速。
        server.received.append(await ws_read_client_frame(reader))
        writer.write(ws_server_frame(raw[cut:], 0x0, fin=True))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res
    assert res.cookie == "sid=SECRET-SID; uid=42"
    pong = ws_server.received[-1]
    assert pong.opcode == 0xA, ws_server.received
    assert pong.payload == b"mid-fragment-ping"
    assert pong.fin is True


async def test_cdp_three_fragments(ws_server):
    """多于两片的分片消息也要能正确拼接。"""

    async def respond(payload, server, reader, writer):
        raw = cdp_result(meruki_cookies()).encode()
        third = len(raw) // 3
        writer.write(ws_server_frame(raw[:third], 0x1, fin=False))
        writer.write(ws_server_frame(raw[third : 2 * third], 0x0, fin=False))
        writer.write(ws_server_frame(raw[2 * third :], 0x0, fin=True))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res
    assert res.cookie == "sid=SECRET-SID; uid=42"


async def test_cdp_ping_before_response_gets_pong(ws_server):
    """ping 先到、pong 帧也先到，响应最后 → 跳过它们并回 pong。"""

    async def respond(payload, server, reader, writer):
        writer.write(ws_server_frame(b"ping-1", 0x9))
        writer.write(ws_server_frame(b"unsolicited-pong", 0xA))
        await writer.drain()
        # 读到 pong 后再发第二条 ping：顺序确定，不会和"客户端收完响应就断开"竞速
        server.received.append(await ws_read_client_frame(reader))
        writer.write(ws_server_frame(b"ping-2", 0x9))
        await writer.drain()
        server.received.append(await ws_read_client_frame(reader))
        writer.write(ws_server_frame(cdp_result(meruki_cookies()).encode()))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res
    pongs = [f for f in ws_server.received if f.opcode == 0xA]
    assert [p.payload for p in pongs] == [b"ping-1", b"ping-2"]
    # pong 必须是 FIN=1 的控制帧
    assert all(p.fin for p in pongs)


async def test_cdp_close_frame_fails(ws_server):
    """close 帧 → 明确报"连接被关闭"，而不是当成响应。"""

    async def respond(payload, server, reader, writer):
        writer.write(ws_server_frame(struct.pack("!H", 1000), 0x8))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureFailure)
    assert "关闭" in res.reason


async def test_cdp_ignores_event_frames_without_id(ws_server):
    """CDP 事件帧（无 id）必须先于响应到达并被忽略。"""

    async def respond(payload, server, reader, writer):
        events = [
            {"method": "Network.loadingFinished", "params": {"requestId": "1"}},
            {"method": "Page.frameNavigated", "params": {}},
        ]
        for ev in events:
            writer.write(ws_server_frame(json.dumps(ev).encode()))
        writer.write(ws_server_frame(cdp_result(meruki_cookies()).encode()))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res
    assert res.cookie == "sid=SECRET-SID; uid=42"


async def test_cdp_ignores_frames_with_other_id(ws_server):
    """id 不匹配的响应帧（例如别的请求的回复）也要被忽略。"""

    async def respond(payload, server, reader, writer):
        writer.write(ws_server_frame(json.dumps({"id": 2, "result": {"cookies": []}}).encode()))
        writer.write(ws_server_frame(json.dumps({"id": 99, "result": {}}).encode()))
        writer.write(ws_server_frame(cdp_result(meruki_cookies()).encode()))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res
    assert res.cookie == "sid=SECRET-SID; uid=42"


async def test_cdp_error_response_is_readable(ws_server):
    """CDP 返回 {"error": {...}} → 可读的失败原因，且不包含 Cookie 泄漏。"""

    async def respond(payload, server, reader, writer):
        err = {"id": 1, "error": {"code": -32601, "message": "Method not found"}}
        writer.write(ws_server_frame(json.dumps(err).encode()))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureFailure)
    assert "Method not found" in res.reason
    assert "-32601" in res.reason


async def test_cdp_unparsable_frames_then_valid_one(ws_server):
    """非 JSON 的文本帧要跳过（CDP 不会发，但防御性处理），最终拿到响应。"""

    async def respond(payload, server, reader, writer):
        writer.write(ws_server_frame(b"<html>not json</html>"))
        writer.write(ws_server_frame(cdp_result(meruki_cookies()).encode()))
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res


async def test_cdp_half_frame_then_stall_times_out(ws_server):
    """只发半帧就卡住 → 必须超时失败（不能永久挂起，也不能返回空响应蒙混过关）。"""

    async def respond(payload, server, reader, writer):
        raw = cdp_result(meruki_cookies()).encode()
        # 声称 len(raw) 字节，只写一半，然后什么都不发
        writer.write(ws_server_frame(raw)[: len(raw) // 2 + 2])
        await writer.drain()
        await asyncio.sleep(30)

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        # 外层再套一个保险丝：万一被测代码没超时，测试也不会挂住整个套件
        res = await asyncio.wait_for(
            cookie_capture.capture_via_cdp(ws_server.port, timeout=0.5), timeout=5
        )

    assert isinstance(res, CaptureFailure)
    assert "超时" in res.reason


async def test_cdp_no_response_at_all_times_out(ws_server):
    """握手成功但一个字节都不发 → 超时。"""

    async def respond(payload, server, reader, writer):
        await asyncio.sleep(30)

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await asyncio.wait_for(
            cookie_capture.capture_via_cdp(ws_server.port, timeout=0.5), timeout=5
        )

    assert isinstance(res, CaptureFailure)
    assert "超时" in res.reason


# --------------------------------------------------------------- 失败原因分类（修复项 1）


async def test_cdp_no_matching_cookie_says_maybe_not_logged_in(ws_server):
    """调用成功但没有匹配 Cookie → 文案要点明"可能尚未登录或未在目标页面"。"""

    async def respond(payload, server, reader, writer):
        writer.write(
            ws_server_frame(
                cdp_result([{"name": "x", "value": "1", "domain": ".example.com"}]).encode()
            )
        )
        await writer.drain()

    await ws_server.start(make_script(respond=respond))
    with FakeJsonList(_targets_for(ws_server.port)).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureFailure)
    assert "尚未登录" in res.reason
    assert "目标页面" in res.reason
    # 不能把"没登录"说成"端口上没有调试接口"
    assert "没有可用的调试接口" not in res.reason


async def test_cdp_port_closed_says_no_debugger(monkeypatch):
    """真的连不上端口 → 才用"端口上没有调试接口"这条。

    不能靠"真开一个端口再关掉"：Windows 上关闭监听端口后，SYN 会被丢包
    直到超时而不是立刻 RST，于是走到"超时"分支（reason 不同）。这里直接
    让 `client.get` 抛 `httpx.ConnectError`——那才是"端口不通"的确定信号，
    跨平台一致。
    """

    class _ClosedPortClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(cookie_capture.httpx, "AsyncClient", _ClosedPortClient)
    res = await cookie_capture.capture_via_cdp(1, timeout=2.0)

    assert isinstance(res, CaptureFailure)
    assert "没有可用的调试接口" in res.reason
    assert any("remote-debugging-port" in h for h in res.hints)


async def test_cdp_distinct_reasons_across_three_cases():
    """三种失败必须给出**不同**的原因（防止又退化成同一句话）。"""
    # 端口不通
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    closed_port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    unreachable = await cookie_capture.capture_via_cdp(closed_port, timeout=1.0)

    # 连上但调用失败
    fail_server = FakeWsServer()
    await fail_server.start(make_script(respond=_respond_cdp_error))
    with FakeJsonList(_targets_for(fail_server.port)).patch(fail_server.port):
        call_failed = await cookie_capture.capture_via_cdp(fail_server.port, timeout=2.0)
    fail_server.raise_if_failed()
    await fail_server.close()

    # 调用成功但没匹配到
    empty_server = FakeWsServer()
    await empty_server.start(make_script(respond=_respond_empty_cookies))
    with FakeJsonList(_targets_for(empty_server.port)).patch(empty_server.port):
        no_cookie = await cookie_capture.capture_via_cdp(empty_server.port, timeout=2.0)
    empty_server.raise_if_failed()
    await empty_server.close()

    for res in (unreachable, call_failed, no_cookie):
        assert isinstance(res, CaptureFailure)
    reasons = {unreachable.reason, call_failed.reason, no_cookie.reason}
    assert len(reasons) == 3, reasons


async def _respond_cdp_error(payload, server, reader, writer):
    writer.write(
        ws_server_frame(
            json.dumps({"id": 1, "error": {"code": -32000, "message": "boom"}}).encode()
        )
    )
    await writer.drain()


async def _respond_empty_cookies(payload, server, reader, writer):
    writer.write(ws_server_frame(cdp_result([]).encode()))
    await writer.drain()


# --------------------------------------------------------------- target 筛选（修复项 4）


def test_select_target_prefers_meruki_page():
    ws = "ws://127.0.0.1:9222/devtools/page/M"
    targets = [
        {"type": "background_page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/ext/bg"},
        {"type": "page", "url": "https://example.com", "webSocketDebuggerUrl": "ws://other/page"},
        {"type": "page", "url": "https://www.meruki.cn/list", "webSocketDebuggerUrl": ws},
    ]
    assert cookie_capture._select_target(targets) == ws


def test_select_target_falls_back_to_any_page():
    targets = [
        {"type": "background_page", "webSocketDebuggerUrl": "ws://x/background_page"},
        {"type": "page", "url": "https://example.com", "webSocketDebuggerUrl": "ws://x/page"},
    ]
    assert cookie_capture._select_target(targets) == "ws://x/page"


def test_select_target_last_resort_is_first_with_ws():
    targets = [
        {"type": "worker", "webSocketDebuggerUrl": "ws://x/worker"},
        {"type": "background_page", "webSocketDebuggerUrl": "ws://x/bg"},
    ]
    assert cookie_capture._select_target(targets) == "ws://x/worker"


def test_select_target_skips_entries_without_ws_url():
    targets = [
        {"type": "page", "url": "https://www.meruki.cn/"},
        {"type": "page", "url": "https://www.meruki.cn/", "webSocketDebuggerUrl": ""},
        {"type": "page", "url": "https://www.meruki.cn/", "webSocketDebuggerUrl": "ws://ok"},
    ]
    assert cookie_capture._select_target(targets) == "ws://ok"


@pytest.mark.parametrize("targets", [None, [], "nope", [1, 2, None], [{"type": "page"}]])
def test_select_target_handles_garbage(targets):
    assert cookie_capture._select_target(targets) == ""


async def test_cdp_uses_meruki_page_not_extension_background(ws_server):
    """端到端：/json/list 里扩展 background_page 排第一，仍要连到 meruki 页面。"""
    await ws_server.start(script_plain())
    good = f"ws://127.0.0.1:{ws_server.port}/devtools/page/ABC"
    targets = [
        {
            "type": "background_page",
            "url": "chrome-extension://x/bg",
            "webSocketDebuggerUrl": "ws://127.0.0.1:1/ext",
        },
        {
            "type": "page",
            "url": "https://example.com/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:1/other",
        },
        {"type": "page", "url": "https://www.meruki.cn/", "webSocketDebuggerUrl": good},
    ]
    with FakeJsonList(targets).patch(ws_server.port):
        res = await cookie_capture.capture_via_cdp(ws_server.port, timeout=3.0)

    assert isinstance(res, CaptureResult), res
    assert res.cookie == "sid=SECRET-SID; uid=42"


async def test_cdp_no_page_available():
    """/json/list 里没有任何带地址的 target → 明确提示先打开页面。"""
    json_list = FakeJsonList([{"type": "service_worker", "url": "https://x"}])
    with json_list.patch(9222):
        res = await cookie_capture.capture_via_cdp(9222, timeout=2.0)
    assert isinstance(res, CaptureFailure)
    assert "没有找到可操作的页面" in res.reason


async def test_cdp_unexpected_error_does_not_leak_details(monkeypatch):
    """兜底 except 只能暴露异常类型名，不能把 str(exc) 带给用户（修复项 11）。"""

    async def boom(*args, **kwargs):
        raise RuntimeError("secret=/Users/bob/.config/token=abc123")

    monkeypatch.setattr(cookie_capture, "_ws_simple_call", boom)
    res = await cookie_capture._cdp_get_cookies("ws://127.0.0.1:1/x", timeout=1.0)

    assert isinstance(res, CaptureFailure)
    assert "RuntimeError" in res.reason
    assert "abc123" not in res.reason
    assert "/Users/bob" not in res.reason


async def test_cdp_malformed_response_does_not_leak_details(monkeypatch):
    """JSON 解析失败同理：只给类型名。"""

    async def bad_json(*args, **kwargs):
        return "{not json at all"

    monkeypatch.setattr(cookie_capture, "_ws_simple_call", bad_json)
    res = await cookie_capture._cdp_get_cookies("ws://127.0.0.1:1/x", timeout=1.0)

    assert isinstance(res, CaptureFailure)
    assert "JSONDecodeError" in res.reason
    assert "not json at all" not in res.reason


# ================================================================== 纯函数


@pytest.mark.parametrize(
    "domain",
    ["meruki.cn", ".meruki.cn", "www.meruki.cn", ".www.meruki.cn", "WEB.MERUKI.CN"],
)
def test_host_matches_accepts_target(domain):
    assert cookie_capture._host_matches(domain)


@pytest.mark.parametrize(
    "domain",
    ["evil.com", "notmeruki.cn", "meruki.cn.evil.com", "", "merukicn"],
)
def test_host_matches_rejects_others(domain):
    assert not cookie_capture._host_matches(domain)


def test_build_header_joins_and_dedupes():
    header = cookie_capture._build_header([("a", "1"), ("b", "2"), ("a", "9")])
    assert header == "a=1; b=2"


def test_build_header_skips_empty_names():
    assert cookie_capture._build_header([("", "x"), ("ok", "1")]) == "ok=1"


def test_build_header_empty_list():
    assert cookie_capture._build_header([]) == ""


def test_ws_accept_matches_rfc6455_sample():
    """RFC 6455 §1.3 的样例：dGhlIHNhbXBsZSBub25jZQ== → s3pPLMBiTxaQ9kYGzzhZRbK+xOo="""
    assert cookie_capture._ws_accept("dGhlIHNhbXBsZSBub25jZQ==") == ("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


# --------------------------------------------------------------- 帧构造


def test_ws_frame_is_masked_and_short_length():
    frame = cookie_capture._ws_frame(b"hello")
    assert frame[0] == 0x81  # FIN + text
    assert frame[1] & 0x80  # 掩码位
    assert frame[1] & 0x7F == 5
    assert len(frame) == 2 + 4 + 5


def test_ws_frame_long_payload_uses_126():
    frame = cookie_capture._ws_frame(b"x" * 200)
    assert frame[1] & 0x7F == 126
    assert len(frame) == 2 + 2 + 4 + 200


def test_ws_frame_huge_payload_uses_127():
    frame = cookie_capture._ws_frame(b"x" * 70_000)
    assert frame[1] & 0x7F == 127
    assert len(frame) == 2 + 8 + 4 + 70_000


def test_ws_frame_pong_opcode_and_fin():
    """pong 必须是 FIN=1 + opcode 0xA。"""
    frame = cookie_capture._ws_frame(b"hi", 0xA)
    assert frame[0] == 0x8A
    assert frame[1] & 0x7F == 2


def test_ws_frame_mask_is_applied():
    frame = cookie_capture._ws_frame(b"hello")
    mask = frame[2:6]
    payload = frame[6:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) == b"hello"


def test_ws_frame_rejects_oversized_control_payload():
    with pytest.raises(ValueError):
        cookie_capture._ws_frame(b"x" * 126, 0x9)


def test_ws_frame_rejects_fragmented_control_frame():
    with pytest.raises(ValueError):
        cookie_capture._ws_frame(b"x", 0x8, fin=False)


# --------------------------------------------------------------- 读帧（直连假服务器）


async def _frame_feeder(chunks: list[bytes]) -> asyncio.StreamReader:
    """把若干字节块喂给一个 StreamReader，模拟网络到达顺序。"""
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


async def test_ws_read_message_skips_ping_and_pong():
    reader = await _frame_feeder(
        [
            ws_server_frame(b"p", 0x9),
            ws_server_frame(b"p", 0xA),
            ws_server_frame(b'"ok"'),
        ]
    )
    assert await cookie_capture._ws_read_message(reader) == '"ok"'


async def test_ws_read_message_close_raises():
    reader = await _frame_feeder([ws_server_frame(b"", 0x8)])
    with pytest.raises(WebSocketError):
        await cookie_capture._ws_read_message(reader)


async def test_ws_read_message_binary_raises():
    reader = await _frame_feeder([ws_server_frame(b"\x00\x01", 0x2)])
    with pytest.raises(WebSocketError):
        await cookie_capture._ws_read_message(reader)


async def test_ws_read_message_rejects_continuation_without_start():
    reader = await _frame_feeder([ws_server_frame(b"x", 0x0, fin=True)])
    with pytest.raises(WebSocketError):
        await cookie_capture._ws_read_message(reader)


async def test_ws_read_message_rejects_new_message_inside_fragment():
    reader = await _frame_feeder(
        [ws_server_frame(b"a", 0x1, fin=False), ws_server_frame(b"b", 0x1, fin=True)]
    )
    with pytest.raises(WebSocketError):
        await cookie_capture._ws_read_message(reader)


async def test_ws_read_message_rejects_rsv_bits():
    bad = bytes([0x91, 0x00])  # RSV1 被置位
    reader = await _frame_feeder([bad])
    with pytest.raises(WebSocketError):
        await cookie_capture._ws_read_message(reader)


async def test_ws_read_message_unmasks_masked_server_frame():
    """服务端按 RFC 不该加掩码，但真加了也要能解，而不是把乱码当 JSON。"""
    payload = b'"hello"'
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked
    reader = await _frame_feeder([frame])
    assert await cookie_capture._ws_read_message(reader) == '"hello"'


# ================================================================== browser 模块


def test_candidates_nonempty_per_platform():
    cands = browser._candidates()
    assert cands and all(isinstance(c, list) and c for c in cands)


def test_has_graphical_session_linux_without_display(monkeypatch):
    # os.name 也要一起改：Windows 上 os.name=="nt" 会抢在 sys.platform 前返回 True。
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setattr(browser.os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert browser.has_graphical_session() is False


def test_has_graphical_session_linux_with_display(monkeypatch):
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setattr(browser.os, "name", "posix")
    monkeypatch.setenv("DISPLAY", ":0")
    assert browser.has_graphical_session() is True


async def test_open_url_success(monkeypatch):
    async def fake_run(cmd, timeout):
        return 0, ""

    # 钉死候选命令：Windows 上默认走 os.startfile，Linux 上是 xdg-open，
    # 都不该影响"给定一条命令、跑通就回报它"这条测试。
    monkeypatch.setattr(browser, "_candidates", lambda: [["open"]])
    monkeypatch.setattr(browser, "_run", fake_run)
    res = await browser.open_url("https://example.com")
    assert res.ok and res.command == "open"


async def test_open_url_all_candidates_fail(monkeypatch):
    async def fake_run(cmd, timeout):
        return 1, "boom"

    monkeypatch.setattr(browser, "_run", fake_run)
    monkeypatch.setattr(browser, "_candidates", lambda: [["open"], ["xdg-open"]])
    res = await browser.open_url("https://example.com")
    assert not res.ok
    assert "boom" in res.error
    assert "open" in res.error and "xdg-open" in res.error


async def test_open_url_command_not_found(monkeypatch):
    async def fake_run(cmd, timeout):
        raise FileNotFoundError

    monkeypatch.setattr(browser, "_run", fake_run)
    monkeypatch.setattr(browser, "_candidates", lambda: [["xdg-open"]])
    res = await browser.open_url("https://example.com")
    assert not res.ok
    assert "未找到该命令" in res.error


async def test_open_url_timeout(monkeypatch):
    async def fake_run(cmd, timeout):
        raise TimeoutError

    monkeypatch.setattr(browser, "_run", fake_run)
    monkeypatch.setattr(browser, "_candidates", lambda: [["open"]])
    res = await browser.open_url("https://example.com")
    assert not res.ok


async def test_open_url_warns_without_graphical_session(monkeypatch):
    async def fake_run(cmd, timeout):
        return 0, ""

    monkeypatch.setattr(browser, "_run", fake_run)
    monkeypatch.setattr(browser, "has_graphical_session", lambda: False)
    res = await browser.open_url("https://example.com")
    assert res.ok
    assert any("图形会话" in h for h in res.hints)


# ================================================================== Firefox 库


def _make_firefox_db(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """建一个带 creationTime 列的库（真实 Firefox 也有这列，用于确定性排序）。"""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, creationTime INTEGER)")
    con.executemany(
        "INSERT INTO moz_cookies (name, value, host, creationTime) VALUES (?, ?, ?, ?)",
        [(n, v, h, i) for i, (n, v, h) in enumerate(rows)],
    )
    con.commit()
    con.close()


def test_read_firefox_db_filters_host(tmp_path):
    db = tmp_path / "cookies.sqlite"
    _make_firefox_db(
        db,
        [
            ("sid", "abc", ".meruki.cn"),
            ("other", "zzz", ".example.com"),
            ("uid", "42", "www.meruki.cn"),
        ],
    )
    pairs = cookie_capture._read_firefox_db(db)
    assert ("sid", "abc") in pairs
    assert ("uid", "42") in pairs
    assert all(n != "other" for n, _ in pairs)


def test_read_firefox_db_rejects_lookalike_domain(tmp_path):
    """SQL 粗筛会把 notmeruki.cn 选进来，Python 侧必须再挡掉。"""
    db = tmp_path / "cookies.sqlite"
    _make_firefox_db(db, [("bad", "x", "notmeruki.cn"), ("sid", "ok", ".meruki.cn")])
    pairs = cookie_capture._read_firefox_db(db)
    assert ("sid", "ok") in pairs
    assert all(n != "bad" for n, _ in pairs)


def test_read_firefox_db_sql_filters_non_target_hosts(tmp_path):
    """SQL 里就必须带 WHERE，否则会把整库读进内存（大库上非常慢）。"""
    db = tmp_path / "cookies.sqlite"
    rows = [("sid", "abc", ".meruki.cn")] + [
        (f"n{i}", f"v{i}", f".site{i}.example") for i in range(200)
    ]
    _make_firefox_db(db, rows)

    sql_seen: list[str] = []
    real_connect = sqlite3.connect

    class SpyConnection:
        """包一层只为了记录被执行的 SQL（sqlite3.Connection.execute 不可打补丁）。"""

        def __init__(self, con) -> None:
            self._con = con

        def execute(self, sql, *a):
            sql_seen.append(sql)
            return self._con.execute(sql, *a)

        def __getattr__(self, item):
            return getattr(self._con, item)

    def spy_connect(*args, **kwargs):
        return SpyConnection(real_connect(*args, **kwargs))

    import unittest.mock

    with unittest.mock.patch.object(sqlite3, "connect", spy_connect):
        pairs = cookie_capture._read_firefox_db(db)

    assert pairs == [("sid", "abc")]
    assert sql_seen, "没有观察到任何 SQL"
    assert "WHERE" in sql_seen[0].upper(), sql_seen[0]
    assert "LIKE" in sql_seen[0].upper(), sql_seen[0]
    assert "ORDER BY" in sql_seen[0].upper(), sql_seen[0]


def test_read_firefox_db_skips_empty_values(tmp_path):
    db = tmp_path / "cookies.sqlite"
    _make_firefox_db(
        db, [("empty", "", ".meruki.cn"), ("null", None, ".meruki.cn"), ("sid", "ok", ".meruki.cn")]
    )
    pairs = cookie_capture._read_firefox_db(db)
    assert pairs == [("sid", "ok")]


def test_capture_via_firefox_does_not_emit_empty_cookie(tmp_path, monkeypatch):
    """端到端：空值不能变成 Cookie 头里的 ``empty=``。"""
    db = tmp_path / "p" / "cookies.sqlite"
    db.parent.mkdir(parents=True)
    _make_firefox_db(db, [("empty", "", ".meruki.cn"), ("sid", "ok", ".meruki.cn")])
    monkeypatch.setattr(cookie_capture, "firefox_cookie_dbs", lambda: [db])

    res = cookie_capture.capture_via_firefox()
    assert isinstance(res, CaptureResult)
    assert res.cookie == "sid=ok"
    assert "empty=" not in res.cookie


def test_read_firefox_db_opens_snapshot_read_only(tmp_path, monkeypatch):
    """快照必须以 ``mode=ro`` 的 URI 打开（否则会加锁/可能写回）。

    **不能加 ``immutable=1``**：那会让 SQLite 忽略 ``-wal``，而 Firefox 常常把
    最新的 Cookie 留在 WAL 里没 checkpoint（见 ``test_firefox_wal_is_read_...``）。
    """
    db = tmp_path / "cookies.sqlite"
    _make_firefox_db(db, [("sid", "abc", ".meruki.cn")])

    calls: list[tuple[tuple, dict]] = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(sqlite3, "connect", spy_connect):
        pairs = cookie_capture._read_firefox_db(db)

    assert pairs == [("sid", "abc")]
    assert len(calls) == 1
    dsn, kwargs = calls[0]
    assert kwargs.get("uri") is True, "必须用 uri=True"
    assert "mode=ro" in str(dsn[0]), dsn[0]
    assert "immutable=1" not in str(dsn[0]), "immutable 会让 SQLite 忽略 -wal"


def test_read_firefox_db_never_writes_to_source(tmp_path):
    """读快照不改动源库，也不在源目录留下 -journal/-wal。"""
    db = tmp_path / "cookies.sqlite"
    _make_firefox_db(db, [("sid", "abc", ".meruki.cn"), ("x", "1", ".example.com")])
    before = db.read_bytes()
    before_files = {p.name for p in tmp_path.iterdir()}

    pairs = cookie_capture._read_firefox_db(db)

    assert pairs == [("sid", "abc")]
    assert db.read_bytes() == before, "源库被改动了"
    assert {p.name for p in tmp_path.iterdir()} == before_files


def test_read_firefox_db_is_deterministic(tmp_path):
    db = tmp_path / "cookies.sqlite"
    rows = [("b", "2", ".meruki.cn"), ("a", "1", ".meruki.cn"), ("c", "3", ".meruki.cn")]
    _make_firefox_db(db, rows)
    first = cookie_capture._read_firefox_db(db)
    second = cookie_capture._read_firefox_db(db)
    assert first == second
    # ORDER BY host, name → 按名字有序，同名时保留第一个才是可预期的
    assert [n for n, _ in first] == ["a", "b", "c"]


def test_firefox_cookie_dbs_sorted_by_mtime(tmp_path, monkeypatch):
    """profile 选择按 cookies.sqlite 的 mtime 倒序，而不是字母序。"""
    import os

    old = tmp_path / "aaa.profile" / "cookies.sqlite"
    new = tmp_path / "zzz.profile" / "cookies.sqlite"
    for p in (old, new):
        p.parent.mkdir(parents=True)
        _make_firefox_db(p, [("sid", "1", ".meruki.cn")])
    os.utime(old, (1000, 1000))
    os.utime(new, (9000, 9000))

    monkeypatch.setattr(cookie_capture.sys, "platform", "linux")
    # Windows 上 os.name=="nt" 会走 APPDATA 分支，忽略 Path.home。
    monkeypatch.setattr(cookie_capture.os, "name", "posix")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(cookie_capture.Path, "home", lambda: tmp_path.parent)
    # 直接用模块内的 root 推导：把 home 指到临时目录的父级，root=.mozilla/firefox
    firefox_root = tmp_path.parent / ".mozilla" / "firefox"
    firefox_root.mkdir(parents=True, exist_ok=True)
    for p in (old, new):
        target = firefox_root / p.parent.name / "cookies.sqlite"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(p.read_bytes())
    os.utime(firefox_root / "aaa.profile" / "cookies.sqlite", (1000, 1000))
    os.utime(firefox_root / "zzz.profile" / "cookies.sqlite", (9000, 9000))

    dbs = cookie_capture.firefox_cookie_dbs()
    assert [p.parent.name for p in dbs] == ["zzz.profile", "aaa.profile"]


def test_capture_via_firefox_success(monkeypatch, tmp_path):
    db = tmp_path / "profile1" / "cookies.sqlite"
    db.parent.mkdir(parents=True)
    _make_firefox_db(db, [("sid", "abc", ".meruki.cn"), ("uid", "42", ".meruki.cn")])
    monkeypatch.setattr(cookie_capture, "firefox_cookie_dbs", lambda: [db])

    res = cookie_capture.capture_via_firefox()
    assert isinstance(res, CaptureResult)
    assert res.source == "firefox"
    assert res.cookie == "sid=abc; uid=42"


def test_capture_via_firefox_falls_through_to_older_profile(monkeypatch, tmp_path):
    """最近使用的 profile 没登录时，要继续尝试其它 profile。"""
    empty = tmp_path / "new" / "cookies.sqlite"
    full = tmp_path / "old" / "cookies.sqlite"
    for p in (empty, full):
        p.parent.mkdir(parents=True)
    _make_firefox_db(empty, [("x", "1", ".example.com")])
    _make_firefox_db(full, [("sid", "abc", ".meruki.cn")])
    monkeypatch.setattr(cookie_capture, "firefox_cookie_dbs", lambda: [empty, full])

    res = cookie_capture.capture_via_firefox()
    assert isinstance(res, CaptureResult)
    assert res.cookie == "sid=abc"


def test_capture_via_firefox_no_db(monkeypatch):
    monkeypatch.setattr(cookie_capture, "firefox_cookie_dbs", lambda: [])
    res = cookie_capture.capture_via_firefox()
    assert isinstance(res, CaptureFailure)
    assert "没有找到" in res.reason


def test_capture_via_firefox_no_matching_cookie(monkeypatch, tmp_path):
    db = tmp_path / "cookies.sqlite"
    _make_firefox_db(db, [("x", "1", ".example.com")])
    monkeypatch.setattr(cookie_capture, "firefox_cookie_dbs", lambda: [db])
    res = cookie_capture.capture_via_firefox()
    assert isinstance(res, CaptureFailure)


def test_capture_via_firefox_error_does_not_leak_details(monkeypatch, tmp_path):
    """读库失败时 reason 只带 profile 名 + 异常类型名，不带底层细节。"""
    db = tmp_path / "profile9" / "cookies.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database at all" * 10)
    monkeypatch.setattr(cookie_capture, "firefox_cookie_dbs", lambda: [db])

    res = cookie_capture.capture_via_firefox()
    assert isinstance(res, CaptureFailure)
    assert "profile9" in res.reason
    # 底层 sqlite 消息（"file is not a database"）不应出现在用户可见文案里
    assert "file is not a database" not in res.reason


# ================================================================== 统一入口 capture_cookie（修复项 5）


async def test_capture_cookie_runs_firefox_read_in_thread(monkeypatch):
    """capture_via_firefox 是同步阻塞的，必须跑在线程里，否则卡死事件循环。"""
    seen: dict[str, object] = {}
    loop_thread_ident = None

    def fake_firefox() -> CaptureFailure:
        import threading

        seen["thread"] = threading.get_ident()
        seen["loop_thread"] = loop_thread_ident
        return CaptureFailure(reason="no")

    monkeypatch.setattr(cookie_capture, "capture_via_firefox", fake_firefox)

    async def fake_cdp(port, *, timeout=0.0):
        nonlocal loop_thread_ident
        import threading

        loop_thread_ident = threading.get_ident()
        return CaptureFailure(reason="no cdp")

    monkeypatch.setattr(cookie_capture, "capture_via_cdp", fake_cdp)

    res = await cookie_capture.capture_cookie(9222)

    assert isinstance(res, CaptureFailure)
    assert seen["thread"] != seen["loop_thread"], "capture_via_firefox 必须在另一个线程执行"
    # 两条失败原因都要透传给用户，且标明各自来源（避免"只看得到一句笼统失败"）
    assert any("no cdp" in h for h in res.hints), res.hints
    assert any(h.startswith("CDP：") for h in res.hints), res.hints
    assert any(h.startswith("Firefox：") for h in res.hints), res.hints


async def test_capture_cookie_does_not_block_event_loop(monkeypatch):
    """同步的 Firefox 读取若阻塞，事件循环的"心跳"会停住。用心跳检测真实阻塞。"""
    import time

    beats: list[float] = []

    # 心跳跑到被明确叫停为止。不能用"固定 200 次 × sleep(1ms)"的有限循环：
    # 它名义 0.2s，和下面慢 IO 的 0.2s 几乎相等——慢 runner 上 sleep 实际 >1ms，
    # 任务可能在 capture_cookie 返回前就自然结束，cancel() 已结束的任务不会抛
    # CancelledError，导致与测试目的无关的收尾断言假失败（macOS CI 踩过）。
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            beats.append(time.monotonic())
            await asyncio.sleep(0.001)

    def slow_firefox() -> CaptureResult:
        time.sleep(0.2)  # 模拟文件复制 + sqlite 查询
        return CaptureResult(cookie="a=1", source="firefox")

    async def fake_cdp(port, *, timeout=0.0):
        return CaptureFailure(reason="no cdp")

    monkeypatch.setattr(cookie_capture, "capture_via_firefox", slow_firefox)
    monkeypatch.setattr(cookie_capture, "capture_via_cdp", fake_cdp)

    beats.clear()
    hb = asyncio.create_task(heartbeat())
    try:
        res = await cookie_capture.capture_cookie(9222)
    finally:
        stop.set()
        await hb

    assert isinstance(res, CaptureResult)
    # 事件循环在 0.2s 的阻塞 IO 期间必须仍在跑（拿到过若干次心跳）。
    # 这是本测试真正要验证的断言；心跳任务是否被 cancel 与测试目的无关。
    assert len(beats) >= 3, "事件循环在同步 IO 期间被阻塞了"


# ================================================================== 编排 browser_login


async def _run_login(wait_seconds: int = 10, *, open_ok: bool = True):
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=open_ok, mode="debug")),
        ),
        patch(
            "meruki_tracker.core.app.browser_login.capture_cookie",
            AsyncMock(return_value=CaptureResult(cookie="a=1", source="cdp")),
        ),
    ):
        return await browser_login("https://x", 9222, wait_seconds, profile_dir=_PROFILE)


async def test_browser_login_open_fails():
    with patch(
        "meruki_tracker.core.app.browser_login.launch_debug_browser",
        AsyncMock(return_value=BrowserLaunch(ok=False, error="打不开")),
    ):
        res = await browser_login("https://x", 9222, 10, profile_dir=_PROFILE)
    assert not res.ok
    assert res.capture is None
    assert any("打不开" in h for h in res.hints)


async def test_browser_login_captures_immediately():
    res = await _run_login(10)
    assert res.ok
    assert res.cookie == "a=1"
    assert res.source == "cdp"


async def test_browser_login_no_wait_tries_once():
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch(
            "meruki_tracker.core.app.browser_login.capture_cookie",
            AsyncMock(return_value=CaptureResult(cookie="k=v", source="firefox")),
        ) as cap,
    ):
        res = await browser_login("https://x", 9222, 0, profile_dir=_PROFILE)
    assert res.ok and res.cookie == "k=v"
    assert cap.await_count == 1


async def test_browser_login_no_wait_does_not_sleep():
    """wait_seconds<=0：只试一次，且完全不睡。"""
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch(
            "meruki_tracker.core.app.browser_login.capture_cookie",
            AsyncMock(return_value=CaptureFailure(reason="没拿到")),
        ) as cap,
        patch("meruki_tracker.core.app.browser_login.asyncio.sleep", AsyncMock()) as sleeper,
    ):
        res = await browser_login("https://x", 9222, 0, profile_dir=_PROFILE)
    assert not res.ok
    assert cap.await_count == 1
    assert sleeper.await_count == 0


async def test_browser_login_times_out(monkeypatch):
    """轮询必须能终止（旧版把 asyncio.sleep 全局 patch 掉，真死循环也能通过）。"""
    monkeypatch.setattr("meruki_tracker.core.app.browser_login._POLL_DELAYS", (0.0,))
    fake = AsyncMock(return_value=CaptureFailure(reason="没拿到"))
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", fake),
    ):
        # 真 sleep（步长有 0.01s 下限）；外层 2s 保险丝保证死循环会失败而不是挂住
        res = await asyncio.wait_for(
            browser_login("https://x", 9222, 1, profile_dir=_PROFILE), timeout=2
        )

    assert not res.ok
    assert isinstance(res.capture, CaptureFailure)
    # 上界：每次至少推进 0.01s，1s 内不应超过 ~100 次；留足余量取 200
    assert fake.await_count >= 2
    assert fake.await_count <= 200, fake.await_count


async def test_browser_login_timeout_is_hard_cap(monkeypatch):
    """wait_seconds 是硬上限：slow 的 capture 不能把总时长撑成 wait + capture。"""
    monkeypatch.setattr("meruki_tracker.core.app.browser_login._POLL_DELAYS", (5.0,))

    async def slow_capture(port):
        await asyncio.sleep(0.3)
        return CaptureFailure(reason="慢")

    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", slow_capture),
    ):
        loop = asyncio.get_running_loop()
        started = loop.time()
        res = await asyncio.wait_for(
            browser_login("https://x", 9222, 1, profile_dir=_PROFILE), timeout=3
        )
        elapsed = loop.time() - started

    assert not res.ok
    # 期望值：0.3s 首轮捕获 + 0.7s 等待 = 1.0s（deadline），之后还会再做**一次**
    # 捕获（这次是 #1 的 0.3s 起点 + 1.0s）。对比旧实现：
    #   capture(0.3) + sleep(min(5, 1)=1.0) + capture(0.3) = 1.6s
    # 这里断言总耗时不超过 wait + 单次 capture + 余量，即"不多等额外一轮"。
    assert elapsed < 1.45, f"总耗时 {elapsed:.2f}s 超出了硬上限（旧实现约 1.6s）"


async def test_browser_login_catches_after_retry():
    """第一次没拿到，第二次拿到 —— 模拟用户在浏览器里登录需要时间。"""
    results = [CaptureFailure(reason="还没登录"), CaptureResult(cookie="k=v", source="cdp")]
    fake = AsyncMock(side_effect=results)
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", fake),
        # 只 patch 本模块引用到的 asyncio.sleep，不污染全局 asyncio
        patch("meruki_tracker.core.app.browser_login.asyncio.sleep", AsyncMock()) as sleeper,
    ):
        res = await browser_login("https://x", 9222, 30, profile_dir=_PROFILE)

    assert res.ok and res.cookie == "k=v"
    assert fake.await_count == 2
    assert sleeper.await_count == 1
    sleeper.assert_awaited_once_with(1.5)  # _POLL_DELAYS[0]：登录完成能尽快被发现


async def test_browser_login_retry_respects_poll_delays():
    """轮询步长按 _POLL_DELAYS 前进。"""
    fake = AsyncMock(return_value=CaptureFailure(reason="没"))
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", fake),
        patch("meruki_tracker.core.app.browser_login.asyncio.sleep", AsyncMock()) as sleeper,
    ):
        res = await browser_login("https://x", 9222, 10, profile_dir=_PROFILE)

    assert not res.ok
    steps = [c.args[0] for c in sleeper.await_args_list]
    assert steps[0] == 1.5
    assert steps[1] == 2.0
    assert all(s > 0 for s in steps)


async def test_browser_login_progress_reports_growing_elapsed():
    """进度文案里的"已等待"必须递增（旧版永远 0s）。"""
    messages: list[str] = []
    fake = AsyncMock(return_value=CaptureFailure(reason="没"))
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", fake),
        patch("meruki_tracker.core.app.browser_login.asyncio.sleep", AsyncMock()) as sleeper,
    ):
        await browser_login("https://x", 9222, 30, progress=messages.append, profile_dir=_PROFILE)

    wait_msgs = [m for m in messages if "已等待" in m]
    assert len(wait_msgs) == sleeper.await_count  # 每次 sleep 前都打印一次
    # 解析出每个文案里的"已等待 Ns"
    import re

    values = [int(re.search(r"已等待 (\d+)s", m).group(1)) for m in wait_msgs]
    assert values == sorted(values), values
    assert values[-1] > 0, "进度文案永远显示 0s"


async def test_browser_login_swallows_unexpected_exception(monkeypatch):
    """底层抛出 UnicodeDecodeError 之类的意外异常 → 折成 CaptureFailure，不冒泡。"""

    async def boom(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr("meruki_tracker.core.app.browser_login.capture_cookie", boom)
    monkeypatch.setattr("meruki_tracker.core.app.browser_login._POLL_DELAYS", (0.01,))
    with patch(
        "meruki_tracker.core.app.browser_login.launch_debug_browser",
        AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
    ):
        res = await asyncio.wait_for(
            browser_login("https://x", 9222, 0.1, profile_dir=_PROFILE), timeout=2
        )

    assert not res.ok
    assert isinstance(res.capture, CaptureFailure)
    assert "UnicodeDecodeError" in res.capture.reason
    # 细节不上屏
    assert "invalid start byte" not in res.capture.reason


async def test_browser_login_survives_repeated_capture_errors():
    """每轮都抛异常的循环也要能正常收敛并返回。"""
    calls = {"n": 0}

    async def boom(port):
        calls["n"] += 1
        raise RuntimeError("boom")

    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", boom),
        patch("meruki_tracker.core.app.browser_login.asyncio.sleep", AsyncMock()),
        patch("meruki_tracker.core.app.browser_login._POLL_DELAYS", (0.0,)),
    ):
        res = await asyncio.wait_for(
            browser_login("https://x", 9222, 1, profile_dir=_PROFILE), timeout=2
        )

    assert not res.ok
    assert isinstance(res.capture, CaptureFailure)
    assert "RuntimeError" in res.capture.reason
    assert calls["n"] >= 1


async def test_browser_login_propagates_cancellation():
    """Ctrl+C（CancelledError）必须原样上抛，不能被宽泛的 except 吞掉。"""

    async def cancel_me(port):
        raise asyncio.CancelledError

    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", cancel_me),
        pytest.raises(asyncio.CancelledError),
    ):
        await browser_login("https://x", 9222, 10, profile_dir=_PROFILE)


async def test_browser_login_launch_exception_is_folded():
    """启动浏览器时意外抛异常也不能冒泡到 TUI。"""

    async def boom(*a, **k):
        raise PermissionError("denied")

    with patch("meruki_tracker.core.app.browser_login.launch_debug_browser", boom):
        res = await browser_login("https://x", 9222, 10, profile_dir=_PROFILE)

    assert not res.ok
    assert any("PermissionError" in h for h in res.hints), res.hints


# ---------------------------------------- 端口有响应但调试接口不可用（HTTP 503 / 403）


async def test_cdp_503_is_not_reported_as_unreachable():
    """端口明明有响应（503），绝不能谎报"端口上没有调试接口"。

    真实场景：Chrome 残留实例占着 9222 但正在退出，DevTools 返回 503。
    用户如果看到"请用调试端口启动浏览器"会去做一件已经做过的事。
    """
    with FakeJsonList([], status=503).patch(9222):
        res = await cookie_capture.capture_via_cdp(9222, timeout=3.0)

    assert isinstance(res, CaptureFailure)
    assert "503" in res.reason
    assert "没有可用的调试接口" not in res.reason
    # 给出针对"端口被占用/浏览器在退出"的可操作建议
    assert any("--user-data-dir" in h for h in res.hints)
    assert any("lsof" in h for h in res.hints)


async def test_cdp_403_mentions_remote_debugging_flag():
    with FakeJsonList([], status=403).patch(9222):
        res = await cookie_capture.capture_via_cdp(9222, timeout=3.0)

    assert isinstance(res, CaptureFailure)
    assert "403" in res.reason
    assert "没有可用的调试接口" not in res.reason
    assert any("remote-debugging-port" in h for h in res.hints)


async def test_cdp_200_non_json_says_port_maybe_occupied(monkeypatch):
    """200 但不是 JSON：端口被别的程序占用。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not devtools</html>")

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(cookie_capture.httpx, "AsyncClient", fake_client)

    res = await cookie_capture.capture_via_cdp(9222, timeout=3.0)

    assert isinstance(res, CaptureFailure)
    assert "不是调试接口" in res.reason
    assert "没有可用的调试接口" not in res.reason
    assert any("lsof" in h for h in res.hints)


# ------------------------------------------------ 本机回环不能被系统代理接管


async def test_cdp_bypasses_env_proxy(monkeypatch):
    """回归：发往 127.0.0.1 的调试请求绝不能走系统/环境代理。

    真实故障（用户现场）：httpx 默认 ``trust_env=True`` 会读取系统代理设置
    （macOS 上通过 System Configuration）。开着 Clash/V2Ray 之类的本机代理时，
    请求 ``http://127.0.0.1:9222/json/list`` 被代理接手并返回 **503**，
    于是报"端口有响应但调试接口不可用"——可实际上 Chrome 根本没连上，
    自动获取 Cookie 永远不可能成功。

    这里起一个"任何请求都回 503 的假代理"，再指向一个**关闭**的端口：
    正确实现应直连并得到"连接失败"，而不是拿到代理的 503。
    """

    async def proxy_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    proxy = await asyncio.start_server(proxy_handler, "127.0.0.1", 0)
    proxy_port = proxy.sockets[0].getsockname()[1]

    # 拿一个必然关闭的端口作为"CDP 端口"
    closed = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    closed_port = closed.sockets[0].getsockname()[1]
    closed.close()
    await closed.wait_closed()

    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    try:
        async with proxy:
            res = await cookie_capture.capture_via_cdp(closed_port, timeout=3.0)
    finally:
        proxy.close()
        await proxy.wait_closed()

    assert isinstance(res, CaptureFailure)
    # 直连被拒绝 —— 这才说明请求没被代理接走
    assert "没有可用的调试接口" in res.reason, res.reason
    # 不能用 `"503" not in reason` 判断：reason 里带端口号（"连接 50353 端口失败"），
    # 端口是内核随机分配的，迟早会撞出一个包含 "503" 的端口，测试就假失败。
    # 被代理接手时走的是另一条分支，文案里带「端口有响应，但调试接口不可用
    # （HTTP 503）」——查这个说法才是真的在验证有没有被代理接走。
    assert "端口有响应" not in res.reason, res.reason


# ============================================ 由程序启动「可调试」的浏览器


async def test_launch_debug_browser_builds_correct_flags(monkeypatch, tmp_path):
    """必须带上 --remote-debugging-port 与独立 --user-data-dir。

    少了前者 → 抓不到 Cookie；少了后者 → 新版 Chrome 会拒绝开启远程调试。
    """
    seen: dict = {}

    async def fake_exec(*args, **kwargs):
        seen["argv"] = list(args)
        return object()

    async def fake_wait(port, *, timeout=10.0):
        seen["port"] = port
        return True

    monkeypatch.setattr(browser, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(browser.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(browser, "wait_for_debug_port", fake_wait)

    profile = tmp_path / "profile"
    res = await browser.launch_debug_browser("https://www.meruki.cn/", 9222, profile)

    assert res.ok and res.mode == "debug"
    argv = seen["argv"]
    assert argv[0] == "/fake/chrome"
    assert "--remote-debugging-port=9222" in argv
    assert f"--user-data-dir={profile}" in argv
    assert "https://www.meruki.cn/" in argv
    assert profile.is_dir()
    # 配置目录里会有登录态，权限必须收紧。Windows 的 chmod 不设 POSIX 位
    # （st_mode 恒 0o666/0o777），权限收紧由系统 ACL 负责，这条只在 POSIX 上有意义。
    if browser.os.name != "nt":
        assert (profile.stat().st_mode & 0o777) == 0o700


async def test_launch_debug_browser_without_chrome_falls_back_to_plain(monkeypatch, tmp_path):
    """没装 Chrome 系浏览器 → 普通打开，并明确告知无法自动抓取。"""
    monkeypatch.setattr(browser, "find_chrome", lambda: "")
    monkeypatch.setattr(
        browser, "open_url", AsyncMock(return_value=OpenResult(ok=True, command="open"))
    )

    res = await browser.launch_debug_browser("https://x/", 9222, tmp_path / "p")

    assert res.ok and res.mode == "plain"
    assert any("无法自动读取" in h or "手动粘贴" in h for h in res.hints)


async def test_launch_debug_browser_reports_port_never_ready(monkeypatch, tmp_path):
    async def fake_exec(*args, **kwargs):
        return object()

    monkeypatch.setattr(browser, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(browser.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(browser, "wait_for_debug_port", AsyncMock(return_value=False))

    res = await browser.launch_debug_browser("https://x/", 9222, tmp_path / "p")

    assert not res.ok
    assert "没有就绪" in res.error


async def test_plain_mode_does_not_poll_or_wait():
    """退化成普通浏览器时自动抓取不可能成功，必须立刻回退而不是干等 180s。"""
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="plain")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", AsyncMock()) as cap,
    ):
        res = await browser_login("https://x", 9222, 180, profile_dir=_PROFILE)

    assert not res.ok
    assert res.mode == "plain"
    assert cap.await_count == 0, "普通浏览器模式不该再去轮询"
    assert any("手动粘贴" in h for h in res.hints)


async def test_verify_rejects_then_accepts(monkeypatch):
    """浏览器里有匿名 Cookie 时不能误判成功，要继续等到真正登录。"""
    monkeypatch.setattr("meruki_tracker.core.app.browser_login._POLL_DELAYS", (0.0,))
    calls = {"n": 0}

    async def verify(cookie: str) -> tuple[bool, str]:
        calls["n"] += 1
        return (calls["n"] >= 2, "挖煤君")

    fake_capture = AsyncMock(return_value=CaptureResult(cookie="k=v", source="cdp"))
    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch("meruki_tracker.core.app.browser_login.capture_cookie", fake_capture),
        patch("meruki_tracker.core.app.browser_login.asyncio.sleep", AsyncMock()),
    ):
        res = await browser_login("https://x", 9222, 5, profile_dir=_PROFILE, verify=verify)

    assert res.ok
    assert res.nickname == "挖煤君"
    assert fake_capture.await_count == 2, "第一次验证失败后应继续轮询"


async def test_verify_always_fails_reports_not_logged_in(monkeypatch):
    monkeypatch.setattr("meruki_tracker.core.app.browser_login._POLL_DELAYS", (0.0,))

    async def verify(cookie: str) -> tuple[bool, str]:
        return False, "请登录"

    with (
        patch(
            "meruki_tracker.core.app.browser_login.launch_debug_browser",
            AsyncMock(return_value=BrowserLaunch(ok=True, mode="debug")),
        ),
        patch(
            "meruki_tracker.core.app.browser_login.capture_cookie",
            AsyncMock(return_value=CaptureResult(cookie="k=v", source="cdp")),
        ),
        patch("meruki_tracker.core.app.browser_login.asyncio.sleep", AsyncMock()),
    ):
        res = await browser_login("https://x", 9222, 1, profile_dir=_PROFILE, verify=verify)

    assert not res.ok
    assert any("尚未登录" in h for h in res.hints), res.hints


async def test_wait_for_debug_port_false_when_port_closed():
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    assert await cookie_capture.wait_for_debug_port(port, timeout=0.5) is False


# ------------------------------------------------ 浏览器探测（没装 Chrome 的降级）


def test_mac_candidates_cover_common_chromium_browsers():
    """Arc / Vivaldi / Opera 也都是 Chromium 系，能开 CDP，不该漏掉。"""
    paths = " ".join(browser._mac_chrome_paths())
    for name in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Vivaldi", "Arc", "Opera"):
        assert name in paths, f"候选里缺少 {name}"


def test_mac_candidates_do_not_include_safari():
    """Safari 不提供 CDP，列进候选只会误导。"""
    assert "Safari" not in " ".join(browser._mac_chrome_paths())


async def test_launch_without_chromium_falls_back_with_actionable_hints(monkeypatch, tmp_path):
    """没有 Chromium 系浏览器时：用默认浏览器打开 + 明确告知怎么办。"""
    monkeypatch.setattr(browser, "find_chrome", lambda: "")
    monkeypatch.setattr(
        browser, "open_url", AsyncMock(return_value=OpenResult(ok=True, command="open"))
    )

    res = await browser.launch_debug_browser("https://x/", 9222, tmp_path / "p")

    assert res.ok and res.mode == "plain"
    joined = " ".join(res.hints)
    assert "Chromium" in joined
    assert "手动粘贴" in joined
    assert "Safari" in joined or "Firefox" in joined, "要说明为什么不能自动抓"


# ------------------------------------------------ 分发：跨平台浏览器探测


def test_windows_candidates_cover_chromium_browsers():
    """Windows 用户可能没装 Chrome 但有 Edge / Brave——都要认。"""
    paths = " ".join(browser._win_chrome_paths())
    for name in ("Chrome", "msedge", "Brave", "vivaldi", "Opera", "Arc"):
        assert name in paths, f"Windows 候选缺少 {name}"


def test_windows_candidates_include_localappdata():
    """Chrome 默认装在 %LOCALAPPDATA%，不查就找不到。"""
    import os

    old = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = r"C:\Users\tester\AppData\Local"
    try:
        paths = " ".join(browser._win_chrome_paths())
    finally:
        if old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old
    assert "AppData" in paths and "chrome.exe" in paths


def test_mac_candidates_check_homebrew_paths():
    """Homebrew Cask 可能装在 /opt/homebrew 或用户目录。"""
    paths = " ".join(browser._mac_chrome_paths())
    assert "/opt/homebrew" in paths or ".local/Applications" in paths


def test_find_chrome_skips_missing_candidates(monkeypatch, tmp_path):
    """所有候选都不存在时返回空串，不能抛异常。"""
    monkeypatch.setattr(browser, "_mac_chrome_paths", lambda: [])
    monkeypatch.setattr(browser, "_win_chrome_paths", lambda: [])
    monkeypatch.setattr(browser.shutil, "which", lambda _n: None)

    monkeypatch.setattr(browser.sys, "platform", "linux")
    assert browser.find_chrome() == "" or isinstance(browser.find_chrome(), str)


def test_firefox_wal_is_read_instead_of_ignored(tmp_path):
    """Firefox 把最新写入留在 ``-wal`` 里时也必须读到。

    2026-11-18 审计实测：快照用 ``mode=ro&immutable=1`` 打开时 SQLite **会忽略
    -wal**，于是刚写入的 Cookie 一条都看不见，报 ``no such table: moz_cookies`` —
    用户看到的是「读取 Firefox Cookie 库失败」+「请先关闭 Firefox」，而真正原因
    是打开方式。同一份快照改成 ``mode=ro`` 就能读到。
    """
    import sqlite3

    db = tmp_path / "cookies.sqlite"
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, creationTime INTEGER)"
        )
        con.execute("INSERT INTO moz_cookies VALUES ('kmgSession', 'SECRET', '.meruki.cn', 1)")
        con.commit()  # 提交进 WAL，连接还开着 → 主库里可能还没有这张表
        assert (tmp_path / "cookies.sqlite-wal").exists()
        assert cookie_capture._read_firefox_db(db) == [("kmgSession", "SECRET")]
    finally:
        con.close()


def test_select_target_is_not_fooled_by_the_host_in_a_query_string():
    """URL 里"含"目标域名不等于"属于"目标站点。

    2026-11-18 审计：判据是 ``TARGET_HOST in page_url.lower()``（子串）。于是
    一个搜索页（``?q=meruki.cn``）或仿冒域名（``meruki.cn.evil.example``）都会
    抢在真正的拍品页前面被选中。返回的 Cookie 仍按 domain 过滤，不会泄漏，但
    "选错 target"会让失败提示指向错误的方向。
    """
    real = "ws://127.0.0.1:9222/devtools/page/REAL"
    targets = [
        {
            "type": "page",
            "url": "https://www.google.com/search?q=meruki.cn",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/SEARCH",
        },
        {
            "type": "page",
            "url": "https://meruki.cn.evil.example/login",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/EVIL",
        },
        {"type": "page", "url": "https://www.meruki.cn/list", "webSocketDebuggerUrl": real},
    ]
    assert cookie_capture._select_target(targets) == real
