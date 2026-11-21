"""微信个人号（ClawBot / iLink）通道：线格式、登录状态机、发提醒。

假服务器是**真的 HTTP 服务**（`BaseHTTPRequestHandler`），不是 mock：iLink 的坑
基本都在头部、body 形状与状态机分支上，mock 掉就什么都测不到。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from meruki_tracker.core.app.wechat import (
    CHANNEL_VERSION,
    IlinkClient,
    WeChatError,
    WeChatSessionExpired,
    fetch_qr,
    poll_qr,
    random_uin,
)


class _Sink:
    """按路径脚本化应答的本地 HTTP 服务，并记录每次请求。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, dict] = {}
        self.status_code = 200
        self.raw_body = ""
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                path = self.path.split("?")[0]
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                sink.calls.append(
                    {
                        "path": path,
                        "query": query,
                        "payload": json.loads(raw) if raw else {},
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                body = sink.raw_body or json.dumps(sink.responses.get(path, {}))
                data = body.encode()
                self.send_response(sink.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _handle
            do_POST = _handle

            def log_message(self, *args) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture()
def sink():
    server = _Sink()
    try:
        yield server
    finally:
        server.close()


def test_fetch_qr_uses_the_documented_wire_format(sink):
    sink.responses["/ilink/bot/get_bot_qrcode"] = {
        "qrcode": "qr-123",
        "qrcode_img_content": "http://weixin.qq.com/x/abc",
    }
    qrcode, content = fetch_qr(sink.url, local_tokens=("tok-1",))

    assert (qrcode, content) == ("qr-123", "http://weixin.qq.com/x/abc")
    call = sink.last()
    assert call["path"] == "/ilink/bot/get_bot_qrcode"
    assert "bot_type=3" in call["query"]
    assert call["payload"] == {"local_token_list": ["tok-1"]}
    # 取码是 GET 语义的接口，但也必须带 App-Id/版本头，否则服务端不认
    assert call["headers"]["ilink-app-id"] == "bot"
    assert call["headers"]["ilink-app-clientversion"] == "256"


def test_fetch_qr_reports_a_missing_payload(sink):
    sink.responses["/ilink/bot/get_bot_qrcode"] = {"qrcode": "qr-123"}
    with pytest.raises(WeChatError, match="没返回二维码内容"):
        fetch_qr(sink.url)


def test_poll_qr_passes_the_code_and_verify_code(sink):
    sink.responses["/ilink/bot/get_qrcode_status"] = {"status": "wait"}
    data = poll_qr(sink.url, "qr-1/2", verify_code="123456")

    assert data["status"] == "wait"
    query = sink.last()["query"]
    assert "qrcode=qr-1%2F2" in query
    assert "verify_code=123456" in query


def test_random_uin_is_base64_of_a_decimal_number():
    import base64

    for _ in range(5):
        decoded = base64.b64decode(random_uin()).decode()
        assert decoded.isdigit()
        assert 0 <= int(decoded) <= 0xFFFFFFFF


def test_client_headers_carry_the_bearer_token(sink):
    sink.responses["/ilink/bot/getupdates"] = {"ret": 0, "msgs": [], "get_updates_buf": "c1"}
    IlinkClient("tok-9", base_url=sink.url).get_updates("")

    headers = sink.last()["headers"]
    assert headers["authorization"] == "Bearer tok-9"
    assert headers["authorizationtype"] == "ilink_bot_token"
    assert headers["x-wechat-uin"]


def test_get_updates_echoes_the_cursor_and_parses_messages(sink):
    sink.responses["/ilink/bot/getupdates"] = {
        "ret": 0,
        "get_updates_buf": "cursor-2",
        "msgs": [
            {
                "from_user_id": "u1",
                "context_token": "ctx-1",
                "item_list": [{"type": 1, "text_item": {"text": "在吗"}}],
            },
            {"from_user_id": "u2"},  # 没 context_token：忽略，发不了
        ],
    }
    result = IlinkClient("tok", base_url=sink.url).get_updates("cursor-1")

    assert sink.last()["payload"]["get_updates_buf"] == "cursor-1"
    assert sink.last()["payload"]["base_info"]["channel_version"] == CHANNEL_VERSION
    assert result.cursor == "cursor-2"
    assert [m.user_id for m in result.messages] == ["u1"]
    assert result.messages[0].context_token == "ctx-1"
    assert result.messages[0].text == "在吗"


def test_session_expiry_is_reported_as_its_own_error(sink):
    sink.responses["/ilink/bot/getupdates"] = {"ret": -14}
    with pytest.raises(WeChatSessionExpired, match="重新扫码"):
        IlinkClient("tok", base_url=sink.url).get_updates("")


def test_other_error_codes_become_wechat_errors(sink):
    sink.responses["/ilink/bot/sendmessage"] = {"errcode": -2, "errmsg": "参数错误"}
    with pytest.raises(WeChatError, match="参数错误"):
        IlinkClient("tok", base_url=sink.url).send_text("u1", "ctx", "hi")


def test_send_text_builds_the_documented_message(sink):
    sink.responses["/ilink/bot/sendmessage"] = {"ret": 0}
    IlinkClient("tok", base_url=sink.url).send_text("u1", "ctx-9", "拍品要结束了")

    msg = sink.last()["payload"]["msg"]
    assert msg["to_user_id"] == "u1"
    assert msg["context_token"] == "ctx-9"
    assert msg["message_type"] == 2
    assert msg["item_list"] == [{"type": 1, "text_item": {"text": "拍品要结束了"}}]
    assert msg["client_id"]  # uuid，每次不同


def test_context_store_round_trips_through_sqlite(tmp_path):
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.app.wechat import ContextStore

    conn = connect(tmp_path / "meruki.db")
    store = ContextStore(conn)
    store.save("u1", "ctx-1", updated_at="2026-11-11T00:00:00Z")
    store.save("u1", "ctx-2", updated_at="2026-11-11T01:00:00Z")
    store.save("u2", "ctx-3", updated_at="2026-11-11T00:30:00Z")

    # 同一用户只留最新 token（协议里就是"每个用户保留最新"）
    assert ContextStore(conn).tokens() == {"u1": "ctx-2", "u2": "ctx-3"}
    store.clear()
    assert ContextStore(conn).tokens() == {}
    conn.close()


def test_wechat_bot_sends_to_every_known_session(tmp_path):
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.app.wechat import ContextStore, WeChatBot

    conn = connect(tmp_path / "meruki.db")
    store = ContextStore(conn)
    store.save("u1", "ctx-1", updated_at="2026-11-11T00:00:00Z")
    store.save("u2", "ctx-2", updated_at="2026-11-11T00:00:00Z")

    sent: list[tuple[str, str, str]] = []

    class _Client:
        def send_text(self, user_id, context_token, text):
            if user_id == "u2":
                raise WeChatError("这个会话已经不能用了")
            sent.append((user_id, context_token, text))

    bot = WeChatBot(client=_Client(), store=store)
    assert bot.send_all("拍品要结束了") == 1
    assert sent == [("u1", "ctx-1", "拍品要结束了")]
    assert "不能用" in bot.last_error  # 一个失败不影响另一个
    conn.close()


def _provider(sink):
    from meruki_tracker.core.app.qrlogin import IlinkProvider

    return IlinkProvider(base_url=sink.url, timeout=5.0)


def test_provider_fetch_returns_something_scannable(sink):
    sink.responses["/ilink/bot/get_bot_qrcode"] = {
        "qrcode": "qr-1",
        "qrcode_img_content": "http://weixin.qq.com/x/abc",
    }
    challenge = _provider(sink).fetch()

    assert challenge.kind == "wechat"
    assert challenge.payload == "http://weixin.qq.com/x/abc"
    assert challenge.token == "qr-1"  # token 存会话句柄，不是二维码内容
    assert "扫码" in challenge.hint


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("wait", "pending"),
        ("scaned", "scanned"),
        ("expired", "failed"),
        ("binded_redirect", "failed"),
        ("need_verifycode", "failed"),
        ("verify_code_blocked", "failed"),
    ],
)
def test_provider_maps_qr_states(sink, status, expected):
    sink.responses["/ilink/bot/get_qrcode_status"] = {"status": status}
    provider = _provider(sink)
    challenge = _challenge()
    assert provider.poll(challenge).state == expected


def test_provider_success_carries_the_credential(sink):
    sink.responses["/ilink/bot/get_qrcode_status"] = {
        "status": "confirmed",
        "bot_token": "tok-abc",
        "ilink_bot_id": "bot-1",
        "baseurl": "https://other.example",
    }
    status = _provider(sink).poll(_challenge())

    assert status.state == "success"
    assert status.credential == "tok-abc"
    assert status.base_url == "https://other.example"
    assert "bot-1" in status.account


def test_provider_success_without_credential_is_failed(sink):
    sink.responses["/ilink/bot/get_qrcode_status"] = {"status": "confirmed"}
    status = _provider(sink).poll(_challenge())

    assert status.state == "failed"
    assert "凭据" in status.message


def test_provider_redirects_the_poll_to_the_idc_host(sink):
    from meruki_tracker.core.app.qrlogin import QrChallenge

    sink.responses["/ilink/bot/get_qrcode_status"] = {
        "status": "scaned_but_redirect",
        "redirect_host": "127.0.0.1:1",
    }
    provider = _provider(sink)
    status = provider.poll(QrChallenge(kind="wechat", payload="x", token="qr"))
    assert status.state == "scanned"
    assert provider._poll_base == "https://127.0.0.1:1"


def _challenge():
    from meruki_tracker.core.app.qrlogin import QrChallenge

    return QrChallenge(kind="wechat", payload="http://x/1", token="qr-1")


def test_build_provider_needs_the_switch(tmp_path):
    from meruki_tracker.core.app.qrlogin import IlinkProvider, build_provider
    from meruki_tracker.core.config import ChannelsConfig

    assert build_provider(None) is None
    assert build_provider(ChannelsConfig()) is None  # 没打开就不给扫码

    provider = build_provider(ChannelsConfig(wechat_enable=True, wechat_token="tok"))
    assert isinstance(provider, IlinkProvider)


def test_build_provider_honours_the_requested_channel():
    """指定了渠道就只造那一个；没开就不给——不能偷偷换成另一个渠道的码。"""
    from meruki_tracker.core.app.qrlogin import (
        KIND_QQ,
        KIND_WECHAT,
        IlinkProvider,
        QqProvider,
        build_provider,
    )
    from meruki_tracker.core.config import ChannelsConfig

    both = ChannelsConfig(wechat_enable=True, qq_enable=True)
    assert isinstance(build_provider(both, KIND_WECHAT), IlinkProvider)
    assert isinstance(build_provider(both, KIND_QQ), QqProvider)
    # 留空时按老规矩 QQ 优先
    fallback = build_provider(both)
    assert fallback is not None and fallback.kind == KIND_QQ

    only_wechat = ChannelsConfig(wechat_enable=True)
    assert build_provider(only_wechat, KIND_QQ) is None, "QQ 没开就不该造 QQ 的 provider"
    assert build_provider(only_wechat, KIND_WECHAT) is not None

    assert build_provider(ChannelsConfig(), KIND_WECHAT) is None


def test_build_provider_feeds_the_stored_token_back(sink):
    """本地已有 token 时报给服务端，已绑定的机器人会回 binded_redirect 而不是新开会话。"""
    from meruki_tracker.core.app.qrlogin import build_provider
    from meruki_tracker.core.config import ChannelsConfig

    sink.responses["/ilink/bot/get_bot_qrcode"] = {
        "qrcode": "qr",
        "qrcode_img_content": "http://x/1",
    }
    provider = build_provider(
        ChannelsConfig(wechat_enable=True, wechat_token="tok-old", wechat_base_url=sink.url)
    )
    provider.fetch()
    assert sink.last()["payload"] == {"local_token_list": ["tok-old"]}


def test_wechat_long_poll_failure_is_visible_in_the_log(tmp_path, caplog):
    """长轮询一直失败必须是**能在日志里查到**的。

    2026-11-18 审计：中间掉线一次记 debug 无所谓（35 秒长轮询偶尔断开是常态），
    但这里失败后会每 5 秒重试且永不放弃——地址写错、网关持续 500、token 被换掉
    都走这条路。日志默认 INFO 级别，debug 等于没记：微信通道静默死掉，用户只会
    觉得"提醒怎么不来了"。
    """
    import asyncio
    import logging
    import time

    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.app.wechat import ContextStore, WeChatBot

    conn = connect(tmp_path / "meruki.db")
    store = ContextStore(conn)

    class _Client:
        def get_updates(self, cursor):
            raise WeChatError("网关持续 500")

    bot = WeChatBot(client=_Client(), store=store)

    async def _drive() -> None:
        task = asyncio.create_task(bot.run())
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if any("长轮询失败" in r.message for r in caplog.records):
                    return
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await task

    with caplog.at_level(logging.INFO, logger="meruki_tracker.core.app.wechat"):
        asyncio.run(_drive())

    hits = [r for r in caplog.records if "长轮询失败" in r.message]
    assert hits, "长轮询失败必须在 INFO 级别可见"
    assert hits[0].levelno >= logging.WARNING
    assert "网关持续 500" in hits[0].message
    conn.close()


def test_wechat_bot_can_retry_only_when_the_session_is_still_valid(tmp_path):
    """会话过期是终态（token 已作废），不能自动重连；其它原因才值得重试。"""
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.app.wechat import ContextStore, WeChatBot

    conn = connect(tmp_path / "m.db")
    bot = WeChatBot(client=object(), store=ContextStore(conn))

    # 没出错时认为可以重试（真遇到瞬时错误 run() 内部会自己 sleep 重试）
    assert bot.can_retry() is True
    conn.close()


def test_wechat_bot_honours_the_retryable_channel_protocol(tmp_path):
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.app.notifications import RetryableChannel
    from meruki_tracker.core.app.wechat import ContextStore, WeChatBot

    conn = connect(tmp_path / "m.db")
    bot = WeChatBot(client=object(), store=ContextStore(conn))
    assert isinstance(bot, RetryableChannel), "微信 bot 没兑现 can_retry 契约"
    conn.close()


def test_wechat_bot_can_retry_is_false_after_a_real_session_expiry(tmp_path):
    """真跑一次会话过期的 run()，之后 can_retry() 必须是 False。"""
    import asyncio

    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.app.wechat import ContextStore, WeChatBot

    conn = connect(tmp_path / "m.db")

    class _Client:
        def get_updates(self, cursor):
            raise WeChatSessionExpired("微信会话已过期，请重新扫码挂载")

    bot = WeChatBot(client=_Client(), store=ContextStore(conn))
    asyncio.run(bot.run())  # 会话过期会正常 return
    assert bot.can_retry() is False
    conn.close()
