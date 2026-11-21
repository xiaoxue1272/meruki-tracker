"""详情结果 -> 状态推进（纯函数）。

end_time 的精确值只来自详情；列表只用来判断"还在不在关注列表里"。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .models import (
    AuctionState,
    DoorState,
    EndTime,
    Transition,
    TransitionKind,
    UserQuery,
)


@dataclass(frozen=True, slots=True)
class DetailResult:
    """一次详情拉取的结果。

    failed=True 表示这次没拿到有效结果（网络/风控/解析失败）。
    ended=True 表示详情确认拍卖已经结束。
    """

    failed: bool = False
    ended: bool = False
    end_at: datetime | None = None
    #: 详情里的**当前价**（日元）。列表接口不返回价格，价格只能来自这里。
    current_bid: float | None = None
    #: 已结束拍品的成交价（未结束时为 None）
    final_bid: float | None = None
    #: 一口价（``buyNowPrice``）。一口价商品没有拍卖价，成交价要看这个。
    #: ``None`` 表示这次详情没给（老调用点），``0`` 表示确实没有一口价。
    buy_now_price: float | None = None
    #: 详情响应里有没有 ``buyNowPrice``（用来区分"撤掉了"和"没拿到"）。
    buy_now_price_present: bool = False
    #: 我此刻**是否还关注**这个拍品（详情顶层的 ``hasCollect``）。
    #:
    #: ``None`` = 这次没拿到（失败 / 老响应），**保持原状态不动**；
    #: ``False`` = 用户在网站上取消了关注 → 转 ``UNFOLLOWED``，从此不再轮询。
    collected: bool | None = None


def apply_detail(
    state: AuctionState,
    detail: DetailResult,
    now: datetime,
    failure_limit: int = 3,
) -> tuple[AuctionState, Transition | None]:
    """把详情结果合并进拍品状态。

    Args:
        state: 当前拍品状态。
        detail: 详情拉取结果。
        now: 当前时刻（UTC）。
        failure_limit: 连续失败多少次判定为无法确认。

    Returns:
        新状态与（可能的）迁移记录。
    """
    if detail.failed:
        failures = state.detail_failures + 1
        if failures >= failure_limit and state.door_state is not DoorState.INVALID:
            return (
                replace(state, detail_failures=failures, door_state=DoorState.INVALID),
                Transition(
                    TransitionKind.INVALIDATED,
                    state.asin,
                    f"详情连续失败 {failures} 次",
                ),
            )
        return replace(state, detail_failures=failures), None

    # 价格每次都更新：列表不返回价格，详情是唯一来源。
    #
    # **当前价与一口价分开存**，不合并：一口价商品（ヤフオク 的「即決価格」）没有
    # 拍卖价，``bidPrice`` 恒为 0、真实价格在 ``buyNowPrice``；而普通拍卖可能两个
    # 都有（当前价 1,300 / 一口价 3,000），合并成一个数就分不清是"拍到了多少"还是
    # "能直接买走多少钱"。真实反馈：「你分开两个字段展示吧，就当前价和一口价」。
    #
    # 只有**正数**才写：0 表示"没有"，写进去会显示「¥0」，比「—」更误导。
    base = replace(state, detail_failures=0)
    if detail.current_bid:
        base = replace(base, latest_bid=detail.current_bid)
    if detail.buy_now_price_present:
        # 字段在就**以它为准**：给了价就更新，给 0（归一成 None）就是卖家撤掉了
        # 即决价，必须把本地旧价清掉。以前只看 ``is not None``，旧价会永远挂着。
        base = replace(base, buy_now_price=detail.buy_now_price)

    # R8：详情说"你不再关注这个拍品了"（``hasCollect=False``）。
    #
    # 这一条**必须独立于"拍卖是否结束"**：``user_query`` 是"我关不关注"，
    # ``door_state`` 是"拍卖走没走完"，两者正交。实测过的典型情况是一条**早已
    # 结束**的拍品被用户取消关注 —— 它 ``hasCollect=False`` 且 ``over=False``
    # （雅虎上又上架重新竞拍了），此时既不能当"还在关注"，也不能当"已结束"。
    #
    # 这个分支的价值在于**停止提问**：以前 `hasCollect` 没被读，从「进行中」列表
    # 消失的拍品会永远留在「关注中」，每轮都拉一次详情且永远不会结束——随着使用
    # 只增不减。现在一发现取关就转 ``UNFOLLOWED``，``_needs_detail()`` 判据里的
    # ``user_query is FOLLOWING`` 自动不再命中，轮询到此为止。
    #
    # ``None``（这次没拿到字段）必须保持原状态：把"不知道"当成"取关了"会让所有
    # 老响应都把拍品标成取关。
    if detail.collected is False and base.user_query is UserQuery.FOLLOWING:
        unfollowed = replace(
            base,
            user_query=UserQuery.UNFOLLOWED,
            unfollowed_at=now,
        )
        # 取关是"用户动作"，优先于同一轮里的结束判定——但结束本身仍然成立，
        # 所以先把细节跟完再发这条迁移（见下方 ended 分支的合并写法）。
        base = unfollowed
        unfollow_transition = Transition(TransitionKind.UNFOLLOWED, state.asin)
    else:
        unfollow_transition = None

    # R7：详情确认已结束
    if detail.ended:
        # 结束时间也听详情的，但**不能留一个未来时间**：一口价中途被买走 / 卖家
        # 提前终止时，原定截止还在未来，界面就会显示「已结束」+ 一个明天的结束时间。
        # 这种情况记"确认它结束的那一刻"；正常到点结束的详情给的就是截止本身，
        # 那比"我们发现它的时刻"更准，直接用。
        real_end = detail.end_at
        if real_end is None or real_end > now:
            real_end = now
        base = replace(base, end_time=EndTime.exact(real_end, now))
        if base.door_state is DoorState.ENDED:
            # 早就结束了、这一轮才发现取关：状态改了，迁移也要记下来
            return base, unfollow_transition
        ended_state = replace(base, door_state=DoorState.ENDED, concluded_at=now)
        # 同一轮里「结束」与「取关」都成立时，先报取关（用户的动作），
        # door_state 那两个改动的结果都已经在状态里，不靠迁移表达。
        return ended_state, (unfollow_transition or Transition(TransitionKind.ENDED, state.asin))

    # R9：从 invalid 恢复
    recovered = base.door_state is DoorState.INVALID
    if recovered:
        base = replace(base, door_state=DoorState.ACTIVE)

    if detail.end_at is None:
        if recovered:
            return base, unfollow_transition or Transition(TransitionKind.RECOVERED, state.asin)
        return base, unfollow_transition

    # R6/延期：时间变了（> 1 秒视为变更）
    old_at = base.end_time.at
    new_end = EndTime.exact(detail.end_at, now)
    if old_at is not None and abs((detail.end_at - old_at).total_seconds()) > 1:
        return (
            replace(base, end_time=new_end),
            unfollow_transition
            or Transition(
                TransitionKind.END_TIME_CHANGED,
                state.asin,
                f"{old_at.isoformat()} -> {detail.end_at.isoformat()}",
                # 两头都带上：通知层要据此分「延后」还是「提前」。
                # 只给 detail 那串文本的话，判断方向就得解析自己拼的字符串。
                old_end_at=old_at,
                new_end_at=detail.end_at,
            ),
        )

    if recovered:
        return (
            replace(base, end_time=new_end),
            unfollow_transition or Transition(TransitionKind.RECOVERED, state.asin),
        )
    return replace(base, end_time=new_end), unfollow_transition
