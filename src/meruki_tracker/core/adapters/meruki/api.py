"""meruki RPC 协议层：请求构造 + 错误分域 + 节流。

真实协议（2026-10-29 实测）：
- 列表 Sig.Front.User.AppYahooAcOrder.CollectionList
  参数 {"status": "进行中"|"已结束", "page": N, "keyword": ""}
  返回 data.Data[] 每条含 Asin / BidDeadline / Name / Url / DetailUrl(hex) / Id 等
- 详情 Sig.Front.SubSite.AppYahoo.Detail（GET，参数 url=hex(详情url)）
  返回 data.goodsInfo.deadDate / deadTimeWithZone / autoExtend / earlyTermination 等
- 未登录返回 {"code":403,"msg":"请登录"}
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from ...errors import AuthExpiredError, NetworkError, ProtocolChangedError, WafBlockedError
from ...logging_setup import get_logger

log = get_logger("api")

#: 雅虎日本站的时区。列表接口的 ``BidDeadline`` 用的是**北京时间**（+08），
#: 详情接口的 ``deadTimeWithZone`` 用 +09:00，同一个结束时刻在两个字段里
#: 差一个小时——这是站点的历史包袱，不是我们算错。
SITE_JST = timezone(timedelta(hours=9))

API_HOST = "https://web.meruki.cn/"
SITE_ORIGIN = "https://www.meruki.cn"

SVC_COLLECTION_LIST = "Sig.Front.User.AppYahooAcOrder.CollectionList"
SVC_DETAIL = "Sig.Front.SubSite.AppYahoo.Detail"
SVC_USER_INFO = "Sig.Front.User.AppUserSetting.GetUserInfo"
# 站内消息列表（分页）：{"page":1}。三类消息混在一起，不支持按类型过滤。
SVC_MESSAGES = "Sig.Front.User.AppUser.GetMessageWeb"
# 「我的竞拍」订单列表（分页）：POST {"page":1,"status":"竞拍中","sort":"subtimedesc"}。
# 这是"我拍了什么"的权威来源，也是「出价被超过」价格比对的基准
# （``JPYPrice`` = 我的出价）。2026-11-16 实测 ``竞拍中`` / ``已得标`` /
# ``未得标`` 三档都能返回数据，早期文档记的"竞拍中恒为 Data: null"已过时。
SVC_ORDER_LIST = "Sig.Front.User.AppYahooAcOrder.List"
# 单个订单详情（含 log 事件流）：GET {"id":"<12 位订单短 ID>"}。
# 注意参数要的是列表行里的短 Id（DEMOORD00005），不是 OrderId（DEMOORDER00000001），
# 传 OrderId 会返回 code 500 "服务器错误"。
SVC_ORDER_DETAIL = "Sig.Front.User.AppYahooAcOrder.Detail"
# 翻译（标题 + 描述一起翻，POST）：
# {"formatType":"text"|"html","sourceLanguage":"ja","targetLanguage":"zh-CN",
#  "titleText":...,"descText":...} → {"title":...,"desc":...}
# 实测：不需要 Cookie 也能用；单次内容上限约 10000 字符，超了直接 500。
SVC_TRANSLATE = "Sig.Front.User.Translate.TranslateHtml"

# 登录相关（2026-10-22 旧项目实测，本项目 2026-10-30 复用）
# 发送验证码：{"type":"SMS","user":手机号,"dialCode":"+86","verify":"","captchaType":0}
SVC_SEND_VERIFY_CODE = "Sig.Front.AppApi.SendVerifyCodeActionV1"
# 短信登录：{"mobile":手机号,"code":验证码,"verify":null,"captchaType":0}
# 凭证在响应 Set-Cookie 里（kmgSession*，约 365 天），不在 body
SVC_LOGIN_BY_SMS = "Sig.Front.AppApi.LoginBySMS"

#: 短信登录默认国家码
DEFAULT_DIAL_CODE = "+86"

STATUS_ONGOING = "进行中"
STATUS_ENDED = "已结束"
#: 「我的竞拍」里还在进行中的那一档（我出过价、还没结标）。
STATUS_BIDDING = "竞拍中"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_AUTH_MSG = ("请登录", "未登录", "登录已失效", "登录过期", "会话已过期", "unauthorized")


def _looks_like_auth_msg(msg: str) -> bool:
    """判断业务 msg 是否表示"凭证失效"。

    统一按小写比较：服务端可能返回 ``Unauthorized`` / ``UNAUTHORIZED`` 等大小写
    变体，直接 ``in`` 匹配会漏判，导致本应提示"重新登录"的场景被当成未知错误。
    """
    low = msg.lower()
    return any(marker in low for marker in _AUTH_MSG)


#: HTML 页面里出现这些特征才判"凭证失效"（其余 HTML 归协议异常，别误导用户重新登录）。
#: 英文特征用词边界匹配：裸子串 ``login`` 会命中 ``catalogindex`` / ``catalogin`` 这类
#: 正常页面内容，从而把普通错误页误报成"请重新登录"。
_LOGIN_URL_MARKERS = re.compile(r"(?<![a-z0-9])(login|signin|sign-in|passport)(?![a-z0-9])")
_LOGIN_TEXT_MARKERS = ("请登录", "登录页", "重新登录", "登录已失效", "登录超时")

#: 列表接口正常响应（含真空列表）必然齐全的分页字段
_PAGING_FIELDS = ("TotalItem", "TotalPage", "CurrentPage")

#: WAF 冷却系数：命中 WAF 后停顿 min_interval 的这么多倍
_WAF_COOLDOWN_FACTOR = 10.0

#: 重试退避基准（秒）与上限（秒）
_RETRY_BASE = 0.5
_RETRY_CAP = 8.0
_WAF_RETRY_BASE = 3.0
_WAF_RETRY_CAP = 30.0


@dataclass(frozen=True, slots=True)
class ListItem:
    """列表接口返回的一条拍品——**字段与接口一一对应，一个都不丢**。

    2026-10-31 实测 ``Data[]`` 每条恰好 10 个字段：

    ==================  ================================================
    ``Asin``            雅虎全局拍品号（``v1000000001`` 这种），跨账号稳定
    ``Id``              挖煤姬收藏表自增主键（纯数字），列表按它倒序
    ``Name``            标题
    ``Url``             拍品页地址
    ``DetailUrl``       详情 RPC 要用的 hex 编码地址
    ``BidDeadline``     结束时间，站点本地时间（+08）字符串，无时区
    ``BidDeadlineStr``  倒计时文案（``"18h: 13m:"``），有时残缺，仅展示
    ``CreatedTime``     这条收藏的创建时间
    ``ImageUrl``        主图地址
    ``UserId``          卖家 ID
    ==================  ================================================

    以前只留 6 个字段，把 ``BidDeadlineStr`` / ``CreatedTime`` / ``ImageUrl`` /
    ``UserId`` 直接扔了。现在全留，并且额外带上 ``raw``（原始 dict）：
    本地库要能回答"接口当时到底回了什么"，而不只是"我们当初想存什么"。
    """

    asin: str
    item_id: str = ""
    name: str = ""
    url: str = ""
    detail_url: str = ""
    bid_deadline: str = ""
    bid_deadline_str: str = ""
    created_time: str = ""
    image_url: str = ""
    user_id: str = ""
    #: 接口原始 JSON（服务端加字段时无需改表也能留存）
    raw: Mapping[str, Any] = field(default_factory=dict)

    #: 别名：语义更直白的结束时间原文（沿用旧属性名，避免大面积改调用点）
    @property
    def list_deadline(self) -> str:
        return self.bid_deadline

    @property
    def detail_url_hex(self) -> str:
        return self.detail_url


@dataclass(frozen=True, slots=True)
class ListPage:
    """列表接口的一页——分页元信息同样一个不丢。

    ``ItemPerPage`` / ``ShowPageNum`` 是接口真实返回的顶层字段，以前直接丢弃。
    它们不是判断"数据是否可信"的必要条件（那是 ``TotalItem`` / ``TotalPage`` /
    ``CurrentPage`` 三件套的事），但既然接口回了，本地就该有。
    """

    items: tuple[ListItem, ...]
    total_item: int
    current_page: int
    total_page: int
    status: str = STATUS_ONGOING
    item_per_page: int = 0
    show_page_num: int = 0


@dataclass(frozen=True, slots=True)
class MyBid:
    """「我的竞拍」里的一条出价记录——**我拍了什么、出了多少**。

    这是「出价被超过」价格比对的基准：``jpy_price`` 是我的出价（预约价/最高价），
    拿它和详情里的当前价 ``bidPrice`` 比，就能知道有没有人超过我。站内消息只是
    服务端的"顺带通知"，会漏发（实测有输了却没收到消息的订单），所以两条都要。

    Attributes:
        asin: 拍品号。
        order_id: 订单短 ID（``DEMOORD00001`` 这种），详情接口要的就是它。
        jpy_price: **我的出价**（日元）。接口给的是字符串（``'6000'``），这里已转数。
            注意与已结束订单的 ``FinalPrice``（成交价）区分，两者不是一回事。
        status: 服务端文案（``预约出价`` / ``未得标`` / ``订单已处理`` 等）。
        deadline: 结标时间原文（站点本地时间，无时区）。
        name: 标题（部分档位没有）。
    """

    asin: str
    order_id: str = ""
    jpy_price: float | None = None
    status: str = ""
    deadline: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class MyBidPage:
    """「我的竞拍」一页。"""

    items: tuple[MyBid, ...]
    total_item: int
    current_page: int
    total_page: int
    status: str = STATUS_BIDDING


@dataclass(frozen=True, slots=True)
class DetailInfo:
    """详情接口解析结果。时间以 deadTimeWithZone 为准。"""

    asin: str
    end_at: datetime | None
    ended: bool
    bid_price: float | None
    buy_now_price: float | None
    auto_extend: bool
    early_termination: bool
    name: str
    #: ``goodsInfo`` 里**有没有** ``buyNowPrice`` 这个键。
    #:
    #: 光看 ``buy_now_price`` 分不清两件事：卖家撤掉了即决价（接口给 0，被
    #: 归一成 ``None``）与这次响应压根没带这个字段（也是 ``None``）。前者必须
    #: 把本地旧价清掉，后者要保持原值不动，所以额外记这个标记。
    buy_now_price_present: bool = False
    #: 我此刻**是否还关注**这个拍品（顶层的 ``hasCollect``）。
    #:
    #: ``None`` = 响应里没这个字段（老接口 / 异常响应），此时**不能**据此判断用户
    #: 有没有取关，调用方必须保持原状态。``False`` 才是"确实不在关注列表里了"。
    #:
    #: 为什么这个字段重要：列表接口只给「进行中」，拍品从里面消失后，本地分不清
    #: 是结束了还是被取关了，以前只能一直轮询下去。``hasCollect`` 直接给出答案，
    #: 而且**与拍卖是否结束完全正交**——2026-11-15 实测：一条已结束很久、用户刚
    #: 取消关注的拍品 ``hasCollect=False`` 且 ``over=False``（雅虎上又上架了），
    #: 而 21 条已结束但仍在关注的拍品全是 ``True``。
    #:
    #: **不能用 ``collectInfo`` 判**：从未关注过的拍品上它也是非空 dict（实测 23/23），
    #: 那是"商品信息"，不是"收藏关系"。
    collected: bool | None = None
    #: **我的出价**（顶层的 ``reservePrice``），没出过价是 0 → 归一成 ``None``。
    #:
    #: 2026-11-16 两次实测的结论——**只能当线索，不能当基准**：
    #:
    #: - 领先时：``u1000000005`` 是 ``reservePrice=6000``，与「我的竞拍」订单
    #:   列表里的 ``JPYPrice=6000`` 一致；
    #: - **被别人超过之后就变成 0/缺失**（同一条拍品、同一天，订单列表仍是
    #:   6000，详情却是 ``None``），已结束的样本也全是 0。
    #:
    #: 所以"我的出价"的权威来源只有 :meth:`MerukiApi.fetch_my_bids`
    #: （``Sig.Front.User.AppYahooAcOrder.List``）；这个字段只适合在恰好拉到
    #: 详情时做个旁证，别拿它做长期基准或事后回放。
    #:
    #: 另外 ``isAsinActiveByUser`` 靠不住——我在拍的拍品上它也是 ``False``，
    #: 不要拿它判断"我是否参拍"。
    my_bid: float | None = None
    #: 接口原始 data（落 ``detail_items.raw_json``，服务端加字段不用改表）
    raw: Mapping[str, Any] = field(default_factory=dict)


class MerukiApi:
    """meruki RPC 客户端（单账号实例）。"""

    def __init__(
        self,
        transport: Any,
        *,
        cookie: str,
        timeout: float = 20.0,
        min_interval: float = 0.5,
        jitter: float = 0.2,
        retry: int = 0,
    ) -> None:
        self._transport = transport
        self.cookie = cookie
        self.timeout = timeout
        self._min_interval = min_interval
        self._jitter = jitter
        self._retry = max(0, retry)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    # ---------------------------------------------------------------- 公共

    async def fetch_list_page(self, page: int, *, status: str = STATUS_ONGOING) -> ListPage:
        data = await self._call(
            SVC_COLLECTION_LIST,
            {"status": status, "page": page, "keyword": ""},
        )
        return _map_list_page(data, page, status=status)

    async def fetch_detail(self, detail_url_hex: str) -> DetailInfo:
        raw = await self._call_get(SVC_DETAIL, {"url": detail_url_hex})
        return _map_detail(raw)

    async def fetch_messages_raw(self, page: int = 1) -> dict[str, Any]:
        """拉一页站内消息，返回原始 data（不做解析）。

        消息分三类混在一起（``拍卖信息`` / ``通知信息`` / ``交易信息``），接口
        **不支持按类型过滤**——传 ``{"name": "拍卖信息"}`` 和 ``{"type": ...}``
        都跟不传一样（实测 TotalItem 与返回内容完全相同）。所以要靠调用方自己
        在标题里过滤。

        Args:
            page: 页号，从 1 开始。

        Returns:
            原始 data 对象，含 ``Data[]`` / ``TotalItem`` / ``TotalPage`` 等。
        """
        return await self._call(SVC_MESSAGES, {"page": page})

    async def translate(
        self,
        *,
        title: str = "",
        desc: str = "",
        target: str = "zh-CN",
        source: str = "ja",
    ) -> tuple[str, str]:
        """翻译标题与描述，返回 ``(标题译, 描述译)``。

        为什么走服务端而不是本地词典：这是挖煤姬 App 里那个翻译功能背后的接口
        （前端 chunk ``P6yP6oRX.js`` 调的同一个服务名），已经是人话，不需要
        自己接翻译引擎。

        实测（2026-11-05）：
        - **不需要登录**也能用——它翻译的是公开的商品文案；
        - 单次内容上限约 10000 字符，超了服务端返回 code 500；
        - ``target`` 传 ``en`` / ``zh-CN`` / ``zh-TW`` 都能翻。

        Args:
            title: 商品标题（可空）。
            desc: 商品描述（可空，调用方负责先用 ``clip_for_translation`` 截断）。
            target: 目标语言。
            source: 源语言。

        Returns:
            ``(标题译文, 描述译文)``。任一侧为空表示接口没给译文。

        Raises:
            ProtocolChangedError: 响应里既没有 title 也没有 desc。
        """
        data = await self._call(
            SVC_TRANSLATE,
            {
                "formatType": "text",
                "sourceLanguage": source,
                "targetLanguage": target,
                "titleText": title,
                "descText": desc,
            },
        )
        got_title = str(data.get("title") or "")
        got_desc = str(data.get("desc") or "")
        if not got_title and not got_desc:
            raise ProtocolChangedError("翻译接口返回空结果（标题与描述都没有）")
        return got_title, got_desc

    async def fetch_order_detail_raw(self, order_id: str) -> dict[str, Any]:
        """拉单个订单详情，返回原始 data（含 ``log`` 事件流）。

        这是把「站内消息标题里的订单短 ID」翻译成 asin 的**唯一**途径：
        ``data["url"]`` 是 hex 编码的雅虎拍卖地址，解出来末段就是 asin。

        Args:
            order_id: 12 位订单短 ID（如 ``DEMOORD00002``）。注意不是
                ``OrderId``——传长的那种会得到 ``code 500 "服务器错误"``。

        Returns:
            原始 data 对象。
        """
        return await self._call_get(SVC_ORDER_DETAIL, {"id": order_id})

    async def fetch_my_bids(self, *, status: str = STATUS_BIDDING, page: int = 1) -> MyBidPage:
        """拉一页「我的竞拍」——我出过价的东西都在这里。

        用途有两个：①「我拍了什么」的权威名册（含我的出价金额）；②给
        「出价被超过」的价格比对提供基准 ``jpy_price``。

        ``status`` 三档都可用：``竞拍中``（还在进行）、``已得标``、``未得标``。
        默认只拉进行中的那一档——已结束的订单对"有没有人超过我"没意义，而
        它们的 ``JPYPrice`` 与成交价 ``FinalPrice`` 不同，混用会算错。

        Args:
            status: 分桶，默认 :data:`STATUS_BIDDING`。
            page: 页号，从 1 开始。

        Returns:
            解析好的一页。用户从没出过价时 ``items`` 为空、分页字段为 0。

        Raises:
            ProtocolChangedError: 响应结构不符（分页字段缺失但有数据）。
        """
        data = await self._call(SVC_ORDER_LIST, {"status": status, "page": page})
        return _map_my_bid_page(data, page, status=status)

    async def verify(self) -> str:
        """探活：返回用户昵称（成功即凭证有效）。"""
        data = await self._call(SVC_USER_INFO, {})
        return str(data.get("nickname") or data.get("Nickname") or "")

    # ---------------------------------------------------------------- 手机验证码登录

    async def send_sms_code(self, mobile: str, *, dial_code: str = DEFAULT_DIAL_CODE) -> None:
        """给手机号发送登录验证码。

        Raises:
            WafBlockedError: 触发风控（需人工处理）。
            NetworkError: 网络问题，可重试。
            ProtocolChangedError: 接口结构变化。
        """
        await self._rpc(
            SVC_SEND_VERIFY_CODE,
            "POST",
            {"type": "SMS", "user": mobile, "dialCode": dial_code, "verify": "", "captchaType": 0},
        )

    async def login_by_sms(self, mobile: str, code: str) -> str:
        """用手机号 + 验证码登录，返回**新的 Cookie 头**。

        凭证不在响应体里，而在 ``Set-Cookie``（``kmgSession*``）。这里把登录
        响应里的 cookie 全部收集起来拼成 Cookie 头，交给上层验证并保存。

        Raises:
            AuthExpiredError: 验证码错误/过期（提示重新输入或重发）。
            WafBlockedError: 触发风控。
            ProtocolChangedError: 登录成功但没拿到会话 Cookie（站点可能改版）。
        """
        raw = await self._request_raw(
            SVC_LOGIN_BY_SMS,
            "POST",
            {"mobile": mobile, "code": code, "verify": None, "captchaType": 0},
        )
        cookie = _cookie_header_from(raw.headers)
        if not cookie:
            raise ProtocolChangedError(
                "登录响应里没有会话 Cookie（期望 Set-Cookie 含 kmgSession*），站点可能已改版"
            )
        return cookie

    async def aclose(self) -> None:
        """释放传输资源。"""
        await self._transport.aclose()

    # ---------------------------------------------------------------- RPC 机制

    async def _call(self, service: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc(service, "POST", params)

    async def _call_get(self, service: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc(service, "GET", params)

    def _prepare(
        self, service: str, method: str, params: dict[str, Any]
    ) -> tuple[str, dict[str, str], dict[str, Any] | None]:
        """把 (服务名, 方法, 参数) 变成 (url, headers, json_body)。

        GET 的参数进 query（全部转字符串，接口不认数字），POST 的参数进 body。
        这段以前在 ``_rpc`` 和 ``_request_raw`` 里各抄了一份，改协议头得改两处。
        """
        query = {"n": service, "from": "RYM-PC", "isNew": "18", "language": "zh-CN"}
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-Hans;q=0.9",
            "Origin": SITE_ORIGIN,
            "Referer": SITE_ORIGIN + "/",
        }
        json_body: dict[str, Any] | None = None
        if method == "GET":
            query.update({k: str(v) for k, v in params.items() if v is not None})
        else:
            json_body = {k: v for k, v in params.items() if v is not None}
            headers["Content-Type"] = "application/json;charset=UTF-8"
        return API_HOST + "?" + urllib.parse.urlencode(query), headers, json_body

    async def _rpc(self, service: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url, headers, json_body = self._prepare(service, method, params)
        self._check_cookie()

        last: Exception | None = None
        for attempt in range(self._retry + 1):
            attempt_headers = dict(headers)
            if self.cookie:
                attempt_headers["Cookie"] = self.cookie
            try:
                async with self._lock:
                    await self._throttle()
                    try:
                        resp = await self._transport.request(
                            method,
                            url,
                            headers=attempt_headers,
                            json_body=json_body,
                            timeout=self.timeout,
                        )
                    finally:
                        # 失败也要记时间，否则重试会绕过节流
                        self._last_call = time.monotonic()
                return _parse_body(resp)
            except AuthExpiredError:
                raise
            except ProtocolChangedError:
                raise
            except (WafBlockedError, NetworkError) as exc:
                last = exc
                if attempt >= self._retry:
                    raise  # 重抛最后一次异常；显式 raise，不靠 assert（python -O 会剥掉）
                await asyncio.sleep(self._backoff_seconds(attempt, exc))
        # 不可达：循环要么 return，要么在最后一次异常时 raise。留一个显式异常，
        # 免得将来有人改坏循环条件后静默吞掉异常。
        raise RuntimeError("重试循环意外结束") from last

    async def _request_raw(self, service: str, method: str, params: dict[str, Any]) -> Any:
        """发请求并返回**原始 Response**（需要读 Set-Cookie 时用）。

        登录接口的凭证在响应头里，而 ``_rpc`` 只返回解析后的 data，所以这里
        单独走一遍；错误分派复用 ``_parse_body``，行为与普通请求一致。
        """
        url, headers, json_body = self._prepare(service, method, params)
        self._check_cookie()

        async with self._lock:
            await self._throttle()
            try:
                resp = await self._transport.request(
                    method, url, headers=headers, json_body=json_body, timeout=self.timeout
                )
            finally:
                self._last_call = time.monotonic()
        # 统一错误分派：4xx/5xx 或业务码异常在这里抛出
        _parse_body(resp)
        return resp

    def _check_cookie(self) -> None:
        """Cookie 头绝不能带 CR/LF/NUL——否则可注入任意 HTTP 头。

        Raises:
            AuthExpiredError: Cookie 格式非法（脏数据，需重新登录）。
        """
        if not self.cookie:
            return
        for bad, shown in (("\r", "\\r"), ("\n", "\\n"), ("\x00", "\\x00")):
            if bad in self.cookie:
                raise AuthExpiredError(f"Cookie 格式非法（含 {shown}），请重新登录")

    async def _throttle(self, *, penalty: float = 0.0) -> None:
        """最小请求间隔 + 抖动 + 追加冷却（WAF 命中后由调用方抬高 penalty）。"""
        gap = time.monotonic() - self._last_call
        interval = self._min_interval
        if self._jitter > 0:
            interval *= 1 + random.uniform(-self._jitter, self._jitter)
        interval += penalty
        if gap < interval:
            await asyncio.sleep(interval - gap)

    def _backoff_seconds(self, attempt: int, exc: Exception) -> float:
        """指数退避 + jitter；WAF 起点更高、上限更大（越被拦越要慢）。"""
        if isinstance(exc, WafBlockedError):
            base, cap, kind = _WAF_RETRY_BASE, _WAF_RETRY_CAP, "waf"
        else:
            base, cap, kind = _RETRY_BASE, _RETRY_CAP, "network"
        raw = min(cap, base * (2**attempt))
        delay = raw * (1 + random.uniform(0, 0.5))
        if kind == "waf":
            # WAF 冷却按"间隔倍数"补一次，避免与 _throttle 的普通间隔叠加得过于剧烈
            delay = max(delay, self._min_interval * _WAF_COOLDOWN_FACTOR)
        return delay


# ---------------------------------------------------------------- 响应解析


def _looks_like_login_page(text: str) -> bool:
    """HTML 是否带登录特征。不带特征的 HTML（如 502 错误页）不是凭证问题。"""
    low = text.lower()
    if _LOGIN_URL_MARKERS.search(low):
        return True
    return any(marker in low for marker in _LOGIN_TEXT_MARKERS)


def _parse_body(resp: Any) -> dict[str, Any]:
    """按错误域解析响应体。

    分流顺序（先看传输层 status，再看业务码，最后才看正文形态）：

    1. status >= 500 → NetworkError（服务端抖动，可重试，绝不说"凭证失效"）
    2. status 401/403 → AuthExpiredError
    3. 正文是 HTML → 有登录特征才 AuthExpiredError，否则 ProtocolChangedError
    4. 正文是 JSON → 看 code（401/403/"请登录" → Auth；203 → WAF；5xx → Network；
       其余非 200 → ProtocolChangedError）

    Raises:
        AuthExpiredError: 凭证失效（只有明确证据才这么判）。
        ProtocolChangedError: 结构不符 / 站点改版。
        NetworkError: 服务端错误（5xx / 限流 429）。
        WafBlockedError: 疑似被拦截。
    """
    raw = resp.text.strip()
    status = int(getattr(resp, "status", 0) or 0)

    if status == 429:
        raise NetworkError("请求过于频繁（HTTP 429），稍后重试")
    if status >= 500:
        raise NetworkError(f"服务端错误（HTTP {status}），稍后重试")

    if status in (401, 403):
        raise AuthExpiredError(f"HTTP {status}，凭证可能已失效")

    if raw.lower().startswith(("<!doctype", "<html", "<")):
        if _looks_like_login_page(raw):
            raise AuthExpiredError(f"返回登录相关页面（HTTP {status}），凭证可能已失效")
        raise ProtocolChangedError(f"返回 HTML 页面而非 JSON（HTTP {status}）")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        if status >= 400:
            raise NetworkError(f"HTTP {status} 且响应不是 JSON: {raw[:120]!r}") from exc
        raise ProtocolChangedError(f"响应不是合法 JSON: {raw[:120]!r}") from exc
    if not isinstance(payload, dict):
        raise ProtocolChangedError("响应根节点不是对象")

    code = payload.get("code")
    if code == 200:
        data = payload.get("data")
        if data is None:
            return {}
        if not isinstance(data, dict):
            return {"_raw": data}
        return data

    msg = str(payload.get("msg") or payload.get("message") or "")
    if code in (401, 403) or _looks_like_auth_msg(msg):
        raise AuthExpiredError(f"接口返回未授权 (code={code}): {msg}")
    if code == 203:
        raise WafBlockedError(f"风控要求人机验证 (code=203): {msg}")
    if code is None:
        raise ProtocolChangedError(f"响应缺少 code 字段: {raw[:120]!r}")
    if isinstance(code, int) and code >= 500:
        raise NetworkError(f"服务端错误 (code={code}): {msg}")
    raise ProtocolChangedError(f"接口返回未预期业务码 (code={code}): {msg}")


def _map_list_page(data: dict[str, Any], page: int, *, status: str = STATUS_ONGOING) -> ListPage:
    """解析列表页。

    正常情况下（包括用户真的没有关注任何拍品、列表为空）响应一定带
    TotalItem / TotalPage / CurrentPage 三个字段。缺任何一个都说明这不是
    正常响应（WAF 降级页、未登录空壳、服务端抖动），必须判协议异常：
    绝不能把异常空壳当成"列表为空 → 已全部取关"的完整观测。

    ``ItemPerPage`` / ``ShowPageNum`` 缺失不算异常——它们只用于展示与诊断，
    不参与"数据可不可信"的判断。

    Args:
        data: 接口返回的 data 字典。
        page: 本次请求的页码。**只用于报错定位**——分页以响应里的
            ``CurrentPage`` 为准；把"我要的是第 3 页、回来的却是别的"写进异常，
            翻页出问题时才查得动。
        status: 请求时用的 status 分桶，原样带回便于落库。

    Raises:
        ProtocolChangedError: Data 不是数组，或分页字段缺失/非法。
    """
    items_raw = data.get("Data")
    if not isinstance(items_raw, list):
        raise ProtocolChangedError(
            f"列表第 {page} 页响应缺少 Data 数组（实际键: {sorted(data)[:10]}）"
        )

    missing = [f for f in _PAGING_FIELDS if data.get(f) is None]
    if missing:
        raise ProtocolChangedError(
            f"列表第 {page} 页响应缺少分页字段 {missing}（实际键: {sorted(data)[:10]}），"
            "疑似异常空壳，本轮数据不可信"
        )
    try:
        total_item = int(data["TotalItem"])
        total_page = int(data["TotalPage"])
        current_page = int(data["CurrentPage"])
    except (TypeError, ValueError) as exc:
        raise ProtocolChangedError(f"列表第 {page} 页分页字段不是整数: {exc}") from exc
    if total_page < 1 or current_page < 1 or total_item < 0:
        raise ProtocolChangedError(
            f"列表第 {page} 页分页字段越界: TotalItem={total_item}"
            f" TotalPage={total_page} CurrentPage={current_page}"
        )
    items = []
    skipped = 0
    for item in items_raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        asin = str(item.get("Asin") or "")
        if not asin:
            skipped += 1
            continue
        items.append(
            ListItem(
                asin=asin,
                item_id=str(item.get("Id") or ""),
                name=str(item.get("Name") or ""),
                url=str(item.get("Url") or ""),
                detail_url=str(item.get("DetailUrl") or ""),
                bid_deadline=str(item.get("BidDeadline") or ""),
                bid_deadline_str=str(item.get("BidDeadlineStr") or ""),
                created_time=str(item.get("CreatedTime") or ""),
                image_url=str(item.get("ImageUrl") or ""),
                user_id=str(item.get("UserId") or ""),
                raw=dict(item),
            )
        )
    # 行级静默跳过会**骗过整条链路**：Data 里有行、却一条 Asin 都没解析出来时，
    # 上游拿到的是"分页齐全 + items 为空"，会判成"这一页到底了、本轮观测完整"，
    # 于是 ``diff_list_rounds`` 把整张表判成 disappeared（每轮刷出几百条假事件）。
    # 字段改名 / 行变成数组 / 行里混 null 都能走到这里，必须显式喊出来。
    if items_raw and not items:
        raise ProtocolChangedError(
            f"列表第 {page} 页有 {len(items_raw)} 行，但一条 Asin 都没解析出来（结构可能变了）"
        )
    if skipped:
        # 少数行坏不影响整页：跳过它们，但留痕（静默吞掉会让"少了一条"永远查不到）。
        log.warning("列表第 %d 页有 %d 行解析不出 Asin，已跳过", page, skipped)
    return ListPage(
        items=tuple(items),
        total_item=total_item,
        current_page=current_page,
        total_page=total_page,
        status=status,
        item_per_page=_as_int(data.get("ItemPerPage")),
        show_page_num=_as_int(data.get("ShowPageNum")),
    )


def _map_my_bid_page(data: dict[str, Any], page: int, *, status: str) -> MyBidPage:
    """解析「我的竞拍」一页。

    与 :func:`_map_list_page` 的差别是**更宽容**：一次都没出过价时，服务端可能
    只回一个空壳（``Data: null`` 连分页字段都没有）。对这块数据来说"我没有出价"
    是完全正常的状态，不该判协议异常——不像关注列表，那里空列表关系到"是不是
    全被取关了"，必须严格。所以这里的规则是：

    - ``Data`` 非空 → 分页字段必须齐全，否则判协议异常（结构变了，不能瞎猜）；
    - ``Data`` 为空/为 null → 接受，分页字段缺就按 0 处理。

    Args:
        data: 接口返回的 data。
        page: 请求页码（只用于报错定位）。
        status: 请求用的分桶，原样带回。

    Raises:
        ProtocolChangedError: 有数据但分页字段缺失，或 ``Data`` 不是数组。
    """
    raw = data.get("Data")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ProtocolChangedError(
            f"我的竞拍第 {page} 页的 Data 不是数组（实际类型: {type(raw).__name__}）"
        )

    def _int_field(name: str) -> int:
        try:
            return int(data.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    if raw:
        missing = [f for f in _PAGING_FIELDS if data.get(f) is None]
        if missing:
            raise ProtocolChangedError(
                f"我的竞拍第 {page} 页有 {len(raw)} 条数据却缺少分页字段 {missing}"
                f"（实际键: {sorted(data)[:10]}），疑似异常响应"
            )

    items = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        asin = str(row.get("Asin") or "").strip()
        if not asin:
            continue
        items.append(
            MyBid(
                asin=asin,
                order_id=str(row.get("Id") or "").strip(),
                jpy_price=_as_price(row.get("JPYPrice")),
                status=str(row.get("StatusStr") or ""),
                deadline=str(row.get("BidDeadline") or ""),
                name=str(row.get("Name") or ""),
            )
        )
    return MyBidPage(
        items=tuple(items),
        total_item=max(0, _int_field("TotalItem")),
        current_page=max(1, _int_field("CurrentPage") or page),
        total_page=max(1, _int_field("TotalPage") or 1),
        status=status,
    )


def _map_detail(data: dict[str, Any]) -> DetailInfo:
    goods = data.get("goodsInfo")
    if not isinstance(goods, dict):
        raise ProtocolChangedError(f"详情响应缺少 goodsInfo（实际键: {sorted(data)[:12]}）")

    end_at = _parse_end_time(goods)
    return DetailInfo(
        asin=str(goods.get("asin") or ""),
        end_at=end_at,
        ended=bool(data.get("over")),
        bid_price=_as_price(goods.get("bidPrice")),
        buy_now_price=_as_price(goods.get("buyNowPrice")),
        # 光有 buy_now_price 还不够：0 被归一成 None，而"卖家撤掉了即决价"
        # 和"这次响应没这个字段"都会得到 None。多带一个"字段在不在"，
        # 下游才能把**撤掉**（清空旧价）和**没拿到**（保持原值）区分开。
        buy_now_price_present="buyNowPrice" in goods,
        auto_extend=str(goods.get("autoExtend")) in ("会", "True", "true"),
        early_termination=str(
            data.get("other", {}).get("earlyTermination")
            if isinstance(data.get("other"), dict)
            else ""
        )
        in ("会", "True", "true"),
        name=str(goods.get("name") or ""),
        # 缺字段时留 None（不是 False）：老接口/异常响应没说"没关注"，
        # 当成 False 会把还在关注的拍品误标成已取关。
        collected=_optional_bool(data.get("hasCollect")),
        # 顶层 reservePrice = 我的最高出价；0/缺失统一成 None（没出过价）
        my_bid=_as_price(data.get("reservePrice")),
        raw=data,
    )


def _optional_bool(value: Any) -> bool | None:
    """把接口里的 bool 字段转成 ``bool | None``（缺字段/无法识别 → None）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return None


def _parse_end_time(goods: dict[str, Any]) -> datetime | None:
    """解析结束时间。

    优先级（实测 2026-10-29）：
    1. deadTimeWithZone = "2026-10-29T21:54:40+09:00"  —— 带时区，唯一权威
    2. deadDate        = "2026-10-29 20:54:40"          —— 北京时间，无时区标记
    3. other.deadTime  = "2026-10-29 21:54(日本时间)"   —— 只到分钟，不用
    """
    zone = goods.get("deadTimeWithZone")
    if isinstance(zone, str) and zone:
        try:
            dt = datetime.fromisoformat(zone.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                # 字段名叫 WithZone，但万一给的是裸时间：**必须按日本时间解释**。
                # ``astimezone(UTC)`` 对 naive 值是按宿主机时区解释的，换个 TZ
                # 环境变量结果就差 8-9 小时——这种"只在别人机器上错"的 bug
                # 最难查。真实样本全是 +09:00，这里只是兜底。
                log.warning("deadTimeWithZone 没有时区标记（%r），按日本时间解释", zone)
                dt = dt.replace(tzinfo=SITE_JST)
            return dt.astimezone(UTC)
        except ValueError:
            pass

    raw = goods.get("deadDate")
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
        except ValueError:
            pass
    return None


def _as_float(value: Any) -> float | None:
    """把"可能是数字的字符串"转成 float；**非有限值一律给 None**。

    ``float("NaN")`` / ``float("1e400")`` 都不会抛异常，却会一路变成
    ``nan`` / ``inf``：存进 SQLite 时 NaN 变 NULL（读回来是 None，内存与库不
    一致），inf 能存但显示成「¥inf」，按价格排序时 `nan` 比较恒 False。这些值
    在真实接口里只会来自坏数据，当"没有这个数"处理最安全。
    """
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return amount


def _as_price(value: Any) -> float | None:
    """价格字段：**0 归一成 None**。

    接口用 0 表示"没有这个价"（一口价商品的 ``bidPrice`` 恒为 0，普通拍卖的
    ``buyNowPrice`` 恒为 0），不是真的 0 円。原样存进库会让下游每个
    ``is not None`` 判断都以为"有价"，只是恰好显示层又按 falsy 处理了才没出错
    ——那是运气，不是设计。
    """
    amount = _as_float(value)
    # 负价同样当"没有"：真实接口不会给负数，出现就说明这条数据已经不可信，
    # 显示成「¥-5」只会让人以为程序算错了。
    if amount is None or amount <= 0:
        return None
    return amount


def _as_int(value: Any, default: int = 0) -> int:
    """把接口里的"可能是数字也可能是字符串"的值转成 int。

    分页元信息（ItemPerPage / ShowPageNum）缺失或非数字时**不报错**——
    它们不参与可信度判断，缺了就当 0 处理。
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cookie_header_from(headers: tuple[tuple[str, str], ...]) -> str:
    """从响应头里拼出 Cookie 头。

    ``Set-Cookie`` 每条都可能带属性（Path / Domain / Expires …），这里只取
    ``name=value`` 部分；同名 cookie 保留**最后一个**——登录响应可能先设一个
    临时会话再覆盖成正式会话。
    """
    pairs: dict[str, str] = {}
    for name, value in headers:
        if name.lower() != "set-cookie":
            continue
        head = value.split(";", 1)[0].strip()
        if "=" not in head:
            continue
        k, v = head.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            pairs[k] = v
    return "; ".join(f"{k}={v}" for k, v in pairs.items())
