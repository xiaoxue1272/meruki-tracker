"""详情屏、纯文本详情、清理确认屏的渲染。

本文件由 ``screens.py`` 按渲染函数族拆出，纯搬运、逻辑未改。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from rich.cells import cell_len
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ...core.adapters.store.snapshot import Snapshot
from ...core.domain.models import AuctionState, DoorState, UserQuery
from ...core.domain.render import (
    format_time,
    marker_style,
    remaining_style,
    render_end_time,
    render_marker,
    render_remaining,
)
from ..views import LABELS, ListView
from .common import (
    PanelLine,
    _fit,
    _fixed_price_style,
    _panel,
    _price_style,
    fixed_price_text,
    price_text,
)

# 状态枚举 -> 中文（详情屏不该把内部枚举名直接怼给用户）
_USER_TEXT = {UserQuery.FOLLOWING: "关注中", UserQuery.UNFOLLOWED: "已离开列表"}
_DOOR_TEXT = {DoorState.ACTIVE: "进行中", DoorState.ENDED: "已结束", DoorState.INVALID: "无法确认"}


def wrap_plain(text: str, *, width: int) -> list[str]:
    """把纯文本按宽度折成显示行。

    用 rich 自己的 ``Text.wrap`` 而不是 ``ceil(cell_len / width)`` 估算：中文没
    空格，估算和实际差一两行是常态（渲染审计踩过，``cell_len // inner`` 在
    40~80 列下低估了 8 处）。这里只要行数正确，不关心样式。
    """
    if not text:
        return []
    probe = Console(width=max(1, width), force_terminal=False, no_color=True, legacy_windows=False)
    return [line.plain for line in Text(text).wrap(probe, max(1, width))]


def _yes_no(flag: bool | None, *, unknown: str = "—") -> Text:
    """把详情接口的 ``会``/``不会`` 布尔画成中文。

    ``None`` 走 ``unknown``：拦也拦不住"还没拉过详情"这种情况，但**不能**把
    它画成"不会"——那会让人以为拍品真不支持提前结束。
    """
    if flag is None:
        return Text(unknown, style="dim")
    if flag:
        return Text("会", style="bold")
    return Text("不会", style="dim")


def render_detail_text(
    console: Console,
    *,
    st: AuctionState,
    text: str,
    translated: bool,
    scroll: int,
    busy: str = "",
) -> None:
    """详情里的"商品描述"全文屏（可滚动）。

    描述动辄上千字，塞进详情屏会把价格那些真正要看的信息挤没，所以单开一屏。
    ``scroll`` 是按**显示行**算的偏移量，不是字符偏移——折行之后一个逻辑行
    可能占好几行，只有按显示行滚才不会跳。
    """
    height = console.height or 24
    width = console.width or 80

    # 行预算必须**先算再画**：极矮终端（3~4 行）里"标题 + 忙碌行 + 正文 + 提示"
    # 会一起超出去。以前这里 `room = max(1, height - 3)` 强行给正文留 1 行，
    # 于是 height=3 时渲染出 4 行、页脚被顶掉。
    head = "描述 · 译文" if translated else "描述"
    show_busy = bool(busy) and height >= 3
    fixed = 1 + (1 if show_busy else 0)
    avail = height - fixed

    if show_busy:
        _fit(console, Text(f"⏳ {busy}", style="yellow"))
    _fit(console, Text.assemble((head, "bold"), ("   ", ""), (st.name, "dim")))

    lines = wrap_plain(text, width=width)
    if not lines:
        if avail >= 1:
            _fit(console, Text("（这个拍品没有描述）", style="dim"))
        return
    if avail < 1:
        # 连一行正文都放不下：宁可只显示标题，也不要把页脚挤掉（那是唯一的退路）
        return

    total = len(lines)
    # 提示行只在"放得下正文 + 提示"时才有；放不下就优先给正文，别为了提示
    # 把正文砍到 0 行。
    want_hint = total > avail
    show_hint = want_hint and avail >= 2
    room = avail - 1 if show_hint else avail
    room = max(1, min(room, avail))

    start = max(0, min(scroll, max(0, total - room)))
    for line in lines[start : start + room]:
        _fit(console, Text(line))

    if show_hint:
        below = total - (start + room)
        hint = f"{start + 1}-{start + room}/{total} 行"
        # 上下都要提：滚到中间时只说"下面还有"会让用户以为上面是开头。
        marks = []
        if start > 0:
            marks.append(f"↑ {start}")
        if below > 0:
            marks.append(f"↓ {below}")
        if marks:
            hint = " · ".join([*marks, hint])
        _fit(console, Text(hint, style="dim"))


def render_clean_confirm(
    console: Console,
    *,
    view: ListView,
    targets: list[AuctionState],
    tz: ZoneInfo,
    notice: tuple[str, ...] = (),
) -> None:
    """清理确认。

    **删除是不可逆的**，所以先把"删哪些、删多少"完整摆出来再问一次；
    同时写清"只删本地记录，不影响网站上的关注"——否则用户会以为清理
    等于批量取关。
    """
    label = LABELS[view]
    avail = console.height or 24
    room_lines = max(1, avail - 3)  # 上下边框 + 呼吸空行

    # 信息层级：标题（删什么、多少）> 「只删本地记录」这条关键说明 > 条目 >
    # 第二条说明 > 空行。宁可少列几个拍品，也要留住"这不等于取关"。
    base: list[PanelLine] = [
        (Text.assemble(("清理 ", "bold"), (f"{len(targets)} 个{label}的拍品", "bold red")), 0),
        ("", 2),
        (Text("只删本地记录，不会取消挖煤姬上的关注，也不影响对方的订单", style="dim"), 1),
        (Text("观测历史会保留，以后重新关注还能看到过去的记录", style="dim"), 2),
    ]
    if view is ListView.INVALID:
        # 「无法确认」不是终态：清理后只要拍品还在「进行中」列表里，
        # 下轮刷新会重新发现它、详情从零重试。不能让人以为"删了就没了"。
        base.append(
            (
                Text(
                    "这些拍品还在永久重试：若仍被关注，下轮刷新会重新出现并从零重试；"
                    "清理只清掉当前的失败记录",
                    style="yellow",
                ),
                1,
            )
        )

    tail: list[PanelLine] = []
    if notice:
        tail.append(("", 2))
        tail.extend((Text(n, style="yellow"), 0) for n in notice[:2])

    items: list[PanelLine] = []
    for st in targets:
        when = ""
        if st.end_time.at is not None:
            when = format_time(st.end_time.at, tz)
        elif st.unfollowed_at is not None:
            when = format_time(st.unfollowed_at, tz)
        line = Text.assemble((render_marker(st), "dim"), (" ", ""))
        line.append(st.name or st.asin)
        if when:
            line.append(f"  {when}", style="dim")
        items.append((line, 0))

    # 条目区能占几行（-1 是条目前的空行）
    spare = room_lines - len(base) - len(tail) - 1
    body = [*base, *tail]
    if items and spare >= 2:
        shown = items if len(items) <= spare else items[: spare - 1]
        block: list[PanelLine] = [("", 2), *shown]
        hidden = len(items) - len(shown)
        if hidden:
            block.append((Text(f"… 还有 {hidden} 个", style="dim"), 0))
        body = [*base, *block, *tail]
    elif items and room_lines - len(base) - len(tail) >= 1:
        # 一行条目都放不下：至少说清"具体哪几个没列出来"，别让人以为是全部。
        body = [
            *base,
            (Text(f"… {len(items)} 个拍品列不下（窗口太小），确认后仍会全部清理", style="dim"), 0),
            *tail,
        ]
    _panel(console, body, title=f"清理确认 · {label}", border_style="red")


#: 详情屏标签列宽（显示列）。取最长标签「结束时间 / 时间确认 / 详情失败」= 8 列 + 2 空隙
_LABEL_COLS = 10


def detail_room_for_observations(height: int) -> int:
    """详情屏能放几条观测（其余固定内容先占位）。"""
    return max(0, min(5, height - 15))


def render_detail(
    console: Console,
    *,
    st: AuctionState,
    snapshot: Snapshot,
    observations: list[tuple[str, str, str]],
    tz: ZoneInfo,
    notice: tuple[str, ...] = (),
    desc: str = "",
    desc_translated: bool = False,
    title: str = "",
    auto_extend: bool | None = None,
    early_termination: bool | None = None,
) -> None:
    """单个拍品详情屏（整屏，不追加）。

    ``notice`` 用来提示"后台刷新已完成"这类一次性信息——用户不在列表屏时
    不该被强行拉回去，但得知道数据已经更新了。

    ``title`` 是标题（有译文时传译文，否则留空用 ``st.name``）——日文标题在
    中文界面里同样看不懂，翻译了描述却不翻标题只算做了一半。

    ``desc`` 是商品描述（已有译文时传译文，``desc_translated`` 标明是哪种），
    只显示前几行——描述动辄上千字，全塞进来会把价格这些真正要看的信息挤没，
    全文按 ``v`` 单开一屏。

    ``auto_extend`` / ``early_termination`` 是拍品的**规则属性**，来自详情接口
    的 ``autoExtend`` / ``other.earlyTermination``（原始值 ``会`` / ``不会``）：

    - ``提前结束 = 会``：卖家**可以**在原定结束前直接结束并卖给别人，等于
      你有随时被截胡的风险——这是要立刻决定的信号，不是花边。
    - ``自动延长 = 会``：结束前五分钟有人抬价就再延五分钟（可重复），等于
      结束时间可能一推再推。

    ``None`` = 还没拉过详情 / 响应没这两个字段，**不显示**（不能把"不知道"
    画成"不会"）。注意这两个都**不是事件**：实测 41 条详情里 37 条
    ``earlyTermination=会``、34 条 ``autoExtend=会``，其中 17 条还在进行中——
    按"为真就报警"会天天误报。
    """
    now = datetime.now(UTC)
    height = console.height or 24
    rows_room = detail_room_for_observations(height)

    def row(label: str, value: Text | str) -> Text:
        # 按**显示列**补齐，不能用 f"{label:<7}"：中文标签 2 字符占 4 列，
        # 用字符数补齐会让「结束时间」这行的值比其他行右移 2 列。
        pad = max(0, _LABEL_COLS - cell_len(label))
        return Text.assemble((label + " " * pad, "dim"), value)

    # 顺序按"决策要用到的先后"排：还剩多久、什么时候结束、现在多少钱，才是进这屏
    # 要看的东西；拍品号和链接是抄送信息，放最后。倒计时加粗+按紧急度换色。
    #
    # 优先级（_panel 按它丢行）：0 = 永不丢。剩余/结束时间/当前价/状态是这屏的
    # 存在理由；拍品号、链接、各种时间戳是抄送信息，矮终端下先牺牲它们。
    lines: list[PanelLine] = [
        (Text(title or st.name, style="bold"), 0),
        ("", 1),
        (row("剩余", Text(render_remaining(st, now), style=remaining_style(st, now))), 0),
        (row("结束时间", Text(render_end_time(st, tz), style="bold")), 0),
        (row("当前价", Text(price_text(st.latest_bid), style=_price_style(st) or "")), 0),
        (
            row(
                "一口价",
                Text(
                    fixed_price_text(st.buy_now_price),
                    style=_fixed_price_style(st) or "",
                ),
            ),
            2,
        ),
        (
            row(
                "状态",
                Text(
                    f"{_USER_TEXT.get(st.user_query, st.user_query.value)} · "
                    f"{_DOOR_TEXT.get(st.door_state, st.door_state.value)}",
                    style=marker_style(st),
                ),
            ),
            0,
        ),
    ]

    # 规则属性：不是事件，是"这拍品可能随时结束 / 会被延长"的底牌。
    # 只在真的有值时才占一行——没拉过详情就画一行"提前结束 —"是纯噪音，
    # 而且"不知道"和"不会"在屏幕上长得太像，容易被当成后者。
    # 优先级 2：比价格次要，矮终端先丢；正常终端一定看得到。
    if early_termination is not None:
        # 标签必须 <= 4 个汉字（8 列）：_LABEL_COLS 是 10，5 个汉字正好占满，
        # 值会紧贴标签，和别的行对不齐（真机 100x34 抓到过）。
        lines.append((row("提前结束", _yes_no(early_termination)), 2))
    if auto_extend is not None:
        lines.append((row("自动延长", _yes_no(auto_extend)), 2))

    # 抄送信息放最后：拍品号和链接是用来复制的，不是用来决策的。
    lines.append((row("拍品号", st.asin), 3))
    lines.append((row("链接", st.url), 3))

    # 描述：只放前几行预览。它是"卖家怎么说的"，能帮判断成色和坑，但比价格
    # 次要——矮终端下先丢它（优先级 2），价格和倒计时永不丢。
    if desc:
        preview = wrap_plain(desc, width=max(10, (console.width or 80) - _LABEL_COLS))
        shown = preview[:3]
        # 「描述（译）」正好占满 10 列标签宽度，会贴住正文；缩一档留个空格
        label = "描述·译" if desc_translated else "描述"
        lines.append((row(label, Text(shown[0] if shown else "", style="")), 2))
        for extra in shown[1:]:
            lines.append((Text(" " * _LABEL_COLS + extra), 2))
        if len(preview) > len(shown):
            hint = f"…共 {len(preview)} 行，按 v 看全文"
            lines.append((Text(" " * _LABEL_COLS + hint, style="dim"), 2))

    # 只有真的失败过才提，0 次属于噪音
    if st.detail_failures:
        lines.append(
            (
                row(
                    "详情失败",
                    Text(f"{st.detail_failures} 次（已用列表数据兜底）", style="bold red"),
                ),
                2,
            )
        )
    if snapshot.generated_at:
        lines.append((row("数据时间", format_time(snapshot.generated_at, tz)), 2))
    if st.end_time.confirmed_at:
        lines.append((row("时间确认", f"于 {format_time(st.end_time.confirmed_at, tz)}"), 3))
    if st.unfollowed_at:
        lines.append((row("取关于", format_time(st.unfollowed_at, tz)), 3))
    if st.concluded_at:
        lines.append((row("结束于", format_time(st.concluded_at, tz)), 3))

    # 提示语：一行，放在面板之后（它比抄送信息重要）。
    tail = [t for t in (Text(n, style="yellow") for n in notice[:1])]
    tail_rows = len(tail)

    # 观测表能占几行，取决于**面板实际占了多少**。预算必须精确：prompt_toolkit
    # 的 Window 不滚动，多算一行就把页脚顶掉、少算一行就白丢观测。以前这里完全
    # 没预算，40x12 的终端渲染出 21 行，观测表整块消失（含"还有更多"的提示）。
    #
    # _panel 会把每行截断（所以"每条 = 1 行"成立），再按优先级砍到放得下。
    # 这里照它的规则先算出它会占多少行，剩下的才给观测表。
    min_obs = 4 if observations else 0  # 标题 + 表头 + 至少 1 行 + "还有 N 条"
    # 面板本身不许超过终端高度：极矮终端（3~4 行）下把 avail 交给 _panel 去
    # 处理，它会在 room < 1 时退化成"去边框平铺"，比硬画一个放不下的框强。
    panel_avail = max(1, min(height - tail_rows, height - tail_rows - min_obs))
    room_in_panel = panel_avail - 2  # 上下边框
    if room_in_panel >= 3:
        room_in_panel -= 1  # _panel 的末尾呼吸空行
    panel_rows = 2 + min(len(lines), max(0, room_in_panel)) + (1 if panel_avail - 2 >= 3 else 0)

    _panel(console, lines, title="拍品详情", border_style="blue", avail=panel_avail)
    for text in tail:
        _fit(console, text)

    # 观测表：标题 1 + 表头 1 + N 行（+ 截断提示 1）
    budget = max(0, height - panel_rows - tail_rows - 2)
    room = max(0, min(rows_room, budget))
    if observations and room > 0 and len(observations) > room:
        room = max(1, room - 1)  # 挤一行出来放"另有 N 条"
    elif observations and budget <= 0:
        room = 0
    if observations and room > 0:
        shown = observations[:room]
        table = Table(title="最近观测", show_header=True, header_style="bold", box=None)
        # 时间和来源都**禁止折行**：30 列宽的终端下「2026-10-29 12:00:00」会被折成
        # 两行，一条观测悄悄吃掉 2 行高度，预算全部失效（真实踩过）。宁可截断。
        table.add_column("时间", no_wrap=True)
        table.add_column("来源", no_wrap=True)
        table.add_column("原始值", no_wrap=True, overflow="ellipsis")
        for ts, kind, raw in shown:
            table.add_row(Text(_local_time(ts, tz)), Text(kind), Text(raw[:30]))
        console.print(table)
        if len(observations) > len(shown):
            _fit(
                console,
                Text(f"另有 {len(observations) - len(shown)} 条更早的观测未显示", style="dim"),
            )
    elif observations:
        # 只有还放得下一行才提示，否则这行本身就会把页脚顶掉
        if panel_rows + tail_rows < height:
            _fit(console, Text("终端太矮，观测记录未显示（放大窗口可看）", style="dim"))
    else:
        if panel_rows + tail_rows < height:
            _fit(console, Text("这个拍品还没有观测记录", style="dim"))


def _local_time(raw: str, tz: ZoneInfo) -> str:
    """观测时间是 UTC ISO 串；显示前转用户时区，避免与同屏时间差 8 小时。"""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw[:19]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return format_time(dt, tz)
