"""C4 第四刀：``refresh`` 只抽两个 helper，主干 ``try/except/finally`` 不动。

- ``_log_refresh_done``：把成功日志的拼装从主干挪出来；
- ``_refresh_failure``：把两个 ``except`` 分支共用的收尾集中到一处。

这两个函数都是**纯搬运**：``LastAttempt.error`` 与 ``RefreshResult.error`` 的
有意不对称、日志文案、``_write_failed_snapshot`` 的落盘调用必须逐字保持。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from meruki_tracker.core.adapters.meruki.api import ListItem
from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.db import connect
from meruki_tracker.core.adapters.store.observations import ObservationStore
from meruki_tracker.core.adapters.store.snapshot import Snapshot, SnapshotStore
from meruki_tracker.core.app import refresher as refresher_mod
from meruki_tracker.core.app.refresher import DetailStats, Refresher, RefresherOptions
from meruki_tracker.core.domain.diff import (
    KIND_APPEARED,
    KIND_DISAPPEARED,
    KIND_FIELD_CHANGED,
    Change,
)
from meruki_tracker.core.domain.models import Transition, TransitionKind
from meruki_tracker.core.errors import (
    AuthExpiredError,
    NetworkError,
    ProtocolChangedError,
    WafBlockedError,
)

NOW = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)


class _NoBidsApi:
    """只实现 ``refresh`` 会用到的那一个方法。

    这些用例测的是日志拼装与异常分支，列表/详情全被 monkeypatch 掉了；
    但"我的竞拍"是 refresh 里真实会调的一步（判断出价被超过），得给个替身。
    """

    async def fetch_my_bids(self, *, status: str = "竞拍中", page: int = 1):
        from meruki_tracker.core.adapters.meruki.api import MyBidPage

        return MyBidPage(items=(), total_item=0, current_page=1, total_page=1, status=status)


def _build(tmp_path: Path, snapshots: Any) -> Refresher:
    db = tmp_path / "meruki.db"
    conn = connect(db)
    creds = CredentialStore(db, conn=conn)
    creds.save("sid=test")
    return Refresher(
        api=_NoBidsApi(),
        snapshots=snapshots,
        credentials=creds,
        observations=ObservationStore(db, conn=conn),
        options=RefresherOptions(detail_enabled=False),
    )


class _RecordingStore:
    """记录 write() 调用的替身；可选地在写盘时抛 OSError。"""

    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[Snapshot] = []
        self._explode = explode

    def write(self, snap: Snapshot) -> None:
        self.calls.append(snap)
        if self._explode:
            raise OSError(28, "No space left on device")


# ------------------------------------------------------------------ _refresh_failure


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (AuthExpiredError("登录过期"), "auth"),
        (WafBlockedError("被拦"), "waf"),
        (ProtocolChangedError("结构变了"), "protocol"),
        (NetworkError("网络不通"), "network"),
    ],
)
def test_failure_classifies_each_domain_error(tmp_path: Path, exc, kind: str) -> None:
    """四个 MerukiTrackerError 子类仍按 _classify 分成四种 error_kind。"""
    store = _RecordingStore()
    refresher = _build(tmp_path, store)

    snap, result = refresher._refresh_failure(Snapshot(), NOW, exc)

    assert result.ok is False
    assert result.error_kind == kind
    assert result.error == str(exc)
    assert snap.last_attempt.ok is False
    assert snap.last_attempt.at == NOW
    assert snap.last_attempt.error == str(exc)  # 领域异常：不带类型前缀
    assert store.calls == [snap]  # 失败态快照照旧落盘


def test_failure_internal_error_has_type_prefix_only_in_snapshot(tmp_path: Path) -> None:
    """未预期异常：LastAttempt.error 带类型前缀，RefreshResult.error 不带。"""
    store = _RecordingStore()
    refresher = _build(tmp_path, store)

    snap, result = refresher._refresh_failure(Snapshot(), NOW, RuntimeError("boom"))

    assert result.error_kind == "internal"
    assert result.error == "boom"  # 不带前缀
    assert snap.last_attempt.error == "RuntimeError: boom"  # 落盘带前缀
    assert store.calls == [snap]


def test_failure_write_error_does_not_mask_original(tmp_path: Path) -> None:
    """写盘失败不能顶替原始错误（这个 helper 必须继续用 _write_failed_snapshot）。"""
    store = _RecordingStore(explode=True)
    refresher = _build(tmp_path, store)

    _snap, result = refresher._refresh_failure(Snapshot(), NOW, NetworkError("网络不通"))

    assert result.error_kind == "network"
    assert "网络不通" in result.error
    assert "space" not in result.error.lower()
    assert len(store.calls) == 1  # 确实尝试过落盘


def test_failure_keeps_previous_auctions(tmp_path: Path) -> None:
    """失败态快照保留上一轮拍品，只改 last_attempt。"""
    from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery

    st = AuctionState(
        asin="A1",
        name="拍品",
        url="u",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(NOW + timedelta(days=1), NOW),
    )
    previous = Snapshot(auctions=(st,))
    refresher = _build(tmp_path, _RecordingStore())

    snap, _ = refresher._refresh_failure(previous, NOW, NetworkError("x"))

    assert snap.auctions == (st,)
    assert snap.last_attempt.ok is False


# ------------------------------------------------------------------ _log_refresh_done


def _list_result(n: int) -> Any:
    """造一个 _ListResult。用真实 Observation/ListItem，wiring 用例会跑真的 advance/diff。"""
    from meruki_tracker.core.domain.models import Observation

    items = [
        Observation(asin=f"A{i}", name=f"拍品{i}", url=f"u{i}", latest_bid=None, observed_at=NOW)
        for i in range(n)
    ]
    raw_items = [ListItem(asin=f"A{i}", name=f"拍品{i}", raw={"Asin": f"A{i}"}) for i in range(n)]
    return refresher_mod._ListResult(
        items=items,
        complete=True,
        detail_urls={},
        raw_items=raw_items,
        pages=[],
    )


def test_log_is_empty_changes_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """空变更列表 → 「无变化」；断言 log.info 的格式串与 5 个实参逐字不变。"""
    calls: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(refresher_mod.log, "info", lambda fmt, *args: calls.append((fmt, args)))
    refresher = _build(tmp_path, _RecordingStore())
    stats = DetailStats(ok=3, failed=1)
    transitions = [Transition(TransitionKind.DISCOVERED, "A")] * 2

    refresher._log_refresh_done(_list_result(4), [], stats, transitions)

    fmt, args = calls[0]
    assert fmt == "刷新完成：列表 %d 条（%s），详情 成功%d/失败%d，迁移 %d 条"
    assert args == (4, "无变化", 3, 1, 2)


def test_log_summarizes_appeared_disappeared_and_field_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """summarize(changes) 的人话摘要确实进了日志，不是写死的「无变化」。"""
    calls: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(refresher_mod.log, "info", lambda fmt, *args: calls.append((fmt, args)))
    refresher = _build(tmp_path, _RecordingStore())
    changes = [
        Change(asin="A1", kind=KIND_APPEARED),
        Change(asin="A2", kind=KIND_APPEARED),
        Change(asin="A3", kind=KIND_DISAPPEARED),
        Change(asin="A4", kind=KIND_FIELD_CHANGED, field="name", old="旧", new="新"),
    ]

    refresher._log_refresh_done(_list_result(5), changes, DetailStats(ok=0, failed=0), [])

    assert calls[0][1] == (5, "2 个新出现、1 个消失、1 处字段变化", 0, 0, 0)


def test_refresh_wires_success_log_to_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """主干真的把成功日志交给 helper（不是把 helper 写成了孤儿）。

    直接补掉主干里几乎所有子步骤，只留成功日志与 return；若哪天主干改回
    inline log.info，这条会红。
    """
    calls: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(refresher_mod.log, "info", lambda fmt, *args: calls.append((fmt, args)))
    db = tmp_path / "meruki.db"
    conn = connect(db)
    creds = CredentialStore(db, conn=conn)
    creds.save("sid=test")
    snapshots = SnapshotStore(db, conn=conn)
    refresher = _build(tmp_path, snapshots)

    async def _noop_fetch_outbid():
        return set(), [], 0

    async def _noop_fetch_list(progress):
        return _list_result(2)

    monkeypatch.setattr(refresher, "_fetch_outbid", _noop_fetch_outbid)
    monkeypatch.setattr(refresher, "_fetch_list", _noop_fetch_list)
    monkeypatch.setattr(refresher, "_mirror_items", lambda: {})
    monkeypatch.setattr(refresher, "_mirror_details", lambda: {})
    monkeypatch.setattr(refresher, "_write_mirror", lambda *a, **kw: None)
    monkeypatch.setattr(refresher, "_notify_outbid", lambda rows, now=None: None)
    monkeypatch.setattr(refresher, "_maybe_prune", lambda now: None)
    monkeypatch.setattr(
        refresher,
        "_log_refresh_done",
        lambda listed, changes, stats, transitions: (
            calls.append(("called", ())),
            Refresher._log_refresh_done(refresher, listed, changes, stats, transitions),
        ),
    )

    import asyncio

    snap, result = asyncio.run(refresher.refresh(Snapshot()))
    assert result.ok is True
    assert ("called", ()) in calls  # 主干确实调用了 helper
    assert any(fmt.startswith("刷新完成：") for fmt, _ in calls)
    assert snap.last_attempt.ok is True


# ------------------------------------------------------- 凭证读取失败不是"没登录"


def test_credential_read_failure_is_reported_as_storage(tmp_path: Path) -> None:
    """读凭证抛 OSError 时，不能报成 auth（会把用户指去重新登录）。

    2026-11-18 审计：``CredentialStore.load()`` 以前把所有 ``sqlite3.Error`` 吞成
    空串，于是库表坏了也显示「还没有登录」。现在它抛 OSError，刷新这边必须把
    它翻成 storage，并且**不能**去碰网络（一次请求都不该发）。
    """
    import asyncio

    from meruki_tracker.core.adapters.store.credentials import CredentialStore

    class _BrokenCredentials(CredentialStore):
        def load(self) -> str:  # type: ignore[override]
            raise OSError("读取登录凭证失败: no such table: credentials")

    db = tmp_path / "meruki.db"
    conn = connect(db)
    refresher = Refresher(
        api=_NoBidsApi(),
        snapshots=SnapshotStore(db, conn=conn),
        credentials=_BrokenCredentials(db, conn=conn),
        observations=ObservationStore(db, conn=conn),
        options=RefresherOptions(detail_enabled=False),
    )

    _, result = asyncio.run(refresher.refresh(Snapshot()))

    assert result.ok is False
    assert result.error_kind == "storage"
    assert "读取登录凭证失败" in result.error
