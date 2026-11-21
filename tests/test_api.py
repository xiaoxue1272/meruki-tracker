"""协议层测试：错误分域 + 真实响应样本 → 解析。"""

from __future__ import annotations

import asyncio
import json
import time
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meruki_tracker.core.adapters.meruki.api import (
    DetailInfo,
    MerukiApi,
    _map_detail,
    _map_list_page,
    _parse_body,
    _parse_end_time,
)
from meruki_tracker.core.adapters.meruki.transport import Response
from meruki_tracker.core.errors import (
    AuthExpiredError,
    NetworkError,
    ProtocolChangedError,
    WafBlockedError,
)

# 真实响应样本（2026-10-29 实测，已移除个人标识）
LIST_SAMPLE = {
    "Data": [
        {
            "Asin": "d1249426445",
            "BidDeadline": "2026-10-29 20:54:40",
            "BidDeadlineStr": "4h: 31m: 11s",
            "DetailUrl": (
                "68747470733a2f2f61756374696f6e732e7961686f6f2e636f2"
                "e6a702f6a702f61756374696f6e2f6431323439343236343435"
            ),
            "Id": "50873060",
            "Name": "テスト商品",
            "Url": "https://auctions.yahoo.co.jp/jp/auction/d1249426445",
        }
    ],
    "TotalItem": 10,
    "TotalPage": 1,
    "CurrentPage": 1,
}

DETAIL_GOODS = {
    "goodsInfo": {
        "asin": "d1249426445",
        "name": "テスト商品",
        "deadDate": "2026-10-29 20:54:40",
        "deadTimeWithZone": "2026-10-29T21:54:40+09:00",
        "autoExtend": True,
        "bidPrice": 4100,
        "buyNowPrice": 0,
    },
    "other": {"earlyTermination": "会"},
    "over": False,
}


def resp(payload, status=200, body=None) -> Response:
    raw = body if body is not None else json.dumps(payload, ensure_ascii=False).encode()
    return Response(status=status, headers=(), body=raw)


# ---------------------------------------------------------------- 错误分域


def test_ok_response_returns_data():
    assert _parse_body(resp({"code": 200, "data": {"x": 1}})) == {"x": 1}


def test_403_plain_text_is_auth_expired():
    with pytest.raises(AuthExpiredError):
        _parse_body(resp(None, status=403, body=b"forbidden"))


def test_html_login_page_is_auth_expired():
    with pytest.raises(AuthExpiredError):
        _parse_body(resp(None, status=200, body=b"<html>login</html>"))


def test_html_error_page_is_protocol_error_not_auth():
    """502 网关错误页不该让用户以为要重新登录。"""
    with pytest.raises(ProtocolChangedError):
        _parse_body(resp(None, status=200, body=b"<html><body>Bad Gateway</body></html>"))


def test_502_html_page_is_retryable_server_error():
    with pytest.raises(NetworkError):
        _parse_body(resp(None, status=502, body=b"<html><body>502 Bad Gateway</body></html>"))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_network_error_not_waf(status):
    """服务端抖动是可重试的网络错误，不能告诉用户'请求过快'，更不能判凭证失效。"""
    with pytest.raises(NetworkError):
        _parse_body(resp({"code": 0, "msg": ""}, status=status, body=b""))


def test_business_code_5xx_is_network_error():
    with pytest.raises(NetworkError):
        _parse_body(resp({"code": 500, "msg": "internal"}))


def test_unknown_business_code_is_protocol_error():
    with pytest.raises(ProtocolChangedError):
        _parse_body(resp({"code": 404, "msg": "not found"}))


def test_401_html_without_login_feature_is_auth_expired():
    with pytest.raises(AuthExpiredError):
        _parse_body(resp(None, status=401, body=b"<html>no</html>"))


def test_code_403_message_is_auth_expired():
    with pytest.raises(AuthExpiredError):
        _parse_body(resp({"code": 403, "msg": "请登录"}))


def test_code_401_is_auth_expired():
    with pytest.raises(AuthExpiredError):
        _parse_body(resp({"code": 401, "msg": "unauthorized"}))


def test_code_203_is_waf():
    with pytest.raises(WafBlockedError):
        _parse_body(resp({"code": 203, "msg": "需要验证"}))


def test_bad_json_is_protocol_error():
    with pytest.raises(ProtocolChangedError):
        _parse_body(resp(None, status=200, body=b'{"code": 200, "data": '))


def test_missing_code_is_protocol_error():
    with pytest.raises(ProtocolChangedError):
        _parse_body(resp({"msg": "hello"}))


# ---------------------------------------------------------------- 列表解析


def test_map_list_page():
    page = _map_list_page(LIST_SAMPLE, page=1)
    assert page.total_item == 10
    assert len(page.items) == 1
    assert page.items[0].asin == "d1249426445"
    assert page.items[0].detail_url_hex.startswith("6874")
    assert page.items[0].list_deadline == "2026-10-29 20:54:40"


def test_map_list_page_missing_data_is_protocol_error():
    with pytest.raises(ProtocolChangedError):
        _map_list_page({"TotalItem": 3}, page=1)


def test_map_list_page_empty_but_complete_is_valid():
    """接口正常返回空就是真的空：三字段齐全的空列表是合法观测。"""
    page = _map_list_page({"Data": [], "TotalItem": 0, "TotalPage": 1, "CurrentPage": 1}, page=1)
    assert page.items == ()
    assert page.total_item == 0
    assert page.total_page == 1


@pytest.mark.parametrize("missing", ["TotalItem", "TotalPage", "CurrentPage"])
def test_map_list_page_missing_paging_field_is_protocol_error(missing):
    """异常空壳（WAF/未登录降级）会缺分页字段，绝不能当成'列表为空 → 全部取关'。"""
    data = {"Data": [], "TotalItem": 0, "TotalPage": 1, "CurrentPage": 1}
    data.pop(missing)
    with pytest.raises(ProtocolChangedError):
        _map_list_page(data, page=1)


def test_map_list_page_paging_fields_must_be_numeric():
    with pytest.raises(ProtocolChangedError):
        _map_list_page({"Data": [], "TotalItem": "x", "TotalPage": 1, "CurrentPage": 1}, page=1)


def test_map_list_page_paging_fields_out_of_range():
    with pytest.raises(ProtocolChangedError):
        _map_list_page({"Data": [], "TotalItem": 0, "TotalPage": 0, "CurrentPage": 1}, page=1)


# ---------------------------------------------------------------- 凭证注入


class RecordingTransport:
    """记录所有出站请求的假传输。"""

    def __init__(self, response: Response | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response or resp({"code": 200, "data": {"nickname": "n"}})

    async def request(self, method, url, *, headers=None, json_body=None, timeout=20.0):
        self.calls.append({"headers": dict(headers or {}), "url": url})
        return self._response

    async def aclose(self) -> None:
        return None


def make_api(transport, cookie: str = "a=1", retry: int = 0) -> MerukiApi:
    return MerukiApi(
        transport,
        cookie=cookie,
        timeout=1.0,
        min_interval=0.0,
        jitter=0.0,
        retry=retry,
    )


@pytest.mark.parametrize("evil", ["a=1\r\nX-Injected: pwned", "a=1\nX-Injected: pwned", "a=\x001"])
async def test_cookie_with_crlf_is_rejected_before_sending(evil):
    transport = RecordingTransport()
    with pytest.raises(AuthExpiredError, match="Cookie 格式非法"):
        await make_api(transport, cookie=evil).verify()
    assert transport.calls == [], "非法 Cookie 绝不能发出去"


@pytest.mark.parametrize(
    ("name", "value"),
    [("X-A", "v\r\nX-Injected: 1"), ("X-A\r\nX-B", "v"), ("X-A", "v\x00x")],
)
def test_transport_rejects_illegal_header(name, value):
    from meruki_tracker.core.adapters.meruki.transport import validate_header

    with pytest.raises(ValueError):
        validate_header(name, value)


def test_curl_transport_never_spawns_with_illegal_header(monkeypatch):
    """curl 子进程路径：非法 header 必须在拼命令行之前被拒。"""
    import subprocess

    from meruki_tracker.core.adapters.meruki.transport import CurlSubprocessTransport

    called = []

    def fake_run(*a, **kw):  # pragma: no cover - 不应被调用
        called.append(a)
        raise AssertionError("不应启动 curl")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tr = CurlSubprocessTransport(curl_path="/usr/bin/curl")
    with pytest.raises(ValueError):
        tr._request_sync("GET", "https://example.com", {"Cookie": "a=1\r\nX: 1"}, None, 1.0)
    assert called == []


# ---------------------------------------------------------------- 节流与重试


class FlakyTransport(RecordingTransport):
    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self._errors = errors

    async def request(self, method, url, *, headers=None, json_body=None, timeout=20.0):
        self.calls.append({"headers": dict(headers or {}), "url": url})
        if self._errors:
            raise self._errors.pop(0)
        return resp({"code": 200, "data": {"nickname": "ok"}})


async def test_last_call_updated_even_when_request_raises():
    """异常时也要更新时间戳，否则重试会绕过节流。"""
    transport = FlakyTransport([NetworkError("boom")])
    api = make_api(transport)
    api._min_interval = 0.0
    with pytest.raises(NetworkError):
        await api.verify()
    assert api._last_call > 0.0


async def test_retry_with_exponential_backoff_recovers(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = FlakyTransport([NetworkError("boom"), NetworkError("boom again")])
    api = make_api(transport, retry=2)
    assert await api.verify() == "ok"
    assert len(transport.calls) == 3
    assert len(sleeps) == 2
    assert sleeps[1] >= sleeps[0], "退避必须单调递增（指数 + jitter）"


async def test_waf_gets_longer_cooldown_than_network(monkeypatch):
    calls: list[float] = []

    async def fake_sleep(d: float) -> None:
        calls.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    api = make_api(RecordingTransport(), retry=1)
    api._min_interval = 0.5
    net = api._backoff_seconds(0, NetworkError("x"))
    waf = api._backoff_seconds(0, WafBlockedError("x"))
    assert waf > net, "WAF 场景必须比普通网络错误冷得更久"


async def test_auth_error_is_not_retried():
    transport = FlakyTransport([AuthExpiredError("请登录")])
    api = make_api(transport, retry=3)
    with pytest.raises(AuthExpiredError):
        await api.verify()
    assert len(transport.calls) == 1


async def test_protocol_error_is_not_retried():
    transport = FlakyTransport([ProtocolChangedError("改版")])
    api = make_api(transport, retry=3)
    with pytest.raises(ProtocolChangedError):
        await api.verify()
    assert len(transport.calls) == 1


async def test_throttle_enforces_min_interval():
    api = make_api(RecordingTransport())
    api._min_interval = 0.05
    start = time.monotonic()
    await api.verify()
    await api.verify()
    assert time.monotonic() - start >= 0.045


# ---------------------------------------------------------------- 详情解析


def test_map_detail_uses_zoned_time():
    info = _map_detail(DETAIL_GOODS)
    assert isinstance(info, DetailInfo)
    assert info.end_at == datetime(2026, 10, 29, 12, 54, 40, tzinfo=UTC)
    assert info.auto_extend is True
    assert info.ended is False
    assert info.bid_price == 4100.0


def test_dead_date_fallback_is_beijing_time():
    """deadDate 无时区标记，按北京时间解释。"""
    ts = _parse_end_time({"deadDate": "2026-10-29 20:54:40"})
    assert ts == datetime(2026, 10, 29, 12, 54, 40, tzinfo=UTC)


def test_zoned_time_takes_priority_over_dead_date():
    ts = _parse_end_time(
        {"deadDate": "2026-10-29 20:54:40", "deadTimeWithZone": "2026-10-29T21:54:40+09:00"}
    )
    assert ts == datetime(2026, 10, 29, 12, 54, 40, tzinfo=UTC)


def test_minute_precision_field_is_not_used():
    """other.deadTime 只到分钟，不应被采用。"""
    assert _parse_end_time({"deadDate": "bad", "deadTime": "04:31:10"}) is None


def test_map_detail_missing_goods_is_protocol_error():
    with pytest.raises(ProtocolChangedError):
        _map_detail({"other": {}})


# ---------------------------------------------------------------- curl 多响应块解析


def test_parse_single_response():
    from meruki_tracker.core.adapters.meruki.transport import _parse_last_response_head

    raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
    status, headers = _parse_last_response_head(raw)
    assert status == 200
    assert ("Content-Type", "application/json") in headers


def test_parse_302_then_200_takes_last_block():
    from meruki_tracker.core.adapters.meruki.transport import _parse_last_response_head

    raw = b"HTTP/1.1 302 Found\r\nLocation: /next\r\n\r\nHTTP/1.1 200 OK\r\nX-Final: 1\r\n\r\n"
    status, headers = _parse_last_response_head(raw)
    assert status == 200
    assert headers == [("X-Final", "1")]


def test_parse_100_continue_then_200_skips_1xx():
    from meruki_tracker.core.adapters.meruki.transport import _parse_last_response_head

    raw = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nX-Final: 1\r\n\r\n"
    status, headers = _parse_last_response_head(raw)
    assert status == 200
    assert headers == [("X-Final", "1")]


def test_parse_trailing_partial_block_does_not_win():
    """-D 输出末尾可能有不完整的状态行，不能把空块当最终响应。"""
    from meruki_tracker.core.adapters.meruki.transport import _parse_last_response_head

    raw = b"HTTP/1.1 200 OK\r\nA: 1\r\n\r\n"
    status, _headers = _parse_last_response_head(raw)
    assert status == 200


def test_parse_garbage_head_gives_zero_status():
    from meruki_tracker.core.adapters.meruki.transport import _parse_last_response_head

    assert _parse_last_response_head(b"") == (0, [])
    assert _parse_last_response_head(b"HTTP/1.1 200 OK\r\n")[0] == 200


def _fake_curl(tmp_path, monkeypatch, head: bytes, body: bytes, rc: int = 0):
    """让 CurlSubprocessTransport 的 -D/-o 文件被伪造内容填充。"""
    import subprocess as sp
    import types

    def fake_run(cmd, **kw):
        head_path = Path(cmd[cmd.index("-D") + 1])
        body_path = Path(cmd[cmd.index("-o") + 1])
        head_path.write_bytes(head)
        body_path.write_bytes(body)
        return types.SimpleNamespace(returncode=rc, stdout=b"", stderr=b"")

    monkeypatch.setattr(sp, "run", fake_run)


def test_curl_transport_handles_redirect_chain(tmp_path, monkeypatch):
    """302 + 200 两段：body 只取最终响应之后的内容。"""
    from meruki_tracker.core.adapters.meruki.transport import CurlSubprocessTransport

    _fake_curl(
        tmp_path,
        monkeypatch,
        b"HTTP/1.1 302 Found\r\nLocation: /x\r\n\r\nHTTP/1.1 200 OK\r\nSet-Cookie: a=b\r\n\r\n",
        b'{"code":200,"data":{}}',
    )
    r = CurlSubprocessTransport(curl_path="/usr/bin/curl")._request_sync(
        "GET", "https://example.com", {}, None, 1.0
    )
    assert r.status == 200
    assert r.body == b'{"code":200,"data":{}}'
    assert r.headers == (("Set-Cookie", "a=b"),)


def test_curl_transport_timeout_margin_is_small(monkeypatch):
    """subprocess 超时只能是 timeout + 小余量，不能是 timeout + 15。"""
    import subprocess as sp
    import types

    from meruki_tracker.core.adapters.meruki.transport import CurlSubprocessTransport

    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        seen["max_time"] = cmd[cmd.index("--max-time") + 1]
        Path(cmd[cmd.index("-D") + 1]).write_bytes(b"HTTP/1.1 200 OK\r\n\r\n")
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(sp, "run", fake_run)
    CurlSubprocessTransport(curl_path="/usr/bin/curl")._request_sync(
        "GET", "https://example.com", {}, None, 20.0
    )
    assert seen["timeout"] == 22.0, seen
    assert seen["max_time"] == "20"


def test_curl_transport_does_not_build_header_string_from_unsanitized_input():
    """回归：CRLF 注入曾被真实复现（服务端收到 X-Injected: pwned）。"""
    from meruki_tracker.core.adapters.meruki.transport import CurlSubprocessTransport

    tr = CurlSubprocessTransport(curl_path="/usr/bin/curl")
    with pytest.raises(ValueError, match="非法字符"):
        tr._request_sync(
            "GET",
            "https://example.com",
            {"Cookie": "sid=1\r\nX-Injected: pwned"},
            None,
            1.0,
        )


# ---------------------------------------------------------------- 手机验证码登录


def test_sms_service_names_match_site():
    """接口名来自旧项目 2026-10-22 实测；写错就完全调不通。"""
    from meruki_tracker.core.adapters.meruki.api import SVC_LOGIN_BY_SMS, SVC_SEND_VERIFY_CODE

    assert SVC_SEND_VERIFY_CODE == "Sig.Front.AppApi.SendVerifyCodeActionV1"
    assert SVC_LOGIN_BY_SMS == "Sig.Front.AppApi.LoginBySMS"


def test_cookie_header_from_set_cookie():
    """登录凭证在 Set-Cookie 里，要拼成 Cookie 头。"""
    from meruki_tracker.core.adapters.meruki.api import _cookie_header_from

    headers = (
        ("Content-Type", "application/json"),
        ("Set-Cookie", "kmgSessionX=abc; Path=/; HttpOnly; Secure"),
        ("Set-Cookie", "kmgUserId=42; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT"),
    )
    assert _cookie_header_from(headers) == "kmgSessionX=abc; kmgUserId=42"


def test_cookie_header_prefers_last_value_for_same_name():
    from meruki_tracker.core.adapters.meruki.api import _cookie_header_from

    headers = (
        ("Set-Cookie", "kmgSessionT=temp; Path=/"),
        ("Set-Cookie", "kmgSessionT=real; Path=/"),
    )
    assert _cookie_header_from(headers) == "kmgSessionT=real"


def test_cookie_header_ignores_malformed():
    from meruki_tracker.core.adapters.meruki.api import _cookie_header_from

    assert _cookie_header_from((("Set-Cookie", "novalue"), ("X", "y"))) == ""


class _SmsTransport:
    """按队列返回响应的假传输，记录请求。"""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, method, url, *, headers=None, json_body=None, timeout=20.0):

        self.calls.append({"url": url, "json": json_body, "headers": dict(headers or {})})
        return self.responses.pop(0)

    async def aclose(self) -> None:
        return None


def _resp(status: int = 200, body: bytes = b'{"code":200,"data":{}}', headers=()) -> object:
    from meruki_tracker.core.adapters.meruki.transport import Response

    return Response(status=status, headers=tuple(headers), body=body)


async def test_send_sms_code_request_shape():
    from meruki_tracker.core.adapters.meruki.api import MerukiApi

    transport = _SmsTransport([_resp()])
    api = MerukiApi(transport, cookie="", min_interval=0.0)
    await api.send_sms_code("13800138000")

    call = transport.calls[0]
    assert "SendVerifyCodeActionV1" in call["url"]
    assert call["json"] == {
        "type": "SMS",
        "user": "13800138000",
        "dialCode": "+86",
        "verify": "",
        "captchaType": 0,
    }, call["json"]


async def test_login_by_sms_returns_cookie_from_set_cookie():
    from meruki_tracker.core.adapters.meruki.api import MerukiApi

    transport = _SmsTransport(
        [_resp(headers=[("Set-Cookie", "kmgSessionX=abc; Path=/; HttpOnly")])]
    )
    api = MerukiApi(transport, cookie="", min_interval=0.0)

    cookie = await api.login_by_sms("13800138000", "123456")

    assert cookie == "kmgSessionX=abc"
    call = transport.calls[0]
    assert "LoginBySMS" in call["url"]
    assert call["json"]["mobile"] == "13800138000"
    assert call["json"]["code"] == "123456"


async def test_login_by_sms_without_session_cookie_raises():
    """登录 200 但没有会话 Cookie：站点可能改版，必须明确报错而不是"成功"。"""
    from meruki_tracker.core.adapters.meruki.api import MerukiApi
    from meruki_tracker.core.errors import ProtocolChangedError

    transport = _SmsTransport([_resp()])
    api = MerukiApi(transport, cookie="", min_interval=0.0)

    with pytest.raises(ProtocolChangedError):
        await api.login_by_sms("13800138000", "123456")


async def test_login_by_sms_wrong_code_raises_auth_error():
    from meruki_tracker.core.adapters.meruki.api import MerukiApi
    from meruki_tracker.core.errors import AuthExpiredError

    transport = _SmsTransport(
        [
            _resp(
                200,
                b'{"code":403,"msg":"\xe9\xaa\x8c\xe8\xaf\x81\xe7\xa0\x81\xe9\x94\x99\xe8\xaf\xaf"}',
            )
        ]
    )
    api = MerukiApi(transport, cookie="", min_interval=0.0)

    with pytest.raises(AuthExpiredError):
        await api.login_by_sms("13800138000", "000000")


# ---------------------------------------------------------------------------
# 接口契约回归：这些行为是逆向实测出来的（见文档集 06-接口文档），
# 一旦服务端改了，这里要先红，而不是等线上数据出错才发现。
# ---------------------------------------------------------------------------


def test_list_item_keeps_all_ten_documented_fields():
    """列表行有 10 个字段，少一个都可能让镜像层缺数据。"""
    row = {
        "Asin": "v1000000001",
        "BidDeadline": "2026-10-31 21:02:09",
        "BidDeadlineStr": "3h: 7m: 19s",
        "CreatedTime": "2026-10-30 13:08:27",
        "DetailUrl": (
            "68747470733a2f2f61756374696f6e732e7961686f6f2e636f2"
            "e6a702f6a702f61756374696f6e2f7631323530323230343935"
        ),
        "Id": "50988630",
        "ImageUrl": "https://auctions.c.yimg.jp/x.jpg",
        "Name": "テスト",
        "Url": "https://auctions.yahoo.co.jp/jp/auction/v1000000001",
        "UserId": "9999999",
    }
    page = _map_list_page(
        {"Data": [row], "TotalItem": 1, "TotalPage": 1, "CurrentPage": 1, "ItemPerPage": 10},
        page=1,
    )
    item = page.items[0]
    assert len(row) == 10
    assert item.asin == "v1000000001"
    assert item.item_id == "50988630"
    assert item.bid_deadline_str == "3h: 7m: 19s"
    assert item.created_time == "2026-10-30 13:08:27"
    assert item.image_url == "https://auctions.c.yimg.jp/x.jpg"
    assert item.user_id == "9999999"
    assert item.url == "https://auctions.yahoo.co.jp/jp/auction/v1000000001"
    assert item.raw["Id"] == "50988630"


def test_asin_may_have_no_letter_prefix():
    """约 6% 的 Asin 是纯数字（实测 7/115），不能假设首字符是字母。"""
    plain = {
        "Asin": "1000000002",
        "Name": "x",
        "Url": "https://auctions.yahoo.co.jp/jp/auction/1000000002",
    }
    page = _map_list_page(
        {"Data": [plain], "TotalItem": 1, "TotalPage": 1, "CurrentPage": 1},
        page=1,
    )
    assert page.items[0].asin == "1000000002"


def test_keyword_zero_result_shape_is_protocol_error():
    """零结果时服务端只回 4 个字段（Data 是 null），必须判协议异常。

    这是文档集 06-接口文档 §2.1 记的行为：`keyword` 命中不到时
    TotalItem/TotalPage 直接消失，Data 变 null——和"列表为空"不是一回事。
    """
    shell = {"ItemPerPage": 10, "ShowPageNum": 10, "CurrentPage": 1, "Data": None}
    with pytest.raises(ProtocolChangedError):
        _map_list_page(shell, page=1)


def test_status_is_carried_back_into_page():
    """status 落库用于分桶，必须原样带回，不能写死'进行中'。"""
    for status in ("进行中", "已结束"):
        page = _map_list_page(
            {"Data": [], "TotalItem": 0, "TotalPage": 1, "CurrentPage": 1},
            page=1,
            status=status,
        )
        assert page.status == status


def test_paging_metadata_is_optional():
    """ItemPerPage/ShowPageNum 缺失不算异常——它们只用于诊断。"""
    page = _map_list_page({"Data": [], "TotalItem": 0, "TotalPage": 1, "CurrentPage": 1}, page=1)
    assert page.item_per_page == 0
    assert page.show_page_num == 0


def test_list_deadline_is_site_local_without_timezone():
    """BidDeadline 是北京时间但**不带时区标记**，原样保留不做推断。"""
    page = _map_list_page(LIST_SAMPLE, page=1)
    assert page.items[0].bid_deadline == "2026-10-29 20:54:40"
    assert "+" not in page.items[0].bid_deadline
    assert page.items[0].bid_deadline.endswith(":40")


class _BodyRecordingTransport:
    """记录请求体，用于断言我们实际发出去的参数形状。"""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.urls: list[str] = []

    async def request(self, method, url, *, headers=None, json_body=None, timeout=20.0):
        self.bodies.append(dict(json_body or {}))
        self.urls.append(url)
        return resp(
            {
                "code": 200,
                "data": {"Data": [], "TotalItem": 0, "TotalPage": 1, "CurrentPage": 1},
            }
        )

    async def aclose(self) -> None:
        return None


async def test_fetch_list_page_sends_page_as_json_number():
    """page 必须是 JSON 数字——实测传字符串会 code 400 "无法解析参数"。"""
    transport = _BodyRecordingTransport()
    api = MerukiApi(transport, cookie="a=1", min_interval=0.0, jitter=0.0)
    await api.fetch_list_page(3)
    body = transport.bodies[0]
    assert body["page"] == 3
    assert isinstance(body["page"], int), "page 传字符串会被服务端以模糊报错拒绝"
    assert body["keyword"] == "", "keyword 非空会改变返回形状（缺分页字段）"
    assert body["status"] == "进行中"


async def test_fetch_list_page_passes_status_through():
    """分桶要靠 status 参数，写死成'进行中'会让「已结束」查证失效。"""
    transport = _BodyRecordingTransport()
    api = MerukiApi(transport, cookie="a=1", min_interval=0.0, jitter=0.0)
    await api.fetch_list_page(1, status="已结束")
    assert transport.bodies[0]["status"] == "已结束"


async def test_fetch_list_page_works_without_cookie():
    """关注列表公开与否是服务端定的，客户端不该自己先拦（实测无 Cookie 会拿到 code 403）。"""
    transport = _BodyRecordingTransport()
    api = MerukiApi(transport, cookie="", min_interval=0.0, jitter=0.0)
    await api.fetch_list_page(1)
    assert transport.bodies, "无 Cookie 也应真的发出去，由服务端决定是否拒绝"


def test_zero_prices_are_normalised_to_missing():
    """接口用 0 表示"没有这个价"，不是真的 0 円。

    一口价商品的 ``bidPrice`` 恒为 0、普通拍卖的 ``buyNowPrice`` 恒为 0。
    原样存进库会让下游每个 ``is not None`` 判断都以为"有价"——真实数据里
    已经出现过 ``buy_now_price=0.0``，只是显示层恰好按 falsy 处理了才没出错。
    """
    from meruki_tracker.core.adapters.meruki.api import _as_price

    assert _as_price(0) is None
    assert _as_price(0.0) is None
    assert _as_price("0") is None
    assert _as_price(None) is None
    assert _as_price("") is None
    assert _as_price(1500) == 1500.0
    assert _as_price("1500") == 1500.0


def test_detail_mapping_treats_zero_buy_now_as_absent():
    """端到端：``buyNowPrice=0`` 解析出来必须是 None，不是 0.0。"""
    from meruki_tracker.core.adapters.meruki.api import _map_detail

    data = {
        "over": False,
        "other": {"earlyTermination": None},
        "goodsInfo": {
            "asin": "A1",
            "name": "普通拍卖",
            "bidPrice": 3200,
            "buyNowPrice": 0,
            "deadTimeWithZone": "2026-11-05T21:00:00+09:00",
        },
    }
    info = _map_detail(data)
    assert info.bid_price == 3200.0
    assert info.buy_now_price is None, f"0 应该归一成 None，实际 {info.buy_now_price!r}"


def test_curl_transport_follows_redirects(tmp_path, monkeypatch):
    """curl 命令行必须带 -L：不跟重定向时 30x 就是终态、body 为空。

    2026-11-18 审计：``_build_cmd`` 以前没有 ``-L``，而 httpx 通道是
    ``follow_redirects=True``。cookie 过期被 302 到登录页、站点做 http→https
    或区域跳转时，curl 只写一段 302、body 留空，``_parse_body`` 只能报
    「响应不是合法 JSON」→ 用户看到「站点改版」而不是「登录过期」。
    """
    from meruki_tracker.core.adapters.meruki.transport import CurlSubprocessTransport

    cmd: list[str] = []

    def fake_run(argv, **kw):
        cmd.extend(argv)
        Path(argv[argv.index("-D") + 1]).write_bytes(b"HTTP/1.1 200 OK\r\n\r\n")
        Path(argv[argv.index("-o") + 1]).write_bytes(b'{"code":200,"data":{}}')
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)
    CurlSubprocessTransport(curl_path="/usr/bin/curl")._request_sync(
        "POST", "https://example.com", {}, {"a": 1}, 1.0
    )

    assert "-L" in cmd, "curl 必须跟随重定向"
    assert cmd[cmd.index("--max-redirs") + 1] == "3"
    # POST 跟完重定向不能降级成 GET，否则所有 RPC 都会变成「参数丢了」
    assert "--post301" in cmd and "--post302" in cmd and "--post303" in cmd


def test_fully_unparsable_list_page_is_a_protocol_error():
    """Data 里有行、但一条 Asin 都解析不出来 → 必须报协议异常。

    2026-11-18 审计：行级 ``continue`` 造出"分页齐全 + items 为空"，上游会当成
    "这一页到底了、本轮观测完整"，``diff_list_rounds`` 随即把整张表判成
    disappeared（每轮刷出几百条假事件）。
    """
    from meruki_tracker.core.adapters.meruki.api import _map_list_page
    from meruki_tracker.core.errors import ProtocolChangedError

    base = {"TotalItem": 2, "TotalPage": 1, "CurrentPage": 1}
    with pytest.raises(ProtocolChangedError):
        _map_list_page({"Data": [{"asin": "A1"}], **base}, 1)  # 字段改名
    with pytest.raises(ProtocolChangedError):
        _map_list_page({"Data": [["A1"]], **base}, 1)  # 行变成数组
    # 真的空页不能误报：那是合法的"没有更多了"
    assert len(_map_list_page({"Data": [], **base}, 1).items) == 0
    # 个别行坏就跳过，整页照样能用
    assert len(_map_list_page({"Data": [{"Asin": "OK"}, {"bad": 1}], **base}, 1).items) == 1


def test_detail_keeps_zero_buy_now_price_so_it_can_be_cleared():
    """``buyNowPrice=0`` 不能被归一成 None，否则旧的一口价永远清不掉。

    2026-11-18 审计：``_as_price`` 把 0 变 None，而 ``apply_detail`` 的判据是
    ``detail.buy_now_price is not None``——卖家撤掉即决价后旧价永久残留。
    """
    from meruki_tracker.core.adapters.meruki.api import _map_detail
    from meruki_tracker.core.domain.detail import DetailResult, apply_detail
    from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery

    info = _map_detail(
        {"goodsInfo": {"asin": "A1", "bidPrice": 100, "buyNowPrice": 0, "name": "x"}}
    )
    # 价还是归一成 None（0 不是真价格），但"字段在"这件事必须记下来，
    # 否则 apply_detail 分不清"卖家撤了"和"这次没拿到"。
    assert info.buy_now_price is None
    assert info.buy_now_price_present is True

    now = datetime(2026, 11, 18, tzinfo=UTC)
    state = AuctionState(
        asin="A1",
        name="x",
        url="u",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.pending(),
        buy_now_price=3000.0,
    )
    cleared, _ = apply_detail(
        state, DetailResult(failed=False, buy_now_price=None, buy_now_price_present=True), now
    )
    assert cleared.buy_now_price is None
    # 没带这个字段时保持原值不动
    kept, _ = apply_detail(state, DetailResult(failed=False, buy_now_price=None), now)
    assert kept.buy_now_price == 3000.0


def test_naive_deadline_with_zone_is_read_as_japan_time():
    """``deadTimeWithZone`` 万一没有时区标记，必须按日本时间解释。

    2026-11-18 审计：裸 ``astimezone(UTC)`` 对 naive 值按**宿主机时区**解释，
    TZ=UTC / Tokyo / Shanghai 三种环境算出的结束时间差 8-9 小时。
    """
    from meruki_tracker.core.adapters.meruki.api import _parse_end_time

    assert _parse_end_time({"deadTimeWithZone": "2026-11-15T22:06:48"}) == datetime(
        2026, 11, 15, 13, 6, 48, tzinfo=UTC
    )
    # 带 +09:00 的权威值不受影响
    assert _parse_end_time({"deadTimeWithZone": "2026-11-15T22:06:48+09:00"}) == datetime(
        2026, 11, 15, 13, 6, 48, tzinfo=UTC
    )
