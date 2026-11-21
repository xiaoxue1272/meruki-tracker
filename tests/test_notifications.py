"""通知渠道：组装规则、异步投递、失败隔离，以及**真实**的 SMTP / OneBot 投递。

邮件用一个极小的 SMTP sink（socket 服务端，只实现 ``EHLO/MAIL/RCPT/DATA/QUIT``）
真收一封；OneBot 用本地 HTTP 服务真收一次 POST。两者都不 mock 各自的客户端库，
所以「协议写错了」这类 bug 能被抓住。
"""

from __future__ import annotations

import asyncio
import email
import json
import socketserver
import threading
from email.header import decode_header
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from meruki_tracker.core.app.notifications import (
    ChannelsConfig,
    CompositeNotifier,
    EmailNotifier,
    Notification,
    NullNotifier,
    OneBotV11Notifier,
    WebhookNotifier,
    _body_text,
    _im_text,
    build_notifier,
    send_test,
)


class _Recorder:
    """记录收到的通知，可选抛异常。"""

    def __init__(self, *, explode: bool = False) -> None:
        self.notes: list[Notification] = []
        self._explode = explode

    def notify(self, note: Notification) -> None:
        self.notes.append(note)
        if self._explode:
            raise RuntimeError("渠道炸了")


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    """等后台投递完成（渠道都是丢到线程里做的，不能立即断言）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等投递超时")


# ---------------------------------------------------------------- 组装


def test_nothing_enabled_gives_null_notifier():
    assert isinstance(build_notifier(None), NullNotifier)
    assert isinstance(build_notifier(ChannelsConfig()), NullNotifier)


def test_webhook_enabled_without_url_is_ignored():
    cfg = ChannelsConfig(webhook_enable=True, webhook_url="")
    assert isinstance(build_notifier(cfg), NullNotifier)


def test_email_needs_host_and_recipient():
    assert isinstance(build_notifier(ChannelsConfig(email_enable=True)), NullNotifier)


def test_all_three_channels_compose():
    cfg = ChannelsConfig(
        webhook_enable=True,
        webhook_url="https://example.com/hook",
        email_enable=True,
        email_smtp_host="smtp.example.com",
        email_username="me@example.com",
        email_password="secret",
        email_to="a@example.com, b@example.com",
        onebot_enable=True,
        onebot_url="http://127.0.0.1:3000",
        onebot_target="group:42",
    )
    notifier = build_notifier(cfg)
    assert isinstance(notifier, CompositeNotifier)
    assert [type(c).__name__ for c in notifier.children] == [
        "WebhookNotifier",
        "EmailNotifier",
        "OneBotV11Notifier",
    ]


def test_plain_dict_is_accepted_and_coerced():
    """旧调用点传裸 dict 也能用；不认识的键忽略。"""
    cfg = build_notifier({"webhook_enable": True, "webhook_url": "https://x/y", "wechat": "?"})
    assert isinstance(cfg, WebhookNotifier)


def test_composite_isolates_failures():
    a, b = _Recorder(explode=True), _Recorder()
    CompositeNotifier([a, b]).notify(Notification(title="t", body="b"))
    assert len(a.notes) == 1 and len(b.notes) == 1, "前面的渠道炸了，后面的仍要收到"


# ---------------------------------------------------------------- 文案


def test_body_text_has_asin_and_time():
    text = _body_text(Notification(title="t", body="拍品甲 即将结束", asin="A1"))
    assert "拍品甲 即将结束" in text
    assert "拍品: A1" in text
    assert "时间: " in text


def test_im_text_is_two_lines():
    assert _im_text(Notification(title="标题", body="正文")) == "标题\n正文"


# ---------------------------------------------------------------- 真实 SMTP


def _header(msg, name: str) -> str:
    """解出 RFC2047 编码的标题（``=?utf-8?b?...?=`）。"""
    raw = msg[name]
    if raw is None:
        return ""
    parts = decode_header(raw)
    return "".join(
        chunk.decode(enc or "utf-8", "replace") if isinstance(chunk, bytes) else chunk
        for chunk, enc in parts
    )


def _addr_of(line: str) -> str:
    """从 ``MAIL FROM:<a@b> size=1`` / ``RCPT TO:<c@d>`` 里取出地址。"""
    body = line.partition(":")[2]
    start = body.find("<")
    end = body.find(">", start + 1)
    if start >= 0 and end > start:
        return body[start + 1 : end]
    return body.strip()


class _SmtpSink(socketserver.StreamRequestHandler):
    """够用的 SMTP 服务端：收下一封信，记在 ``received`` 里。"""

    received: ClassVar[list[dict[str, str]]] = []

    def handle(self) -> None:
        self._say("220 localhost mw-test ready")
        mail_from = ""
        rcpt: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            verb = text[:4].upper()
            if verb == "EHLO":
                # 不宣告 STARTTLS：客户端应当退回明文（这正是要测的分支）
                self._say("250-localhost")
                self._say("250 SIZE 10485760")
            elif verb == "HELO":
                self._say("250 localhost")
            elif verb == "MAIL":
                mail_from = _addr_of(text)
                rcpt = []
                self._say("250 OK")
            elif verb == "RCPT":
                rcpt.append(_addr_of(text))
                self._say("250 OK")
            elif verb == "DATA":
                self._say("354 End data with <CR><LF>.<CR><LF>")
                chunks: list[str] = []
                while True:
                    raw = self.rfile.readline()
                    if not raw or raw in (b".\r\n", b".\n"):
                        break
                    chunks.append(raw.decode("utf-8", "replace"))
                type(self).received.append({"from": mail_from, "to": rcpt, "data": "".join(chunks)})
                self._say("250 OK queued")
            elif verb == "QUIT":
                self._say("221 Bye")
                return
            elif verb in ("RSET", "NOOP"):
                self._say("250 OK")
            else:
                self._say("250 OK")

    def _say(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("utf-8"))
        self.wfile.flush()


@pytest.fixture()
def smtp_sink():
    _SmtpSink.received = []
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SmtpSink)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_email_really_sends_a_message(smtp_sink: int):
    notifier = EmailNotifier(
        host="127.0.0.1",
        port=smtp_sink,
        username="",  # 不登录，sink 也不做认证
        password="",
        sender="me@example.com",
        recipients=("a@example.com", "b@example.com"),
        timeout=5.0,
    )

    # 直接跑 _send：本用例验的是 SMTP 协议正确性（真发一封 + 头部/正文编码）。
    # 以前走 notify()→后台 task→轮询 received，CI 上偶发 5s 超时：busy runner
    # 上后台 task 调度不可靠，而 _post 会把 _send 的异常吞掉，失败时只看到
    # "等投递超时"、看不到真实原因。改成同步直呼后完全确定，失败可见真异常。
    # 异步投递 + 吞异常的行为由 test_email_failure_is_swallowed 覆盖。
    notifier._send(Notification(title="结束前 15 分钟", body="拍品甲 即将结束", asin="A1"))
    assert len(_SmtpSink.received) == 1
    mail = _SmtpSink.received[0]
    assert mail["from"] == "me@example.com"
    assert sorted(mail["to"]) == ["a@example.com", "b@example.com"]

    # 按真实邮件客户端的方式解析：非 ASCII 标题会被 RFC2047 编码成 base64，
    # 直接字符串比对会误判。
    parsed = email.message_from_string(mail["data"], policy=email.policy.default)
    assert _header(parsed, "Subject") == "[挖煤姬] 结束前 15 分钟"
    assert parsed["From"] == "me@example.com"
    body = parsed.get_content()
    assert "拍品甲 即将结束" in body
    assert "拍品: A1" in body


def test_email_failure_is_swallowed():
    """端口没人监听时只记日志，绝不把异常抛给调用方。"""
    notifier = EmailNotifier(
        host="127.0.0.1",
        port=1,
        username="",
        password="",
        sender="me@example.com",
        recipients=("a@example.com",),
        timeout=1.0,
    )

    async def go() -> None:
        notifier.notify(Notification(title="t", body="b"))  # 不该抛
        await asyncio.sleep(0.2)

    asyncio.run(go())


# ---------------------------------------------------------------- 真实 OneBot


class _OneBot(BaseHTTPRequestHandler):
    calls: ClassVar[list[dict]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        type(self).calls.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "json": json.loads(raw.decode("utf-8")),
            }
        )
        body = json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音
        return


@pytest.fixture()
def onebot():
    _OneBot.calls = []
    server = HTTPServer(("127.0.0.1", 0), _OneBot)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_onebot_private_message(onebot: str):
    notifier = OneBotV11Notifier(
        base_url=onebot + "/",  # 末尾斜杠要能容忍
        token="tok-123",
        target="private:10001",
        timeout=5.0,
    )
    assert notifier.api_path() == "send_private_msg"

    async def go() -> None:
        notifier.notify(Notification(title="结束提醒", body="拍品甲 即将结束"))
        await _wait_for(lambda: bool(_OneBot.calls))

    asyncio.run(go())
    call = _OneBot.calls[0]
    assert call["path"] == "/send_private_msg"
    assert call["auth"] == "Bearer tok-123"
    assert call["json"] == {
        "message": "结束提醒\n拍品甲 即将结束",
        "auto_escape": True,
        "user_id": "10001",
    }


def test_onebot_group_message_without_token(onebot: str):
    notifier = OneBotV11Notifier(base_url=onebot, target="group:20002")

    async def go() -> None:
        notifier.notify(Notification(title="出价被超过", body="拍品乙 有人加价"))
        await _wait_for(lambda: bool(_OneBot.calls))

    asyncio.run(go())
    call = _OneBot.calls[0]
    assert call["path"] == "/send_group_msg"
    assert call["auth"] == ""
    assert call["json"]["group_id"] == "20002"


def test_onebot_bare_number_means_private():
    assert OneBotV11Notifier(base_url="http://x", target="10086").api_path() == "send_private_msg"


def test_onebot_failure_is_swallowed():
    """端点不可达时只记日志。"""
    notifier = OneBotV11Notifier(base_url="http://127.0.0.1:1", target="1", timeout=1.0)

    async def go() -> None:
        notifier.notify(Notification(title="t", body="b"))  # 不该抛
        await asyncio.sleep(0.2)

    asyncio.run(go())


# ---------------------------------------------------------------- 生命周期


def test_notify_outside_event_loop_is_skipped_not_raised():
    """没有事件循环时（脚本里直接调）跳过投递，但绝不抛异常。"""
    WebhookNotifier("http://127.0.0.1:1/never").notify(Notification(title="t", body="b"))
    OneBotV11Notifier(base_url="http://127.0.0.1:1", target="1").notify(
        Notification(title="t", body="b")
    )


def test_body_text_does_not_repeat_the_asin_it_already_mentions():
    """「出价被超过」的正文里已经写了拍品号，不该再补一行重复的。"""
    text = _body_text(Notification(title="出价被超过", body="拍品甲（拍品 A1）", asin="A1"))
    assert text.count("A1") == 1, text


# ---------------------------------------------------------------- 发送测试消息


def _names(results: list[tuple[str, bool, str]]) -> dict[str, tuple[bool, str]]:
    return {name: (ok, detail) for name, ok, detail in results}


def test_send_test_reports_nothing_enabled_as_empty():
    """一个渠道都没开：返回空列表，由界面提示去打开一个。"""
    assert asyncio.run(send_test(ChannelsConfig())) == []


def test_send_test_lists_enabled_channels_that_are_incomplete():
    cfg = ChannelsConfig(webhook_enable=True, email_enable=True, email_to="")
    got = _names(asyncio.run(send_test(cfg)))
    assert got["Webhook"] == (False, "还没填地址")
    assert got["邮件"][0] is False
    assert "收件人" in got["邮件"][1]


def test_send_test_really_posts_to_the_webhook(onebot: str):
    """OneBot 端点同时也是个能收 JSON 的 HTTP 服务，用它验真实投递。"""
    cfg = ChannelsConfig(onebot_enable=True, onebot_url=onebot, onebot_target="private:42")
    results = asyncio.run(send_test(cfg))
    assert results == [("OneBot", True, "已发送")]
    assert _OneBot.calls[-1]["path"] == "/send_private_msg"
    assert "测试消息" in _OneBot.calls[-1]["json"]["message"]


def test_send_test_reports_a_dead_channel_instead_of_pretending():
    cfg = ChannelsConfig(onebot_enable=True, onebot_url="http://127.0.0.1:1", onebot_target="1")
    results = asyncio.run(send_test(cfg))
    name, ok, detail = results[0]
    assert name == "OneBot"
    assert ok is False
    assert detail  # 不是空字符串：要说出为什么不行


def test_send_test_really_sends_an_email(smtp_sink: int):
    cfg = ChannelsConfig(
        email_enable=True,
        email_smtp_host="127.0.0.1",
        email_smtp_port=smtp_sink,
        email_to="me@example.com",
        email_from="me@example.com",
    )
    results = asyncio.run(send_test(cfg))
    assert results == [("邮件", True, "已发送")]
    assert len(_SmtpSink.received) == 1


def test_send_test_explains_unmounted_wechat_and_qq():
    cfg = ChannelsConfig(wechat_enable=True, qq_enable=True)
    got = _names(asyncio.run(send_test(cfg)))
    assert got["微信"] == (False, "还没扫码挂载")
    assert got["QQ"] == (False, "还没扫码挂载")


def test_send_test_says_qq_channel_is_not_connected_yet():
    """凭据有了但长连没起来——必须如实说，不能显示"成功"。"""
    cfg = ChannelsConfig(qq_enable=True, qq_app_id="888888888", qq_app_secret="s")
    got = _names(asyncio.run(send_test(cfg)))
    assert got["QQ"][0] is False
    assert "连接还没起来" in got["QQ"][1]


def test_send_test_qq_really_sends_through_the_channel():
    """QQ 通道真的被调用，并把会话数报出来。"""

    class _Bot:
        def __init__(self, sent: int) -> None:
            self.sent = sent
            self.texts: list[str] = []

        def send_all(self, text: str) -> int:
            self.texts.append(text)
            return self.sent

    cfg = ChannelsConfig(qq_enable=True, qq_app_id="888888888", qq_app_secret="s")
    bot = _Bot(2)
    got = _names(asyncio.run(send_test(cfg, qq=bot)))
    assert got["QQ"] == (True, "已发送给 2 个会话")
    assert "测试消息" in bot.texts[0]


def test_send_test_qq_explains_an_empty_session_list():
    class _Bot:
        def send_all(self, text: str) -> int:
            return 0

    cfg = ChannelsConfig(qq_enable=True, qq_app_id="888888888", qq_app_secret="s")
    got = _names(asyncio.run(send_test(cfg, qq=_Bot())))
    assert got["QQ"][0] is False
    assert "给机器人发一条消息" in got["QQ"][1]


def test_send_test_counts_wechat_sessions():
    class _Bot:
        def __init__(self, sent: int) -> None:
            self.sent = sent

        def send_all(self, text: str) -> int:
            return self.sent

    cfg = ChannelsConfig(wechat_enable=True)
    assert _names(asyncio.run(send_test(cfg, wechat=_Bot(2))))["微信"] == (
        True,
        "已发送给 2 个会话",
    )
    got = _names(asyncio.run(send_test(cfg, wechat=_Bot(0))))
    assert got["微信"][0] is False
    assert "给机器人发一条消息" in got["微信"][1]


# ------------------------------------------------- 失败必须"看得见"（2026-11-18 审计）


def test_delivery_failure_is_visible_at_the_default_log_level(caplog):
    """投递失败必须留下 INFO 以上能看到的记录。

    默认日志级别是 INFO（``setup_logging``）。以前三处 ``_post`` 都用
    ``log.debug``，用户开了 webhook、地址写错，界面一切正常、日志一个字没有——
    连"为什么没收到"都无从查起（实测连发三次，可见日志 0 条）。
    """
    import logging

    from meruki_tracker.core.app import notifications as mod

    mod._failure_logged.clear()
    notifier = WebhookNotifier("http://127.0.0.1:1/hook", timeout=0.3)

    async def go() -> None:
        notifier.notify(Notification(title="t", body="b"))
        await asyncio.sleep(0.6)

    with caplog.at_level(logging.INFO, logger="meruki_tracker.core.app.notifications"):
        asyncio.run(go())

    hits = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert hits, "投递失败在默认级别下必须可见"
    assert "webhook" in hits[0].getMessage().lower()


def test_repeated_delivery_failures_are_throttled(caplog):
    """同一渠道同类错误十分钟内只报一次，别把日志刷满。"""
    import logging

    from meruki_tracker.core.app import notifications as mod

    mod._failure_logged.clear()
    notifier = WebhookNotifier("http://127.0.0.1:1/hook", timeout=0.3)

    async def go() -> None:
        for _ in range(3):
            notifier.notify(Notification(title="t", body="b"))
            await asyncio.sleep(0.4)

    with caplog.at_level(logging.INFO, logger="meruki_tracker.core.app.notifications"):
        asyncio.run(go())

    hits = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(hits) == 1, [r.getMessage() for r in hits]


def test_email_enabled_but_incomplete_is_not_silent(caplog):
    """开了邮件却只填了逗号：不能静默跳过，日志里要能查到。"""
    import logging

    cfg = ChannelsConfig(email_enable=True, email_smtp_host="smtp.example.com", email_to=",,")
    with caplog.at_level(logging.INFO, logger="meruki_tracker.core.app.notifications"):
        notifier = build_notifier(cfg)

    assert isinstance(notifier, NullNotifier)
    assert any("邮件" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
