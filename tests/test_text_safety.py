"""远端的文字要当**数据**：拍品名 / 描述里带的控制字符不能直接送进终端。

拍品名是卖家自己填的。一个改名成 ``\\x1b[2J`` 的标题就能让整个 TUI 清屏，
``\\x1b]0;xxx\\x07`` 能把终端标题改掉——终端把 ESC 之后的内容当指令执行，而不是
当文字显示。这类"看起来只是显示问题"的注入比崩溃更难查：程序不报错、状态也
没错，只是屏幕上少了一块。

所以规则是：**控制字符在进入显示层之前就抹掉**。换行和制表符保留（描述是多行
的），其余 C0、DEL、C1 一律不留。
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from rich.console import Console

from meruki_tracker.core.domain.goods import description_of
from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery
from meruki_tracker.core.domain.text import scrub
from meruki_tracker.tui.screens import render_list
from meruki_tracker.tui.views import ListView

NASTY = "拍品\x1b[2J\x1b]0;pwned\x07\x08名"


def _state(name: str) -> AuctionState:
    now = datetime.now(UTC)
    return AuctionState(
        asin="A1",
        name=name,
        url="https://x/A1",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(now + timedelta(hours=2), now),
        latest_bid=100.0,
    )


def test_scrub_drops_terminal_control_characters():
    assert scrub("清屏\x1b[2J保留") == "清屏[2J保留"
    assert scrub("标题\x1b]0;pwned\x07尾") == "标题]0;pwned尾"
    assert scrub("退格\x08\x7f尾") == "退格尾"
    assert scrub("\x9b控制") == "控制"


def test_scrub_keeps_the_whitespace_a_description_needs():
    assert scrub("第一行\n第二行\t缩进") == "第一行\n第二行\t缩进"


def test_auction_name_cannot_carry_escape_sequences():
    st = _state(NASTY)
    assert "\x1b" not in st.name
    assert "\x07" not in st.name
    assert "拍品" in st.name and "名" in st.name


def test_description_cannot_carry_escape_sequences():
    text = description_of({"other": {"descText": "描述\x1b[2J文字\x07"}})
    assert "\x1b" not in text
    assert "描述" in text and "文字" in text


def test_list_screen_never_emits_remote_control_sequences():
    """整条链走一遍：远端名字 → 模型 → rich → 屏幕字节。"""
    st = _state(NASTY)
    buf = io.StringIO()
    con = Console(file=buf, width=80, height=24, force_terminal=True, color_system="truecolor")
    snap = type(
        "S",
        (),
        {
            "auctions": (st,),
            "generated_at": datetime.now(UTC),
            "last_attempt": None,
            "account": None,
        },
    )()
    render_list(
        con,
        snap=snap,
        rows=[st],
        cursor=0,
        page=0,
        page_size=10,
        tz=None,
        stale_minutes=60,
        view=ListView.ALL,
        status="",
        notice=(),
        busy="",
    )
    out = buf.getvalue()
    assert "\x1b[2J" not in out, "列表屏把远端的清屏序列原样送到了终端"
    assert "\x1b]0;" not in out, "列表屏把远端的改标题序列原样送到了终端"
