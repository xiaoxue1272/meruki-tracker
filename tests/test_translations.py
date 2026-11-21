"""译文缓存与翻译编排。

测的是"会不会白打接口"这件事：翻译是网络请求，列表十几条拍品如果每轮都重翻，
就是十几倍的请求量。所以缓存命中、失败不落库、批量只翻缺的，都得盯住。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from meruki_tracker.core.adapters.store.db import SCHEMA_VERSION, connect
from meruki_tracker.core.adapters.store.translations import TranslationStore
from meruki_tracker.core.app.translator import Translator
from meruki_tracker.core.domain.goods import TRANSLATE_LIMIT
from meruki_tracker.core.errors import NetworkError


def _store(tmp_path: Path) -> TranslationStore:
    return TranslationStore(tmp_path / "t.db")


class _Api:
    """只实现 translate（Translator 的 Protocol 只要这一个方法）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    async def translate(
        self, *, title: str = "", desc: str = "", target: str = "", source: str = ""
    ):
        self.calls.append({"title": title, "desc": desc, "target": target, "source": source})
        if self._fail:
            raise NetworkError("网络炸了")
        return (f"译:{title}", f"译:{desc}")


def test_schema_has_a_translations_table(tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "translations" in names
        assert SCHEMA_VERSION >= 6
    finally:
        conn.close()


def test_saving_a_translation_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("A1", title="尼康 F3", desc="一行目")
    got = store.get("A1")
    assert got is not None
    assert (got.title, got.desc) == ("尼康 F3", "一行目")
    assert store.count() == 1


def test_both_sides_empty_is_not_saved(tmp_path: Path) -> None:
    """接口什么都没翻出来时不能落一行空壳，否则下次会以为"已翻译"不再重试。"""
    store = _store(tmp_path)
    store.save("A1", title="", desc="")
    assert store.count() == 0


def test_saving_twice_overwrites_instead_of_duplicating(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("A1", title="旧")
    store.save("A1", title="新")
    assert store.count() == 1
    assert store.get("A1").title == "新"


async def test_translated_items_are_no_longer_treated_as_missing(tmp_path: Path) -> None:
    """翻过之后 ``needs`` 就该是 False——批量翻译靠它跳过，而不是靠 translate 自己省。

    ``translate`` 本身**总是**打接口（刷新详情时标题可能被卖家改过，要重翻），
    省请求的判断在 ``needs`` / ``translate_missing`` 这一层。
    """
    api = _Api()
    tr = Translator(api, _store(tmp_path))
    assert tr.needs("A1") is True

    assert await tr.translate("A1", title="ニコン", desc="説明") is not None

    assert tr.needs("A1") is False
    await tr.translate_missing([("A1", "ニコン", "説明")])
    assert len(api.calls) == 1, "已经有译文的拍品不该在批量里再翻一次"


async def test_translator_returns_none_on_failure_and_keeps_the_cache_empty(tmp_path: Path) -> None:
    api = _Api(fail=True)
    tr = Translator(api, _store(tmp_path))

    assert await tr.translate("A1", title="ニコン", desc="説明") is None
    assert tr.cached("A1") is None
    assert tr.needs("A1") is True, "失败了下次还得能重试"


async def test_translator_clips_the_description_to_the_api_limit(tmp_path: Path) -> None:
    api = _Api()
    tr = Translator(api, _store(tmp_path))

    await tr.translate("A1", title="ニコン", desc="あ" * (TRANSLATE_LIMIT + 500))

    assert len(api.calls[0]["desc"]) == TRANSLATE_LIMIT


async def test_translate_missing_only_touches_items_without_a_translation(tmp_path: Path) -> None:
    api = _Api()
    store = _store(tmp_path)
    store.save("A1", title="已有")
    tr = Translator(api, store)

    stats = await tr.translate_missing([("A1", "一", ""), ("A2", "二", "描述二")])

    assert stats.done == 1
    assert [c["title"] for c in api.calls] == ["二"]


async def test_translate_missing_reports_progress(tmp_path: Path) -> None:
    tr = Translator(_Api(), _store(tmp_path))
    seen: list[str] = []

    await tr.translate_missing([("A1", "一", ""), ("A2", "二", "")], progress=seen.append)

    assert seen == ["翻译 1/2", "翻译 2/2"]


async def test_one_failure_does_not_stop_the_rest(tmp_path: Path) -> None:
    """10 条里有 1 条接口抽风，不能丢下另外 9 条。"""

    class _Flaky(_Api):
        async def translate(
            self, *, title: str = "", desc: str = "", target: str = "", source: str = ""
        ):
            self.calls.append({"title": title, "desc": desc, "target": target, "source": source})
            if title == "坏的":
                raise NetworkError("这条炸了")
            return (f"译:{title}", f"译:{desc}")

    tr = Translator(_Flaky(), _store(tmp_path))
    stats = await tr.translate_missing([("A1", "坏的", ""), ("A2", "好的", "")])

    assert (stats.done, stats.failed) == (1, 1)
    assert tr.cached("A2") is not None


async def test_title_of_falls_back_to_the_original(tmp_path: Path) -> None:
    tr = Translator(_Api(), _store(tmp_path))
    assert tr.title_of("A1", "ニコン") == "ニコン"

    await tr.translate("A1", title="尼康", desc="")
    assert tr.title_of("A1", "ニコン") == "译:尼康"


def test_cached_all_returns_everything_at_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("A1", title="一")
    store.save("A2", title="二")
    tr = Translator(_Api(), store)

    assert set(tr.cached_all()) == {"A1", "A2"}


def test_timestamps_are_utc_iso(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("A1", title="一", updated_at=datetime(2026, 11, 5, 12, 0, tzinfo=UTC))

    assert store.get("A1").updated_at.startswith("2026-11-05T12:00")


@pytest.mark.parametrize("asin", ["", "A1"])
def test_delete_removes_only_that_row(tmp_path: Path, asin: str) -> None:
    store = _store(tmp_path)
    store.save("A1", title="一")
    store.save("A2", title="二")

    store.delete(asin)

    remaining = set(store.all())
    assert remaining == ({"A2"} if asin else {"A1", "A2"})
