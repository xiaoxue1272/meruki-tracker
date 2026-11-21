"""设置屏与设置编辑屏的渲染（含可视窗口计算）。

本文件由 ``screens.py`` 按渲染函数族拆出，纯搬运、逻辑未改。
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from .common import PanelLine, _fit, _panel

#: 「一组数」类型的配置项（复查点）每项各占一行，这项的标题行 + 竖排明细。
PointLines = dict[str, tuple[str, ...]]


def _option_rows(opt, bullets: PointLines) -> int:
    """一个选项渲染出来占几行（标题行 + 可能有的竖排明细）。"""
    extra = bullets.get(opt.key)
    return 1 + len(extra) if extra else 1


def _settings_rows(
    options: list,
    start: int,
    end: int,
    cursor: int,
    *,
    help_ok: bool,
    bullets: PointLines | None = None,
) -> int:
    """**精确**算 ``options[start:end]`` 渲染出来占几行。

    必须精确而不是估算——估算差 1 行就会把页脚顶掉，而页脚是唯一退出入口。
    规则与下面的渲染循环逐行对应：
    - 首项一定带分组标题（从 ``start`` 开始时 ``current_group`` 为 None）；
    - 之后换组时：先一个空行，再一个分组标题；
    - 每项占 ``_option_rows()`` 行（复查点那种一组数的是"标题 + 每点一行"）；
    - 选中项在 ``help_ok`` 时多一行帮助文本。
    """
    bullets = bullets or {}
    rows = 0
    group = None
    for idx in range(start, end):
        if options[idx].group != group:
            if group is not None:
                rows += 1  # 分组间空行
            group = options[idx].group
            rows += 1  # 分组标题
        rows += _option_rows(options[idx], bullets)
        if idx == cursor and help_ok:
            rows += 1  # 选中项帮助文本
    return rows


def _visible_window(
    options: list,
    cursor: int,
    room: int,
    *,
    help_ok: bool,
    bullets: PointLines | None = None,
) -> tuple[int, int, bool]:
    """在 ``room`` 行里能显示的 ``[start, end)``，保证包含 ``cursor``。

    返回 ``(start, end, help_fits)``：``help_fits`` 表示选中项的帮助行放得下。
    滚动规则跟列表一致：选中项跑出视口时视口跟着走，首尾不越界。
    """
    if not options or room <= 0:
        return 0, 0, False

    def fits(st: int, en: int, hp: bool) -> bool:
        return _settings_rows(options, st, en, cursor, help_ok=hp, bullets=bullets) <= room

    # 第一轮：带帮助行；放不下就退成不带帮助行。连不带都放不下就只放选中项。
    for hp in (True, False) if help_ok else (False,):
        if not fits(cursor, cursor + 1, hp):
            continue
        start, end = cursor, cursor + 1
        while end < len(options) and fits(start, end + 1, hp):
            end += 1
        while start > 0 and fits(start - 1, end, hp):
            start -= 1
        return start, end, hp
    return cursor, cursor + 1, False


def _plan_settings(
    options: list,
    cursor: int,
    avail: int,
    *,
    allow_hints: bool,
    bullets: PointLines | None = None,
):
    """在 ``avail`` 行里装下尽可能多的选项。

    返回 ``(start, end, help_fits, rows)``；``rows`` 是这段真正渲染出来的行数
    （含分组标题与上下"还有 N 项"提示）。装不下返回 ``(0, 0, False, 0)``。
    """
    for room in range(avail, 0, -1):
        st, en, hf = _visible_window(options, cursor, room, help_ok=True, bullets=bullets)
        hints = (int(st > 0) + int(en < len(options))) if allow_hints else 0
        rows = _settings_rows(options, st, en, cursor, help_ok=hf, bullets=bullets) + hints
        if rows <= avail:
            return st, en, hf, rows
    return 0, 0, False, 0


def render_settings(
    console: Console,
    *,
    options: list,
    cursor: int,
    notice: tuple[str, ...] = (),
    cfg=None,
    action_texts: dict[str, str] | None = None,
    title: str = "设置",
    show_groups: bool = True,
) -> None:
    """设置列表：分组展示全部可调项与当前值，小终端下自动滚动。

    行预算按"先砍说明 → 再砍提示条 → 再砍上下还有 N 项 → 最后砍标题"的顺序
    收缩，直到整屏真的放得下。为什么要这么抠：页脚固定占最后一行，主体多画
    一行就把页脚顶出屏幕，而页脚是唯一能看出怎么退出去的地方。

    Args:
        options: 当前可见的 ``config_options.Option`` 列表。
        cursor: 选中下标。
        notice: 提示（保存成功 / 校验失败）。
        cfg: 用来读当前值。
        title: 顶栏文字。分页模式下只有当前组时传组名，省得和分页条重复。
        show_groups: 是否画 ``── 分组 ──`` 小标题。分页模式下每组一页，
            组名已经在上面的分页条里，再画一次是浪费行。
    """
    body = max(1, console.height)
    bullets = _point_bullets(options, cfg)

    # 越靠前的方案信息越全；第一个装得下的就用它。
    tiers = [
        (True, True, True, min(2, len(notice))),
        (True, True, True, min(1, len(notice))),
        (True, True, False, min(2, len(notice))),
        (True, True, False, 0),
        (True, False, False, 0),
        (False, False, False, 0),
    ]

    # 分页模式下调用方传空标题：组名已经在分页条上，再画一遍是浪费一行，
    # 而一行的代价就是少显示一个配置项。
    if not title:
        tiers = [
            (False, True, True, min(2, len(notice))),
            (False, True, True, min(1, len(notice))),
            (False, True, False, min(2, len(notice))),
            (False, True, False, 0),
            (False, False, False, 0),
        ]

    chosen = None
    seen = set()
    for show_title, show_hint, allow_hints, notice_n in tiers:
        if (show_title, show_hint, allow_hints, notice_n) in seen:
            continue
        seen.add((show_title, show_hint, allow_hints, notice_n))
        header = (2 if show_title else 0) + (1 if show_hint else 0)
        notice_rows = (notice_n + 1) if notice_n else 0
        avail = body - header - notice_rows
        if avail < 1:
            continue
        if options:
            st, en, hf, rows = _plan_settings(
                options, cursor, avail, allow_hints=allow_hints, bullets=bullets
            )
            if st == en:
                continue
        else:
            st, en, hf, rows = 0, 0, False, 0
        chosen = (show_title, show_hint, allow_hints, notice_n, st, en, hf, rows)
        break

    if chosen is None:  # 极端情况：连一整项都放不下
        if not options:
            return
        opt = options[min(cursor, len(options) - 1)]
        # 至少把"我在改什么"和头几个点写出来：只剩一个标题时，用户在 6 行终端里
        # 完全看不出这组有几个点。放不下就少写几个，绝不多写。
        left = body
        _fit(console, Text.assemble(("❯ ", "bold green"), (opt.label, "bold")))
        left -= 1
        for point in bullets.get(opt.key, ())[: max(0, left)]:
            _fit(console, Text(f"    - {point}", style="cyan"))
        return

    show_title, show_hint, allow_hints, notice_n, start, end, help_fits, _ = chosen

    if show_title:
        _fit(console, Text(title, style="bold"))
        if show_hint:
            _fit(console, Text("选中一项按 Enter 修改；改动立即生效并存入数据库", style="dim"))
        console.print()

    if not options:
        _fit(console, Text("没有可调整的配置项", style="dim"))
        return

    if allow_hints and start > 0:
        _fit(console, Text(f"  ↑ 上面还有 {start} 项", style="dim"))

    current_group = None
    for idx in range(start, end):
        opt = options[idx]
        if show_groups and opt.group != current_group:
            if current_group is not None:
                console.print()
            current_group = opt.group
            _fit(console, Text(f"── {opt.group} ──", style="dim"))

        selected = idx == cursor
        marker = "❯ " if selected else "  "
        if opt.key in bullets:
            # 一组数（结束前复查点）：标题单独一行，下面竖排每点一行。
            # 横排成 "6 小时 / 3 小时 / 1 小时 / 15 分钟" 既难扫，窄终端下还会被
            # 截掉尾巴——而被截掉的恰好是"最靠近结束、最该看见"的那个点。
            _fit(
                console,
                Text.assemble(
                    (marker, "bold green" if selected else ""),
                    (opt.label, "bold" if selected else ""),
                ),
            )
            for point in bullets[opt.key]:
                _fit(
                    console,
                    Text(
                        f"    - {point}",
                        style="cyan" if selected else "green",
                    ),
                )
            if selected and help_fits:
                _fit(console, Text(f"    {opt.help}", style="dim"))
            continue

        if opt.is_action:
            # 动作项没有值，显示的是"按下去会发生什么"（由调用方按运行期状态给）
            value = (action_texts or {}).get(opt.key, "Enter 执行")
        else:
            try:
                raw_value = opt.read(cfg)
                # 一组数不走这里（上面已按竖排画完）；其余项按最合适的单位显示
                value = opt.format(
                    raw_value,
                    unit=None if opt.kind == "minutes_list" else opt.display_unit(raw_value),
                )
            except Exception:
                value = "（读取失败）"

        line = Text()
        line.append(marker, style="bold green" if selected else "")
        line.append(f"{opt.label}", style="bold" if selected else "")
        # 值右对齐到固定列，扫一眼就能对比
        padding = max(1, 22 - cell_len(opt.label))
        line.append(" " * padding)
        line.append(value, style="cyan" if selected else "green")
        _fit(console, line)

        if selected and help_fits:
            _fit(console, Text(f"    {opt.help}", style="dim"))

    if allow_hints and end < len(options):
        _fit(console, Text(f"  ↓ 下面还有 {len(options) - end} 项", style="dim"))

    if notice_n:
        console.print()
        for msg in notice[:notice_n]:
            _fit(console, Text(msg, style="yellow"))


def _point_bullets(options: list, cfg) -> PointLines:
    """把 ``minutes_list`` 项的值拆成竖排明细（``- 6 小时`` 这样）。

    顺序/去重都由 :meth:`Option.format_lines` 保证（从远到近、重复合并），
    界面只管一行一行画。
    """
    if cfg is None:
        return {}
    out: PointLines = {}
    for opt in options:
        if opt.kind != "minutes_list":
            continue
        try:
            out[opt.key] = opt.format_lines(opt.read(cfg))
        except Exception:
            out[opt.key] = ("（读取失败）",)
    return out


def _point_window(n: int, cursor: int, room: int) -> tuple[int, int, bool, bool]:
    """在 ``room`` 行里显示 ``[start, end)``，保证 ``cursor`` 看得见。

    返回 ``(start, end, 上面还有, 下面还有)``。提示行也算在 ``room`` 里——先试
    上下各留一行提示，再试只留一行，都放不下就不放提示（此时点铺满 ``room``）。
    一屏只剩一两行时，看见选中的那个点比知道还有几个更重要。
    """
    room = max(1, room)
    if n <= room:
        return 0, n, False, False
    for hints in (2, 1):
        slots = room - hints
        if slots < 1:
            continue
        start = max(0, min(max(0, cursor - slots + 1), n - slots))
        end = min(n, start + slots)
        top, bottom = start > 0, end < n
        if int(top) + int(bottom) <= hints:
            return start, end, top, bottom
    slots = room
    start = max(0, min(max(0, cursor - slots + 1), n - slots))
    return start, start + slots, False, False


def render_point_edit(
    console: Console,
    *,
    label: str,
    help_text: str,
    points: tuple[str, ...],
    cursor: int,
    text: str = "",
    editing: bool = False,
    unit: str = "",
    units: tuple[str, ...] = (),
    notice: tuple[str, ...] = (),
) -> None:
    """逐条增删复查点：一个点一行，加一个 / 删一个都只动那一行。

    Args:
        points: 当前各点的显示文案（已按远到近排好）。
        cursor: 选中第几个点。
        text: 正在输入的新点（``editing`` 为真时才有意义）。
        editing: 是否处在"输入一个点"的状态。
        notice: 上一步的结果（已添加 / 已删除 / 报错）。
    """
    body = max(1, console.height)
    # 标题与输入行是必需品：看不见"在改什么"和"正在输入什么"就没法操作。
    show_input = editing
    base = 1 + int(show_input)
    notice_n = min(2, len(notice))
    # 顺序：先砍说明与空行，再砍提示条，最后才砍点——点才是这一屏的内容。
    for show_help, show_blank in ((True, True), (True, False), (False, False)):
        while notice_n > 0 and base + int(show_help) + int(show_blank) + notice_n + 1 > body:
            notice_n -= 1
        if base + int(show_help) + int(show_blank) + notice_n + 1 <= body:
            break
    show_help = bool(show_help) and base + int(show_help) + int(show_blank) + notice_n + 1 <= body
    show_blank = bool(show_blank) and base + int(show_help) + int(show_blank) + notice_n + 1 <= body
    room = max(0, body - base - int(show_help) - int(show_blank) - notice_n)

    _fit(console, Text(f"修改：{label}", style="bold"))
    if show_help:
        _fit(console, Text(help_text, style="dim"))
    if show_blank:
        console.print()

    if points and room >= 1:
        start, end, top, bottom = _point_window(len(points), cursor, room)
        if top:
            _fit(console, Text(f"  ↑ 上面还有 {start} 个点", style="dim"))
        for idx in range(start, end):
            selected = idx == cursor and not editing
            _fit(
                console,
                Text.assemble(
                    ("❯ " if selected else "  ", "bold green" if selected else ""),
                    (f"- {points[idx]}", "bold cyan" if selected else "cyan"),
                ),
            )
        if bottom:
            _fit(console, Text(f"  ↓ 下面还有 {len(points) - end} 个点", style="dim"))
    elif room >= 1:
        _fit(console, Text("  （还没有点；按 a 加一个）", style="dim"))
    # 一行都放不下时也要说明"还有多少"，否则看起来像点被清空了。但这一行
    # **自己也得在预算内**：以前只要 body>=2 就打，于是 body=2 时它把正在输入
    # 的「新点」行顶出屏幕——用户在盲打，看不到自己输的数字。
    if points and room < 1 and body - base >= 1:
        _fit(console, Text(f"  （{len(points)} 个点放不下）", style="dim"))

    if show_input:
        chips = Text()
        chips.append("新点    ", style="dim")
        chips.append(text or "", style="bold")
        if not text:
            chips.append("（输入后按 Enter）", style="dim")
        if unit and len(units) > 1:
            for name in units:
                chips.append(
                    f" {name} " if name == unit else f" {name}",
                    style="reverse" if name == unit else "dim",
                )
            chips.append("（Tab 切换）", style="dim")
        elif unit:
            chips.append(f"  {unit}", style="dim")
        _fit(console, chips)

    for msg in notice[:notice_n]:
        _fit(console, Text(msg, style="yellow"))


def render_setting_edit(
    console: Console,
    *,
    label: str,
    help_text: str,
    current: str,
    text: str,
    notice: tuple[str, ...] = (),
    hint: str = "",
    unit: str = "",
    units: tuple[str, ...] = (),
    secret: bool = False,
) -> None:
    """编辑单个配置项。

    ``unit`` / ``units`` 用于带单位的项（时间、页数…）：单位可切换时把候选
    单位列出来，让用户知道按 Tab 能换成什么，而不是只有一个说不清含义的标签。

    ``secret`` 为真时把刚输入的明文换成圆点：密码/令牌不该停在屏幕上等人路过
    拍照（终端回滚缓冲区里也不该留下）。
    """
    # 取舍顺序（大数先丢）：帮助说明 > 空行 > 额外提示 > 单位行 > 当前值/新值。
    # 当前值与新值永远在场——看不到自己在改什么就没法改。
    lines: list[PanelLine] = [
        (Text(f"修改：{label}", style="bold"), 0),
        (Text(help_text, style="dim"), 2),
        ("", 1),
        (Text.assemble(("当前值  ", "dim"), current, ("", "")), 0),
        (
            Text.assemble(
                ("新值    ", "dim"),
                ("•" * len(text)) if (secret and text) else "",
                text if not secret else "",
                ("（输入后按 Enter）", "dim") if not text else "",
            ),
            0,
        ),
    ]
    if unit and len(units) > 1:
        chips = Text()
        chips.append("单位    ", style="dim")
        for i, name in enumerate(units):
            if i:
                chips.append(" ")
            chips.append(
                f" {name} " if name == unit else f"{name}",
                style="reverse" if name == unit else "dim",
            )
        chips.append("   Tab 切换", style="dim")
        lines.append((chips, 1))
    elif unit:
        lines.append((Text.assemble(("单位    ", "dim"), unit), 1))
    if hint:
        lines += [("", 1), (Text(hint, style="dim"), 1)]
    for msg in notice[:2]:
        lines.append((Text(msg if msg.startswith("✗") else f"✗ {msg}", style="red"), 0))
    _panel(console, lines, title="设置", border_style="cyan")
