"""定时刷新的随机抖动。

**为什么不能按固定间隔轮询**

设成 15 分钟就每 15 分钟准点敲一次接口，请求间隔在服务端看来是一条没有
任何起伏的直线——这是脚本的特征，不是人的特征。按 ±20% 随机浮动后，相邻
两次间隔互不相同，周期规律就消失了。

抖动只改"什么时候发"，不改"发几次"：长期平均间隔仍等于配置值，不会越抖越偏。

请求内部的节流抖动（``MerukiApi`` 的 ``jitter`` 参数）是另一回事：那个管的是
单次采集里连续请求之间隔多久，这里管的是两次自动采集之间隔多久。
"""

from __future__ import annotations

import random

#: 默认浮动比例：±20%（配置 15 分钟 → 实际 12~18 分钟）
DEFAULT_RATIO = 0.2

#: 抖动后的下限（秒）。间隔配得极小时避免算出 0 或负数导致忙等。
MIN_SECONDS = 1.0


def jittered_seconds(
    base: float,
    ratio: float = DEFAULT_RATIO,
    *,
    rng: random.Random | None = None,
) -> float:
    """把一个固定间隔抖成相邻互不相同的等待秒数。

    Args:
        base: 配置的基准间隔（秒）。
        ratio: 浮动比例，0.2 表示 ±20%；<=0 关闭抖动，>=1 按 1 处理。
        rng: 注入随机源，便于测试确定化。

    Returns:
        ``base × (1 ± ratio)`` 区间内的秒数（可能带小数），不小于
        :data:`MIN_SECONDS`；``base <= 0`` 时返回 0（表示不启用）。
    """
    if base <= 0:
        return 0.0
    if ratio <= 0:
        return float(base)
    # ratio >= 1 时下界会到 0 或负，没有物理意义，按 100% 夹住
    ratio = min(ratio, 1.0)
    draw = (rng or random).uniform(-ratio, ratio)
    return max(MIN_SECONDS, float(base) * (1.0 + draw))
