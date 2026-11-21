"""一次性数据修缮与旧值规范化。

这些函数都**幂等**：跑过就在 ``meta`` 打一个标记，之后启动直接跳过。

* ``normalize_legacy_observation_times`` —— 早期观测结束时间是列表接口的原样
  字符串（无时区），统一成带时区的 ISO；
* ``repair_mislabeled_unfollowed`` / ``drop_unfollowed_state`` —— 「已取关」这个
  状态取消后的数据归并；
* ``drop_obsolete_settings`` —— 删掉已经退役的配置键（否则启动时会被当成未知项）；
* ``drop_ended_history`` / ``split_price_fields`` —— 「已结束」桶与价格字段的调整。

注意：从旧文件布局（``state.json`` / ``cookies`` / ``observations.sqlite``）搬进
``meruki.db`` 的那段迁移代码已经删掉——它只服务一次性的历史升级，那次升级早就
完成了，留着只会让每个新读者去追一条走不通的路。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

log = logging.getLogger(__name__)

#: meta 里记录"观测时间已规范化"的标记，跑过就不再扫描
_OBS_TIME_FLAG = "observations_time_normalized"


def normalize_legacy_observation_times(conn: sqlite3.Connection) -> int:
    """把旧格式的观测结束时间规范成带时区的 ISO（一次性，幂等）。

    早期版本直接存列表接口的原始字符串（``"2026-11-01 21:47:14"``，无时区
    标注），而详情存的是带时区的 ISO。同列两种格式，事后回溯时没法比较。

    这里把能解析的旧值批量转换；解析不了的保持原样（不丢数据）。转换完成后
    在 ``meta`` 打标记，之后启动不再扫描。

    Args:
        conn: 统一数据库连接。

    Returns:
        实际转换的条数。
    """
    done = conn.execute("SELECT value FROM meta WHERE key = ?", (_OBS_TIME_FLAG,)).fetchone()
    if done is not None:
        return 0

    from ...deadline import is_normalized, normalize_deadline

    rows = conn.execute("SELECT id, end_time_raw FROM observations").fetchall()
    updates = []
    for row_id, raw in rows:
        if not raw or is_normalized(raw):
            continue
        normalized = normalize_deadline(raw)
        # 解析不了的会原样返回，这种不算"转换过"，别计进返回值
        if normalized != raw:
            updates.append((normalized, row_id))
    if updates:
        with conn:
            conn.executemany("UPDATE observations SET end_time_raw = ? WHERE id = ?", updates)
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
            (_OBS_TIME_FLAG,),
        )
    return len(updates)


#: meta 里记录"误标取关已修复"的标记，跑过就不再扫描
_UNFOLLOW_REPAIR_FLAG = "unfollow_after_end_repaired"


def repair_mislabeled_unfollowed(conn: sqlite3.Connection) -> int:
    """修复历史上被误标为「已取关」的**正常结束**拍品（一次性，幂等）。

    旧逻辑把"不在进行中列表里"一律当成取关，而列表接口只拉进行中——拍卖一
    结束就从列表消失，于是正常结束的拍品全被标成 ``unfollowed``。用户看到的
    就是"我没取关，它自己变成已取关了"。

    判据为什么可靠：**真正的取关只可能在拍卖还在进行中被我们看见**（只有进行
    中的拍卖才会出现在列表里）。所以 ``unfollowed_at`` 晚于 ``end_at`` 的记录
    一定是被旧逻辑误标的，可以放心改回「已结束」。

    Args:
        conn: 统一数据库连接。

    Returns:
        实际修复的条数。
    """
    done = conn.execute("SELECT value FROM meta WHERE key = ?", (_UNFOLLOW_REPAIR_FLAG,)).fetchone()
    if done is not None:
        return 0

    # ISO 时间串可以直接按字典序比较（都是 UTC、同格式）
    rows = conn.execute(
        """
        SELECT asin FROM auctions
         WHERE user_query = 'unfollowed'
           AND door_state <> 'ended'
           AND end_at IS NOT NULL
           AND unfollowed_at IS NOT NULL
           AND unfollowed_at >= end_at
        """
    ).fetchall()
    if rows:
        with conn:
            conn.executemany(
                """
                UPDATE auctions
                   SET user_query = 'following',
                       unfollowed_at = NULL,
                       door_state = 'ended',
                       concluded_at = end_at
                 WHERE asin = ?
                """,
                [(r[0],) for r in rows],
            )
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
            (_UNFOLLOW_REPAIR_FLAG,),
        )
    return len(rows)


#: meta 里记录"「已取关」状态已撤销"的标记
_UNFOLLOW_DROPPED_FLAG = "unfollowed_state_dropped"


def drop_unfollowed_state(conn: sqlite3.Connection) -> int:
    """把还残留的「已取关」记录并入「已结束」（一次性，幂等）。

    「已取关」这个状态被取消了：列表接口只给「进行中」，拍品从里面消失时分不清
    "结束了"还是"被取消关注了"，那是本地猜的。既然两种情况结论都是"不在进行中
    了"，就统一归终态，用户可以按结束时间排序、也能清理。

    这里把老库里残留的 ``unfollowed`` 行直接改成"已结束"，``concluded_at`` 用原来
    的取关时间兜底（没有就用当前时刻）。不改成 ``following`` 让下一轮刷新再判——
    那会让这些记录在刷新前一直挂在「关注中」，还可能因为 PENDING 结束时间永远
    判不出来。

    Args:
        conn: 统一数据库连接。

    Returns:
        被改写的条数。
    """
    done = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (_UNFOLLOW_DROPPED_FLAG,)
    ).fetchone()
    if done is not None:
        return 0

    rows = conn.execute("SELECT asin FROM auctions WHERE user_query = 'unfollowed'").fetchall()
    if rows:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with conn:
            conn.executemany(
                """
                UPDATE auctions
                   SET user_query = 'following',
                       unfollowed_at = NULL,
                       door_state = 'ended',
                       concluded_at = COALESCE(concluded_at, unfollowed_at, ?)
                 WHERE asin = ?
                """,
                [(now, r[0]) for r in rows],
            )
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
            (_UNFOLLOW_DROPPED_FLAG,),
        )
    return len(rows)


# ---------------------------------------------------------------- 配置键退役

#: 已经退役的配置项。删掉字段后如果不清理，``Config.from_mapping`` 会报
#: "未知配置项"直接启动失败——老库里这些键是**写进库的**，不是代码默认值。
#:
#: ``fetch.ended_sync_pages``：不再主动同步「已结束」历史，改由拍品从「进行中」
#: 滚过去自然补齐。
#:
#: ``detail.ended_price_per_cycle``：它只服务于「批量补价格」那条路径，而那个
#: 入口（``u`` 键）早已移除、函数本身也是没人调的死代码，一并删了。
_OBSOLETE_SETTINGS: tuple[str, ...] = (
    "fetch.ended_sync_pages",
    "detail.ended_price_per_cycle",
    # macOS 通知渠道已移除（用户 2026-11-11 要求"本地的弹窗不要了"）：
    # 老库里还留着这一行，不清掉启动就会报「未知配置项」。
    "channels.system_enable",
    # 三个"节奏"配置项收敛成一个 ``fetch.jitter_ratio``（2026-11-12）：
    # 队列间隔抖动、刷新间隔抖动、队列任务间隔都不再单独存。
    "queue.jitter_ratio",
    "refresh.jitter_ratio",
    "queue.delay_seconds",
    # 任务队列整套删掉（2026-11-13）：刷新互斥改由 refresher 自身的 busy
    # 守卫兜住，不再需要串行队列，也没有"要不要排队"这个开关了。
    "queue.enable",
    # 从来没被任何代码读过的死配置（浏览器登录失败时"是否回退到粘贴"，
    # 实际登录方式由用户在界面上选，不存在自动回退）。
    "login.fallback_to_paste",
    # 结束提醒改成「结束前复查点」这种显式点列表（2026-11-14）：
    # "从某点起每隔多久再提醒一次"那套推算被否掉了，换成手填几个点。
    "notify.lead_minutes",
    "notify.repeat_minutes",
    # 结束改成"每条挨个拉详情、以详情的 over 为准"（2026-11-15）：
    # 页数上限、结束查证、详情复查点、单轮兜底数四个旋钮都没意义了。
    "fetch.list_max_pages",
    "fetch.ended_probe_pages",
    "detail.recheck_before_end_minutes",
    "detail.max_recheck_per_cycle",
)


def drop_obsolete_settings(conn: sqlite3.Connection) -> int:
    """删除已退役的配置项（每次启动都跑一遍）。

    为什么必须有这一步：配置存在库里，删掉代码里的字段之后，**老库里的那一行
    还在**，下次启动 ``Config.from_mapping`` 读到不认识的键就抛
    ``ConfigError: detail.ended_price_per_cycle: 未知配置项`` —— 程序直接起不来。
    真实踩过：删掉字段后拿带旧键的库启动，报的就是这个。

    **刻意不设"已执行"标记**：``DELETE`` 本身就是幂等的（键不存在影响 0 行），
    而这份名单会随时间增加。之前用一个 ``obsolete_settings_dropped`` 标记跳过
    重跑，结果第二次往名单里加键时老库上的那条被**静默忽略**，启动照样崩——
    同样的坑还有一次是 ``ended_history_synced`` 挡住了结束时间回填。
    代价只有启动时两条 ``DELETE``，不值得再引入一个会过期的状态。

    Args:
        conn: 统一数据库连接。

    Returns:
        删掉的配置项条数。
    """
    with conn:
        cur = conn.executemany(
            "DELETE FROM settings WHERE key = ?", [(k,) for k in _OBSOLETE_SETTINGS]
        )
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return removed


# ------------------------------------------------------ 清空旧的「已结束」历史

_ENDED_HISTORY_DROPPED_FLAG = "ended_history_dropped"


def drop_ended_history(conn: sqlite3.Connection) -> int:
    """清掉旧的「已结束」历史记录（一次性，幂等）。

    「已结束」不再靠翻 50 页去同步历史，改由拍品从「进行中」滚过去自然补齐。
    库里那 500 多条是**旧机制**灌进来的，留着会让「已结束」视图看起来像一份
    完整历史、其实再也不会更新。用户明确要求清空。

    只动 ``auctions`` 里的终态行，**不动观测历史**（``observations`` 是诊断用
    的时间线，删了就再也查不到某个拍品当时多少钱）。

    Args:
        conn: 统一数据库连接。

    Returns:
        删掉的拍品条数。
    """
    done = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (_ENDED_HISTORY_DROPPED_FLAG,)
    ).fetchone()
    if done is not None:
        return 0

    with conn:
        cur = conn.execute("DELETE FROM auctions WHERE door_state = 'ended'")
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
            (_ENDED_HISTORY_DROPPED_FLAG,),
        )
    return removed


# ----------------------------------------- 拆分「当前价 / 一口价」两个字段

_PRICE_SPLIT_FLAG = "price_fields_split"


def split_price_fields(conn: sqlite3.Connection) -> int:
    """把「一口价被写进当前价」的老数据拆回两个字段（一次性，幂等）。

    历史经过：
    1. 早先一口价商品的 ``bidPrice`` 是 0，界面直接显示 `¥0`（真实反馈：
       「有些商品价格是0，是因为是一口价商品」）。
    2. 于是加了个兜底——把 ``buyNowPrice`` 塞进 ``latest_bid``，`¥0` 是没了，
       但"当前价"里装的其实是一口价，两个概念混在一列。
    3. 用户要求**分开显示**（「你分开两个字段展示吧，就当前价和一口价」），
       所以这一步把当年塞进去的值挪回 ``buy_now_price``。

    判据（两条同时成立才动）：镜像表里同 asin 的 ``bid_price = 0``（一口价
    商品没有拍卖价），且 ``auctions.latest_bid`` 恰好等于那个
    ``buy_now_price``——说明这个值就是当年兜底塞进去的，不是真实拍卖价。

    顺带把**所有**有详情记录的拍品的一口价补齐（普通拍卖也可能有一口价）。
    这一步同时覆盖了第 1 条那种"``latest_bid`` 还是 0"的原始坏数据：那种行
    一口价会被补上，当前价保持 0（渲染层把 0 当"没有"，显示 `—`）。

    Args:
        conn: 统一数据库连接。

    Returns:
        被拆分（当前价清空）的拍品条数。
    """
    done = conn.execute("SELECT value FROM meta WHERE key = ?", (_PRICE_SPLIT_FLAG,)).fetchone()
    if done is not None:
        return 0

    with conn:
        cur = conn.execute(
            "UPDATE auctions SET latest_bid = NULL, buy_now_price = ("
            "  SELECT d.buy_now_price FROM detail_items d WHERE d.asin = auctions.asin"
            ") WHERE EXISTS ("
            "  SELECT 1 FROM detail_items d WHERE d.asin = auctions.asin"
            "  AND d.bid_price = 0 AND d.buy_now_price > 0"
            "  AND d.buy_now_price = auctions.latest_bid"
            ")"
        )
        split = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        # 其余拍品的一口价也补上（有详情但 buy_now_price 还空着的）
        conn.execute(
            "UPDATE auctions SET buy_now_price = ("
            "  SELECT d.buy_now_price FROM detail_items d WHERE d.asin = auctions.asin"
            ") WHERE buy_now_price IS NULL AND EXISTS ("
            "  SELECT 1 FROM detail_items d WHERE d.asin = auctions.asin"
            "  AND d.buy_now_price > 0"
            ")"
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
            (_PRICE_SPLIT_FLAG,),
        )
    return split
