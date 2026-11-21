"""通知板：A 结束前提醒（预注册 due_at）+ B 出价被超过（注册即到期）。

盯住用户 m14435 的三条语义：所有通知都注册进来；只展示未来 2 小时内将要
通知的；到点交给 Notifier（测试里用记录型替身）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from meruki_tracker.core.app.notices import (
    KIND_ENDING,
    KIND_OUTBID,
    MAX_ENDING_POINTS,
    NOTICE_WINDOW,
    Notice,
    NoticeBoard,
    _fmt_minutes,
    normalize_points,
)
from meruki_tracker.core.domain.models import (
    AuctionState,
    DoorState,
    EndTime,
    UserQuery,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


class _Recorder:
    def __init__(self) -> None:
        self.notes: list = []

    def notify(self, note) -> None:
        self.notes.append(note)


class _Broken:
    def notify(self, note) -> None:
        raise RuntimeError("boom")


def _state(
    asin: str = "A1",
    *,
    end_at: datetime | None = None,
    door: DoorState = DoorState.ACTIVE,
    query: UserQuery = UserQuery.FOLLOWING,
    exact: bool = True,
) -> AuctionState:
    if exact:
        end_time = EndTime.exact(end_at or NOW + timedelta(hours=1), NOW)
    else:
        end_time = EndTime.pending()
    return AuctionState(
        asin=asin,
        name=f"拍品 {asin}",
        url="u",
        user_query=query,
        door_state=door,
        end_time=end_time,
    )


# ---------------------------------------------------------------- A：结束提醒


def test_sync_ending_registers_one_notice_per_point():
    board = NoticeBoard()
    board.sync_ending(
        [_state(end_at=NOW + timedelta(minutes=90))], points_minutes=(60.0, 15.0), now=NOW
    )
    snap = board.snapshot(NOW)
    assert len(snap) == 2
    # 结束在 +90min：60 分钟点 due=+30min 先响，15 分钟点 due=+75min 后响
    assert [n.minutes_before_end for n in snap] == [60.0, 15.0], "按触发时刻升序"
    assert snap[0].due_at == NOW + timedelta(minutes=30)
    assert snap[0].kind == KIND_ENDING


def test_sync_ending_skips_points_already_past():
    """结束前 60 分钟的提醒点已经过了：不补发，只留还没到点的。"""
    board = NoticeBoard()
    board.sync_ending(
        [_state(end_at=NOW + timedelta(minutes=30))], points_minutes=(60.0, 15.0), now=NOW
    )
    snap = board.snapshot(NOW)
    assert len(snap) == 1
    assert snap[0].minutes_before_end == 15.0


def test_sync_ending_ignores_non_trackable_states():
    board = NoticeBoard()
    board.sync_ending(
        [
            _state("E1", door=DoorState.ENDED),
            _state("U1", query=UserQuery.UNFOLLOWED),
            _state("P1", exact=False),
        ],
        points_minutes=(15.0,),
        now=NOW,
    )
    assert board.snapshot(NOW) == ()


def test_sync_ending_is_idempotent_and_follows_time_changes():
    """每轮全量重算：同步两次不翻倍；结束时间变了旧条目自然消失。"""
    board = NoticeBoard()
    board.sync_ending([_state(end_at=NOW + timedelta(minutes=90))], points_minutes=(15.0,), now=NOW)
    board.sync_ending([_state(end_at=NOW + timedelta(minutes=90))], points_minutes=(15.0,), now=NOW)
    assert len(board.snapshot(NOW)) == 1

    # 自动延时把结束时间顶后了：旧的 75 分钟点没了，新的点出现
    board.sync_ending(
        [_state(end_at=NOW + timedelta(minutes=120))], points_minutes=(15.0,), now=NOW
    )
    snap = board.snapshot(NOW)
    assert len(snap) == 1
    assert snap[0].due_at == NOW + timedelta(minutes=105)


def test_sync_ending_drops_when_auction_ends_early():
    board = NoticeBoard()
    board.sync_ending([_state(end_at=NOW + timedelta(minutes=90))], points_minutes=(15.0,), now=NOW)
    assert len(board.snapshot(NOW)) == 1
    # 拍品提前结束（ENDED）：下一轮同步后提醒必须消失
    board.sync_ending([_state(door=DoorState.ENDED)], points_minutes=(15.0,), now=NOW)
    assert board.snapshot(NOW) == ()


# ---------------------------------------------------------------- 窗口语义


def test_snapshot_only_shows_the_two_hour_window():
    board = NoticeBoard()
    board.sync_ending(
        [
            _state("near", end_at=NOW + timedelta(minutes=90)),  # due +75min，窗口内
            _state("far", end_at=NOW + timedelta(hours=5)),  # due +4h45m，窗口外
        ],
        points_minutes=(15.0,),
        now=NOW,
    )
    snap = board.snapshot(NOW)
    assert [n.asin for n in snap] == ["near"]
    # 时间走到 3 小时后，"far" 进入窗口
    later = NOW + timedelta(hours=3)
    assert [n.asin for n in board.snapshot(later)] == ["far"]
    assert timedelta(hours=2) == NOTICE_WINDOW


def test_snapshot_excludes_due_but_unfired_entries():
    """已过 due_at 还没被 take_due 收走的是「该响的」，不是「将要响的」。"""
    board = NoticeBoard()
    board.add_outbid(msg_id=1, asin="A1", title="t", now=NOW - timedelta(seconds=30))
    assert board.snapshot(NOW) == ()
    assert board.pending_count(NOW) == 1


# ---------------------------------------------------------------- B：出价被超过


def test_add_outbid_is_due_immediately_and_dedups_by_msg_id():
    board = NoticeBoard(notifier=_Recorder())
    board.add_outbid(msg_id=1, asin="A1", title="拍品甲", now=NOW)
    board.add_outbid(msg_id=1, asin="A1", title="拍品甲", now=NOW + timedelta(seconds=1))
    assert board.pending_count(NOW) == 1
    due = board.take_due(NOW)
    assert len(due) == 1
    assert due[0].kind == KIND_OUTBID
    assert due[0].title == "出价被超过"
    assert "拍品甲" in due[0].body
    # 收走之后不重复触发
    assert board.take_due(NOW + timedelta(seconds=1)) == ()


def test_different_msg_ids_both_fire():
    board = NoticeBoard()
    board.add_outbid(msg_id=1, asin="A1", title="a", now=NOW)
    board.add_outbid(msg_id=2, asin="A1", title="a", now=NOW)
    assert len(board.take_due(NOW)) == 2


# ---------------------------------------------------------------- 触发与投递


def test_take_due_delivers_to_notifier_and_removes():
    rec = _Recorder()
    board = NoticeBoard(notifier=rec)
    board.sync_ending([_state(end_at=NOW + timedelta(minutes=20))], points_minutes=(15.0,), now=NOW)
    # due_at = NOW+5min；NOW 时还没到点
    assert board.take_due(NOW) == ()
    due = board.take_due(NOW + timedelta(minutes=5, seconds=1))
    assert len(due) == 1
    assert len(rec.notes) == 1
    assert rec.notes[0].kind == KIND_ENDING
    assert rec.notes[0].asin == "A1"
    # 板上已收走
    assert board.pending_count(NOW + timedelta(minutes=6)) == 0


def test_broken_notifier_does_not_break_take_due():
    board = NoticeBoard(notifier=_Broken())
    board.add_outbid(msg_id=1, asin="A1", title="t", now=NOW)
    due = board.take_due(NOW)
    assert len(due) == 1, "投递失败也要把条目收走，不能每秒重复炸"


def test_missed_notices_are_dropped_not_backfilled():
    """程序没在跑时错过的提醒点：sync 直接丢弃，不补发。"""
    board = NoticeBoard()
    # 结束时间只剩 10 分钟，15 分钟提醒点已错过
    board.sync_ending([_state(end_at=NOW + timedelta(minutes=10))], points_minutes=(15.0,), now=NOW)
    assert board.snapshot(NOW) == ()
    assert board.take_due(NOW) == ()


# ---------------------------------------------------------------- 杂项


def test_fmt_minutes():
    assert _fmt_minutes(15.0) == "15 分钟"
    assert _fmt_minutes(60.0) == "1 小时"
    assert _fmt_minutes(360.0) == "6 小时"
    assert _fmt_minutes(90.0) == "90 分钟"


def test_notice_is_frozen():
    n = Notice(kind=KIND_ENDING, asin="A", title="t", body="b", due_at=NOW)
    with pytest.raises(AttributeError):  # frozen dataclass → FrozenInstanceError
        n.asin = "B"  # type: ignore[misc]


# ---------------------------------------------------------------- 提醒的节奏（notify.*）


def test_normalize_points_sorts_far_to_near():
    """用户 m21656：按时间大小排序，远的在前。"""
    assert normalize_points([6, 15, 60, 180, 360]) == (360.0, 180.0, 60.0, 15.0, 6.0)


def test_normalize_points_drops_duplicates():
    """同一个点填两遍不该响两次。"""
    assert normalize_points([60, 60.0, 15, 15]) == (60.0, 15.0)


def test_normalize_points_drops_non_positive_and_junk():
    assert normalize_points([15, 0, -5, 60]) == (60.0, 15.0)
    assert normalize_points(["15", "60"]) == (60.0, 15.0)
    assert normalize_points(["abc", None]) == ()


def test_normalize_points_is_capped():
    """配置是手填的一组数，但真塞几千个点时界面与扫描都白白变慢。

    截掉的必须是**最远**那批：越靠近结束越值钱，"7 天前每小时响一次"没有意义。
    """
    points = normalize_points(range(1, 500))
    assert len(points) == MAX_ENDING_POINTS
    assert points[-1] == 1.0, "最靠近结束的那个点必须留下"
    assert points[0] == 60.0, "留下的最近 60 个点，最大的那个是 60"
    assert points == tuple(sorted(points, reverse=True)), "仍按远到近排列"


def test_normalize_points_keeps_three_decimals():
    assert normalize_points([15.0004]) == (15.0,)


def test_the_window_can_be_changed_at_runtime():
    """``notify.window_minutes`` 改完立刻生效：不重建通知板，只换窗口。"""
    board = NoticeBoard()
    board.sync_ending(
        [_state("mid", end_at=NOW + timedelta(hours=3))],
        points_minutes=(15.0,),
        now=NOW,
    )
    assert [n.asin for n in board.snapshot(NOW)] == []  # 2 小时窗口外
    board.set_window(timedelta(hours=4))
    assert [n.asin for n in board.snapshot(NOW)] == ["mid"]


# ---------------------------------------------------------------- 与刷新循环的接线


def _mini_refresher(tmp_path, *, notices=None, notifier=None):
    from meruki_tracker.core.adapters.store.credentials import CredentialStore
    from meruki_tracker.core.adapters.store.observations import ObservationStore
    from meruki_tracker.core.adapters.store.snapshot import SnapshotStore
    from meruki_tracker.core.app.refresher import Refresher, RefresherOptions

    path = tmp_path / "n.db"
    creds = CredentialStore(path)
    creds.save("kmgSession=FAKE")
    return Refresher(
        api=object(),  # type: ignore[arg-type]  # _notify_outbid 不碰 api
        snapshots=SnapshotStore(path),
        credentials=creds,
        observations=ObservationStore(tmp_path / "obs.db"),
        options=RefresherOptions(detail_enabled=False),
        notifier=notifier,
        notices=notices,
    )


def test_outbid_registers_on_board_and_fires_via_take_due(tmp_path):
    """有通知板：只注册不直投；到点由 take_due 交给板上的 Notifier。"""
    from meruki_tracker.core.adapters.store.messages import MessageRow

    rec = _Recorder()
    board = NoticeBoard(notifier=rec)
    refresher = _mini_refresher(tmp_path, notices=board, notifier=_Recorder())
    rows = [
        MessageRow(msg_id=5, asin="A1", event="出价被超过", title="拍品甲"),
        MessageRow(msg_id=6, asin="", event="出价被超过", title="没有 asin 的不要"),
        MessageRow(msg_id=7, asin="A2", event="别的", title="别的"),
    ]
    refresher._notify_outbid(rows, now=NOW)
    assert board.pending_count(NOW) == 1
    assert rec.notes == [], "注册阶段不该直接投递"

    due = board.take_due(NOW)
    assert len(due) == 1
    assert len(rec.notes) == 1
    assert rec.notes[0].kind == "outbid"


def test_outbid_without_board_falls_back_to_notifier(tmp_path):
    """没有通知板（旧接线 / 测试替身）：保持直接投递的老行为。"""
    from meruki_tracker.core.adapters.store.messages import MessageRow

    rec = _Recorder()
    refresher = _mini_refresher(tmp_path, notices=None, notifier=rec)
    refresher._notify_outbid(
        [MessageRow(msg_id=5, asin="A1", event="出价被超过", title="拍品甲")], now=NOW
    )
    assert len(rec.notes) == 1


@pytest.mark.asyncio
async def test_refresh_syncs_ending_notices_from_latest_snapshot(tmp_path):
    """刷新成功后板上有「结束前 N 分钟」提醒：A 类通知随快照自动重算。"""
    from zoneinfo import ZoneInfo

    from meruki_tracker.core.adapters.meruki.api import DetailInfo, ListItem, ListPage, MyBidPage
    from meruki_tracker.core.adapters.store.credentials import CredentialStore
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.adapters.store.mirror import MirrorStore
    from meruki_tracker.core.adapters.store.observations import ObservationStore
    from meruki_tracker.core.adapters.store.snapshot import Snapshot, SnapshotStore
    from meruki_tracker.core.app.refresher import Refresher, RefresherOptions

    site = ZoneInfo("Asia/Shanghai")
    # BidDeadline 是站点本地时间（+08）；结束定在真实 90 分钟后，
    # 复查点 (60, 15) 的 due 都落在 2 小时窗口内
    deadline = datetime.now(site) + timedelta(minutes=90)

    class _Api:
        cookie = "kmgSession=FAKE"

        async def fetch_list_page(self, page, *, status="进行中"):
            return ListPage(
                items=[
                    ListItem(
                        asin="A1",
                        item_id="1",
                        name="拍品甲",
                        url="u",
                        detail_url="aa",
                        bid_deadline=deadline.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                ],
                total_item=1,
                current_page=1,
                total_page=1,
            )

        async def fetch_detail(self, detail_url_hex):
            # 列表新发现的拍品 end_time 是 PENDING（红线：精确时间只信详情），
            # 详情拉到后才变 EXACT——提醒点也只在 EXACT 之后才注册
            return DetailInfo(
                asin="A1",
                end_at=datetime.now(UTC) + timedelta(minutes=90),
                ended=False,
                bid_price=100.0,
                buy_now_price=None,
                auto_extend=None,
                early_termination=None,
                name="拍品甲",
            )

        async def fetch_my_bids(self, *, status: str = "竞拍中", page: int = 1):
            """这些用例不关心「我的竞拍」：假装我一条都没出过价。"""
            return MyBidPage(items=(), total_item=0, current_page=1, total_page=1, status=status)

        async def aclose(self):
            return None

    path = tmp_path / "r.db"
    conn = connect(path)  # 各 store 共用一条连接：分开连会在写快照时互锁
    creds = CredentialStore(path, conn=conn)
    creds.save("kmgSession=FAKE")
    board = NoticeBoard()
    refresher = Refresher(
        api=_Api(),
        snapshots=SnapshotStore(path, conn=conn),
        credentials=creds,
        observations=ObservationStore(path, conn=conn),
        mirror=MirrorStore(path, conn=conn),
        options=RefresherOptions(
            detail_enabled=True,
            # 提醒点与"多久拉一次详情"彻底无关：详情现在每轮全拉，提醒只看这组点。
            notify_points_minutes=(60.0, 40.0, 20.0),
        ),
        notices=board,
    )
    _snap, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error
    entries = board.snapshot(datetime.now(UTC))
    assert len(entries) == 3
    assert {n.minutes_before_end for n in entries} == {60.0, 40.0, 20.0}, (
        "提醒点来自 notify.*，不是复查点"
    )
    assert all(n.asin == "A1" for n in entries)
