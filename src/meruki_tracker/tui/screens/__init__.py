"""TUI 各「屏幕」的渲染函数。

本包由原来的 ``screens.py``（1586 行）按渲染函数族拆成八个子模块，这里
re-export 全部公开名，使得 ``from . import screens`` + ``screens.render_list(...)``
的用法一字不改。

设计原则（沿用原 ``screens.py``，拆包不改）：

1. **纯渲染**：只往传入的 ``Console`` 画一屏，不读输入、不等待、不清屏——
   清屏和重绘由 :class:`meruki_tracker.tui.tui.Tui` 的统一全屏渲染负责。
2. **外部文本一律包成 ``Text``**：商品名、URL、描述文本、错误消息都来自
   站点或用户，直接拼进 rich markup 会让 ``[/]`` 之类字符抛 MarkupError。
3. **必须适配终端尺寸**：prompt_toolkit 的 Window 不会滚动，超出的内容直接
   消失、且底部按键提示最先牺牲。所有屏幕按 ``console.width/height`` 自适应；
   放不下时明确说明，绝不静默裁掉操作提示。
4. 每个屏幕自带底部按键提示。

注：``no_wrap`` / ``overflow`` 必须作为 ``console.print`` 的参数传；写成
``Text(..., no_wrap=True)`` 会在 rich 15 里被丢弃（内部 join 后重建 Text），
导致长文本照常折行、头部/提示条多占行。
"""

from ..views import ListView
from .common import (
    PanelLine,
    _shrink,
    cookie_summary,
    fixed_price_text,
    mask_cookie,
    price_text,
)
from .detail import (
    detail_room_for_observations,
    render_clean_confirm,
    render_detail,
    render_detail_text,
    wrap_plain,
)
from .list import (
    footer_text,
    list_fixed_height,
    render_list,
    render_refreshing,
    view_bar,
)
from .login import (
    render_cookie_confirm,
    render_login_method,
    render_login_paste,
    render_login_wait,
    render_sms_code,
    render_sms_mobile,
)
from .noticebar import notice_lines, render_noticebar
from .qr import QR_QUIET_ZONE, qr_lines, qr_matrix, qr_size, render_mount
from .settings import (
    render_point_edit,
    render_setting_edit,
    render_settings,
)
from .zonetab import tab_bar

__all__ = [
    "QR_QUIET_ZONE",
    "ListView",
    "PanelLine",
    "_shrink",
    "cookie_summary",
    "detail_room_for_observations",
    "fixed_price_text",
    "footer_text",
    "list_fixed_height",
    "mask_cookie",
    "notice_lines",
    "price_text",
    "qr_lines",
    "qr_matrix",
    "qr_size",
    "render_clean_confirm",
    "render_cookie_confirm",
    "render_detail",
    "render_detail_text",
    "render_list",
    "render_login_method",
    "render_login_paste",
    "render_login_wait",
    "render_mount",
    "render_noticebar",
    "render_point_edit",
    "render_refreshing",
    "render_setting_edit",
    "render_settings",
    "render_sms_code",
    "render_sms_mobile",
    "tab_bar",
    "view_bar",
    "wrap_plain",
]
