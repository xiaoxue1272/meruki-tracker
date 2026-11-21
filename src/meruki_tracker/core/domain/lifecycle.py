"""状态推进（纯函数，零 I/O）。

输入：旧状态 + 本轮列表观测
输出：新状态 + 迁移记录

规则编号与文档《03-设计文档》§4.2、《05-详细实现文档》§3.4 一一对应。

**这里不再判"结束"**。列表接口只给「进行中」，拍品从里面消失既可能是拍完了、
也可能是被取关了，本地猜不准——以前"不在列表就判结束"是推断出来的状态。
用户 2026-11-15 定调：结束只看详情（详情里有 ``over`` 与真实结束时间），
所以终态由 ``domain/detail.py::apply_detail`` 负责，本模块只管"列表里看到的那些"。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import (
    AuctionState,
    DoorState,
    EndTime,
    Observation,
    Transition,
    TransitionKind,
    UserQuery,
)


def advance(
    old: dict[str, AuctionState],
    listed: list[Observation],
    now: datetime,
) -> tuple[dict[str, AuctionState], list[Transition]]:
    """根据本轮列表观测推进状态。

    只处理**列表里出现过的**拍品（新发现 / 仍在 / 重新关注 / 复活）。
    "不在列表里"什么都不做——结束与否由详情决定，见模块 docstring。

    Args:
        old: 旧状态（asin -> 状态）。
        listed: 本轮列表看到的拍品。
        now: 当前时刻（UTC）。

    Returns:
        新状态字典与迁移记录列表。
    """
    new: dict[str, AuctionState] = dict(old)
    transitions: list[Transition] = []

    # R1 / R2 / R5：出现在列表中的拍品
    for obs in listed:
        cur = new.get(obs.asin)
        if cur is None:
            # R1：新关注
            new[obs.asin] = AuctionState(
                asin=obs.asin,
                name=obs.name,
                url=obs.url,
                user_query=UserQuery.FOLLOWING,
                door_state=DoorState.ACTIVE,
                end_time=EndTime.pending(),
                # 列表接口**不返回价格**（只有详情有），别把这个 None 写进来
                latest_bid=obs.latest_bid,
                last_seen_at=now,
            )
            transitions.append(Transition(TransitionKind.DISCOVERED, obs.asin))
        elif cur.door_state is DoorState.ENDED:
            # R6：复活——上轮已判定结束，这轮又出现在「进行中」列表里。
            #
            # 服务端允许同一条收藏记录在两个桶之间移动（用户实测见过：拍品结束后
            # 重新上架竞拍）。Id 不变、位置也不变，靠增量扫描发现不了；但**复活后
            # 它必然出现在「进行中」桶里**，所以每次刷新顺手比对一下就够。
            #
            # 光看列表是发现不了这个的：R2 只刷字段、不看 door_state，会让它永远
            # 停在「已结束」，而实际上正在拍卖。
            #
            # **这条必须判在「重新关注」之前**：一条已结束的拍品被用户取关后
            # 又重新上架，它同时满足「已经取关」和「已经结束」两个条件。以前把
            # 取关判在前面，于是只复位 user_query、door_state 还是 ENDED、旧结束
            # 时间和 concluded_at 全留着，`_needs_detail()` 因为 `door_state is
            # ENDED` 再也不拉详情 —— 它永久停在「已结束」，自己永远不会纠正
            # （而「已结束」是可按 x 清理的视图，用户一清就把记录删了）。
            #
            # 「出现在进行中列表里」本身就证明**用户还关注着它**（这个列表就是
            # 收藏列表），所以这里把 user_query 一并复位，不必再判断是否取关过。
            new[obs.asin] = replace(
                cur,
                user_query=UserQuery.FOLLOWING,
                unfollowed_at=None,
                door_state=DoorState.ACTIVE,
                # 结束时间清掉：新一轮竞拍的结束时间还没确认，宁可显示"时间待确认"
                # 也不能留着上一轮的旧时间（会算出错误的倒计时）。
                end_time=EndTime.pending(),
                concluded_at=None,
                last_seen_at=now,
                name=obs.name or cur.name,
                latest_bid=_keep_bid(cur.latest_bid, obs.latest_bid),
            )
            transitions.append(Transition(TransitionKind.RESURRECTED, obs.asin))
        elif cur.user_query is UserQuery.UNFOLLOWED:
            # R5：重新关注（还没结束的那种）。仅复位关注状态——拍卖本来就没走完，
            # 结束时间、concluded_at 都还是对的，不该跟着清。
            new[obs.asin] = replace(
                cur,
                user_query=UserQuery.FOLLOWING,
                unfollowed_at=None,
                last_seen_at=now,
                name=obs.name or cur.name,
                # 列表不带价格：沿用上次详情拿到的值，别被 None 冲掉
                latest_bid=_keep_bid(cur.latest_bid, obs.latest_bid),
            )
            transitions.append(Transition(TransitionKind.REFOLLOWED, obs.asin))
        else:
            # R2：仍在关注且仍在列表——只刷新字段
            new[obs.asin] = replace(
                cur,
                last_seen_at=now,
                name=obs.name or cur.name,
                latest_bid=_keep_bid(cur.latest_bid, obs.latest_bid),
            )

    return new, transitions


def _keep_bid(previous: float | None, observed: float | None) -> float | None:
    """合并价格：本次没观测到（None）就沿用旧值。

    列表接口**不返回当前价**（真实响应只有 Asin/BidDeadline/Name/Url/DetailUrl/Id），
    价格只能来自详情。以前每轮都用列表的 None 覆盖，导致「详情拿到过价格、下一次
    刷新就变成 —」——用户看到的就是"列表和详情都看不到价格"。
    """
    return observed if observed is not None else previous
