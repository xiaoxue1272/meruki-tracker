"""配置的持久化（``settings`` 表，一个配置项一行）。

**为什么一行一个键**

配置项会随版本增减。整体存一个 JSON blob 的话，升级时"旧值 + 新默认值"
的合并逻辑必须自己写，容易漏项、也难定位是哪一项坏了。一行一个键的好处：

- 读的时候直接按 key 取，缺的用默认值兜住（新版本加的项自动生效）；
- 某一项坏了只影响那一项，不会整份配置读不出来；
- 排查时可以直接 ``SELECT * FROM settings`` 看。

值的类型用 JSON 编码，所以 list / tuple / bool / 数字都能原样往返。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

log = logging.getLogger(__name__)


class SettingsStore:
    """程序内配置的读写。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def load_all(self) -> dict[str, Any]:
        """读出全部配置项。

        单项解码失败只跳过该项（记日志），不让整份配置读不出来——
        否则一个损坏的值就会让程序完全起不来。
        """
        out: dict[str, Any] = {}
        try:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        except sqlite3.Error:
            log.warning("读取配置失败，使用默认值", exc_info=True)
            return {}
        bad: list[str] = []
        for key, raw in rows:
            try:
                out[key] = json.loads(raw)
            except (TypeError, ValueError):
                bad.append(key)
        if bad:
            log.warning("以下配置项无法解析，已忽略: %s", ", ".join(sorted(bad)))
        return out

    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        """写一个配置项。

        Raises:
            OSError: 写库失败（磁盘满 / 只读）。
        """
        payload = json.dumps(_jsonable(value), ensure_ascii=False)
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, payload),
                )
        except sqlite3.Error as exc:
            raise OSError(f"保存配置失败: {exc}") from exc

    def ensure_defaults(self, values: dict[str, Any]) -> int:
        """把**缺失**的配置项用给定值补齐，已存在的一律不动。

        为什么需要它：表里只存"用户改过的项"会让数据库失去单一真实来源的
        意义——想看当前完整配置得"库 + 代码默认值"两头拼。启动时补齐之后，
        ``SELECT * FROM settings`` 就是当前生效的全部配置。

        用 ``INSERT OR IGNORE``：用户改过的值不会被默认值覆盖，而新版本
        新增的配置项会自动出现。

        Args:
            values: 平铺的 ``{"section.name": value}``。

        Returns:
            实际补进去的条数（0 表示库里已经齐了）。
        """
        payload = [(k, json.dumps(_jsonable(v), ensure_ascii=False)) for k, v in values.items()]
        if not payload:
            return 0
        try:
            with self._conn:
                before = self._count()
                self._conn.executemany(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", payload
                )
                return self._count() - before
        except sqlite3.Error as exc:
            raise OSError(f"初始化配置失败: {exc}") from exc

    def _count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM settings").fetchone()
        return int(row[0]) if row else 0

    def clear(self) -> None:
        """清空全部配置（回到默认值）。"""
        with self._conn:
            self._conn.execute("DELETE FROM settings")


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value
