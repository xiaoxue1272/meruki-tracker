"""公共渲染小工具：脱敏、价格格式化、面板/行裁剪、状态文案。

所有屏幕（列表/详情/登录/设置）都从这里取基础件；本模块不依赖其它 screens 子模块。

本文件由 ``screens.py`` 按渲染函数族拆出，纯搬运、逻辑未改。
"""

from __future__ import annotations

import math

from rich.cells import cell_len
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from ...core.domain.models import AuctionState, DoorState, UserQuery


def mask_cookie(cookie: str) -> str:
    """脱敏渲染 Cookie（**定长摘要**，不会因真实 Cookie 很长而撑爆屏幕）。

    - 短值（≤4）整体打码：信息量已接近明文；
    - 长值只留前 2 字符 + 长度：``sid=ab***(len=1234)``。
    """
    out: list[str] = []
    for part in cookie.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if not value:
            out.append(f"{name}=(空)")
        elif len(value) <= 4:
            out.append(f"{name}=***")
        else:
            out.append(f"{name}={value[:2]}***(len={len(value)})")
    return "; ".join(out)


def cookie_summary(cookie: str, *, max_items: int = 3) -> tuple[str, str]:
    """Cookie 的定长摘要，用于确认/粘贴屏。

    Returns:
        ``(摘要文本, 说明)``。摘要最多 ``max_items`` 条，保证屏幕高度与
        Cookie 真实长度无关（真实 Cookie 常有上千字符）。
    """
    parts = [p.strip() for p in cookie.split(";") if "=" in p]
    head = "; ".join(mask_cookie(p) for p in parts[:max_items]) or "（空）"
    count = len(parts)
    note = f"共 {count} 条 · {len(cookie)} 字符"
    if count > max_items:
        note += f"（此处只显示前 {max_items} 条）"
    return head, note


def _keys(text: str) -> Text:
    return Text(text, style="reverse")


_REFRESH_PREFIXES = ("刷新中（自动）· ", "刷新中 · ")


def _strip_refresh_prefix(busy: str) -> str:
    """去掉 ``刷新中 · `` 前缀，供遮罩屏使用。

    遮罩屏标题本身就写着「刷新中」，正文再带一遍前缀是重复噪音；
    但列表屏的状态行需要这个前缀（那里没有标题）。
    """
    for prefix in _REFRESH_PREFIXES:
        if busy.startswith(prefix):
            return busy[len(prefix) :]
    return busy


def _status_text(status: str, busy: str) -> str:
    """状态行文案。只有"进行中"才用沙漏，普通状态别乱加图标。"""
    if busy:
        return f"⏳ {busy}"
    return status


def _fit(target: Console, text: Text | str) -> None:
    """单行输出，超宽用省略号（不折行）。"""
    target.print(text, no_wrap=True, overflow="ellipsis", highlight=False)


#: 面板内容行 = ``(内容, 丢弃优先级)``。0 表示永不丢弃，数字越大越先被砍。
PanelLine = tuple[Text | str, int]


def _shrink(lines: list[PanelLine], room: int) -> list[Text | str]:
    """把面板内容压到 ``room`` 行以内：按优先级丢弃，同级从后往前丢。

    为什么需要：prompt_toolkit 的 Window 不滚动，超出的行被静默裁掉。面板
    底部往往是"怎么操作 / 凭证存哪"这类关键信息，先丢的应该是空行和补充
    说明，而不是它们。优先级用整数表达，调用方自己声明谁更可牺牲。
    """
    keep = list(lines)
    top = max((p for _, p in keep), default=0)
    for priority in range(top, 0, -1):
        while len(keep) > room:
            for i in range(len(keep) - 1, -1, -1):
                if keep[i][1] == priority:
                    del keep[i]
                    break
            else:
                break
    return [text for text, _ in keep]


def _panel(
    target: Console,
    lines: list[PanelLine],
    *,
    title: Text | str,
    border_style: str,
    avail: int | None = None,
) -> None:
    """画一个自适应高度的面板。

    先把每行截断到面板内宽（避免折行把行数撑爆），再按优先级砍到放得下。

    Args:
        avail: 这个面板最多能占几行（**含**上下两条边框和末尾空行）。
               省略时按整个终端高度算。
    """
    if avail is None:
        avail = target.height or 24
    # 标题一律包成 Text：Panel 会把 str 标题当 markup 解析，
    # 外部来的字符串里一个 "[/]" 就能抛 MarkupError（真实踩过）。
    if not isinstance(title, Text):
        title = Text(str(title))
    # 2 行边框；若还余得下就再留 1 行空行当呼吸位
    room = avail - 2
    with_tail = room >= 3
    if with_tail:
        room -= 1

    inner = max(1, (target.width or 80) - 4)  # 边框 2 + 左右内边距 2
    clipped: list[PanelLine] = []
    for text, priority in lines:
        t = text if isinstance(text, Text) else Text(str(text))
        if cell_len(t.plain) > inner:
            t = t.copy()
            t.truncate(inner, overflow="ellipsis")
        clipped.append((t, priority))

    body = _shrink(clipped, max(0, room))
    if room < 1 or len(body) > room:
        # 极端小终端：连必显行都放不进面板。画框只会把内容挤出屏幕，
        # 这时去掉边框直接平铺——能选比好看重要。
        for text in body[: max(1, avail)]:
            _fit(target, text)
        return

    target.print(Panel(Group(*body), title=title, border_style=border_style))
    if with_tail:
        target.print()


def price_text(value: float | None) -> str:
    """价格显示：0 和 None 都当"没有"——0 是接口的"无此价"，不是真的 0 円。"""
    if not value:
        return "—"
    # 老库/外部工具可能塞进 nan / inf：照直格式化会显示成「¥nan」「¥inf」，
    # 比留空更像 bug。宁可当"没有价格"。
    if not math.isfinite(value):
        return "—"
    return f"¥{value:,.0f}"


def fixed_price_text(value: float | None) -> str:
    """一口价的显示。与当前价分开列，所以空的时候明确写「—」而不是留白。"""
    return price_text(value)


def _price_style(st: AuctionState) -> str:
    """当前价的样式：最该被一眼看到的数字，没价格时压暗。"""
    if st.door_state is DoorState.ENDED or st.user_query is UserQuery.UNFOLLOWED:
        return "dim"
    return "" if not st.latest_bid else "bold yellow"


def _fixed_price_style(st: AuctionState) -> str:
    """一口价的样式：它是"可以直接买走"的价格，比当前价弱一档。

    用青色而不是黄色，避免和当前价抢注意力——大多数时候用户盯的是当前价，
    只有当一口价接近当前价时（快被人一口买走）才需要警觉。
    """
    if st.door_state is DoorState.ENDED or st.user_query is UserQuery.UNFOLLOWED:
        return "dim"
    return "" if not st.buy_now_price else "cyan"
