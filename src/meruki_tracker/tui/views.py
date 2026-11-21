"""列表视图：把拍品按"用户还关心什么"切成几组。

为什么需要：列表接口只返回**进行中**的拍品，本地快照里却同时躺着进行中的、
已经结束的、以及详情一直拉不到的。全堆在一屏时，真正要盯的两三个会被几十
条已结束的挤到后面翻不完；但这些记录又不能悄悄丢掉——所以做成**可切换的视图**，
而不是默认过滤：数量就在标题栏上，想看随时 Tab 一下。

分区**互斥且完备**：关注中 / 已结束 / 无法确认，三组之和正好等于全部，
计数不重复也不遗漏。

关于「无法确认」：详情连续失败若干次后 `door_state` 会变成 `INVALID`。按用户
拍板，这些拍品**继续永久轮询**（详情能通了自己就恢复），但得给一个手动出口，
所以单独成一档并且**可以清理**。清理只是把本地记录从快照里拿掉：如果拍品还在
网站的「进行中」列表里，下一轮刷新会重新发现它、从零开始重试。

关于「已取关」：这个视图曾经存在，现在**取消**了。详情接口的 `hasCollect` 能
明确分辨"拍完了"和"你取消关注了"（见 `hasCollect → UNFOLLOWED` 那一刀），
但两种情况的结论对你一样——这个拍品都不用再盯了——所以不值得单开一栏。
它们归到「已结束」，剩余列显示「已离开」、行标记 `·`。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from ..core.domain.models import AuctionState, DoorState, UserQuery


class ListView(StrEnum):
    """列表的分组视图。"""

    FOLLOWING = "following"
    ENDED = "ended"
    #: 详情连续拉不到、还在永久重试的拍品；单独成组是为了能手动清理。
    INVALID = "invalid"
    #: 历史遗留：老快照里可能还有这个状态，界面上已不再作为视图出现。
    UNFOLLOWED = "unfollowed"
    ALL = "all"


LABELS: dict[ListView, str] = {
    ListView.FOLLOWING: "关注中",
    ListView.ENDED: "已结束",
    ListView.INVALID: "无法确认",
    ListView.UNFOLLOWED: "已结束",
    ListView.ALL: "全部",
}

#: Tab 键的切换顺序（最后是"全部"，来回一圈正好回到开头）。
#: 「无法确认」放在「已结束」后面：关注中 / 已结束 的既有顺序不动，
#: 免得打乱已经习惯的 Tab 节奏；它也不是终态，不该混进「已结束」。
ORDER: tuple[ListView, ...] = (
    ListView.FOLLOWING,
    ListView.ENDED,
    ListView.INVALID,
    ListView.ALL,
)

#: 允许"清理"的视图：终态数据，以及详情一直拉不到、用户想手动放弃的。
#: 清理只从本地快照移除，不影响网站上的关注。
CLEANABLE: tuple[ListView, ...] = (ListView.ENDED, ListView.INVALID)


def group_of(st: AuctionState) -> ListView:
    """单个拍品属于哪一组（互斥，永不返回 ALL）。"""
    if st.door_state is DoorState.INVALID:
        # 这是采集问题（详情拉不到），和"还关不关注""拍完没拍完"两维都正交，
        # 所以单独优先判；程序还会继续轮询它，用户也能在「无法确认」里手动清掉。
        return ListView.INVALID
    if st.door_state is DoorState.ENDED:
        return ListView.ENDED
    if st.user_query is UserQuery.UNFOLLOWED:
        # 老数据：以前被标成"已取关"的，现在都归终态组，不再单独成类。
        return ListView.ENDED
    return ListView.FOLLOWING


def matches(st: AuctionState, view: ListView) -> bool:
    """``st`` 是否属于视图 ``view``。"""
    return view is ListView.ALL or group_of(st) is view


def counts(auctions: Iterable[AuctionState]) -> dict[ListView, int]:
    """各视图的条目数（``ALL`` 是总数）。"""
    out: dict[ListView, int] = {v: 0 for v in ORDER}
    for st in auctions:
        out[group_of(st)] += 1
        out[ListView.ALL] += 1
    return out


def next_view(view: ListView, delta: int = 1) -> ListView:
    """循环切换视图（delta 为负则反向）。"""
    order = list(ORDER)
    if view not in order:
        # 老界面停在已取消的视图上：从第一档重新开始，别让 index() 抛异常
        return order[0]
    return order[(order.index(view) + delta) % len(order)]
