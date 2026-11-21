"""「出价被超过」的第二条信号：用本轮详情里的当前价和我的出价比。

为什么需要第二条信号：站内信由服务端判定，最准，但**会漏发**。实测订单
``DEMOORD00003`` 的 log 里只有「价格不足」，没有「出价被超过」，而它最终成交
价 5001 高于我的出价 5000——只靠消息就会漏掉。

规则的边界（2026-11-16 用 8 条历史输掉的订单逐条验证）：

- ``当前价 > 我的出价`` → **一定**被超过（6/6 无例外）；
- ``当前价 <= 我的出价`` → 什么都说明不了。8 条里有 2 条（``s1000000008``
  4100 vs 我 5000、``v1000000009`` 920 vs 我 1000）输了但最终价低于我的出价
  （可能是一口价被买走 / 出价被判「价格不足」/ 卖家取消）。所以这条规则
  **只能正向报警，不能反向推断"没被超过"**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meruki_tracker.core.adapters.meruki.api import (
    DetailInfo,
    ListItem,
    ListPage,
    MyBid,
    MyBidPage,
    _map_detail,
    _map_my_bid_page,
)
from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.db import connect
from meruki_tracker.core.adapters.store.observations import ObservationStore
from meruki_tracker.core.adapters.store.snapshot import Snapshot, SnapshotStore
from meruki_tracker.core.app.notices import KIND_OUTBID, NoticeBoard
from meruki_tracker.core.app.refresher import Refresher, RefresherOptions
from meruki_tracker.core.errors import MerukiTrackerError, ProtocolChangedError

MY_BID = 1_000.0
CURRENT = 1_200.0


# ------------------------------------------------------------------ 接口解析


def test_map_my_bid_page_reads_my_bid_and_status():
    """真机样本：竞拍中的一条，拼的是 2026-11-16 真实响应。"""
    page = _map_my_bid_page(
        {
            "TotalItem": 1,
            "TotalPage": 1,
            "CurrentPage": 1,
            "Data": [
                {
                    "Asin": "u1000000005",
                    "Id": "DEMOORD00001",
                    "JPYPrice": "6000",
                    "StatusStr": "预约出价",
                    "BidDeadline": "2026-11-15 21:06:48",
                    "Name": "Test Camera 690",
                }
            ],
        },
        1,
        status="竞拍中",
    )

    assert page.total_item == 1
    assert len(page.items) == 1
    bid = page.items[0]
    assert bid.asin == "u1000000005"
    assert bid.order_id == "DEMOORD00001"
    # 接口给的是字符串，必须转成数，否则和 bid_price 比大小会 TypeError
    assert bid.jpy_price == 6_000.0
    assert bid.status == "预约出价"
    assert page.status == "竞拍中"


def test_map_my_bid_page_tolerates_no_bids_yet():
    """一条都没出过价是**正常状态**，不能判协议异常（和关注列表的严格性不同）。"""
    page = _map_my_bid_page({"Data": None}, 1, status="竞拍中")
    assert page.items == ()
    assert page.total_item == 0

    page = _map_my_bid_page({}, 1, status="竞拍中")
    assert page.items == ()


def test_map_my_bid_page_rejects_data_without_paging():
    """有数据却没有分页字段 = 结构变了，不能瞎猜。"""
    with pytest.raises(ProtocolChangedError, match="分页字段"):
        _map_my_bid_page({"Data": [{"Asin": "A1", "JPYPrice": "100"}]}, 1, status="竞拍中")


def test_map_detail_reads_reserve_price_as_my_bid():
    """详情顶层 ``reservePrice`` 就是我的出价：真机 u1000000005 是 6000，
    与「我的竞拍」里的 ``JPYPrice`` 一致；没出过价的 5 条全是 0。"""
    info = _map_detail(
        {
            "goodsInfo": {"asin": "u1000000005", "bidPrice": 5_750},
            "reservePrice": 6_000,
            "hasCollect": True,
        }
    )
    assert info.my_bid == 6_000.0
    assert info.bid_price == 5_750.0

    never_bid = _map_detail({"goodsInfo": {"asin": "X", "bidPrice": 1_211}, "reservePrice": 0})
    assert never_bid.my_bid is None, "0 表示没出过价，归一成 None"

    missing = _map_detail({"goodsInfo": {"asin": "Y", "bidPrice": 1}})
    assert missing.my_bid is None


# ------------------------------------------------------------------ 检测逻辑


class _Api:
    """假接口：列表固定一条，详情价由测试指定，我的出价由 ``my_bids`` 给。"""

    def __init__(
        self,
        *,
        asins: tuple[str, ...] = ("A1",),
        detail_price: float = CURRENT,
        my_bids: dict[str, float] | None = None,
        bias_error: Exception | None = None,
        statuses: dict[str, str] | None = None,
    ) -> None:
        self._asins = asins
        self._price = detail_price
        self._my_bids = {"A1": MY_BID} if my_bids is None else my_bids
        self._statuses = statuses or {}
        self._bias_error = bias_error
        self.my_bid_calls = 0

    async def fetch_list_page(self, page: int, *, status: str = "进行中") -> ListPage:
        items = tuple(_item(a) for a in self._asins) if status == "进行中" else ()
        return ListPage(
            items=items,
            total_item=len(items),
            current_page=1,
            total_page=1,
            status=status,
            item_per_page=10,
            show_page_num=10,
        )

    async def fetch_detail(self, detail_url_hex: str) -> DetailInfo:
        # 真接口从 goodsInfo.asin 给 asin；这里从 hex 反推，否则镜像行拿不到 asin，
        # 价格比对会因为"对不上号"而静默跳过（第一版就是这样假绿的）。
        return DetailInfo(
            asin=detail_url_hex.removeprefix("hex"),
            end_at=None,
            ended=False,
            bid_price=self._price,
            buy_now_price=None,
            auto_extend=False,
            early_termination=False,
            name="n",
        )

    async def fetch_my_bids(self, *, status: str = "竞拍中", page: int = 1) -> MyBidPage:
        self.my_bid_calls += 1
        if self._bias_error is not None:
            raise self._bias_error
        items = tuple(
            MyBid(
                asin=asin,
                order_id=f"O{asin}",
                jpy_price=price,
                status=self._statuses.get(asin, "预约出价"),
            )
            for asin, price in self._my_bids.items()
        )
        return MyBidPage(
            items=items, total_item=len(items), current_page=1, total_page=1, status=status
        )


def _item(asin: str) -> ListItem:
    return ListItem(
        asin=asin,
        item_id="1",
        name=f"拍品{asin}",
        url=f"https://x/{asin}",
        detail_url=f"hex{asin}",
        bid_deadline="2026-12-31 21:00:00",
        image_url="",
        user_id="",
        raw={"Asin": asin},
    )


def _build(
    tmp_path: Path, api: Any, *, outbid_by_price: bool = True
) -> tuple[Refresher, NoticeBoard]:
    db = tmp_path / "meruki.db"
    conn = connect(db)
    creds = CredentialStore(db, conn=conn)
    creds.save("sid=test")
    board = NoticeBoard()
    refresher = Refresher(
        api=api,
        snapshots=SnapshotStore(db, conn=conn),
        credentials=creds,
        observations=ObservationStore(db, conn=conn),
        options=RefresherOptions(outbid_by_price=outbid_by_price),
        notices=board,
    )
    return refresher, board


async def test_price_above_my_bid_registers_notice(tmp_path: Path):
    api = _Api(detail_price=CURRENT)
    refresher, board = _build(tmp_path, api)

    _, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert api.my_bid_calls == 1, "每轮只该拉一次「我的竞拍」"

    due = board.take_due(_now())
    assert len(due) == 1, due
    notice = due[0]
    assert notice.kind == KIND_OUTBID
    assert notice.asin == "A1"
    assert "1,200" in notice.body and "1,000" in notice.body, notice.body


async def test_price_not_above_my_bid_is_silent(tmp_path: Path):
    """当前价还没超过我——什么都不说（也不能说"你还领先"，那是另一种推断）。"""
    api = _Api(detail_price=MY_BID)
    refresher, board = _build(tmp_path, api)

    _, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert board.take_due(_now()) == ()


async def test_same_my_bid_is_not_notified_twice(tmp_path: Path):
    """价格每轮都高于我的出价，但同一个出价只该响一次。"""
    api = _Api(detail_price=CURRENT)
    refresher, board = _build(tmp_path, api)

    snap, _ = await refresher.refresh(Snapshot())
    assert len(board.take_due(_now())) == 1

    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error
    assert board.take_due(_now()) == (), "同一个出价不该每轮都提醒"


async def test_raising_my_bid_allows_a_new_notice(tmp_path: Path):
    """我加了价、又被超过，这是新事件，应该再响一次。"""
    api = _Api(detail_price=CURRENT)
    refresher, board = _build(tmp_path, api)

    snap, _ = await refresher.refresh(Snapshot())
    assert len(board.take_due(_now())) == 1

    api._my_bids = {"A1": 1_100.0}  # 我加到 1100，仍低于当前价 1200
    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error
    assert len(board.take_due(_now())) == 1


async def test_switch_off_keeps_only_the_message_signal(tmp_path: Path):
    api = _Api(detail_price=CURRENT)
    refresher, board = _build(tmp_path, api, outbid_by_price=False)

    _, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert api.my_bid_calls == 0, "关掉后不该再拉「我的竞拍」"
    assert board.take_due(_now()) == ()


async def test_failed_order_list_does_not_break_the_refresh(tmp_path: Path):
    """「我的竞拍」拉不到只是这一轮不比对，绝不能让整轮刷新失败。"""
    api = _Api(detail_price=CURRENT, bias_error=MerukiTrackerError("boom"))
    refresher, board = _build(tmp_path, api)

    _, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert board.take_due(_now()) == ()


async def test_unfollowed_item_without_detail_is_skipped(tmp_path: Path):
    """我只出了价、但没关注那条拍品 → 本轮没有它的详情，就不做价格比对。

    这是这条信号的已知边界（写进 README）：价格比对只覆盖你在关注、且本轮
    真的拉了详情的拍品。没关注的出价仍然只有站内信那一条路。
    """
    api = _Api(detail_price=CURRENT, my_bids={"NOT_FOLLOWED": MY_BID})
    refresher, board = _build(tmp_path, api)

    _, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert board.take_due(_now()) == ()


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def test_order_status_alone_reports_outbid(tmp_path: Path):
    """订单状态直接写「价格被超过」时，没有详情也要报。

    这是价格比对覆盖不到的地方：我只出了价、但没关注那条拍品，本轮根本不会
    拉它的详情。2026-11-16 真机抓到过 StatusStr 从「预约出价」变成
    「价格被超过」，所以这条信号是免费的（订单列表本来就要拉）。
    """
    api = _Api(
        my_bids={"NOT_FOLLOWED": MY_BID},
        statuses={"NOT_FOLLOWED": "价格被超过"},
    )
    refresher, board = _build(tmp_path, api)

    _, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error

    due = board.take_due(_now())
    assert len(due) == 1, due
    assert due[0].asin == "NOT_FOLLOWED"
    assert "已被超过" in due[0].body, due[0].body
    assert "当前" not in due[0].body, "没有详情就不该编一个当前价出来"


async def test_stale_status_does_not_override_a_fresh_price(tmp_path: Path):
    """我刚加完价、订单状态还没刷新时，别拿滞后状态误报。

    价格是这一轮刚拉到的、更新鲜；有价格且还没被超过，就以价格为准。
    """
    api = _Api(
        detail_price=MY_BID,
        statuses={"A1": "价格被超过"},
    )
    refresher, board = _build(tmp_path, api)

    _, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert board.take_due(_now()) == ()


# ---------------------------------------------------------------- 分页


class _PagedBidApi(_Api):
    """「竞拍中」有两页：第 1 页 10 条，第 2 页 5 条（服务端每页 10 条）。"""

    def __init__(self, total: int = 15) -> None:
        super().__init__(asins=("A1",))
        self._total = total
        self.pages: list[int] = []

    async def fetch_my_bids(self, *, status: str = "竞拍中", page: int = 1) -> MyBidPage:
        self.pages.append(page)
        start = (page - 1) * 10
        asins = [f"B{i}" for i in range(start, min(start + 10, self._total))]
        items = tuple(MyBid(asin=a, order_id=f"O{a}", jpy_price=1000.0) for a in asins)
        return MyBidPage(
            items=items,
            total_item=self._total,
            current_page=page,
            total_page=(self._total + 9) // 10,
            status=status,
        )


def test_my_bids_are_read_across_all_pages(tmp_path):
    """我的出价超过 10 条时，第 2 页的出价也必须在册——否则那些拍品永远不被比对。"""
    api = _PagedBidApi(total=15)
    ref, _ = _build(tmp_path, api)

    import asyncio

    bids = asyncio.run(ref._fetch_my_bids())
    assert len(bids) == 15
    assert "B14" in bids
    assert api.pages == [1, 2]


class _PagedMessageApi:
    """站内消息共 25 条新消息（id 101..125），每页 10 条、按 id 倒序。"""

    def __init__(self) -> None:
        self.rows = [
            {
                "id": i,
                "title": f"价格被超过 XXEEJKNKKG{i:02d}",
                "name": "系统消息",
                "createdTime": "2026-11-18 10:00",
                "hasRead": False,
                "detailId": "",
            }
            for i in range(101, 126)
        ]
        self.pages: list[int] = []

    async def fetch_messages_raw(self, page: int) -> dict[str, Any]:
        self.pages.append(page)
        ordered = self.rows[::-1]
        return {
            "Data": ordered[(page - 1) * 10 : page * 10],
            "TotalItem": len(self.rows),
            "TotalPage": 3,
            "CurrentPage": page,
        }


class _WatermarkStore:
    def __init__(self, mark: int) -> None:
        self.mark = mark

    def watermark(self) -> int:
        return self.mark

    def save_round(self, rows, *, seen_at, watermark) -> int:
        return len(rows)


def _msg_refresher(api, store):
    ref = Refresher.__new__(Refresher)
    ref._api = api
    ref._messages = store
    ref._messages_enabled = True

    async def fake_resolve(notices):
        return [object() for _ in notices], {n.msg_id for n in notices}

    ref._resolve_outbid = fake_resolve
    return ref


def test_new_messages_are_followed_across_pages(tmp_path):
    """一轮来了超过 10 条消息时，中间那些「价格被超过」不能因为翻页缺失而永久丢掉。"""
    import asyncio

    api = _PagedMessageApi()
    ref = _msg_refresher(api, _WatermarkStore(100))

    _, rows, mark = asyncio.run(ref._fetch_outbid())
    assert len(rows) == 25
    assert api.pages == [1, 2, 3]
    assert mark == 125


def test_message_paging_stops_when_watermark_is_reached(tmp_path):
    """水位线已经覆盖到第 2 页时，翻到第 2 页就够，不必再多请求一页。"""
    import asyncio

    api = _PagedMessageApi()
    ref = _msg_refresher(api, _WatermarkStore(115))

    _, rows, mark = asyncio.run(ref._fetch_outbid())
    assert len(rows) == 10  # 116..125
    assert api.pages == [1]
    assert mark == 125


def test_one_broken_order_does_not_kill_the_whole_round(monkeypatch):
    """一条订单的事件解析崩了，只能跳过它自己。

    2026-11-18 审计：``parse_bid_events`` 在 ``_resolve_outbid`` 里没有兜底。
    它一抛就冒到 ``refresh`` 的 ``except Exception``——整轮记 internal 失败，
    而水位线要等落库之后才推进，于是下一轮又撞同一个订单，变成永久刷新失败
    循环（直到那条消息自己掉出第 1 页）。
    """
    import asyncio

    from meruki_tracker.core.app import refresher as refmod

    class _Api:
        async def fetch_messages_raw(self, page: int) -> dict[str, Any]:
            return {
                "Data": [
                    {
                        "id": 200,
                        "title": "价格被超过 DEMOORD00003",
                        "name": "系统消息",
                        "createdTime": "2026-11-18 10:00",
                        "hasRead": False,
                        "detailId": "",
                    }
                ],
                "TotalItem": 1,
                "TotalPage": 1,
                "CurrentPage": page,
            }

        async def fetch_order_detail_raw(self, order_id: str) -> dict[str, Any]:
            return {"url": "", "log": []}

    def boom(*args, **kwargs):
        raise RuntimeError("接口多了个字段，解析器还没跟上")

    monkeypatch.setattr(refmod, "parse_bid_events", boom)

    ref = Refresher.__new__(Refresher)
    ref._api = _Api()
    ref._messages = _WatermarkStore(100)
    ref._messages_enabled = True

    affected, rows, mark = asyncio.run(ref._fetch_outbid())

    assert rows == [] and affected == set()
    assert mark == 200  # 水位线照常推进，下一轮不会重复撞这条
