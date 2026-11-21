"""状态推进测试：R1-R9 每条规则一个用例。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from meruki_tracker.core.domain.detail import DetailResult, apply_detail
from meruki_tracker.core.domain.lifecycle import advance
from meruki_tracker.core.domain.models import (
    AuctionState,
    DoorState,
    EndTime,
    Observation,
    TimeState,
    TransitionKind,
    UserQuery,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _state(asin: str, *, end_at: datetime) -> AuctionState:
    """一个"关注中、进行中、时间已确认"的拍品。"""
    return AuctionState(
        asin=asin,
        name=asin,
        url="https://x/" + asin,
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(end_at, NOW),
    )


def obs(asin: str, name: str = "test", bid: float | None = 100.0) -> Observation:
    return Observation(
        asin=asin, name=name, url=f"https://x/{asin}", latest_bid=bid, observed_at=NOW
    )


def state(
    asin: str,
    *,
    user_query: UserQuery = UserQuery.FOLLOWING,
    door_state: DoorState = DoorState.ACTIVE,
    end_time: EndTime | None = None,
    detail_failures: int = 0,
) -> AuctionState:
    return AuctionState(
        asin=asin,
        name="test",
        url=f"https://x/{asin}",
        user_query=user_query,
        door_state=door_state,
        end_time=end_time or EndTime.pending(),
        detail_failures=detail_failures,
    )


# ---------------------------------------------------------------- R1


def test_r1_new_asin_becomes_following_active_pending():
    new, tr = advance({}, [obs("A1")], now=NOW)
    st = new["A1"]
    assert st.user_query is UserQuery.FOLLOWING
    assert st.door_state is DoorState.ACTIVE
    assert st.end_time.state is TimeState.PENDING
    assert st.end_time.at is None
    assert st.last_seen_at == NOW
    assert [t.kind for t in tr] == [TransitionKind.DISCOVERED]


# ---------------------------------------------------------------- R2


def test_r2_still_listed_keeps_state():
    old = {"A1": state("A1", end_time=EndTime.exact(NOW + timedelta(hours=1), NOW))}
    new, tr = advance(old, [obs("A1", bid=200.0)], now=NOW)
    st = new["A1"]
    assert st.user_query is UserQuery.FOLLOWING
    assert st.end_time.state is TimeState.EXACT  # 时间不被列表覆盖
    assert st.latest_bid == 200.0
    assert tr == []


# ---------------------------------------------------------------- R3/R4 已删除
#
# 「不在进行中列表 ⇒ 结束」这条规则整套删掉了（用户 2026-11-15：「每条挨个去
# 请求详情，从详情判断是否结束」）。列表接口只给「进行中」，消失既可能是拍完了
# 也可能是被取关，本地猜不准；详情里有 ``over``，那才是事实。下面这几条守着
# 「advance 不再改终态」这件事，别让推断悄悄长回来。


def test_missing_item_is_left_alone():
    """从「进行中」消失：door_state 一点不动，等详情回答。"""
    now = datetime(2026, 10, 30, 23, 30, tzinfo=UTC)
    old = {"A1": _state(asin="A1", end_at=now + timedelta(hours=5))}
    new, transitions = advance(old, [obs("A2")], now)
    assert new["A1"] == old["A1"]
    # 列表里那条 A2 是新发现，除此之外不该有任何迁移
    assert [t.kind for t in transitions] == [TransitionKind.DISCOVERED]


def test_empty_list_changes_nothing():
    """列表一条都没有：不许把所有人判成结束。"""
    now = datetime(2026, 10, 30, 23, 30, tzinfo=UTC)
    old = {
        "A1": _state(asin="A1", end_at=now + timedelta(hours=5)),
        "A2": _state(asin="A2", end_at=now - timedelta(hours=5)),
    }
    new, transitions = advance(old, [], now)
    assert new == old
    assert transitions == []


def test_missing_item_whose_deadline_passed_still_waits_for_detail():
    """结束时间已过也不在这里判结束——那是详情的事。"""
    now = datetime(2026, 10, 30, 23, 30, tzinfo=UTC)
    old = {"A1": _state(asin="A1", end_at=now - timedelta(hours=2))}
    new, transitions = advance(old, [], now)
    assert new["A1"].door_state is DoorState.ACTIVE
    assert transitions == []


# ---------------------------------------------------------------- R5


def test_r5_refollowed_resets_unfollowed():
    old = {
        "A1": state("A1", user_query=UserQuery.UNFOLLOWED).__class__(
            asin="A1",
            name="test",
            url="https://x/A1",
            user_query=UserQuery.UNFOLLOWED,
            door_state=DoorState.ACTIVE,
            end_time=EndTime.pending(),
            unfollowed_at=NOW - timedelta(hours=1),
        )
    }
    new, tr = advance(old, [obs("A1")], now=NOW)
    st = new["A1"]
    assert st.user_query is UserQuery.FOLLOWING
    assert st.unfollowed_at is None
    assert [t.kind for t in tr] == [TransitionKind.REFOLLOWED]


# ---------------------------------------------------------------- R6/R7/R9 apply_detail


def test_r7_detail_confirms_ended():
    st = state("A1")
    new, tr = apply_detail(st, DetailResult(ended=True), NOW)
    assert new.door_state is DoorState.ENDED
    assert new.concluded_at == NOW
    assert tr is not None and tr.kind is TransitionKind.ENDED


def test_r9_invalid_recovers_when_detail_succeeds():
    st = state("A1", door_state=DoorState.INVALID, detail_failures=3)
    new, tr = apply_detail(st, DetailResult(end_at=NOW + timedelta(hours=2)), NOW)
    assert new.door_state is DoorState.ACTIVE
    assert new.detail_failures == 0
    assert new.end_time.state is TimeState.EXACT
    assert tr is not None and tr.kind is TransitionKind.RECOVERED


def test_r8_three_detail_failures_become_invalid():
    st = state("A1", detail_failures=2)
    new, tr = apply_detail(st, DetailResult(failed=True), NOW)
    assert new.door_state is DoorState.INVALID
    assert new.detail_failures == 3
    assert tr is not None and tr.kind is TransitionKind.INVALIDATED


def test_r8_under_limit_stays_active():
    st = state("A1", detail_failures=0)
    new, tr = apply_detail(st, DetailResult(failed=True), NOW)
    assert new.door_state is DoorState.ACTIVE
    assert new.detail_failures == 1
    assert tr is None


def test_end_time_change_detected():
    st = state("A1", end_time=EndTime.exact(NOW + timedelta(hours=1), NOW))
    new, tr = apply_detail(st, DetailResult(end_at=NOW + timedelta(hours=2)), NOW)
    assert new.end_time.at == NOW + timedelta(hours=2)
    assert tr is not None and tr.kind is TransitionKind.END_TIME_CHANGED


def test_detail_sets_exact_time_first_time():
    st = state("A1")
    new, tr = apply_detail(st, DetailResult(end_at=NOW + timedelta(hours=1)), NOW)
    assert new.end_time.state is TimeState.EXACT
    assert tr is None


def test_ended_is_terminal_no_duplicate_transition():
    st = state("A1", door_state=DoorState.ENDED)
    new, tr = apply_detail(st, DetailResult(ended=True), NOW)
    assert new.door_state is DoorState.ENDED
    assert tr is None


# ---------------------------------------------------------------- 模型不变式


def test_pending_cannot_carry_time():
    with pytest.raises(ValueError):
        EndTime(TimeState.PENDING, at=NOW)


def test_exact_requires_time():
    with pytest.raises(ValueError):
        EndTime(TimeState.EXACT)


def test_is_tracked_semantics():
    assert state("A1").is_tracked
    assert state("A1", door_state=DoorState.INVALID).is_tracked
    assert not state("A1", door_state=DoorState.ENDED).is_tracked
    assert not state("A1", user_query=UserQuery.UNFOLLOWED).is_tracked


# ------------------------------------------------- 结束只由详情判定


def test_advance_never_marks_ended_even_when_the_deadline_passed():
    """守卫：``advance`` 里不该再出现任何 ``DoorState.ENDED`` 赋值。"""
    import inspect

    from meruki_tracker.core.domain import lifecycle

    src = inspect.getsource(lifecycle.advance)
    assert "door_state=DoorState.ENDED" not in src, "结束只能由 domain/detail.py 判"


# ---------------------------------------------------------------- R6 复活


def test_ended_item_reappearing_in_ongoing_list_is_resurrected():
    """结束后重新开拍：出现在「进行中」列表里就该变回 active。

    服务端允许同一条收藏记录在两个桶之间移动（真实见过）。R2 只刷字段、不看
    door_state，会让它永远停在「已结束」——而实际上它正在拍卖。
    """
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    ended = replace(
        _state(asin="R", end_at=now - timedelta(days=3)),
        door_state=DoorState.ENDED,
        concluded_at=now - timedelta(days=3),
    )
    seen = Observation(
        asin="R", name="重开拍品", url="https://x/R", latest_bid=None, observed_at=now
    )

    new, transitions = advance({"R": ended}, [seen], now)

    assert new["R"].door_state is DoorState.ACTIVE
    assert new["R"].concluded_at is None
    assert new["R"].name == "重开拍品"
    assert [t.kind for t in transitions] == [TransitionKind.RESURRECTED]
    assert transitions[0].asin == "R"


def test_resurrected_item_drops_stale_end_time():
    """复活后不能留着上一轮的结束时间：那会算出负的倒计时。"""
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    ended = replace(
        _state(asin="R", end_at=now - timedelta(days=3)),
        door_state=DoorState.ENDED,
        concluded_at=now - timedelta(days=3),
    )
    seen = Observation(asin="R", name="R", url="https://x/R", latest_bid=None, observed_at=now)

    new, _ = advance({"R": ended}, [seen], now)

    assert new["R"].end_time.state is TimeState.PENDING
    assert new["R"].end_time.at is None


def test_active_item_in_list_is_not_resurrected():
    """正常进行中的拍品不该被误判成复活。"""
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    cur = _state(asin="A", end_at=now + timedelta(hours=3))
    seen = Observation(asin="A", name="A", url="https://x/A", latest_bid=None, observed_at=now)

    new, transitions = advance({"A": cur}, [seen], now)

    assert new["A"].door_state is DoorState.ACTIVE
    assert transitions == []


# --------------------------------------------------- 历史并入状态


# ------------------------------------------------ 一口价商品（即決価格）


def _detail(**kw):
    from meruki_tracker.core.domain.detail import DetailResult

    base = {
        "failed": False,
        "ended": False,
        "end_at": datetime(2026, 11, 5, tzinfo=UTC),
        "current_bid": None,
        "final_bid": None,
        "buy_now_price": None,
        # 这些用例测的是"价格字段怎么落库"，默认按"响应里有这个字段"构造：
        # 只有 buy_now_price_present 为真时 apply_detail 才会动 buy_now_price
        # （那是为了区分"卖家撤掉即决价"和"这次响应没带这个字段"）。
        "buy_now_price_present": True,
    }
    base.update(kw)
    return DetailResult(**base)


def test_fixed_price_item_keeps_both_prices_apart():
    """当前价与一口价**分开存**，不合并。

    真实反馈：「你分开两个字段展示吧，就当前价和一口价」。一口价商品的
    ``bidPrice`` 恒为 0（没有拍卖价），价格全在 ``buyNowPrice`` 上——
    所以当前价留空（界面显示 `—`），一口价是 1980。
    """
    st, _ = apply_detail(
        _state("A1", end_at=NOW + timedelta(days=3)),
        _detail(current_bid=0.0, buy_now_price=1980.0),
        NOW,
    )
    assert st.latest_bid is None
    assert st.buy_now_price == 1980.0


def test_normal_auction_shows_both_prices():
    """两个都有时各存各的：拍到了多少 vs 能直接买走多少，不能混成一个数。"""
    st, _ = apply_detail(
        _state("A1", end_at=NOW + timedelta(days=3)),
        _detail(current_bid=1300.0, buy_now_price=3000.0),
        NOW,
    )
    assert st.latest_bid == 1300.0
    assert st.buy_now_price == 3000.0


def test_fixed_price_item_without_buy_now_stays_empty():
    """bidPrice=0 且没有一口价时两个字段都空——显示「—」比显示「¥0」诚实。"""
    st, _ = apply_detail(
        _state("A1", end_at=NOW + timedelta(days=3)),
        _detail(current_bid=0.0, buy_now_price=0.0),
        NOW,
    )
    assert st.latest_bid is None
    assert not st.buy_now_price


def test_ending_fixed_price_item_keeps_both_prices():
    """一口价拍品结束时，两个字段都留着，界面自己决定怎么显示。"""
    st, trans = apply_detail(
        _state("A1", end_at=NOW + timedelta(days=3)),
        _detail(current_bid=0.0, buy_now_price=1980.0, ended=True),
        NOW,
    )
    assert st.latest_bid is None
    assert st.buy_now_price == 1980.0
    assert st.door_state is DoorState.ENDED
    assert trans is not None and trans.kind is TransitionKind.ENDED


def test_ended_item_that_was_unfollowed_comes_back_when_relisted() -> None:
    """已结束 + 已取关的拍品重新出现在「进行中」列表时，必须真的复活成 active。

    2026-11-18 审计：``advance`` 的 ``elif`` 链把 ``UNFOLLOWED`` 判在 ``ENDED``
    之前，于是「结束之后又被取关」的拍品同时满足两个条件、只走取关分支——
    ``door_state`` 仍是 ``ENDED``、旧结束时间还在、``concluded_at`` 不清，
    ``_needs_detail`` 因为 ``door_state is ENDED`` 再也不拉详情，它永久停在
    「已结束」（而且还落在可清理视图里，用户按 x 就把它删了）。
    """
    old_end = NOW - timedelta(hours=1)
    ended_then_unfollowed = replace(
        _state("A1", end_at=old_end),
        door_state=DoorState.ENDED,
        concluded_at=old_end,
    )
    ended_then_unfollowed, _ = apply_detail(
        ended_then_unfollowed, DetailResult(failed=False, collected=False), NOW
    )
    assert ended_then_unfollowed.user_query is UserQuery.UNFOLLOWED
    assert ended_then_unfollowed.door_state is DoorState.ENDED

    new, transitions = advance({"A1": ended_then_unfollowed}, [obs("A1")], now=NOW)

    got = new["A1"]
    assert got.user_query is UserQuery.FOLLOWING
    assert got.unfollowed_at is None
    assert got.door_state is DoorState.ACTIVE
    assert got.end_time.state is TimeState.PENDING
    assert got.concluded_at is None
    assert got.is_tracked
    assert [t.kind for t in transitions] == [TransitionKind.RESURRECTED]
