"""右侧「通知队列」侧栏：上面画"将要发生"，下面画"刚发生过"。

用户 2026-11-10 拍板（m14435）：侧栏从「排队清单」改成「通知队列」——所有
通知（结束前提醒 / 出价被超过）都注册进 :class:`~meruki_tracker.core.app.notices.
NoticeBoard`，侧栏只展示 ``now < due_at <= now + notify.window_minutes`` 的部分。

沿用 queuebar 的排版约束：侧栏只有 18 列，刻意不画 ``Panel``（边框 + 内边距
吃掉 4 列）；逐行 ``truncate`` 到可用宽度，行数预算精确到行，放不下的末尾
用「… 还有 N 条」明说。
"""

from __future__ import annotations

from datetime import datetime

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from ...core.app.notices import (
    KIND_ENDED,
    KIND_ENDING,
    KIND_EXTENDED,
    KIND_OUTBID,
    KIND_SHORTENED,
    Notice,
)

#: 通知种类 -> (图标, 标签)。未知种类原样显示 kind，不假装认识。
_KIND_LABELS: dict[str, tuple[str, str]] = {
    KIND_ENDING: ("⏰", "结束提醒"),
    KIND_OUTBID: ("▲", "出价被超过"),
    KIND_ENDED: ("■", "拍卖结束"),
    KIND_EXTENDED: ("＋", "时间延后"),
    KIND_SHORTENED: ("－", "时间提前"),
}


def notice_lines(notice: Notice, *, tz, with_time: bool = True) -> list[Text]:
    """一条通知占两行：种类（图标+标签），然后时刻 + 拍品名。

    18 列侧栏放不下「日期 时刻 标签 名字」一整行——标签被 truncate 掉就
    只剩一串数字，谁也看不懂。种类放第一行整行保住；第二行时刻只写
    ``HH:MM``（窗口只有 2 小时，不跨天，日期是噪音），名字被截就截。

    Args:
        with_time: 第二行**要不要**带 ``HH:MM``。告诉"将要在几点发生"时时刻
            是核心信息；而「已发生」段里每条都是刚发生的，时刻退化成噪音——
            6 列占掉 16 列可用宽度的三分之一，实测正文被截成「20:06 时间延 …」，
            名字全没了。这些条目按发生顺序紧挨着排，先后已经由位置说明了。
    """
    icon, label = _KIND_LABELS.get(notice.kind, ("·", notice.kind))
    head = Text.assemble((f"{icon} ", "bold"), (label, "bold"))
    if not with_time:
        return [head, Text.assemble(("  ", "dim"), (notice.body, "dim"))]
    when = notice.due_at.astimezone(tz).strftime("%H:%M")
    return [head, Text.assemble((f"  {when} ", "dim"), (notice.body, "dim"))]


def render_noticebar(
    console: Console,
    notices: tuple[Notice, ...],
    *,
    now: datetime,
    tz,
    recent: tuple[Notice, ...] = (),
) -> None:
    """画一屏侧栏。

    Args:
        notices: 将要通知的事件（``NoticeBoard.snapshot()`` 已过滤排序）。
        now: 当前时刻。
        tz: 显示时区。
        recent: 刚发生的事件（``NoticeBoard.recent()``，新的在前）。

    「将要」在上、「已发生」在下，不是排版洁癖：上面那些还来得及改变你的
    行为（去加价、去看一眼），下面那些只是告知。18 列侧栏一旦挤满，先牺牲的
    必须是下面那段，所以两者共用一份行预算、按顺序分配。
    """
    inner = max(1, (console.width or 18) - 2)  # 左右各留 1 列，别贴着主屏
    room = max(1, console.height or 24)

    # 标题必须放得下 16 列：「未来 2 小时」写进标题会被截成乱码尾巴，
    # 窗口语义放在文档里说清楚就够了。
    head = Text(f"通知队列 · {len(notices)} 条", style="bold reverse")
    lines: list[Text] = [head]

    # ---- 「将要」段
    avail = max(0, room - 1)  # 扣掉标题
    if not notices:
        lines.append(Text("暂无将到点的通知", style="dim"))
    else:
        per_notice = 2  # 每条通知固定两行
        shown = min(len(notices), avail // per_notice)
        # 至少留 1 行给「还有 N 条」；一条都放不下时也要说清有多少没显示
        if shown < len(notices) and shown > 0:
            while shown > 0 and shown * per_notice + 1 > avail:
                shown -= 1
        hidden = len(notices) - shown
        for notice in notices[:shown]:
            lines.extend(notice_lines(notice, tz=tz))
        if hidden:
            note = f"… 还有 {hidden} 条" if shown else f"… {hidden} 条排不下"
            lines.append(Text(note, style="dim"))

    # ---- 「最近」段：只剩多少用多少
    rest = room - len(lines)
    # 至少要 1 行分隔 + 1 条通知的 2 行；不够就不画——半条比不画更让人困惑
    if recent and rest >= 3:
        lines.append(Text("── 已发生 ──", style="dim"))
        rest -= 1
        shown = min(len(recent), rest // 2)
        for notice in recent[:shown]:
            lines.extend(notice_lines(notice, tz=tz, with_time=False))
        older = len(recent) - shown
        if older > 0:
            lines.append(Text(f"… 更早 {older} 条", style="dim"))

    for line in lines[:room]:
        if cell_len(line.plain) > inner:
            line = line.copy()
            line.truncate(inner, overflow="ellipsis")
        console.print(line, no_wrap=True, overflow="ellipsis", highlight=False)
