"""状态 -> 用户可见字符串（纯函数）。

红线：只有 EXACT 且未结束的拍品才允许出现倒计时。
PENDING / UNFOLLOWED / ENDED / INVALID 一律不得渲染任何时间数字。

倒计时**精确到秒**并且每秒重绘（``Tui._needs_tick`` 控制节拍）：拍卖最后几分钟
是按秒抢的，"剩 3 分钟"这种粗粒度显示在这里没有决策价值。配色也只依赖状态的
紧急程度，不依赖终端主题。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import AuctionState, DoorState, TimeState, UserQuery

PENDING_TEXT = "时间待确认"
ENDED_TEXT = "已结束"
UNFOLLOWED_TEXT = "已离开"
INVALID_TEXT = "无法确认"
OVERDUE_TEXT = "已到时间"

#: 剩余时间列宽（显示列）。中文单位每个字占 2 列，且「天」后面不留空格，所以：
#: 无天数 ``23时59分59秒`` = 12；一位数天 ``9天23时59分59秒`` = 15；
#: 两位数天 ``99天23时59分59秒`` = 16；三位数天 ``999天23时59分59秒`` = 17。
#: 取 17：三位数的天数也装得下，不用省略号。
REMAINING_WIDTH = 17

#: 快到点了：低于这个秒数用红色（竞拍最后十分钟是决策窗口）。
_URGENT_SECONDS = 600
#: 一个小时内用黄色提醒。
_SOON_SECONDS = 3600


def format_remaining(seconds: float) -> str:
    """剩余秒数 -> 倒计时文本（精确到秒）。

    中文单位，不用冒号，**前导的 0 单位一律不写**：

    - ``2天05时07分33秒`` —— 都在
    - ``5时07分33秒`` —— 不到一天，不写 ``0天``
    - ``12分00秒`` —— 不到一小时，不写 ``0时``
    - ``9秒`` —— 不到一分钟，不写 ``0分``

    省掉的是**前导**的零单位；一旦更小的单位被写出来，中间的 00 就必须留着
    （``1时00分05秒`` 不能缩成 ``1时5秒``，那样读起来像 1 小时 5 秒、也可能被
    理解成 1:05）。天与时之间留一个空格，时/分/秒之间不留——读起来是一串。
    """
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}天{hours:02d}时{minutes:02d}分{secs:02d}秒"
    if hours:
        return f"{hours}时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes:02d}分{secs:02d}秒"
    return f"{secs}秒"


def render_remaining(st: AuctionState, now: datetime) -> str:
    """渲染剩余时间列（倒计时到秒）。"""
    if st.door_state is DoorState.INVALID:
        return INVALID_TEXT
    if st.door_state is DoorState.ENDED:
        return ENDED_TEXT
    if st.user_query is UserQuery.UNFOLLOWED:
        return UNFOLLOWED_TEXT
    if st.end_time.state is TimeState.PENDING or st.end_time.at is None:
        return PENDING_TEXT
    delta = (st.end_time.at - now).total_seconds()
    if delta <= 0:
        return OVERDUE_TEXT
    return format_remaining(delta)


def remaining_style(st: AuctionState, now: datetime) -> str:
    """剩余时间列的样式：越接近结束越刺眼。

    返回 rich 样式串而不是 Text，本模块保持"只产字符串"的纯函数性质，
    也方便测试直接断言（不用去解析 ANSI）。
    """
    if st.door_state is DoorState.INVALID:
        return "red"
    if st.door_state is DoorState.ENDED or st.user_query is UserQuery.UNFOLLOWED:
        return "dim"
    if st.end_time.state is TimeState.PENDING or st.end_time.at is None:
        return "yellow"
    delta = (st.end_time.at - now).total_seconds()
    if delta <= 0:
        return "bold red"
    if delta < _URGENT_SECONDS:
        return "bold red"
    if delta < _SOON_SECONDS:
        return "bold yellow"
    if delta < 86400:
        return "bold cyan"
    return "cyan"


def marker_style(st: AuctionState) -> str:
    """状态标记的配色（字符本身已能区分状态，颜色只是加速扫读）。"""
    if st.door_state is DoorState.ENDED or st.user_query is UserQuery.UNFOLLOWED:
        return "dim"
    if st.door_state is DoorState.INVALID:
        return "red"
    if st.end_time.state is TimeState.PENDING:
        return "yellow"
    return "green"


#: 整个界面统一的时间格式（``YYYY-MM-DD HH:MM:SS``）。
#:
#: 以前各处混着 ``%m-%d %H:%M``、``%H:%M:%S``、``%m-%d %H:%M:%S``，同一个时间
#: 在列表、详情、清理确认里长得不一样，跨年时还看不出是哪一年。统一成一种格式。
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
#: 统一格式渲染出来正好 19 列
TIME_WIDTH = 19


def format_time(dt: datetime, tz: ZoneInfo) -> str:
    """按界面统一格式渲染一个时刻。"""
    return dt.astimezone(tz).strftime(TIME_FORMAT)


def render_end_time(st: AuctionState, tz: ZoneInfo) -> str:
    """渲染结束时间列。PENDING 时不显示任何时间。"""
    if st.end_time.state is TimeState.EXACT and st.end_time.at is not None:
        return format_time(st.end_time.at, tz)
    return "—"


def render_marker(st: AuctionState) -> str:
    """状态标记（不依赖颜色，纯字符终端也可读）。"""
    if st.user_query is UserQuery.UNFOLLOWED:
        return "·"
    match st.door_state:
        case DoorState.ENDED:
            return "■"
        case DoorState.INVALID:
            return "?"
    if st.end_time.state is TimeState.PENDING:
        return "◐"
    return "●"
