"""详情里的 ``hasCollect``：判断「用户是否取消关注」，与拍卖结束与否正交。

背景（2026-11-15）：改成「结束只由详情判定、每轮全量拉详情」之后，出现了一个
单调增长的轮询漏洞——拍品从「进行中」列表消失时，本地分不清"结束了"还是"用户
取消关注了"，``lifecycle.advance`` 又不再改终态，于是它永远留在「关注中」，每轮
都拉一次详情、永远不会停。

用户配合做了一次真实取关实验，证明详情接口顶层的 ``hasCollect`` 就是答案：
还在关注（含已结束但没取关的）全 ``True``，从没关注过的全 ``False``，刚取消
关注的那条 ``False``（连查 3 次稳定）。而且它与 ``over``（拍卖是否结束）**完全
正交**：那条刚取消关注的拍品 ``hasCollect=False`` 但 ``over=False``（雅虎上又
重新上架了）。

本文件守的是这条链路的三段：``_map_detail`` 解析 → ``_to_detail_result`` 不把
「没结束时间但取关了」误判成失败 → ``apply_detail`` 转 ``UNFOLLOWED`` 并停轮询。
端到端「取关后不再拉详情」的用例在 ``tests/test_refresh_details.py``。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from meruki_tracker.core.adapters.meruki.api import DetailInfo, _map_detail, _optional_bool
from meruki_tracker.core.app.refresher import _to_detail_result
from meruki_tracker.core.domain.detail import DetailResult, apply_detail
from meruki_tracker.core.domain.models import (
    AuctionState,
    DoorState,
    EndTime,
    TransitionKind,
    UserQuery,
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

#: 详情响应的骨架（goodsInfo 必带，deadTimeWithZone 让 _parse_end_time 能解析）
_DETAIL = {
    "goodsInfo": {
        "asin": "X",
        "name": "测试拍品",
        "deadTimeWithZone": "2026-11-15T21:00:00+09:00",
        "bidPrice": 1300,
        "buyNowPrice": 3000,
    },
    "other": {"earlyTermination": "会"},
    "over": False,
}


def _detail_with(hascollect):
    """构造带（或不带）``hasCollect`` 的详情响应。"""
    data = dict(_DETAIL)
    if hascollect is not ...:
        data["hasCollect"] = hascollect
    return data


def _info(**kw) -> DetailInfo:
    """构造一个 ``DetailInfo``（其余字段给无副作用的默认值）。"""
    base = dict(
        asin="X",
        end_at=None,
        ended=False,
        bid_price=None,
        buy_now_price=None,
        auto_extend=False,
        early_termination=False,
        name="n",
    )
    base.update(kw)
    return DetailInfo(**base)


def _state(**kw) -> AuctionState:
    """一个"关注中、进行中、时间已确认"的拍品。"""
    base = dict(
        asin="X",
        name="n",
        url="https://x/X",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(NOW + timedelta(hours=6), NOW),
    )
    base.update(kw)
    return AuctionState(**base)


# ---------------------------------------------------------------- 协议解析


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("False", False),
        ("1", True),
        ("0", False),
        ("yes", True),
        ("no", False),
        (None, None),
        ("看不懂", None),
        ({}, None),
        ([], None),
    ],
)
def test_optional_bool_table(raw, expected):
    assert _optional_bool(raw) is expected


def test_map_detail_reads_hascollect_true():
    assert _map_detail(_detail_with(True)).collected is True


def test_map_detail_reads_hascollect_false():
    assert _map_detail(_detail_with(False)).collected is False


def test_map_detail_missing_hascollect_is_none_not_false():
    """缺字段必须留 None：当成 False 会把还在关注的拍品误标成已取关。"""
    assert _map_detail(_detail_with(...)).collected is None


def test_map_detail_unrecognised_hascollect_is_none():
    assert _map_detail(_detail_with({"nested": 1})).collected is None


# ---------------------------------------------------------------- _to_detail_result


def test_no_end_time_but_unfollowed_is_not_a_failure():
    """商品下架/响应缺结束时间，但 hasCollect=False 是有效结论，不能当白拉。"""
    result = _to_detail_result(_info(end_at=None, ended=False, collected=False))
    assert result.failed is False, "hasCollect=False 本身就是答案"
    assert result.collected is False


def test_no_end_time_without_unfollow_is_a_failure():
    """没有取关信号也没解析出时间 → 仍然是失败（守住原来的判据）。"""
    for collected in (None, True):
        result = _to_detail_result(_info(end_at=None, ended=False, collected=collected))
        assert result.failed is True


def test_collected_is_carried_into_the_result():
    result = _to_detail_result(_info(end_at=NOW + timedelta(hours=6), ended=False, collected=False))
    assert result.failed is False
    assert result.collected is False


# ---------------------------------------------------------------- apply_detail


def test_unfollow_marks_state_and_stops_the_question():
    """取关但拍卖没结束：转 UNFOLLOWED（door_state 不动），发迁移，记时刻。"""
    new, tr = apply_detail(_state(), DetailResult(collected=False), NOW)
    assert new.user_query is UserQuery.UNFOLLOWED
    assert new.door_state is DoorState.ACTIVE, "取关不等于拍卖结束，两者正交"
    assert new.unfollowed_at == NOW
    assert tr is not None and tr.kind is TransitionKind.UNFOLLOWED


def test_unfollow_and_ended_keep_both_facts():
    """同一轮既结束又取关：两个维度都写进状态，迁移先报用户的动作。"""
    new, tr = apply_detail(_state(), DetailResult(collected=False, ended=True), NOW)
    assert new.user_query is UserQuery.UNFOLLOWED
    assert new.door_state is DoorState.ENDED
    assert tr is not None and tr.kind is TransitionKind.UNFOLLOWED


def test_missing_hascollect_does_not_change_state():
    """None = 这次没拿到，保持原状态，绝不猜。"""
    new, tr = apply_detail(_state(), DetailResult(collected=None), NOW)
    assert new.user_query is UserQuery.FOLLOWING
    assert tr is None


def test_collected_true_does_not_change_state():
    new, tr = apply_detail(_state(), DetailResult(collected=True), NOW)
    assert new.user_query is UserQuery.FOLLOWING
    assert tr is None


def test_already_ended_item_that_was_unfollowed():
    """早就结束、这一轮才发现取关：仍是终态，但要把 user_query 改过来。"""
    st = _state(door_state=DoorState.ENDED)
    new, tr = apply_detail(st, DetailResult(collected=False, ended=True), NOW)
    assert new.user_query is UserQuery.UNFOLLOWED
    assert new.door_state is DoorState.ENDED
    assert tr is not None and tr.kind is TransitionKind.UNFOLLOWED, "状态改了迁移也要记"


def test_unfollow_is_idempotent():
    """已经是 UNFOLLOWED 再看到 False，不发重复迁移。"""
    st = replace(_state(), user_query=UserQuery.UNFOLLOWED)
    new, tr = apply_detail(st, DetailResult(collected=False), NOW)
    assert new.user_query is UserQuery.UNFOLLOWED
    assert tr is None


def test_unfollow_wins_the_transition_over_end_time_change():
    """取关 + 结束时间同时变：状态里都改了，迁移只报一次取关。"""
    st = _state(end_time=EndTime.exact(NOW + timedelta(hours=6), NOW))
    new, tr = apply_detail(
        st,
        DetailResult(collected=False, end_at=NOW + timedelta(hours=9)),
        NOW,
    )
    assert new.user_query is UserQuery.UNFOLLOWED
    assert new.end_time.at == NOW + timedelta(hours=9), "结束时间照样更新"
    assert tr is not None and tr.kind is TransitionKind.UNFOLLOWED


def test_failed_detail_never_triggers_unfollow():
    """失败的详情没有 hasCollect 结论，不能顺手把人标成取关。"""
    new, tr = apply_detail(_state(), DetailResult(failed=True), NOW)
    assert new.user_query is UserQuery.FOLLOWING
    assert tr is None
