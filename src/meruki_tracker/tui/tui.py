"""TUI：**一个常驻的 prompt_toolkit 应用** + 屏幕状态机。

为什么这么设计（旧实现的三个毛病都源于此）：

- 旧版每个动作键都 ``app.exit()``，全屏应用一退，终端回到进入前的画面 ——
  列表"消失"；
- 详情走 rich 直接 ``print`` + ``input()``，往普通屏幕**追加**，从不清理；
- 动作窗口里没有应用接管终端，敲的字符被终端回显、还会缓冲到下一次按键。

现在整个会话只有一个 ``Application``：屏幕（列表 / 详情 / 登录各步 / 设置）
由状态机切换，每次重绘都是整屏，从不追加；所有按键都由应用接管，不会出现
"字符漏到屏幕上"；刷新/浏览器登录都在应用内以后台任务执行，进度直接
画在当前屏上。

登录不再依赖 questionary：方式选择、粘贴 Cookie、确认使用都是本应用内的屏幕，
因此不需要退出应用再嵌套另一个事件循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from prompt_toolkit import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from rich.console import Console
from rich.text import Text

from ..core.adapters.browser import find_chrome
from ..core.adapters.store.snapshot import (
    Account,
    LastAttempt,
    SchemaVersionError,
    Snapshot,
)
from ..core.app.browser_login import browser_login
from ..core.app.context import AppContext, do_refresh, load_snapshot, verify_cookie
from ..core.app.notices import KIND_ENDING, KIND_OUTBID, normalize_points
from ..core.app.notifications import RetryableChannel
from ..core.app.qrlogin import KIND_QQ, KIND_WECHAT, build_provider
from ..core.config import Config
from ..core.config_options import OPTIONS_BY_KEY, apply_edit, option_visible
from ..core.domain.goods import clip_for_translation, description_of
from ..core.domain.models import AuctionState, DoorState, TimeState, UserQuery
from ..core.domain.render import format_time, render_end_time
from ..core.errors import (
    AuthExpiredError,
    ConfigError,
    MamError,
    MerukiTrackerError,
    NetworkError,
    WafBlockedError,
)
from ..core.jitter import jittered_seconds
from . import screens
from .views import CLEANABLE, LABELS, ListView, counts, matches, next_view
from .zones import next_group

log = logging.getLogger(__name__)

#: 采集进度最多多久刷一次界面（秒）。太小会闪，太大会看着像卡住。
_PROGRESS_REDRAW_INTERVAL = 0.5

#: 扫码挂载的轮询间隔（秒）。微信二维码有效期约 150 秒，5 秒一次够快也不疼。
_MOUNT_POLL_SECONDS = 5.0

#: 右侧「排队清单」侧栏的列数，以及显示它的最小终端宽度。
#: 低于这个宽度侧栏自动隐藏（用户拍板）：列表本身在窄屏已经很挤，再切走
#: 一列数据不值当。改这个值要同步改 ``_sidebar_visible()`` 的判断。
_SIDEBAR_WIDTH = 18
_SIDEBAR_MIN_TERMINAL = 80

# "没有结束时间"（PENDING）在倒序里排最后：给最小值，reverse 后被顶到末尾。
_NEG_INF = datetime.min.replace(tzinfo=UTC)


class Screen(StrEnum):
    """当前正在显示的屏幕。"""

    LIST = "list"
    DETAIL = "detail"
    SETTINGS = "settings"
    SETTING_EDIT = "setting-edit"
    POINT_EDIT = "point-edit"
    LOGIN_METHOD = "login-method"
    LOGIN_PASTE = "login-paste"
    LOGIN_WAIT = "login-wait"
    LOGIN_SMS_MOBILE = "login-sms-mobile"  # 输入手机号
    LOGIN_SMS_CODE = "login-sms-code"  # 输入验证码
    COOKIE_CONFIRM = "cookie-confirm"
    CLEAN_CONFIRM = "clean-confirm"
    DETAIL_DESC = "detail-desc"
    MOUNT = "mount"


class Tui:
    """整个监视器的交互界面。"""

    def __init__(self, ctx: AppContext, cfg: Config) -> None:
        self.ctx = ctx
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.snap: Snapshot = load_snapshot(ctx)

        self._screen = Screen.LIST
        self._status = ""
        self._busy = ""
        #: 是否用"刷新遮罩"盖住列表。只有启动阶段（还没有任何数据）才用；
        #: 平时的刷新不遮罩——数据是旧的但可用，没必要让屏幕闪。
        self._overlay = False
        self._notice: tuple[str, ...] = ()

        # 列表
        #: 当前视图（关注中 / 已结束 / 无法确认 / 全部）。切换视图会重建 _rows。
        self._view = ListView.FOLLOWING
        self._rows: list[AuctionState] = []
        self._cursor = 0
        self._page = 0

        # 详情
        self._detail: AuctionState | None = None
        self._detail_obs: list[tuple[str, str, str]] = []
        #: 详情原始 JSON（描述、图片只在这里面，auctions 表没有）
        self._detail_raw: dict[str, Any] = {}
        #: 详情的两个**规则属性**（``None`` = 还没拉到详情，屏上不显示）。
        #: 它们不在 AuctionState 里：那是状态机关心的东西，这是"这拍品可能
        #: 随时结束 / 会被延长"的底牌，只在详情屏露一次面。
        self._detail_auto_extend: bool | None = None
        self._detail_early_termination: bool | None = None
        #: 描述全文屏的滚动偏移（按显示行，不是字符）
        self._desc_scroll = 0
        #: asin -> (标题译, 描述译)。翻译要打网络，翻过就不再翻。
        # 译文缓存（asin -> (标题, 描述)）。启动时从库里整批载入，之后
        # 由 Translator 负责取/存；界面只读这份内存快照，避免每帧查库。
        self._translations: dict[str, tuple[str, str]] = {}
        self._load_translations()
        #: 「配置」页当前分组（分页表就是 config_options.GROUP_ORDER）
        self._config_group = ""
        # 设置
        self._settings_cursor = 0
        self._editing_key: str = ""
        #: 编辑态当前选中的单位（时间类配置可切换秒/分钟/小时/天）
        self._edit_unit: str = ""
        # 一组数（结束前复查点）的逐条编辑
        #: 选中第几个点
        self._point_cursor: int = 0
        #: 正在输入的内容（只在 ``_point_input_on`` 为真时有意义）
        self._point_input: str = ""
        #: 是否处在"输入一个点"的状态
        self._point_input_on: bool = False
        #: 输入的是新点（None）还是替换第几个（下标）——改一个错数字不该删了再加
        self._point_replace: int | None = None

        # 登录
        self._startup_login = False
        self._startup_cancelled = False
        self._input_text = ""
        self._pending_cookie = ""
        self._pending_nickname = ""
        #: 短信登录：手机号 / 已发送的验证码状态
        self._sms_mobile = ""
        self._sms_sent = False
        self._sms_mobile_input = ""
        self._pending_source = ""
        self._reveal_cookie = False
        self._wait_status = ""

        self._app: Application | None = None
        self._action_task: asyncio.Task | None = None
        self._paste_task: asyncio.Task | None = None
        self._auto_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        # 扫码挂载：一张待扫的码 + 轮询任务
        self._mount_provider: Any = None
        self._mount_challenge: Any = None
        self._mount_state = ""
        self._mount_message = ""
        self._mount_task: asyncio.Task | None = None
        self._mount_kind = ""
        #: 微信收消息循环（拿 context_token 才能在之后主动推提醒）
        self._wechat_task: asyncio.Task | None = None
        self._qq_task: asyncio.Task | None = None
        #: 所有后台任务都收在这个集合里，退出时统一 cancel。串行队列已经删掉：
        #: 刷新互斥由 ``refresher.refresh()`` 自己的 busy 守卫兜住，翻译/补详情
        #: 写的是内存或各自的表，不存在"两个任务整份覆盖同一份快照"的问题。
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 生命周期

    async def run(self) -> int:
        """运行整个会话，直到用户退出。"""
        self._rebuild()
        self._prepare_start()

        self._app = self._build_app()
        self._start_auto_refresh()
        try:
            await self._app.run_async()
        finally:
            await self._shutdown()
            self._app = None

        if self._startup_cancelled:
            print("未登录，且本地没有缓存数据 —— 已退出。下次运行可重新登录。")
        return 0

    def _build_app(self, *, input=None, output=None) -> Application:
        """构造全屏应用。

        ``input`` / ``output`` 可注入：自动化测试用 prompt_toolkit 的管道输入 +
        DummyOutput 真实发送按键，从而在不依赖终端的情况下覆盖键盘交互。
        """
        kwargs = {}
        if input is not None:
            kwargs["input"] = input
        if output is not None:
            kwargs["output"] = output
        app = Application(
            layout=Layout(
                HSplit(
                    [
                        # 主体 + 右侧「排队清单」侧栏。侧栏用
                        # ConditionalContainer 包起来：filter 为假时它连
                        # preferred_width 都是 0，VSplit 会把整行让给主体。
                        VSplit(
                            [
                                # 只读渲染，没有可编辑区域：不隐藏光标的话，
                                # prompt_toolkit 会把光标停在左上角一直闪，像坏掉的输入框。
                                Window(
                                    FormattedTextControl(
                                        self._render_body, show_cursor=False, focusable=False
                                    ),
                                    height=Dimension(weight=1),
                                    always_hide_cursor=True,
                                ),
                                ConditionalContainer(
                                    Window(
                                        FormattedTextControl(
                                            self._render_noticebar,
                                            show_cursor=False,
                                            focusable=False,
                                        ),
                                        width=Dimension.exact(_SIDEBAR_WIDTH),
                                        always_hide_cursor=True,
                                    ),
                                    Condition(self._sidebar_visible),
                                ),
                            ]
                        ),
                        # 页脚：**固定最后一行**，永远可见、不随内容滚走
                        Window(
                            FormattedTextControl(self._render_footer),
                            height=1,
                            always_hide_cursor=True,
                        ),
                    ]
                )
            ),
            key_bindings=self._build_keys(),
            full_screen=True,
            # 倒计时刷新由我们自己的 ticker 驱动：prompt_toolkit 的
            # refresh_interval 在 run_async 启动时就被捕获，之后改属性无效。
            refresh_interval=None,
            **kwargs,
        )
        # 默认要等 0.5s 才把单独的 ESC 当成 Esc 键（为了区分转义序列），
        # 按"返回"时明显发钝。本地终端里 0.1s 足够。
        app.ttimeoutlen = 0.1
        return app

    def _prepare_start(self) -> None:
        """启动引导：有 Cookie 直接进列表；没有就先进登录。"""
        bot = getattr(self.ctx, "wechat", None)
        if bot is not None:
            self._start_wechat_loop(bot)
        qq = getattr(self.ctx, "qq", None)
        if qq is not None:
            self._start_qq_loop(qq)
        if self.ctx.credentials.load():
            self._start_refresh()
            return
        if self._has_something_to_show():
            self._status = "还没有登录：按 s 进配置，在「登录」组里登录"
            return
        self._screen = Screen.LOGIN_METHOD
        self._startup_login = True

    def _is_logged_in(self) -> bool:
        """当前是否持有凭证（用于措辞：已登录时 l 是"换号"而不是"登录"）。"""
        return bool(self.ctx.credentials.load())

    def _has_something_to_show(self) -> bool:
        return bool(self.ctx.credentials.load() or self.snap.auctions)

    async def _shutdown(self) -> None:
        pending = [
            t
            for t in (self._action_task, self._paste_task, self._auto_task, self._tick_task)
            if t is not None
        ]
        pending.extend(self._tasks)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await task
                except Exception:  # 退出时不再关心任务内部错误
                    log.debug("任务退出时抛出异常", exc_info=True)

    def _exit_now(self) -> None:
        if self._app is not None:
            self._app.exit()

    def _cancel_startup(self) -> None:
        """启动阶段的登录被取消：直接结束程序（没有缓存可看）。"""
        self._startup_cancelled = True
        self._exit_now()

    # ------------------------------------------------------------ 渲染

    def _invalidate(self) -> None:
        if self._app is None:
            return
        self._ensure_ticker()
        self._app.invalidate()

    def _has_countdown(self) -> bool:
        """当前屏是否真有"每秒都在变"的倒计时文字。

        只有列表/详情里、且确有"关注中 + 进行中 + 已确认时间"的拍品才算；
        登录、设置这些静态屏一秒重绘一次纯属浪费。
        """
        if self._screen not in (Screen.LIST, Screen.DETAIL):
            return False
        return any(
            a.end_time.state is TimeState.EXACT
            and a.door_state is DoorState.ACTIVE
            and a.user_query is UserQuery.FOLLOWING
            for a in self._rows
        )

    def _needs_tick(self) -> bool:
        """要不要有个 1Hz 的 ticker 在跑。

        两个理由各管各的：通知板上有「提醒窗口内将到点」或「已到点还没收走」
        的条目时，到点触发靠它（跟当前在哪个屏无关）；本屏有倒计时时才需要
        每秒重绘。注意这里只管**ticker 要不要活**，重绘是 ``_tick_loop`` 里
        另一回事——静态屏挂着 ticker 等提醒时不该每秒白画一遍。
        """
        board = getattr(self.ctx, "notices", None)
        if board is not None and board.pending_count(datetime.now(UTC)):
            return True
        return self._has_countdown()

    def _ensure_ticker(self) -> None:
        if self._app is None:
            return
        # 已经结束的任务要当成"没有"：以前只判 ``is None``，于是心跳一旦因为
        # 某一拍抛异常而退出，就再也不会被重建（倒计时和提醒永久停摆）。
        if self._tick_task is not None and self._tick_task.done():
            self._tick_task = None
        if self._needs_tick() and self._tick_task is None:
            self._tick_task = self._spawn(self._tick_loop(), name="tick")
        elif not self._needs_tick() and self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            # 到点的通知在这里触发（投递 + 闪提示）。1Hz 就够：提醒精度是
            # 分钟级，1 秒误差无意义；也不给每条通知单起 sleep 任务。
            #
            # 每一拍都兜一层：心跳是"永久后台任务"，任何一次意外（某行数据
            # 怪、app 正在关闭…）都不该让整个倒计时与提醒永久停摆。
            try:
                fired = self._fire_notices()
                if self._app is None:
                    continue
                # 只有「刚有通知到点」或「本屏有每秒变化的倒计时」才重绘。
                # 静态屏（设置/登录…）挂着 ticker 只是为了等提醒，每秒整屏
                # 重画一次（实测 1.8ms）纯浪费，跳过。
                if fired or self._has_countdown():
                    self._app.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("定时刷新这一拍出错（下一拍继续）", exc_info=True)

    def _sidebar_visible(self) -> bool:
        """窄屏把侧栏藏起来，把整行都让给列表。

        ``ConditionalContainer`` 的 filter 和 ``_render_body`` 的 Console 宽度
        必须用**同一个**判断，否则主体按较宽排版、实际可用宽度更窄，表现就是
        列表被静默裁列（文档集 07 踩过最多坑的地方）。
        """
        width, _ = self._screen_size()
        return width >= _SIDEBAR_MIN_TERMINAL

    def _render_body(self) -> ANSI:
        """主体区域（不含页脚）——高度扣页脚、宽度扣侧栏。

        宽度必须和 ``VSplit`` 实际分给这个 Window 的一致：侧栏可见时主体只有
        ``终端宽 - _SIDEBAR_WIDTH`` 列。各屏 ``render_*`` 全靠 ``console.width``
        做砍列 / 截断预算，这里少扣一列它们就会按不存在的空间排版。
        """
        width, height = self._screen_size()
        if self._sidebar_visible():
            width -= _SIDEBAR_WIDTH
        width = max(1, width)
        console = Console(
            width=width,
            height=max(1, height - 1),  # 页脚固定占 1 行
            force_terminal=True,
            color_system="truecolor",
            # 宽度是我们按 _screen_size() 自己算好、要 rich 原样用的。rich 在
            # "旧式 Windows 控制台"上会偷偷把 width 减 1，让我们整套砍列预算
            # 全错位（GitHub windows runner 上 4 个测试因此挂掉）。全屏 TUI
            # 走 prompt_toolkit，必然有 VT，不需要 rich 的 legacy 补偿。
            legacy_windows=False,
        )
        with console.capture() as cap:
            self._draw(console)
        return ANSI(cap.get())

    def _render_noticebar(self) -> ANSI:
        """右侧通知队列——只画 ``notify.window_minutes`` 内将要通知的事件。"""
        _, height = self._screen_size()
        console = Console(
            width=_SIDEBAR_WIDTH,
            height=max(1, height - 1),  # 页脚固定占 1 行
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,  # 同 _render_body：宽度是我们算好的，别偷偷减 1
        )
        board = getattr(self.ctx, "notices", None)
        now = datetime.now(UTC)
        notices = board.snapshot(now) if board is not None else ()
        # 刚发生的事件（结束 / 时间变化 / 出价被超过）也放侧栏，但只占「将要」
        # 用剩的行数——上面那些还来得及改变行为，下面那些只是告知。
        recent = board.recent() if board is not None else ()
        with console.capture() as cap:
            screens.render_noticebar(console, notices, now=now, tz=self.tz, recent=recent)
        return ANSI(cap.get())

    # ---------------------------------------------------------------- 通知板

    def _sync_notices(self) -> None:
        """按当前快照重算「结束前 N 分钟」提醒（幂等）。

        生产环境刷新成功后 refresher 也会同步一次；这里兜住**启动时从缓存
        加载**与测试替身（假 refresher 不碰通知板）两条路。
        """
        board = getattr(self.ctx, "notices", None)
        if board is None:
            return
        board.sync_ending(
            self.snap.auctions,
            # 提醒点就是配置里那组「结束前复查点」，和详情复查点无关：
            # "多久拉一次详情"不等于"多久提醒一次"。
            points_minutes=normalize_points(self.cfg.notify.points_minutes),
            now=datetime.now(UTC),
        )

    def _fire_notices(self) -> bool:
        """收走到点的通知：投递在 ``take_due`` 里做，这里只闪界面提示。

        Returns:
            本次是否真的有通知到点（``_tick_loop`` 据此决定要不要重绘）。
        """
        board = getattr(self.ctx, "notices", None)
        if board is None:
            return False
        due = board.take_due(datetime.now(UTC))
        if not due:
            return False
        flashes = [
            f"⏰「{n.body}」{n.title}"
            if n.kind == KIND_ENDING
            else f"▲ {n.body}"
            if n.kind == KIND_OUTBID
            else f"◆「{n.body}」{n.title}"
            for n in due
        ]
        self._notice = (*flashes, *self._notice)[:3]
        return True

    def _edit_has_units(self) -> bool:
        """设置编辑屏里的选项能不能切单位（能才在页脚提示 Tab）。"""
        if self._screen not in (Screen.SETTING_EDIT, Screen.POINT_EDIT) or not self._editing_key:
            return False
        opt = OPTIONS_BY_KEY.get(self._editing_key)
        return bool(opt is not None and opt.units)

    def _render_footer(self) -> ANSI:
        """固定页脚：始终显示当前可用按键。"""
        width, _ = self._screen_size()
        console = Console(
            width=width,
            height=1,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,  # 页脚宽度也是自己算的，别让 rich 偷偷减 1 截断退出键
        )
        page, pages = self._page, self._page_count()

        text = screens.footer_text(
            str(self._screen),
            page=page,
            page_count=pages,
            reveal=self._reveal_cookie,
            can_resend=not self._busy,
            busy=self._busy if (self._screen is Screen.LIST and self._overlay) else "",
            can_clean=self._view in CLEANABLE and bool(self._rows),
            translate_on=self._translate_on(),
            edit_units=self._edit_has_units(),
            width=width,
        )
        with console.capture() as cap:
            console.print(
                Text(text, style="reverse"), no_wrap=True, overflow="ellipsis", highlight=False
            )
        return ANSI(cap.get())

    def _groups(self) -> tuple[str, ...]:
        """配置页的分页表——直接就是 ``GROUP_ORDER``，加一组自动多一页。"""
        from ..core.config_options import GROUP_ORDER

        return tuple(GROUP_ORDER)

    def _tab_index(self) -> int:
        groups = self._groups()
        if not groups:
            return 0
        if self._config_group not in groups:
            self._config_group = groups[0]
        return groups.index(self._config_group)

    def _draw_bars(self, console: Console) -> None:
        """只在配置页画一根分组条，并把它占掉的行从 ``console.height`` 扣掉。

        列表页的三视图栏由 ``render_list`` 自己画（要带计数、要参与布局）；
        登录/详情/清理确认/挂载这些是占满整屏的子屏，不画条。外层「大分页」
        已经拆掉，所以这里最多只有一根条。

        为什么直接改 ``console.height``：各屏的排版预算都按 ``console.height``
        算，条画在同一个 console 上，不同步减高就会出现"预算比实际多一行"，
        最后一行页脚被顶出屏幕——渲染审计里反复踩的正是这一类。
        """
        if self._screen not in (Screen.SETTINGS, Screen.SETTING_EDIT, Screen.POINT_EDIT):
            return
        width = console.width or 80
        console.print(
            screens.tab_bar(
                list(self._groups()), self._tab_index(), width=width, compact=width < 52
            ),
            no_wrap=True,
            overflow="ellipsis",
            highlight=False,
        )
        console.height = max(1, console.height - 1)

    def _draw(self, console: Console) -> None:
        self._clamp_view()
        self._draw_bars(console)
        match self._screen:
            case Screen.LIST if self._overlay and self._busy:
                # 采集期间遮掉列表：此刻数据正在变，不该让用户读/翻/进详情
                screens.render_refreshing(
                    console,
                    busy=self._busy,
                    page=self._page,
                    page_count=self._page_count(),
                )
            case Screen.LIST:
                screens.render_list(
                    console,
                    snap=self.snap,
                    rows=self._rows,
                    cursor=self._cursor,
                    page=self._page,
                    page_size=self._page_size,
                    tz=self.tz,
                    stale_minutes=self.cfg.storage.data_stale_minutes,
                    status=self._status,
                    busy=self._busy,
                    notice=self._notice,
                    view=self._view,
                    view_counts=self._view_counts(),
                    names=self._translation_titles(),
                )
            case Screen.CLEAN_CONFIRM:
                screens.render_clean_confirm(
                    console,
                    view=self._view,
                    targets=self._clean_targets(),
                    tz=self.tz,
                    notice=self._notice,
                )
            case Screen.DETAIL if self._detail is not None and self._overlay and self._busy:
                screens.render_refreshing(console, busy=self._busy)
            case Screen.DETAIL if self._detail is not None:
                screens.render_detail(
                    console,
                    st=self._detail,
                    snapshot=self.snap,
                    observations=self._detail_obs,
                    tz=self.tz,
                    notice=self._notice,
                    desc=self._desc_text(),
                    desc_translated=self._desc_is_translated(),
                    title=self._title_text(),
                    auto_extend=self._detail_auto_extend,
                    early_termination=self._detail_early_termination,
                )
            case Screen.DETAIL_DESC if self._detail is not None:
                screens.render_detail_text(
                    console,
                    st=self._detail,
                    text=self._desc_text(),
                    translated=self._desc_is_translated(),
                    scroll=self._desc_scroll,
                    busy=self._busy,
                )
            case Screen.MOUNT:
                screens.render_mount(
                    console,
                    challenge=self._mount_challenge,
                    state=self._mount_state or "pending",
                    message=self._mount_message,
                    label=getattr(self._mount_provider, "label", ""),
                    notice=self._notice,
                )
            case Screen.SETTINGS:
                options = self._visible_options()
                self._clamp_settings(options)
                screens.render_settings(
                    console,
                    options=options,
                    cursor=self._settings_cursor,
                    notice=self._notice,
                    cfg=self.cfg,
                    action_texts=self._action_texts(),
                    title="",
                    show_groups=False,
                )
            case Screen.SETTING_EDIT if self._editing_key:
                opt = OPTIONS_BY_KEY[self._editing_key]
                current_value = opt.read(self.cfg)
                screens.render_setting_edit(
                    console,
                    label=opt.label,
                    help_text=opt.help,
                    # 一组数（复查点）不套统一单位：360 分钟 / 60 分钟 / 15 分钟
                    # 又长又重复，逐项挑最合适的单位写成 6 小时 / 1 小时 / 15 分钟
                    current=opt.format(
                        current_value,
                        unit=None if opt.kind == "minutes_list" else (self._edit_unit or None),
                    ),
                    text=self._input_text,
                    notice=self._notice,
                    unit=self._edit_unit,
                    units=opt.units,
                    secret=opt.secret,
                )
            case Screen.POINT_EDIT if self._editing_key:
                opt = OPTIONS_BY_KEY[self._editing_key]
                screens.render_point_edit(
                    console,
                    label=opt.label,
                    help_text=opt.help,
                    points=opt.format_lines(opt.read(self.cfg)),
                    cursor=self._point_cursor,
                    text=self._point_input,
                    editing=self._point_input_on,
                    unit=self._edit_unit,
                    units=opt.units,
                    notice=self._notice,
                )
            case Screen.LOGIN_METHOD:
                screens.render_login_method(
                    console,
                    has_cookie=bool(self.ctx.credentials.load()),
                    browser_available=bool(find_chrome()),
                )
            case Screen.LOGIN_PASTE:
                screens.render_login_paste(
                    console,
                    masked=screens.cookie_summary(self._input_text)[0] if self._input_text else "",
                    count=len(self._input_text),
                    busy=self._busy,
                    notice=self._notice,
                )
            case Screen.LOGIN_SMS_MOBILE:
                screens.render_sms_mobile(
                    console,
                    mobile=self._sms_mobile_input,
                    busy=self._busy,
                    notice=self._notice,
                )
            case Screen.LOGIN_SMS_CODE:
                screens.render_sms_code(
                    console,
                    mobile=self._sms_mobile,
                    code=self._input_text,
                    busy=self._busy,
                    notice=self._notice,
                    can_resend=not self._busy,
                )
            case Screen.LOGIN_WAIT:
                screens.render_login_wait(
                    console,
                    title="浏览器登录",
                    status=self._wait_status,
                    hints=(
                        "程序会启动一个独立的 Chrome 窗口，请在其中完成登录",
                        "登录成功后会自动读取 Cookie，无需手动复制",
                    ),
                )
            case Screen.COOKIE_CONFIRM:
                screens.render_cookie_confirm(
                    console,
                    cookie=self._pending_cookie,
                    source=self._pending_source,
                    reveal=self._reveal_cookie,
                    cookie_path=str(self.ctx.credentials.path),
                )
            case _:
                console.print(Text("…"))

    # ------------------------------------------------------------ 按键

    def _screen_is(self, screen: Screen):
        """生成"当前是否在该屏幕"的 prompt_toolkit 过滤器。"""
        return Condition(lambda: self._screen is screen)

    def _build_keys(self) -> KeyBindings:
        """按键绑定。

        **必须按屏幕加 filter**：否则全局的 ``r``（刷新）、``s``（设置）等会
        抢走粘贴屏里的普通字符——Cookie 是字母数字串，``sid=`` 里的 ``s`` 会被
        当成"设置"吞掉，用户却看不出少了什么。

        按屏幕拆成六个 ``_bind_*`` 方法，但**调用顺序不能随便调**：
        prompt_toolkit 对同一屏同一键的多条绑定取**最后注册**的那条
        （``KeyProcessor`` 里 ``matches[-1]``）。全局键（q / escape / enter）
        必须先注册，子屏的同键绑定才能压过它——例如设置屏的 enter 要盖过
        全局 enter、描述屏的 escape/q 要盖过全局 escape/q。
        """
        kb = KeyBindings()
        self._bind_global_keys(kb)
        self._bind_nav_keys(kb)
        self._bind_list_keys(kb)
        self._bind_detail_keys(kb)
        self._bind_login_keys(kb)
        self._bind_settings_keys(kb)
        return kb

    def _bind_global_keys(self, kb: KeyBindings) -> None:
        """全局键：退出 / 返回 / 确认。**必须最先注册**——见 ``_build_keys`` docstring。"""
        on_paste = self._screen_is(Screen.LOGIN_PASTE)
        not_paste = ~on_paste

        @kb.add("c-c")
        def _quit(event) -> None:
            # 启动阶段 Ctrl+C 与 q/Esc 一致：都算"放弃启动"，退出时给出说明
            if self._startup_login and not self.ctx.credentials.load():
                self._startup_cancelled = True
            event.app.exit()

        @kb.add("q", filter=not_paste)
        def _back_or_quit(event) -> None:
            if self._screen is Screen.LIST:
                event.app.exit()
            elif self._screen is Screen.LOGIN_METHOD and self._startup_login:
                # 启动阶段在"选择登录方式"屏按 q = 放弃启动
                self._cancel_startup()
            else:
                self._leave_subscreen()

        @kb.add("escape")
        def _escape(event) -> None:
            if self._screen is Screen.LIST:
                return
            if self._screen is Screen.LOGIN_METHOD and self._startup_login:
                self._cancel_startup()
                return
            if self._screen is Screen.SETTING_EDIT:
                # 编辑中按 Esc：放弃本次修改，回到设置列表
                self._editing_key = ""
                self._input_text = ""
                self._notice = ()
                self._screen = Screen.SETTINGS
                self._invalidate()
                return
            if self._screen is Screen.LOGIN_SMS_CODE:
                # 验证码屏回上一步（改手机号），而不是直接退出整个登录流程
                self._screen = Screen.LOGIN_SMS_MOBILE
                self._notice = ()
                self._invalidate()
                return
            self._leave_subscreen()

        @kb.add("enter")
        def _enter(event) -> None:
            if self._screen is Screen.LIST:
                self._open_detail()
            elif self._screen is Screen.LOGIN_PASTE:
                self._submit_paste()
            elif self._screen is Screen.LOGIN_SMS_MOBILE:
                self._submit_sms_mobile()
            elif self._screen is Screen.LOGIN_SMS_CODE:
                self._submit_sms_code()
            elif self._screen is Screen.COOKIE_CONFIRM:
                self._accept_cookie()
            elif self._screen is Screen.CLEAN_CONFIRM:
                self._do_clean()
            elif self._screen in (
                Screen.DETAIL,
                Screen.LOGIN_WAIT,
                Screen.SETTINGS,
            ):
                self._leave_subscreen()
            # LOGIN_METHOD：Enter 不做任何事——以前会静默取消登录（甚至退出程序）

    def _bind_list_keys(self, kb: KeyBindings) -> None:
        """列表屏：刷新、翻译、设置、光标、翻页、切视图、清理。"""
        on_list = self._screen_is(Screen.LIST)
        # 这里**没有** ``idle`` 锁了：``_busy`` 只是进度文案，不再当按键锁。
        # 后台动作直接起任务、由 refresher 自己的 busy 守卫防重复刷新；刷新时
        # 按 t / s / x 都该立刻有回应，而不是被悄悄吞掉。

        @kb.add("r", filter=on_list)
        def _refresh(event) -> None:
            self._start_refresh()

        # 登录**没有**列表快捷键：和挂载一样，它已经是「配置 → 登录」里的一行
        # 动作项。以前这里有个 ``l``，页脚也跟着占一格；页脚本来就挤，而换号是
        # 低频操作，删掉把位置让给高频键。
        @kb.add("r", filter=self._screen_is(Screen.MOUNT))
        def _remount(event) -> None:
            # 显式按 r 才换码：已挂载的人不该因为进来看一眼就被迫重扫
            self._open_mount(self._mount_kind, force=True)

        @kb.add("t", filter=on_list)
        def _toggle_translate_list(event) -> None:
            self._toggle_translate()

        @kb.add("down", filter=on_list)
        def _down(event) -> None:
            self._move(1)

        @kb.add("up", filter=on_list)
        def _up(event) -> None:
            self._move(-1)

        # 左右箭头切视图（关注中 / 已结束 / 无法确认 / 全部），和翻页分开：
        # 以前 ←→ 是翻页、[ ] 是切视图，两个都"横着按"，用户记不住哪对管哪层。
        # 现在横向一律是"换一屏同类内容"——左退一屏、右进一屏。
        @kb.add("right", filter=on_list)
        def _view_right(event) -> None:
            self._cycle_view(1)

        @kb.add("left", filter=on_list)
        def _view_left(event) -> None:
            self._cycle_view(-1)

        # 翻页只留 n / p：页数本来就只有一页两页，不该占用最顺手的箭头。
        @kb.add("n", filter=on_list)
        def _next(event) -> None:
            self._turn_page(1)

        @kb.add("p", filter=on_list)
        def _prev(event) -> None:
            self._turn_page(-1)

        @kb.add("x", filter=on_list)
        def _clean(event) -> None:
            self._open_clean_confirm()

    def _bind_nav_keys(self, kb: KeyBindings) -> None:
        """换屏键：列表 ↔ 配置（``s``）与"切一个页签"（``Tab`` / 左右箭头）。

        外层「大分页」已经拆掉：默认就是列表，按 ``s`` 进配置。切换不同页签
        （列表的三视图、配置的七个分组）统一用 ``Tab`` / ``Shift+Tab``，左右
        箭头是同一件事的顺手版本。

        这里绑 ``tab`` 不会抢掉编辑态的 ``Tab``（切单位）——``_bind_settings_keys``
        最后注册，prompt_toolkit 同屏同键取最后注册的那个。
        """
        on_list = self._screen_is(Screen.LIST)
        on_settings = self._screen_is(Screen.SETTINGS)

        @kb.add("s", filter=on_list)
        def _settings(event) -> None:
            self._open_settings()

        @kb.add("tab", filter=on_list)
        def _view_next(event) -> None:
            self._cycle_view(1)

        @kb.add("s-tab", filter=on_list)
        def _view_prev(event) -> None:
            self._cycle_view(-1)

        @kb.add("tab", filter=on_settings)
        def _config_next(event) -> None:
            self._cycle_config_group(1)

        @kb.add("s-tab", filter=on_settings)
        def _config_prev(event) -> None:
            self._cycle_config_group(-1)

        # 配置页的上下箭头管光标，左右箭头就空着——拿来切分组正好。
        @kb.add("right", filter=on_settings)
        def _config_right(event) -> None:
            self._cycle_config_group(1)

        @kb.add("left", filter=on_settings)
        def _config_left(event) -> None:
            self._cycle_config_group(-1)

    def _bind_detail_keys(self, kb: KeyBindings) -> None:
        """详情屏与拍品描述全文屏。"""
        on_detail = self._screen_is(Screen.DETAIL)
        on_desc = self._screen_is(Screen.DETAIL_DESC)

        @kb.add("r", filter=on_detail)
        def _refresh_from_detail(event) -> None:
            # 详情屏的 r 只刷新**当前这一条**。真实反馈：「详情页面里面 r 刷新之后
            # 提示要我退回列表，我理解在详情里面的 r，应该仅刷新当前 id 对应的数据」。
            # 之前这里是整体刷新（列表 + 详情），会把用户甩回列表屏——在详情页按
            # 刷新却被弹出去，动作和结果对不上。
            self._refresh_current_detail()

        @kb.add("v", filter=on_detail)
        def _open_desc(event) -> None:
            # 描述动辄上千字，塞进详情屏会把价格挤没，所以单开一屏看全文。
            if not self._desc_text():
                self._notice = ("这个拍品没有描述",)
                self._invalidate()
                return
            self._desc_scroll = 0
            self._screen = Screen.DETAIL_DESC
            self._invalidate()

        @kb.add("t", filter=on_detail)
        def _toggle_translate_detail(event) -> None:
            self._toggle_translate()

        @kb.add("escape", filter=on_desc)
        @kb.add("q", filter=on_desc)
        def _leave_desc(event) -> None:
            self._screen = Screen.DETAIL
            self._invalidate()

        @kb.add("down", filter=on_desc)
        @kb.add("j", filter=on_desc)
        def _desc_down(event) -> None:
            self._desc_scroll += 1
            self._invalidate()

        @kb.add("up", filter=on_desc)
        @kb.add("k", filter=on_desc)
        def _desc_up(event) -> None:
            self._desc_scroll = max(0, self._desc_scroll - 1)
            self._invalidate()

        @kb.add("pagedown", filter=on_desc)
        def _desc_page_down(event) -> None:
            _, rows = self._screen_size()
            self._desc_scroll += max(1, rows - 4)
            self._invalidate()

        @kb.add("pageup", filter=on_desc)
        def _desc_page_up(event) -> None:
            _, rows = self._screen_size()
            self._desc_scroll = max(0, self._desc_scroll - max(1, rows - 4))
            self._invalidate()

    def _bind_login_keys(self, kb: KeyBindings) -> None:
        """登录方式 / Cookie 确认 / 粘贴 / 手机号 / 验证码各输入屏。"""
        on_method = self._screen_is(Screen.LOGIN_METHOD)
        on_confirm = self._screen_is(Screen.COOKIE_CONFIRM)
        on_paste = self._screen_is(Screen.LOGIN_PASTE)
        on_sms_mobile = self._screen_is(Screen.LOGIN_SMS_MOBILE)
        on_sms_code = self._screen_is(Screen.LOGIN_SMS_CODE)

        @kb.add("1", filter=on_method)
        def _one(event) -> None:
            self._start_browser_login()

        @kb.add("2", filter=on_method)
        def _two(event) -> None:
            self._notice = ()
            self._sms_mobile_input = ""
            self._sms_sent = False
            self._screen = Screen.LOGIN_SMS_MOBILE
            self._invalidate()

        @kb.add("3", filter=on_method)
        def _three(event) -> None:
            self._input_text = ""
            self._screen = Screen.LOGIN_PASTE
            self._invalidate()

        @kb.add("y", filter=on_confirm)
        def _yes(event) -> None:
            self._accept_cookie()

        @kb.add("v", filter=on_confirm)
        def _toggle_reveal(event) -> None:
            self._reveal_cookie = not self._reveal_cookie
            self._invalidate()

        @kb.add("backspace", filter=on_paste)
        @kb.add("c-h", filter=on_paste)
        def _backspace(event) -> None:
            if self._input_text:
                self._input_text = self._input_text[:-1]
                self._invalidate()

        # 手机号输入屏：只收数字与 +

        @kb.add("backspace", filter=on_sms_mobile)
        @kb.add("c-h", filter=on_sms_mobile)
        def _mobile_backspace(event) -> None:
            if self._sms_mobile_input:
                self._sms_mobile_input = self._sms_mobile_input[:-1]
                self._invalidate()

        @kb.add(Keys.Any, filter=on_sms_mobile)
        def _mobile_any(event) -> None:
            if event.data and all(ch.isdigit() or ch == "+" for ch in event.data):
                self._sms_mobile_input += event.data
                self._invalidate()

        @kb.add(Keys.BracketedPaste, filter=on_sms_mobile)
        def _mobile_paste(event) -> None:
            self._sms_mobile_input += "".join(ch for ch in event.data if ch.isdigit() or ch == "+")
            self._invalidate()

        # 验证码输入屏：只收数字

        @kb.add("backspace", filter=on_sms_code)
        @kb.add("c-h", filter=on_sms_code)
        def _code_backspace(event) -> None:
            if self._input_text:
                self._input_text = self._input_text[:-1]
                self._invalidate()

        @kb.add(Keys.Any, filter=on_sms_code)
        def _code_any(event) -> None:
            if event.data and event.data.isdigit():
                self._input_text += event.data
                self._invalidate()

        @kb.add(Keys.BracketedPaste, filter=on_sms_code)
        def _code_paste(event) -> None:
            self._input_text += "".join(ch for ch in event.data if ch.isdigit())
            self._invalidate()

        @kb.add("r", filter=on_sms_code)
        def _resend(event) -> None:
            self._resend_sms_code()

        @kb.add(Keys.BracketedPaste, filter=on_paste)
        def _paste(event) -> None:
            # 整段粘贴一次性到达：Cookies 动辄上千字符，逐字符处理会很慢
            self._input_text += event.data.replace("\r", "").replace("\n", "")
            self._invalidate()

        @kb.add(Keys.Any, filter=on_paste)
        def _any(event) -> None:
            if event.data.isprintable():
                self._input_text += event.data
                self._invalidate()

    def _bind_settings_keys(self, kb: KeyBindings) -> None:
        """设置屏、设置编辑屏。"""
        on_settings = self._screen_is(Screen.SETTINGS)
        on_edit = self._screen_is(Screen.SETTING_EDIT)

        @kb.add("down", filter=on_settings)
        def _set_down(event) -> None:
            self._move_settings(1)

        @kb.add("up", filter=on_settings)
        def _set_up(event) -> None:
            self._move_settings(-1)

        @kb.add("enter", filter=on_settings)
        def _set_enter(event) -> None:
            self._begin_edit()

        @kb.add("backspace", filter=on_edit)
        @kb.add("c-h", filter=on_edit)
        def _edit_backspace(event) -> None:
            if self._input_text:
                self._input_text = self._input_text[:-1]
                self._invalidate()

        @kb.add("enter", filter=on_edit)
        def _edit_enter(event) -> None:
            self._commit_edit()

        # 带单位的项：Tab / Shift-Tab 换单位。数字按当前单位解释，落库仍是规范单位。
        @kb.add("tab", filter=on_edit)
        def _edit_unit_next(event) -> None:
            self._cycle_edit_unit(1)

        @kb.add("s-tab", filter=on_edit)
        def _edit_unit_prev(event) -> None:
            self._cycle_edit_unit(-1)

        @kb.add(Keys.Any, filter=on_edit)
        def _edit_any(event) -> None:
            if event.data.isprintable():
                self._input_text += event.data
                self._invalidate()

        @kb.add(Keys.BracketedPaste, filter=on_edit)
        def _edit_paste(event) -> None:
            self._input_text += event.data.replace("\r", "").replace("\n", "")
            self._invalidate()

        # ---- 一组数（结束前复查点）：一个点一行，逐个加、逐个删 ----
        on_points = self._screen_is(Screen.POINT_EDIT)
        on_point_input = Condition(lambda: self._point_input_on)

        @kb.add("down", filter=on_points)
        def _point_down(event) -> None:
            self._move_point(1)

        @kb.add("up", filter=on_points)
        def _point_up(event) -> None:
            self._move_point(-1)

        @kb.add("a", filter=on_points & ~on_point_input)
        def _point_add(event) -> None:
            self._start_point_input()

        # x 和 d 都认：一个来自"删除"的首字母，一个是 vim 习惯
        @kb.add("x", filter=on_points & ~on_point_input)
        @kb.add("d", filter=on_points & ~on_point_input)
        def _point_drop(event) -> None:
            self._drop_current_point()

        @kb.add("enter", filter=on_points)
        def _point_enter(event) -> None:
            if self._point_input_on:
                self._commit_point()
            else:
                self._start_point_input(replace=self._point_cursor)

        @kb.add("tab", filter=on_points)
        def _point_unit_next(event) -> None:
            self._cycle_edit_unit(1)

        @kb.add("s-tab", filter=on_points)
        def _point_unit_prev(event) -> None:
            self._cycle_edit_unit(-1)

        @kb.add("escape", filter=on_points)
        def _point_escape(event) -> None:
            # 输入态先退输入态：敲错半个点不该直接把整屏关掉
            self._cancel_point_input()

        @kb.add("q", filter=on_points)
        def _point_quit(event) -> None:
            self._cancel_point_input()

        @kb.add("backspace", filter=on_points & on_point_input)
        @kb.add("c-h", filter=on_points & on_point_input)
        def _point_backspace(event) -> None:
            if self._point_input:
                self._point_input = self._point_input[:-1]
                self._invalidate()

        @kb.add(Keys.Any, filter=on_points & on_point_input)
        def _point_any(event) -> None:
            if event.data.isprintable():
                self._point_input += event.data
                self._invalidate()

        @kb.add(Keys.BracketedPaste, filter=on_points & on_point_input)
        def _point_paste(event) -> None:
            self._point_input += event.data.replace("\r", "").replace("\n", "")
            self._invalidate()

    # ------------------------------------------------------------ 设置

    def _visible_options(self) -> list:
        """当前分组下的配置项。

        分页之后一屏只放一组：组名已经在分页条上，再在一屏里滚七个组既难找、
        又让"下面还有 N 项"这类提示失去意义。
        """
        groups = self._groups()
        if not groups:
            return []
        if self._config_group not in groups:
            self._config_group = groups[0]
        return [
            o
            for o in self._all_options()
            if o.group == self._config_group and option_visible(o, self.cfg)
        ]

    def _setting(self, key: str, fallback: Any = "") -> Any:
        """读设置值，读不到就退回启动时那份配置。

        扫码挂载成功后凭据只写进了库、没回写 ``self.cfg``；不查库的话，
        「渠道状态」会一直说"还没挂载"，而用户明明刚扫完。
        """
        try:
            value = self.ctx.settings.get(key)
        except Exception:
            log.debug("读设置失败: %s", key, exc_info=True)
            return fallback
        return fallback if value is None else value

    def _all_options(self) -> list:
        """全部配置项，按分组展示顺序排列（组内保持选项表里的顺序）。"""
        from ..core.config_options import GROUP_ORDER, OPTIONS

        order = {g: i for i, g in enumerate(GROUP_ORDER)}
        indexed = list(enumerate(OPTIONS))
        indexed.sort(key=lambda pair: (order.get(pair[1].group, 99), pair[0]))
        return [opt for _, opt in indexed]

    def _clamp_settings(self, options: list) -> None:
        if not options:
            self._settings_cursor = 0
            return
        self._settings_cursor = max(0, min(len(options) - 1, self._settings_cursor))

    def _open_settings(self) -> None:
        self._screen = Screen.SETTINGS
        self._notice = ()
        self._settings_cursor = 0
        groups = self._groups()
        if self._config_group not in groups and groups:
            self._config_group = groups[0]
        self._invalidate()

    def _cancel_mount(self) -> None:
        """停掉挂载轮询并清掉那张码。

        离开这一页就别再问网关了：二维码是一次性的，用户看不到的时候继续
        轮询既没有意义，还会让网关那边多一份会话。
        """
        if self._mount_task is not None:
            self._mount_task.cancel()
            self._mount_task = None
        self._mount_provider = None
        self._mount_challenge = None
        self._mount_state = ""
        self._mount_message = ""

    def _cycle_config_group(self, delta: int) -> None:
        groups = self._groups()
        if not groups:
            return
        if self._config_group not in groups:
            self._config_group = groups[0]
        self._config_group = next_group(groups, self._config_group, delta)
        # 换组等于换了一屏内容，光标必须归零，否则可能指到列表尾之外
        self._settings_cursor = 0
        self._notice = ()
        self._invalidate()

    def _move_settings(self, delta: int) -> None:
        options = self._visible_options()
        if not options:
            return
        self._settings_cursor = max(0, min(len(options) - 1, self._settings_cursor + delta))
        self._notice = ()
        self._invalidate()

    def _begin_edit_for(self, key: str) -> None:
        """直接进入某一项的编辑态（测试与快捷键复用）。"""
        opt = OPTIONS_BY_KEY.get(key)
        if opt is None:
            return
        if opt.kind == "minutes_list":
            self._open_point_edit(key)
            return
        self._editing_key = key
        self._input_text = ""
        self._edit_unit = opt.display_unit(opt.read(self.cfg))
        self._notice = ()
        self._screen = Screen.SETTING_EDIT
        self._invalidate()

    def _open_point_edit(self, key: str) -> None:
        """进「一组数」的逐条编辑屏。

        和文本编辑屏分开是两个理由：一是列表要一行一个点，行数随点数变化；
        二是"加一个"和"改一个"是不同的动作，混在同一个输入框里又要靠解析
        猜用户想干什么（这正是用户 m22010 不满的地方）。
        """
        opt = OPTIONS_BY_KEY.get(key)
        if opt is None:
            return
        self._editing_key = key
        self._point_cursor = 0
        self._point_input = ""
        self._point_input_on = False
        self._point_replace = None
        self._edit_unit = opt.display_unit(opt.read(self.cfg))
        self._notice = ()
        self._screen = Screen.POINT_EDIT
        self._invalidate()

    def _move_point(self, delta: int) -> None:
        """在点的列表里上下移动（输入态时不响应，避免边打字边跳）。"""
        if self._point_input_on:
            return
        opt = OPTIONS_BY_KEY.get(self._editing_key)
        if opt is None:
            return
        count = len(opt.format_lines(opt.read(self.cfg)))
        if not count:
            return
        self._point_cursor = max(0, min(count - 1, self._point_cursor + delta))
        self._notice = ()
        self._invalidate()

    def _start_point_input(self, *, replace: int | None = None) -> None:
        """开始输入一个点（``replace`` 给下标时是改那一条，否则是新增）。"""
        opt = OPTIONS_BY_KEY.get(self._editing_key)
        if opt is None:
            return
        self._point_input_on = True
        self._point_replace = replace
        self._point_input = ""
        if replace is not None:
            lines = opt.format_lines(opt.read(self.cfg))
            if 0 <= replace < len(lines):
                # 把那一条原样的数字回填进输入框：改一个写错的数不用重敲整行
                self._point_input = lines[replace].lstrip("- ").strip()
        self._notice = ()
        self._invalidate()

    def _cancel_point_input(self) -> None:
        if self._point_input_on:
            self._point_input_on = False
            self._point_input = ""
            self._point_replace = None
            self._notice = ()
            self._invalidate()
            return
        self._editing_key = ""
        self._screen = Screen.SETTINGS
        self._notice = ()
        self._invalidate()

    def _commit_point(self) -> None:
        """提交输入框里的那一个点：新增或替换。"""
        from ..core.config_options import add_point, drop_point

        opt = OPTIONS_BY_KEY.get(self._editing_key)
        if opt is None:
            return
        try:
            minutes = opt.parse_point(self._point_input, unit=self._edit_unit)
        except ConfigError as exc:
            self._notice = (str(exc),)
            self._invalidate()
            return
        old_cfg = self.cfg
        try:
            new_cfg = old_cfg
            if self._point_replace is not None:
                lines = opt.format_lines(opt.read(old_cfg))
                if 0 <= self._point_replace < len(lines):
                    # 先删旧点再加新点：改一个数不该变成"多了一个点"
                    old_minutes = opt.parse_point(
                        lines[self._point_replace].lstrip("- ").strip(), unit=None
                    )
                    if round(old_minutes, 6) == round(minutes, 6):
                        self._notice = ()
                        self._point_input_on = False
                        self._point_input = ""
                        self._point_replace = None
                        self._invalidate()
                        return
                    trimmed = drop_point(old_cfg, opt.key, old_minutes)
                    new_cfg = add_point(trimmed, opt.key, minutes)
            else:
                new_cfg = add_point(old_cfg, opt.key, minutes)
        except ConfigError as exc:
            self._notice = (str(exc),)
            self._invalidate()
            return
        try:
            self.ctx.settings.set(opt.key, opt.read(new_cfg))
        except OSError as exc:
            self._notice = (f"✗ 没能保存：{exc}",)
            self._invalidate()
            return
        was_replace = self._point_replace is not None
        self._apply_config(new_cfg)
        lines = opt.format_lines(opt.read(new_cfg))
        self._point_cursor = max(0, min(len(lines) - 1, self._point_cursor))
        self._point_input_on = False
        self._point_input = ""
        self._point_replace = None
        self._notice = (f"✓ 已{'改掉' if was_replace else '加上'}一个点",)
        self._invalidate()

    def _drop_current_point(self) -> None:
        """删掉选中的那个点（最后一个删不掉，会给出说明）。"""
        from ..core.config_options import drop_point

        opt = OPTIONS_BY_KEY.get(self._editing_key)
        if opt is None:
            return
        lines = opt.format_lines(opt.read(self.cfg))
        if not lines or self._point_cursor >= len(lines):
            return
        try:
            minutes = opt.parse_point(lines[self._point_cursor].lstrip("- ").strip(), unit=None)
            new_cfg = drop_point(self.cfg, opt.key, minutes)
        except ConfigError as exc:
            self._notice = (str(exc),)
            self._invalidate()
            return
        try:
            self.ctx.settings.set(opt.key, opt.read(new_cfg))
        except OSError as exc:
            self._notice = (f"✗ 没能保存：{exc}",)
            self._invalidate()
            return
        self._apply_config(new_cfg)
        remaining = opt.format_lines(opt.read(new_cfg))
        self._point_cursor = max(0, min(len(remaining) - 1, self._point_cursor))
        self._notice = (f"✓ 已删掉 {minutes:g} 分钟那一个",)
        self._invalidate()

    def _cycle_edit_unit(self, delta: int = 1) -> None:
        """在编辑态切换单位（没有可选单位时什么都不做）。"""
        opt = OPTIONS_BY_KEY.get(self._editing_key)
        if opt is None or len(opt.units) <= 1:
            return
        units = opt.units
        try:
            idx = units.index(self._edit_unit)
        except ValueError:
            idx = 0
        self._edit_unit = units[(idx + delta) % len(units)]
        self._invalidate()

    def _begin_edit(self) -> None:
        options = self._visible_options()
        if not options:
            return
        opt = options[self._settings_cursor]
        if opt.kind == "action":
            self._run_action(opt.key)
            return
        if opt.kind == "switch":
            # 开关项按 Enter 直接翻面：让用户在一屏里敲"开"再回车，是把
            # 二选一硬做成填空题。
            self._toggle_switch(opt)
            return
        if opt.kind == "minutes_list":
            # 一组数不走"填一行再解析"，改成逐条增删（用户 m22010）
            self._open_point_edit(opt.key)
            return
        self._editing_key = opt.key
        self._input_text = ""
        self._edit_unit = opt.display_unit(opt.read(self.cfg))
        self._notice = ()
        self._screen = Screen.SETTING_EDIT
        self._invalidate()

    def _mount_text(self, kind: str) -> str:
        """某个渠道的扫码行显示什么：已经有凭据就明说，别再让人扫。"""
        if not self._mount_ready(kind):
            return "Enter 打开二维码"
        if self._channel_dropped(kind):
            # 凭据还在，但收消息/推提醒的长连已经停了（微信会话过期会直接
            # ``return``）。这时候还写「已挂载」，用户会以为提醒还在送。
            return "已挂载 · 通道已断开，Enter 重开"
        who = self._setting("channels.qq_app_id", "") if kind == KIND_QQ else ""
        return f"已挂载 {who}".strip() if who else "已挂载 · Enter 查看"

    def _channel_dropped(self, kind: str) -> bool:
        """这个渠道的长连是不是已经退出了。

        ``None``（从没起过）与"还在跑"都算正常：没起之前谈不上断开，起过又
        结束才是真的掉了。任务以异常结束也算——两种都不再收消息。
        """
        task = self._wechat_task if kind == KIND_WECHAT else self._qq_task
        return task is not None and task.done() and not task.cancelled()

    def _action_texts(self) -> dict[str, str]:
        """动作项在设置页里显示什么：要把"现在是什么状态"说在前面。"""
        from ..core.config_options import (
            ACTION_LOGIN,
            ACTION_MOUNT_QQ,
            ACTION_MOUNT_WECHAT,
            ACTION_TEST_MESSAGE,
        )

        login = "已登录 · Enter 更换" if self._is_logged_in() else "未登录 · Enter 登录"
        return {
            ACTION_LOGIN: login,
            ACTION_MOUNT_WECHAT: self._mount_text(KIND_WECHAT),
            ACTION_MOUNT_QQ: self._mount_text(KIND_QQ),
            ACTION_TEST_MESSAGE: "Enter 发送",
        }

    def _run_action(self, key: str) -> None:
        """执行设置页里的动作项（登录 / 发送测试消息）。"""
        from ..core.config_options import (
            ACTION_LOGIN,
            ACTION_MOUNT_QQ,
            ACTION_MOUNT_WECHAT,
            ACTION_TEST_MESSAGE,
        )

        if key in (ACTION_MOUNT_WECHAT, ACTION_MOUNT_QQ):
            # 哪一行点进来的就扫哪个渠道的码：两个渠道可以在同一组里同时开着
            self._open_mount(KIND_WECHAT if key == ACTION_MOUNT_WECHAT else KIND_QQ)
            return
        if key == ACTION_LOGIN:
            # 进「选择登录方式」屏（列表页已经没有 l 这个入口了）
            self._notice = ()
            self._screen = Screen.LOGIN_METHOD
            self._invalidate()
            return
        if key == ACTION_TEST_MESSAGE:
            if self._busy:
                self._notice = ("✗ 正在忙，等这一轮结束再发测试消息",)
                self._invalidate()
                return
            self._launch("test-message", self._do_test_message)
            return
        self._notice = (f"✗ 未知动作：{key}",)
        self._invalidate()

    async def _do_test_message(self) -> None:
        """给所有已启用的渠道各发一条测试消息，逐条报告结果。"""
        from ..core.app.notifications import send_test

        wechat = getattr(self.ctx, "wechat", None)
        try:
            qq = getattr(self.ctx, "qq", None)
            results = await send_test(self.cfg.channels, wechat=wechat, qq=qq)
        except Exception as exc:  # 兜底：测试消息不能把程序搞崩
            log.exception("发送测试消息失败")
            self._notice = (f"✗ 发送测试消息失败：{type(exc).__name__}",)
            self._invalidate()
            return
        if not results:
            self._notice = ("✗ 没有可用的通知渠道：到「配置 → 通知」里打开一个并填好",)
            self._invalidate()
            return
        ok = sum(1 for _, sent, _ in results if sent)
        lines: list[str] = []
        for name, sent, detail in results:
            mark = "✓" if sent else "✗"
            lines.append(f"{mark} {name}：{detail}")
        self._notice = (f"测试消息：{ok}/{len(results)} 个渠道成功", *lines[:3])
        self._invalidate()

    def _toggle_switch(self, opt) -> None:
        """直接把一个开关项翻面并保存（不走编辑屏）。"""
        try:
            value = not bool(opt.read(self.cfg))
            new_cfg = apply_edit(self.cfg, opt.key, "开" if value else "关")
        except ConfigError as exc:
            self._notice = (str(exc),)
            self._invalidate()
            return
        try:
            self.ctx.settings.set(opt.key, value)
        except OSError as exc:
            self._notice = (f"保存失败：{exc}",)
            self._invalidate()
            return
        self._apply_config(new_cfg)
        # 翻译开关既在设置里也在列表/详情按 t，两处行为要一致：
        # 打开就把缺译文的补上，关闭只改显示。
        if opt.key == "translate.enable" and value:
            self._launch("translate", lambda: self._translate_view(quiet=True))
        self._notice = (f"{opt.label}：{'开' if value else '关'}",)
        self._invalidate()

    def _commit_edit(self) -> None:
        """保存一项配置：校验 → 写库 → 立即应用到运行期。"""
        if not self._editing_key:
            return
        text = self._input_text.strip()
        if not text:
            opt = OPTIONS_BY_KEY.get(self._editing_key)
            if opt is None or not opt.allow_empty:
                self._notice = ("请输入新值（直接按 Esc 可取消）",)
                self._invalidate()
                return
            # 可选字符串（地址、密码、令牌…）：清空是合法操作，用来"取消设置"

        try:
            new_cfg = apply_edit(self.cfg, self._editing_key, text, unit=self._edit_unit or None)
        except ConfigError as exc:
            self._notice = (str(exc),)
            self._invalidate()
            return
        except Exception as exc:  # 兜底：不能让配置界面把程序搞崩
            log.exception("修改配置失败")
            self._notice = (f"修改失败：{type(exc).__name__}",)
            self._invalidate()
            return

        # 落库
        try:
            self.ctx.settings.set(self._editing_key, self._value_of(new_cfg, self._editing_key))
        except OSError as exc:
            self._notice = (f"保存失败：{exc}",)
            self._invalidate()
            return

        self._apply_config(new_cfg)
        label = OPTIONS_BY_KEY[self._editing_key].label
        self._editing_key = ""
        self._input_text = ""
        self._screen = Screen.SETTINGS
        self._notice = (f"已保存：{label}",)
        self._invalidate()

    def _value_of(self, cfg: Config, key: str):
        section, _, name = key.partition(".")
        return getattr(getattr(cfg, section), name)

    def _apply_config(self, new_cfg: Config) -> None:
        """把新配置应用到正在运行的组件上（尽量不重启）。"""
        old = self.cfg
        self.cfg = new_cfg
        self.tz = ZoneInfo(new_cfg.timezone)

        # 网络参数与刷新间隔直接改对象属性
        self.ctx.api.timeout = new_cfg.fetch.timeout_seconds
        self.ctx.api._min_interval = new_cfg.fetch.min_request_interval
        self.ctx.api._retry = new_cfg.fetch.retry
        # 抖动也在这里。它同样只在 ``build_context`` 构造时传过一次，漏掉
        # 就等于"能在设置里改、改了不生效"——而它恰恰是防风控最常调的一项。
        self.ctx.api._jitter = new_cfg.fetch.jitter_ratio
        ref = self.ctx.refresher
        # 只写真实存在的属性。以前这里还写了 ``_list_max_pages`` / ``_exact_recheck``
        # / ``_max_recheck`` / ``_retention_days`` 四个——其中后两个根本不存在，
        # 一直被 suppress(AttributeError) 吞掉，看着像生效其实是空写。
        with contextlib.suppress(AttributeError):
            ref._detail_enabled = new_cfg.detail.enable
            ref._retention = timedelta(days=max(1, new_cfg.storage.observations_retention_days))
            ref._notify_points = normalize_points(new_cfg.notify.points_minutes)
            # 事件类通知的开关：改完立刻生效，不用等重启
            ref._notify_ended = new_cfg.notify.ended
            ref._notify_extended = new_cfg.notify.extended
            ref._notify_shortened = new_cfg.notify.shortened
            # 这两个开关也是构造时传的快照：关掉「出价被超过」却还在每轮拉
            # 站内消息、还在比对价格，正是这一项想省掉的事。
            ref._messages_enabled = new_cfg.messages.enable
            ref._outbid_by_price = new_cfg.notify.outbid_by_price

        # 通知的节奏：窗口改了就换掉侧栏口径，提醒点变了要立刻重算一遍，
        # 否则要等下一次刷新才生效（用户改完会盯着右侧队列看）。
        board = getattr(self.ctx, "notices", None)
        if board is not None:
            board.set_window(timedelta(minutes=new_cfg.notify.window_minutes))
            if normalize_points(old.notify.points_minutes) != normalize_points(
                new_cfg.notify.points_minutes
            ):
                self._sync_notices()

        # 渠道配置变了要重建"扇出器"：地址 / 超时 / 开关全都封在
        # ``build_notifier`` 里，不重建就还是启动时那一份。以前只在「扫码挂载
        # 成功」之后重建，于是手填 webhook 地址、打开开关，界面回"已保存"，
        # 到点却仍走 ``NullNotifier``——一个字都发不出去。
        if old.channels != new_cfg.channels:
            from ..core.app.notifications import build_notifier

            notifier = build_notifier(
                new_cfg.channels,
                wechat=getattr(self.ctx, "wechat", None),
                qq=getattr(self.ctx, "qq", None),
            )
            if board is not None:
                board._notifier = notifier
            # 刷新器另持一份（站内消息那条路径直接用），两边必须是同一个
            with contextlib.suppress(AttributeError):
                self.ctx.refresher._notifier = notifier

        # 定时刷新间隔变了就重建定时器（每轮 sleep 会重新读 fetch.jitter_ratio，
        # 所以抖动本身不用"重新配置"）
        if old.refresh.auto_seconds != new_cfg.refresh.auto_seconds:
            if self._auto_task is not None:
                self._auto_task.cancel()
                self._auto_task = None
            self._start_auto_refresh()

        self._rebuild()
        self._invalidate()

    # ------------------------------------------------------------ 列表操作

    @property
    def _page_size(self) -> int:
        # 按列表屏真实的固定行数（标题/表头/页脚/状态/提示）算数据行数，
        # 别拍脑袋留 9 行——那样 24 行终端只显示 15 条，白白浪费 5 行。
        _, rows = self._screen_size()
        rows -= 1  # 页脚固定占一行
        fixed = screens.list_fixed_height(status=self._status, busy=self._busy, notice=self._notice)
        return max(1, rows - fixed)

    def _screen_size(self) -> tuple[int, int]:
        """当前屏幕宽高。

        **优先问 prompt_toolkit**（``app.output.get_size()``）：它才是真正
        决定绘制区域的一方。用 ``shutil.get_terminal_size()`` 在部分 pty
        （例如 ``script`` 包装的会话）里可能与它不一致，导致我们按较宽的
        布局排版、而实际可用宽度更窄——表现就是列表被挤成"剩…"。
        """
        if self._app is not None:
            try:
                size = self._app.output.get_size()
                return max(1, size.columns), max(1, size.rows)
            except Exception:  # 输出对象不支持取尺寸时退回系统调用
                log.debug("读取应用尺寸失败", exc_info=True)
        ts = shutil.get_terminal_size((100, 24))
        return max(1, ts.columns), max(1, ts.lines)

    def _page_count(self) -> int:
        return max(1, (len(self._rows) + self._page_size - 1) // self._page_size)

    def _clamp_view(self) -> None:
        """把光标/页码收敛到当前尺寸下合法的范围。

        终端缩放的瞬间，page_size 会变（例如 24 行 15 条/页 → 40 行 31 条/页），
        旧页码可能已越界——不收敛的话会渲染出"整页空白"，而 Enter 还会打开
        一个屏幕上看不见的拍品。
        """
        if self._rows:
            self._cursor = max(0, min(len(self._rows) - 1, self._cursor))
        else:
            self._cursor = 0
        size = self._page_size
        self._page = max(0, min(self._page_count() - 1, self._page))
        if self._rows and not (self._page * size <= self._cursor < (self._page + 1) * size):
            self._page = self._cursor // size

    def _rebuild(self, *, keep_focus: bool = False) -> None:
        """按当前视图重建行列表。

        **所有视图都按结束时间倒序**（晚的在前）。这样三屏翻页方向一致，
        不用在视图之间切换时重新适应；「已结束」里刚结束的、关注里刚结束的
        都在最前面。没有结束时间（``PENDING``）的永远排最后。

        Args:
            keep_focus: 刷新类场景传 True——光标**跟着原来那条拍品走**，而不是
                停在同一个下标上。排序键是结束时间，一次刷新就可能把它挪走
                （自动延时会把它顶到最前），只夹下标的话用户盯着的那条会在他
                眼皮底下换成另一条拍卖，还没人告诉他。
        """
        # **所有视图一律按结束时间倒序**（新的在前）。真实反馈：「所有列表都是按
        # 结束时间倒序排序」。以前只有「已结束」倒序，关注中还是"快结束的优先"，
        # 两屏的翻页方向相反，翻起来要不停换脑子。
        focused = ""
        if keep_focus and self._rows:
            focused = self._rows[min(self._cursor, len(self._rows) - 1)].asin
        rows = sorted(
            (a for a in self.snap.auctions if matches(a, self._view)),
            key=lambda a: (a.end_time.at or _NEG_INF, a.asin),
            reverse=True,
        )
        self._rows = rows
        if focused:
            for idx, row in enumerate(rows):
                if row.asin == focused:
                    self._cursor = idx
                    break
            else:
                # 原来那条已经不在这个视图里了（比如刚结束、滚出关注中）：
                # 夹到最近的合法位置，别让光标指到空处。
                self._cursor = min(self._cursor, max(0, len(rows) - 1))
        else:
            self._cursor = min(self._cursor, max(0, len(rows) - 1))
        self._page = min(self._page, self._page_count() - 1)
        # 快照可能刚变过（刷新/清理/详情落库都会走到这里）：重算结束提醒。
        # sync_ending 是全量重建，幂等且 O(拍品数×复查点)，每次重建都同步
        # 也毫无压力，换来的是"任何路径改了数据，提醒都跟着最新走"。
        self._sync_notices()

    def _view_counts(self) -> dict[ListView, int]:
        return counts(self.snap.auctions)

    def _cycle_view(self, delta: int = 1) -> None:
        """在视图之间循环切换。

        切换后**光标回到首行**：视图之间没有可比的位置关系，沿用旧下标会让
        用户以为"还是刚才那条"，实际已经换了一条完全不相干的拍卖。
        """
        self._view = next_view(self._view, delta)
        self._cursor = 0
        self._page = 0
        self._notice = ()
        self._rebuild()
        self._invalidate()

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        self._cursor = max(0, min(len(self._rows) - 1, self._cursor + delta))
        self._page = self._cursor // self._page_size
        self._invalidate()

    def _turn_page(self, delta: int) -> None:
        old_page = self._page
        self._page = max(0, min(self._page_count() - 1, self._page + delta))
        # 已在首/末页时按 p/n 不该把光标弹回页首——那会让用户"按一下什么都没变、
        # 选区却跳了"。
        if self._page != old_page:
            self._cursor = min(self._page * self._page_size, max(0, len(self._rows) - 1))
        self._invalidate()

    def _open_detail(self) -> None:
        if not self._rows:
            return
        st = self._rows[self._cursor]
        self._detail = st
        try:
            self._detail_obs = self.ctx.observations.recent(st.asin, 5)
        except Exception:  # 观测库读失败不该阻止看详情
            log.debug("读取观测失败", exc_info=True)
            self._detail_obs = []
        self._load_detail_raw(st.asin)
        # 开了翻译就直接翻：用户不该为"帮我翻一下"再按一次 t。
        if self._translate_on() and st.asin not in self._translations:
            self._launch("translate", self._translate_current)
        self._screen = Screen.DETAIL
        self._invalidate()
        # 还没有详情（从没拉到过、或详情一直失败）时自动拉一次：用户点进来就是
        # 想看它，不拉的话他会盯着空价格，还得先知道"要按 r"。
        # 已经有过详情的**不自动拉**——那属于后台轮询的活，进详情就看缓存，
        # 想看最新的按 r。真实反馈：「详情也都改成只有按 enter 进去了且没有详情
        # 的话再去获取详情」。
        if self._has_no_detail(st) and not self.ctx.refresher.running:
            self._busy = "刷新中 · 正在拉详情…"
            self._notice = ("⏳ 正在获取详情…",)
            self._launch("detail", lambda item=st: self._do_fetch_this_detail(item))
            self._invalidate()

    def _load_detail_raw(self, asin: str, *, keep_scroll: bool = False) -> None:
        """读这个拍品的详情原始 JSON（描述、图片在里面）。

        读失败只当作"没有详情"——描述看不到不该让详情屏打不开。

        Args:
            keep_scroll: 保留当前的描述滚动位置。后台刷新后重读时用：用户正
                读到描述中间，被刷新把位置弹回顶部很烦人。
        """
        if not keep_scroll:
            self._desc_scroll = 0
        try:
            self._detail_raw = self.ctx.mirror.detail_raw(asin)
        except Exception:  # 镜像是辅助数据，读不到就不显示描述
            log.debug("读取详情原始 JSON 失败 %s", asin, exc_info=True)
            self._detail_raw = {}
        # 规则属性跟原始 JSON 一起读：同一次进屏、同一条镜像，不该分两趟。
        try:
            stored = self.ctx.mirror.detail_item(asin)
        except Exception:
            log.debug("读取详情镜像失败 %s", asin, exc_info=True)
            stored = None
        self._detail_auto_extend = None if stored is None else stored.auto_extend
        self._detail_early_termination = None if stored is None else stored.early_termination

    def _desc_text(self) -> str:
        """当前该显示的描述：开了翻译且有译文就显示译文，否则原文。"""
        if self._detail is None:
            return ""
        if self._translate_on():
            cached = self._translations.get(self._detail.asin)
            if cached is not None and cached[1]:
                return cached[1]
        return description_of(self._detail_raw)

    def _title_text(self) -> str:
        """当前该显示的标题：开了翻译且有译文就显示译文，否则空串（回落到 st.name）。"""
        if self._detail is None or not self._translate_on():
            return ""
        cached = self._translations.get(self._detail.asin)
        return cached[0] if cached else ""

    def _desc_is_translated(self) -> bool:
        return (
            self._detail is not None
            and self._translate_on()
            and self._detail.asin in self._translations
        )

    def _refresh_current_detail(self) -> None:
        """详情屏按 r：只重拉当前这一条，刷新完仍停在详情屏。

        和 ``_open_detail`` 里的自动补数据共用一个执行体，区别只在"什么时候触发"：
        进详情且没有详情时自动拉一次；用户按 r 时不管有没有都重拉。
        """
        if self._detail is None:
            return
        if self._task_running("detail"):
            self._notice = ("详情正在刷新，等这一条回来",)
            self._invalidate()
            return
        st = self._detail
        self._busy = "刷新中 · 正在拉这条详情…"
        # 按下去要立刻有反应：只改 busy 的话，详情屏没有遮罩（已有数据），
        # 用户看到的画面几乎没动，会怀疑按键没生效。
        self._notice = (f"⏳ 正在刷新「{st.name}」…",)
        self._launch("detail", lambda item=st: self._do_fetch_this_detail(item))
        self._invalidate()

    # ------------------------------------------------------------ 翻译

    def _translate_on(self) -> bool:
        """全局翻译开关（存在 settings 里，重启后仍生效）。"""
        return bool(self.cfg.translate.enable)

    def _load_translations(self) -> None:
        """把库里已缓存的译文读进内存。

        翻译是网络请求，界面每帧都去查库/发请求显然不行：启动时整批读一次，
        之后由翻译流程增量更新。读失败只当没有缓存（大不了重翻）。
        """
        store = getattr(self.ctx, "translations", None)
        if store is None:
            return
        try:
            self._translations = {k: (v.title, v.desc) for k, v in store.all().items()}
        except Exception:
            log.debug("读取译文缓存失败", exc_info=True)
            self._translations = {}

    def _translation_titles(self) -> dict[str, str] | None:
        """列表用的 ``asin -> 译文标题``；没开翻译返回 None（调用方用原文）。"""
        if not self._translate_on():
            return None
        return {k: v[0] for k, v in self._translations.items() if v[0]}

    def _raw_desc(self, asin: str) -> str:
        """从镜像里读这条拍品的描述原文（没有就空串）。"""
        try:
            raw = self.ctx.mirror.detail_raw(asin)
        except Exception:
            log.debug("读取详情原始 JSON 失败 %s", asin, exc_info=True)
            return ""
        return description_of(raw)

    async def _translate_one(self, asin: str, title: str, desc: str) -> tuple[str, str] | None:
        """翻一条，返回 (标题, 描述)；失败返回 None。

        优先走 ``ctx.translator``（带落库缓存）；没有时退回直接调接口——
        只为老测试与降级路径，正常运行时一定有 translator。
        """
        translator = getattr(self.ctx, "translator", None)
        if translator is not None:
            got = await translator.translate(asin, title=title, desc=desc)
            return None if got is None else (got.title, got.desc)
        try:
            return await self.ctx.api.translate(title=title, desc=clip_for_translation(desc))
        except (AuthExpiredError, MerukiTrackerError):
            return None
        except Exception:
            log.debug("翻译失败 %s", asin, exc_info=True)
            return None

    def _toggle_translate(self) -> None:
        """全局开关翻译：列表和详情一起生效，并落库（重启后仍生效）。"""
        want = not self._translate_on()
        try:
            new_cfg = apply_edit(self.cfg, "translate.enable", "开" if want else "关")
        except ConfigError as exc:
            self._notice = (str(exc),)
            self._invalidate()
            return
        try:
            self.ctx.settings.set("translate.enable", want)
        except OSError as exc:
            self._notice = (f"保存失败：{exc}",)
            self._invalidate()
            return
        self._apply_config(new_cfg)
        if not want:
            self._notice = ("翻译已关闭，显示原文",)
            self._invalidate()
            return
        self._notice = ("翻译已开启",)
        self._invalidate()
        # 详情屏：只翻这一条；列表屏：把当前这一页缺译文的补上。
        if self._screen is Screen.DETAIL and self._detail is not None:
            self._launch("translate", self._translate_current)
        else:
            self._launch("translate", self._translate_view)

    async def _translate_current(self) -> None:
        """翻当前详情这一条（开翻译后进详情/刷新详情都会走这里）。"""
        st = self._detail
        if st is None or not self._translate_on() or st.asin in self._translations:
            return
        got = await self._translate_one(st.asin, st.name, description_of(self._detail_raw))
        if got is not None:
            self._translations[st.asin] = got
        self._invalidate()

    async def _translate_view(self, *, quiet: bool = False) -> None:
        """把当前视图里还没有译文的拍品逐条翻掉。

        一条失败不中断：10 条里有 1 条接口抽风，不能丢下另外 9 条。

        ``quiet=True`` 用于刷新后自动补译文：全部都有译文时什么都不提示——
        每 3 分钟弹一次"都有译文了"是噪音。
        """
        todo = [a for a in self._rows if a.asin not in self._translations]
        if not todo:
            if not quiet:
                self._notice = ("当前视图的拍品都有译文了",)
                self._invalidate()
            return
        done = failed = 0
        try:
            for idx, st in enumerate(todo, start=1):
                self._busy = f"翻译 {idx}/{len(todo)}"
                self._invalidate()
                got = await self._translate_one(st.asin, st.name, self._raw_desc(st.asin))
                if got is None:
                    failed += 1
                else:
                    self._translations[st.asin] = got
                    done += 1
                    self._invalidate()
        except asyncio.CancelledError:
            raise
        finally:
            self._busy = ""
        note = f"✓ 翻译完成：{done} 条"
        if failed:
            note += f"，{failed} 条没翻出来（可再按 t 重试）"
        self._notice = (note,)
        self._invalidate()

    async def _do_fetch_this_detail(self, st: AuctionState) -> None:
        """进详情时给这一条补一次详情（只在"还没有详情"时用）。

        这不是"详情刷新"——那个已经去掉了。这只是补数据：从没拉到过详情的拍品，
        用户点进来总得看到点什么，不能让他盯着空价格还得先猜"要按 r"。
        """
        try:
            new_state = await self.ctx.refresher.refresh_detail_of(st)
        except asyncio.CancelledError:
            raise
        except AuthExpiredError:
            self._notice = ("✗ 登录已失效，按 Esc 返回列表，再按 s 进配置重新登录",)
        except MerukiTrackerError as exc:
            self._notice = (f"✗ 详情没拿到：{exc}",)
        except Exception as exc:
            log.exception("进详情时自动拉详情失败")
            self._notice = (f"✗ 详情没拿到（{type(exc).__name__}）",)
        else:
            self._rows = [new_state if r.asin == new_state.asin else r for r in self._rows]
            self._persist_detail_change(new_state)
            # 刷新在跑时 _persist_detail_change 拒绝了写回并留了"等这轮结束"的提示，
            # 别再用"✓ 已刷新"把它盖掉——那样用户以为已经存下了，实际会被盖回。
            refresh_running = self._refresh_in_flight()
            # **只有用户还停在这条拍品的详情屏上**才动这一屏的字段。
            # 以前这里只在赋值 `_detail` 时比了 asin，但紧接着的 `_notice` /
            # `_load_detail_raw` / 两个「提前结束·自动延长」标志位却无条件写——
            # 用户在 A1 触发自动拉详情、Esc 回列表、点进 A2，等 A1 的慢请求回来
            # 时，A2 的详情屏就会显示 A1 的描述和徽标（标题价格却是 A2 的）。
            # 真实网络下这个时序是常态，不是边角。
            if self._detail is None or self._detail.asin != new_state.asin:
                return
            self._detail = new_state
            if not refresh_running:
                self._notice = (self._detail_change_note(st, new_state),)
            self._reload_detail_obs()
            # 描述跟着详情一起回来：不重读的话这一屏还是"没有描述"，
            # 得等用户退出去再进来才刷出来。
            self._load_detail_raw(new_state.asin)
            # 刷新详情时顺手把译文也拉一遍（用户要求：刷新就带翻译）。
            # 标题可能被卖家改过，所以这里不看缓存里有没有——有也重翻一次。
            if self._translate_on():
                got = await self._translate_one(
                    new_state.asin, new_state.name, description_of(self._detail_raw)
                )
                if got is not None:
                    self._translations[new_state.asin] = got
        finally:
            self._busy = ""
            self._invalidate()

    def _detail_change_note(self, before: AuctionState, after: AuctionState) -> str:
        """刷完给一句话说明这次拿到了什么。

        只说"已刷新"等于没说——用户按 r 是想知道**有没有变**。所以把变了的字段
        点名说清楚；一个都没变也明说，别让人怀疑是不是根本没拉到。
        """
        if after.detail_failures:
            return "✗ 这次没拿到有效详情，已保留上次的数据"
        changes: list[str] = []
        if after.latest_bid != before.latest_bid:
            changes.append(f"当前价 {screens.price_text(after.latest_bid)}")
        if after.buy_now_price != before.buy_now_price:
            changes.append(f"一口价 {screens.fixed_price_text(after.buy_now_price)}")
        if after.end_time.at != before.end_time.at:
            changes.append(f"结束时间 {render_end_time(after, self.tz)}")
        stamp = format_time(datetime.now(UTC), self.tz)
        if not changes:
            return f"✓ 已刷新（{stamp}），数据没有变化"
        return f"✓ 已刷新（{stamp}）：" + "，".join(changes)

    @staticmethod
    def _has_no_detail(st: AuctionState) -> bool:
        """这条拍品是不是"还没有详情"。

        判据是**两个价格都没有且详情从没成功过**：一口价商品没有当前价
        （``latest_bid`` 为空）但有一口价，那也算已经有详情了，不该再打一次接口。
        """
        return not st.latest_bid and not st.buy_now_price and st.detail_failures == 0

    # ------------------------------------------------------------ 详情刷新

    def _reload_detail_obs(self) -> None:
        """重读当前拍品的观测记录。

        真实反馈：「进入详情之后刷新，观测列表不会变」。以前刷新只同步了
        ``_detail`` 这个状态对象，观测是进详情那一刻读的一份拷贝，之后再也不动。
        """
        if self._detail is None:
            self._detail_obs = []
            return
        try:
            self._detail_obs = self.ctx.observations.recent(self._detail.asin, 5)
        except Exception:  # 观测库读失败不该阻止看详情
            log.debug("读取观测失败", exc_info=True)
            self._detail_obs = []

    def _persist_detail_change(self, new_state: AuctionState) -> None:
        """把改过的单个拍品写回快照（写失败只提示，不能让界面崩）。

        详情屏看得见的数据必须和库里一致：不写的话退出程序就丢了，
        下次启动看到的还是旧价格。

        刷新在跑时**拒绝写回**：刷新整场只认它开工那一刻的 ``previous`` 快照，
        最后一整表覆盖。这会儿写进去的值会被那轮结果原样盖掉，界面内存却停在
        手工值，库和界面短暂不一致（审计复现过）。和清理用同一道守卫——
        刷新只要几秒，等它结束比做增量合并简单得多，也不会算错。
        """
        if self._refresh_in_flight():
            self._notice = ("正在刷新，等这一轮结束再保存详情改动", "改动会被这轮刷新盖掉")
            return
        updated = replace(
            self.snap,
            auctions=tuple(
                new_state if a.asin == new_state.asin else a for a in self.snap.auctions
            ),
        )
        try:
            self.ctx.snapshots.write(updated, allow_empty=True)
        except (OSError, SchemaVersionError) as exc:
            log.warning("详情变更写快照失败", exc_info=True)
            self._notice = (f"✗ 详情没能保存：{exc}", "数据未改动，可以稍后重试")
            return
        self.snap = updated

    def _refresh_detail_view(self) -> None:
        """刷新结束后，把正在看的详情同步成最新数据。

        详情屏持有的是 ``AuctionState`` 快照对象；刷新会整份替换 ``_rows``，
        不同步的话用户会一直看着刷新前的旧价格，直到退出再进一次。
        """
        if self._detail is None:
            return
        target = self._detail.asin
        for row in self._rows:
            if row.asin == target:
                self._detail = row
                break
        # 描述与两个规则属性也要重读：只换 ``AuctionState`` 的话，用户会看到
        # "价格更新了、卖家新写的描述和自动延长标记还是旧的"，得退出去再进来。
        # 手动 ``r`` 那条路径一直有这一步，后台刷新这条以前漏了。
        self._load_detail_raw(target, keep_scroll=True)
        # 观测也要重读：只同步状态对象的话，详情屏底下的观测表会一直停在
        # 进屏那一刻的样子，看起来像"刷新没生效"。
        self._reload_detail_obs()

    # ------------------------------------------------------------ 清理

    def _clean_targets(self) -> list[AuctionState]:
        """当前视图下会被清理的拍品。"""
        return [a for a in self.snap.auctions if matches(a, self._view)]

    def _refresh_in_flight(self) -> bool:
        """有刷新正在跑吗。

        刷新全程只认"开始那一刻"的快照：它拿 ``previous`` 拼出新快照，最后
        一整表覆盖回去。所以刷新途中清理掉的拍品，会被那一轮原封不动写回来
        ——界面提示"✓ 已清理"，下一秒它们又都回来了（实测复现过）。

        刷新只要几秒，等它结束比做增量合并简单得多，也不会算错。
        """
        return self._task_running("refresh") or bool(getattr(self.ctx.refresher, "running", False))

    def _open_clean_confirm(self) -> None:
        """进入清理确认。

        只允许在 CLEANABLE 视图里清理（「已结束」和「无法确认」）——"关注中"
        的拍品还在跑，没有"清理"的语义；"全部"视图混着活数据，一键清理风险太高。
        「无法确认」不是终态，但用户拍板要一个手动放弃的出口，所以也放行。
        """
        if self._view not in CLEANABLE:
            allowed = "、".join(f"「{LABELS[v]}」" for v in CLEANABLE)
            self._notice = (f"只能在{allowed}视图里清理；按 Tab 切过去",)
            self._invalidate()
            return
        if self._refresh_in_flight():
            self._notice = ("正在刷新，等这一轮结束再清理", "这轮刷新会把刚清掉的记录写回来")
            self._invalidate()
            return
        targets = self._clean_targets()
        if not targets:
            self._notice = (f"「{LABELS[self._view]}」视图里没有可清理的拍品",)
            self._invalidate()
            return
        self._notice = ()
        self._screen = Screen.CLEAN_CONFIRM
        self._invalidate()

    def _do_clean(self) -> None:
        """执行清理：从快照里移除当前视图的拍品。

        只动 ``snapshot``（本地记录），不碰网站上的关注，也不删观测历史——
        用户以后重新关注同一条，还能看到过去的价格曲线。
        """
        targets = self._clean_targets()
        if not targets:
            self._screen = Screen.LIST
            self._invalidate()
            return
        # 确认屏开着的时候，定时刷新可能正好起来了：那这一轮的快照会把清理
        # 结果盖掉，宁可退回列表让用户等一下。
        if self._refresh_in_flight():
            self._screen = Screen.LIST
            self._notice = ("正在刷新，等这一轮结束再清理", "这轮刷新会把刚清掉的记录写回来")
            self._invalidate()
            return
        doomed = {a.asin for a in targets}
        remaining = tuple(a for a in self.snap.auctions if a.asin not in doomed)
        updated = replace(self.snap, auctions=remaining)
        try:
            # 这里**显式**允许写空：清理本来就可能把最后一个拍品删掉，
            # EmptySnapshotError 保护的是"采集异常导致整份清空"，不是这个场景。
            self.ctx.snapshots.write(updated, allow_empty=True)
        except (OSError, SchemaVersionError) as exc:
            log.warning("清理后写快照失败", exc_info=True)
            self._notice = (f"✗ 清理没能保存：{exc}", "数据未改动，可以稍后重试")
            self._invalidate()
            return
        self.snap = updated
        self._cursor = 0
        self._page = 0
        self._rebuild()
        self._screen = Screen.LIST
        self._notice = (f"✓ 已清理 {len(targets)} 个{LABELS[self._view]}的拍品",)
        self._invalidate()

    # ------------------------------------------------------------ 扫码挂载

    def _mount_ready(self, kind: str) -> bool:
        """这个渠道是不是已经有可用凭据了。

        凭据可能是扫码换来的，也可能是用户自己在设置里一个字一个字填的
        （QQ 的 AppID / AppSecret 就能手填）。手填完再被要求扫一次码很荒唐，
        所以**只要有凭据就不再取码**，`r` 才是"我要换一个"。
        """
        if kind == "qq":
            return bool(self._setting("channels.qq_app_id", "")) and bool(
                self._setting("channels.qq_app_secret", "")
            )
        return bool(self._setting("channels.wechat_token", ""))

    def _open_mount(self, kind: str = "", *, force: bool = False) -> None:
        """打开「扫码挂载」屏（配置区的一屏）。

        Args:
            kind: 扫哪个渠道的码。配置页里每个渠道有自己的扫码行，点哪行就扫哪个；
                留空（老入口 / 只有一条路时）按启用情况挑一个，QQ 优先。
            force: 已经有凭据时也重新取码。``False`` 时**不取码**：只把
                「已挂载」摆出来，想看码得自己按 ``r``。否则每次点进来都换一张
                新码，等于逼着已经配好的人再扫一次。
        """
        provider = build_provider(self.cfg.channels, kind)
        if provider is None:
            self._screen = Screen.SETTINGS
            self._notice = ("还没开机器人渠道；在「通知」组里打开「QQ 机器人」或「微信机器人」",)
            self._invalidate()
            return
        # 重新取码时先把上一轮的轮询停掉，否则会有两个循环同时问网关
        if self._mount_task is not None:
            self._mount_task.cancel()
            self._mount_task = None
        kind = getattr(provider, "kind", kind)
        self._mount_kind = kind
        self._mount_provider = provider
        self._mount_challenge = None
        self._screen = Screen.MOUNT
        self._notice = ()
        if not force and self._mount_ready(kind):
            self._mount_state = "success"
            who = self._setting("channels.qq_app_id", "") if kind == "qq" else ""
            # 状态行左边已经有「已挂载」，这里只补是哪个机器人
            self._mount_message = f"{provider.label} {who}".strip()
            self._notice = ("按 r 重新取码，换一个账号",)
            self._invalidate()
            return
        self._mount_state = "pending"
        self._mount_message = ""
        self._mount_task = self._spawn(self._mount_loop(provider), name="mount")
        self._invalidate()

    async def _mount_loop(self, provider: Any) -> None:
        """取码 → 每 5 秒轮询，直到成功/失败/用户离开。

        节奏固定 5 秒：微信二维码有效期约 150 秒，5 秒一次既够快又不会把网关
        打疼（这是用户坐在屏幕前等的一次性动作，不需要更密）。
        """
        try:
            self._mount_state = "pending"
            self._mount_message = "正在取二维码…"
            self._invalidate()
            # provider 是同步的（HTTP 用 urllib 阻塞），丢线程里别卡住界面
            challenge = await asyncio.to_thread(provider.fetch)
            self._mount_challenge = challenge
            self._mount_message = ""
            self._mount_state = "pending"
            self._invalidate()
            while True:
                await asyncio.sleep(_MOUNT_POLL_SECONDS)
                status = await asyncio.to_thread(provider.poll, challenge)
                self._mount_state = status.state
                self._mount_message = status.message
                if status.state == "success":
                    self._on_mounted(provider, status)
                    return
                if status.state == "failed":
                    self._invalidate()
                    return
                self._invalidate()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("扫码挂载失败", exc_info=True)
            self._mount_state = "failed"
            self._mount_message = str(exc)
            self._invalidate()
        finally:
            if self._mount_provider is provider:
                self._mount_provider = None
            self._mount_task = None

    def _on_mounted(self, provider: Any, status: Any) -> None:
        """扫码成功：把凭据存进设置、换掉活的客户端、起收消息循环。"""
        from ..core.app.wechat import ContextStore, IlinkClient, WeChatBot

        credential = getattr(status, "credential", "")
        if not credential:
            self._notice = ("✓ 已扫码，但服务端没给凭据，请重新取码",)
            self._invalidate()
            return
        if getattr(provider, "kind", "") == "qq":
            self._save_qq_credentials(credential)
            return
        try:
            self.ctx.settings.set("channels.wechat_token", credential)
            base_url = getattr(status, "base_url", "") or self.cfg.channels.wechat_base_url
            if base_url and base_url != self.cfg.channels.wechat_base_url:
                self.ctx.settings.set("channels.wechat_base_url", base_url)
        except Exception:
            log.warning("挂载凭据没写进设置", exc_info=True)
        # 换掉上下文里的通道：通知板与刷新器都直接持有那个 notifier
        conn = getattr(self.ctx, "db_conn", None)
        if conn is not None:
            bot = WeChatBot(
                client=IlinkClient(
                    credential,
                    base_url=getattr(status, "base_url", "") or self.cfg.channels.wechat_base_url,
                    timeout=self.cfg.channels.wechat_timeout_seconds,
                ),
                store=ContextStore(conn),
            )
            self.ctx.wechat = bot
            from ..core.app.notifications import build_notifier

            notifier = build_notifier(self.cfg.channels, wechat=bot)
            if self.ctx.notices is not None:
                self.ctx.notices._notifier = notifier
        bot = getattr(self.ctx, "wechat", None)
        if bot is not None:
            self._start_wechat_loop(bot)
        qq = getattr(self.ctx, "qq", None)
        if qq is not None:
            self._start_qq_loop(qq)
        self._notice = (f"✓ {provider.label}已挂载：{status.account}",)
        self._invalidate()

    def _save_qq_credentials(self, credential: str) -> None:
        """QQ 扫码换回的是 AppID + AppSecret 两个值，存起来等发消息用。"""
        try:
            data = json.loads(credential)
            app_id = str(data.get("app_id") or "")
            app_secret = str(data.get("app_secret") or "")
        except (ValueError, AttributeError):
            log.warning("QQ 凭据格式不对，没存")
            self._notice = ("✓ 扫码成功，但凭据读不出来，请重新取码",)
            self._invalidate()
            return
        if not app_id or not app_secret:
            self._notice = ("✓ 扫码成功，但缺 AppID/AppSecret，请重新取码",)
            self._invalidate()
            return
        try:
            self.ctx.settings.set("channels.qq_app_id", app_id)
            self.ctx.settings.set("channels.qq_app_secret", app_secret)
        except Exception:
            log.warning("QQ 凭据没写进设置", exc_info=True)
            self._notice = ("✗ QQ 凭据没存下来，检查数据库是否可写",)
            self._invalidate()
            return
        # 换掉上下文里的通道并起长连：openid 只出现在用户先说话的事件里，
        # 不连 WebSocket 就永远没有会话可推。
        conn = getattr(self.ctx, "db_conn", None)
        if conn is not None:
            from ..core.app.notifications import build_notifier
            from ..core.app.qqchannel import QqBot, QqBotClient, session_store

            bot = QqBot(
                client=QqBotClient(
                    app_id=app_id,
                    app_secret=app_secret,
                    api_base=self.cfg.channels.qq_api_base,
                    timeout=self.cfg.channels.qq_timeout_seconds,
                ),
                store=session_store(conn),
            )
            self.ctx.qq = bot
            notifier = build_notifier(
                self.cfg.channels, wechat=getattr(self.ctx, "wechat", None), qq=bot
            )
            if self.ctx.notices is not None:
                self.ctx.notices._notifier = notifier
        qq = getattr(self.ctx, "qq", None)
        if qq is not None:
            self._start_qq_loop(qq)
        self._notice = (f"✓ QQ 机器人已挂载：{app_id}",)
        self._invalidate()

    def _start_wechat_loop(self, bot: Any) -> None:
        """收消息循环：拿到 context_token 才能在之后主动推提醒。"""
        if self._wechat_task is not None and not self._wechat_task.done():
            return
        task = self._spawn(bot.run(), name="wechat")
        task.add_done_callback(
            lambda t: self._channel_stopped("微信提醒通道", t, bot, self._start_wechat_loop)
        )
        self._wechat_task = task

    def _start_qq_loop(self, bot: Any) -> None:
        """QQ 网关长连：收 ``C2C_MESSAGE_CREATE`` 才能知道该推给谁。

        只建客户端不连 WebSocket 是发不出去的——openid 只出现在用户先说话的
        事件里，不连就永远没有会话可推。
        """
        if self._qq_task is not None and not self._qq_task.done():
            return
        task = self._spawn(bot.run(), name="qq")
        task.add_done_callback(
            lambda t: self._channel_stopped("QQ 机器人通道", t, bot, self._start_qq_loop)
        )
        self._qq_task = task

    def _channel_stopped(
        self,
        what: str,
        task: asyncio.Task,
        bot: Any = None,
        restart: Any = None,
    ) -> None:
        """通道长连退出时：能自动重连就重连，否则在列表上闪一句。

        ``bot`` / ``restart`` 是可选参数——传了就问 ``bot.can_retry()``，为真
        直接重启长连（配置页那行不会显示"断开"，用户无感）；为假或没传才走
        提示。微信会话过期（errcode -14）token 已作废，``can_retry()`` 为假，
        必须提示重新扫码；QQ 的 ``run()`` 自带重连，走到这里基本是关程序。

        程序退出时这些任务是被 cancel 掉的，那条路既不重连也不弹提示——
        用户自己关的。
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            log.warning("%s已退出（没有报错，通常是会话过期或凭据被换掉）", what)
        else:
            log.warning("%s异常退出: %s", what, exc)
        if bot is not None and restart is not None and isinstance(bot, RetryableChannel):
            try:
                retry = bool(bot.can_retry())
            except Exception:  # 通道实现方的问题不能拖垮界面
                log.warning("%s 的 can_retry() 抛错，按不可重试处理", what, exc_info=True)
                retry = False
            if retry:
                log.info("%s退出，按通道要求自动重连", what)
                restart(bot)
                return
        self._notice = (f"⚠ {what}已断开：按 s 进配置重新扫码",)
        self._invalidate()

    def _leave_subscreen(self) -> None:
        """从子屏返回；顺手取消还在跑的登录任务。

        **启动阶段不再直接退出程序**：从粘贴/浏览器登录/确认屏退出，一律回到
        「选择登录方式」，让用户还能改用手动粘贴或重试；只有在该屏上按
        Esc/q 才算放弃启动。
        """
        if self._screen is Screen.LOGIN_PASTE and self._paste_task is not None:
            self._paste_task.cancel()
            self._paste_task = None
            self._busy = ""
        if self._screen is Screen.LOGIN_WAIT and self._action_task is not None:
            self._action_task.cancel()
            self._action_task = None
        if self._screen is Screen.COOKIE_CONFIRM:
            self._pending_cookie = ""
        if self._screen is Screen.MOUNT:
            # 挂载屏在配置区，永远从配置页进、回配置页（列表页没有入口了）
            self._cancel_mount()
            self._screen = Screen.SETTINGS
            self._invalidate()
            return
        if self._startup_login and self._screen is not Screen.LOGIN_METHOD:
            self._screen = Screen.LOGIN_METHOD
        else:
            self._screen = Screen.LIST
        self._invalidate()

    # ------------------------------------------------------------ 动作

    def _launch(self, name: str, make_coro) -> None:
        """起一个后台任务，收进 ``self._tasks`` 以便退出时统一取消。

        串行队列整套删掉了（用户 m19845）：刷新互斥由 ``refresher.refresh()``
        自己的 busy 守卫兜住，翻译/补详情写的是内存或各自的表，不存在「两个
        任务整份覆盖同一份快照」的问题。``make_coro`` 是无参工厂——每次调用
        现造协程，别提前把协程对象挂着。
        """
        self._spawn(make_coro(), name=name)
        self._invalidate()

    def _task_running(self, *names: str) -> bool:
        """有没有同名的后台任务还在跑。

        模态屏上的"提交"类按键（发验证码 / 登录 / 补详情）以前没有这道守卫：
        连按两下 Enter 就是两条短信、两次 ``login_by_sms``。``r`` 刷新列表那边
        一直有 ``_busy`` 守卫，这些入口漏了。
        """
        return any(not t.done() and t.get_name() in names for t in self._tasks)

    def _spawn(self, coro, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_refresh(self) -> None:
        # **所有视图的刷新是同一件事**，包括「已结束」和「全部」。
        #
        # 「已结束」不再是一个需要单独翻 50 页去同步的桶：它的内容来自拍品从
        # 「进行中」滚过去（advance 判终态）+ 本地已有的历史。真实反馈：
        # 「已结束就依赖我们关注中的状态滚动，已结束、全部这两个页面的刷新都按
        # 关注中的刷新逻辑来」。
        #
        # 串行队列删掉后，手动/自动刷新都可能撞在一起：先看 refresher 自己的
        # busy 守卫，正在采就明确说一声，不起第二个任务（也别让 _busy 被覆盖）。
        #
        # 手动刷新连**详情**一起拉：用户按 r 就是要看最新价格，不该"列表更新了
        # 但价格还是三小时前的"。真实反馈：「现在列表的刷新带一下获取详情吧」。
        if self._busy or self.ctx.refresher.running:
            self._notice = ("已在刷新中，等这一轮结束再按 r",)
            self._invalidate()
            return
        self._launch("refresh", lambda: self._do_refresh())
        self._busy = "刷新中 · 正在准备…"
        # 已经有一份可用数据时就不遮罩：用户能继续看旧列表，
        # 采完再整体替换。只有"什么都还没有"（首次启动）才遮。
        self._overlay = not self.snap.auctions
        self._notice = ()
        self._invalidate()

    async def _do_refresh(self, *, from_auto: bool = False) -> None:
        # 前缀统一写「刷新中」：以前手动刷新只显示「进行中 第 2 页 · 已拉 10 条」，
        # 看不出这是刷新动作还是某种常驻状态；后台刷新则完全无声。
        #
        # 手动与定时现在跑的是同一套逻辑：每轮都全量拉详情、以详情判结束。
        # 区别只剩提示文案。
        prefix = "刷新中（自动）· " if from_auto else "刷新中 · "

        last_draw = [0.0]

        def progress(msg: str) -> None:
            # 状态文本随时更新，但**重绘要节流**：
            # - 每条都重绘会明显闪烁（一次采集可能几十上百次）；
            # - 完全不重绘则进度看着"卡住不动"，用户以为程序死了。
            # 折中：最多每 0.5 秒刷一次状态行。
            self._busy = f"{prefix}{msg}"
            now = time.monotonic()
            if now - last_draw[0] >= _PROGRESS_REDRAW_INTERVAL:
                last_draw[0] = now
                self._invalidate()

        try:
            new_snap, result = await do_refresh(self.ctx, self.snap, progress)
            self.snap = new_snap
            # 按最新数据重排，且光标跟着原来那条走——刷新不该让用户"看着的拍品
            # 悄悄换成另一条"。
            self._rebuild(keep_focus=True)
            self._refresh_detail_view()
            if result.ok:
                self._status = ""
                # 不在列表屏时给一句提示：用户不该被强行拉回列表，
                # 但得有办法知道"后台已经把数据刷好了"。
                self._notice = (
                    ()
                    if self._screen is Screen.LIST
                    else ("↻ 后台刷新完成，返回列表可查看最新数据",)
                )
                # 开了翻译就把当前视图缺译文的补上（已缓存的直接跳过，
                # 所以稳态下这里一次网络请求都不会发）。
                if self._translate_on():
                    self._launch("translate", lambda: self._translate_view(quiet=True))
            else:
                self._status = ""
                self._notice = _refresh_notice(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 兜底：任何意外都不该让界面崩
            log.exception("刷新未预期异常")
            self._notice = (
                f"✗ 刷新时出现意外错误（{type(exc).__name__}）",
                "→ 可再按 r 重试",
            )
        finally:
            self._busy = ""
            self._overlay = False
            # 采集完成：一次性渲染最终结果（期间没有增量重绘）。串行队列删掉
            # 后没有"待触发槽"——刷新期间再按 r 只会得到「已在刷新中」的提示，
            # 用户想再刷一次得等这一轮结束自己再按，这是有意为之的简单语义。

    # ------------------------------------------------------------ 登录

    def _start_browser_login(self) -> None:
        if self._action_task is not None and not self._action_task.done():
            return
        self._screen = Screen.LOGIN_WAIT
        self._wait_status = "正在启动浏览器…"
        self._notice = ()
        self._action_task = self._spawn(self._do_browser_login(), name="browser-login")
        self._invalidate()

    async def _do_browser_login(self) -> None:
        cfg = self.cfg.login
        try:
            result = await browser_login(
                cfg.url,
                cfg.debug_port,
                cfg.wait_seconds,
                profile_dir=self.ctx.data_dir / "chrome-profile",
                verify=self._verify_cb,
                progress=self._set_wait_status,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("浏览器登录未预期异常")
            self._fail_login(f"浏览器登录出错（{type(exc).__name__}）")
            return

        if result.ok:
            self._pending_cookie = result.cookie
            self._pending_nickname = result.nickname
            self._pending_source = result.source
            self._reveal_cookie = False
            self._screen = Screen.COOKIE_CONFIRM
        else:
            reason = getattr(result.capture, "reason", "") or "未能获取 Cookie"
            self._fail_login(reason, result.hints)
        self._invalidate()

    def _set_wait_status(self, text: str) -> None:
        self._wait_status = text
        self._invalidate()

    def _fail_login(self, reason: str, hints: tuple[str, ...] = ()) -> None:
        # 启动阶段失败也回「选择登录方式」：用户可以改用手动粘贴或重试，
        # 而不是被直接踢出程序（以前浏览器起不来就等于无法登录）。
        self._notice = (f"✗ {reason}", *hints, "→ 可以重试，或改用手动粘贴 Cookie")
        if self._startup_login:
            self._screen = Screen.LOGIN_METHOD
        else:
            self._screen = Screen.LIST
        self._invalidate()

    # ------------------------------------------------------------ 手机验证码登录

    def _submit_sms_mobile(self) -> None:
        """提交手机号 → 发送验证码。"""
        if self._task_running("sms-send"):
            return  # 连按 Enter 不再发第二条短信（发码是计费 + 风控敏感操作）
        mobile = self._sms_mobile_input.strip()
        if not mobile or len(mobile) < 6:
            self._notice = ("手机号看起来不完整，请检查后重试",)
            self._invalidate()
            return
        self._notice = ()
        self._busy = "正在发送验证码…"
        # origin 在**按下时**取一次：协程体要等事件循环调度才跑，那时用户可能已经
        # 按了 Esc，从里面取会把"离开后的屏"当成起点，守卫就失效了。
        self._launch("sms-send", lambda: self._do_send_sms(mobile, origin=self._screen))
        self._invalidate()

    async def _do_send_sms(self, mobile: str, *, origin: Screen) -> None:
        notice: tuple[str, ...] = ()
        failed = False
        try:
            await self.ctx.api.send_sms_code(mobile)
        except asyncio.CancelledError:
            raise
        except WafBlockedError:
            notice, failed = ("发送太频繁或触发风控，稍后再试",), True
        except NetworkError as exc:
            notice, failed = (f"发送验证码失败：{exc}", "检查网络后重试"), True
        except MamError as exc:
            notice, failed = (f"发送验证码失败：{exc}",), True
        except Exception as exc:
            log.exception("发送验证码时出现意外错误")
            notice = (f"发送验证码时出现意外错误（{type(exc).__name__}）",)
            failed = True
        finally:
            self._busy = ""
            # 用户已经按 Esc 回到手机号屏、或换到别的屏去了：晚到的结果不许把
            # 他拽回去（真实感受就是"Esc 好像没生效"）。跟 ``_do_paste_login``
            # 的做法保持一致。
            if self._screen is origin:
                if failed:
                    self._notice = notice
                    self._screen = Screen.LOGIN_SMS_MOBILE
                elif notice:
                    self._notice = notice
                else:
                    self._sms_mobile = mobile
                    self._sms_sent = True
                    self._input_text = ""
                    self._screen = Screen.LOGIN_SMS_CODE
            self._invalidate()

    def _resend_sms_code(self) -> None:
        """在验证码屏按 r 重发（走同一个发送流程，回到验证码屏）。"""
        if not self._sms_mobile:
            return
        if self._task_running("sms", "sms-send"):
            return  # 已经在发了，别叠加
        self._notice = ()
        self._busy = "正在重新发送验证码…"
        self._launch("sms", lambda: self._do_resend_sms(origin=self._screen))
        self._invalidate()

    async def _do_resend_sms(self, *, origin: Screen) -> None:
        notice: tuple[str, ...] = ()
        try:
            await self.ctx.api.send_sms_code(self._sms_mobile)
        except asyncio.CancelledError:
            raise
        except WafBlockedError:
            notice = ("发送太频繁或触发风控，稍后再试",)
        except MamError as exc:
            notice = (f"重发失败：{exc}",)
        except Exception as exc:
            log.exception("重发验证码时出现意外错误")
            notice = (f"重发时出现意外错误（{type(exc).__name__}）",)
        else:
            notice = ("验证码已重新发送",)
            self._input_text = ""
        finally:
            self._busy = ""
            if self._screen is origin:  # 离开这一屏就不再提示
                self._notice = notice
            self._invalidate()

    def _submit_sms_code(self) -> None:
        """提交验证码 → 换取 Cookie。"""
        if self._task_running("sms-login"):
            return  # 连按 Enter 不再并发登录两次
        code = self._input_text.strip()
        if not code:
            self._notice = ("请先输入收到的验证码",)
            self._invalidate()
            return
        self._notice = ()
        self._busy = "正在登录…"
        self._launch("sms-login", lambda: self._do_sms_login(code, origin=self._screen))
        self._invalidate()

    async def _do_sms_login(self, code: str, *, origin: Screen) -> None:
        notice: tuple[str, ...] = ()
        done_screen: Screen | None = None
        pending: str | None = None
        nickname = ""
        try:
            cookie = await self.ctx.api.login_by_sms(self._sms_mobile, code)
        except asyncio.CancelledError:
            raise
        except AuthExpiredError as exc:
            notice = (f"验证码不对或已过期（{exc}）", "按 r 重新发送，或检查后重输")
            done_screen = Screen.LOGIN_SMS_CODE
        except WafBlockedError:
            notice, done_screen = ("登录触发风控，稍后再试",), Screen.LOGIN_SMS_CODE
        except MamError as exc:
            notice, done_screen = (f"登录失败：{exc}",), Screen.LOGIN_SMS_CODE
        except Exception as exc:
            log.exception("短信登录时出现意外错误")
            notice = (f"登录时出现意外错误（{type(exc).__name__}）",)
            done_screen = Screen.LOGIN_SMS_CODE
        else:
            # 拿到会话 Cookie：和浏览器登录一样，先给用户确认再保存
            ok, nickname = await self._verify_cb(cookie)
            if not ok:
                notice, done_screen = (f"登录失败：{nickname}",), Screen.LOGIN_SMS_CODE
            else:
                pending = cookie
        finally:
            self._busy = ""
            # 用户按 Esc 离开验证码屏后再也不想被拽回来（发码那条同理）。
            if self._screen is origin:
                if pending is not None:
                    self._pending_cookie = pending
                    self._pending_nickname = nickname
                    self._pending_source = "手机验证码"
                    self._reveal_cookie = False
                    self._screen = Screen.COOKIE_CONFIRM
                else:
                    self._notice = notice
                    if done_screen is not None:
                        self._screen = done_screen
            self._invalidate()

    async def _verify_cb(self, cookie: str) -> tuple[bool, str]:
        return await verify_cookie(self.ctx, cookie)

    def _submit_paste(self) -> None:
        if self._paste_task is not None and not self._paste_task.done():
            return  # 验证中重复按 Enter 不再发起新请求
        cookie = self._input_text.strip()
        if not cookie:
            self._notice = ("还没有粘贴内容：先把 Cookie 粘进来，再按 Enter 提交",)
            self._invalidate()
            return
        self._notice = ()
        self._busy = "正在验证 Cookie…"
        self._paste_task = self._spawn(self._do_paste_login(cookie), name="paste-login")
        self._invalidate()

    async def _do_paste_login(self, cookie: str) -> None:
        try:
            ok, message = await self._verify_cb(cookie)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("粘贴登录验证异常")
            ok, message = False, f"验证 Cookie 时出现意外错误（{type(exc).__name__}）"
        finally:
            self._busy = ""
            self._paste_task = None

        if self._screen is not Screen.LOGIN_PASTE:
            # 用户已经离开粘贴屏：不要用晚到的结果劫持当前屏幕
            self._invalidate()
            return

        if ok:
            self._pending_cookie = cookie
            self._pending_nickname = message
            self._pending_source = "手动粘贴"
            self._reveal_cookie = False
            self._screen = Screen.COOKIE_CONFIRM
        else:
            self._notice = (
                f"✗ {message}",
                "→ 请确认 Cookie 完整；按 Esc 可改用浏览器登录",
            )
        self._invalidate()

    def _accept_cookie(self) -> None:
        if not self._pending_cookie:
            return
        try:
            self._save_verified(self._pending_cookie, self._pending_nickname)
        except OSError as exc:
            # 磁盘满/只读：告诉用户，别把 traceback 糊进 TUI；留在确认屏可重试
            log.exception("保存 Cookie 失败")
            self._notice = (
                f"✗ 无法保存 Cookie：{exc}",
                "→ 请检查数据目录是否可写、磁盘是否已满",
            )
            self._invalidate()
            return
        self._pending_cookie = ""
        self._input_text = ""
        self._notice = ()
        self._screen = Screen.LIST
        self._startup_login = False
        self._invalidate()
        self._start_refresh()

    def _save_verified(self, cookie: str, nickname: str) -> None:
        self.ctx.credentials.save(cookie)
        self.ctx.api.cookie = cookie
        self.snap = replace(
            self.snap,
            last_attempt=LastAttempt(),
            account=Account(
                label=self.cfg.account.label,
                nickname=nickname,
                cookie_state="valid",
            ),
        )
        try:
            self.ctx.snapshots.write(self.snap)
        except (OSError, SchemaVersionError):
            # 快照写失败不影响登录本身（凭证已经落盘，下次采集会重写快照）
            log.warning("登录后写快照失败", exc_info=True)
        self._status = f"已登录：{nickname or '未知昵称'}"

    # ------------------------------------------------------------ 定时刷新

    def _start_auto_refresh(self) -> None:
        interval = self.cfg.refresh.auto_seconds or 0
        if interval <= 0:
            return
        self._auto_task = self._spawn(self._auto_loop(interval), name="auto-refresh")

    async def _auto_loop(self, interval: int) -> None:
        while True:
            # 每轮重新抽一次间隔，而不是"每 15 分钟准点敲一次"——固定间隔是
            # 脚本特征；±20% 抖动后相邻间隔互不相同，长期平均仍是 interval。
            await asyncio.sleep(jittered_seconds(interval, self.cfg.fetch.jitter_ratio))
            # 兜一层：这一轮里 ``credentials.load()`` 读失败会抛 ``OSError``
            # （数据库被锁、磁盘出错…）。不兜就等于"自动刷新从此再也不跑"，
            # 而且界面上一个字都不说。
            try:
                if self._busy or self.ctx.refresher.running:
                    continue
                if not self.ctx.credentials.load():
                    continue
                self._launch("refresh", lambda: self._do_refresh(from_auto=True))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("自动刷新这一轮出错（下一轮继续）", exc_info=True)


#: 各失败域的原始错误里若出现这些词，说明与结论重复，不必再显示原文
_NOTICE_DUP_WORDS = {
    "auth": ("失效", "过期", "登录", "未授权", "expired", "unauthorized"),
    "waf": ("拦截", "风控", "频繁"),
    "network": ("网络", "超时", "连接", "不通"),
    "busy": ("采集", "进行中"),
}


def _refresh_notice(result) -> tuple[str, ...]:
    """把一次失败的刷新翻译成「一句结论 + 一句下一步」。

    以前拼成「提示语｜原始错误」一行，两边都在说"失效"，又长又绕。
    现在结论用自然语言，原始错误另起一行（排查时仍需要它）。
    """
    detail = (result.error or "").strip()
    if result.error_kind == "auth":
        hint, advice = "登录已失效", "按 s 进配置重新登录"
    elif result.error_kind == "waf":
        hint, advice = "请求被风控拦截", "稍等一会儿再按 r；频繁出现可换个网络"
    elif result.error_kind == "network":
        hint, advice = "网络连接失败", "检查网络后按 r 重试"
    elif result.error_kind == "protocol":
        hint, advice = "接口返回的结构不对", "可能是站点改版，稍后按 r 重试或检查网络"
    elif result.error_kind == "busy":
        hint, advice = "上一次刷新还没结束", "稍等片刻"
    else:
        # 措辞跟界面统一用「刷新」：界面上早就不叫「采集」了，两套词会让用户
        # 怀疑说的是不是同一件事。
        hint, advice = "刷新失败", "按 r 重试"

    # 原始错误常常只是结论的同义重复（"凭证已失效" vs "登录已失效"），
    # 此时不再把两句话都摆出来；只有它提供额外信息时才附在括号里。
    duplicated = any(word in detail for word in _NOTICE_DUP_WORDS.get(result.error_kind, ()))
    if not detail or duplicated:
        return (f"✗ {hint}", f"→ {advice}")
    # 原始错误本身常带括号，用「—」分隔比再套一层括号清楚
    return (f"✗ {hint} — {detail}", f"→ {advice}")
