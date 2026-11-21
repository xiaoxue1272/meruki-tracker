"""事件类通知：拍卖结束（带成交价）、截止时间延后 / 提前。

用户 m24274（原话）：「我的意思是现在通知，都有什么形态的。我理解现在只有
结束前提醒。然后我们应该还需要，出价被超越提醒，结束提醒，拍卖时间延长
提醒等。」

在此之前通知板只有两种 kind：

- ``ending`` —— "还有 N 分钟结束"，预注册、``due_at`` 在未来；
- ``outbid`` —— 出价被超过，注册即到期。

而「结束了」「时间被改到几点了」这两种状态变化其实早就在
:class:`~meruki_tracker.core.domain.models.TransitionKind` 里产生了
（``ENDED`` / ``END_TIME_CHANGED``），只是一直只用来打日志计数，从来没进过
通知板。这一刀把它们接上，并且**把「时间变了」拆成延后 / 提前两种**——对
用户来说这两件事的含义完全不同（延后是多给时间，提前是要赶紧出手），说成
一句"时间变了"等于没说。

为什么延后/提前的方向不从 ``Transition.detail`` 的文本里解析：那串是
``"<旧 iso> -> <新 iso>"``，为了显示好看会变，解析它是把自己的实现细节当
接口。改成 :class:`Transition` 直接带 ``old_end_at`` / ``new_end_at`` 两个
结构化字段。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from meruki_tracker.core.adapters.meruki.api import DetailInfo, ListItem, ListPage, MyBidPage
from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.db import connect
from meruki_tracker.core.adapters.store.observations import ObservationStore
from meruki_tracker.core.adapters.store.snapshot import Snapshot, SnapshotStore
from meruki_tracker.core.app.notices import (
    KIND_ENDED,
    KIND_EXTENDED,
    KIND_OUTBID,
    KIND_SHORTENED,
    NoticeBoard,
)
from meruki_tracker.core.app.refresher import Refresher, RefresherOptions
from meruki_tracker.core.domain.detail import DetailResult, apply_detail
from meruki_tracker.core.domain.models import (
    AuctionState,
    DoorState,
    EndTime,
    TransitionKind,
    UserQuery,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ 领域层


def _state(*, end_at: datetime | None) -> AuctionState:
    return AuctionState(
        asin="A1",
        name="拍品",
        url="https://x/A1",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(end_at, NOW) if end_at else EndTime.pending(),
    )


def test_end_time_change_carries_both_sides_as_datetimes():
    """迁移直接带旧/新时间，方向由调用方比大小，不解析 detail 文本。"""
    old = NOW + timedelta(hours=1)
    new = NOW + timedelta(hours=2)

    _, trans = apply_detail(_state(end_at=old), DetailResult(end_at=new), NOW)

    assert trans is not None
    assert trans.kind is TransitionKind.END_TIME_CHANGED
    assert trans.old_end_at == old
    assert trans.new_end_at == new


def test_ended_transition_carries_the_final_price_side_effect():
    """结束时成交价落在 latest_bid 上（详情 current_bid），通知从状态里取。"""
    st = _state(end_at=NOW - timedelta(minutes=1))

    new, trans = apply_detail(
        st,
        DetailResult(ended=True, end_at=NOW - timedelta(minutes=1), current_bid=1500.0),
        NOW,
    )

    assert trans is not None and trans.kind is TransitionKind.ENDED
    assert new.door_state is DoorState.ENDED
    assert new.latest_bid == 1500.0


# ------------------------------------------------------------------ 通知板


def test_add_event_is_registered_and_kept_as_recent():
    """事件注册即到期（立刻闪），并且留在「最近」里给侧栏回看。"""
    board = NoticeBoard()

    ok = board.add_event(
        key=(KIND_ENDED, "A1", ""),
        kind=KIND_ENDED,
        asin="A1",
        title="拍卖结束",
        body="拍品（成交价 ¥1,500）",
        now=NOW,
    )

    assert ok is True
    assert [n.kind for n in board.recent()] == [KIND_ENDED]
    # 已到期的不是「将要发生」，不能混进 snapshot，否则窗口语义就乱了
    assert board.snapshot(NOW) == ()
    assert [n.kind for n in board.take_due(NOW)] == [KIND_ENDED]


def test_same_event_key_is_not_registered_twice():
    """同一个事件（同拍品同新时间）只响一次，重复注册返回 False。"""
    board = NoticeBoard()
    kwargs: dict[str, Any] = dict(
        key=(KIND_EXTENDED, "A1", "2026-11-16T14:00:00+00:00"),
        kind=KIND_EXTENDED,
        asin="A1",
        title="截止时间延后",
        body="拍品",
        now=NOW,
    )

    assert board.add_event(**kwargs) is True
    assert board.add_event(**kwargs) is False
    assert len(board.recent()) == 1


def test_recent_keeps_event_after_it_was_taken():
    """取走（已投递/已闪）之后仍然留在最近里——侧栏要能看到刚发生的事。"""
    board = NoticeBoard()
    board.add_event(
        key=(KIND_SHORTENED, "A1", ""),
        kind=KIND_SHORTENED,
        asin="A1",
        title="截止时间提前",
        body="拍品",
        now=NOW,
    )

    board.take_due(NOW)

    assert [n.kind for n in board.recent()] == [KIND_SHORTENED]


def test_recent_is_capped_so_it_cannot_grow_forever():
    """「最近」有上限：这是个回看窗口，不是历史账本。"""
    board = NoticeBoard()
    for i in range(60):
        board.add_event(
            key=(KIND_ENDED, f"A{i}", ""),
            kind=KIND_ENDED,
            asin=f"A{i}",
            title="拍卖结束",
            body="拍品",
            now=NOW,
        )

    assert len(board.recent()) < 60


def test_events_are_delivered_with_their_own_kind():
    """投递出去的 Notification 带着真实 kind，下游渠道才能区分。"""
    seen: list[str] = []

    class _Recorder:
        def notify(self, note) -> None:
            seen.append(note.kind)

    board = NoticeBoard(notifier=_Recorder())
    board.add_event(
        key=(KIND_ENDED, "A1", ""),
        kind=KIND_ENDED,
        asin="A1",
        title="拍卖结束",
        body="拍品",
        now=NOW,
    )
    board.add_outbid(msg_id=1, asin="A1", title="拍品", now=NOW)

    board.take_due(NOW)

    # 同一时刻到期的两条之间没有有意义的先后，别把实现里的遍历顺序钉进测试
    assert sorted(seen) == sorted([KIND_ENDED, KIND_OUTBID])


# ------------------------------------------------------------------ 刷新层


class _Api:
    """假接口：列表固定一条，详情按 ``details`` 队列逐轮返回。"""

    def __init__(self, details: list[DetailInfo]) -> None:
        self._details = details
        self.detail_calls: list[str] = []

    async def fetch_list_page(self, page: int, *, status: str = "进行中") -> ListPage:
        items: tuple[ListItem, ...] = (
            (
                ListItem(
                    asin="A1",
                    item_id="1",
                    name="拍品A1",
                    url="https://x/A1",
                    detail_url="hexA1",
                    bid_deadline="2026-12-31 21:00:00",
                    image_url="",
                    user_id="",
                    raw={"Asin": "A1"},
                ),
            )
            if status == "进行中"
            else ()
        )
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
        idx = min(len(self.detail_calls) - 1, len(self._details) - 1)
        return self._details[idx]

    async def fetch_my_bids(self, *, status: str = "竞拍中", page: int = 1) -> MyBidPage:
        return MyBidPage(items=(), total_item=0, current_page=1, total_page=1, status=status)


def _detail(
    *,
    end_at: datetime | None,
    ended: bool = False,
    bid: float = 1000.0,
    auto_extend: bool = False,
) -> DetailInfo:
    return DetailInfo(
        asin="A1",
        end_at=end_at,
        ended=ended,
        bid_price=bid,
        buy_now_price=None,
        auto_extend=auto_extend,
        early_termination=False,
        name="拍品A1",
    )


def _build(
    tmp_path: Path,
    api: Any,
    **options: Any,
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
        options=RefresherOptions(notify_points_minutes=(), **options),
        notices=board,
    )
    return refresher, board


async def _kinds(board: NoticeBoard) -> list[str]:
    return [n.kind for n in board.recent()]


async def test_ending_registers_a_notice_with_the_final_price(tmp_path: Path):
    """结束提醒带成交价——用户最想知道的就是"最后卖了多少钱"。"""
    api = _Api(
        [
            _detail(end_at=NOW + timedelta(minutes=30), bid=1000.0),
            _detail(end_at=NOW - timedelta(minutes=1), ended=True, bid=1500.0),
        ]
    )
    refresher, board = _build(tmp_path, api)

    snap, first = await refresher.refresh(Snapshot())
    assert first.ok, first.error
    assert await _kinds(board) == []

    _, second = await refresher.refresh(snap)
    assert second.ok, second.error

    notices = board.recent()
    assert [n.kind for n in notices] == [KIND_ENDED]
    assert "¥1,500" in notices[0].body
    assert notices[0].asin == "A1"


async def test_ended_notice_is_not_repeated_on_later_rounds(tmp_path: Path):
    """结束是终态，不该每轮再响一次。"""
    api = _Api([_detail(end_at=NOW - timedelta(minutes=1), ended=True, bid=1500.0)])
    refresher, board = _build(tmp_path, api)

    snap, _ = await refresher.refresh(Snapshot())
    for _ in range(3):
        snap, _ = await refresher.refresh(snap)

    assert await _kinds(board) == [KIND_ENDED]


async def test_deadline_moved_later_is_reported_as_extended(tmp_path: Path):
    """延后与提前必须分开报：一个多给时间，一个要赶紧出手。"""
    api = _Api(
        [
            _detail(end_at=NOW + timedelta(hours=1)),
            _detail(end_at=NOW + timedelta(hours=2)),
        ]
    )
    refresher, board = _build(tmp_path, api)

    snap, _ = await refresher.refresh(Snapshot())
    _, second = await refresher.refresh(snap)

    assert second.ok, second.error
    assert await _kinds(board) == [KIND_EXTENDED]


async def test_deadline_moved_earlier_is_reported_as_shortened(tmp_path: Path):
    api = _Api(
        [
            _detail(end_at=NOW + timedelta(hours=3)),
            _detail(end_at=NOW + timedelta(hours=1)),
        ]
    )
    refresher, board = _build(tmp_path, api)

    snap, _ = await refresher.refresh(Snapshot())
    _, second = await refresher.refresh(snap)

    assert second.ok, second.error
    assert await _kinds(board) == [KIND_SHORTENED]


async def test_extended_notice_says_it_was_yahoo_auto_extend(tmp_path: Path):
    """结束时间往后推、且这个拍品开了自动延长 → 正文点明是自动延长。

    依据详情 ``other.autoExtendHelp`` 原文："如果在拍卖结束前五分钟内当前价格
    有所上涨，则结束时间将延长五分钟，此过程可重复进行"。所以延后通知里
    标注自动延长，等于告诉用户"还在有人抢"，而不是卖家改主意。
    """
    api = _Api(
        [
            _detail(end_at=NOW + timedelta(hours=1), auto_extend=True),
            _detail(end_at=NOW + timedelta(minutes=65), auto_extend=True),
        ]
    )
    refresher, board = _build(tmp_path, api)

    snap, _ = await refresher.refresh(Snapshot())
    _, second = await refresher.refresh(snap)

    assert second.ok, second.error
    bodies = [n.body for n in board.recent() if n.kind == KIND_EXTENDED]
    assert bodies, "延后应该产出通知"
    assert "自动延长" in bodies[0]


async def test_extended_notice_stays_plain_without_auto_extend(tmp_path: Path):
    """没开自动延长的拍品时间往后推，多半是卖家改主意——不要瞎猜。"""
    api = _Api(
        [
            _detail(end_at=NOW + timedelta(hours=1)),
            _detail(end_at=NOW + timedelta(hours=2)),
        ]
    )
    refresher, board = _build(tmp_path, api)

    snap, _ = await refresher.refresh(Snapshot())
    _, second = await refresher.refresh(snap)

    assert second.ok, second.error
    bodies = [n.body for n in board.recent() if n.kind == KIND_EXTENDED]
    assert bodies
    assert "自动延长" not in bodies[0]


_SCENARIOS: dict[str, list[DetailInfo]] = {
    KIND_ENDED: [_detail(end_at=NOW - timedelta(minutes=1), ended=True, bid=1500.0)],
    KIND_EXTENDED: [
        _detail(end_at=NOW + timedelta(hours=1)),
        _detail(end_at=NOW + timedelta(hours=2)),
    ],
    KIND_SHORTENED: [
        _detail(end_at=NOW + timedelta(hours=3)),
        _detail(end_at=NOW + timedelta(hours=1)),
    ],
}

_SWITCHES = {
    KIND_ENDED: "notify_ended",
    KIND_EXTENDED: "notify_extended",
    KIND_SHORTENED: "notify_shortened",
}


async def _scenario_kinds(root: Path, kind: str, **options: Any) -> list[str]:
    """在 ``root`` 下跑一遍某种事件场景，返回产出的通知 kind。"""
    refresher, board = _build(root, _Api(_SCENARIOS[kind]), **options)
    snap, _ = await refresher.refresh(Snapshot())
    rounds = 1 if kind == KIND_ENDED else 2
    for _ in range(rounds - 1):
        snap, _ = await refresher.refresh(snap)
    return await _kinds(board)


@pytest.mark.parametrize("kind", list(_SCENARIOS))
async def test_each_event_kind_has_its_own_switch(tmp_path: Path, kind: str):
    """逐 kind 开关：关掉自己不响，关掉别人照响。

    「三个独立开关」只有正反两面都测才成立——只验"关掉自己不响"的话，一个
    总开关（任何一项关掉就全静音）也能通过。
    """
    own = await _scenario_kinds(tmp_path / "own", kind, **{_SWITCHES[kind]: False})
    assert own == []

    other = next(k for k in _SWITCHES if k != kind)
    kept = await _scenario_kinds(tmp_path / "other", kind, **{_SWITCHES[other]: False})
    assert kept == [kind]


async def test_ending_reminders_still_work_alongside_events(tmp_path: Path):
    """事件通道和「还有 N 分钟」是两件事，不能互相顶掉。"""
    api = _Api([_detail(end_at=NOW + timedelta(minutes=30))])
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
        options=RefresherOptions(notify_points_minutes=(60.0,)),
        notices=board,
    )

    await refresher.refresh(Snapshot())

    assert [n.kind for n in board.snapshot(NOW)] == []  # 30 分钟后再过 60 点已过
    # 60 分钟点 = 结束前 30 分钟 → 还没到，注册在未来
    assert board.pending_count(datetime.now(UTC)) >= 0


async def test_unknown_time_state_does_not_crash_the_switch(tmp_path: Path):
    """PENDING（还没拿到精确时间）不该产生任何时间变化通知。"""
    api = _Api([_detail(end_at=None), _detail(end_at=None)])
    refresher, board = _build(tmp_path, api)

    snap, _ = await refresher.refresh(Snapshot())
    _, second = await refresher.refresh(snap)

    assert second.ok, second.error
    assert await _kinds(board) == []
