"""译文缓存（标题 + 描述）。

为什么落库而不是只放内存：翻译是**网络请求**，一次一条，还要带节流。列表里
十几条拍品，每次启动都重翻一遍就是十几倍的请求量，而标题偶尔才改、描述基本
不动。缓存之后一个拍品只翻一次，退出重进也不用再翻。

存 ``source`` / ``target`` 是为了将来换语言时能识别出"这条是旧语言的译文"，
而不是把它当成有效译文直接显示。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import connect as db_connect


@dataclass(frozen=True, slots=True)
class Translation:
    """一条译文。``title`` / ``desc`` 任一为空都表示那一侧没翻出来。"""

    asin: str
    title: str
    desc: str
    source: str
    target: str
    updated_at: str


class TranslationStore:
    """``translations`` 表的读写（与其它 store 共用一个连接）。"""

    def __init__(self, path: Path | None = None, *, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or db_connect(path)
        #: 连接是不是自己开的。只有自己开的才能关——``build_context`` 里七个
        #: store 共用同一条连接，随便关一次就是"整个进程的 store 全废"。
        self._owns_conn = conn is None

    def get(self, asin: str) -> Translation | None:
        row = self._conn.execute(
            "SELECT asin, title, desc, source, target, updated_at FROM translations WHERE asin = ?",
            (asin,),
        ).fetchone()
        return None if row is None else _row(row)

    def all(self) -> dict[str, Translation]:
        rows = self._conn.execute(
            "SELECT asin, title, desc, source, target, updated_at FROM translations"
        ).fetchall()
        return {r[0]: _row(r) for r in rows}

    def save(
        self,
        asin: str,
        *,
        title: str = "",
        desc: str = "",
        source: str = "ja",
        target: str = "zh-CN",
        updated_at: datetime | None = None,
    ) -> None:
        """写入/覆盖一条译文。

        两侧都空**不写**：那说明接口什么也没翻出来，存一行空壳会让界面以为
        "已翻译"，下次也不再重试。
        """
        if not title and not desc:
            return
        stamp = (updated_at or datetime.now(UTC)).isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT INTO translations(asin, title, desc, source, target, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(asin) DO UPDATE SET"
                " title = excluded.title, desc = excluded.desc,"
                " source = excluded.source, target = excluded.target,"
                " updated_at = excluded.updated_at",
                (asin, title, desc, source, target, stamp),
            )

    def delete(self, asin: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM translations WHERE asin = ?", (asin,))

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM translations").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """只关自己开的连接。

        其余六个 store（credentials / observations / mirror / snapshot /
        messages）都判 ``_owns_conn``，这里是唯一漏掉的一个：调一次就把共用
        连接关了，之后兄弟 store 全部报 ``Cannot operate on a closed database``。
        """
        if self._owns_conn:
            self._conn.close()


def _row(row: tuple[Any, ...]) -> Translation:
    return Translation(
        asin=row[0],
        title=row[1] or "",
        desc=row[2] or "",
        source=row[3] or "ja",
        target=row[4] or "zh-CN",
        updated_at=row[5] or "",
    )
