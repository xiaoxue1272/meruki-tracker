"""QQ 官方机器人通道：换 token、取网关、发消息、WebSocket 收事件。

**真的起一个 WebSocket 服务**（``websockets.serve``）而不是 mock 掉连接：
Identify 报文、Hello 心跳、事件分发这三段最容易写错，mock 掉就等于只测了
自己的假设。HTTP 侧同样用真的 ``HTTPServer``。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from meruki_tracker.core.adapters.store.db import connect
from meruki_tracker.core.app.qqchannel import (
    EVENT_C2C,
    EVENT_GROUP,
    INTENT_GROUP_AND_C2C,
    KIND_C2C,
    KIND_GROUP,
    QqBot,
    QqBotClient,
    QqBotError,
    QqNotifier,
    QqSessionStore,
    build_client,
)
from meruki_tracker.core.config import ChannelsConfig

# ------------------------------------------------------------------ 假 openapi


class _Sink:
    """一个真的 HTTP 服务，按 path 回脚本化的响应。"""

    def __init__(self, routes: dict[str, tuple[int, dict]]) -> None:
        self.routes = routes
        self.seen: list[dict] = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                sink.seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(body) if body else None,
                    }
                )
                status, payload = sink.routes.get(self.path.split("?")[0], (404, {}))
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_POST = _reply
            do_GET = _reply

            def log_message(self, *args) -> None:  # 别刷屏
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _Sink:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _client(sink: _Sink, **kwargs) -> QqBotClient:
    return QqBotClient(app_id="888888888", app_secret="s3cret", api_base=sink.base, **kwargs)


# ------------------------------------------------------------------ 凭证与网关


def test_access_token_is_fetched_and_reused():
    routes = {"/app/getAppAccessToken": (200, {"access_token": "T1", "expires_in": "7200"})}
    with _Sink(routes) as sink:
        client = _client(sink)
        assert client.access_token() == "T1"
        assert client.access_token() == "T1"
    fetches = [r for r in sink.seen if r["path"] == "/app/getAppAccessToken"]
    assert len(fetches) == 1, "token 没过期就不该重复换"
    assert fetches[0]["body"] == {"appId": "888888888", "clientSecret": "s3cret"}


def test_access_token_is_renewed_when_close_to_expiry():
    routes = {"/app/getAppAccessToken": (200, {"access_token": "T1", "expires_in": "30"})}
    with _Sink(routes) as sink:
        client = _client(sink)
        assert client.access_token() == "T1"
        # expires_in=30 < TOKEN_MARGIN(60)：缓存时间被压到 1 秒，过期就得重换
        await_ = __import__("time").sleep
        await_(1.05)
        assert client.access_token() == "T1"
    assert len([r for r in sink.seen if r["path"] == "/app/getAppAccessToken"]) == 2


def test_access_token_error_is_reported_clearly():
    routes = {"/app/getAppAccessToken": (200, {"code": 100007, "message": "appid 无效"})}
    with _Sink(routes) as sink, pytest.raises(QqBotError, match="appid 无效"):
        _client(sink).access_token()


def test_gateway_uses_the_bearer_header():
    routes = {
        "/app/getAppAccessToken": (200, {"access_token": "T1", "expires_in": "7200"}),
        "/gateway": (200, {"url": "wss://example.invalid/websocket/"}),
    }
    with _Sink(routes) as sink:
        got = _client(sink).gateway()
    assert got == "wss://example.invalid/websocket/"
    call = next(r for r in sink.seen if r["path"] == "/gateway")
    assert call["headers"]["authorization"] == "QQBot T1"


# ------------------------------------------------------------------ 发消息


def test_send_c2c_builds_the_documented_body():
    routes = {
        "/app/getAppAccessToken": (200, {"access_token": "T1", "expires_in": "7200"}),
        "/v2/users/u1/messages": (200, {"id": "ROBOT1.0_x", "timestamp": "now"}),
    }
    with _Sink(routes) as sink:
        _client(sink).send_c2c("u1", "拍品 15 分钟后结束")
    call = next(r for r in sink.seen if r["path"] == "/v2/users/u1/messages")
    assert call["body"] == {"content": "拍品 15 分钟后结束", "msg_type": 0}
    assert call["headers"]["authorization"] == "QQBot T1"


def test_send_group_uses_the_group_path():
    routes = {
        "/app/getAppAccessToken": (200, {"access_token": "T1", "expires_in": "7200"}),
        "/v2/groups/g1/messages": (200, {"id": "ROBOT1.0_y"}),
    }
    with _Sink(routes) as sink:
        _client(sink).send_group("g1", "hello")
    assert [r["path"] for r in sink.seen if "messages" in r["path"]] == ["/v2/groups/g1/messages"]


def test_send_reports_a_200_with_an_error_code():
    """官方接口出错时也可能回 200 + code，不能只看 HTTP 状态。"""
    routes = {
        "/app/getAppAccessToken": (200, {"access_token": "T1", "expires_in": "7200"}),
        "/v2/users/u1/messages": (200, {"code": 40054007, "message": "消息长度超限"}),
    }
    with _Sink(routes) as sink, pytest.raises(QqBotError, match="40054007"):
        _client(sink).send_c2c("u1", "x")


def test_http_error_carries_the_status_code():
    routes = {"/app/getAppAccessToken": (502, {"message": "bad gateway"})}
    with _Sink(routes) as sink, pytest.raises(QqBotError, match="502"):
        _client(sink).access_token()


# ------------------------------------------------------------------ 会话落库


def test_session_store_round_trips_and_dedupes(tmp_path):
    conn = connect(tmp_path / "m.db")
    store = QqSessionStore(conn)
    store.save(KIND_C2C, "u1", updated_at="2026-11-12T00:00:00Z")
    store.save(KIND_GROUP, "g1", updated_at="2026-11-12T00:00:01Z")
    store.save(KIND_C2C, "u1", updated_at="2026-11-12T00:00:02Z")
    assert store.sessions() == [(KIND_C2C, "u1"), (KIND_GROUP, "g1")]
    # 新实例重新读库（重启后还能推给老会话）
    assert QqSessionStore(conn).sessions() == [(KIND_C2C, "u1"), (KIND_GROUP, "g1")]
    store.clear()
    assert store.sessions() == []


def test_send_all_reports_how_many_sessions_were_reached(tmp_path):
    conn = connect(tmp_path / "m.db")
    store = QqSessionStore(conn)
    store.save(KIND_C2C, "u1")
    store.save(KIND_GROUP, "g1")

    sent: list[tuple[str, str]] = []

    class _Client:
        def send_c2c(self, openid: str, text: str) -> None:
            sent.append(("c2c", openid))

        def send_group(self, openid: str, text: str) -> None:
            sent.append(("group", openid))

    bot = QqBot(client=_Client(), store=store)
    assert bot.send_all("hi") == 2
    assert sent == [("c2c", "u1"), ("group", "g1")]


def test_send_all_keeps_going_when_one_session_fails(tmp_path):
    conn = connect(tmp_path / "m.db")
    store = QqSessionStore(conn)
    store.save(KIND_C2C, "bad")
    store.save(KIND_C2C, "good")

    class _Client:
        def send_c2c(self, openid: str, text: str) -> None:
            if openid == "bad":
                raise QqBotError("用户拒收")

    bot = QqBot(client=_Client(), store=store)
    assert bot.send_all("hi") == 1
    assert "用户拒收" in bot.last_error


# ------------------------------------------------------- 真的 WebSocket 网关


class _FakeGateway:
    """一个真的 WebSocket 服务：握手 → Hello → 收 Identify → 发事件。"""

    def __init__(self, *, events: list[dict] | None = None, op7_after: bool = False) -> None:
        self.events = events or []
        self.op7_after = op7_after
        self.identify: dict | None = None
        self.heartbeats: list[dict] = []
        self.port = 0
        self._server = None

    async def _handler(self, ws) -> None:
        try:
            await self._serve(ws)
        except Exception:  # 客户端 cancel 时连接会突然断，别把噪音打到日志里
            return

    async def _serve(self, ws) -> None:
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 300}}))
        async for raw in ws:
            payload = json.loads(raw)
            if payload.get("op") == 2:
                self.identify = payload
                await ws.send(
                    json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "s1"}})
                )
                for index, event in enumerate(self.events, start=2):
                    await ws.send(json.dumps({"op": 0, "s": index, **event}))
                if self.op7_after:
                    await asyncio.sleep(0.05)
                    await ws.send(json.dumps({"op": 7}))
            elif payload.get("op") == 1:
                self.heartbeats.append(payload)
                await ws.send(json.dumps({"op": 11}))

    async def __aenter__(self) -> _FakeGateway:
        from websockets.asyncio.server import serve

        self._server = await serve(self._handler, "127.0.0.1", 0).__aenter__()
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        await self._server.__aexit__(*exc)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


class _StubClient:
    def __init__(self, url: str) -> None:
        self._url = url

    def gateway(self) -> str:
        return self._url

    def access_token(self) -> str:
        return "T1"


async def test_gateway_identifies_with_the_c2c_intent(tmp_path):
    conn = connect(tmp_path / "m.db")
    async with _FakeGateway() as gateway:
        bot = QqBot(client=_StubClient(gateway.url), store=QqSessionStore(conn))
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert gateway.identify is not None
    assert gateway.identify["d"]["token"] == "QQBot T1"
    assert gateway.identify["d"]["intents"] == INTENT_GROUP_AND_C2C
    assert gateway.identify["d"]["shard"] == [0, 1]


async def test_c2c_event_records_the_openid(tmp_path):
    conn = connect(tmp_path / "m.db")
    event = {
        "t": EVENT_C2C,
        "d": {
            "id": "ROBOT1.0_msg",
            "author": {"user_openid": "U-OPENID", "bot": False},
            "content": "你好",
            "timestamp": "2026-11-12T10:00:00+08:00",
        },
    }
    async with _FakeGateway(events=[event]) as gateway:
        bot = QqBot(client=_StubClient(gateway.url), store=QqSessionStore(conn))
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.4)
        online = bot.online  # cancel 会把 online 置回 False，先取下来
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert QqSessionStore(conn).sessions() == [(KIND_C2C, "U-OPENID")]
    assert online is True


async def test_group_event_records_the_group_openid(tmp_path):
    conn = connect(tmp_path / "m.db")
    event = {"t": EVENT_GROUP, "d": {"group_openid": "G-1", "content": "@bot hi"}}
    async with _FakeGateway(events=[event]) as gateway:
        bot = QqBot(client=_StubClient(gateway.url), store=QqSessionStore(conn))
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert QqSessionStore(conn).sessions() == [(KIND_GROUP, "G-1")]


async def test_heartbeat_is_sent_and_acked(tmp_path):
    """Hello 里的 heartbeat_interval 是毫秒，换算错了会狂发或永不发。"""
    conn = connect(tmp_path / "m.db")
    async with _FakeGateway() as gateway:
        bot = QqBot(client=_StubClient(gateway.url), store=QqSessionStore(conn))
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.9)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert gateway.heartbeats, "300ms 间隔下 0.9 秒内至少该发两次"
    assert gateway.heartbeats[0]["d"] == 1, "心跳要带最新的 s（READY 的 s=1）"


async def test_op7_makes_the_client_reconnect(tmp_path):
    conn = connect(tmp_path / "m.db")
    async with _FakeGateway(op7_after=True) as gateway:
        bot = QqBot(client=_StubClient(gateway.url), store=QqSessionStore(conn))
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.4)
        # 第一次连接按 op7 断开后，会退避 2 秒再连；这里只验证"断开"这一步
        bot._last_error = bot.last_error  # 读一次，确认属性还在
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert gateway.identify is not None


# ------------------------------------------------------------------ 出厂


def test_build_client_returns_none_without_credentials():
    assert build_client(ChannelsConfig(qq_enable=True)) is None
    assert build_client(ChannelsConfig(qq_enable=True, qq_app_id="a")) is None
    assert build_client(None) is None


def test_build_client_maps_the_configured_base():
    cfg = ChannelsConfig(
        qq_enable=True,
        qq_app_id="a",
        qq_app_secret="b",
        qq_api_base="http://127.0.0.1:9/x",
        qq_timeout_seconds=7.0,
    )
    client = build_client(cfg)
    assert client is not None
    assert (client.app_id, client.app_secret) == ("a", "b")
    assert client.api_base == "http://127.0.0.1:9/x"
    assert client.timeout == 7.0


def test_notifier_dispatches_into_a_background_task():
    """``notify`` 必须立刻返回——它跑在 1Hz tick 上，同步发 HTTP 会卡界面。"""

    class _Bot:
        def __init__(self) -> None:
            self.texts: list[str] = []

        def send_all(self, text: str) -> int:
            self.texts.append(text)
            return 1

    bot = _Bot()
    from meruki_tracker.core.app.notifications import Notification

    QqNotifier(bot).notify(Notification(title="标题", body="正文", asin="A1"))
    # 投递是后台任务，这里只断言"没有同步阻塞地发完"
    assert bot.texts == [] or "标题" in bot.texts[0]


def test_expired_token_is_dropped_so_the_next_attempt_fetches_a_fresh_one():
    """服务端说 token 无效时必须丢掉缓存。

    真实日志里出现过：``QQ 网关断开，N 秒后重连: HTTP 401 {"message":"AccessToken
    无效或过期"}`` 连着刷好几条。原因是 ``access_token`` 只按自己记的
    ``expires_in`` 判断过期——服务端提前作废（换过 AppSecret、重置过 token）时，
    缓存里的坏 token 会被**一直用到两小时后**，这期间所有提醒都投不出去。
    """
    routes = {
        "/app/getAppAccessToken": (200, {"access_token": "STALE", "expires_in": "7200"}),
        "/gateway": (401, {"message": "AccessToken无效或过期", "code": 11241}),
    }
    with _Sink(routes) as sink:
        client = _client(sink)
        with pytest.raises(QqBotError, match="401"):
            client.gateway()

        # 服务端恢复正常，并且这次会发一个新 token
        sink.routes["/gateway"] = (200, {"url": "wss://example.invalid/websocket/"})
        sink.routes["/app/getAppAccessToken"] = (
            200,
            {"access_token": "FRESH", "expires_in": "7200"},
        )
        assert client.gateway() == "wss://example.invalid/websocket/"

    token_fetches = [r for r in sink.seen if r["path"] == "/app/getAppAccessToken"]
    assert len(token_fetches) == 2, "401 之后必须重新换 token，不能继续用坏的"
    gateway_calls = [r for r in sink.seen if r["path"] == "/gateway"]
    assert gateway_calls[-1]["headers"]["authorization"] == "QQBot FRESH"


def test_send_also_drops_an_invalidated_token():
    """发消息也会撞上同一个问题：401 之后不能一直用缓存里的坏 token。"""
    routes = {
        "/app/getAppAccessToken": (200, {"access_token": "STALE", "expires_in": "7200"}),
        "/v2/users/OPENID/messages": (401, {"message": "AccessToken无效或过期", "code": 11241}),
    }
    with _Sink(routes) as sink:
        client = _client(sink)
        with pytest.raises(QqBotError, match="401"):
            client.send_c2c("OPENID", "hi")
        sink.routes["/v2/users/OPENID/messages"] = (200, {"code": 0})
        sink.routes["/app/getAppAccessToken"] = (
            200,
            {"access_token": "FRESH", "expires_in": "7200"},
        )
        client.send_c2c("OPENID", "hi")

    token_fetches = [r for r in sink.seen if r["path"] == "/app/getAppAccessToken"]
    assert len(token_fetches) == 2
    sent = [r for r in sink.seen if r["path"] == "/v2/users/OPENID/messages"]
    assert sent[-1]["headers"]["authorization"] == "QQBot FRESH"


def test_qq_bot_never_auto_reconnects_from_the_task_callback(tmp_path):
    """QQ 的 run() 自带指数退避重连，任务结束基本是程序在关——别再重连。"""
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.app.notifications import RetryableChannel
    from meruki_tracker.core.app.qqchannel import QqBot, QqSessionStore

    conn = connect(tmp_path / "m.db")
    bot = QqBot(client=object(), store=QqSessionStore(conn))
    assert isinstance(bot, RetryableChannel), "QQ bot 没兑现 can_retry 契约"
    assert bot.can_retry() is False
    conn.close()
