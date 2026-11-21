"""显示规则测试——红线所在。

红线：只有 EXACT 且未结束的拍品允许出现倒计时。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, TimeState, UserQuery
from meruki_tracker.core.domain.render import (
    format_remaining,
    marker_style,
    remaining_style,
    render_end_time,
    render_marker,
    render_remaining,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
TZ = ZoneInfo("Asia/Shanghai")
#: 任何"像倒计时"的文本：中文单位，精确到秒，**前导的 0 单位不写**
#: （``9秒`` / ``12分00秒`` / ``5时07分33秒`` / ``2天05时07分33秒``）。
#: 四个互斥分支各对应一种精度；小时的写法里 ``00时`` 只在带天数时允许，
#: 所以旧的 ``0时12分00秒`` 不再算合法倒计时。
COUNTDOWN_RE = re.compile(
    r"^(?:\d+天(?:[1-9]\d*|0\d)时\d{2}分\d{2}秒"
    r"|[1-9]\d*时\d{2}分\d{2}秒"
    r"|\d{2}分\d{2}秒"
    r"|\d+秒)$"
)


def st(
    *,
    user_query: UserQuery = UserQuery.FOLLOWING,
    door_state: DoorState = DoorState.ACTIVE,
    end_time: EndTime | None = None,
    latest_bid: float | None = 1250.0,
    buy_now_price: float | None = None,
) -> AuctionState:
    return AuctionState(
        asin="A1",
        name="测试商品",
        url="https://x/A1",
        user_query=user_query,
        door_state=door_state,
        end_time=end_time or EndTime.pending(),
        latest_bid=latest_bid,
        buy_now_price=buy_now_price,
    )


# ---------------------------------------------------------------- 红线


def test_pending_never_shows_countdown():
    """PENDING 永远只显示"时间待确认"，不含任何数字时间。

    注意：这里**不**做 `@pytest.mark.parametrize("minutes", ...)` 的参数化。
    ``EndTime.__post_init__`` 拒绝 PENDING 携带时间，所以"PENDING + 不同剩余时长"
    这种输入在模型层就不存在——硬造参数只会得到几个完全相同的用例（假参数化）。
    红线由两条互补的断言守护：
    1. ``test_pending_cannot_carry_time``：模型层禁止 PENDING 带时间（输入侧）；
    2. 本用例的精确断言：PENDING 的输出恰好是"时间待确认"，且一个数字都没有（输出侧）。
    """
    out = render_remaining(st(), NOW)
    assert out == "时间待确认"
    assert not re.search(r"\d", out)
    assert not COUNTDOWN_RE.match(out)


@pytest.mark.parametrize(
    "status_text",
    ["时间待确认", "已离开", "已结束", "无法确认", "已到时间"],
)
def test_non_countdown_texts_never_match_countdown_regex(status_text):
    """所有非倒计时状态文案都必须不匹配 COUNTDOWN_RE，这条正则才算真的守住了红线。"""
    assert not COUNTDOWN_RE.match(status_text)


def test_pending_cannot_carry_time():
    """红线第一道闸：PENDING 在模型层就带不了时间，倒计时无从谈起。"""
    with pytest.raises(ValueError, match="PENDING"):
        EndTime(TimeState.PENDING, at=NOW + timedelta(minutes=5))


def test_pending_exact_state_and_at_none():
    """第二道闸：PENDING 的 at 必须是 None，渲染层据此短路。"""
    end = EndTime.pending()
    assert end.state is TimeState.PENDING
    assert end.at is None
    assert render_remaining(st(end_time=end), NOW) == "时间待确认"


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=9), "9秒"),
        (timedelta(minutes=1), "01分00秒"),
        (timedelta(minutes=5, seconds=7), "05分07秒"),
        (timedelta(minutes=59, seconds=59), "59分59秒"),
        (timedelta(hours=1), "1时00分00秒"),
        (timedelta(hours=23, minutes=59, seconds=59), "23时59分59秒"),
        (timedelta(days=1), "1天00时00分00秒"),
        (timedelta(days=7, hours=5, minutes=7, seconds=33), "7天05时07分33秒"),
    ],
)
def test_countdown_matches_regex_only_when_exact(delta, expected):
    """COUNTDOWN_RE 用来反向核实：只有 EXACT 才允许渲染出"像倒计时"的文本。

    这同时让模块里的 COUNTDOWN_RE 真正发挥作用——它断言的是"倒计时长什么样"，
    进而保证上面 PENDING 用例的 ``not COUNTDOWN_RE.match(out)`` 有意义。
    """
    out = render_remaining(st(end_time=EndTime.exact(NOW + delta, NOW)), NOW)
    assert out == expected
    assert COUNTDOWN_RE.match(out), f"{out!r} 应该是合法的倒计时格式"


def test_unfollowed_never_shows_countdown():
    s = st(
        user_query=UserQuery.UNFOLLOWED,
        end_time=EndTime.exact(NOW + timedelta(minutes=12), NOW),
    )
    out = render_remaining(s, NOW)
    assert out == "已离开"
    assert not re.search(r"\d", out)


def test_ended_never_shows_countdown():
    s = st(door_state=DoorState.ENDED, end_time=EndTime.exact(NOW + timedelta(minutes=12), NOW))
    assert render_remaining(s, NOW) == "已结束"


def test_invalid_never_shows_countdown():
    s = st(door_state=DoorState.INVALID, end_time=EndTime.exact(NOW + timedelta(minutes=12), NOW))
    assert render_remaining(s, NOW) == "无法确认"


# ---------------------------------------------------------------- 正向：EXACT 才出现倒计时


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=30), "30秒"),
        (timedelta(minutes=12), "12分00秒"),
        (timedelta(minutes=59, seconds=5), "59分05秒"),
        (timedelta(hours=1), "1时00分00秒"),
        (timedelta(hours=2, minutes=30), "2时30分00秒"),
        (timedelta(days=3), "3天00时00分00秒"),
    ],
)
def test_exact_shows_countdown(delta, expected):
    s = st(end_time=EndTime.exact(NOW + delta, NOW))
    assert render_remaining(s, NOW) == expected


def test_exact_overdue():
    s = st(end_time=EndTime.exact(NOW - timedelta(seconds=5), NOW))
    assert render_remaining(s, NOW) == "已到时间"


# ---------------------------------------------------------------- 其他列


def test_end_time_pending_shows_dash():
    assert render_end_time(st(), TZ) == "—"


def test_end_time_exact_formatted_in_tz():
    at = datetime(2026, 10, 28, 14, 30, 15, tzinfo=UTC)  # 北京 22:30:15
    s = st(end_time=EndTime.exact(at, NOW))
    assert render_end_time(s, TZ) == "2026-10-28 22:30:15"


def test_markers_are_distinct_and_colorless():
    markers = {
        render_marker(st()),
        render_marker(st(end_time=EndTime.exact(NOW + timedelta(hours=1), NOW))),
        render_marker(st(door_state=DoorState.ENDED)),
        render_marker(st(door_state=DoorState.INVALID)),
        render_marker(st(user_query=UserQuery.UNFOLLOWED)),
    }
    assert len(markers) == 5
    assert all(len(m) == 1 for m in markers)


# ---------------------------------------------------------------- 精确到秒 + 醒目


def test_countdown_is_precise_to_the_second():
    """倒计时精确到秒：最后几分钟按秒抢，粗粒度没有决策价值。"""
    s = st(end_time=EndTime.exact(NOW + timedelta(minutes=3, seconds=7), NOW))
    assert render_remaining(s, NOW) == "03分07秒"

    # 同一分钟内，过一秒就要少一秒（不是只显示"3 分钟"）
    later = st(end_time=EndTime.exact(NOW + timedelta(minutes=3, seconds=7), NOW))
    assert render_remaining(later, NOW + timedelta(seconds=1)) == "03分06秒"


def test_countdown_never_shows_negative():
    s = st(end_time=EndTime.exact(NOW - timedelta(seconds=5), NOW))
    assert render_remaining(s, NOW) == "已到时间"
    assert not re.search(r"-\d", render_remaining(s, NOW))


@pytest.mark.parametrize(
    "delta,style",
    [
        (timedelta(days=3), "cyan"),
        (timedelta(hours=5), "bold cyan"),
        (timedelta(minutes=30), "bold yellow"),
        (timedelta(minutes=5), "bold red"),
        (timedelta(seconds=-1), "bold red"),
    ],
)
def test_urgency_colors_get_louder_as_the_deadline_nears(delta, style):
    """越接近结束越刺眼：紧急程度只取决于剩余时间，不依赖终端主题。"""
    s = st(end_time=EndTime.exact(NOW + delta, NOW))
    assert remaining_style(s, NOW) == style


def test_non_countdown_states_are_dimmed_not_colored_as_urgent():
    """非倒计时状态不许用"紧急红"——那会让人以为还有一分钟。"""
    assert remaining_style(st(end_time=EndTime.pending()), NOW) == "yellow"
    assert remaining_style(st(door_state=DoorState.ENDED), NOW) == "dim"
    assert remaining_style(st(user_query=UserQuery.UNFOLLOWED), NOW) == "dim"
    assert remaining_style(st(door_state=DoorState.INVALID), NOW) == "red"
    assert marker_style(st(door_state=DoorState.ENDED)) == "dim"
    # st() 默认是 PENDING（还不知道结束时间），标记是黄色的"待确认"
    assert marker_style(st()) == "yellow"
    assert marker_style(st(end_time=EndTime.exact(NOW + timedelta(hours=1), NOW))) == "green"


def test_countdown_uses_chinese_units():
    """倒计时用中文单位，不用 ``0:12:00`` 那种冒号写法。

    真实反馈：「现在是 X天 XX:XX:XX 你改成 X年X月X日X时X秒」。年月日对倒计时
    没有意义（拍卖都是天级以内），时分秒才是有用的粒度。
    """
    assert format_remaining(12 * 60) == "12分00秒"
    assert format_remaining(3600) == "1时00分00秒"
    assert format_remaining(23 * 3600 + 59 * 60 + 59) == "23时59分59秒"
    assert format_remaining(86400) == "1天00时00分00秒"
    assert format_remaining(7 * 86400 + 5 * 3600 + 7 * 60 + 33) == "7天05时07分33秒"
    # 两位数天数也要完整显示（曾经列宽算少了 1 列，"45天…" 被截断）
    assert format_remaining(45 * 86400 + 59 * 60 + 59) == "45天00时59分59秒"


def test_remaining_width_fits_the_widest_common_countdown():
    """列宽必须装得下最长会出现的倒计时，否则倒计时自己会被截掉。"""
    from rich.cells import cell_len

    from meruki_tracker.core.domain.render import REMAINING_WIDTH

    for seconds in (9, 3600, 24 * 3600 - 1, 86400, 45 * 86400 + 3599, 99 * 86400 + 86399):
        text = format_remaining(seconds)
        assert cell_len(text) <= REMAINING_WIDTH, (
            f"{text!r} 是 {cell_len(text)} 列，超出 {REMAINING_WIDTH}"
        )


def test_countdown_drops_leading_zero_units():
    """前导的 0 单位不写。

    真实反馈：「没有天数时不写，同样的其他单位（时分）没有也不写。」
    """
    assert format_remaining(9) == "9秒"
    assert format_remaining(59) == "59秒"
    assert format_remaining(60) == "01分00秒"
    assert format_remaining(12 * 60 + 5) == "12分05秒"
    assert format_remaining(3600 + 5) == "1时00分05秒"
    assert format_remaining(5 * 3600 + 7 * 60 + 33) == "5时07分33秒"
    # 带天数时，小时位要保留（00时也是信息：它表示"还没到一个更整的小时"）
    assert format_remaining(86400 + 33) == "1天00时00分33秒"


def test_countdown_keeps_inner_zero_units():
    """省掉的只是**前导**零单位；中间的 00 必须留着。

    ``1时00分05秒`` 不能缩成 ``1时5秒``——那样读起来像 1 小时 5 秒，也可能被
    理解成 1:05。
    """
    assert format_remaining(3600 + 5) == "1时00分05秒"
    assert format_remaining(86400 + 5) == "1天00时00分05秒"
