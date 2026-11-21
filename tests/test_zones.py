"""配置分组的纯逻辑：循环切换。

外层大分页已经拆掉（2026-11-13）：默认就是列表，按 ``s`` 进配置，
配置内部按 ``config_options.GROUP_ORDER`` 分组分页。这一层刻意不含界面
代码——分组顺序错了，不该需要起一个 prompt_toolkit 应用才测得出来。
"""

from __future__ import annotations

import pytest

from meruki_tracker.tui.zones import next_group


def test_next_group_cycles_and_falls_back():
    groups = ("基本", "通知", "登录")
    assert next_group(groups, "基本", 1) == "通知"
    assert next_group(groups, "登录", 1) == "基本"
    assert next_group(groups, "基本", -1) == "登录"
    assert next_group(groups, "不在表里", 1) == "基本"
    assert next_group((), "基本", 1) == ""


@pytest.mark.parametrize("delta", [1, -1])
def test_group_cycle_returns_to_the_start(delta: int):
    groups = ("基本", "通知", "登录")
    here = groups[0]
    for _ in range(len(groups)):
        here = next_group(groups, here, delta)
    assert here == groups[0]
