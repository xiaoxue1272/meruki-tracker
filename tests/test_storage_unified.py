"""统一数据库：三个 store 共用一个 meruki.db、各占各的表。

（旧布局 state.json / cookies / observations.sqlite 的搬迁代码已经删掉：
只服务一次性的历史升级，而升级早就完成了。留着只会让每个新读者都去追一条
走不通的路。）
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.db import connect as db_connect
from meruki_tracker.core.adapters.store.observations import ObservationStore
from meruki_tracker.core.adapters.store.snapshot import Account, Snapshot, SnapshotStore
from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery


def _state(asin: str = "A1") -> AuctionState:
    return AuctionState(
        asin=asin,
        name="测试拍品",
        url="https://x/A1",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(datetime(2026, 11, 1, tzinfo=UTC), datetime.now(UTC)),
        latest_bid=100.0,
    )


# ------------------------------------------------------------------ 共用同一个库


def test_all_stores_share_one_file(tmp_path: Path):
    """凭证 / 快照 / 观测都在 meruki.db，不再各占一个文件。"""
    path = tmp_path / "meruki.db"
    conn = db_connect(path)
    try:
        creds = CredentialStore(path, conn=conn)
        snaps = SnapshotStore(path, conn=conn)
        obs = ObservationStore(path, conn=conn)

        creds.save("a=1")
        snaps.write(Snapshot(account=Account(nickname="我"), auctions=(_state(),)))
        obs.add(asin="A1", observed_at=datetime.now(UTC), kind="list")

        assert creds.load() == "a=1"
        assert snaps.read().auctions[0].asin == "A1"
        assert len(obs.recent("A1")) == 1
    finally:
        conn.close()

    # data 目录下只应有一个库文件（外加 WAL/SHM）
    names = {p.name for p in tmp_path.iterdir()}
    assert names <= {"meruki.db", "meruki.db-wal", "meruki.db-shm"}, names


def test_tables_exist(tmp_path: Path):
    conn = db_connect(tmp_path / "meruki.db")
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"meta", "credentials", "snapshot", "auctions", "observations"} <= names


def test_credentials_single_row(tmp_path: Path):
    """表上 CHECK(id=1)：库里永远只有一份凭证。"""
    path = tmp_path / "meruki.db"
    store = CredentialStore(path)
    store.save("a=1")
    store.save("b=2")
    assert store.load() == "b=2"
    n = store._conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
    assert n == 1


def test_snapshot_header_single_row(tmp_path: Path):
    path = tmp_path / "meruki.db"
    store = SnapshotStore(path)
    store.write(Snapshot(auctions=(_state("A1"),)))
    store.write(Snapshot(auctions=(_state("A2"),)))
    assert store.read().auctions[0].asin == "A2"
    assert store._conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 1


def test_observation_survives_a_sibling_store_transaction_failure(tmp_path: Path):
    """本轮观测不能被别的 store 的失败事务一起回滚掉。

    2026-11-18 审计：``ObservationStore.add()`` 只 ``execute`` 不提交，靠刷新
    末尾那一次 ``commit()`` 兜底。而刷新中途 ``_write_mirror`` / ``save_round``
    都用 ``with self._conn:``，其中任何一个抛错都会回滚**整条共享连接**，把本轮
    观测全扔掉——刷新仍报成功，日志只说"镜像落库失败（不影响刷新结果）"。
    """
    db = tmp_path / "meruki.db"
    conn = db_connect(db)
    obs = ObservationStore(db, conn=conn)
    now = datetime.now(UTC)
    obs.add(asin="A1", observed_at=now, kind="detail")
    obs.add(asin="A2", observed_at=now, kind="detail")

    try:  # 模拟镜像落库在事务里抛错
        with conn:
            conn.execute(
                "INSERT INTO observations (asin, observed_at, kind) VALUES ('X', 'x', 'detail')"
            )
            raise ValueError("镜像落库失败")
    except ValueError:
        pass

    rows = conn.execute("SELECT asin FROM observations ORDER BY asin").fetchall()
    conn.close()
    assert rows == [("A1",), ("A2",)]


def test_credential_read_failure_is_not_reported_as_no_login(tmp_path: Path):
    """读凭证的**故障**不能伪装成「还没有登录」。

    2026-11-18 审计：``CredentialStore.load()`` 把所有 ``sqlite3.Error`` 都吞成
    空串，于是库表损坏 / 磁盘满 / 连接已关全被报成「还没有登录」，界面指引用户
    重新登录——而登录本身也会失败，用户就卡在"让我登录 → 登录不了 → 还是说
    没登录"里，真正的原因一个字都看不到。
    """
    import pytest

    path = tmp_path / "meruki.db"
    store = CredentialStore(path)
    store.save("a=1")
    store._conn.execute("DROP TABLE credentials")
    store._conn.commit()

    with pytest.raises(OSError, match="读取登录凭证失败"):
        store.load()


def test_closing_one_store_does_not_kill_the_shared_connection(tmp_path: Path):
    """store 的 ``close()`` 只能关自己开的连接。

    真隐患：所有 store 共用 ``build_context`` 里那一条连接，但
    ``TranslationStore.close()`` 是唯一一个无条件 ``self._conn.close()`` 的
    （其余六个都判 ``self._owns_conn``）。谁以后顺手调一次它，整个进程里
    所有 store 立刻全废——而且是"上一行还好好、下一行就报 closed database"
    那种最难查的形态。
    """
    db = tmp_path / "meruki.db"
    conn = db_connect(db)
    try:
        from meruki_tracker.core.adapters.store.translations import TranslationStore

        credentials = CredentialStore(db, conn=conn)
        translations = TranslationStore(db, conn=conn)
        credentials.save("sid=test")

        translations.close()

        # 共用连接还活着：兄弟 store 照常读写
        assert credentials.load() == "sid=test"
    finally:
        conn.close()
