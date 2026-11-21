"""终端里的二维码与「扫码挂载」屏。

二维码用半块字符画：一个字符格覆盖上下两个模块，所以行数只有模块数的一半。
**颜色必须显式给**——``on_white`` 底 + 黑前景，白色模块是空格、黑色模块是
``▀`` / ``▄`` / ``█``。这样不依赖终端主题（浅色主题下单纯用前景色画会整片
反相，扫码枪认不出来）。
"""

from __future__ import annotations

import qrcode
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from ...core.app.qrlogin import (
    KIND_QQ,
    KIND_WECHAT,
    STATE_FAILED,
    STATE_PENDING,
    STATE_SCANNED,
    STATE_SUCCESS,
    QrChallenge,
)

#: 二维码静区（模块数）。规范要求 4；终端里 2 已经够扫码枪认，还能省 4 列。
QR_QUIET_ZONE = 2

#: 半块字符：一个字符格 = 上下两个模块
_BLOCK = {
    (False, False): " ",
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
}

_BLOCK_STYLE = "black on white"


def qr_matrix(payload: str, *, error_correction: int | None = None) -> list[list[bool]]:
    """把一段文本编码成二维码矩阵（含静区）。"""
    level = qrcode.constants.ERROR_CORRECT_M if error_correction is None else error_correction
    code = qrcode.QRCode(
        version=None,
        error_correction=level,
        box_size=1,
        border=QR_QUIET_ZONE,
    )
    code.add_data(payload)
    code.make(fit=True)
    return [list(row) for row in code.get_matrix()]


def qr_lines(payload: str) -> list[Text]:
    """把二维码画成 ``Text`` 行（上下两个模块合成一行）。"""
    matrix = qr_matrix(payload)
    width = len(matrix[0])
    lines: list[Text] = []
    for y in range(0, len(matrix), 2):
        top = matrix[y]
        bottom = matrix[y + 1] if y + 1 < len(matrix) else [False] * width
        text = Text()
        for x in range(width):
            text.append(_BLOCK[(bool(top[x]), bool(bottom[x]))], style=_BLOCK_STYLE)
        lines.append(text)
    return lines


def qr_size(payload: str) -> int:
    """二维码占多少列（= 模块数）。"""
    return len(qr_matrix(payload)[0])


# ------------------------------------------------------------------ 挂载屏

_KIND_TITLES = {KIND_WECHAT: "挂载微信机器人", KIND_QQ: "挂载 QQ 机器人"}

_STATE_LABELS = {
    STATE_PENDING: ("等待扫码", "yellow"),
    STATE_SCANNED: ("已扫码，等确认", "yellow"),
    STATE_SUCCESS: ("已挂载", "green"),
    STATE_FAILED: ("挂载失败", "red"),
}


def render_mount(
    console: Console,
    *,
    challenge: QrChallenge | None,
    state: str = STATE_PENDING,
    message: str = "",
    label: str = "",
    notice: tuple[str, ...] = (),
) -> None:
    """画「扫码挂载」整屏。

    排版优先级：状态行 > 二维码 > 标题 > 提示。二维码是这一屏唯一有用的东西，
    当高度不够时先砍提示与标题，**二维码本身尽量保住**——保不住就明说需要多高
    的窗口，别画半个让人扫不上。
    """
    width = max(20, console.width or 80)
    height = max(3, console.height or 24)

    text_state, style = _STATE_LABELS.get(state, (state, "white"))
    title = _KIND_TITLES.get(challenge.kind if challenge else "", label or "扫码挂载")
    status_line = Text.assemble((f"{text_state}", f"bold {style}"))
    if message:
        status_line.append(f"  {message}", style="dim")

    qr: list[Text] = []
    if challenge is not None and challenge.payload:
        try:
            qr = qr_lines(challenge.payload)
        except Exception as exc:
            message = f"二维码生成失败：{exc}"
            qr = []
    qr_cols = cell_len(qr[0].plain) if qr else 0

    hint_lines: list[Text] = []
    if challenge is not None and challenge.hint:
        hint_lines.append(Text(challenge.hint, style="dim"))
    for line in notice[:2]:
        hint_lines.append(Text(line, style="yellow"))

    # 高度预算：标题 1 + 状态 1 + 空 1 + 底部提示若干
    fixed = 3
    room_for_qr = height - fixed - len(hint_lines)
    too_narrow = qr and qr_cols > width - 2
    too_short = qr and len(qr) > max(0, room_for_qr)

    console.print(Text(title, style="bold"), no_wrap=True, overflow="ellipsis", highlight=False)
    console.print(status_line, no_wrap=True, overflow="ellipsis", highlight=False)
    console.print()

    body_rows = 1  # 中段（二维码 / 降级提示）占的行数，下面按它算余量
    if qr and not too_narrow and not too_short:
        pad = max(0, (width - qr_cols) // 2)
        for line in qr:
            console.print(Text(" " * pad) + line if pad else line, highlight=False)
        body_rows = len(qr)
    elif qr and too_narrow:
        console.print(
            Text(
                f"二维码有 {qr_cols} 列，当前窗口只有 {width} 列；把窗口拉宽或换个更大的终端",
                style="yellow",
            ),
            no_wrap=True,
            overflow="ellipsis",
        )
    elif qr and too_short:
        console.print(
            Text(
                f"二维码要 {len(qr)} 行，当前只放得下 {max(0, room_for_qr)} 行；把窗口拉高再试",
                style="yellow",
            ),
            no_wrap=True,
            overflow="ellipsis",
        )
    elif challenge is not None and not challenge.payload:
        console.print(Text("这个渠道没有二维码可扫。", style="yellow"), no_wrap=True)

    # 提示行也必须进预算：以前它们是"有 height 就打"，可 height 被 max(3, …) 兜住
    # 恒大于 0，于是矮终端上整屏比可用高度多出几行（80×6 实测画 7 行），被
    # prompt_toolkit 从**底部**裁掉——恰好砍掉"按 r 重新取码"这条唯一出路。
    leftover = height - fixed - body_rows
    for line in hint_lines[: max(0, leftover)]:
        console.print(line, no_wrap=True, overflow="ellipsis", highlight=False)
