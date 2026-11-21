"""Cookie 凭证读写（存进统一数据库 meruki.db 的 credentials 表）。

要点：

- **落盘前校验**：含 CR/LF/NUL 的 Cookie 会被拒绝（HTTP 头注入），脏数据
  既不写库也不被读出。
- **单行约束**：表上 ``CHECK (id = 1)``，写入即整行替换，天然只有一份凭证。
- **读不到即视为未登录**：库打不开、行缺失、历史脏数据都返回空串，
  让上层走"要求重新登录"，绝不把损坏的值交给网络层。
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .db import connect as db_connect

#: Cookie 头里出现这些字符即为脏数据（CRLF 头注入 / 空字节截断）。
_FORBIDDEN_COOKIE_CHARS = ("\r", "\n", "\x00")


def validate_cookie(cookie: str) -> str:
    """校验并规范化 Cookie，脏数据在落盘前就被拒绝。

    Args:
        cookie: 原始 Cookie 头。

    Returns:
        去除首尾空白后的 Cookie 头。

    Raises:
        ValueError: 含 CR / LF / NUL（会导致 HTTP 头注入）。
    """
    if not isinstance(cookie, str):
        raise ValueError("Cookie 必须是字符串")
    for bad in _FORBIDDEN_COOKIE_CHARS:
        if bad in cookie:
            shown = bad.encode("unicode_escape").decode("ascii")
            raise ValueError(f"Cookie 含非法字符 {shown}，拒绝保存（疑似头注入），请重新登录")
    return cookie.strip()


class CredentialStore:
    """Cookie 凭证的持久化。"""

    def __init__(self, path: Path | None = None, *, conn: sqlite3.Connection | None = None) -> None:
        # path 保留兼容：调用方传的是统一库路径
        self.path = path or Path("meruki.db")
        self._conn = conn if conn is not None else db_connect(self.path)
        self._owns_conn = conn is None

    def load(self) -> str:
        """读取 Cookie 头；没有或已损坏时返回空串。

        历史遗留的脏 Cookie（含 CR/LF/NUL）视为损坏凭证，返回空串让上层要求
        重新登录，绝不原样交出。

        **只有"真的没有"才返回空串**：以前这里把所有 ``sqlite3.Error`` 都吞成
        空串，于是库表损坏 / 磁盘满 / 连接已关 / ``database is locked`` 全被
        报成「还没有登录」，界面指引用户去重新登录——而登录本身也会失败，
        用户就在"让我登录 → 登录不了 → 还是说没登录"里打转，真正的原因
        （IO / 库故障）一个字都看不到。读不出来是故障，抛出去。
        """
        try:
            row = self._conn.execute("SELECT cookie FROM credentials WHERE id = 1").fetchone()
        except sqlite3.Error as exc:
            raise OSError(f"读取登录凭证失败: {exc}") from exc
        if row is None:
            return ""
        try:
            return validate_cookie(str(row[0]))
        except ValueError:
            return ""

    def save(self, cookie: str) -> None:
        """写入 Cookie 头（整行替换）。

        Raises:
            ValueError: Cookie 含 CR / LF / NUL。
            OSError: 数据库写入失败（磁盘满 / 只读 / 被锁）。
        """
        clean = validate_cookie(cookie)
        try:
            with self._conn:  # 事务
                self._conn.execute(
                    "INSERT INTO credentials (id, cookie, updated_at) VALUES (1, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET"
                    "  cookie=excluded.cookie, updated_at=excluded.updated_at",
                    (clean, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as exc:
            raise OSError(f"保存 Cookie 失败: {exc}") from exc

    def clear(self) -> None:
        with contextlib.suppress(sqlite3.Error), self._conn:
            self._conn.execute("DELETE FROM credentials")

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()
