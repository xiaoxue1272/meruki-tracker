"""结束时间字符串的归一化，以及历史数据的批量规范化。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from meruki_tracker.core.adapters.store.db import connect as db_connect
from meruki_tracker.core.adapters.store.migrate import normalize_legacy_observation_times
from meruki_tracker.core.deadline import SITE_TZ, is_normalized, normalize_deadline

# ------------------------------------------------------------------ 归一化


def test_list_format_becomes_iso_with_site_timezone():
    """列表接口的 BidDeadline 不带时区，按站点时区(UTC+8)补上。"""
    assert normalize_deadline("2026-11-01 21:47:14") == "2026-11-01T21:47:14+08:00"
    assert normalize_deadline("2026-11-01 21:47") == "2026-11-01T21:47:00+08:00"
    assert normalize_deadline("2026/11/01 21:47:14") == "2026-11-01T21:47:14+08:00"


def test_same_instant_as_detail_utc():
    """列表与详情表示同一时刻时，归一化后必须相等（这是"可比较"的前提）。"""
    from_list = datetime.fromisoformat(normalize_deadline("2026-11-01 21:47:14"))
    from_detail = datetime.fromisoformat("2026-11-01T13:47:14+00:00")
    assert from_list == from_detail


def test_already_iso_is_left_alone():
    iso = "2026-11-01T13:47:14+00:00"
    assert normalize_deadline(iso) == iso


def test_unparseable_kept_raw():
    """解析不了就保留原值——宁可格式不规范也不能丢数据。"""
    assert normalize_deadline("garbage") == "garbage"
    assert normalize_deadline("") == ""


def test_is_normalized():
    assert is_normalized("2026-11-01T13:47:14+00:00")
    assert is_normalized("2026-11-01T21:47:14+08:00")
    assert is_normalized("")  # 空值不算"需要迁移"
    assert not is_normalized("2026-11-01 21:47:14")  # 无时区
    assert not is_normalized("garbage")


def test_site_timezone_is_utc8():
    assert SITE_TZ.utcoffset(None).total_seconds() == 8 * 3600


# ------------------------------------------------------------------ 历史数据批量迁移


def _db_with_legacy_rows(tmp_path: Path):
    conn = db_connect(tmp_path / "meruki.db")
    with conn:
        conn.executemany(
            "INSERT INTO observations (asin, observed_at, kind, end_time_raw) VALUES (?, ?, ?, ?)",
            [
                ("A1", "2026-10-30T01:00:00+00:00", "list", "2026-11-01 21:47:14"),
                ("A2", "2026-10-30T01:00:00+00:00", "list", "2026-11-02 21:02:25"),
                ("A3", "2026-10-30T01:00:00+00:00", "detail", "2026-11-01T13:47:14+00:00"),
                ("A4", "2026-10-30T01:00:00+00:00", "list", "garbage"),
            ],
        )
    return conn


def test_migration_normalizes_legacy_rows(tmp_path: Path):
    conn = _db_with_legacy_rows(tmp_path)
    try:
        fixed = normalize_legacy_observation_times(conn)
        assert fixed == 2, "只该转换两条能解析的无时区值"

        rows = dict(conn.execute("SELECT asin, end_time_raw FROM observations").fetchall())
        assert rows["A1"] == "2026-11-01T21:47:14+08:00"
        assert rows["A2"] == "2026-11-02T21:02:25+08:00"
        assert rows["A3"] == "2026-11-01T13:47:14+00:00", "已经是 ISO 的不动"
        assert rows["A4"] == "garbage", "解析不了的保持原样"
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path):
    """跑过一次就打标记，之后不再扫描（也不重复转换）。"""
    conn = _db_with_legacy_rows(tmp_path)
    try:
        assert normalize_legacy_observation_times(conn) == 2
        assert normalize_legacy_observation_times(conn) == 0, "第二次应当什么都不做"
    finally:
        conn.close()


def test_migration_marks_meta(tmp_path: Path):
    conn = _db_with_legacy_rows(tmp_path)
    try:
        normalize_legacy_observation_times(conn)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='observations_time_normalized'"
        ).fetchone()
        assert row is not None and row[0] == "1"
    finally:
        conn.close()
