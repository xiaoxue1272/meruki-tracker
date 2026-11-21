"""状态快照：落在统一数据库 meruki.db。

不变式（原先是"json 整份原子替换"保证，现在由事务保证）：
- 一致性：``snapshot`` 与 ``auctions`` 在**同一个事务**里替换，提交前崩溃则回滚，
  读者只会看到完整的旧快照或完整的新快照，不会看到半份
- 失败不覆盖：写入失败时事务回滚，旧数据完好
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ...domain.models import (
    AuctionState,
    DoorState,
    EndTime,
    TimeState,
    UserQuery,
)
from .db import SCHEMA_VERSION
from .db import connect as db_connect

log = logging.getLogger(__name__)


class EmptySnapshotError(RuntimeError):
    """用空快照覆盖非空数据（保护性拒绝）。"""


class SchemaVersionError(ValueError):
    """库里的 schema_version 与代码不一致（整份数据都不可信，必须显式报错）。"""


@dataclass(frozen=True, slots=True)
class LastAttempt:
    at: datetime | None = None
    ok: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class Account:
    label: str = "me"
    nickname: str = ""
    cookie_state: str = "unknown"


@dataclass(frozen=True, slots=True)
class Snapshot:
    generated_at: datetime | None = None
    last_attempt: LastAttempt = field(default_factory=LastAttempt)
    account: Account = field(default_factory=Account)
    auctions: tuple[AuctionState, ...] = ()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _parse_iso(value: str | None) -> datetime | None:
    """解析库里的 ISO 时间；读不懂返回 None（由调用方决定怎么降级）。

    不抛异常是有意的：这一列是人手可改的（改数据修 bug 时真会手写），一个
    破字符串不该让整份快照消失。

    **不带时区的时间按 UTC 补全**。库里写的一律是 UTC，但人手写进去的
    （``UPDATE auctions SET end_at='2026-11-20 10:00:00'``）没有偏移量，
    ``fromisoformat`` 会给一个 naive datetime；naive 一旦进了 ``EndTime``，
    倒计时那行 ``end - now`` 会抛 ``TypeError: can't subtract offset-naive
    and offset-aware``，整屏渲染直接失败——一条手改的数据换一次白屏。
    """
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        log.warning("库里的时间读不懂，按缺失处理: %r", value)
        return None
    if at.tzinfo is None:
        log.warning("库里的时间没有时区，按 UTC 处理: %r", value)
        return at.replace(tzinfo=UTC)
    return at


def _parse_enum(value, enum, *, default, column: str, asin: str):
    """把库里的字符串还原成枚举；值读不懂时回落到 ``default`` 并留日志。

    回落的都是"安全侧"取值：未知关注状态当仍在关注、未知门状态当仍在进行、
    未知时间状态当待确认——宁可多轮询一轮，也不能丢掉整行。
    """
    try:
        return enum(value)
    except ValueError:
        log.warning("%s 的值读不懂（%r），按 %s 处理: %s", column, value, default.value, asin)
        return default


class SnapshotStore:
    """快照读写（落在统一数据库里）。

    接口保持 ``read()`` / ``write()`` 不变，但持久化从 ``state.json`` 换成
    ``meruki.db`` 的两张表（``snapshot`` + ``auctions``）。

    **写用事务**：旧实现靠"整份原子替换 json"保证失败不覆盖；改成库之后，
    在同一个事务里"删旧行 + 写新行 + 提交"，提交前崩溃会整体回滚，语义等价。
    """

    def __init__(self, path: Path | None = None, *, conn: sqlite3.Connection | None = None) -> None:
        # path 参数保留是为了兼容既有调用方（传的是 data 目录下的库路径）
        self._conn = conn if conn is not None else db_connect(path or Path("meruki.db"))
        self._owns_conn = conn is None
        # 同一连接被多线程使用时，写事务必须串行，否则会互相踩事务边界
        self._write_lock = threading.Lock()

    def read(self) -> Snapshot | None:
        """读取快照；还不存在返回 None。

        **也要拿写锁**：``write()`` 是"删旧行 + 写新行"，事务提交前同一条连接
        上的读者能看到中间态（实测 2054 次并发读里 357 次读到 0 条或半份列表，
        甚至读到 ``snapshot`` 头行已删、``auctions`` 还没写的瞬间）。现在所有
        数据库调用都在同一个事件循环线程里，所以还没被用户看到；但 ``db.py``
        明确承诺"读者要么看到旧快照、要么看到新快照，不会看到半份"，而且一旦
        有人把刷新挪进 ``to_thread``，界面就会偶发显示"没有任何拍品"。读极短，
        串行化的代价可以忽略。

        Raises:
            ValueError: 数据库里的 schema 版本不受支持。
        """
        with self._write_lock:
            return self._read_locked()

    def _read_locked(self) -> Snapshot | None:
        """真正的读实现；调用方必须已持有 ``_write_lock``。"""
        version = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if version is not None and str(version[0]) != str(SCHEMA_VERSION):
            raise SchemaVersionError(f"不支持的快照版本: {version[0]}（期望 {SCHEMA_VERSION}）")

        head = self._conn.execute(
            "SELECT generated_at, attempt_at, attempt_ok, attempt_err,"
            " account_name, nickname, cookie_state FROM snapshot WHERE id = 1"
        ).fetchone()
        if head is None:
            return None

        rows = self._conn.execute(
            "SELECT asin, name, url, user_query, door_state, time_state, end_at,"
            " confirmed_at, latest_bid, buy_now_price, detail_failures, last_seen_at,"
            " unfollowed_at, concluded_at FROM auctions ORDER BY asin"
        ).fetchall()
        return Snapshot(
            generated_at=_parse_iso(head[0]),
            last_attempt=LastAttempt(
                at=_parse_iso(head[1]),
                ok=bool(head[2]),
                error=str(head[3] or ""),
            ),
            account=Account(
                label=str(head[4] or "me"),
                nickname=str(head[5] or ""),
                cookie_state=str(head[6] or "unknown"),
            ),
            auctions=tuple(_auction_from_row(r) for r in rows),
        )

    def write(self, snap: Snapshot, *, allow_empty: bool = False) -> None:
        """事务内整份替换快照。

        Args:
            snap: 要写入的快照。
            allow_empty: 允许把已有数据覆盖成空列表。

        Raises:
            OSError: 写入失败（sqlite 错误会被包成本异常，调用方按磁盘问题处理）。
            SchemaVersionError: 库是**更晚的版本**建的，拒绝写入。
            EmptySnapshotError: 用空快照覆盖非空数据，且没有显式允许。

        为什么要这道闸：快照是**整份替换**语义，一次误传空快照就会把库里
        所有拍品抹掉，而且没有任何提示。真需要清空（比如用户把关注全部取消）
        时由调用方显式传 ``allow_empty=True`` 表态，避免"手滑清库"。
        """
        # 锁在 __init__ 里建好了。以前这里有个 getattr 兜底，看起来"更安全"，
        # 实际每次调用都会**新建一把锁**——两把不同的锁互不排斥，等于没锁。
        lock = self._write_lock
        if not snap.auctions and not allow_empty:
            existing = self._conn.execute("SELECT COUNT(*) FROM auctions").fetchone()[0]
            if existing:
                raise EmptySnapshotError(
                    f"拒绝用空快照覆盖已有的 {existing} 个拍品；"
                    "如果确实要清空，请显式传 allow_empty=True"
                )
        try:
            with lock, self._conn:  # 串行 + 事务：异常自动回滚，旧快照不受影响
                # 版本闸门（必须在锁内、事务内查）：读那边（read()）遇到不认识的
                # 版本会抛 SchemaVersionError，上层 load_snapshot 接住后返回空快照
                # ——**写这边不拦的话，紧接着第一次刷新就会用一个"空快照 +
                # allow_empty=True"把整张 auctions 表清掉**，而「已结束」那些行
                # 不在「进行中」列表里，删了就再也补不回来。真实场景：从新版本
                # 退回旧版本、两个版本共用一个 data 目录、手改过库。
                version = self._conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if version is not None and str(version[0]) != str(SCHEMA_VERSION):
                    raise SchemaVersionError(
                        f"不支持的快照版本: {version[0]}（期望 {SCHEMA_VERSION}），"
                        "拒绝写入以免覆盖数据"
                    )
                self._conn.execute("DELETE FROM auctions")
                self._conn.execute("DELETE FROM snapshot")
                self._conn.execute(
                    "INSERT INTO snapshot"
                    " (id, generated_at, attempt_at, attempt_ok, attempt_err,"
                    "  account_name, nickname, cookie_state)"
                    " VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _iso(snap.generated_at),
                        _iso(snap.last_attempt.at),
                        1 if snap.last_attempt.ok else 0,
                        snap.last_attempt.error or "",
                        snap.account.label if snap.account else "me",
                        snap.account.nickname if snap.account else "",
                        snap.account.cookie_state if snap.account else "unknown",
                    ),
                )
                self._conn.executemany(
                    "INSERT INTO auctions (asin, name, url, user_query, door_state,"
                    " time_state, end_at, confirmed_at, latest_bid, buy_now_price,"
                    " detail_failures, last_seen_at, unfollowed_at, concluded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            a.asin,
                            a.name,
                            a.url,
                            a.user_query.value,
                            a.door_state.value,
                            a.end_time.state.value,
                            _iso(a.end_time.at),
                            _iso(a.end_time.confirmed_at),
                            a.latest_bid,
                            a.buy_now_price,
                            a.detail_failures,
                            _iso(a.last_seen_at),
                            _iso(a.unfollowed_at),
                            _iso(a.concluded_at),
                        )
                        for a in snap.auctions
                    ],
                )
        except sqlite3.Error as exc:
            raise OSError(f"写快照失败: {exc}") from exc

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()


def _auction_from_row(row: tuple) -> AuctionState:
    (
        asin,
        name,
        url,
        user_query,
        door_state,
        time_state,
        end_at,
        confirmed_at,
        latest_bid,
        buy_now_price,
        detail_failures,
        last_seen_at,
        unfollowed_at,
        concluded_at,
    ) = row
    state = _parse_enum(
        time_state, TimeState, default=TimeState.PENDING, column="time_state", asin=asin
    )
    at = _parse_iso(end_at)
    if state is TimeState.EXACT and at is None:
        # exact 却没有时间：这一行自相矛盾。降级成"待确认"保住其余字段——
        # 若让它抛错，load_snapshot 会把整份快照当"版本不支持"丢掉，之后
        # 一次刷新就把不在进行中列表里的「已结束」永久写没（真实踩过）。
        log.warning("time_state=exact 但没有可用的 end_at，降级为待确认: %s", asin)
        state = TimeState.PENDING
    if state is TimeState.PENDING:
        end_time = EndTime.pending()
    else:
        # 旧 json 实现里确认时间缺失时用 now() 兜底，这里保持一致
        end_time = EndTime.exact(at, _parse_iso(confirmed_at) or at or datetime.now(UTC))
    return AuctionState(
        asin=asin,
        name=name or "",
        url=url or "",
        user_query=_parse_enum(
            user_query, UserQuery, default=UserQuery.FOLLOWING, column="user_query", asin=asin
        ),
        door_state=_parse_enum(
            door_state, DoorState, default=DoorState.ACTIVE, column="door_state", asin=asin
        ),
        end_time=end_time,
        latest_bid=latest_bid,
        buy_now_price=buy_now_price,
        detail_failures=int(detail_failures or 0),
        last_seen_at=_parse_iso(last_seen_at),
        unfollowed_at=_parse_iso(unfollowed_at),
        concluded_at=_parse_iso(concluded_at),
    )
