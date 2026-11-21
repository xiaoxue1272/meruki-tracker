"""登录相关各屏：方式选择、粘贴 Cookie、Cookie 确认、等待、短信验证码。

本文件由 ``screens.py`` 按渲染函数族拆出，纯搬运、逻辑未改。
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from .common import PanelLine, _fit, _keys, _panel, cookie_summary


def render_login_method(
    console: Console, *, has_cookie: bool, browser_available: bool = True
) -> None:
    """选择登录方式。

    每个选项都标出**前提与代价**：分发给别人用时，用户得知道"需要什么、
    会占用什么"，而不是点了才发现浏览器没装。

    Args:
        has_cookie: 是否已登录（措辞从"登录"变成"切换账号"）。
        browser_available: 本机是否找得到 Chromium 系浏览器。
    """
    verb = "切换账号" if has_cookie else "登录"

    # 浏览器登录：没有 Chromium 就用不了，直接把原因写出来
    if browser_available:
        browser_desc = "自动打开独立 Chrome 窗口并读取 Cookie（会占约 20-30MB 用于保持登录态）"
        browser_style = "dim"
    else:
        browser_desc = "本机没找到 Chrome / Edge / Brave 等浏览器，此方式不可用"
        browser_style = "yellow"

    # 每行带丢弃优先级：**选项本身永不丢**（0），说明行先丢（2），
    # 空行最后丢（1）——空行丢了顶多挤一点，选项丢了就没法选了。
    lines: list[PanelLine] = [
        (Text(f"选择{verb}方式", style="bold"), 1),
        ("", 1),
        (Text.assemble(("  1  ", "bold green"), "浏览器登录"), 0),
        (Text(f"     {browser_desc}", style=browser_style), 2),
        ("", 1),
        (Text.assemble(("  2  ", "bold green"), "手机验证码登录", ("  （推荐）", "green")), 0),
        (Text("     用挖煤姬绑定的手机号收短信验证码；不需要额外软件与空间", style="dim"), 2),
        ("", 1),
        (Text.assemble(("  3  ", "bold green"), "手动粘贴 Cookie"), 0),
        (Text("     在任意浏览器（含 Safari / Firefox）登录后把 Cookie 复制过来", style="dim"), 2),
        ("", 1),
        (Text.assemble(("  Esc", "bold"), " 取消"), 0),
    ]
    if has_cookie:
        lines += [
            ("", 1),
            (Text("当前已是登录状态；用以下任一方式重新登录会覆盖现有 Cookie", style="yellow"), 2),
        ]
    else:
        lines += [
            ("", 1),
            (Text("在服务器上运行？先在本机登录，再把 Cookie 复制到服务器使用", style="dim"), 2),
        ]
    _panel(console, lines, title=verb, border_style="cyan")


def render_login_paste(
    console: Console,
    *,
    masked: str,
    count: int,
    busy: str = "",
    notice: tuple[str, ...] = (),
) -> None:
    """粘贴 Cookie 屏。

    输入内容只显示**定长摘要**，不会随输入变长把按键提示挤出屏幕；
    错误信息当场显示在这里，而不是等用户回列表才看见。
    """
    lines: list[PanelLine] = [
        (Text("粘贴 Cookie", style="bold"), 0),
        (Text("从浏览器复制：F12 → Network → 任一请求 → Request Headers → Cookie", style="dim"), 2),
        ("", 1),
        (
            Text.assemble(
                ("已输入   ", "dim"),
                (f"{count} 字符" if count else "空", "green" if count else "dim"),
            ),
            0,
        ),
    ]
    if masked:
        lines.append((Text.assemble(("内容     ", "dim"), masked), 1))
    lines.append(("", 1))
    lines.append((Text("内容会脱敏显示，不会留在终端历史里", style="dim"), 2))
    if busy:
        lines += [("", 1), (Text(f"⏳ {busy}", style="yellow"), 0)]
    for note in notice[:2]:
        lines.append((Text(note if note.startswith("✗") else f"✗ {note}", style="red"), 0))
    _panel(console, lines, title="手动粘贴 Cookie", border_style="cyan")


def render_cookie_confirm(
    console: Console,
    *,
    cookie: str,
    source: str,
    reveal: bool,
    cookie_path: str = "",
) -> None:
    """拿到 Cookie 后确认是否使用。

    默认脱敏；按 v 显示全文时**自动折行**——Cookie 常有几百上千字符，截成
    一行加省略号等于没显示，用户拿不到能复制的文本。真的放不下时明确说出
    "还有几行没显示"，而不是假装已经显示完整。
    """
    height = console.height or 24
    inner = max(1, (console.width or 80) - 4)  # 边框 2 + 左右内边距 2
    head, note = cookie_summary(cookie)

    if reveal:
        label = "Cookie 全文："
        shown = cookie
    else:
        label = "Cookie（已脱敏）："
        shown = head
    rows = list(Text(shown, style="green").wrap(console, inner)) or [Text("")]

    # 取舍顺序（大数先丢）：服务器用法 > 空行 > 元信息 > 安全警示 > Cookie 本身。
    # 安全警示排在空行之后丢——它是这屏存在的理由之一，不能最先没。
    tail: list[PanelLine] = [
        ("", 2),
        (Text.assemble(("来源     ", "dim"), source or "未知"), 1),
        (Text.assemble(("内容     ", "dim"), note), 1),
        ("", 2),
        (Text("这是你的登录凭证，等同于密码：不要发给他人，也不要提交到代码仓库", style="dim"), 1),
    ]
    if cookie_path:
        tail += [("", 2), (Text(f"确认后写入数据库 {cookie_path}", style="dim"), 3)]

    # 与 _panel 同口径地算正文可用行数：扣上下边框，再扣末尾呼吸行。
    room = height - 2
    if room >= 3:
        room -= 1
    hint_rows = (2 if not reveal else 1) if cookie_path else 0
    cookie_room = max(1, room - 1 - len(tail) - hint_rows)

    truncated = len(rows) > cookie_room
    if truncated:
        keep = max(1, cookie_room - 1)
        rest = Text(f"… 还有 {len(rows) - keep} 行没显示", style="dim")
        rows = [*rows[:keep], rest]

    lines: list[PanelLine] = [(Text(label, style="bold"), 0)]
    lines += [(row, 0) for row in rows]
    lines += tail
    if cookie_path:
        if reveal and not truncated:
            lines.append(
                (Text("服务器上使用：文本已在上方完整显示，直接选中复制即可", style="dim"), 3)
            )
        elif reveal:
            lines.append(
                (Text("服务器上使用：上面只显示了一部分，拷这个 db 文件过去更稳", style="dim"), 3)
            )
        else:
            lines.append((Text("服务器上使用：按 v 显示完整文本后复制", style="dim"), 3))
            lines.append((Text("也可以直接把这个 db 文件拷到服务器", style="dim"), 3))
    _panel(console, lines, title="已获取 Cookie", border_style="green")


def render_login_wait(
    console: Console, *, title: str, status: str, hints: tuple[str, ...] = ()
) -> None:
    """等待型登录屏（浏览器登录进度）。"""
    lines: list[PanelLine] = [(Text(status or "正在准备…", style="yellow"), 0)]
    if hints:
        lines.append(("", 1))
        lines.extend((Text(f"  • {h}", style="dim"), 2) for h in hints)
    _panel(console, lines, title=title, border_style="cyan")


def render_sms_mobile(
    console: Console, *, mobile: str, busy: str = "", notice: tuple[str, ...] = ()
) -> None:
    """输入手机号（准备接收验证码）。"""
    lines: list[PanelLine] = [
        (Text("手机验证码登录", style="bold"), 0),
        (Text("填写挖煤姬绑定的手机号，我们会发送一条短信验证码", style="dim"), 2),
        ("", 1),
        (Text.assemble(("手机号   ", "dim"), mobile or ("（未输入）", "dim")), 0),
    ]
    if busy:
        lines += [("", 1), (Text(f"⏳ {busy}", style="yellow"), 0)]
    for note in notice[:2]:
        lines.append((Text(note if note.startswith("✗") else f"✗ {note}", style="red"), 0))
    _panel(console, lines, title="手机验证码", border_style="cyan")


def render_sms_code(
    console: Console,
    *,
    mobile: str,
    code: str,
    busy: str = "",
    notice: tuple[str, ...] = (),
    can_resend: bool = False,
) -> None:
    """输入验证码。验证码不脱敏（短、一次性，用户需要看清自己输了几位）。"""
    hint = "Enter 登录 · Backspace 删除 · Esc 返回"
    if can_resend:
        hint += " · r 重新发送验证码"

    lines: list[PanelLine] = [
        (Text("输入短信验证码", style="bold"), 0),
        (Text.assemble(("已发送到 ", "dim"), mobile), 1),
        ("", 1),
        (Text.assemble(("验证码   ", "dim"), code or ("（未输入）", "dim")), 0),
    ]
    if busy:
        lines += [("", 1), (Text(f"⏳ {busy}", style="yellow"), 0)]
    for note in notice[:2]:
        lines.append((Text(note if note.startswith("✗") else f"✗ {note}", style="red"), 0))
    # 按键提示放在面板外、页脚之上，所以面板得少留一行给它
    _panel(
        console, lines, title="手机验证码", border_style="cyan", avail=(console.height or 24) - 1
    )
    _fit(console, _keys(hint))
