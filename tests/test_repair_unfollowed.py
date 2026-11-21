"""一次性数据修复：把被误标为「已取关」的已结束拍品改回来。

旧逻辑把"不在进行中列表里"一律算取关，而列表接口只拉进行中——拍卖一结束就
从列表消失，于是用户会看到"我没取关，它自己变成已取关了"。

修复的判据：**真正的取关只可能发生在拍卖还在进行中的时候**（只有进行中的拍卖
才会出现在列表里）。所以 ``unfollowed_at >= end_at`` 一定是旧逻辑误标的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meruki_tracker.core.adapters.store.db import connect as db_connect
from meruki_tracker.core.adapters.store.migrate import (
    drop_obsolete_settings,
    drop_unfollowed_state,
    repair_mislabeled_unfollowed,
)

# ------------------------------------------------------------------ 夹具


def _insert(
    conn,
    asin: str,
    *,
    user_query: str = "unfollowed",
    door_state: str = "active",
    end_at: str | None = "2026-10-30T13:25:10Z",
    unfollowed_at: str | None = "2026-10-30T15:20:24.153622Z",
    concluded_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO auctions
            (asin, name, url, user_query, door_state, time_state, end_at,
             confirmed_at, latest_bid, detail_failures, last_seen_at,
             unfollowed_at, concluded_at)
        VALUES (?, ?, 'https://x', ?, ?, 'exact', ?, NULL, NULL, 0, NULL, ?, ?)
        """,
        (asin, asin, user_query, door_state, end_at, unfollowed_at, concluded_at),
    )
    conn.commit()


def _row(conn, asin: str):
    return conn.execute(
        "SELECT user_query, door_state, unfollowed_at, concluded_at FROM auctions WHERE asin = ?",
        (asin,),
    ).fetchone()


# ------------------------------------------------------------------ 修复


def test_unfollowed_after_end_is_repaired_to_ended(tmp_path: Path):
    """核心场景：结束时已过、取关时间却晚于结束时间 → 那是被误标的。"""
    conn = db_connect(tmp_path / "m.db")
    try:
        _insert(conn, "A1")
        assert repair_mislabeled_unfollowed(conn) == 1
        # 注意：SQLite 里 'ended' 是原样字符串
        assert _row(conn, "A1")[:2] == ("following", "ended")
    finally:
        conn.close()


def test_concluded_at_is_backfilled_from_end_at(tmp_path: Path):
    """修好的记录要带结束时间，否则「已结束」视图里没有时间可显示。"""
    conn = db_connect(tmp_path / "m.db")
    try:
        _insert(conn, "A1")
        repair_mislabeled_unfollowed(conn)
        _, _, unfollowed_at, concluded_at = _row(conn, "A1")
        assert unfollowed_at is None, "误标的取关时间必须清掉"
        assert concluded_at == "2026-10-30T13:25:10Z"
    finally:
        conn.close()


def test_real_unfollow_before_end_is_kept(tmp_path: Path):
    """真取关：结束时间还没到就取关了 → 不能动。"""
    conn = db_connect(tmp_path / "m.db")
    try:
        _insert(conn, "A1", unfollowed_at="2026-10-29T10:00:00Z")
        assert repair_mislabeled_unfollowed(conn) == 0
        assert _row(conn, "A1")[:2] == ("unfollowed", "active")
    finally:
        conn.close()


def test_already_ended_is_left_alone(tmp_path: Path):
    """已经标成 ended 的不重复处理。"""
    conn = db_connect(tmp_path / "m.db")
    try:
        _insert(conn, "A1", door_state="ended", unfollowed_at=None, concluded_at="x")
        assert repair_mislabeled_unfollowed(conn) == 0
        assert _row(conn, "A1")[:2] == ("unfollowed", "ended")
    finally:
        conn.close()


def test_following_rows_untouched(tmp_path: Path):
    conn = db_connect(tmp_path / "m.db")
    try:
        _insert(conn, "A1", user_query="following", unfollowed_at=None)
        assert repair_mislabeled_unfollowed(conn) == 0
        assert _row(conn, "A1")[:2] == ("following", "active")
    finally:
        conn.close()


def test_row_without_times_is_untouched(tmp_path: Path):
    """缺时间戳的记录判不了，宁可不动——错误地"已结束"比"已取关"更糟。"""
    conn = db_connect(tmp_path / "m.db")
    try:
        _insert(conn, "A1", end_at=None)
        _insert(conn, "A2", unfollowed_at=None)
        assert repair_mislabeled_unfollowed(conn) == 0
        assert _row(conn, "A1")[:2] == ("unfollowed", "active")
        assert _row(conn, "A2")[:2] == ("unfollowed", "active")
    finally:
        conn.close()


def test_repair_is_idempotent(tmp_path: Path):
    """跑过一次就打标记，之后启动不再扫描（大库不重复全表更新）。"""
    conn = db_connect(tmp_path / "m.db")
    try:
        _insert(conn, "A1")
        assert repair_mislabeled_unfollowed(conn) == 1
        # 即使再插入符合条件的新行，第二次也不会跑
        _insert(conn, "A2")
        assert repair_mislabeled_unfollowed(conn) == 0
        assert _row(conn, "A2")[:2] == ("unfollowed", "active")
    finally:
        conn.close()


def test_repair_handles_many_rows_at_once(tmp_path: Path):
    conn = db_connect(tmp_path / "m.db")
    try:
        for i in range(5):
            _insert(conn, f"A{i}")
        assert repair_mislabeled_unfollowed(conn) == 5
        rows = conn.execute(
            "SELECT COUNT(*) FROM auctions WHERE user_query='following' AND door_state='ended'"
        ).fetchone()
        assert rows[0] == 5
    finally:
        conn.close()


# ---------------------------------------------------------------- 撤销「已取关」


def test_drop_unfollowed_moves_legacy_rows_to_ended(tmp_path):
    """老库里残留的「已取关」直接并入「已结束」。

    这个状态已经被取消：列表接口只给「进行中」，拍品消失时分不清"拍完了"还是
    "被取关"，两种情况结论一致。迁移必须幂等，且不能把确实还在跑的拍品改坏。
    """
    from datetime import UTC, datetime

    from meruki_tracker.core.adapters.store.db import connect

    conn = connect(tmp_path / "meruki.db")
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with conn:
        conn.execute(
            "INSERT INTO auctions(asin, name, url, user_query, door_state, time_state,"
            " unfollowed_at) VALUES ('OLD', 'n', 'u', 'unfollowed', 'active', 'pending', ?)",
            (now.isoformat().replace("+00:00", "Z"),),
        )
        conn.execute(
            "INSERT INTO auctions(asin, name, url, user_query, door_state, time_state)"
            " VALUES ('LIVE', 'n', 'u', 'following', 'active', 'pending')"
        )

    dropped = drop_unfollowed_state(conn)

    assert dropped == 1
    row = conn.execute(
        "SELECT user_query, door_state, unfollowed_at, concluded_at FROM auctions"
        " WHERE asin = 'OLD'"
    ).fetchone()
    assert row[0] == "following"
    assert row[1] == "ended"
    assert row[2] is None
    assert row[3] is not None, "结束时间要有个兜底，否则排序全落到最后"
    live = conn.execute(
        "SELECT user_query, door_state FROM auctions WHERE asin = 'LIVE'"
    ).fetchone()
    assert tuple(live) == ("following", "active"), "还在跑的拍品不能被动"
    assert drop_unfollowed_state(conn) == 0, "幂等：跑第二次什么都不做"
    conn.close()


# ---------------------------------------------- 退役配置项 / 清空已结束历史


def test_drop_obsolete_settings_removes_retired_keys(tmp_path: Path):
    """老库里残留的退役配置项必须删掉。

    配置存在库里，删掉代码里的字段之后那一行**还在**，下次启动
    ``Config.from_mapping`` 读到不认识的键就抛 ConfigError，程序直接起不来。
    """
    from meruki_tracker.core.adapters.store.migrate import drop_obsolete_settings
    from meruki_tracker.core.adapters.store.settings import SettingsStore

    db = tmp_path / "meruki.db"
    conn = db_connect(db)
    store = SettingsStore(conn)
    store.set("fetch.ended_sync_pages", 5)
    store.set("refresh.auto_seconds", 300)
    conn.commit()

    removed = drop_obsolete_settings(conn)

    assert removed == 1
    keys = set(store.load_all())
    assert "fetch.ended_sync_pages" not in keys
    assert "refresh.auto_seconds" in keys, "别的配置项不能被误删"


def test_drop_obsolete_settings_is_idempotent(tmp_path: Path):
    from meruki_tracker.core.adapters.store.migrate import drop_obsolete_settings

    conn = db_connect(tmp_path / "meruki.db")
    drop_obsolete_settings(conn)
    assert drop_obsolete_settings(conn) == 0


def test_drop_ended_history_clears_old_rows(tmp_path: Path):
    """旧机制灌进来的「已结束」历史要清掉，但**观测历史不能动**。

    观测是诊断用的时间线，删了就再也查不到某个拍品当时多少钱。
    """
    from meruki_tracker.core.adapters.store.migrate import drop_ended_history

    conn = db_connect(tmp_path / "meruki.db")
    with conn:
        conn.executemany(
            "INSERT INTO auctions(asin, name, url, user_query, door_state, time_state)"
            " VALUES (?, ?, '', 'following', ?, 'pending')",
            [
                ("H1", "旧的已结束", "ended"),
                ("H2", "旧的已结束二", "ended"),
                ("A1", "还在拍", "active"),
            ],
        )
        conn.execute(
            "INSERT INTO observations(asin, observed_at, kind) VALUES ('H1', '2026-01-01T00:00:00Z', 'detail')"
        )
    conn.commit()

    removed = drop_ended_history(conn)

    assert removed == 2
    left = {r[0] for r in conn.execute("SELECT asin FROM auctions")}
    assert left == {"A1"}, "只该删终态的，进行中不动"
    obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert obs == 1, "观测历史不能删"


def test_drop_ended_history_is_idempotent(tmp_path: Path):
    from meruki_tracker.core.adapters.store.migrate import drop_ended_history

    conn = db_connect(tmp_path / "meruki.db")
    drop_ended_history(conn)
    assert drop_ended_history(conn) == 0


def test_obsolete_cleanup_is_not_gated_by_a_one_shot_flag(tmp_path: Path) -> None:
    """第二次往退役名单里加键，老库上那条也必须被清掉。

    以前这一步用 ``meta.obsolete_settings_dropped`` 跳过重跑：第一次清完就
    永久跳过，后来名单里再加一个键，老库上那条被静默忽略 → 启动报
    ``ConfigError``。真机踩到（`detail.ended_price_per_cycle`）。
    """
    from meruki_tracker.core.adapters.store.settings import SettingsStore
    from meruki_tracker.core.app.context import load_config

    db = tmp_path / "meruki.db"
    conn = db_connect(db)
    # 先跑一次清理，制造"已经清理过"的库状态
    assert drop_obsolete_settings(conn) == 0
    conn.commit()
    # 再把这个键塞回去（模拟"名单后来才加上的那条"）
    SettingsStore(conn).set("detail.ended_price_per_cycle", 30)
    conn.commit()
    conn.close()

    load_config(tmp_path)  # 不能抛

    conn = db_connect(db)
    left = conn.execute(
        "SELECT COUNT(*) FROM settings WHERE key = 'detail.ended_price_per_cycle'"
    ).fetchone()[0]
    assert left == 0, "第二次启动也该把它清掉"


@pytest.mark.parametrize(
    "key,value",
    [
        ("fetch.ended_sync_pages", 5),
        # 它只服务于「批量补价格」那条路径，那个入口删了，字段也删了
        ("detail.ended_price_per_cycle", 30),
    ],
)
def test_obsolete_setting_in_db_no_longer_blocks_startup(tmp_path: Path, key: str, value: int):
    """端到端：带退役键的老库能正常启动（这是那个 ConfigError 的回归）。

    删一个配置字段只改代码是不够的：老库的 ``settings`` 表里那一行**还在**，
    ``Config.from_mapping`` 读到不认识的键就抛 ``ConfigError``，程序直接起不来。
    """
    from meruki_tracker.core.adapters.store.settings import SettingsStore
    from meruki_tracker.core.app.context import load_config

    db = tmp_path / "meruki.db"
    conn = db_connect(db)
    SettingsStore(conn).set(key, value)
    conn.commit()
    conn.close()

    cfg = load_config(tmp_path)  # 不能抛

    assert cfg.refresh.auto_seconds > 0


# ------------------------------------------- 拆分「当前价 / 一口价」两个字段


def test_split_price_fields_moves_a_pasted_buy_now_back(tmp_path: Path) -> None:
    """当年塞进 latest_bid 的一口价，要挪回 buy_now_price。

    历史：``bidPrice`` 为 0 时界面显示 ¥0，于是把 ``buyNowPrice`` 兜底写进了
    ``latest_bid``。后来用户要求分开显示（「你分开两个字段展示吧」），这个值
    得挪回去，否则「当前价」那一列会显示一个根本不存在的拍卖价。
    """
    from meruki_tracker.core.adapters.store.migrate import split_price_fields

    db = tmp_path / "m.db"
    conn = db_connect(db)
    with conn:
        conn.execute(
            "INSERT INTO auctions(asin, name, url, user_query, door_state, time_state,"
            " latest_bid) VALUES ('FIX1', 'n', 'u', 'following', 'active', 'exact', 1980)"
        )
        conn.execute(
            "INSERT INTO detail_items(asin, name, bid_price, buy_now_price, observed_at)"
            " VALUES ('FIX1', 'n', 0, 1980, '2026-11-04T00:00:00Z')"
        )

    assert split_price_fields(conn) == 1
    row = conn.execute(
        "SELECT latest_bid, buy_now_price FROM auctions WHERE asin='FIX1'"
    ).fetchone()
    assert row == (None, 1980.0)
    conn.close()


def test_split_price_fields_leaves_a_real_bid_alone(tmp_path: Path) -> None:
    """有真拍卖价的不能动——判据要求镜像表 bid_price = 0。"""
    from meruki_tracker.core.adapters.store.migrate import split_price_fields

    db = tmp_path / "m.db"
    conn = db_connect(db)
    with conn:
        conn.execute(
            "INSERT INTO auctions(asin, name, url, user_query, door_state, time_state,"
            " latest_bid) VALUES ('NORM', 'n', 'u', 'following', 'active', 'exact', 1300)"
        )
        conn.execute(
            "INSERT INTO detail_items(asin, name, bid_price, buy_now_price, observed_at)"
            " VALUES ('NORM', 'n', 1300, 3000, '2026-11-04T00:00:00Z')"
        )

    assert split_price_fields(conn) == 0
    row = conn.execute(
        "SELECT latest_bid, buy_now_price FROM auctions WHERE asin='NORM'"
    ).fetchone()
    # 当前价不动，一口价顺手补上（普通拍卖也可能有一口价）
    assert row == (1300.0, 3000.0)
    conn.close()


def test_split_price_fields_is_idempotent(tmp_path: Path) -> None:
    from meruki_tracker.core.adapters.store.migrate import split_price_fields

    db = tmp_path / "m.db"
    conn = db_connect(db)
    with conn:
        conn.execute(
            "INSERT INTO auctions(asin, name, url, user_query, door_state, time_state,"
            " latest_bid) VALUES ('FIX1', 'n', 'u', 'following', 'active', 'exact', 1980)"
        )
        conn.execute(
            "INSERT INTO detail_items(asin, name, bid_price, buy_now_price, observed_at)"
            " VALUES ('FIX1', 'n', 0, 1980, '2026-11-04T00:00:00Z')"
        )

    assert split_price_fields(conn) == 1
    assert split_price_fields(conn) == 0
    conn.close()
