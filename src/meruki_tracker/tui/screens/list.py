"""列表屏、视图栏、页脚与刷新遮罩的渲染。

本文件由 ``screens.py`` 按渲染函数族拆出，纯搬运、逻辑未改。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from rich.cells import cell_len
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.adapters.store.snapshot import Snapshot
from ...core.domain.models import AuctionState, DoorState, UserQuery
from ...core.domain.render import (
    REMAINING_WIDTH,
    TIME_WIDTH,
    format_time,
    marker_style,
    remaining_style,
    render_end_time,
    render_marker,
    render_remaining,
)
from ..views import LABELS, ORDER, ListView
from .common import (
    _fit,
    _fixed_price_style,
    _price_style,
    _status_text,
    _strip_refresh_prefix,
    fixed_price_text,
    price_text,
)


def footer_text(
    screen: str,
    *,
    page: int = 0,
    page_count: int = 1,
    reveal: bool = False,
    can_resend: bool = False,
    busy: str = "",
    can_clean: bool = False,
    translate_on: bool = False,
    edit_units: bool = False,
    width: int = 0,
) -> str:
    """页脚按键提示。**固定显示在屏幕最后一行**，不随内容滚走。

    Args:
        can_clean: 当前视图能不能清理（只有终态视图能）。
        translate_on: 译文是否已开（决定 ``t`` 显示"翻译"还是"原文"）。
        edit_units: 正在编辑的选项有没有可切换的单位（有才提示 ``Tab 切单位``）。
        width: 可用宽度；传入时按键提示会按优先级裁剪，保证 ``q 退出``
            这类"总得看一眼"的提示不会被截断到看不见。
    """
    if screen == "list":
        if busy:
            # 遮罩期间列表按键都失效，页脚不该再显示它们
            return "刷新中… · q 退出"
        return _list_footer(
            page=page,
            page_count=page_count,
            can_clean=can_clean,
            translate_label="t 原文" if translate_on else "t 翻译",
            width=width,
        )
    # 其余屏的提示也按 ``(文字, 需求宽度)`` 组织：宽度不够时先丢"锦上添花"
    # 的项（need 大），必要时丢中间项，**首尾一律保留**——首是"怎么操作"，
    # 尾是"怎么退出"，被 ellipsis 砍掉尾巴比少了某个快捷键难受得多。
    if screen == "clean-confirm":
        return _degrade([("Enter 确认清理", 0), ("Esc 取消", 0)], width=width, fallback="Esc 取消")
    # 配置页按 ``s`` 从列表进来，**q 和 Esc 走的是"退回列表"而不是"退出程序"**，
    # 所以这里一律写"返回列表"。以前写"q 退出"，用户按下去发现没退，很困惑。
    if screen == "settings":
        return _degrade(
            [
                ("↑↓ 选择", 0),
                ("Enter 修改", 2),
                ("Tab 切分组", 3),
                ("Esc / q 返回列表", 0),
            ],
            width=width,
            fallback="q 返回列表",
        )
    if screen == "setting-edit":
        parts = [("Enter 保存", 0), ("Backspace 删除", 4)]
        if edit_units:
            parts.append(("Tab 切单位", 3))
        parts.append(("Esc 取消", 0))
        return _degrade(parts, width=width, fallback="Esc 取消")
    if screen == "point-edit":
        # 逐条增删：a 加一个、x 删一个，Enter 改选中那条。不用"保存"——每步
        # 都立刻落库了（和列表页的刷新一样，改完就是改完了）。
        parts = [("↑↓ 选择", 0), ("Enter 改", 2), ("a 加一个", 2), ("x 删一个", 2)]
        if edit_units:
            parts.append(("Tab 切单位", 3))
        parts.append(("Esc 返回", 0))
        return _degrade(parts, width=width, fallback="Esc 返回")
    if screen == "detail":
        # 详情屏的 r 只刷新当前这一条，和列表的 r（整桶刷新）不是一回事，写清楚。
        return _degrade(
            [
                ("r 刷新这一条", 0),
                ("v 看描述", 3),
                ("t 原文" if translate_on else "t 翻译", 3),
                ("Esc / q 返回列表", 0),
            ],
            width=width,
            fallback="q 返回列表",
        )
    if screen == "mount":
        return _degrade(
            [
                ("r 重新取码", 1),
                ("Esc / q 返回", 0),
            ],
            width=width,
            fallback="q 返回",
        )
    if screen == "detail-desc":
        return _degrade(
            [("↑↓ 滚动", 0), ("PgUp/PgDn 翻页", 3), ("Esc / q 返回详情", 0)],
            width=width,
            fallback="q 返回详情",
        )
    if screen == "login-method":
        return _degrade(
            [("按 1 / 2 / 3 选择", 0), ("Esc 取消", 0)], width=width, fallback="Esc 取消"
        )
    if screen == "login-paste":
        return _degrade(
            [
                ("Enter 提交", 0),
                ("Backspace 删除一个字符", 4),
                ("Esc 取消", 0),
            ],
            width=width,
            fallback="Esc 取消",
        )
    if screen == "login-sms-mobile":
        return _degrade(
            [("Enter 发送验证码", 0), ("Backspace 删除", 4), ("Esc 返回", 0)],
            width=width,
            fallback="Esc 返回",
        )
    if screen == "login-sms-code":
        parts = [("Enter 登录", 0), ("Backspace 删除", 4), ("Esc 返回", 0)]
        if can_resend:
            parts.append(("r 重新发送验证码", 3))
        return _degrade(parts, width=width, fallback="Esc 返回")
    if screen == "login-wait":
        return _degrade(
            [("Esc 取消等待", 0), ("Ctrl+C 退出程序", 1)], width=width, fallback="Esc 取消等待"
        )
    if screen == "cookie-confirm":
        return _degrade(
            [
                ("Enter 登录", 0),
                (f"v {'隐藏' if reveal else '明文显示'}", 3),
                ("Esc 取消", 0),
            ],
            width=width,
            fallback="Esc 取消",
        )
    return ""


def _degrade(parts: list[tuple[str, int]], *, width: int, fallback: str) -> str:
    """把 ``(文字, 丢弃次序)`` 拼成一行，宽度不够就按次序砍。

    第二项是**丢弃次序**：0 表示必显，数字越大越先丢。裁剪顺序：
    1. 不传 ``width`` 或本来就放得下 → 全上；
    2. 一次丢一个丢弃次序最大的，丢掉后能放下就停（一口气把非必显的全过滤掉
       会把靠后的必显项也一起砍掉）；
    3. 还是放不下 → 从中间往两边丢，**首尾保住**；
    4. 连首尾都放不下 → 用 ``fallback``（通常是"q 返回列表"这种最短的退出提示），
       真放不下再按显示宽度截断。
    """
    sep = " · "

    def render(items: list[tuple[str, int]]) -> str:
        return sep.join(text for text, _ in items)

    kept = list(parts)
    if not width or cell_len(render(kept)) <= width:
        return render(kept)
    # 一次丢一个"最不要紧"的（need 最大的），丢掉后能放下就停——一口气按
    # need 过滤会把本该留住的项也一起砍掉。
    while cell_len(render(kept)) > width:
        droppable = [(i, need) for i, (_, need) in enumerate(kept) if need > 0]
        if not droppable:
            break
        worst = max(need for _, need in droppable)
        index = max(i for i, need in droppable if need == worst)
        kept.pop(index)
    out = render(kept)
    if cell_len(out) <= width:
        return out
    # 还是放不下：从中间丢，首尾保住（首=怎么操作，尾=怎么退出）
    while len(kept) > 2 and cell_len(render(kept)) > width:
        kept.pop(len(kept) // 2)
    out = render(kept)
    return out if cell_len(out) <= width else _clip(fallback, width)


def _clip(text: str, width: int) -> str:
    """按**显示宽度**截断，末尾补省略号。汉字占两格，``len()`` 会算少。"""
    if width <= 0 or cell_len(text) <= width:
        return text
    kept = ""
    used = 0
    for ch in text:
        w = cell_len(ch)
        if used + w > width - 1:
            break
        kept += ch
        used += w
    return kept + "…"


def _list_footer(
    *,
    page: int,
    page_count: int,
    can_clean: bool,
    translate_label: str = "t 翻译",
    width: int,
) -> str:
    """列表页脚：三档宽度降级，**永远保住 ``q 退出``**。

    为什么需要降级而不是截断：rich 的 ellipsis 会砍掉行尾——而"怎么退出"
    恰好排在最后一个。宁可少显示 ``s 设置`` 这类低频键、甚至把 ``Enter 详情``
    缩成 ``Enter``，也不能让用户找不到退出方式。

    Args:
        page_count: 只有一页时**不显示翻页提示**——"翻页 1/1"是纯噪音，
            还占着页脚最紧张的位置。
        width: 可用宽度（显示列数）。0 表示不限制。
    """
    sep = " · "
    # 翻页只有 n / p：左箭头让给了"切视图"，页数又常常只有一页。
    paging = f"n/p 翻页 {page + 1}/{page_count}"

    # 完整档：need 是"至少这么宽才值得显示"，0 表示必显
    full: list[tuple[str, int]] = [("↑↓ 选择", 0)]
    if page_count > 1:
        full.append((paging, 0))
    # Tab 切列表四个视图；s 是进配置页的**唯一**入口（外层大分页已拆掉），
    # 两个都给 need=0 必显。need 同时是"超宽先丢谁"的排序，给高值反而
    # 最先被丢掉（踩过）。
    full += [
        ("Tab 切视图", 0),
        ("Enter 详情", 0),
        ("r 刷新", 54),
        (translate_label, 70),
        # x 清理给 need=0（必显）：它是「已结束」视图**独有**的操作，别的视图根本
        # 没有，丢了就再也找不到。而 need 同时是"超宽时先丢谁"的排序依据——给
        # 80 反而会最先被丢，语义正好反了。
        ("x 清理", 0),
        ("s 设置", 0),
        ("q 退出", 0),
    ]
    if not can_clean:
        full = [item for item in full if item[0] != "x 清理"]

    def join(items: list[tuple[str, int]]) -> str:
        return sep.join(text for text, _ in items)

    if not width:
        return join(full)

    kept = [(text, need) for text, need in full if need == 0 or width >= need]
    # 还超宽就按 need 从大到小丢（越靠后越不重要）
    while cell_len(join(kept)) > width:
        droppable = [(i, need) for i, (_, need) in enumerate(kept) if need > 0]
        if not droppable:
            break
        worst = max(droppable, key=lambda x: x[1])[0]
        kept.pop(worst)
    text = join(kept)
    if cell_len(text) <= width:
        return text

    # 逐档降级：从信息多到少试，第一个放得下的就用。
    # ``Tab 切视图``、``s 设置``、``q 退出`` 是三个"丢了就再也回不来"的键：
    # 前两个各自是进另一个视图 / 进配置页的唯一入口，后者是唯一退出方式。
    # 真实踩过：rich 的 ellipsis 会砍行尾，而 ``q 退出`` 恰在最后，窄屏会连
    # 退出路径都看不到——所以这里主动降级，而不是让渲染去截。
    paged = [("↑↓ 选择", 0)]
    if page_count > 1:
        paged.append((paging, 0))
    tiers: list[list[tuple[str, int]]] = [
        [*paged, ("Tab 切视图", 0), ("s 设置", 0), ("Enter 详情", 0), ("q 退出", 0)],
        [*paged, ("Tab 切视图", 0), ("s 设置", 0), ("q 退出", 0)],
        [("↑↓ 选择", 0), ("Tab 切视图", 0), ("s 设置", 0), ("q 退出", 0)],
        [("Tab 切视图", 0), ("s 设置", 0), ("q 退出", 0)],
        [("↑↓", 0), ("Tab", 0), ("s 设置", 0), ("q 退出", 0)],
        [("s 设置", 0), ("Enter", 0), ("q 退出", 0)],
        [("s 设置", 0), ("q 退出", 0)],
        [("↑↓", 0), ("q 退出", 0)],
        [("q 退出", 0)],
        [("q", 0)],
    ]
    for candidate in tiers:
        text = join(candidate)
        if cell_len(text) <= width:
            return text
    return "q"


def _empty_hint(view: ListView, snap: Snapshot, view_counts: dict[ListView, int]) -> str:
    """空列表提示。**"这个视图空"不等于"没数据"**——别说错话。"""
    if view is not ListView.ALL and view_counts.get(ListView.ALL):
        others = [LABELS[v] for v in ORDER if v is not view and view_counts.get(v)]
        where = "、".join(others) if others else "其他视图"
        return f"{LABELS[view]}视图里没有拍品；按 Tab 切到{where}"
    if snap.last_attempt.at is None:
        return "还没有采集过数据，按 r 拉取关注列表"
    return "关注列表为空；在挖煤姬里关注拍品后按 r 刷新"


def list_fixed_height(*, status: str, busy: str, notice: tuple[str, ...]) -> int:
    """列表屏除数据行以外的固定行数（标题 + 视图栏 + 新鲜度 + 表头 + 状态/提示）。

    不含页脚：页脚由 Tui 固定在屏幕最后一行，那一行从可用高度里扣。
    """
    lines = 4  # 标题 + 视图切换栏 + 新鲜度 + 表头
    if status or busy:
        lines += 1
    lines += min(3, len(notice)) + (1 if len(notice) > 3 else 0)
    return lines


def view_bar(
    current: ListView,
    view_counts: dict[ListView, int],
    *,
    compact: bool = False,
) -> Text:
    """列表顶部的视图切换栏：``关注中 3 · 已结束 12 · 无法确认 1 · 全部 16``。

    选中项高亮、未选中项压暗，但**每一项都带计数**——数量是用户决定要不要
    切过去的唯一依据，藏起来等于没有。窄屏（compact）只留选中视图，
    避免这一行被截断成无意义的片段。
    """
    if compact:
        return Text.assemble(
            ("◧", "dim"),
            (f" {LABELS[current]} {view_counts.get(current, 0)} ", "reverse"),
        )
    # 每一项都同样加空格、同样不加粗：选中只靠反显。加粗/压暗会让选中项看着
    # 比别的大一圈，切视图时整行高度感也跟着变。
    out = Text()
    for i, view in enumerate(ORDER):
        if i:
            out.append("·", style="dim")
        label = LABELS[view]
        count = view_counts.get(view, 0)
        out.append(f" {label} {count} ", style="reverse" if view is current else "")
    return out


def render_refreshing(console: Console, *, busy: str, page: int = 0, page_count: int = 1) -> None:
    """刷新遮罩：刷新期间用整屏进度代替列表。

    以前刷新只是底部多一行 ⏳，列表照常显示——用户会盯着旧数据看不出在跑，
    还可能翻页/进详情拿到一半新一半旧的状态。刷新期间直接把列表遮掉，
    语义上更准确：**此刻数据正在变，先别读**。
    """
    height = max(1, console.height or 24)
    # 面板内可用宽度：边框 2 + 左右内边距 4（padding=(0, 2)）→ 少 6 列。
    # 按 -4 估算会让长进度文本在面板里折行，行数预算当场失效（真实踩过）。
    inner = max(1, (console.width or 80) - 6)

    def line(text: Text | str) -> Text:
        """截断到面板内宽。**只有 truncate 能真正阻止折行**——no_wrap 在
        Panel 内部不生效，折行多出来的行会把页脚顶掉。"""
        t = text if isinstance(text, Text) else Text(str(text))
        if cell_len(t.plain) > inner:
            t = t.copy()
            t.truncate(inner, overflow="ellipsis")
        return t

    body: list[Text] = [
        line(""),
        line(Text("⏳  刷新中", style="bold yellow")),
        line(""),
        line(Text(_strip_refresh_prefix(busy) or "准备中…", style="yellow")),
    ]
    if page_count > 1:
        body += [line(""), line(Text(f"列表第 {page + 1}/{page_count} 页", style="dim"))]
    body += [line(""), line(Text("刷新中请稍候；Esc 不会中断本轮刷新", style="dim"))]

    # 极矮终端：连边框都放不下时去掉面板直接平铺，能看比好看重要。
    if height < len(body) + 2:
        for text in body[:height]:
            _fit(console, text)
        return

    # 垂直居中。关键：**上下留白从同一个 spare 里分**，否则两个 max(0, …) 各自
    # 独立计算必然打架——以前 `pad` 没扣边框和那一行空行，`trailing` 又把超发
    # 掩盖成 0，结果 h≤9 时所有宽度都溢出。
    spare = height - len(body) - 2  # 2 = 上下边框
    pad = max(0, spare // 3)
    trailing = max(0, spare - pad)

    for _ in range(pad):
        console.print()
    console.print(Panel(Group(*body), title="刷新", border_style="yellow", padding=(0, 2)))
    for _ in range(trailing):
        console.print()


def render_list(
    console: Console,
    *,
    snap: Snapshot,
    rows: list[AuctionState],
    cursor: int,
    page: int,
    page_size: int,
    tz: ZoneInfo,
    stale_minutes: int,
    status: str = "",
    busy: str = "",
    notice: tuple[str, ...] = (),
    view: ListView = ListView.ALL,
    view_counts: dict[ListView, int] | None = None,
    names: dict[str, str] | None = None,
) -> None:
    """主列表屏。列随终端宽度自适应，**倒计时列最后才砍**。

    ``names`` 是 ``asin -> 显示用标题``（开了翻译才有）：只换显示，不动
    ``AuctionState.name``——那是接口原文，写回库里会把日文标题覆盖成中文。
    """
    now = datetime.now(UTC)
    width = max(10, console.width)
    height = console.height or 24
    view_counts = view_counts or {}
    # 极矮终端（H<6）连"标题+视图栏+新鲜度+表头"都放不下：先砍表头。
    height_ok = height >= list_fixed_height(status=status, busy=busy, notice=notice) + 1

    # 标题只放"是什么 + 有多少"；新鲜度单独一行，出错也不挤占主标题
    shown = len(rows)
    total = len(snap.auctions)
    if view is ListView.ALL or shown == total:
        headline = f"挖煤姬拍卖 · {total} 个拍品"
    else:
        headline = f"挖煤姬拍卖 · {LABELS[view]} {shown} 个（共 {total} 个）"
    if snap.generated_at:
        stale = (now - snap.generated_at).total_seconds() > stale_minutes * 60
        when = format_time(snap.generated_at, tz)
        freshness = f"⚠ 数据已过期（采集于 {when}）" if stale else f"更新于 {when}"
    else:
        freshness = "尚未采集"

    # 砍列优先级：当前价 → 结束时间 → 拍品名 → 剩余（倒计时是产品核心）
    # 阈值按"各列定宽之和 + 拍品名至少 13 列"算出来，不是拍的：
    # 剩余列按**实际内容**定宽（下限能放下表头，上限 REMAINING_WIDTH）。
    # 固定 17 列的问题：大多数值只有 8~12 列，右对齐后表头「剩余」在列首、
    # 数值在列尾，中间空一大截，看着像两列东西。真实反馈：「剩余这一列的排版
    # 有点问题」。取整屏最宽的那条，列宽就贴着内容。
    remain_width = max(
        4,
        min(REMAINING_WIDTH, max((cell_len(render_remaining(r, now)) for r in rows), default=0)),
    )

    # 列宽预算。threshold 是"这一档最少要多少列"：
    #   wide   (标记3) + (剩余 remain+2) + 21(结束) + 13(当前价) + 13(一口价) + 2(名字padding)
    #   mid    (标记3) + (剩余 remain+2) + 21(结束) + 13(当前价) + 2
    #   narrow (标记3) + (剩余 remain+2) + 2
    # 一口价只在 wide 档出现：它比当前价次要，窄屏先砍它。
    if width >= 96:
        layout = "wide"
    elif width >= 80:
        layout = "mid"
    elif width >= 62:
        layout = "compact"
    elif width >= 34:
        layout = "narrow"
    else:
        layout = "tiny"

    fixed = remain_width + 2  # 剩余列（含 padding(0,1) 每列 2）
    if layout != "tiny":
        fixed += 1 + 2  # 标记列（极窄档不显示）
    if layout in ("wide", "mid", "compact"):
        fixed += TIME_WIDTH + 2  # "2026-11-01 21:07:27" 是 19 列
    if layout in ("wide", "mid"):
        fixed += 11 + 2  # 当前价
    if layout == "wide":
        fixed += 11 + 2  # 一口价
    # 「拍品」列自己的 padding(0,1) 也是 2 格。以前漏算，表格实需 width+2，
    # rich 在定宽列放不下时会**整列丢弃**（窄屏把标记列吃掉的真实原因）。
    fixed += 2
    name_width = max(4, width - fixed)

    table = Table(show_header=height_ok, header_style="bold", box=None, padding=(0, 1))
    if layout != "tiny":
        table.add_column("", width=1)
    # 表头要左对齐、数据要右对齐：rich 的 justify 是**列级**属性，表头会跟着
    # 数据一起右对齐（"剩余"被推到列尾，看着像错位）。把表头传成 Text 自带
    # justify 就能只改表头这一格，列仍然 right。
    table.add_column(Text("剩余", justify="left"), width=remain_width, justify="right")
    if layout in ("wide", "mid", "compact"):
        table.add_column("结束时间", width=TIME_WIDTH)
    if layout in ("wide", "mid"):
        table.add_column("当前价", width=11, justify="right")
    if layout == "wide":
        table.add_column("一口价", width=11, justify="right")
    table.add_column("拍品", width=max(4, name_width), no_wrap=True, overflow="ellipsis")

    start = page * page_size
    for i, st in enumerate(rows[start : start + page_size], start=start):
        style = (
            "dim"
            if st.user_query is UserQuery.UNFOLLOWED or st.door_state is DoorState.ENDED
            else ""
        )
        if i == cursor:
            style = "reverse" if not style else "dim reverse"
        cells: list[object] = []
        if layout != "tiny":
            cells.append(Text("❯" if i == cursor else render_marker(st), style=marker_style(st)))
        # 倒计时是这一屏的核心：精确到秒 + 按紧急程度加粗换色（10 分钟内红色）。
        cells.append(Text(render_remaining(st, now), style=remaining_style(st, now), no_wrap=True))
        if layout in ("wide", "mid", "compact"):
            cells.append(Text(render_end_time(st, tz), no_wrap=True))
        if layout in ("wide", "mid"):
            cells.append(Text(price_text(st.latest_bid), style=_price_style(st), no_wrap=True))
        if layout == "wide":
            cells.append(
                Text(
                    fixed_price_text(st.buy_now_price),
                    style=_fixed_price_style(st),
                    no_wrap=True,
                )
            )
        cells.append(Text((names or {}).get(st.asin) or st.name))
        table.add_row(*cells, style=style)

    # 逐行输出，每画一行扣一次预算：极矮终端下宁可少画，也不能把页脚顶出屏幕
    # （页脚是唯一能看出怎么退出去的地方，见 _panel 的同类说明）。
    budget = max(1, height)

    _fit(console, Text(headline, style="bold"))
    budget -= 1
    if budget < 1:
        return
    # 视图栏：每一项都带计数，计数是"要不要切过去"的唯一依据，所以单独占一行。
    _fit(console, view_bar(view, view_counts, compact=layout in ("narrow", "tiny")))
    budget -= 1
    if budget < 1:
        return
    _fit(
        console,
        Text(
            freshness,
            style="yellow"
            if snap.generated_at and (now - snap.generated_at).total_seconds() > stale_minutes * 60
            else "dim",
        ),
    )
    budget -= 1

    # 表头占 1 行（height_ok 为假时整张表都不画）
    if height_ok and budget >= 1:
        console.print(table)
        budget -= 1
        if not rows and budget >= 1:
            _fit(console, Text(_empty_hint(view, snap, view_counts), style="dim"))
            budget -= 1

    line = _status_text(status, busy)
    if line and budget >= 1:
        _fit(console, Text(line, style="yellow"))
        budget -= 1
    for hint in notice[:3]:
        if budget < 1:
            break
        _fit(console, Text(f"  {hint}", style="yellow"))
        budget -= 1
    if len(notice) > 3 and budget >= 1:
        _fit(
            console,
            Text(f"  …还有 {len(notice) - 3} 条提示（按 r 重新采集会把提示清掉）", style="dim"),
        )
