"""观测历史（统一数据库 meruki.db 的 observations 表，纯追加）。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import connect as db_connect


class ObservationStore:
    """观测库。每次采集的原始值都落库，便于事后追溯与重算。"""

    def __init__(self, path: Path | None = None, *, conn: sqlite3.Connection | None = None) -> None:
        # path 保留兼容：调用方传的是统一库路径
        self.path = path or Path("meruki.db")
        self._conn = conn if conn is not None else db_connect(self.path)
        self._owns_conn = conn is None

    def add(
        self,
        *,
        asin: str,
        observed_at: datetime,
        kind: str,
        end_time_raw: str = "",
        latest_bid: float | None = None,
    ) -> None:
        """追加一条观测，**立刻提交**。

        以前这里只 ``execute`` 不提交，靠刷新末尾那一次 ``commit()`` 兜底。但
        刷新中途还会调 ``snapshots.write`` / ``_write_mirror`` / ``messages.save_round``，
        它们各自用 ``with self._conn:``——其中任何一个在事务里抛错，``sqlite3``
        会**回滚整条共享连接**，把本轮已经"写成功"的观测一起扔掉（实测：加两条、
        让镜像落库抛错 → 库里剩 0 条，而刷新仍报成功，日志只说"镜像落库失败
        （不影响刷新结果）"）。观测是纯追加的历史，没有"要么全有要么全无"的要求，
        逐条提交最稳。
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO observations (asin, observed_at, kind, end_time_raw, latest_bid)"
                " VALUES (?, ?, ?, ?, ?)",
                (asin, observed_at.isoformat(), kind, end_time_raw, latest_bid),
            )

    def add_many(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """批量插入观测。

        用 ``executemany`` 单次往返：一次刷新可能有几十上百条观测，逐条 ``add``
        会带来同等的 Python↔sqlite 往返开销。显式开启事务，避免每条都隐式提交。
        """
        payload = [
            (
                r["asin"],
                r["observed_at"].isoformat(),
                r["kind"],
                r.get("end_time_raw", ""),
                r.get("latest_bid"),
            )
            for r in rows
        ]
        if not payload:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT INTO observations (asin, observed_at, kind, end_time_raw, latest_bid)"
                " VALUES (?, ?, ?, ?, ?)",
                payload,
            )

    def commit(self) -> None:
        self._conn.commit()

    def recent(self, asin: str, limit: int = 5) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT observed_at, kind, end_time_raw FROM observations"
            " WHERE asin = ? ORDER BY id DESC LIMIT ?",
            (asin, limit),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def prune(self, before: datetime) -> int:
        cur = self._conn.execute(
            "DELETE FROM observations WHERE observed_at < ?", (before.isoformat(),)
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()
