"""站内消息通道：「出价被超过」的解析、落库与触发器。

分三块：
- :mod:`meruki_tracker.core.domain.messages` 的纯解析（消息 → 订单 → asin 映射）
- :mod:`meruki_tracker.core.adapters.store.messages` 的落库与水位线
- :mod:`meruki_tracker.core.app.refresher` 的接线：消息触发插队拉详情、失败不影响刷新

**为什么这些测试值得写**

「出价被超过」是本项目唯一一条**服务端主动告诉我们**的价格信号，链路长
（消息 → 订单短 ID → 订单详情 → hex url → asin → 强制拉详情），任何一环
断了都只是"悄悄不起作用"，不会报错。没有测试就只能靠运气发现。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from meruki_tracker.core.adapters.meruki.api import DetailInfo, ListPage, MyBidPage
from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.db import connect
from meruki_tracker.core.adapters.store.messages import MessageRow, MessageStore
from meruki_tracker.core.adapters.store.mirror import MirrorStore
from meruki_tracker.core.adapters.store.observations import ObservationStore
from meruki_tracker.core.adapters.store.snapshot import Snapshot, SnapshotStore
from meruki_tracker.core.app.notifications import (
    ChannelsConfig,
    Notification,
    NullNotifier,
    build_notifier,
)
from meruki_tracker.core.app.refresher import Refresher, RefresherOptions
from meruki_tracker.core.domain.messages import (
    asin_from_yahoo_url,
    parse_bid_events,
    parse_messages,
    pick_new,
)
from meruki_tracker.core.errors import NetworkError

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

# 真实的订单详情 url（hex 编码的雅虎拍卖地址，末段是 1000000002）
_HEX_URL = "68747470733a2f2f61756374696f6e732e7961686f6f2e636f2e6a702f6a702f61756374696f6e2f31303030303030303032"


# ---------------------------------------------------------------- 纯解析


def test_asin_from_hex_encoded_yahoo_url():
    """订单详情里的 url 是 hex 编码的，要能解出 asin。"""
    assert asin_from_yahoo_url(_HEX_URL) == "1000000002"


def test_asin_from_plain_url_also_works():
    """明文地址也认（测试与将来接口改回明文都用得上）。"""
    assert asin_from_yahoo_url("https://auctions.yahoo.co.jp/jp/auction/1000000002") == "1000000002"


def test_asin_keeps_single_letter_prefix():
    """asin 允许单个字母前缀（约 94% 的拍品都有）。"""
    assert (
        asin_from_yahoo_url("https://auctions.yahoo.co.jp/jp/auction/j1000000012") == "j1000000012"
    )


def test_asin_handles_trailing_slash():
    assert (
        asin_from_yahoo_url("https://auctions.yahoo.co.jp/jp/auction/v1000000001/") == "v1000000001"
    )


@pytest.mark.parametrize("raw", ["", "   ", "not-hex-not-url", "https://example.com/foo"])
def test_asin_returns_empty_for_unparseable(raw: str):
    """解不出来必须返回空串，而不是抛异常或返回半截路径。

    调用方靠"空串 = 没拿到"来跳过这条消息；抛异常会让整轮刷新失败。
    """
    assert asin_from_yahoo_url(raw) == ""


def test_parse_bid_events_sorts_old_to_new():
    """log 数组是**新到旧**返回的，解析后要翻转成旧到新。"""
    detail = {
        "url": _HEX_URL,
        "log": [
            {"Event": "竞拍失败", "Time": "2026-10-30 03:19"},
            {"Event": "出价被超过", "Time": "2026-10-29 21:30"},
            {"Event": "出价成功", "Time": "2026-10-29 21:24"},
        ],
    }
    events = parse_bid_events("DEMOORD00002", detail)
    assert [e.event for e in events] == ["出价成功", "出价被超过", "竞拍失败"]
    assert all(e.asin == "1000000002" for e in events)


def test_parse_bid_events_marks_outbid():
    detail = {"url": _HEX_URL, "log": [{"Event": "出价被超过", "Time": "2026-10-29 21:30"}]}
    events = parse_bid_events("X", detail)
    assert events[0].is_outbid is True


def test_parse_bid_events_empty_when_url_missing():
    """没有 url 就解不出 asin，事件对调用方没用——返回空列表而不是半成品。"""
    assert (
        parse_bid_events("X", {"log": [{"Event": "出价被超过", "Time": "2026-10-29 21:30"}]}) == []
    )


def test_parse_bid_events_tolerates_garbage():
    """订单详情字段变了不能让刷新挂掉。"""
    assert parse_bid_events("X", {"url": _HEX_URL, "log": "不是数组"}) == []
    assert parse_bid_events("X", {"url": _HEX_URL, "log": [None, 1, {"noEvent": 1}]}) == []
    assert parse_bid_events("X", None) == []


def test_parse_messages_skips_blank_titles():
    """空标题的消息确实存在（实测 30 条里 2 条），没有标题就判断不了类型。"""
    data = {
        "Data": [
            {"id": 3, "name": "拍卖信息", "title": "您的拍卖 ABCDEF123456 价格被超过"},
            {"id": 2, "name": "拍卖信息", "title": ""},
            {"id": 1, "name": "拍卖信息", "title": "   "},
        ]
    }
    assert [n.msg_id for n in parse_messages(data)] == [3]


def test_parse_messages_tolerates_garbage():
    assert parse_messages(None) == []
    assert parse_messages({}) == []
    assert parse_messages({"Data": "不是数组"}) == []
    assert parse_messages({"Data": [None, {"id": "不是数字"}]}) == []


def test_message_order_id_extraction():
    """标题里的 12 位大写是订单短 ID。"""
    data = {"Data": [{"id": 1, "title": "您的拍卖 DEMOORD00002 价格被超过", "name": "拍卖信息"}]}
    notice = parse_messages(data)[0]
    assert notice.order_id == "DEMOORD00002"
    assert notice.is_outbid is True


def test_message_without_order_id_still_parses():
    """抠不到订单号时消息本身仍要能解析出来（落一条无 asin 的记录供排查）。"""
    data = {"Data": [{"id": 1, "title": "价格被超过", "name": "拍卖信息"}]}
    notice = parse_messages(data)[0]
    assert notice.order_id == ""
    assert notice.is_outbid is True


def test_non_outbid_message_is_not_outbid():
    data = {"Data": [{"id": 1, "title": "您的订单 ABCDEF123456 已入库", "name": "交易信息"}]}
    assert parse_messages(data)[0].is_outbid is False


def test_pick_new_returns_oldest_first():
    """同一拍品连发多条时要按时间顺序处理，倒序会让最新状态被旧消息覆盖。"""
    data = {
        "Data": [
            {"id": 105, "title": "您的拍卖 AAAAAAAAAAAA 价格被超过"},
            {"id": 103, "title": "您的拍卖 AAAAAAAAAAAA 价格被超过"},
            {"id": 101, "title": "您的拍卖 AAAAAAAAAAAA 价格被超过"},
        ]
    }
    fresh = pick_new(parse_messages(data), since_id=100)
    assert [n.msg_id for n in fresh] == [101, 103, 105]


def test_pick_new_filters_by_watermark():
    data = {"Data": [{"id": 105, "title": "您的拍卖 AAAAAAAAAAAA 价格被超过"}]}
    assert pick_new(parse_messages(data), since_id=105) == []


# ---------------------------------------------------------------- 存储层


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "t.db"


def test_watermark_starts_at_zero(db_path: Path):
    assert MessageStore(db_path).watermark() == 0


def test_watermark_only_goes_up(db_path: Path):
    """回退水位线会让旧消息被重复处理（重复拉订单详情 = 白费请求）。"""
    store = MessageStore(db_path)
    store.set_watermark(100)
    store.set_watermark(50)
    assert store.watermark() == 100


def test_save_events_is_idempotent(db_path: Path):
    """同一条消息重复落库只能留一行——唯一索引 + INSERT OR IGNORE。"""
    store = MessageStore(db_path)
    row = MessageRow(msg_id=101, order_id="XYZ", asin="A1", event="出价被超过")
    assert store.save_events([row], seen_at=NOW) == 1
    assert store.save_events([row], seen_at=NOW) == 0
    assert store.count() == 1


def test_save_events_keeps_the_whole_log_of_one_message(db_path: Path):
    """一条消息会带出订单的整条 log，这些事件共用同一个 msg_id，必须都留下。

    踩过的坑：唯一索引最初只建在 ``msg_id`` 上，结果一条消息只落下第一行。
    而 log 是新到旧排列的，第一条是「竞拍失败」或「用户取消」——最该看的
    「出价被超过」反而被丢掉了，而且丢得悄无声息（``OR IGNORE`` 不报错）。
    """
    store = MessageStore(db_path)
    rows = [
        MessageRow(msg_id=101, order_id="XYZ", asin="A1", event="出价成功", happened_at="21:24"),
        MessageRow(msg_id=101, order_id="XYZ", asin="A1", event="出价被超过", happened_at="21:30"),
        MessageRow(
            msg_id=101, order_id="XYZ", asin="A1", event="竞拍失败", happened_at="2026-10-30 03:19"
        ),
    ]
    assert store.save_events(rows, seen_at=NOW) == 3
    assert store.count() == 3
    assert [e.event for e in store.events(asin="A1")] == ["出价成功", "出价被超过", "竞拍失败"]
    assert store.latest_outbid("A1") is not None


def test_save_events_still_dedupes_identical_events(db_path: Path):
    """放宽唯一索引之后，"同一个事件"仍然只能落一次（重放安全）。"""
    store = MessageStore(db_path)
    row = MessageRow(msg_id=101, asin="A1", event="出价被超过", happened_at="21:30")
    assert store.save_events([row], seen_at=NOW) == 1
    assert store.save_events([row], seen_at=NOW) == 0
    assert store.count() == 1


def test_v3_database_is_upgraded_to_allow_multiple_events(tmp_path: Path):
    """老库（v3，唯一索引只在 msg_id 上）升级后要能落下整条事件流。

    这是真实的升级路径：先前的版本跑过一轮，``bid_events`` 里只有零星几行，
    索引也还是老样子。升级如果只是改版本号、不换索引，那条过于严格的唯一
    约束会一直留着，事件流永远只有第一条。
    """
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE bid_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER NOT NULL,
            order_id TEXT NOT NULL DEFAULT '',
            asin TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL DEFAULT '',
            happened_at TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            notified INTEGER NOT NULL DEFAULT 0,
            seen_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX ux_bid_events_msg ON bid_events(msg_id);
        INSERT INTO meta VALUES('schema_version','3');
        INSERT INTO bid_events(msg_id, asin, event, seen_at)
            VALUES(101,'A1','用户取消','x');
        """
    )
    conn.commit()
    conn.close()

    store = MessageStore(path)
    # 老数据还在，且现在能补上同一条消息的其余事件
    assert store.count() == 1
    added = store.save_events(
        [
            MessageRow(msg_id=101, asin="A1", event="出价被超过", happened_at="21:30"),
            MessageRow(msg_id=101, asin="A1", event="竞拍失败", happened_at="22:00"),
        ],
        seen_at=NOW,
    )
    assert added == 2
    assert store.count() == 3
    assert store.latest_outbid("A1") is not None


def test_latest_outbid(db_path: Path):
    store = MessageStore(db_path)
    store.save_events(
        [
            MessageRow(msg_id=101, order_id="X", asin="A1", event="出价成功"),
            MessageRow(msg_id=102, order_id="Y", asin="A1", event="出价被超过"),
        ],
        seen_at=NOW,
    )
    latest = store.latest_outbid("A1")
    assert latest is not None
    assert latest.msg_id == 102
    assert latest.is_outbid is True


def test_latest_outbid_returns_none_when_no_outbid(db_path: Path):
    store = MessageStore(db_path)
    store.save_events([MessageRow(msg_id=1, asin="A1", event="出价成功")], seen_at=NOW)
    assert store.latest_outbid("A1") is None


def test_events_returns_oldest_first(db_path: Path):
    store = MessageStore(db_path)
    store.save_events(
        [
            MessageRow(msg_id=103, asin="A1", event="出价被超过"),
            MessageRow(msg_id=101, asin="A1", event="出价成功"),
        ],
        seen_at=NOW,
    )
    assert [e.msg_id for e in store.events(asin="A1")] == [101, 103]


def test_events_empty_after_save_nothing(db_path: Path):
    assert MessageStore(db_path).save_events([], seen_at=NOW) == 0


# ---------------------------------------------------------------- 通知通道


def test_null_notifier_does_nothing():
    NullNotifier().notify(Notification(title="x", body="y"))


@pytest.mark.parametrize("channels", [None, {}, {"desktop": {"enable": True}}, {"wechat": "x"}])
def test_unknown_or_empty_channels_still_fall_back_to_null(channels):
    """没启用任何已知渠道（或写了不认识的名字）：什么都不发。"""
    notifier = build_notifier(channels)
    assert isinstance(notifier, NullNotifier)


def test_disabled_channels_are_not_built():
    """开关关着的时候，即使填了地址也不组装渠道。"""
    cfg = ChannelsConfig(webhook_enable=False, webhook_url="https://example.com/hook")
    assert isinstance(build_notifier(cfg), NullNotifier)


# ---------------------------------------------------------------- refresher 接线


class _ScriptedApi:
    """按脚本返回列表 + 消息 + 订单详情；记录收到过哪些请求。"""

    def __init__(
        self,
        *,
        items: tuple[Any, ...] = (),
        messages: dict[str, Any] | None = None,
        orders: dict[str, Any] | None = None,
        messages_error: Exception | None = None,
        order_error: Exception | None = None,
    ) -> None:
        self._items = items
        self._messages = messages or {"Data": []}
        self._orders = orders or {}
        self._messages_error = messages_error
        self._order_error = order_error
        self.cookie = "x"
        self.statuses: list[str] = []
        self.messages_calls = 0
        self.order_calls: list[str] = []
        self.detail_calls: list[str] = []

    async def fetch_list_page(self, page: int, *, status: str = "进行中") -> ListPage:
        self.statuses.append(status)
        return ListPage(
            items=self._items, total_item=len(self._items), current_page=1, total_page=1
        )

    async def fetch_my_bids(self, *, status: str = "竞拍中", page: int = 1):
        """这些用例不关心「我的竞拍」：假装我一条都没出过价。"""
        return MyBidPage(items=(), total_item=0, current_page=1, total_page=1, status=status)

    async def fetch_messages_raw(self, page: int = 1) -> dict[str, Any]:
        self.messages_calls += 1
        if self._messages_error is not None:
            raise self._messages_error
        return self._messages

    async def fetch_order_detail_raw(self, order_id: str) -> dict[str, Any]:
        self.order_calls.append(order_id)
        if self._order_error is not None:
            raise self._order_error
        return self._orders.get(order_id, {})

    async def fetch_detail(self, detail_url_hex: str) -> DetailInfo:
        self.detail_calls.append(detail_url_hex)
        # 结束时间必须相对**真实当前时间**：refresh 内部用 datetime.now(UTC)，
        # 写死一个过去的时刻会让拍品立刻落进 P0「到点待确认」，
        # 那样 force 有没有生效测试都会绿（假绿，实测过）。
        return DetailInfo(
            asin="A1",
            end_at=datetime.now(UTC) + timedelta(hours=3),
            ended=False,
            bid_price=999.0,
            buy_now_price=None,
            auto_extend=None,
            early_termination=None,
            name="n",
        )


def _make_refresher(tmp_path: Path, api: Any, *, messages: MessageStore | None = None) -> Any:
    path = tmp_path / "r.db"
    conn = connect(path)
    if messages is None:
        messages = MessageStore(path, conn=conn)
    creds = CredentialStore(path, conn=conn)
    creds.save("kmgSession=FAKE")  # 没有 cookie 的话 refresh 会直接返回 auth 失败
    return Refresher(
        api=api,
        snapshots=SnapshotStore(path, conn=conn),
        credentials=creds,
        observations=ObservationStore(path, conn=conn),
        mirror=MirrorStore(path, conn=conn),
        messages=messages,
        options=RefresherOptions(),
    )


def _empty_snapshot() -> Snapshot:
    return Snapshot(generated_at=NOW, last_attempt=None, account=None, auctions=())


def _list_item(asin: str) -> Any:
    from meruki_tracker.core.adapters.meruki.api import ListItem

    return ListItem(
        asin=asin,
        item_id="1",
        name=f"拍品 {asin}",
        url=f"https://auctions.yahoo.co.jp/jp/auction/{asin}",
        detail_url="aabb",
        # 同 fetch_detail：相对真实当前时间，避免一上来就被判成"已到结束时间"
        bid_deadline=(datetime.now(UTC) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
    )


@pytest.mark.asyncio
async def test_first_run_only_sets_watermark_and_does_not_backfill(tmp_path: Path):
    """首次接入不回溯历史：否则一上来就拉几十个订单详情，纯属自找风控。"""
    api = _ScriptedApi(
        items=(_list_item("A1"),),
        messages={
            "Data": [
                {"id": 900, "title": "您的拍卖 DEMOORD00002 价格被超过", "name": "拍卖信息"},
            ]
        },
        orders={"DEMOORD00002": {"url": _HEX_URL, "log": []}},
    )
    refresher = _make_refresher(tmp_path, api)
    await refresher.refresh(_empty_snapshot())
    assert api.order_calls == [], "首次运行不该去拉订单详情"
    assert refresher._messages.watermark() == 900


@pytest.mark.asyncio
async def test_outbid_message_forces_detail_fetch(tmp_path: Path):
    """收到「出价被超过」→ 该拍品插队拉详情，**不等常规复查点**。

    这是整条消息通道的目的：服务端说"有人超过你"，就该立刻看一眼当前价，
    而不是等下一个 6 小时 / 1 小时 / 15 分钟复查点。
    """
    path = tmp_path / "r.db"
    conn = connect(path)
    messages = MessageStore(path, conn=conn)
    messages.set_watermark(100)  # 假装已经接过消息
    target = "1000000002"
    api = _ScriptedApi(
        items=(_list_item(target),),
        messages={"Data": [{"id": 101, "title": "您的拍卖 DEMOORD00002 价格被超过"}]},
        orders={
            "DEMOORD00002": {
                "url": _HEX_URL,
                "log": [{"Event": "出价被超过", "Time": "2026-10-29 21:30"}],
            }
        },
    )
    refresher = _make_refresher(tmp_path, api, messages=messages)

    # 第 1 轮：把拍品拉进快照（此时详情地址还未知，_fill_details 会跳过）
    snap, _ = await refresher.refresh(_empty_snapshot())
    assert any(a.asin == target for a in snap.auctions)
    api.detail_calls.clear()

    # 第 2 轮：同一条消息还在（水位线没推，因为 101 > 100 但第一轮已处理过）
    # 重新造一条更新的消息，确保本轮又被触发
    api._messages = {"Data": [{"id": 102, "title": "您的拍卖 DEMOORD00002 价格被超过"}]}
    await refresher.refresh(snap)

    # 关键断言：该拍品的详情被强制拉过
    assert api.detail_calls, f"「出价被超过」应触发详情拉取，实际详情调用={api.detail_calls}"
    assert "1000000002" in {ev.asin for ev in messages.events()}


@pytest.mark.asyncio
async def test_message_fetch_failure_does_not_fail_refresh(tmp_path: Path):
    """消息是锦上添花：拉不到也不能让整轮刷新失败。"""
    api = _ScriptedApi(
        items=(_list_item("A1"),),
        messages_error=NetworkError("断了"),
    )
    refresher = _make_refresher(tmp_path, api)
    _, result = await refresher.refresh(_empty_snapshot())
    assert result.ok is True


@pytest.mark.asyncio
async def test_order_detail_failure_does_not_fail_refresh(tmp_path: Path):
    path = tmp_path / "r.db"
    conn = connect(path)
    messages = MessageStore(path, conn=conn)
    messages.set_watermark(100)
    api = _ScriptedApi(
        items=(_list_item("A1"),),
        messages={"Data": [{"id": 101, "title": "您的拍卖 DEMOORD00002 价格被超过"}]},
        order_error=NetworkError("订单挂了"),
    )
    refresher = _make_refresher(tmp_path, api, messages=messages)
    _, result = await refresher.refresh(_empty_snapshot())
    assert result.ok is True


@pytest.mark.asyncio
async def test_messages_disabled_skips_entirely(tmp_path: Path):
    """关掉开关后一个消息请求都不该发。"""
    path = tmp_path / "r.db"
    conn = connect(path)
    api = _ScriptedApi(items=(_list_item("A1"),))
    creds = CredentialStore(path, conn=conn)
    creds.save("kmgSession=FAKE")
    refresher = Refresher(
        api=api,
        snapshots=SnapshotStore(path, conn=conn),
        credentials=creds,
        observations=ObservationStore(path, conn=conn),
        mirror=MirrorStore(path, conn=conn),
        messages=MessageStore(path, conn=conn),
        options=RefresherOptions(
            messages_enabled=False,
        ),
    )
    await refresher.refresh(_empty_snapshot())
    assert api.messages_calls == 0


@pytest.mark.asyncio
async def test_watermark_advances_only_after_events_saved(tmp_path: Path):
    """事件落库之后才推水位线——崩在中间最多重复解析，不会丢消息。"""
    path = tmp_path / "r.db"
    conn = connect(path)
    messages = MessageStore(path, conn=conn)
    messages.set_watermark(100)
    api = _ScriptedApi(
        items=(_list_item("1000000002"),),
        messages={"Data": [{"id": 101, "title": "您的拍卖 DEMOORD00002 价格被超过"}]},
        orders={
            "DEMOORD00002": {
                "url": _HEX_URL,
                "log": [{"Event": "出价被超过", "Time": "2026-10-29 21:30"}],
            }
        },
    )
    refresher = _make_refresher(tmp_path, api, messages=messages)
    await refresher.refresh(_empty_snapshot())
    assert messages.watermark() == 101
    assert messages.count() >= 1


@pytest.mark.asyncio
async def test_non_outbid_message_advances_watermark_without_order_calls(tmp_path: Path):
    """不关心的消息也要推水位线，否则每轮都要重新扫一遍。"""
    path = tmp_path / "r.db"
    conn = connect(path)
    messages = MessageStore(path, conn=conn)
    messages.set_watermark(100)
    api = _ScriptedApi(
        items=(_list_item("A1"),),
        messages={
            "Data": [{"id": 105, "title": "您的订单 ABCDEF123456 已入库", "name": "交易信息"}]
        },
    )
    refresher = _make_refresher(tmp_path, api, messages=messages)
    await refresher.refresh(_empty_snapshot())
    assert api.order_calls == []
    assert messages.watermark() == 105


@pytest.mark.asyncio
async def test_failing_notifier_does_not_break_refresh(tmp_path: Path):
    """通知抛异常绝不能影响采集——实现方被骗了也得兜住。"""

    class _Broken:
        def notify(self, note: Notification) -> None:
            raise RuntimeError("通知炸了")

    path = tmp_path / "r.db"
    conn = connect(path)
    messages = MessageStore(path, conn=conn)
    messages.set_watermark(100)
    api = _ScriptedApi(
        items=(_list_item("A1"),),
        messages={"Data": [{"id": 101, "title": "您的拍卖 DEMOORD00002 价格被超过"}]},
        orders={
            "DEMOORD00002": {
                "url": _HEX_URL,
                "log": [{"Event": "出价被超过", "Time": "2026-10-29 21:30"}],
            }
        },
    )
    creds = CredentialStore(path, conn=conn)
    creds.save("kmgSession=FAKE")
    refresher = Refresher(
        api=api,
        snapshots=SnapshotStore(path, conn=conn),
        credentials=creds,
        observations=ObservationStore(path, conn=conn),
        mirror=MirrorStore(path, conn=conn),
        messages=messages,
        notifier=_Broken(),
        options=RefresherOptions(),
    )
    _, result = await refresher.refresh(_empty_snapshot())
    assert result.ok is True


# ---------------------------------------------------------------- 终端字符清洗（审计遗留 2）


def test_parse_messages_scrubs_terminal_control_characters():
    """消息标题/分类来自站内系统，进通知前必须洗掉终端控制字符。

    2026-11-18 审计遗留：拍品名和描述已接 ``scrub()``，但站内消息的标题会进
    通知 body（``add_outbid`` 用 ``title`` 拼正文），一条含 ESC 序列的标题能
    让终端清屏/改标题。消息也是网络输入，和卖家填的字没有本质区别。
    """
    data = {
        "Data": [
            {
                "id": 7,
                "name": "拍卖\x1b]0;伪造标题\x07信息",
                "title": "您的拍卖 ABCDEF123456 \x1b[2J价格被超过\x00",
                "createdTime": "2026-11-18\x1b[31m 12:00",
                "detailId": "D\x1b[2J123",
            }
        ]
    }
    notice = parse_messages(data)[0]
    assert "\x1b" not in notice.title
    assert "\x00" not in notice.title
    assert "\x1b" not in notice.category
    assert "\x1b" not in notice.created_at
    assert "\x1b" not in notice.detail_id
    # 清洗不能破坏业务判断：去控制字符后订单号、类型还得认得出来
    assert notice.order_id == "ABCDEF123456"
    assert notice.is_outbid is True
