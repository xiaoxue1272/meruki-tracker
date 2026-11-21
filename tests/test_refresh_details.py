"""每轮刷新都把关注中的每一条详情拉一遍，结束由详情判。

用户 2026-11-15 定调：「关注列表去拉取全量的拍品…每条挨个去请求详情，从详情
判断是否结束」。以前的"手动全拉 / 定时只补该补的"两套逻辑、以及"从进行中列表
消失就判结束"的推断，都删掉了。

这些用例守的就是这条新契约：不再有"这一轮拉不拉"的开关，也不再有推断式终态。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from meruki_tracker.core.adapters.meruki.api import DetailInfo, ListItem, ListPage, MyBidPage
from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.db import connect
from meruki_tracker.core.adapters.store.mirror import MirrorStore
from meruki_tracker.core.adapters.store.observations import ObservationStore
from meruki_tracker.core.adapters.store.snapshot import Snapshot, SnapshotStore
from meruki_tracker.core.app.refresher import Refresher, RefresherOptions
from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery

NOW = datetime.now(UTC)


class _Api:
    """列表固定返回两条进行中的拍品，详情每次成功并回一个递增的价格。"""

    def __init__(self, asins: tuple[str, ...] = ("A1", "A2")) -> None:
        self.detail_calls: list[str] = []
        self._asins = asins
        self._price = 1000.0

    async def fetch_list_page(self, page: int, *, status: str = "进行中") -> ListPage:
        items = tuple(_item(a, f"拍品{a}") for a in self._asins)
        if status != "进行中":
            items = ()
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
        self.detail_calls.append(detail_url_hex)
        self._price += 1
        return DetailInfo(
            asin="",
            # 结束时间给远一点：别落进"到点待确认"，那会让每个拍品都被选中，
            # 测不出"全量"和"按复查点"的区别。
            end_at=datetime.now(UTC) + timedelta(days=3),
            ended=False,
            bid_price=self._price,
            buy_now_price=None,
            auto_extend=False,
            early_termination=None,
            name="n",
        )

    async def fetch_my_bids(self, *, status: str = "竞拍中", page: int = 1):
        """这些用例不关心「我的竞拍」：假装我一条都没出过价。"""
        return MyBidPage(items=(), total_item=0, current_page=1, total_page=1, status=status)

    async def aclose(self) -> None:
        return None


def _item(asin: str, name: str) -> ListItem:
    return ListItem(
        asin=asin,
        item_id="1",
        name=name,
        url=f"https://x/{asin}",
        detail_url=f"hex{asin}",
        bid_deadline="2026-12-31 21:00:00",
        bid_deadline_str="1h: 00m: 00s",
        created_time="2026-10-01 10:00:00",
        image_url=f"https://img/{asin}.jpg",
        user_id="9999999",
        raw={"Asin": asin},
    )


def _build(tmp_path: Path, api: Any) -> tuple[Refresher, SnapshotStore]:
    db = tmp_path / "meruki.db"
    conn = connect(db)
    creds = CredentialStore(db, conn=conn)
    creds.save("sid=test")
    snapshots = SnapshotStore(db, conn=conn)
    refresher = Refresher(
        api=api,
        snapshots=snapshots,
        credentials=creds,
        observations=ObservationStore(db, conn=conn),
        mirror=MirrorStore(db, conn=conn),
        options=RefresherOptions(),
    )
    return refresher, snapshots


async def test_every_refresh_fetches_every_detail(tmp_path: Path):
    """每一轮都把关注中的每一条详情拉一遍——手动、定时没有区别。"""
    api = _Api()
    refresher, _ = _build(tmp_path, api)

    snap, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert sorted(api.detail_calls) == ["hexA1", "hexA2"], api.detail_calls
    api.detail_calls.clear()

    # 第二轮：两条都有精确时间了，以前这种"没人到复查点"的轮次一条都不拉
    snap, result = await refresher.refresh(snap)

    assert result.ok, result.error
    assert sorted(api.detail_calls) == ["hexA1", "hexA2"], api.detail_calls
    assert len(snap.auctions) == 2


async def test_ended_items_are_not_swept_again(tmp_path: Path):
    """已结束的拍品不再拉详情（成交价不会再变）。

    但它**怎么**变成已结束的，现在只有一条路：详情说 ``over``。
    """
    api = _Api(asins=("L1",))
    refresher, _ = _build(tmp_path, api)

    snap, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert snap.auctions[0].door_state is DoorState.ACTIVE

    # 详情开始报「结束了」——这才是终态的唯一来源
    real_end = datetime.now(UTC)
    calls: list[str] = []

    async def fetch_detail(hex_: str):
        calls.append(hex_)
        return DetailInfo(
            asin="L1",
            end_at=real_end,
            ended=True,
            bid_price=4_200.0,
            buy_now_price=None,
            auto_extend=False,
            early_termination=None,
            name="n",
        )

    api.fetch_detail = fetch_detail  # type: ignore[method-assign]
    snap, result = await refresher.refresh(snap)

    assert result.ok, result.error
    assert snap.auctions[0].door_state is DoorState.ENDED, "详情说结束了才算结束"
    assert snap.auctions[0].latest_bid == 4_200.0, "成交价来自详情"

    # 第三轮：它已经结束，也就不在「进行中」列表里了。
    # 注意不能让它继续留在列表里——那会被 R6 判成"复活"，自然要重新拉详情。
    api._asins = ()
    calls.clear()
    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error
    assert snap.auctions[0].door_state is DoorState.ENDED, "不在列表也不改终态"
    assert calls == [], f"已结束的不该反复拉，实际 {calls}"


async def test_unfollowed_item_stops_being_polled(tmp_path: Path):
    """用户取消关注（hasCollect=False）后，这条拍品不再每轮拉详情。

    这就是 2026-11-15 真实取关实验发现的漏洞：改成「结束只由详情判定」之后，
    从「进行中」列表消失的拍品既不满足 ENDED、也不再被列表改终态，于是一直留在
    「关注中」，每轮都拉一次详情、永远不会停。``hasCollect`` 直接说"没关注了"，
    转成 UNFOLLOWED，``_needs_detail`` 的 ``user_query is FOLLOWING`` 自动不再命中。

    注意：取关那条拍品 ``over=False``（雅虎上又上架重新竞拍了）——正交性正是靠
    这个字段成立的，用结束时间推断永远做不到。
    """
    api = _Api(asins=("U1",))
    cancelled = {"on": False}

    async def fetch_detail(hex_: str) -> DetailInfo:
        api.detail_calls.append(hex_)
        return DetailInfo(
            asin="U1",
            # 没给结束时间：下架/重新上架的响应里可能缺，取关仍要识别出来
            end_at=None,
            ended=False,
            bid_price=1300.0,
            buy_now_price=3000.0,
            auto_extend=False,
            early_termination=None,
            name="n",
            collected=not cancelled["on"],
        )

    api.fetch_detail = fetch_detail  # type: ignore[method-assign]
    refresher, _ = _build(tmp_path, api)

    snap, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    assert snap.auctions[0].user_query is UserQuery.FOLLOWING
    assert api.detail_calls == ["hexU1"]

    # 用户取消关注：拍品从「进行中」列表消失，详情回 hasCollect=False。
    # 顺手清空内存里的地址字典（模拟程序重启）：详情地址必须能从镜像表补上，
    # 否则取关识别会变成"只在没重启过时有效"。
    cancelled["on"] = True
    api._asins = ()
    api.detail_calls.clear()
    refresher._detail_urls.clear()

    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error
    got = snap.auctions[0]
    assert got.user_query is UserQuery.UNFOLLOWED, "取关要落在状态里，不是只记一句日志"
    assert got.door_state is DoorState.ACTIVE, "取关不等于拍卖结束，两者正交"
    assert api.detail_calls == ["hexU1"], "识别取关的那一轮仍需拉到详情"

    # 之后不再拉：这是整个修复的目的（否则请求数随使用单调增长）
    api.detail_calls.clear()
    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error
    assert api.detail_calls == [], f"已取关不该反复拉，实际 {api.detail_calls}"
    assert snap.auctions[0].user_query is UserQuery.UNFOLLOWED


async def test_buyout_midauction_lands_in_ended_with_the_final_price(tmp_path: Path):
    """带一口价的拍卖被中途买走：进「已结束」，并且成交价是**一口价**。

    真实提问：「有一个拍卖订单，同时还带一口价，然后他在拍卖的中途被人一口价
    买走了，我们能正确的把他转移到已结束里面吗？会不会判断成已取关？」

    三件事都要对：
    1. 判终态（不是「已取关」——那个状态已取消）；
    2. 结束时间是**实际结束的那一刻**，不是原定截止（原定截止还在未来）；
    3. 当前价是成交价（一口价），不是"最后看到的竞价"。
    """
    end_at = datetime.now(UTC) + timedelta(hours=6)  # 原定截止：还早
    api = _Api(asins=("BUY1",))
    bought = {"on": False}

    async def fetch_detail(hex_: str) -> DetailInfo:
        api.detail_calls.append(hex_)
        return DetailInfo(
            asin="BUY1",
            end_at=end_at,
            ended=bought["on"],
            bid_price=5_000.0 if bought["on"] else 3_300.0,
            buy_now_price=5_000.0,
            auto_extend=False,
            early_termination="有" if bought["on"] else "会",
            name="一口价商品",
        )

    api.fetch_detail = fetch_detail  # type: ignore[method-assign]
    refresher, _ = _build(tmp_path, api)

    snap, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    before = snap.auctions[0]
    assert before.door_state is DoorState.ACTIVE
    assert before.latest_bid == 3_300.0
    assert before.buy_now_price == 5_000.0

    # 中途被人一口价买走：从「进行中」列表消失，而原定截止还在未来
    bought["on"] = True
    api._asins = ()
    api.detail_calls.clear()

    snap, result = await refresher.refresh(snap)

    assert result.ok, result.error
    got = snap.auctions[0]
    assert got.door_state is DoorState.ENDED, "被买走就是结束，不是取关"
    assert got.latest_bid == 5_000.0, "成交价应为一口气价，不是买走前的竞价"
    assert got.end_time.at is not None
    assert got.end_time.at < end_at, "结束时间应是实际结束时刻，不是原定截止"
    assert api.detail_calls == ["hexBUY1"], "终态那一轮正好补一次详情"


# ---- 详情页刷新要能用镜像表里存的地址（重启后内存是空的）----------------------


class _StoredUrlApi:
    """只提供 ``fetch_detail``：这条测试走的是 ``refresh_detail_of`` 单条路径。"""

    def __init__(self, price: float = 4400.0) -> None:
        self.price = price
        self.calls: list[str] = []

    async def fetch_detail(self, detail_url_hex: str) -> DetailInfo:
        self.calls.append(detail_url_hex)
        return DetailInfo(
            asin="",
            end_at=NOW - timedelta(days=1),
            ended=True,
            bid_price=self.price,
            buy_now_price=None,
            auto_extend=None,
            early_termination=None,
            name="n",
        )


def _seed_detail_url(mirror: MirrorStore, asin: str, url: str) -> None:
    """把 ``asin -> detail_url`` 写进镜像表（模拟"重启后内存里没有地址"）。"""
    conn = mirror._conn
    conn.execute(
        "INSERT OR REPLACE INTO list_items (status, asin, detail_url, name, url, seen_at)"
        " VALUES ('已结束', ?, ?, ?, ?, ?)",
        (
            asin,
            url,
            f"拍品 {asin}",
            f"https://auctions.yahoo.co.jp/jp/auction/{asin}",
            NOW.isoformat(),
        ),
    )
    conn.commit()


async def test_refresh_detail_of_falls_back_to_the_stored_url(tmp_path: Any):
    """重启后内存里的地址是空的，详情页刷新要能从镜像表补上。

    否则用户点进一条历史详情、按 r 只会看到"还没有这个拍品的详情地址"。
    """
    api = _StoredUrlApi(price=4400.0)
    ref, _ = _build(tmp_path, api)
    mirror = ref._mirror
    _seed_detail_url(mirror, "H1", "deadbeef")
    state = AuctionState(
        asin="H1",
        name="拍品 H1",
        url="https://auctions.yahoo.co.jp/jp/auction/H1",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ENDED,
        end_time=EndTime.pending(),
    )
    new_state = await ref.refresh_detail_of(state)
    assert new_state.latest_bid == 4400.0
    assert api.calls == ["deadbeef"]


class _SmallPageApi(_Api):
    """服务端每页只给 2 条（``ItemPerPage=2``），共 4 条拍品分 2 页。"""

    def __init__(self) -> None:
        super().__init__(asins=("A1", "A2", "A3", "A4"))
        self.pages: list[int] = []

    async def fetch_list_page(self, page: int, *, status: str = "进行中") -> ListPage:
        self.pages.append(page)
        if status != "进行中":
            return ListPage(items=(), total_item=0, current_page=page, total_page=1, status=status)
        start = (page - 1) * 2
        items = tuple(_item(a, f"拍品{a}") for a in ("A1", "A2", "A3", "A4")[start : start + 2])
        return ListPage(
            items=items,
            total_item=4,
            current_page=page,
            total_page=2,
            status=status,
            item_per_page=2,
            show_page_num=10,
        )


async def test_short_page_size_does_not_end_the_list_early(tmp_path: Path):
    """“本页不满就是最后一页”必须按接口自报的每页条数判。

    2026-11-18 审计：判据用的是配置里的 ``page_size_expected=10``。服务端哪天
    改成每页 2 条、20 条，第 1 页就会被误判成最后一页，后半截拍品在
    ``diff_list_rounds`` 里全部变成 disappeared。
    """
    api = _SmallPageApi()
    refresher, _ = _build(tmp_path, api)

    snap, result = await refresher.refresh(Snapshot())

    assert result.ok, result.error
    assert api.pages == [1, 2], api.pages
    assert sorted(a.asin for a in snap.auctions) == ["A1", "A2", "A3", "A4"]
