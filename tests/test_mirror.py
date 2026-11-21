"""接口镜像 + 逐轮比对 + 定时抖动的测试（不触网）。

三部分各管一件事：

1. ``MirrorStore`` 把接口给的**每个字段**都存下来，且一轮采集是一个事务。
2. ``domain/diff.py`` 只报"变了什么"，不猜"为什么变"。
3. ``jittered_seconds`` 让相邻两次自动刷新的间隔互不相同。
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meruki_tracker.core.adapters.meruki.api import ListItem, _map_list_page
from meruki_tracker.core.adapters.store.db import connect
from meruki_tracker.core.adapters.store.mirror import MirrorStore
from meruki_tracker.core.domain.diff import (
    KIND_APPEARED,
    KIND_DETAIL_REFRESHED,
    KIND_DISAPPEARED,
    KIND_FIELD_CHANGED,
    Change,
    diff_detail_rounds,
    diff_list_rounds,
    summarize,
)
from meruki_tracker.core.jitter import MIN_SECONDS, jittered_seconds

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _row(asin: str, **kw) -> dict:
    base = {
        "asin": asin,
        "item_id": kw.get("item_id", "1"),
        "name": kw.get("name", f"拍品 {asin}"),
        "url": kw.get("url", f"https://x/{asin}"),
        "detail_url": kw.get("detail_url", f"hex{asin}"),
        "bid_deadline": kw.get("bid_deadline", "2026-10-31 21:00:00"),
        "bid_deadline_str": kw.get("bid_deadline_str", "1h: 00m: 00s"),
        "created_time": kw.get("created_time", "2026-10-30 10:00:00"),
        "image_url": kw.get("image_url", f"https://img/{asin}.jpg"),
        "user_id": kw.get("user_id", "9999999"),
    }
    return base


# ------------------------------------------------------------------ API 全字段


def test_list_item_keeps_every_field_the_api_returns():
    """接口给的 10 个字段一个都不能丢。"""
    payload = {
        "TotalItem": 1,
        "TotalPage": 1,
        "CurrentPage": 1,
        "ItemPerPage": 10,
        "ShowPageNum": 5,
        "Data": [
            {
                "Asin": "v1234567890",
                "Id": "50988630",
                "Name": "尼康 F3",
                "Url": "https://page.auctions.yahoo.co.jp/jp/auction/v1234567890",
                "DetailUrl": "abc123",
                "BidDeadline": "2026-10-31 21:02:09",
                "BidDeadlineStr": "18h: 13m:",
                "CreatedTime": "2026-10-30 10:00:00",
                "ImageUrl": "https://img/x.jpg",
                "UserId": "9999999",
            }
        ],
    }
    page = _map_list_page(payload, 1, status="进行中")
    item = page.items[0]
    assert (item.asin, item.item_id, item.name) == ("v1234567890", "50988630", "尼康 F3")
    assert item.detail_url == "abc123"
    assert item.bid_deadline == "2026-10-31 21:02:09"
    assert item.bid_deadline_str == "18h: 13m:"
    assert item.created_time == "2026-10-30 10:00:00"
    assert item.image_url == "https://img/x.jpg"
    assert item.user_id == "9999999"
    # 原始 JSON 兜住未来新增字段
    assert item.raw["Asin"] == "v1234567890"


def test_list_page_keeps_paging_metadata():
    page = _map_list_page(
        {
            "TotalItem": 3,
            "TotalPage": 2,
            "CurrentPage": 2,
            "ItemPerPage": 10,
            "ShowPageNum": 5,
            "Data": [],
        },
        2,
    )
    assert (page.status, page.item_per_page, page.show_page_num) == ("进行中", 10, 5)


def test_legacy_property_aliases_still_work():
    """老的属性名没删，避免下游（refresher / 测试）集体改名。"""
    item = ListItem(asin="a", bid_deadline="x", detail_url="y")
    assert item.list_deadline == "x"
    assert item.detail_url_hex == "y"


# ------------------------------------------------------------------ 镜像存储


def _store(tmp_path: Path) -> MirrorStore:
    return MirrorStore(tmp_path / "meruki.db", conn=connect(tmp_path / "meruki.db"))


def test_save_round_persists_every_list_field(tmp_path: Path):
    store = _store(tmp_path)
    store.save_round(
        status="进行中",
        pages=[{"status": "进行中", "page": 1, "item_per_page": 10, "returned": 1}],
        items=[_row("A1", item_id="777")],
        seen_at=NOW,
    )
    rows = store.list_items()
    assert len(rows) == 1
    got = rows[0]
    assert (got.status, got.asin, got.item_id) == ("进行中", "A1", "777")
    assert got.image_url == "https://img/A1.jpg"
    assert got.user_id == "9999999"
    assert got.bid_deadline_str == "1h: 00m: 00s"


def test_save_round_records_paging_metadata(tmp_path: Path):
    store = _store(tmp_path)
    store.save_round(
        status="进行中",
        pages=[
            {
                "status": "进行中",
                "page": 1,
                "item_per_page": 10,
                "show_page_num": 5,
                "current_page": 1,
                "total_item": 14,
                "total_page": 2,
                "returned": 10,
            }
        ],
        items=[],
        seen_at=NOW,
    )
    assert store.count("list_pages") == 1


def test_incomplete_round_does_not_wipe_previous_items(tmp_path: Path):
    """列表残缺时不能把上一轮条目当成"都不见了"。"""
    store = _store(tmp_path)
    store.save_round(status="进行中", pages=[], items=[_row("A1"), _row("A2")], seen_at=NOW)
    # 残缺的一轮（比如第一页就出错了）：不替换
    store.save_round(status="进行中", pages=[], items=[_row("A1")], seen_at=NOW, replace=False)
    assert {r.asin for r in store.list_items()} == {"A1", "A2"}


def test_complete_round_replaces_bucket(tmp_path: Path):
    store = _store(tmp_path)
    store.save_round(status="进行中", pages=[], items=[_row("A1"), _row("A2")], seen_at=NOW)
    store.save_round(status="进行中", pages=[], items=[_row("A1")], seen_at=NOW, replace=True)
    assert {r.asin for r in store.list_items()} == {"A1"}


def test_buckets_are_independent(tmp_path: Path):
    """同一个拍品在两个 status 分桶里都有，互不覆盖。"""
    store = _store(tmp_path)
    store.save_round(status="进行中", pages=[], items=[_row("A1", name="旧名")], seen_at=NOW)
    store.save_round(status="已结束", pages=[], items=[_row("A1", name="新名")], seen_at=NOW)
    assert len(store.list_items()) == 2
    assert {r.name for r in store.list_items(status="已结束")} == {"新名"}


def test_detail_and_change_round_trip(tmp_path: Path):
    store = _store(tmp_path)
    store.save_round(
        status="进行中",
        pages=[],
        items=[_row("A1")],
        details=[
            {
                "asin": "A1",
                "name": "尼康",
                "end_at": "2026-10-31T21:00:00+00:00",
                "ended": False,
                "bid_price": 12500.0,
                "buy_now_price": None,
                "auto_extend": True,
                "early_termination": None,
                "raw": {"goodsInfo": {"x": 1}},
            }
        ],
        changes=[{"asin": "A1", "kind": "appeared", "field": "", "old_value": "", "new_value": ""}],
        seen_at=NOW,
    )
    details = store.detail_items()
    assert details["A1"].bid_price == 12500.0
    assert details["A1"].auto_extend is True
    assert details["A1"].ended is False
    recent = store.recent_changes()
    assert recent and recent[0]["kind"] == KIND_APPEARED


def test_failed_round_writes_nothing(tmp_path: Path):
    """半轮数据比没有数据更糟：写失败必须整轮回滚。"""
    store = _store(tmp_path)
    bad = {**_row("BAD"), "page": "不是数字"}
    with pytest.raises(ValueError):
        store.save_round(
            status="进行中",
            pages=[],
            items=[_row("A1"), bad],
            seen_at=NOW,
        )
    # 事务已回滚：连第一条也不能留下
    assert store.count("list_items") == 0


def test_unknown_table_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="不支持的镜像表"):
        _store(tmp_path).count("auctions; DROP TABLE meta")


# ------------------------------------------------------------------ 比对


def test_diff_reports_appeared_and_field_change():
    prev = {"A1": _row("A1", name="旧名")}
    cur = {"A1": _row("A1", name="新名"), "A2": _row("A2")}
    changes = diff_list_rounds(prev, cur, complete=True)
    kinds = {c.kind for c in changes}
    assert KIND_APPEARED in kinds and KIND_FIELD_CHANGED in kinds
    appeared = [c for c in changes if c.kind == KIND_APPEARED]
    assert [c.asin for c in appeared] == ["A2"]
    changed = [c for c in changes if c.kind == KIND_FIELD_CHANGED]
    assert changed[0].field == "name"
    assert (changed[0].old, changed[0].new) == ("旧名", "新名")


def test_diff_reports_disappeared_only_when_complete():
    prev = {"A1": _row("A1"), "A2": _row("A2")}
    cur = {"A1": _row("A1")}
    assert [c.asin for c in diff_list_rounds(prev, cur, complete=True)] == ["A2"]
    # 列表不完整：绝不报消失
    assert diff_list_rounds(prev, cur, complete=False) == []


def test_diff_ignores_countdown_field():
    """BidDeadlineStr 是倒计时，每轮都变，比它会刷屏。"""
    prev = {"A1": _row("A1", bid_deadline_str="5h: 00m: 00s")}
    cur = {"A1": _row("A1", bid_deadline_str="4h: 59m: 58s")}
    assert diff_list_rounds(prev, cur, complete=True) == []


def test_diff_ignores_unknown_extra_keys():
    """镜像行里多出来的键（raw 之类）不参与比较，也不会崩。"""
    prev = {"A1": {**_row("A1"), "raw": {"a": 1}}}
    cur = {"A1": {**_row("A1"), "raw": {"a": 2}}}
    assert diff_list_rounds(prev, cur, complete=True) == []


def test_detail_diff_skips_first_observation():
    """第一次拿到详情不算"变了"。"""
    cur = {"A1": {"asin": "A1", "name": "x", "end_at": "", "ended": False, "bid_price": 1.0}}
    assert diff_detail_rounds({}, cur) == []


def test_detail_diff_reports_price_change():
    prev = {"A1": {"asin": "A1", "name": "x", "bid_price": 100.0, "ended": False}}
    cur = {"A1": {"asin": "A1", "name": "x", "bid_price": 200.0, "ended": True}}
    changes = diff_detail_rounds(prev, cur)
    fields = {c.field for c in changes}
    assert "bid_price" in fields and "ended" in fields
    assert all(c.kind == KIND_DETAIL_REFRESHED for c in changes)


def test_summarize_is_readable():
    assert summarize([]) == "无变化"
    text = summarize(
        [
            Change(asin="a", kind=KIND_APPEARED),
            Change(asin="b", kind=KIND_DISAPPEARED),
            Change(asin="c", kind=KIND_FIELD_CHANGED, field="name"),
        ]
    )
    assert "新出现" in text and "消失" in text and "字段变化" in text


def test_change_label_is_chinese():
    """落库的 field 是英文键，展示给人时要中文。"""
    change = Change(asin="a", kind=KIND_FIELD_CHANGED, field="name", old="旧", new="新")
    assert change.label == "标题"
    assert change.as_row()["old_value"] == "旧"


# ------------------------------------------------------------------ 抖动


def test_jitter_stays_within_20_percent():
    rng = random.Random(1234)
    for _ in range(500):
        got = jittered_seconds(900, 0.2, rng=rng)
        assert 900 * 0.8 <= got <= 900 * 1.2


def test_jitter_produces_different_intervals():
    """固定间隔是脚本特征；抖动后相邻值不应全部相同。"""
    rng = random.Random(7)
    values = {jittered_seconds(900, 0.2, rng=rng) for _ in range(20)}
    assert len(values) > 10


def test_jitter_averages_to_base():
    rng = random.Random(99)
    samples = [jittered_seconds(600, 0.2, rng=rng) for _ in range(3000)]
    assert abs(sum(samples) / len(samples) - 600) < 10


def test_jitter_disabled_and_zero_base():
    assert jittered_seconds(900, 0) == 900.0
    assert jittered_seconds(0) == 0.0
    assert jittered_seconds(-5) == 0.0


def test_jitter_never_returns_zero():
    """间隔配得极小时也不能抖出 0，否则循环会忙等。"""
    rng = random.Random(3)
    for _ in range(100):
        assert jittered_seconds(2, 0.9, rng=rng) >= MIN_SECONDS


def test_jitter_ratio_is_clamped():
    """ratio >= 1 时下界会到 0 或负数，按 100% 夹住。"""
    rng = random.Random(11)
    for _ in range(100):
        assert jittered_seconds(100, 5.0, rng=rng) >= MIN_SECONDS
