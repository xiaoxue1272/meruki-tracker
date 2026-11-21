"""配置分组的循环切换。

外层「大分页」（列表 / 通知 / 任务 / 配置）已经拆掉：默认就是列表，按 ``s``
进配置。配置内部按 ``config_options.GROUP_ORDER`` 分组分页，``[`` / ``]`` 横切，
切换顺序就在这里算——**分组表就是分页表**，加一组自动多一页。
"""

from __future__ import annotations


def next_group(groups: tuple[str, ...], group: str, delta: int = 1) -> str:
    """循环切换配置分组；``group`` 不在表里时退回第一组。"""
    order = list(groups)
    if not order:
        return ""
    if group not in order:
        return order[0]
    return order[(order.index(group) + delta) % len(order)]
