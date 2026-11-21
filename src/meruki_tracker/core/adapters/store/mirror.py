"""接口镜像存储：把接口返回的**每个字段**原样落库。

**为什么要有这一层**

以前本地只存"我们理解的字段"（asin / name / url / 结束时间 / 价格），接口
多给的 ``BidDeadlineStr`` / ``CreatedTime`` / ``ImageUrl`` / ``UserId`` /
``ItemPerPage`` … 解析完就扔了。后果是：出了问题只能翻日志，回答不了"服务端
当时到底回了什么"，也没法在服务端加字段后回补历史。

现在接口给什么就存什么，外加 ``raw_json`` 兜住未来新增的字段——服务端加字段
不必改表也能留证。``auctions`` 表退化成**派生投影**，真相在这里。

**一致性**

一轮采集的所有写入都发生在刷新事务里（``Refresher.refresh`` 内），失败时
整轮不落库，看到的永远是完整的一轮。``list_items`` 按 ``(status, asin)``
主键 upsert，同一个拍品在两个分桶里都出现也不会互相覆盖。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import connect as db_connect


@dataclass(frozen=True, slots=True)
class StoredListItem:
    """``list_items`` 表的一行（接口列表条目的完整镜像）。"""

    status: str
    asin: str
    item_id: str
    name: str
    url: str
    detail_url: str
    bid_deadline: str
    bid_deadline_str: str
    created_time: str
    image_url: str
    user_id: str
    seen_at: str
    page: int = 1


@dataclass(frozen=True, slots=True)
class StoredDetail:
    """``detail_items`` 表的一行（接口详情条目的完整镜像）。"""

    asin: str
    name: str
    end_at: str | None
    ended: bool | None
    bid_price: float | None
    buy_now_price: float | None
    auto_extend: bool | None
    early_termination: bool | None
    observed_at: str


class MirrorStore:
    """接口镜像读写。与其他 store 共用同一个 sqlite 连接。"""

    def __init__(self, path: Path | None = None, *, conn: sqlite3.Connection | None = None) -> None:
        self.path = path or Path("meruki.db")
        self._conn = conn if conn is not None else db_connect(self.path)
        self._owns_conn = conn is None

    # ---------------------------------------------------------------- 写入

    def save_round(
        self,
        *,
        status: str,
        pages: Sequence[Mapping[str, Any]],
        items: Sequence[Mapping[str, Any]],
        details: Sequence[Mapping[str, Any]] = (),
        changes: Sequence[Mapping[str, Any]] = (),
        seen_at: datetime,
        replace: bool = True,
    ) -> int:
        """一轮采集的全部镜像写入，**一个事务**。

        为什么不让每个方法各自提交：一轮采集是"一次观测"，半个观测比没有观测
        更糟——分页写了一半、条目没写，事后比对会得出完全错误的结论。这里要么
        全落库、要么全不落（``with self._conn`` 失败自动回滚）。

        Args:
            status: 本轮列表的 status 分桶。
            pages: 每页的分页元信息（``page`` / ``item_per_page`` / …）。
            items: 列表条目（含 ``raw``）。
            details: 详情条目。
            changes: 比对产出的变更事件。
            seen_at: 本轮采集时刻（所有行共用）。
            replace: 是否整桶替换。**本轮列表不完整时必须为 False**——残缺的
                列表不能当作"其他拍品都不见了"的证据。

        Returns:
            写入的条目数（不含分页元信息与事件），便于日志与断言。
        """
        stamp = seen_at.isoformat()
        with self._conn:
            for pg in pages:
                self._conn.execute(
                    "INSERT INTO list_pages (status, page, item_per_page, show_page_num,"
                    " current_page, total_item, total_page, returned, fetched_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(pg.get("status") or status),
                        int(pg.get("page") or 1),
                        int(pg.get("item_per_page") or 0),
                        int(pg.get("show_page_num") or 0),
                        int(pg.get("current_page") or 0),
                        int(pg.get("total_item") or 0),
                        int(pg.get("total_page") or 0),
                        int(pg.get("returned") or 0),
                        stamp,
                    ),
                )
            if replace:
                self._conn.execute("DELETE FROM list_items WHERE status = ?", (status,))
            payload = [
                (
                    status,
                    str(it["asin"]),
                    str(it.get("item_id") or ""),
                    str(it.get("name") or ""),
                    str(it.get("url") or ""),
                    str(it.get("detail_url") or ""),
                    str(it.get("bid_deadline") or ""),
                    str(it.get("bid_deadline_str") or ""),
                    str(it.get("created_time") or ""),
                    str(it.get("image_url") or ""),
                    str(it.get("user_id") or ""),
                    json.dumps(it.get("raw") or {}, ensure_ascii=False),
                    stamp,
                    int(it.get("page") or 1),
                )
                for it in items
            ]
            if payload:
                self._conn.executemany(
                    "INSERT INTO list_items (status, asin, item_id, name, url, detail_url,"
                    " bid_deadline, bid_deadline_str, created_time, image_url, user_id,"
                    " raw_json, seen_at, page)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(status, asin) DO UPDATE SET"
                    "   item_id=excluded.item_id, name=excluded.name, url=excluded.url,"
                    "   detail_url=excluded.detail_url, bid_deadline=excluded.bid_deadline,"
                    "   bid_deadline_str=excluded.bid_deadline_str,"
                    "   created_time=excluded.created_time, image_url=excluded.image_url,"
                    "   user_id=excluded.user_id, raw_json=excluded.raw_json,"
                    "   seen_at=excluded.seen_at, page=excluded.page",
                    payload,
                )
            for d in details:
                self._upsert_detail(d, stamp)
            if changes:
                self._conn.executemany(
                    "INSERT INTO changes (asin, kind, field, old_value, new_value, detected_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            str(c["asin"]),
                            str(c["kind"]),
                            str(c.get("field") or ""),
                            str(c.get("old_value") or ""),
                            str(c.get("new_value") or ""),
                            stamp,
                        )
                        for c in changes
                    ],
                )
        return len(payload)

    # ---------------------------------------------------------------- 读取

    def _upsert_detail(self, d: Mapping[str, Any], stamp: str) -> None:
        """写一条详情（``save_round`` 与 ``save_details`` 共用）。"""
        self._conn.execute(
            "INSERT INTO detail_items (asin, name, end_at, ended, bid_price,"
            " buy_now_price, auto_extend, early_termination, raw_json, observed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(asin) DO UPDATE SET"
            "   name=excluded.name, end_at=excluded.end_at, ended=excluded.ended,"
            "   bid_price=excluded.bid_price, buy_now_price=excluded.buy_now_price,"
            "   auto_extend=excluded.auto_extend,"
            "   early_termination=excluded.early_termination,"
            "   raw_json=excluded.raw_json, observed_at=excluded.observed_at",
            (
                str(d["asin"]),
                str(d.get("name") or ""),
                d.get("end_at"),
                _opt_bool(d.get("ended")),
                d.get("bid_price"),
                d.get("buy_now_price"),
                _opt_bool(d.get("auto_extend")),
                _opt_bool(d.get("early_termination")),
                json.dumps(d.get("raw") or {}, ensure_ascii=False),
                stamp,
            ),
        )

    def save_details(self, details: Sequence[Mapping[str, Any]], *, seen_at: datetime) -> int:
        """只写详情镜像（不动列表、不动分页）。

        为什么要单独开一个：详情与列表分桶无关——同一个拍品在「进行中」或
        「已结束」桶里都可能有详情。用 ``save_round`` 硬塞一个 ``status`` 参数
        是误导。给手动查看详情（``refresh_detail_of``）用。
        """
        if not details:
            return 0
        stamp = seen_at.isoformat()
        with self._conn:
            for d in details:
                self._upsert_detail(d, stamp)
        return len(details)

    def list_items(self, status: str | None = None) -> list[StoredListItem]:
        """读列表镜像；``status`` 为 None 时返回全部分桶。"""
        sql = (
            "SELECT status, asin, item_id, name, url, detail_url, bid_deadline,"
            " bid_deadline_str, created_time, image_url, user_id, seen_at, page"
            " FROM list_items"
        )
        args: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY status, item_id DESC, asin"
        return [
            StoredListItem(
                status=r[0],
                asin=r[1],
                item_id=r[2],
                name=r[3],
                url=r[4],
                detail_url=r[5],
                bid_deadline=r[6],
                bid_deadline_str=r[7],
                created_time=r[8],
                image_url=r[9],
                user_id=r[10],
                seen_at=r[11],
                page=r[12],
            )
            for r in self._conn.execute(sql, args).fetchall()
        ]

    def detail_urls(self, status: str | None = None) -> dict[str, str]:
        """``asin -> detail_url`` 映射。

        ``Refresher`` 靠 ``detail_url`` 才能拉详情（也就是拿到价格）。它平时从
        列表响应里攒一份内存字典，但**重启之后就空了**——而「已结束」的 500+
        条历史不会每轮都重拉，价格补齐必须能从库里拿到地址。
        """
        sql = "SELECT asin, detail_url FROM list_items WHERE detail_url != ''"
        args: tuple[Any, ...] = ()
        if status is not None:
            sql += " AND status = ?"
            args = (status,)
        return {r[0]: r[1] for r in self._conn.execute(sql, args).fetchall()}

    def detail_items(self) -> dict[str, StoredDetail]:
        rows = self._conn.execute(
            "SELECT asin, name, end_at, ended, bid_price, buy_now_price, auto_extend,"
            " early_termination, observed_at FROM detail_items"
        ).fetchall()
        out: dict[str, StoredDetail] = {}
        for r in rows:
            out[r[0]] = StoredDetail(
                asin=r[0],
                name=r[1],
                end_at=r[2],
                ended=None if r[3] is None else bool(r[3]),
                bid_price=r[4],
                buy_now_price=r[5],
                auto_extend=None if r[6] is None else bool(r[6]),
                early_termination=None if r[7] is None else bool(r[7]),
                observed_at=r[8],
            )
        return out

    def detail_item(self, asin: str) -> StoredDetail | None:
        """读某个拍品的详情镜像（``None`` = 没拉过详情）。

        ``detail_items()`` 会把整表读出来：详情屏每进一次都要这两三个字段，
        没必要为了一个 asin 扫全表（关注数上百时全表读会明显变慢）。
        """
        r = self._conn.execute(
            "SELECT asin, name, end_at, ended, bid_price, buy_now_price, auto_extend,"
            " early_termination, observed_at FROM detail_items WHERE asin = ?",
            (asin,),
        ).fetchone()
        if r is None:
            return None
        return StoredDetail(
            asin=r[0],
            name=r[1],
            end_at=r[2],
            ended=None if r[3] is None else bool(r[3]),
            bid_price=r[4],
            buy_now_price=r[5],
            auto_extend=None if r[6] is None else bool(r[6]),
            early_termination=None if r[7] is None else bool(r[7]),
            observed_at=r[8],
        )

    def detail_raw(self, asin: str) -> dict[str, Any]:
        """读某个拍品的详情原始 JSON（``{}`` 表示没拉过详情）。

        描述、图片这些"只有详情里才有"的内容不在 ``auctions`` 表里，而是跟着
        原始 JSON 存在 ``detail_items.raw_json``——服务端加字段不用改表。
        详情屏要显示它们，所以得能按 asin 取回来。
        """
        row = self._conn.execute(
            "SELECT raw_json FROM detail_items WHERE asin = ?", (asin,)
        ).fetchone()
        if not row or not row[0]:
            return {}
        try:
            parsed = json.loads(row[0])
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def recent_changes(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT asin, kind, field, old_value, new_value, detected_at FROM changes"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "asin": r[0],
                "kind": r[1],
                "field": r[2],
                "old": r[3],
                "new": r[4],
                "at": r[5],
            }
            for r in rows
        ]

    def prune(self, before: datetime) -> dict[str, int]:
        """删掉 ``before`` 之前的**流水**（分页元信息 + 变更事件）。

        ``list_items`` / ``detail_items`` 存的是"当前状态"，不能按时间删；但
        ``list_pages`` 每轮每个分桶各写一行、``changes`` 每轮有多少变动写多少行，
        两者都是只增不减的流水。真实库里 ``changes`` 曾经涨到 13 万行（9 MB，
        因为一个已修的 bug 每轮刷 548 条假事件），``list_pages`` 则是每天约 200
        行——都不致命，但没有上限的表迟早要出问题，跟着观测的保留期一起清最省事。

        Args:
            before: 这个时刻之前的行会被删掉。

        Returns:
            每张表删掉的行数（只含非零项）。
        """
        stamp = before.isoformat()
        removed: dict[str, int] = {}
        with self._conn:
            for table, column in (("list_pages", "fetched_at"), ("changes", "detected_at")):
                cur = self._conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (stamp,))
                if cur.rowcount and cur.rowcount > 0:
                    removed[table] = int(cur.rowcount)
        return removed

    def count(self, table: str) -> int:
        """诊断用：某张镜像表的行数。表名白名单校验，避免拼接注入。"""
        if table not in ("list_items", "list_pages", "detail_items", "changes"):
            raise ValueError(f"不支持的镜像表: {table}")
        row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()


def _opt_bool(value: Any) -> int | None:
    """三态布尔：``None`` 保持 None（未知），否则存 0/1。"""
    if value is None:
        return None
    return int(bool(value))
