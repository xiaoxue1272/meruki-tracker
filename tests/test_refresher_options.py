"""``RefresherOptions``：把「一次刷新怎么跑」的策略从 ``Refresher.__init__`` 收进一个类型。

这里专门守住两件静默失效的事：

1. ``from_config`` 是纯字段搬运，写错映射不会抛异常，只会让生产环境按错的策略跑——所以每个
   来源字段都给**互不相同**的非默认值，逐字段断言，任何一处接错都会被抓住。
2. ``__init__`` 里 ``opt.*`` 到 ``self._*`` 的搬运同理，靠构造后直接查内部状态。
   顺手锁住 A4：``retry_backoff_seconds`` / ``self._backoff`` 不许复活。
"""

from __future__ import annotations

from dataclasses import replace

from meruki_tracker.core.app.refresher import Refresher, RefresherOptions
from meruki_tracker.core.config import Config


def _distinct_config() -> Config:
    """每项策略都给互不相同、且不是默认值的数，便于抓映射接错。"""
    cfg = Config.defaults()
    return replace(
        cfg,
        fetch=replace(cfg.fetch, page_size_expected=13),
        detail=replace(cfg.detail, enable=False),
        storage=replace(cfg.storage, observations_retention_days=21),
        # messages.enable 保持默认 True（与 detail.enable=False 不同），
        # 这样两个 bool 万一被对调，两边断言都会红。
        messages=replace(cfg.messages, enable=True),
    )


def test_from_config_maps_every_field():
    opt = RefresherOptions.from_config(_distinct_config())
    assert opt.page_size_expected == 13
    assert opt.detail_enabled is False
    assert opt.observations_retention_days == 21
    assert opt.messages_enabled is True


def test_from_config_on_defaults_matches_option_defaults():
    """生产默认值不该和 Options 默认值漂移。"""
    assert RefresherOptions.from_config(Config.defaults()) == RefresherOptions()


def test_refresher_uses_options_not_its_own_defaults():
    opt = RefresherOptions(
        page_size_expected=4,
        detail_enabled=False,
        observations_retention_days=7,
        messages_enabled=False,
    )
    # __init__ 只是存下协作者，不调用它们的方法，用占位对象即可。
    r = Refresher(
        api=object(),  # type: ignore[arg-type]
        snapshots=object(),  # type: ignore[arg-type]
        credentials=object(),  # type: ignore[arg-type]
        observations=object(),  # type: ignore[arg-type]
        options=opt,
    )
    assert r._page_size == 4
    assert r._detail_enabled is False
    assert r._retention.days == 7
    assert r._messages_enabled is False


def test_no_options_falls_back_to_defaults():
    r = Refresher(
        api=object(),  # type: ignore[arg-type]
        snapshots=object(),  # type: ignore[arg-type]
        credentials=object(),  # type: ignore[arg-type]
        observations=object(),  # type: ignore[arg-type]
    )
    assert r._page_size == RefresherOptions().page_size_expected
    # 挑"拉哪几条详情"的旋钮已随"每轮全拉"一起删掉，别让它们偷偷长回来
    for gone in ("_list_max_pages", "_recheck_points", "_max_recheck", "_ended_probe_pages"):
        assert not hasattr(r, gone)


def test_dead_retry_backoff_does_not_come_back():
    """审计 A4：``retry_backoff_seconds`` 曾存进 ``self._backoff`` 却从没被读。
    重试只在 API 层做（fetch.retry），这一层再重试会把请求数放大成平方级。"""
    assert "retry_backoff_seconds" not in RefresherOptions.__dataclass_fields__
    r = Refresher(
        api=object(),  # type: ignore[arg-type]
        snapshots=object(),  # type: ignore[arg-type]
        credentials=object(),  # type: ignore[arg-type]
        observations=object(),  # type: ignore[arg-type]
    )
    assert not hasattr(r, "_backoff")
