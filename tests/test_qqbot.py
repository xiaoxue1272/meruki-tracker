"""QQ 官方机器人扫码绑定：线格式、状态机、秘钥解密。

协议是从官方 npm 包 ``@tencent-connect/qqbot-connector@1.2.0`` 里读出来的，这里用
**真的本地 HTTP 服务**对着测——这些接口的坑都在 body 字段名和状态码上，mock 掉就什么
都测不到。
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from meruki_tracker.core.app import qqbot


class _Sink:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, dict] = {}
        self.status_code = 200
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                path = self.path.split("?")[0]
                sink.calls.append({"path": path, "payload": json.loads(raw) if raw else {}})
                body = json.dumps(sink.responses.get(path, {})).encode()
                self.send_response(sink.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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


def test_create_bind_task_posts_a_random_key(sink):
    sink.responses["/lite/create_bind_task"] = {"retcode": 0, "data": {"task_id": "T-1"}}
    task = qqbot.create_bind_task(host=sink.url)

    assert task.task_id == "T-1"
    sent_key = sink.last()["payload"]["key"]
    # key 是 32 字节 base64，既是请求参数也是后面解 AppSecret 的 AES 密钥
    assert len(base64.b64decode(sent_key)) == 32
    assert task.key == sent_key


def test_create_bind_task_surfaces_business_errors(sink):
    sink.responses["/lite/create_bind_task"] = {"retcode": 100, "msg": "频率限制"}
    with pytest.raises(qqbot.QqBindError, match="频率限制"):
        qqbot.create_bind_task(host=sink.url)


def test_create_bind_task_requires_a_task_id(sink):
    sink.responses["/lite/create_bind_task"] = {"retcode": 0, "data": {}}
    with pytest.raises(qqbot.QqBindError, match="task_id"):
        qqbot.create_bind_task(host=sink.url)


def test_poll_bind_task_parses_status_and_fields(sink):
    sink.responses["/lite/poll_bind_result"] = {
        "retcode": 0,
        "data": {
            "status": 2,
            "bot_appid": 123456,
            "bot_encrypt_secret": "xxx",
            "user_openid": "openid-1",
        },
    }
    result = qqbot.poll_bind_task("T-1", host=sink.url)

    assert sink.last()["payload"] == {"task_id": "T-1"}
    assert result.status == qqbot.STATUS_COMPLETED
    assert result.app_id == "123456"  # 服务端给的是数字，统一成字符串
    assert result.encrypt_secret == "xxx"
    assert result.user_openid == "openid-1"


def test_poll_bind_task_defaults_to_none_status(sink):
    sink.responses["/lite/poll_bind_result"] = {"retcode": 0}
    assert qqbot.poll_bind_task("T-1", host=sink.url).status == qqbot.STATUS_NONE


def test_connect_url_carries_task_and_source():
    url = qqbot.connect_url("T-1", source="meruki watch")
    assert url.startswith("https://q.qq.com/qqbot/openclaw/connect.html?")
    assert "task_id=T-1" in url
    assert "source=meruki%20watch" in url  # 必须转义，空格直接拼会坏
    assert "_wv=2" in url


def test_decrypt_secret_round_trips():
    key = qqbot.generate_key()
    blob = qqbot.encrypt_secret_for_test("APP-SECRET", key)
    assert qqbot.decrypt_secret(blob, key) == "APP-SECRET"


def test_decrypt_secret_rejects_the_wrong_key():
    blob = qqbot.encrypt_secret_for_test("APP-SECRET", qqbot.generate_key())
    with pytest.raises(qqbot.QqBindError, match="解不开"):
        qqbot.decrypt_secret(blob, qqbot.generate_key())


def test_decrypt_secret_rejects_garbage():
    with pytest.raises(qqbot.QqBindError):
        qqbot.decrypt_secret("not-base64!!", qqbot.generate_key())


def test_http_errors_are_reported_with_the_body(sink):
    sink.status_code = 502
    with pytest.raises(qqbot.QqBindError, match="HTTP 502"):
        qqbot.create_bind_task(host=sink.url)


def test_unreachable_host_is_reported():
    with pytest.raises(qqbot.QqBindError, match="连不上"):
        qqbot.create_bind_task(host="http://127.0.0.1:1", timeout=2.0)


def test_provider_walks_the_whole_bind_flow(sink):
    """一条完整链路：取码 → 待扫 → 已扫 → 完成并解出 AppSecret。

    这条用例守的是「key 要按 task_id 存住、成功时用它解密」——把 key 丢了，
    扫码流程看起来成功，实际换不回能用的凭据。
    """
    from meruki_tracker.core.app.qrlogin import (
        STATE_PENDING,
        STATE_SCANNED,
        STATE_SUCCESS,
        QqProvider,
    )

    sink.responses["/lite/create_bind_task"] = {"retcode": 0, "data": {"task_id": "T-9"}}
    provider = QqProvider(host=sink.url)
    challenge = provider.fetch()

    assert challenge.kind == "qq"
    assert "task_id=T-9" in challenge.payload
    assert challenge.token == "T-9"
    # 密钥只活在内存里，不能进二维码 URL
    key = provider._keys["T-9"]
    assert key not in challenge.payload

    sink.responses["/lite/poll_bind_result"] = {"retcode": 0, "data": {"status": 0}}
    assert provider.poll(challenge).state == STATE_PENDING

    sink.responses["/lite/poll_bind_result"] = {"retcode": 0, "data": {"status": 1}}
    assert provider.poll(challenge).state == STATE_SCANNED

    sink.responses["/lite/poll_bind_result"] = {
        "retcode": 0,
        "data": {
            "status": 2,
            "bot_appid": "1020001",
            "bot_encrypt_secret": qqbot.encrypt_secret_for_test("SEC-REAL", key),
        },
    }
    done = provider.poll(challenge)
    assert done.state == STATE_SUCCESS
    assert done.account == "1020001"
    assert json.loads(done.credential) == {"app_id": "1020001", "app_secret": "SEC-REAL"}
    # 用掉就丢，避免内存里长期留着一把没用的密钥
    assert "T-9" not in provider._keys


def test_provider_reports_expiry_and_bad_credentials(sink):
    from meruki_tracker.core.app.qrlogin import STATE_FAILED, QqProvider

    sink.responses["/lite/create_bind_task"] = {"retcode": 0, "data": {"task_id": "T-2"}}
    provider = QqProvider(host=sink.url)
    challenge = provider.fetch()

    sink.responses["/lite/poll_bind_result"] = {"retcode": 0, "data": {"status": 3}}
    assert provider.poll(challenge).state == STATE_FAILED

    # 完成但没给加密凭据
    sink.responses["/lite/poll_bind_result"] = {
        "retcode": 0,
        "data": {"status": 2, "bot_appid": "1"},
    }
    failed = provider.poll(challenge)
    assert failed.state == STATE_FAILED
    assert "重新取码" in failed.message


def test_provider_fails_loudly_when_the_key_is_gone(sink):
    """key 丢了不能静默返回一个假凭据——那会让用户以为挂好了。"""
    from meruki_tracker.core.app.qrlogin import STATE_FAILED, QqProvider

    sink.responses["/lite/create_bind_task"] = {"retcode": 0, "data": {"task_id": "T-3"}}
    provider = QqProvider(host=sink.url)
    challenge = provider.fetch()
    provider._keys.clear()  # 模拟取了第二次码

    sink.responses["/lite/poll_bind_result"] = {
        "retcode": 0,
        "data": {"status": 2, "bot_appid": "1", "bot_encrypt_secret": "any"},
    }
    failed = provider.poll(challenge)
    assert failed.state == STATE_FAILED
    assert "密钥" in failed.message
