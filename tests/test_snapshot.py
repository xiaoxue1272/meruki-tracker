"""快照原子性与失败语义测试。"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from meruki_tracker.core.adapters.store.snapshot import (
    Account,
    LastAttempt,
    Snapshot,
    SnapshotStore,
)
from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, TimeState, UserQuery


def make_state(asin="A1") -> AuctionState:
    return AuctionState(
        asin=asin,
        name="n",
        url="u",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(datetime(2026, 10, 29, 12, 0, tzinfo=UTC), datetime.now(UTC)),
        latest_bid=100.0,
    )


def test_roundtrip(tmp_path):
    store = SnapshotStore(tmp_path / "meruki.db")
    snap = Snapshot(
        generated_at=datetime.now(UTC),
        last_attempt=LastAttempt(at=datetime.now(UTC), ok=True),
        account=Account(label="me", nickname="nick"),
        auctions=(make_state(),),
    )
    store.write(snap)
    back = store.read()
    assert back is not None
    assert back.account.nickname == "nick"
    assert back.auctions[0].asin == "A1"
    assert back.auctions[0].end_time.state is TimeState.EXACT


def test_rewrite_replaces_instead_of_appending(tmp_path):
    """整份覆盖写：第二次写不该留下上一份的行（以前是 json 整份替换，现在是 DELETE + INSERT）。"""
    path = tmp_path / "meruki.db"
    store = SnapshotStore(path)
    store.write(Snapshot(auctions=(make_state(),)))
    store.write(Snapshot(auctions=(make_state("A2"),)))
    back = store.read()
    assert back is not None
    assert [a.asin for a in back.auctions] == ["A2"]


def test_file_permissions_are_0600(tmp_path):
    path = tmp_path / "meruki.db"
    SnapshotStore(path).write(Snapshot(auctions=()))
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_missing_snapshot_returns_none(tmp_path):
    """还没采集过：库里没有 snapshot 行。"""
    assert SnapshotStore(tmp_path / "meruki.db").read() is None


def test_unknown_schema_version_raises(tmp_path):
    """库里的 schema_version 与代码不一致时明确报错，而不是读出半份数据。"""
    from meruki_tracker.core.adapters.store.db import connect

    path = tmp_path / "meruki.db"
    conn = connect(path)
    with conn:
        conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.close()

    with pytest.raises(ValueError):
        SnapshotStore(path).read()


def test_pending_roundtrip_keeps_no_time(tmp_path):
    store = SnapshotStore(tmp_path / "meruki.db")
    st = AuctionState(
        asin="P1",
        name="n",
        url="u",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.pending(),
    )
    store.write(Snapshot(auctions=(st,)))
    back = store.read()
    assert back is not None
    assert back.auctions[0].end_time.state is TimeState.PENDING
    assert back.auctions[0].end_time.at is None


# ---------------------------------------------------------------- 并发写


def test_concurrent_writes_never_collide(tmp_path):
    """8 线程并发写：事务 + busy_timeout 下不该丢错误，最终数据完整可读。"""
    import threading

    path = tmp_path / "meruki.db"
    store = SnapshotStore(path)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for _ in range(10):
                store.write(Snapshot(auctions=(make_state(f"A{i}"),)))
        except BaseException as exc:  # 想要看到具体异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    back = store.read()
    assert back is not None and len(back.auctions) == 1


def test_no_leftover_temp_files(tmp_path):
    """改用事务后不该再产生任何 *.tmp 残留。"""
    path = tmp_path / "meruki.db"
    store = SnapshotStore(path)
    for i in range(5):
        store.write(Snapshot(auctions=(make_state(f"A{i}"),)))
    leftovers = [p.name for p in tmp_path.iterdir() if "tmp" in p.name]
    assert leftovers == []


def test_write_failure_keeps_previous_snapshot(tmp_path, monkeypatch):
    """写新快照失败时事务回滚，旧快照必须完好（等价于旧的"失败不覆盖"）。"""
    import sqlite3

    path = tmp_path / "meruki.db"
    store = SnapshotStore(path)
    store.write(Snapshot(auctions=(make_state("OLD"),)))

    # 让批量插入失败，模拟写库出错（sqlite 连接属性只读，包一层可写对象）
    class _BoomConn:
        def __init__(self, real) -> None:
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def executemany(self, *_a, **_kw):
            raise sqlite3.OperationalError("disk I/O error")

        def __enter__(self):
            # 转发到真实连接，让事务真正开始
            return self._real.__enter__()

        def __exit__(self, *exc):
            # 转发异常，触发真实事务回滚
            return self._real.__exit__(*exc)

    store._conn = _BoomConn(store._conn)
    with pytest.raises(OSError):
        store.write(Snapshot(auctions=(make_state("NEW"),)))

    back = store.read()
    assert back is not None and back.auctions[0].asin == "OLD"


# ---------------------------------------------------------------- 凭证：库权限与校验


def test_db_file_is_0600(tmp_path):
    """数据库文件（含 WAL/SHM）只给本人读写。"""
    from meruki_tracker.core.adapters.store.credentials import CredentialStore

    path = tmp_path / "meruki.db"
    CredentialStore(path).save("a=1")
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    "evil",
    ["a=1\r\nX-Injected: pwned", "a=1\nX-Injected: pwned", "a=1\x00b"],
)
def test_credentials_save_rejects_crlf(tmp_path, evil):
    """含 CR/LF/NUL 的 Cookie 拒绝入库（HTTP 头注入）。"""
    from meruki_tracker.core.adapters.store.credentials import CredentialStore

    store = CredentialStore(tmp_path / "meruki.db")
    with pytest.raises(ValueError):
        store.save(evil)
    assert store.load() == "", "被拒绝的值不该出现在库里"


def test_credentials_roundtrip_and_clear(tmp_path):
    from meruki_tracker.core.adapters.store.credentials import CredentialStore

    store = CredentialStore(tmp_path / "meruki.db")
    assert store.load() == ""
    store.save("a=1; b=2")
    assert store.load() == "a=1; b=2"
    store.save("c=3")  # 覆盖：库里始终只有一份
    assert store.load() == "c=3"
    store.clear()
    assert store.load() == ""


def test_credentials_load_ignores_dirty_row(tmp_path):
    """库里若混进脏 Cookie（例如手工写库），读取时视为未登录而不是原样交出。"""
    from meruki_tracker.core.adapters.store.credentials import CredentialStore

    path = tmp_path / "meruki.db"
    store = CredentialStore(path)
    with store._conn:
        store._conn.execute(
            "INSERT INTO credentials (id, cookie, updated_at) VALUES (1, ?, 'x')",
            ("a=1\r\nX-Injected: pwned",),
        )
    assert store.load() == ""


# ---------------------------------------------------------------- sqlite


def test_observations_uses_wal_and_time_index(tmp_path):
    from meruki_tracker.core.adapters.store.observations import ObservationStore

    store = ObservationStore(tmp_path / "obs.sqlite")
    try:
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        names = {
            r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "ix_obs_time" in names
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM observations WHERE observed_at < ?", ("x",)
        ).fetchall()
        assert any("ix_obs_time" in str(row[-1]) for row in plan), plan
    finally:
        store.close()


# ---------------------------------------------------------------- refresher 完整性


def _mk_refresher(tmp_path, api, retention_days=90):
    from meruki_tracker.core.adapters.store.credentials import CredentialStore
    from meruki_tracker.core.adapters.store.observations import ObservationStore
    from meruki_tracker.core.app.refresher import Refresher, RefresherOptions

    creds = CredentialStore(tmp_path / "cookies")
    creds.save("a=1")
    obs = ObservationStore(tmp_path / "obs.sqlite")
    refresher = Refresher(
        api=api,
        snapshots=SnapshotStore(tmp_path / "meruki.db"),
        credentials=creds,
        observations=obs,
        options=RefresherOptions(
            detail_enabled=False,
            observations_retention_days=retention_days,
        ),
    )
    return refresher, obs


class _ListApi:
    def __init__(self, page_factory) -> None:
        self._factory = page_factory
        self.calls = 0

    async def fetch_list_page(self, page, *, status="进行中"):
        self.calls += 1
        return self._factory(page)

    async def fetch_my_bids(self, *, status="竞拍中", page=1):
        """这些用例只关心列表分页，假装我一条都没出过价。"""
        from meruki_tracker.core.adapters.meruki.api import MyBidPage

        return MyBidPage(items=(), total_item=0, current_page=1, total_page=1, status=status)


async def test_empty_list_does_not_end_anything(tmp_path):
    """接口正常返回空 ≠ 拍品结束了。

    2026-11-15 起结束只由详情判（详情里有 ``over``）。列表空只说明"这一轮没看到"，
    可能被取关、可能只是接口抽风——本地不做终态判定，拍品停在 ACTIVE 等详情回答。
    """
    from meruki_tracker.core.adapters.meruki.api import ListPage
    from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery

    old = AuctionState(
        asin="A1",
        name="n",
        url="u",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.pending(),
    )
    api = _ListApi(lambda p: ListPage(items=(), total_item=0, current_page=1, total_page=1))
    refresher, obs = _mk_refresher(tmp_path, api)
    try:
        snap, result = await refresher.refresh(Snapshot(auctions=(old,)))
    finally:
        obs.close()
    assert result.ok
    assert snap.auctions[0].door_state is DoorState.ACTIVE
    assert snap.auctions[0].user_query is UserQuery.FOLLOWING


async def test_missing_paging_field_fails_refresh_and_keeps_state(tmp_path):
    """异常空壳（缺字段）→ 协议异常 → 本轮失败，绝不误判取关。"""
    from meruki_tracker.core.adapters.meruki.api import _map_list_page
    from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery
    from meruki_tracker.core.errors import ProtocolChangedError

    old = AuctionState(
        asin="A1",
        name="n",
        url="u",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.pending(),
    )

    def factory(page):
        return _map_list_page({"Data": [], "TotalItem": 0}, page=page)  # 缺 TotalPage/CurrentPage

    refresher, obs = _mk_refresher(tmp_path, _ListApi(factory))
    try:
        snap, result = await refresher.refresh(Snapshot(auctions=(old,)))
    finally:
        obs.close()
    assert result.ok is False
    assert result.error_kind == "protocol"
    assert isinstance(ProtocolChangedError("x"), ProtocolChangedError)
    assert snap.auctions[0].user_query is UserQuery.FOLLOWING, "不完整列表不能作为取关证据"


async def test_empty_page_stops_pagination_even_if_total_page_lies(tmp_path):
    """total_page 声称有多页但第 1 页为空 → 视为到底，不再翻页。"""
    from meruki_tracker.core.adapters.meruki.api import ListPage

    api = _ListApi(lambda p: ListPage(items=(), total_item=30, current_page=p, total_page=3))
    refresher, obs = _mk_refresher(tmp_path, api)
    try:
        _snap, result = await refresher.refresh(Snapshot())
    finally:
        obs.close()
    assert result.ok
    assert api.calls == 1
    assert result.stats.listed == 0


async def test_retention_days_from_config_is_used(tmp_path):
    """配置里的保留天数必须真正生效（不再是硬编码 90 天）。"""
    from datetime import timedelta

    from meruki_tracker.core.adapters.meruki.api import ListPage

    api = _ListApi(lambda p: ListPage(items=(), total_item=0, current_page=1, total_page=1))
    refresher, obs = _mk_refresher(tmp_path, api, retention_days=1)
    try:
        old_ts = datetime.now(UTC) - timedelta(days=5)
        obs.add(asin="A1", observed_at=old_ts, kind="list")
        obs.add(asin="A2", observed_at=datetime.now(UTC), kind="list")
        obs.commit()
        _snap, result = await refresher.refresh(Snapshot())
        assert result.ok
        assert result.error_kind == ""
        remaining = {r[0] for r in obs._conn.execute("SELECT asin FROM observations")}
        assert remaining == {"A2"}, "保留 1 天应删掉 5 天前的观测"
    finally:
        obs.close()


async def test_prune_is_throttled_to_once_a_day(tmp_path, monkeypatch):
    """配 docstring：每天最多清理一次。"""
    import meruki_tracker.core.app.refresher as refresher_mod
    from meruki_tracker.core.adapters.meruki.api import ListPage

    calls: list[int] = []
    real = refresher_mod._prune_observations

    def spy(store, *, retention):
        calls.append(1)
        real(store, retention=retention)

    monkeypatch.setattr(refresher_mod, "_prune_observations", spy)
    api = _ListApi(lambda p: ListPage(items=(), total_item=0, current_page=1, total_page=1))
    refresher, obs = _mk_refresher(tmp_path, api)
    try:
        await refresher.refresh(Snapshot())
        await refresher.refresh(Snapshot())
        await refresher.refresh(Snapshot())
    finally:
        obs.close()
    assert len(calls) == 1


async def test_context_close_closes_sqlite_even_if_api_aclose_fails(tmp_path):
    """api.aclose() 抛异常也必须关掉 sqlite（否则 WAL 文件残留、句柄泄漏）。"""
    from dataclasses import replace

    from meruki_tracker.core.app.context import build_context
    from meruki_tracker.core.config import Config, StorageConfig

    cfg = replace(Config.defaults(), storage=StorageConfig(data_dir=str(tmp_path / "data")))
    ctx = build_context(cfg)

    async def boom():
        raise RuntimeError("transport close failed")

    ctx.api.aclose = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await ctx.close()
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError):
        ctx.observations._conn.execute("SELECT 1")  # 连接已关闭


# ---------------------------------------------------------------- 空快照保护


def test_empty_snapshot_refuses_to_wipe_existing_data(tmp_path):
    """快照是整份替换：误传空快照会把所有拍品抹掉，必须拒绝。"""
    from meruki_tracker.core.adapters.store.snapshot import EmptySnapshotError

    store = SnapshotStore(tmp_path / "meruki.db")
    store.write(Snapshot(auctions=(make_state("A1"), make_state("A2"))))

    with pytest.raises(EmptySnapshotError):
        store.write(Snapshot(auctions=()))

    back = store.read()
    assert back is not None
    assert len(back.auctions) == 2, "被拒绝后原数据必须完好"


def test_empty_snapshot_allowed_when_explicit(tmp_path):
    """确实要清空时（用户取关全部）显式传 allow_empty=True 即可。"""
    store = SnapshotStore(tmp_path / "meruki.db")
    store.write(Snapshot(auctions=(make_state("A1"),)))

    store.write(Snapshot(auctions=()), allow_empty=True)

    back = store.read()
    assert back is not None and back.auctions == ()


def test_empty_snapshot_on_fresh_db_is_fine(tmp_path):
    """库里本来就没数据时，写空快照不该报错（首次采集前）。"""
    store = SnapshotStore(tmp_path / "meruki.db")
    store.write(Snapshot(auctions=()))
    assert store.read() is not None


# ---------------------------------------------------------------- 坏行容错
#
# 一条读不懂的行不该让整份快照消失。真实踩过：手工改库时漏填 end_at，而
# time_state 还写着 exact → ``EndTime.exact(None, ...)`` 抛 ValueError →
# ``load_snapshot`` 把它当成"版本不支持"整份丢弃 → 下一次刷新把 23 条
# 「已结束」写没（它们不在进行中列表里，永远不会被重新发现）。


def _seed_row(path, **overrides):
    """直接往 auctions 表塞一行，绕过 AuctionState 的校验。"""
    from meruki_tracker.core.adapters.store.db import connect

    conn = connect(path)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshot (id, generated_at, attempt_at, attempt_ok,"
            " attempt_err, account_name, nickname, cookie_state)"
            " VALUES (1, ?, ?, 1, '', 'me', '', 'ok')",
            ("2026-10-29T12:00:00Z", "2026-10-29T12:00:00Z"),
        )
        cols = {
            "asin": "BAD1",
            "name": "坏行",
            "url": "u",
            "user_query": "following",
            "door_state": "ended",
            "time_state": "exact",
            "end_at": "2026-10-29T12:00:00Z",
            "confirmed_at": "2026-10-29T12:00:00Z",
            "latest_bid": None,
            "buy_now_price": None,
            "detail_failures": 0,
            "last_seen_at": None,
            "unfollowed_at": None,
            "concluded_at": "2026-10-29T12:00:00Z",
        }
        cols.update(overrides)
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO auctions ({names}) VALUES ({marks})", tuple(cols.values()))
    conn.close()
    return path


def test_exact_row_without_end_at_is_downgraded_not_fatal(tmp_path):
    """exact 但没有 end_at：降级成「时间待确认」，其余行照读。"""
    path = tmp_path / "meruki.db"
    SnapshotStore(path).write(
        Snapshot(
            generated_at=datetime.now(UTC),
            last_attempt=LastAttempt(at=datetime.now(UTC), ok=True),
            auctions=(make_state("OK1"),),
        )
    )
    _seed_row(path, end_at=None)  # write 会 DELETE 全表，坏行要在写完之后塞

    snap = SnapshotStore(path).read()
    assert snap is not None
    got = {a.asin: a for a in snap.auctions}
    assert set(got) == {"OK1", "BAD1"}
    assert got["BAD1"].end_time.state is TimeState.PENDING
    assert got["BAD1"].end_time.at is None
    assert got["BAD1"].door_state is DoorState.ENDED  # 其余字段照原样保留


def test_unparseable_end_at_is_downgraded_not_fatal(tmp_path):
    """end_at 是人手写进去的破字符串：同样降级，不能连累别的行。"""
    path = tmp_path / "meruki.db"
    SnapshotStore(path).write(
        Snapshot(
            generated_at=datetime.now(UTC),
            last_attempt=LastAttempt(at=datetime.now(UTC), ok=True),
            auctions=(make_state("OK1"),),
        )
    )
    _seed_row(path, end_at="2026年10月29日 12:00")

    snap = SnapshotStore(path).read()
    assert snap is not None
    got = {a.asin: a for a in snap.auctions}
    assert got["BAD1"].end_time.state is TimeState.PENDING
    assert got["OK1"].end_time.state is TimeState.EXACT


def test_unknown_enum_value_does_not_wipe_the_snapshot(tmp_path):
    """枚举值读不懂（老版本写入/手改）：回落到安全值，别丢掉整行。"""
    path = tmp_path / "meruki.db"
    SnapshotStore(path).write(
        Snapshot(
            generated_at=datetime.now(UTC),
            last_attempt=LastAttempt(at=datetime.now(UTC), ok=True),
            auctions=(make_state("OK1"),),
        )
    )
    _seed_row(path, door_state="exploded", user_query="ghost")

    snap = SnapshotStore(path).read()
    assert snap is not None
    got = {a.asin: a for a in snap.auctions}
    assert got["BAD1"].door_state is DoorState.ACTIVE
    assert got["BAD1"].user_query is UserQuery.FOLLOWING


def test_version_mismatch_is_reported_as_its_own_error(tmp_path):
    """版本不符仍是显式异常（不同于坏行），让上层提示得准确。"""
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.adapters.store.snapshot import SchemaVersionError

    path = tmp_path / "meruki.db"
    conn = connect(path)
    with conn:
        conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.close()

    with pytest.raises(SchemaVersionError):
        SnapshotStore(path).read()


def test_naive_timestamp_from_a_hand_edited_row_is_read_as_utc(tmp_path) -> None:
    """库里手写的时间没有时区时，必须按 UTC 补全，不能把 naive 往下游传。

    2026-11-18 审计：``_parse_iso`` 用裸 ``fromisoformat``，``end_at='2026-11-20
    10:00:00'`` 这种手改值会得到 naive datetime，一路进 ``EndTime``；倒计时那行
    ``end - now`` 随即抛 ``TypeError: can't subtract offset-naive and
    offset-aware datetimes``——一条脏数据换一次整屏渲染失败。
    """
    path = tmp_path / "meruki.db"
    SnapshotStore(path).write(
        Snapshot(
            generated_at=datetime.now(UTC),
            last_attempt=LastAttempt(at=datetime.now(UTC), ok=True),
            auctions=(make_state("OK1"),),
        )
    )
    _seed_row(path, end_at="2026-11-20 10:00:00")  # write 会 DELETE 全表，坏行要后塞

    snap = SnapshotStore(path).read()
    assert snap is not None
    got = {a.asin: a for a in snap.auctions}
    at = got["BAD1"].end_time.at
    assert at is not None
    assert at.tzinfo is not None
    assert at == datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    # 同一条行里没问题的拍品照读
    assert got["OK1"].end_time.state is TimeState.EXACT


def test_write_refuses_when_the_database_is_from_a_newer_version(tmp_path) -> None:
    """库是更晚版本建的：读拒绝之后，**写也必须拒绝**。

    2026-11-18 审计：``read()`` 有版本校验、``load_snapshot`` 接住后返回空快照，
    但 ``write()`` 没有校验也无条件整份替换。于是"从新版本退回旧版本"时第一次
    刷新就会用空快照（``allow_empty=True``）把 ``auctions`` 整表清掉——「已结束」
    那些行不在「进行中」列表里，删了再也补不回来。
    """
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.adapters.store.snapshot import SchemaVersionError

    path = tmp_path / "meruki.db"
    store = SnapshotStore(path)
    store.write(Snapshot(auctions=(make_state("A1"),)))
    conn = connect(path)
    with conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.close()

    with pytest.raises(SchemaVersionError):
        store.write(Snapshot(auctions=()), allow_empty=True)

    # 数据一条都不能少（这条就是「不许静默清库」）
    conn = connect(path)
    rows = conn.execute("SELECT asin FROM auctions").fetchall()
    conn.close()
    assert rows == [("A1",)]


def test_read_never_sees_a_half_written_snapshot(tmp_path) -> None:
    """``read()`` 必须等写事务结束，不能读到"删完还没写"的中间态。

    2026-11-18 审计：``read()`` 不拿写锁，同一条连接上与 ``write()`` 并发时，
    2054 次读里有 357 次读到 0 条或半份列表（甚至 ``snapshot`` 头行已删、
    ``auctions`` 还没写的瞬间）。``db.py`` 明确承诺"读者要么看到旧快照、
    要么看到新快照，不会看到半份"。

    这里手工造出那个中断点：删掉旧行、暂停、再写新行。读者若不等锁，就会
    读到"删完还没写"的空表。
    """
    import threading

    path = tmp_path / "meruki.db"
    store = SnapshotStore(path)
    store.write(Snapshot(auctions=(make_state("A1"),)))

    inside = threading.Event()
    release = threading.Event()

    def half_written_transaction() -> None:
        with store._write_lock, store._conn:
            store._conn.execute("DELETE FROM auctions")
            inside.set()
            release.wait(2.0)
            row = store._conn.execute("SELECT * FROM (SELECT ? , ?)", ("A2", "新拍品")).fetchone()
            assert row is not None
            store._conn.execute(
                "INSERT INTO auctions (asin, name, url, user_query, door_state,"
                " time_state, end_at, confirmed_at, latest_bid, buy_now_price,"
                " detail_failures, last_seen_at, unfollowed_at, concluded_at)"
                " VALUES ('A2', '新拍品', 'u', 'following', 'active', 'pending',"
                " NULL, NULL, NULL, NULL, 0, NULL, NULL, NULL)"
            )

    writer = threading.Thread(target=half_written_transaction)
    writer.start()
    assert inside.wait(2.0)

    got: list[Snapshot | None] = []
    reader = threading.Thread(target=lambda: got.append(store.read()))
    reader.start()
    release.set()
    writer.join(2.0)
    reader.join(2.0)

    assert got and got[0] is not None
    assert [a.asin for a in got[0].auctions] == ["A2"]


def test_mirror_prune_drops_only_the_rolling_tables(tmp_path):
    """镜像清理只删流水（分页元信息 / 变更事件），不碰当前状态。

    ``list_pages`` 每轮每个分桶写一行、``changes`` 有多少变动写多少行，两者
    都是只增不减的流水（真实库 ``changes`` 曾涨到 13 万行）。``list_items`` /
    ``detail_items`` 存的是当前状态，**不能**按时间删。
    """
    from datetime import timedelta

    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.adapters.store.mirror import MirrorStore
    from meruki_tracker.core.app.refresher import _prune_mirror

    db = tmp_path / "meruki.db"
    conn = connect(db)
    mirror = MirrorStore(db, conn=conn)
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    with conn:
        for stamp in (old, fresh):
            conn.execute(
                "INSERT INTO list_pages (status, page, fetched_at) VALUES ('进行中', 1, ?)",
                (stamp,),
            )
            conn.execute(
                "INSERT INTO changes (asin, kind, detected_at) VALUES ('A1', 'appeared', ?)",
                (stamp,),
            )
        conn.execute(
            "INSERT INTO list_items (status, asin, seen_at) VALUES ('进行中', 'A1', ?)", (old,)
        )

    _prune_mirror(mirror, retention=timedelta(days=90))

    assert mirror.count("list_pages") == 1
    assert mirror.count("changes") == 1
    assert mirror.count("list_items") == 1, "当前状态不能按时间删"
