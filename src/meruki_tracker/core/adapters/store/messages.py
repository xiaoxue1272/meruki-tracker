"""出价事件存储：站内消息里的「出价被超过」落库 + 水位线。

**水位线（watermark）为什么必要**

``GetMessageWeb`` 每次返回最新 10 条，不认"你已经看过了"。如果不记水位线，
每轮刷新都会把同一批消息重新解析一遍、重新拉订单详情——那才是最费请求的做法
（一条消息一个订单详情请求）。

水位线取**已处理消息里最大的 ``msg_id``**，存进 ``meta`` 表。消息 id 是服务端
自增的，单调递增，所以 ``> 水位线`` 就等价于"新的"。第一次跑没有水位线时，
只把当前最大 id 记下来当起点，**不回溯历史**——否则首次启动会一口气拉几百条
陈年消息、几百个订单详情，直接被风控盯上。

**为什么用 ``INSERT OR IGNORE`` + 唯一索引**

同一条消息可能因为水位线推进失败（比如崩溃在写水位线之前）被重复处理一次。
``OR IGNORE`` + 唯一索引保证同一个事件只落库一行：重复解析是无副作用的，
重复通知才是问题（这个交给 ``notified`` 列和上面的唯一索引兜）。

唯一索引建在 ``(msg_id, event, happened_at)`` 上，**不是** ``msg_id``：一条
「出价被超过」消息会把订单的整条 log 一起落库（价格不足 → 出价成功 → 出价
被超过 → 竞拍失败），这些事件共用同一个 ``msg_id``。只按 ``msg_id`` 去重会
只留下第一条，恰好丢掉最该看的「出价被超过」——实测踩过这个坑。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .db import connect as db_connect

#: 水位线在 ``meta`` 表里的键
WATERMARK_KEY = "messages_watermark"


@dataclass(frozen=True, slots=True)
class StoredBidEvent:
    """``bid_events`` 表的一行。"""

    msg_id: int
    order_id: str
    asin: str
    event: str
    happened_at: str
    category: str
    title: str
    created_at: str
    notified: bool
    seen_at: str

    @property
    def is_outbid(self) -> bool:
        return self.event == "出价被超过"


@dataclass(frozen=True, slots=True)
class MessageRow:
    """待落库的一条出价事件（由刷新层从消息 + 订单详情组装）。"""

    msg_id: int
    order_id: str = ""
    asin: str = ""
    event: str = ""
    happened_at: str = ""
    category: str = ""
    title: str = ""
    created_at: str = ""


class MessageStore:
    """出价事件读写。与其他 store 共用同一个 sqlite 连接。"""

    def __init__(self, path: Path | None = None, *, conn: sqlite3.Connection | None = None) -> None:
        self.path = path or Path("meruki.db")
        self._conn = conn if conn is not None else db_connect(self.path)
        self._owns_conn = conn is None

    # ---------------------------------------------------------------- 水位线

    def watermark(self) -> int:
        """已处理到哪一条消息（返回 ``msg_id``，没见过消息时为 0）。"""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (WATERMARK_KEY,)
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(str(row[0]))
        except ValueError:
            return 0

    def set_watermark(self, msg_id: int) -> None:
        """推进水位线。**只增不减**——回退会让旧消息被重复处理。

        单独调用时自己提交（这是给"只推水位线、不写事件"的场景用的，例如首次
        接入时记个起点）。要与事件写入绑定在同一个事务里，请用
        :meth:`save_round`。
        """
        with self._conn:
            self._bump(msg_id)

    def _bump(self, msg_id: int) -> None:
        """推进水位线的裸操作（不提交，供事务内调用）。"""
        current = self.watermark()
        if msg_id <= current:
            return
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (WATERMARK_KEY, str(msg_id)),
        )

    # ---------------------------------------------------------------- 写入

    def save_round(self, rows: list[MessageRow], *, seen_at: datetime, watermark: int = 0) -> int:
        """一轮消息处理的全部写入，**一个事务**：事件 + 水位线。

        为什么要绑在一起：如果先写事件再推水位线、中间崩溃，那几条消息会被
        重复处理一次（无害，唯一索引挡掉）；但如果**先推水位线再写事件**、
        中间崩溃，那几条消息就**永久丢了**——水位线已经越过它们了。所以两个
        写操作必须在同一个事务里，要么都成、要么都不成。

        同一 ``msg_id`` 重复写入会被唯一索引挡掉（``INSERT OR IGNORE``），
        所以重放同一批是安全的。

        Args:
            rows: 待写事件。
            seen_at: 本轮采集时刻。
            watermark: 本轮处理完要推到哪（0 = 只有首次接入时的那次推进生效）。

        Returns:
            真正新增的事件行数（已存在的不计）。
        """
        stamp = seen_at.isoformat()
        added = 0
        with self._conn:
            for row in rows:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO bid_events"
                    " (msg_id, order_id, asin, event, happened_at, category, title,"
                    "  created_at, notified, seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (
                        row.msg_id,
                        row.order_id,
                        row.asin,
                        row.event,
                        row.happened_at,
                        row.category,
                        row.title,
                        row.created_at,
                        stamp,
                    ),
                )
                added += cur.rowcount
            if watermark > 0:
                self._bump(watermark)
        return added

    def save_events(self, rows: list[MessageRow], *, seen_at: datetime) -> int:
        """只落库事件、不动水位线（测试与诊断用）。"""
        return self.save_round(rows, seen_at=seen_at)

    # ---------------------------------------------------------------- 读取

    def events(self, *, asin: str | None = None, limit: int = 100) -> list[StoredBidEvent]:
        """读事件，按时间从旧到新。

        排序键是 ``(msg_id, id)`` 而不是只有 ``msg_id``：一条消息会带出订单的
        整条 log（多条事件共用一个 ``msg_id``），只按 ``msg_id`` 排的话组内顺序
        未定义——实测读出来是倒的。``id`` 是自增主键，等价于写入顺序，而写入
        顺序就是 ``parse_bid_events`` 排好的时间顺序。
        """
        sql = (
            "SELECT msg_id, order_id, asin, event, happened_at, category, title,"
            " created_at, notified, seen_at FROM bid_events"
        )
        args: tuple[object, ...] = ()
        if asin is not None:
            sql += " WHERE asin = ?"
            args = (asin,)
        sql += " ORDER BY msg_id DESC, id DESC LIMIT ?"
        args += (limit,)
        rows = self._conn.execute(sql, args).fetchall()
        out = [
            StoredBidEvent(
                msg_id=r[0],
                order_id=r[1],
                asin=r[2],
                event=r[3],
                happened_at=r[4],
                category=r[5],
                title=r[6],
                created_at=r[7],
                notified=bool(r[8]),
                seen_at=r[9],
            )
            for r in rows
        ]
        out.reverse()  # 从旧到新
        return out

    def latest_outbid(self, asin: str) -> StoredBidEvent | None:
        """某个拍品最近一次「出价被超过」。没有则返回 None。"""
        row = self._conn.execute(
            "SELECT msg_id, order_id, asin, event, happened_at, category, title,"
            " created_at, notified, seen_at FROM bid_events"
            " WHERE asin = ? AND event = '出价被超过'"
            " ORDER BY msg_id DESC, id DESC LIMIT 1",
            (asin,),
        ).fetchone()
        if row is None:
            return None
        return StoredBidEvent(
            msg_id=row[0],
            order_id=row[1],
            asin=row[2],
            event=row[3],
            happened_at=row[4],
            category=row[5],
            title=row[6],
            created_at=row[7],
            notified=bool(row[8]),
            seen_at=row[9],
        )

    def count(self) -> int:
        """诊断用：事件总行数。"""
        row = self._conn.execute("SELECT COUNT(*) FROM bid_events").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()
