"""列表视图分组 + 清理的纯逻辑测试。

分组必须**互斥且完备**：各组之和等于全部，一个拍品只能落在某一组——
否则计数会骗人（标题写 3 个、切过去只有 2 个）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery
from meruki_tracker.tui.views import (
    CLEANABLE,
    LABELS,
    ORDER,
    ListView,
    counts,
    group_of,
    matches,
    next_view,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _auction(
    asin: str,
    *,
    user: UserQuery = UserQuery.FOLLOWING,
    door: DoorState = DoorState.ACTIVE,
) -> AuctionState:
    return AuctionState(
        asin=asin,
        name=asin,
        url="https://x/" + asin,
        user_query=user,
        door_state=door,
        end_time=EndTime.exact(NOW, NOW),
    )


# ------------------------------------------------------------------ 分组


def test_active_following_goes_to_following():
    assert group_of(_auction("a")) is ListView.FOLLOWING


def test_ended_following_goes_to_ended():
    st = _auction("a", door=DoorState.ENDED)
    assert group_of(st) is ListView.ENDED


def test_unfollowed_legacy_rows_fold_into_ended():
    """老数据里残留的"已取关"：归「已结束」组，不再单独成类。

    `hasCollect` 已经能分辨取关，但结论和"拍完了"一样，界面上不值得多出一栏。
    """
    assert group_of(_auction("a", user=UserQuery.UNFOLLOWED)) is ListView.ENDED
    assert (
        group_of(_auction("b", user=UserQuery.UNFOLLOWED, door=DoorState.ENDED)) is ListView.ENDED
    )


def test_invalid_goes_to_its_own_group():
    """详情连续拉不到 → 单独「无法确认」视图，用户能手动清理。

    程序还会永久轮询它（用户拍板），所以它既不是终态、也不该混进「关注中」
    把正常的拍品淹掉；单独成档是给的手动出口。
    """
    assert group_of(_auction("a", door=DoorState.INVALID)) is ListView.INVALID
    # INVALID 优先于「已结束」/「已取关」：采集问题跟那两维正交
    assert group_of(_auction("b", door=DoorState.INVALID, user=UserQuery.UNFOLLOWED)) is (
        ListView.INVALID
    )


def test_groups_are_mutually_exclusive_and_complete():
    rows = [
        _auction("1"),
        _auction("2", door=DoorState.ENDED),
        _auction("3", user=UserQuery.UNFOLLOWED),
        _auction("4", door=DoorState.INVALID),
        _auction("5", user=UserQuery.UNFOLLOWED, door=DoorState.ENDED),
    ]
    c = counts(rows)
    assert c[ListView.ALL] == len(rows)
    group_sum = sum(c[v] for v in ORDER if v is not ListView.ALL)
    assert group_sum == len(rows), "互斥完备：各组之和必须等于总数"
    # 每一组都能被 matches 精确还原，数量对得上
    for view in ORDER:
        assert len([r for r in rows if matches(r, view)]) == c[view]


def test_all_view_matches_everything():
    assert all(matches(_auction(str(i), door=DoorState.ENDED), ListView.ALL) for i in range(3))


def test_counts_of_empty_input():
    c = counts([])
    assert all(v == 0 for v in c.values())


# ------------------------------------------------------------------ 切换


def test_next_view_cycles_forward_and_wraps():
    seen = []
    view = ORDER[0]
    for _ in range(len(ORDER)):
        seen.append(view)
        view = next_view(view, 1)
    assert seen == list(ORDER)
    assert view is ORDER[0], "转一圈必须回到起点"


def test_next_view_cycles_backward():
    assert next_view(ORDER[0], -1) is ORDER[-1]


def test_labels_cover_every_view():
    for view in ORDER:
        assert LABELS[view], f"{view} 没有中文名"


def test_only_terminal_and_invalid_views_are_cleanable():
    """「关注中」还在跑、「全部」混着活数据，都不该出现清理入口。

    「无法确认」不是终态，但用户明确要一个手动放弃的出口，所以也可清理。
    """
    assert set(CLEANABLE) == {ListView.ENDED, ListView.INVALID}
    assert ListView.FOLLOWING not in CLEANABLE
    assert ListView.ALL not in CLEANABLE
    assert ListView.UNFOLLOWED not in CLEANABLE


@pytest.mark.parametrize("delta", [1, -1, 3, -3])
def test_cycle_always_lands_on_a_known_view(delta: int):
    for view in ORDER:
        assert next_view(view, delta) in ORDER


def test_view_order_has_exactly_four_tabs():
    """「已取关」这一栏取消了；「无法确认」单独成档但放在「已结束」后面。

    前三档顺序沿用旧习惯（关注中 → 已结束），新增的档插在「全部」之前，
    免得把已经习惯的 Tab 节奏整个打乱。
    """
    assert ORDER == (
        ListView.FOLLOWING,
        ListView.ENDED,
        ListView.INVALID,
        ListView.ALL,
    )
    assert ListView.UNFOLLOWED not in ORDER
    # 老枚举还在（老快照能读出来），但不能因此多出一栏
    assert LABELS[ListView.UNFOLLOWED] == LABELS[ListView.ENDED]


def test_cycling_never_lands_on_the_cancelled_view():
    """来回切一圈，永远不该停在「已取关」上。"""
    view = ListView.FOLLOWING
    seen: list[ListView] = []
    for _ in range(len(ORDER)):
        seen.append(view)
        view = next_view(view)
    assert seen == list(ORDER)
    assert view is ListView.FOLLOWING, "转一圈必须回到开头"
