"""站内消息里的「出价被超过」：把服务端通知变成可用的出价事件。

**为什么需要这一层**

挖煤姬服务端会主动给用户写站内消息：你在某个拍品上出过价，别人超过你，
它就发一条「您的拍卖 XXXX 价格被超过」。这是服务端**已经替我们算好**的事实，
比我们自己轮询详情、比对 `bidPrice` 去猜"是不是被人超了"要准得多，也便宜得多
（一次 ``GetMessageWeb`` 拿 10 条，而拉一次详情要 22KB）。

但消息本身不好直接用，有三个坑：

1. **标题里的 ID 不是 asin**。标题是 ``您的拍卖 DEMOORD00002 价格被超过``，
   那串 12 位大写是**订单短 ID**，跟关注列表里的 asin（``j1000000012`` 这种）
   完全不是一个体系。要靠 ``AppYahooAcOrder.Detail`` 的 ``url`` 字段
   （hex 编码的雅虎地址）解出来——本模块的 :func:`asin_from_yahoo_url` 干这个。

2. **消息没有价格**。``content`` 只有一句「您的订单价格不足」，不含出价金额、
   不含当前价。所以消息只能当**触发器**（"这个拍品有情况，去拉一次详情"），
   不能当价格来源。

3. **只有出过价的拍品才会有**。只关注不出价，永远收不到。

**这不是"推送"**

服务端不推、前端也不轮询（整个站点 401 个 chunk 里没有任何 WebSocket / SSE /
消息未读轮询）。消息是用户打开消息页时才读一次的。我们主动轮询
``GetMessageWeb`` 只是为了及时拿到这个**已经存在的事实**，本质上还是拉取。

本模块是纯函数、零 I/O：只做"从 JSON 里认出哪些是我们关心的"和"解析 ID"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .text import scrub

#: 标题里出现这个子串才当作出价事件
TITLE_OUTBID = "价格被超过"

#: 标题里的订单短 ID：连续 12 位大写字母/数字。
#: 实测 ``DEMOORD00002`` / ``DEMOORD00003`` / ``DEMOORD00004`` 都符合。
_ORDER_ID_RE = re.compile(r"\b([A-Z0-9]{12})\b")

#: 雅虎拍卖详情地址：``https://auctions.yahoo.co.jp/jp/auction/<asin>``。
#: asin 允许带单字母前缀（``j1000000012``），也允许纯数字（实测约 6% 是纯数字）。
_YAHOO_AUCTION_RE = re.compile(r"/auction/([A-Za-z]?\d+)/?$")


@dataclass(frozen=True, slots=True)
class MessageNotice:
    """一条站内消息（只保留我们真正会用的字段）。"""

    msg_id: int
    category: str
    title: str
    created_at: str
    has_read: bool
    detail_id: str

    @property
    def order_id(self) -> str:
        """从标题里抠出 12 位订单短 ID；抠不到返回空串。"""
        match = _ORDER_ID_RE.search(self.title)
        return match.group(1) if match else ""

    @property
    def is_outbid(self) -> bool:
        """是不是「出价被超过」类消息。"""
        return TITLE_OUTBID in self.title


@dataclass(frozen=True, slots=True)
class BidEvent:
    """一条出价事件（来自订单详情的 ``log`` 数组）。"""

    order_id: str
    asin: str
    event: str
    happened_at: str

    @property
    def is_outbid(self) -> bool:
        return self.event == "出价被超过"


def parse_messages(data: Any) -> list[MessageNotice]:
    """把 ``GetMessageWeb`` 的一页结果解析成 :class:`MessageNotice` 列表。

    接口返回 ``data.Data`` 数组，每条 8 个字段。缺字段/类型不对的一律跳过——
    消息列表是"锦上添花"的信息源，不能因为它字段变了就让整轮刷新失败。

    Args:
        data: ``GetMessageWeb`` 响应里的 ``data`` 对象。

    Returns:
        解析出的消息列表（顺序保持接口原序，即 id 从新到旧）。
    """
    if not isinstance(data, dict):
        return []
    rows = data.get("Data")
    if not isinstance(rows, list):
        return []

    out: list[MessageNotice] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            msg_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        # 消息的每个字符串字段都来自网络，都会流进通知正文/落库后被显示。
        # 和拍品名一样先 scrub()：ESC 序列能让终端清屏、改标题，而消息来源
        # 不由我们控制（审计遗留 2，2026-11-18）。
        title = scrub(str(row.get("title") or ""))
        # 标题为空的消息确实存在（实测 30 条里有 2 条），没有标题就没法判断类型
        if not title.strip():
            continue
        out.append(
            MessageNotice(
                msg_id=msg_id,
                category=scrub(str(row.get("name") or "")),
                title=title,
                created_at=scrub(str(row.get("createdTime") or "")),
                has_read=bool(row.get("hasRead")),
                detail_id=scrub(str(row.get("detailId") or "")),
            )
        )
    return out


def pick_new(notices: list[MessageNotice], *, since_id: int) -> list[MessageNotice]:
    """挑出比本地水位线新的消息，按 id 从旧到新排。

    从旧到新是为了让调用方按时间顺序处理——同一拍品可能连发多条
    （实测一次竞拍里被超 6 次就有 6 条），倒序处理会让"最新状态"被旧消息覆盖。
    """
    fresh = [n for n in notices if n.msg_id > since_id]
    return sorted(fresh, key=lambda n: n.msg_id)


def asin_from_yahoo_url(raw: str) -> str:
    """从雅虎拍卖地址里取 asin。

    ``AppYahooAcOrder.Detail`` 返回的 ``url`` 是 hex 编码的完整地址，例如
    ``68747470733a2f2f...2f31303030303030303032`` →
    ``https://auctions.yahoo.co.jp/jp/auction/1000000002``。

    也接受已经解码好的明文地址（方便测试与将来接口改回明文）。

    Args:
        raw: hex 编码字符串或明文 URL。

    Returns:
        asin（可能带单字母前缀）；解析不出来返回空串。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "/" not in text:
        # 看起来是 hex：解一下。解不开就当作没拿到
        try:
            text = bytes.fromhex(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    path = urlparse(text).path or text
    match = _YAHOO_AUCTION_RE.search(path)
    return match.group(1) if match else ""


def parse_bid_events(order_id: str, data: Any) -> list[BidEvent]:
    """把 ``AppYahooAcOrder.Detail`` 的 ``log`` 数组解析成出价事件。

    实测的 ``log`` 长这样（新到旧）：

    .. code-block:: json

        [{"Event": "竞拍失败",   "Time": "2026-10-30 03:19"},
         {"Event": "出价被超过", "Time": "2026-10-29 21:30"},
         {"Event": "出价成功",   "Time": "2026-10-29 21:24"},
         {"Event": "价格不足",   "Time": "2026-10-29 21:22"},
         {"Event": "用户取消",   "Time": "2026-10-29 15:50"}]

    ``价格不足`` → ``出价成功`` → ``出价被超过`` 这个顺序正好对上站内消息的
    时间戳，说明服务端是在**竞拍进行中**实时记录的，不是结束后补发。

    Args:
        order_id: 订单短 ID（标题里那 12 位）。
        data: ``AppYahooAcOrder.Detail`` 响应里的 ``data`` 对象。

    Returns:
        出价事件列表（按发生时间从旧到新）。asin 解析不出来时返回空列表——
        没有 asin 的事件对调用方没用。
    """
    if not isinstance(data, dict):
        return []
    asin = asin_from_yahoo_url(str(data.get("url") or ""))
    if not asin:
        return []
    raw_log = data.get("log")
    if not isinstance(raw_log, list):
        return []

    events: list[BidEvent] = []
    for row in raw_log:
        if not isinstance(row, dict):
            continue
        event = str(row.get("Event") or "").strip()
        if not event:
            continue
        events.append(
            BidEvent(
                order_id=order_id,
                asin=asin,
                event=event,
                happened_at=str(row.get("Time") or ""),
            )
        )
    events.sort(key=lambda e: e.happened_at)
    return events
