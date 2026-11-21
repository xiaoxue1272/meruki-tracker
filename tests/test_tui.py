"""TUI 状态机的键盘交互测试。

**这里真的在跑 prompt_toolkit 应用**：用 ``create_pipe_input()`` 把按键喂进
应用，用 ``DummyOutput`` 兜住渲染输出。重构前这套 TUI 完全没有自动化测试，
几个 bug 都是在盲区里漏掉的；现在按键、屏幕切换、后台动作都能被断言。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import meruki_tracker.tui.tui as tui_mod
from meruki_tracker.core.adapters.store.snapshot import Account, Snapshot
from meruki_tracker.core.app.refresher import RefreshResult
from meruki_tracker.core.config import Config
from meruki_tracker.core.config_options import GROUP_NOTIFY, OPTIONS, apply_edit
from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery
from meruki_tracker.tui.tui import Screen, Tui
from meruki_tracker.tui.views import ListView

# ------------------------------------------------------------------ 替身


class _Cred:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def load(self) -> str:
        return self.value

    def save(self, cookie: str) -> None:
        self.value = cookie


class _Api:
    def __init__(self) -> None:
        self.cookie = ""
        self.translate_calls: list[dict] = []

    async def translate(self, *, title: str = "", desc: str = "", **kwargs):
        """默认翻成带"译"前缀的样子；测失败时按需覆盖。"""
        self.translate_calls.append({"title": title, "desc": desc})
        return (f"译:{title}", f"译:{desc}")


class _Refresher:
    running = False

    async def refresh(self, *a, **k):  # pragma: no cover - 不应被直接调用
        raise AssertionError("Tui 应走 do_refresh")

    async def refresh_detail_of(self, state):
        """默认原样返回；测试按需 monkeypatch 成具体行为。"""
        return state


class _Snapshots:
    def __init__(self, snap: Snapshot | None = None) -> None:
        self._snap = snap
        self.written: Snapshot | None = None

    def read(self):
        return self._snap

    def write(self, snap: Snapshot, *, allow_empty: bool = False) -> None:
        self.written = snap


class _Observations:
    def recent(self, asin: str, n: int = 5):
        return [("2026-10-29T12:00:00", "list", "x")]

    def commit(self) -> None:
        return None


class _Ctx:
    def __init__(self, tmp_path: Path, *, cookie: str = "", snap: Snapshot | None = None) -> None:
        self.data_dir = tmp_path
        self.cfg = Config()
        self.credentials = _Cred(cookie)
        self.api = _Api()
        self.refresher = _Refresher()
        self.snapshots = _Snapshots(snap)
        self.observations = _Observations()
        self.settings = _Settings()
        self.mirror = _Mirror()
        from meruki_tracker.core.app.notices import NoticeBoard

        self.notices = NoticeBoard()

    async def close(self) -> None:
        pass


_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
TZ = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")


def _draw_body(tui, *, cols: int = 100, rows: int = 30) -> str:
    """把当前屏的主体渲染成纯文本（剥掉 ANSI），供断言用。"""
    tui._screen_size = lambda: (cols, rows)
    return _ANSI_RE.sub("", str(tui._render_body()))


class _Mirror:
    """内存版镜像表：只实现详情屏要用的 detail_raw / detail_item。"""

    def __init__(
        self,
        details: dict[str, dict] | None = None,
        items: dict[str, object] | None = None,
    ) -> None:
        self._details = dict(details or {})
        self._items = dict(items or {})

    def detail_raw(self, asin: str) -> dict:
        return self._details.get(asin, {})

    def detail_item(self, asin: str):
        return self._items.get(asin)


class _Settings:
    """内存版 settings 表（配置存在库里，测试用内存替身）。"""

    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value

    def load_all(self) -> dict:
        return dict(self._data)


def _auction(asin: str = "A1", name: str = "测试拍品") -> AuctionState:
    from datetime import UTC, datetime

    return AuctionState(
        asin=asin,
        name=name,
        url="https://x/A1",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(datetime(2026, 11, 1, tzinfo=UTC), datetime.now(UTC)),
        latest_bid=100.0,
    )


@pytest.fixture(autouse=True)
def _quiet_actions(monkeypatch):
    """默认把会触网的动作用替身覆盖，避免测试联网。"""

    async def fake_refresh(ctx, snap, progress=None, **kwargs):
        return snap, RefreshResult(ok=True)

    async def fake_verify(ctx, cookie):
        return True, "挖煤君"

    monkeypatch.setattr(tui_mod, "do_refresh", fake_refresh)
    monkeypatch.setattr(tui_mod, "verify_cookie", fake_verify)


async def _drive(tui: Tui, keys: str, *, expect_exit: bool = True, hold: float = 0.12):
    """把按键喂给真实的 prompt_toolkit 应用，跑一小段后断言。"""
    with create_pipe_input() as pipe:
        tui._rebuild()
        tui._prepare_start()
        app = tui._build_app(input=pipe, output=DummyOutput())
        tui._app = app
        task = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)  # 让应用进入事件循环
        if keys:
            pipe.send_text(keys)
        if expect_exit:
            await asyncio.wait_for(task, timeout=5)
        else:
            # 先等**按键真的被消化完**再退出。以前只 sleep 一个固定时长，机器忙
            # （比如全量测试在后台跑）时按键还没处理完就 exit()，表现为随机失败：
            # 跑全量时踩到过 "Esc 之后应该回到列表，实际还停在确认屏"。
            loop = asyncio.get_running_loop()
            await asyncio.sleep(0.02)  # 让输入管道把按键喂进 key_processor
            deadline = loop.time() + 5.0
            # 只看队列、不调 empty_queue()：那个方法会**清空**队列，把还没处理的
            # 按键直接丢掉（第一次写的时候踩了，测试跑得又慢又假绿）。
            while loop.time() < deadline and app.key_processor.input_queue:
                await asyncio.sleep(0.005)
            # 再留一点时间让按键触发的异步任务收敛（refresh/翻译等）
            await asyncio.sleep(hold)
            app.exit()
            await asyncio.wait_for(task, timeout=5)
        tui._app = None
        await tui._shutdown()


# ------------------------------------------------------------------ 退出与启动


async def test_q_on_list_exits(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    await _drive(tui, "q")
    assert tui._startup_cancelled is False


async def test_startup_without_cookie_or_cache_shows_login_method(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path), Config())
    await _drive(tui, "", expect_exit=False)
    assert tui._screen is Screen.LOGIN_METHOD
    assert tui._startup_login is True


async def test_cancelling_startup_login_marks_cancelled(tmp_path: Path):
    """启动阶段既没登录也没缓存时，退出登录屏就该直接结束程序。"""
    tui = Tui(_Ctx(tmp_path), Config())
    await _drive(tui, "q")
    assert tui._startup_cancelled is True


async def test_startup_with_cache_shows_list_even_without_cookie(tmp_path: Path):
    snap = Snapshot(account=Account(label="me"), auctions=(_auction(),))
    tui = Tui(_Ctx(tmp_path, snap=snap), Config())
    await _drive(tui, "", expect_exit=False)
    assert tui._screen is Screen.LIST
    assert "登录" in tui._status


async def test_startup_with_cookie_kicks_refresh(tmp_path: Path):
    calls: list[int] = []

    async def counting_refresh(ctx, snap, progress=None, **kwargs):
        calls.append(1)
        return snap, RefreshResult(ok=True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tui_mod, "do_refresh", counting_refresh)
        tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
        await _drive(tui, "", expect_exit=False)

    assert calls == [1]


# ------------------------------------------------------------------ 列表按键


async def test_enter_opens_detail_and_q_returns(tmp_path: Path):
    snap = Snapshot(auctions=(_auction(),))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    await _drive(tui, "\r", expect_exit=False)
    assert tui._screen is Screen.DETAIL
    assert tui._detail is not None

    await _drive(tui, "q", expect_exit=False)
    assert tui._screen is Screen.LIST


async def test_arrow_keys_move_cursor(tmp_path: Path):
    snap = Snapshot(auctions=(_auction("A1", "一"), _auction("A2", "二")))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    await _drive(tui, "", expect_exit=False)
    assert tui._cursor == 0
    await _drive(tui, "", expect_exit=False)
    assert tui._cursor == 0


# ------------------------------------------------------------------ 刷新


async def test_r_starts_refresh_and_clears_busy(tmp_path: Path):
    gate = asyncio.Event()
    seen: list[str] = []

    async def slow_refresh(ctx, snap, progress=None, **kwargs):
        if progress:
            progress("列表 第 1 页")
        seen.append("started")
        await gate.wait()
        return snap, RefreshResult(ok=True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tui_mod, "do_refresh", slow_refresh)
        tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
        with create_pipe_input() as pipe:
            tui._rebuild()
            tui._prepare_start()
            app = tui._build_app(input=pipe, output=DummyOutput())
            tui._app = app
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text("r")
            await asyncio.sleep(0.15)

            assert seen == ["started"]
            assert "列表 第 1 页" in tui._busy, tui._busy
            # 刷新期间按键不该切换屏幕/触发别的动作
            pipe.send_text("2")
            await asyncio.sleep(0.05)
            assert tui._screen is Screen.LIST

            gate.set()
            await asyncio.sleep(0.15)
            assert tui._busy == ""

            app.exit()
            await asyncio.wait_for(task, timeout=5)
            tui._app = None
            await tui._shutdown()


# ------------------------------------------------------------------ 登录屏


async def test_l_is_no_longer_a_list_shortcut(tmp_path: Path):
    """登录只剩「配置 → 登录」一条路，列表页按 l 不该有任何反应。"""
    from meruki_tracker.tui import screens
    from meruki_tracker.tui.views import ListView

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    await _drive(tui, "l", expect_exit=False)
    assert tui._screen is Screen.LIST
    assert tui._view is ListView.FOLLOWING
    assert "l 换号" not in screens.footer_text("list", width=200)
    assert "l 登录" not in screens.footer_text("list", width=200)


async def test_login_is_reachable_from_the_config_page(tmp_path: Path):
    from meruki_tracker.core.config_options import ACTION_LOGIN

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._open_settings()
    tui._run_action(ACTION_LOGIN)
    assert tui._screen is Screen.LOGIN_METHOD

    await _drive(tui, "q", expect_exit=False)
    assert tui._screen is Screen.LIST


async def test_paste_screen_accepts_typing_and_backspace(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    with create_pipe_input() as pipe:
        tui._rebuild()
        tui._prepare_start()
        tui._screen = Screen.LOGIN_METHOD  # 登录方式屏现在从「配置 → 登录」进
        app = tui._build_app(input=pipe, output=DummyOutput())
        tui._app = app
        task = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)

        pipe.send_text("3")  # 选择手动粘贴
        await asyncio.sleep(0.05)
        assert tui._screen is Screen.LOGIN_PASTE

        pipe.send_text("sid=abc")
        await asyncio.sleep(0.1)
        assert tui._input_text == "sid=abc"

        pipe.send_text("\x7f")  # Backspace
        await asyncio.sleep(0.1)
        assert tui._input_text == "sid=ab"

        app.exit()
        await asyncio.wait_for(task, timeout=5)
        tui._app = None
        await tui._shutdown()


async def test_paste_screen_handles_bracketed_paste(tmp_path: Path):
    """整段粘贴（Cookies 常上千字符）应一次性接收，而不是逐字符。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    with create_pipe_input() as pipe:
        tui._rebuild()
        tui._prepare_start()
        tui._screen = Screen.LOGIN_METHOD  # 登录方式屏现在从「配置 → 登录」进
        app = tui._build_app(input=pipe, output=DummyOutput())
        tui._app = app
        task = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("3")
        await asyncio.sleep(0.05)
        pipe.send_text("\x1b[200~sid=abcdef123456; uid=42\x1b[201~")
        await asyncio.sleep(0.15)
        assert tui._input_text == "sid=abcdef123456; uid=42"

        app.exit()
        await asyncio.wait_for(task, timeout=5)
        tui._app = None
        await tui._shutdown()


async def test_accept_cookie_saves_and_starts_refresh(tmp_path: Path, monkeypatch):
    """确认使用 Cookie 后：写入凭证、更新快照、跳回列表并刷新。"""
    calls: list[int] = []

    async def counting_refresh(ctx, snap, progress=None, **kwargs):
        calls.append(1)
        return snap, RefreshResult(ok=True)

    monkeypatch.setattr(tui_mod, "do_refresh", counting_refresh)

    ctx = _Ctx(tmp_path, cookie="")
    tui = Tui(ctx, Config())
    tui._pending_cookie = "sid=abc"
    tui._pending_nickname = "挖煤君"
    tui._screen = Screen.COOKIE_CONFIRM

    tui._accept_cookie()

    assert ctx.credentials.load() == "sid=abc"
    assert ctx.api.cookie == "sid=abc"
    assert ctx.snapshots.written is not None
    assert ctx.snapshots.written.account.nickname == "挖煤君"
    assert tui._screen is Screen.LIST
    await asyncio.sleep(0.05)  # 让刷新的后台任务真正跑起来
    await tui._shutdown()
    assert calls == [1]


async def test_cancel_cookie_confirm_clears_pending(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._pending_cookie = "sid=abc"
    tui._screen = Screen.COOKIE_CONFIRM

    tui._leave_subscreen()

    assert tui._pending_cookie == ""
    assert tui._screen is Screen.LIST


# ------------------------------------------------------------------ 分页与重绘节流


async def test_ticker_only_when_countdown_visible(tmp_path: Path):
    """只有真的在显示倒计时时才需要每秒重绘。"""
    from meruki_tracker.core.domain.models import EndTime

    pending = AuctionState(
        asin="P1",
        name="pending",
        url="u",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.pending(),
    )
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._app = tui._build_app(output=DummyOutput())

    tui._rows = [pending]
    tui._screen = Screen.LIST
    assert tui._needs_tick() is False, "全是 PENDING 时没有倒计时可走"

    tui._rows = [_auction()]
    assert tui._needs_tick() is True

    tui._screen = Screen.SETTINGS
    assert tui._needs_tick() is False, "设置屏不需要每秒重绘"

    tui._app = None


async def test_tick_does_not_redraw_static_screen_without_a_due_notice(tmp_path: Path):
    """静态屏挂着 ticker 等提醒时不该每秒整屏重绘。

    改之前 ``_tick_loop`` 无条件 ``invalidate()``：只要有一条提醒落在 2 小时
    窗口内，即使停在设置屏也会连续 2 小时每秒重画一遍（实测 1.8ms/次）。
    """
    from datetime import timedelta

    from meruki_tracker.core.app.notices import KIND_ENDING, Notice, NoticeBoard

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._app = tui._build_app(output=DummyOutput())
    tui._screen = Screen.SETTINGS

    board = tui.ctx.notices
    assert isinstance(board, NoticeBoard)
    # 一条 30 分钟后才到点的提醒：ticker 要活（等着它），但每秒都不该重绘
    due = datetime.now(UTC) + timedelta(minutes=30)
    notice = Notice(
        kind=KIND_ENDING,
        asin="A1",
        title="结束提醒",
        body="拍品甲 即将结束",
        due_at=due,
        minutes_before_end=60.0,
    )
    board._ending[(notice.asin, notice.due_at)] = notice

    calls = {"n": 0}
    tui._app.invalidate = lambda: calls.__setitem__("n", calls["n"] + 1)  # type: ignore[method-assign]

    loop = asyncio.create_task(tui._tick_loop())
    await asyncio.sleep(2.2)  # 两轮 tick
    loop.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop
    assert calls["n"] == 0, "没有到点、也没有倒计时的静态屏不该被重绘"
    tui._app = None


def test_page_size_never_below_one(monkeypatch):
    """极矮终端下每页至少 1 行，不能算出负数把页面撑爆。"""
    import shutil as _shutil

    # 用 os.terminal_size 而不是自定义小类：pytest 自己的终端汇报会调用
    # shutil.get_terminal_size 并做 ``width, _ = ...`` 解包，自定义对象解不开，
    # 一旦这条用例失败就变成 INTERNALERROR（踩过）。
    _size = os.terminal_size((40, 5))
    monkeypatch.setattr(_shutil, "get_terminal_size", lambda *a, **k: _size)
    tui = Tui.__new__(Tui)
    tui._app = None  # 无应用时退回 shutil 取尺寸
    tui._status = ""
    tui._busy = ""
    tui._notice = ()
    assert Tui._page_size.fget(tui) >= 1


# ------------------------------------------------------------------ 审计修复回归


async def test_paste_failure_is_visible_on_paste_screen(tmp_path: Path, monkeypatch):
    """粘贴验证失败必须当场显示，不能等回列表才看见（否则像"按了没反应"）。"""

    async def bad_verify(ctx, cookie):
        return False, "凭证无效"

    monkeypatch.setattr(tui_mod, "verify_cookie", bad_verify)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.LOGIN_PASTE
    tui._input_text = "sid=bad"

    await tui._do_paste_login("sid=bad")

    assert tui._screen is Screen.LOGIN_PASTE
    assert any("凭证无效" in n for n in tui._notice), tui._notice

    import io

    from rich.console import Console

    buf = io.StringIO()
    tui._draw(Console(file=buf, width=80, height=24, force_terminal=False))
    assert "凭证无效" in buf.getvalue()


async def test_paste_submit_dedupes_while_verifying(tmp_path: Path, monkeypatch):
    """验证中重复按 Enter 不能发起多个请求。"""
    gate = asyncio.Event()
    calls = {"n": 0}

    async def slow_verify(ctx, cookie):
        calls["n"] += 1
        await gate.wait()
        return True, "昵称"

    monkeypatch.setattr(tui_mod, "verify_cookie", slow_verify)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.LOGIN_PASTE
    tui._input_text = "sid=abc"

    tui._submit_paste()
    tui._submit_paste()
    tui._submit_paste()
    await asyncio.sleep(0.05)

    assert calls["n"] == 1, "重复提交应被忽略"
    assert tui._busy, "验证期间要显示忙碌状态"
    gate.set()
    await asyncio.sleep(0.05)
    await tui._shutdown()


async def test_leaving_paste_screen_cancels_verification(tmp_path: Path, monkeypatch):
    gate = asyncio.Event()

    async def slow_verify(ctx, cookie):
        await gate.wait()
        return True, "昵称"

    monkeypatch.setattr(tui_mod, "verify_cookie", slow_verify)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.LOGIN_PASTE
    tui._input_text = "sid=abc"
    tui._submit_paste()
    await asyncio.sleep(0.02)

    tui._leave_subscreen()

    assert tui._paste_task is None
    await tui._shutdown()


async def test_late_paste_result_does_not_hijack_screen(tmp_path: Path, monkeypatch):
    """离开粘贴屏后才返回的验证结果，不能把当前屏幕切成确认屏。"""

    async def ok_verify(ctx, cookie):
        return True, "昵称"

    monkeypatch.setattr(tui_mod, "verify_cookie", ok_verify)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.DETAIL  # 用户已经进详情屏

    await tui._do_paste_login("sid=abc")

    assert tui._screen is Screen.DETAIL, "不能在别的屏上弹确认框"


async def test_verify_exception_is_folded(tmp_path: Path, monkeypatch):
    async def boom(ctx, cookie):
        raise RuntimeError("传输层炸了")

    monkeypatch.setattr(tui_mod, "verify_cookie", boom)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.LOGIN_PASTE

    await tui._do_paste_login("sid=abc")  # 不应抛出

    assert any("意外错误" in n for n in tui._notice), tui._notice


def test_accept_cookie_handles_disk_error(tmp_path: Path, monkeypatch):
    """写盘失败要提示并留在确认屏，而不是把 traceback 糊进 TUI。"""
    tui = Tui(_Ctx(tmp_path, cookie=""), Config())
    tui._screen = Screen.COOKIE_CONFIRM
    tui._pending_cookie = "sid=abc"
    tui._pending_nickname = "昵称"

    def boom(cookie):
        raise OSError("磁盘只读")

    monkeypatch.setattr(tui.ctx.credentials, "save", boom)

    tui._accept_cookie()

    assert tui._screen is Screen.COOKIE_CONFIRM, "失败时留在确认屏可重试"
    assert any("保存" in n for n in tui._notice)


async def test_startup_cancel_from_paste_returns_to_method_screen(tmp_path: Path):
    """启动阶段从粘贴屏退出，应回「选择登录方式」而不是直接关程序。"""
    tui = Tui(_Ctx(tmp_path), Config())
    tui._prepare_start()
    assert tui._screen is Screen.LOGIN_METHOD
    tui._screen = Screen.LOGIN_PASTE

    tui._leave_subscreen()

    assert tui._screen is Screen.LOGIN_METHOD
    assert tui._startup_cancelled is False


async def test_startup_login_failure_returns_to_method_screen(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path), Config())
    tui._prepare_start()

    tui._fail_login("浏览器起不来")

    assert tui._screen is Screen.LOGIN_METHOD
    assert tui._startup_cancelled is False, "失败不该直接退出程序"
    assert tui._notice


async def test_resize_clamps_page_and_cursor(tmp_path: Path):
    """终端变大后旧页码可能越界，渲染前必须收敛（否则整页空白）。"""
    snap = Snapshot(auctions=tuple(_auction(asin=f"A{i}") for i in range(30)))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._page = 5  # 越界页码
    tui._cursor = 29

    tui._clamp_view()

    assert tui._page <= tui._page_count() - 1
    start = tui._page * tui._page_size
    assert start <= tui._cursor < start + tui._page_size, "光标必须落在当前页内"


def test_turn_page_does_not_move_cursor_on_first_page(tmp_path: Path):
    """已在第一页时按 p：页面不变，光标也不该被弹回第一行。"""
    snap = Snapshot(auctions=tuple(_auction(asin=f"A{i}") for i in range(30)))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._page = 0
    tui._cursor = 3

    tui._turn_page(-1)

    assert tui._page == 0
    assert tui._cursor == 3, "页面没变就不该动光标"


def test_escape_has_short_timeout(tmp_path: Path):
    """单独 Esc 不该等默认 0.5s 才生效。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    app = tui._build_app(output=DummyOutput())
    assert app.ttimeoutlen <= 0.2


# ------------------------------------------------------------------ 光标


class _CursorRecordingOutput(DummyOutput):
    """只记录"渲染期间"的光标显示请求。

    prompt_toolkit 在 Application 启动/退出时也会调 ``show_cursor()``（那是
    Renderer.reset 的正常行为），所以必须用 in_render 标记把它们排除掉，
    否则测不出"界面上真的画了光标"。
    """

    def __init__(self) -> None:
        super().__init__()
        self.shown_during_render = 0
        self.in_render = False
        self.final_render = False

    def show_cursor(self) -> None:
        # 只关心常规帧；应用退出时 prompt_toolkit 会主动恢复终端光标
        # （is_done 渲染），那是正常且必要的。
        if self.in_render and not self.final_render:
            self.shown_during_render += 1

    def hide_cursor(self) -> None:
        return None


async def test_tui_never_draws_a_cursor(tmp_path: Path):
    """整个界面是只读的：不该有光标停在第一个字符上闪。"""
    import prompt_toolkit.renderer as renderer_mod

    out = _CursorRecordingOutput()
    original = renderer_mod.Renderer.render

    def wrapped(self, app, layout, is_done: bool = False):
        out.in_render = True
        out.final_render = is_done
        try:
            return original(self, app, layout, is_done)
        finally:
            out.in_render = False
            out.final_render = False

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    with create_pipe_input() as pipe:
        tui._rebuild()
        tui._prepare_start()
        app = tui._build_app(input=pipe, output=out)
        tui._app = app
        renderer_mod.Renderer.render = wrapped
        try:
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.25)
            app.exit()
            await asyncio.wait_for(task, timeout=5)
        finally:
            renderer_mod.Renderer.render = original
            tui._app = None
            await tui._shutdown()

    assert out.shown_during_render == 0, "渲染帧里不该显示光标"


# ------------------------------------------------------------------ 提示文案


def test_refresh_notice_avoids_duplicate_wording():
    """原始错误若只是结论的同义重复，就不要再摆一遍（以前是"登录已失效｜凭证已失效"）。"""
    from meruki_tracker.core.app.refresher import RefreshResult
    from meruki_tracker.tui.tui import _refresh_notice

    lines = _refresh_notice(RefreshResult(ok=False, error="凭证已失效", error_kind="auth"))
    assert lines[0] == "✗ 登录已失效"
    assert "凭证已失效" not in lines[0], "重复措辞不该再出现"
    assert lines[1].startswith("→"), "第二行要给出下一步动作"


def test_refresh_notice_keeps_informative_detail():
    """原始错误含额外信息时要保留，否则排查无从下手。"""
    from meruki_tracker.core.app.refresher import RefreshResult
    from meruki_tracker.tui.tui import _refresh_notice

    err = "列表响应缺少 Data 数组（实际键: ['TotalItem']）"
    lines = _refresh_notice(RefreshResult(ok=False, error=err, error_kind="protocol"))
    assert "Data 数组" in lines[0]
    assert "站点改版" in lines[1]


def test_refresh_notice_covers_all_error_kinds():
    from meruki_tracker.core.app.refresher import RefreshResult
    from meruki_tracker.tui.tui import _refresh_notice

    for kind in ("auth", "waf", "network", "protocol", "busy", "internal"):
        lines = _refresh_notice(
            RefreshResult(ok=False, error=f"{kind} 的原始错误", error_kind=kind)
        )
        assert len(lines) == 2, kind
        assert lines[0].startswith("✗"), kind
        assert lines[1].startswith("→"), kind


# ------------------------------------------------------------------ 手机验证码登录


class _SmsApi:
    """记录短信登录调用的假 API。"""

    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        login_cookie: str = "kmgSession=abc; uid=1",
        login_error: Exception | None = None,
    ) -> None:
        self.cookie = ""
        self.sent: list[str] = []
        self.logins: list[tuple[str, str]] = []
        self._send_error = send_error
        self._login_cookie = login_cookie
        self._login_error = login_error

    async def send_sms_code(self, mobile: str, *, dial_code: str = "+86") -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(mobile)

    async def login_by_sms(self, mobile: str, code: str) -> str:
        if self._login_error is not None:
            raise self._login_error
        self.logins.append((mobile, code))
        return self._login_cookie


def _sms_ctx(tmp_path: Path, api) -> _Ctx:
    ctx = _Ctx(tmp_path, cookie="")
    ctx.api = api
    return ctx


def test_login_method_offers_sms():
    import io

    from rich.console import Console

    from meruki_tracker.tui import screens

    buf = io.StringIO()
    screens.render_login_method(Console(file=buf, width=84, height=24), has_cookie=False)
    out = buf.getvalue()
    assert "手机验证码登录" in out
    assert "1 / 2 / 3" in screens.footer_text("login-method")


async def test_sms_mobile_then_code_flow(tmp_path: Path):
    """完整流程：输手机号 → 发码 → 输验证码 → 拿到 Cookie → 确认屏。"""
    api = _SmsApi()
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_MOBILE
    tui._sms_mobile_input = "13800138000"

    tui._submit_sms_mobile()
    await asyncio.sleep(0.05)

    assert api.sent == ["13800138000"]
    assert tui._screen is Screen.LOGIN_SMS_CODE
    assert tui._sms_mobile == "13800138000"

    tui._input_text = "123456"
    tui._submit_sms_code()
    await asyncio.sleep(0.05)

    assert api.logins == [("13800138000", "123456")]
    assert tui._screen is Screen.COOKIE_CONFIRM
    assert tui._pending_cookie == "kmgSession=abc; uid=1"
    assert tui._pending_source == "手机验证码"
    await tui._shutdown()


async def test_sms_mobile_rejects_incomplete_number(tmp_path: Path):
    api = _SmsApi()
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_MOBILE
    tui._sms_mobile_input = "138"

    tui._submit_sms_mobile()
    await asyncio.sleep(0.02)

    assert api.sent == [], "号码不完整不该发请求"
    assert tui._screen is Screen.LOGIN_SMS_MOBILE
    assert any("不完整" in n for n in tui._notice)


async def test_sms_send_failure_stays_on_mobile_screen(tmp_path: Path):
    from meruki_tracker.core.errors import NetworkError

    api = _SmsApi(send_error=NetworkError("网络不通"))
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_MOBILE
    tui._sms_mobile_input = "13800138000"

    tui._submit_sms_mobile()
    await asyncio.sleep(0.05)

    assert tui._screen is Screen.LOGIN_SMS_MOBILE
    assert any("发送验证码失败" in n for n in tui._notice)
    await tui._shutdown()


async def test_sms_wrong_code_stays_on_code_screen(tmp_path: Path):
    from meruki_tracker.core.errors import AuthExpiredError

    api = _SmsApi(login_error=AuthExpiredError("验证码错误"))
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_CODE
    tui._sms_mobile = "13800138000"
    tui._input_text = "000000"

    tui._submit_sms_code()
    await asyncio.sleep(0.05)

    assert tui._screen is Screen.LOGIN_SMS_CODE, "验证码错要留在原屏重输"
    assert any("验证码" in n for n in tui._notice)
    await tui._shutdown()


async def test_sms_verify_failure_does_not_save(tmp_path: Path, monkeypatch):
    """登录接口给了 Cookie，但验证不通过时不能进确认屏。"""
    api = _SmsApi()

    async def bad_verify(ctx, cookie):
        return False, "凭证无效"

    monkeypatch.setattr(tui_mod, "verify_cookie", bad_verify)
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_CODE
    tui._sms_mobile = "13800138000"
    tui._input_text = "123456"

    tui._submit_sms_code()
    await asyncio.sleep(0.05)

    assert tui._screen is Screen.LOGIN_SMS_CODE
    assert tui._pending_cookie == ""
    await tui._shutdown()


async def test_sms_resend(tmp_path: Path):
    api = _SmsApi()
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_CODE
    tui._sms_mobile = "13800138000"
    tui._input_text = "12"

    tui._resend_sms_code()
    await asyncio.sleep(0.05)

    assert api.sent == ["13800138000"]
    assert tui._input_text == "", "重发后清空已输入的验证码"
    await tui._shutdown()


async def test_escape_from_code_screen_goes_back_to_mobile(tmp_path: Path):
    """验证码屏按 Esc 回上一层（改手机号），而不是退出整个登录流程。"""
    api = _SmsApi()
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_CODE

    with create_pipe_input() as pipe:
        tui._rebuild()
        app = tui._build_app(input=pipe, output=DummyOutput())
        tui._app = app
        task = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("\x1b")
        await asyncio.sleep(0.25)
        assert tui._screen is Screen.LOGIN_SMS_MOBILE
        app.exit()
        await asyncio.wait_for(task, timeout=5)
        tui._app = None
        await tui._shutdown()


# ------------------------------------------------------------------ 页脚固定在最后一行


async def test_footer_is_a_separate_fixed_window(tmp_path: Path):
    """页脚是独立的 1 行窗口，不跟主体内容一起滚动/裁掉。

    主体现在是 ``VSplit([主窗口, 侧栏])``：仍占满除页脚外的行，页脚依旧独立 1 行。
    """
    from prompt_toolkit.layout.containers import (
        ConditionalContainer,
        HSplit,
        VSplit,
        Window,
    )

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    app = tui._build_app(output=DummyOutput())
    tui._app = app

    root = app.layout.container
    assert isinstance(root, HSplit), "布局应是上下分栏"
    assert len(root.children) == 2, "主体 + 页脚"
    body, footer = root.children
    assert isinstance(body, VSplit), "主体是左右分栏（主窗口 + 侧栏）"
    assert isinstance(footer, Window)
    assert len(body.children) == 2, "主窗口 + 侧栏容器"
    assert isinstance(body.children[0], Window)
    assert isinstance(body.children[1], ConditionalContainer), "侧栏可隐藏"
    assert footer.height == 1, "页脚固定占 1 行"
    tui._app = None


async def test_body_height_reserves_one_line_for_footer(tmp_path: Path):
    """主体渲染高度要比屏幕少 1 行，否则页脚会被挤出屏幕。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._rebuild()
    seen: dict = {}

    import meruki_tracker.tui.tui as mod

    real_console = mod.Console

    def spy(*a, **kw):
        seen["height"] = kw.get("height")
        return real_console(*a, **kw)

    tui._screen_size = lambda: (100, 24)
    mod.Console = spy
    try:
        tui._render_body()
    finally:
        mod.Console = real_console

    assert seen["height"] == 23, "24 行屏幕要给页脚留 1 行"


def test_footer_text_for_every_screen():
    """每个屏幕都有页脚文案（不能出现空白页脚）。"""
    from meruki_tracker.tui import screens

    for name in (
        "list",
        "detail",
        "login-method",
        "login-paste",
        "login-sms-mobile",
        "login-sms-code",
        "login-wait",
        "cookie-confirm",
    ):
        assert screens.footer_text(name, page=0, page_count=1), f"{name} 缺少页脚"


def test_footer_mentions_resend_only_on_code_screen():
    from meruki_tracker.tui import screens

    assert "重新发送" in screens.footer_text("login-sms-code", can_resend=True)
    assert "重新发送" not in screens.footer_text("login-sms-code", can_resend=False)


# ------------------------------------------------------------------ 刷新遮罩 / 价格


def test_list_footer_switches_to_busy_text_while_refreshing():
    """采集遮罩期间列表按键都失效，页脚不该再列它们。"""
    from meruki_tracker.tui import screens

    idle = screens.footer_text("list", page=0, page_count=2)
    busy = screens.footer_text("list", page=0, page_count=2, busy="刷新中 · 进行中")

    assert "↑↓ 选择" in idle
    assert "↑↓ 选择" not in busy
    assert "刷新中" in busy and "q 退出" in busy


def test_refresh_overlay_hides_the_list():
    """刷新中整屏只显示进度，不显示旧列表。"""
    import io

    from rich.console import Console

    from meruki_tracker.tui import screens

    buf = io.StringIO()
    screens.render_refreshing(
        Console(file=buf, width=84, height=24), busy="刷新中 · 列表第 2 页 · 已拉 32 条"
    )
    out = buf.getvalue()
    assert "刷新中" in out
    assert "列表第 2 页" in out
    # 遮罩标题已经写着「刷新中」，正文不该再重复一遍前缀
    assert "刷新中 · 列表" not in out
    assert "当前价" not in out, "遮罩期间不该显示列表表头"
    assert "剩余" not in out


def test_refresh_overlay_fits_short_terminal():
    import io

    from rich.console import Console

    from meruki_tracker.tui import screens

    for h in (10, 16, 24):
        buf = io.StringIO()
        screens.render_refreshing(Console(file=buf, width=80, height=h), busy="x")
        assert len(buf.getvalue().splitlines()) <= h, h


async def test_list_draws_overlay_only_when_overlay_flag_set(tmp_path: Path):
    """遮罩由 _overlay 控制（只有首次无数据时为真），不是"busy 就遮"。"""
    import io

    from rich.console import Console

    snap = Snapshot(auctions=tuple(_auction(asin=f"A{i}") for i in range(5)))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()

    # 空闲：正常列表
    buf = io.StringIO()
    tui._draw(Console(file=buf, width=84, height=24, force_terminal=False))
    assert "当前价" in buf.getvalue()

    # 刷新中但不遮罩：列表照常可见（这是普通刷新的行为）
    tui._busy = "刷新中 · 进行中 第 1 页"
    tui._overlay = False
    buf = io.StringIO()
    tui._draw(Console(file=buf, width=84, height=24, force_terminal=False))
    assert "当前价" in buf.getvalue(), "有数据时采集中也不该遮罩"

    # 首次加载：遮罩
    tui._overlay = True
    buf = io.StringIO()
    tui._draw(Console(file=buf, width=84, height=24, force_terminal=False))
    assert "刷新中" in buf.getvalue()
    assert "当前价" not in buf.getvalue(), "遮罩期间不显示列表"


# ------------------------------------------------------------------ 列表页脚页码


def test_list_footer_still_uses_list_page(tmp_path: Path):
    """列表屏页脚用的是列表页码；只有一页时干脆不显示翻页提示。"""
    import re

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.LIST
    tui._page = 2
    tui._rows = [object()] * 3
    tui._app = tui._build_app(output=DummyOutput())
    tui._page_count = lambda: 3  # 3 行刚好 1 页，凑出多页来看页码

    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", str(tui._render_footer()))
    assert "3/" in text, text
    tui._app = None


# ------------------------------------------------------------------ 刷新不遮罩、不增量重绘


async def test_refresh_does_not_overlay_when_data_exists(tmp_path: Path):
    """已有数据时按 r：不遮罩，列表继续可见。"""
    import io

    from rich.console import Console

    snap = Snapshot(auctions=tuple(_auction(asin=f"A{i}") for i in range(5)))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._spawn = lambda coro, name: (coro.close(), None)[1]  # 不真的跑采集

    tui._start_refresh()

    assert tui._overlay is False, "有数据时不该遮罩"
    buf = io.StringIO()
    tui._draw(Console(file=buf, width=90, height=24, force_terminal=False))
    out = buf.getvalue()
    assert "剩余" in out, "列表应当仍然可见"
    assert "刷新中 · 正在准备…" in out, "状态行要明说这是刷新"


async def test_first_load_overlays_because_there_is_nothing_to_show(tmp_path: Path):
    """首次启动没有任何数据：用遮罩显示进度。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._rebuild()
    tui._spawn = lambda coro, name: (coro.close(), None)[1]

    tui._start_refresh()

    assert tui._overlay is True


async def test_refresh_progress_redraws_are_throttled(tmp_path: Path, monkeypatch):
    """采集进度要更新，但不能每条都重绘——节流到最多每 0.5 秒一次。

    完全不重绘：进度看着卡住不动，用户以为程序死了；
    每条都重绘：一次采集几十上百次整屏刷新，明显闪。
    """
    import time

    from prompt_toolkit.output import DummyOutput

    snap = Snapshot(auctions=tuple(_auction(asin=f"A{i}") for i in range(3)))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._app = tui._build_app(output=DummyOutput())

    redraws = {"n": 0}
    original = tui._app.invalidate

    def counting():
        redraws["n"] += 1
        original()

    tui._app.invalidate = counting  # type: ignore[method-assign]

    seen: list[str] = []

    async def fake_refresh(ctx, snap_old, progress, **kwargs):
        # 模拟一次耗时 1.2 秒的采集：分 12 步，每步 0.1 秒
        for i in range(12):
            progress(f"列表 第 {i + 1} 页 · 已拉 {(i + 1) * 10} 条")
            seen.append(tui._busy)
            await asyncio.sleep(0.1)
        return snap_old, types.SimpleNamespace(ok=True)

    import meruki_tracker.tui.tui as mod

    monkeypatch.setattr(mod, "do_refresh", fake_refresh)
    tui._screen = Screen.LIST
    started = time.monotonic()
    await tui._do_refresh()
    elapsed = time.monotonic() - started

    assert len(seen) == 12, "进度文本要跟着更新"
    # 1.2 秒 / 0.5 秒 ≈ 2~3 次中间刷新 + 结束时 1 次
    assert 1 < redraws["n"] <= 6, f"重绘应被节流，实际 {redraws['n']} 次（耗时 {elapsed:.2f}s）"
    tui._app = None


async def test_overlay_cleared_after_refresh_finishes(tmp_path: Path, monkeypatch):
    import meruki_tracker.tui.tui as mod

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._rebuild()
    tui._overlay = True
    tui._busy = "刷新中 · 进行中"

    async def fake_refresh(ctx, snap_old, progress, **kwargs):
        return snap_old, types.SimpleNamespace(ok=True)

    monkeypatch.setattr(mod, "do_refresh", fake_refresh)
    await tui._do_refresh()

    assert tui._overlay is False
    assert tui._busy == ""


async def test_refresh_progress_says_it_is_a_refresh(tmp_path: Path, monkeypatch):
    """进度文案要明说是「刷新中」。

    以前手动刷新只显示「进行中 第 1 页 · 已拉 10 条」——「进行中」是挖煤姬的
    列表状态词，跟"程序在动"这件事撞车，看不出是刷新动作还是常驻状态。
    """
    import meruki_tracker.tui.tui as mod

    snap = Snapshot(auctions=tuple(_auction(asin=f"A{i}") for i in range(2)))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._app = tui._build_app(output=DummyOutput())

    seen: list[str] = []

    async def fake_refresh(ctx, snap_old, progress, **kwargs):
        progress("进行中 第 1 页 · 已拉 10 条")
        seen.append(tui._busy)
        return snap_old, types.SimpleNamespace(ok=True)

    monkeypatch.setattr(mod, "do_refresh", fake_refresh)

    await tui._do_refresh()
    assert seen == ["刷新中 · 进行中 第 1 页 · 已拉 10 条"]

    await tui._do_refresh(from_auto=True)
    assert seen[-1] == "刷新中（自动）· 进行中 第 1 页 · 已拉 10 条"


# ------------------------------------------------------------------ 设置界面


async def test_s_opens_settings_from_list(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._rebuild()
    with create_pipe_input() as pipe:
        app = tui._build_app(input=pipe, output=DummyOutput())
        tui._app = app
        task = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("s")
        await asyncio.sleep(0.1)
        assert tui._screen is Screen.SETTINGS
        app.exit()
        await asyncio.wait_for(task, timeout=5)
        tui._app = None
        await tui._shutdown()


def test_settings_cursor_moves_and_clamps(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.SETTINGS
    tui._settings_cursor = 0

    tui._move_settings(1)
    assert tui._settings_cursor == 1
    tui._move_settings(-1)
    assert tui._settings_cursor == 0
    tui._move_settings(-5)  # 不能越界
    assert tui._settings_cursor == 0


async def test_settings_edit_saves_and_applies(tmp_path: Path):
    """改完立即生效，并且落库。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.SETTINGS

    tui._begin_edit_for("refresh.auto_seconds")
    # 默认单位跟着屏幕上的显示单位走（180 秒 = 3 分钟，所以进来就是分钟）
    assert tui._edit_unit == "分钟"
    tui._input_text = "10"
    tui._commit_edit()

    assert tui._screen is Screen.SETTINGS
    assert tui.cfg.refresh.auto_seconds == 600
    assert tui.ctx.settings.get("refresh.auto_seconds") == 600
    assert any("已保存" in n for n in tui._notice)


async def test_settings_edit_respects_the_selected_unit(tmp_path: Path):
    """切到秒再输入，同一个数字含义不同——单位是输入的一部分。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.SETTINGS

    tui._begin_edit_for("refresh.auto_seconds")
    tui._cycle_edit_unit(1)
    assert tui._edit_unit == "小时"
    tui._input_text = "2"
    tui._commit_edit()

    assert tui.cfg.refresh.auto_seconds == 7200


async def test_settings_edit_cycles_units_with_wraparound(tmp_path: Path):
    """Tab 在候选单位之间循环，不会卡在最后一个。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._begin_edit_for("refresh.auto_seconds")
    seen = [tui._edit_unit]
    for _ in range(3):
        tui._cycle_edit_unit(1)
        seen.append(tui._edit_unit)
    assert seen == ["分钟", "小时", "天", "秒"]
    tui._cycle_edit_unit(-1)
    assert tui._edit_unit == "天"


async def test_settings_edit_has_no_unit_switch_for_plain_counts(tmp_path: Path):
    """「失败重试次数」这类计数项只有一个单位，Tab 不该把它改乱。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._begin_edit_for("fetch.retry")
    assert tui._edit_unit == "次"
    tui._cycle_edit_unit(1)
    assert tui._edit_unit == "次"


async def test_invalid_setting_is_rejected_and_not_saved(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.SETTINGS

    tui._begin_edit_for("refresh.auto_seconds")
    tui._input_text = "abc"
    tui._commit_edit()

    assert tui._screen is Screen.SETTING_EDIT, "校验失败应停留在编辑屏"
    assert tui.cfg.refresh.auto_seconds == 180, "配置不能被改坏"
    assert tui.ctx.settings.get("refresh.auto_seconds") is None
    assert any("数字" in n for n in tui._notice)


async def test_escape_cancels_edit(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.SETTINGS
    tui._begin_edit_for("refresh.auto_seconds")
    tui._input_text = "999"

    with create_pipe_input() as pipe:
        app = tui._build_app(input=pipe, output=DummyOutput())
        tui._app = app
        task = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("\x1b")
        await asyncio.sleep(0.25)
        assert tui._screen is Screen.SETTINGS
        assert tui.cfg.refresh.auto_seconds == 180, "取消不该改动配置"
        app.exit()
        await asyncio.wait_for(task, timeout=5)
        tui._app = None
        await tui._shutdown()


async def test_settings_survive_restart(tmp_path: Path):
    """存进库里的配置，下次启动要读得回来（用真实数据库，不用内存替身）。"""
    from dataclasses import replace

    from meruki_tracker.core.adapters.store.db import connect as db_connect
    from meruki_tracker.core.adapters.store.settings import SettingsStore
    from meruki_tracker.core.app.context import load_config
    from meruki_tracker.core.config import StorageConfig

    data_dir = tmp_path / "data"
    cfg = replace(Config.defaults(), storage=StorageConfig(data_dir=str(data_dir)))

    # 写：模拟设置界面保存
    conn = db_connect(data_dir / "meruki.db")
    try:
        SettingsStore(conn).set("refresh.auto_seconds", 900)
    finally:
        conn.close()

    # 读：模拟下次启动
    assert load_config(data_dir).refresh.auto_seconds == 900
    assert cfg.refresh.auto_seconds == 180, "原配置对象不受影响"


# ------------------------------------------------------------------ 刷新完成后的行为


async def test_refresh_keeps_user_on_detail_screen(tmp_path: Path, monkeypatch):
    """不在列表屏时不该被强行拉回去，只给一句提示。"""
    import types

    import meruki_tracker.tui.tui as mod

    snap = Snapshot(auctions=(_auction(asin="A1"), _auction(asin="A2")))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = tui._rows[0]

    async def fake_refresh(ctx, snap_old, progress, **kwargs):
        return snap_old, types.SimpleNamespace(ok=True)

    monkeypatch.setattr(mod, "do_refresh", fake_refresh)
    await tui._do_refresh()

    assert tui._screen is Screen.DETAIL, "不该把用户从详情屏拉走"
    assert any("后台刷新完成" in n for n in tui._notice), tui._notice


async def test_refresh_in_list_has_no_extra_notice(tmp_path: Path, monkeypatch):
    import types

    import meruki_tracker.tui.tui as mod

    snap = Snapshot(auctions=(_auction(asin="A1"),))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._screen = Screen.LIST

    async def fake_refresh(ctx, snap_old, progress, **kwargs):
        return snap_old, types.SimpleNamespace(ok=True)

    monkeypatch.setattr(mod, "do_refresh", fake_refresh)
    await tui._do_refresh()

    assert tui._notice == (), "在列表屏时结果已经渲染出来，不需要额外提示"


async def test_refresh_updates_open_detail_object(tmp_path: Path, monkeypatch):
    """详情屏开着时刷新，正在看的拍品要同步成新数据（否则看到旧价格）。"""
    import types
    from dataclasses import replace

    import meruki_tracker.tui.tui as mod

    old = _auction(asin="A1")
    snap = Snapshot(auctions=(old,))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = old

    updated = replace(old, latest_bid=9999.0)
    new_snap = Snapshot(auctions=(updated,))

    async def fake_refresh(ctx, snap_old, progress, **kwargs):
        return new_snap, types.SimpleNamespace(ok=True)

    monkeypatch.setattr(mod, "do_refresh", fake_refresh)
    await tui._do_refresh()

    assert tui._detail is not None
    assert tui._detail.latest_bid == 9999.0, "详情屏的数据要跟着刷新"


# ------------------------------------------------------------------ 详情刷新


async def test_detail_r_refreshes_the_whole_dataset(tmp_path: Path):
    """详情屏的 r 就是刷新——刷的是整份数据，不是"只刷这一条详情"。

    真实反馈：「刷新改回列表+详情，去掉单独的详情刷新」。
    """
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(_auction("A1"),))), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = tui._rows[0]
    called: list[str] = []

    async def fake_refresh(*a, **k):
        called.append("refresh")

    tui._do_refresh = fake_refresh  # type: ignore[method-assign]
    await _drive(tui, "r", expect_exit=False)
    assert "refresh" in called, f"详情屏的 r 应该走整体刷新，实际 {called}"


async def test_fetching_this_detail_updates_row_and_observations(tmp_path: Path, monkeypatch):
    from dataclasses import replace

    old = _auction(asin="A1")
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(old,))), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = old

    updated = replace(old, latest_bid=5555.0)

    async def fake_refresh_of(state):
        return updated

    monkeypatch.setattr(tui.ctx.refresher, "refresh_detail_of", fake_refresh_of)
    await tui._do_fetch_this_detail(old)

    assert tui._detail is not None and tui._detail.latest_bid == 5555.0
    # 列表里的同一拍品也要同步（返回列表时看到的是新值）
    assert tui._rows[0].latest_bid == 5555.0


async def test_fetching_this_detail_failure_keeps_previous_data(tmp_path: Path, monkeypatch):
    from meruki_tracker.core.errors import MerukiTrackerError

    old = _auction(asin="A1")
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(old,))), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = old

    async def boom(state):
        raise MerukiTrackerError("网络不通")

    monkeypatch.setattr(tui.ctx.refresher, "refresh_detail_of", boom)
    await tui._do_fetch_this_detail(old)

    assert tui._detail is not None and tui._detail.latest_bid == 100.0, "失败不能清掉已有数据"
    assert any("详情没拿到" in n for n in tui._notice)


def test_detail_footer_advertises_refresh():
    from meruki_tracker.tui import screens

    assert "r 刷新" in screens.footer_text("detail")
    assert "刷新详情" not in screens.footer_text("detail")


# ------------------------------------------------------------------ 视图切换


def _mixed_snapshot() -> Snapshot:
    from datetime import timedelta

    from meruki_tracker.core.domain.models import DoorState, EndTime, UserQuery

    base = datetime(2026, 11, 1, tzinfo=UTC)
    active = _auction("A1", "还在拍")
    ended = replace(
        _auction("A2", "已结束的"),
        door_state=DoorState.ENDED,
        end_time=EndTime.exact(base - timedelta(days=1), NOW_FOR_TESTS),
    )
    gone = replace(
        _auction("A3", "老数据:已离开列表的"),
        user_query=UserQuery.UNFOLLOWED,
        unfollowed_at=base,
    )
    return Snapshot(auctions=(active, ended, gone))


def _snapshot_with_invalid(*, failures: int = 3) -> Snapshot:
    """三视图 + 一条「详情连续拉不到」的 INVALID 拍品。"""
    from meruki_tracker.core.domain.models import DoorState

    extra = replace(
        _auction("A4", "详情一直拉不到"),
        door_state=DoorState.INVALID,
        detail_failures=failures,
    )
    cur = _mixed_snapshot()
    return Snapshot(auctions=(*cur.auctions, extra))


NOW_FOR_TESTS = datetime(2026, 10, 30, tzinfo=UTC)


async def test_tab_cycles_views(tmp_path: Path):
    """``Tab`` / ``Shift+Tab`` 在四档视图之间循环；启动时是「关注中」。

    外层大分页已经拆掉；翻页是 ``n`` / ``p``，``Tab`` 只负责切视图。
    「无法确认」插在「已结束」和「全部」之间，所以转一圈需要四下。
    """
    from meruki_tracker.tui.views import ListView

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    # 注意 _drive 每次都重建应用但**保留 Tui 状态**，所以视图是累加的
    await _drive(tui, "\t", expect_exit=False)
    assert tui._view is ListView.ENDED

    await _drive(tui, "\t", expect_exit=False)
    assert tui._view is ListView.INVALID, "「无法确认」是第三档"

    await _drive(tui, "\t", expect_exit=False)
    assert tui._view is ListView.ALL, "转一圈的最后一步是「全部」"

    await _drive(tui, "\t", expect_exit=False)
    assert tui._view is ListView.FOLLOWING, "再按一下回到开头"

    await _drive(tui, "\x1b[Z", expect_exit=False)
    assert tui._view is ListView.ALL, "``Shift+Tab`` 往回转"


async def test_right_and_left_arrows_switch_views(tmp_path: Path):
    """左右箭头现在切视图；翻页让给 n / p，箭头不再改动页码。"""
    from meruki_tracker.tui.views import ListView

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    await _drive(tui, "\x1b[C", expect_exit=False)  # →
    assert tui._view is ListView.ENDED
    assert tui._page == 0, "切视图不该顺手翻页"

    await _drive(tui, "\x1b[D", expect_exit=False)  # ←
    assert tui._view is ListView.FOLLOWING


async def test_bracket_keys_no_longer_switch_views(tmp_path: Path):
    """``[`` / ``]`` 已经解绑：切视图只认 Tab 和左右箭头。"""
    from meruki_tracker.tui.views import ListView

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    await _drive(tui, "]", expect_exit=False)
    await _drive(tui, "[", expect_exit=False)
    assert tui._view is ListView.FOLLOWING


async def test_switching_view_filters_rows(tmp_path: Path):
    """切到「已结束」后列表里只有终态的，活的一个都看不见。

    四档顺序：关注中 → 已结束 → 无法确认 → 全部 → 回到关注中。
    """
    from meruki_tracker.tui.views import ListView

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    tui._rebuild()
    assert len(tui._rows) == 1, "关注中只有 A1"

    tui._cycle_view()  # → 已结束
    # A3 是老数据里残留的"已离开"，一并归到终态组（排序按结束时间，这里不比顺序）
    assert sorted(r.asin for r in tui._rows) == ["A2", "A3"]

    tui._cycle_view()  # → 无法确认（这份快照里没有 INVALID 拍品）
    assert tui._view is ListView.INVALID
    assert tui._rows == []

    tui._cycle_view()  # → 全部
    assert len(tui._rows) == 3

    tui._cycle_view()  # → 回到关注中
    assert tui._view is ListView.FOLLOWING
    assert len(tui._rows) == 1


async def test_switching_view_resets_cursor(tmp_path: Path):
    """换视图要回到首行：两个视图的行之间没有对应关系。"""
    snap = Snapshot(auctions=tuple(_auction(f"A{i}") for i in range(5)))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._move(3)
    assert tui._cursor == 3
    tui._cycle_view()
    assert tui._cursor == 0


async def test_ended_view_lists_the_newest_first(tmp_path: Path):
    """「已结束」按结束时间**倒序**：刚结束的才是要看的。

    正序（关注中/全部用它是对的）会让 500 多条历史从两年前开始翻，
    用户抱怨过"已结束还是之前那点数据"——其实翻到第一页的全是老拍品。
    """
    from datetime import timedelta

    from meruki_tracker.core.domain.models import DoorState, EndTime
    from meruki_tracker.tui.views import ListView

    base = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    old = replace(
        _auction("OLD"),
        door_state=DoorState.ENDED,
        end_time=EndTime.exact(base - timedelta(days=300), NOW_FOR_TESTS),
    )
    new = replace(
        _auction("NEW"),
        door_state=DoorState.ENDED,
        end_time=EndTime.exact(base - timedelta(hours=1), NOW_FOR_TESTS),
    )
    no_time = replace(_auction("NOTIME"), door_state=DoorState.ENDED, end_time=EndTime.pending())
    snap = Snapshot(auctions=(old, new, no_time))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._view = ListView.ENDED

    tui._rebuild()

    # 没有结束时间的排最后，不能因为倒序被顶到第一页
    assert [r.asin for r in tui._rows] == ["NEW", "OLD", "NOTIME"]


def test_view_bar_shows_every_count():
    """视图栏必须带计数——数量是用户决定要不要切过去的唯一依据。"""
    from meruki_tracker.tui import screens
    from meruki_tracker.tui.views import counts

    rows = _mixed_snapshot().auctions
    out = _render_view_bar(screens, counts(rows))
    for label in ("关注中", "已结束", "无法确认", "全部"):
        assert label in out, out
    assert "关注中 1" in out.replace("  ·  ", " ")
    assert "已结束 2" in out.replace("  ·  ", " "), out


def _render_view_bar(screens, c) -> str:
    from io import StringIO

    from rich.console import Console

    console = Console(file=StringIO(), width=100, height=10)
    with console.capture() as cap:
        console.print(screens.view_bar(screens.ListView.FOLLOWING, c))
    return cap.get()


def test_invalid_view_shows_only_invalid_and_its_count():
    """「无法确认」单独成档，计数也得跟其它档一起对得上。"""
    from meruki_tracker.tui import screens
    from meruki_tracker.tui.views import counts

    rows = _snapshot_with_invalid().auctions
    c = counts(rows)
    assert c[ListView.INVALID] == 1
    assert c[ListView.ALL] == 4
    out = _render_view_bar(screens, c)
    assert "无法确认 1" in out.replace("  ·  ", " "), out


def test_narrow_terminal_view_bar_shows_only_current():
    """窄屏只留当前视图，别渲染成一串被截断的碎片。"""
    from io import StringIO

    from rich.console import Console

    from meruki_tracker.tui import screens
    from meruki_tracker.tui.views import ListView, counts

    console = Console(file=StringIO(), width=40, height=10)
    with console.capture() as cap:
        console.print(screens.view_bar(ListView.ENDED, counts([]), compact=True))
    out = cap.get()
    assert "已结束" in out
    assert "关注中" not in out


# ------------------------------------------------------------------ 清理


class _FakeTask:
    """只满足 ``_task_running`` 需要的那两个方法（不进事件循环）。"""

    def __init__(self, name: str) -> None:
        self._name = name

    def done(self) -> bool:
        return False

    def get_name(self) -> str:
        return self._name


def _named_task(name: str) -> _FakeTask:
    return _FakeTask(name)


def _cleanable(tmp_path: Path, *, snap: Snapshot | None = None) -> Tui:
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap or _mixed_snapshot()), Config())
    tui._rebuild()
    return tui


async def test_clean_refuses_on_following_view(tmp_path: Path):
    """「关注中」里都是还在跑的拍品，没有"清理"的语义。"""
    from meruki_tracker.tui.views import ListView

    tui = _cleanable(tmp_path)
    assert tui._view is ListView.FOLLOWING
    tui._open_clean_confirm()
    assert tui._screen is Screen.LIST, "不能进确认屏"
    assert any("Tab 切" in n for n in tui._notice)


async def test_clean_refuses_on_all_view(tmp_path: Path):
    """「全部」混着活数据，一键清理风险太高。"""
    from meruki_tracker.tui.views import ListView

    tui = _cleanable(tmp_path)
    tui._view = ListView.ALL
    tui._rebuild()
    tui._open_clean_confirm()
    assert tui._screen is Screen.LIST
    assert tui._notice


async def test_x_opens_clean_confirm_on_ended_view(tmp_path: Path):
    from meruki_tracker.tui.views import ListView

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    await _drive(tui, "\tx", expect_exit=False)  # Tab 到「已结束」再按 x
    assert tui._view is ListView.ENDED
    assert tui._screen is Screen.CLEAN_CONFIRM


async def test_invalid_view_is_cleanable(tmp_path: Path):
    """用户要的手动出口：详情一直拉不到的拍品能在「无法确认」里清掉。

    自动逻辑不变（`_needs_detail` 仍永久轮询），清理只动本地快照。
    """
    tui = _cleanable(tmp_path, snap=_snapshot_with_invalid())
    tui._view = ListView.INVALID
    tui._rebuild()
    assert [a.asin for a in tui._rows] == ["A4"], "这一档只放 INVALID 拍品"

    tui._open_clean_confirm()
    assert tui._screen is Screen.CLEAN_CONFIRM
    assert [a.asin for a in tui._clean_targets()] == ["A4"]

    tui._do_clean()
    assert tui._screen is Screen.LIST
    asins = sorted(a.asin for a in tui.snap.auctions)
    assert asins == ["A1", "A2", "A3"], f"只该删掉 A4，实际 {asins}"
    assert tui.ctx.snapshots.written is not None, "清理必须落库"


async def test_enter_on_clean_confirm_removes_only_that_view(tmp_path: Path):
    """确认后只删当前视图的，别的视图一条都不能少。"""
    from meruki_tracker.tui.views import ListView

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    await _drive(tui, "\tx\r", expect_exit=False)  # Tab → 已结束，x → 确认屏，Enter → 清理
    assert tui._screen is Screen.LIST
    asins = sorted(a.asin for a in tui.snap.auctions)
    assert asins == ["A1"], f"「已结束」视图里的 A2、A3 都该清掉，实际 {asins}"
    assert tui.ctx.snapshots.written is not None, "清理必须落库"
    assert tui._view is ListView.ENDED


async def test_esc_on_clean_confirm_changes_nothing(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    await _drive(tui, "\tx\x1b", expect_exit=False)  # Tab → 已结束，x 开确认屏，Esc 取消
    assert tui._screen is Screen.LIST
    assert len(tui.snap.auctions) == 3
    assert tui.ctx.snapshots.written is None


async def test_clean_write_failure_keeps_data(tmp_path: Path, monkeypatch):
    """写盘失败时快照不能变——否则界面上删了、重开又回来。"""
    from meruki_tracker.tui.views import ListView

    tui = _cleanable(tmp_path)
    tui._view = ListView.ENDED
    tui._rebuild()

    def boom(snap, *, allow_empty=False):
        raise OSError("磁盘满")

    monkeypatch.setattr(tui.ctx.snapshots, "write", boom)
    tui._open_clean_confirm()
    tui._do_clean()
    assert len(tui.snap.auctions) == 3, "写失败必须保留原数据"
    assert any("清理没能保存" in n for n in tui._notice)
    assert tui._screen is Screen.CLEAN_CONFIRM, "失败留在确认屏，让用户能重试"


def test_clean_targets_matches_current_view(tmp_path: Path):
    from meruki_tracker.tui.views import ListView

    tui = _cleanable(tmp_path)
    tui._view = ListView.ENDED
    tui._rebuild()
    assert [a.asin for a in tui._clean_targets()] == ["A2", "A3"]


def test_clean_refusal_names_both_cleanable_views(tmp_path: Path):
    """拒绝提示要说清到底哪几档能清理，不能只写「已结束」。"""
    tui = _cleanable(tmp_path)
    tui._open_clean_confirm()
    notice = " ".join(tui._notice)
    assert "已结束" in notice and "无法确认" in notice, notice


def test_footer_advertises_clean_only_on_cleanable_views():
    from meruki_tracker.tui import screens

    def footer(can_clean: bool) -> str:
        return screens.footer_text("list", page=0, page_count=1, can_clean=can_clean, width=120)

    assert "x 清理" in footer(True)
    assert "x 清理" not in footer(False)


def test_footer_never_truncates_quit_key():
    """窄终端也不能把 q 退出挤掉——那是用户唯一的退出路径。"""
    from meruki_tracker.tui import screens

    for width in (30, 40, 50, 60, 70, 80):
        text = screens.footer_text("list", page=0, page_count=1, can_clean=True, width=width)
        assert "q 退出" in text, f"width={width} 丢了退出键: {text}"
        assert len(text) <= max(width, 1) or width >= 80, f"width={width} 超宽: {text}"


# ---------------------------------------------------------------- 补价格入口


def _noprice_ended(asin: str = "H1") -> AuctionState:
    from datetime import timedelta

    from meruki_tracker.core.domain.models import DoorState, EndTime

    base = datetime(2026, 11, 1, tzinfo=UTC)
    return AuctionState(
        asin=asin,
        name=f"历史 {asin}",
        url=f"https://x/{asin}",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ENDED,
        end_time=EndTime.exact(base - timedelta(days=1), NOW_FOR_TESTS),
        latest_bid=None,
        concluded_at=base - timedelta(days=1),
    )


async def test_enter_on_a_priceless_item_fetches_the_detail(tmp_path: Path):
    """列表上价格是「—」时，回车进详情应该自动去拉一次。

    用**进行中**的拍品测：这才是常态（刚关注的拍品还没拉过详情），
    而且证明这条逻辑不只在「已结束」视图生效。
    """
    priceless_active = replace(_auction(), latest_bid=None)
    snap = Snapshot(auctions=(priceless_active,))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    calls: list[str] = []

    async def fake_detail(state):
        calls.append(state.asin)
        return replace(state, latest_bid=8300.0)

    tui.ctx.refresher.refresh_detail_of = fake_detail  # type: ignore[method-assign]
    await _drive(tui, "\r", expect_exit=False)
    assert calls == ["A1"]
    assert tui._screen is Screen.DETAIL
    assert tui._detail is not None
    assert tui._detail.latest_bid == 8300.0


async def test_enter_on_a_priced_item_does_not_fetch(tmp_path: Path):
    """已经有价格的拍品，进详情不该白白多发一个请求。"""
    snap = Snapshot(auctions=(_auction(),))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    calls: list[str] = []

    async def fake_detail(state):
        calls.append(state.asin)
        return state

    tui.ctx.refresher.refresh_detail_of = fake_detail  # type: ignore[method-assign]
    await _drive(tui, "\r", expect_exit=False)
    assert calls == []


async def test_r_on_the_ended_view_refreshes_like_everyone_else(tmp_path: Path):
    """「已结束」「全部」两个视图的 r 和「关注中」完全一样。

    真实反馈：「已结束就依赖我们关注中的状态滚动。已结束，全部，这两个页面的
    刷新都按关注中的刷新逻辑来」。以前「已结束」的 r 会偷偷改成"翻 50 页同步
    历史"，现在这条路径删掉了，三个视图同一个入口。
    """
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    called: list[str] = []

    async def fake_refresh(*a, **k):
        called.append("refresh")

    tui._do_refresh = fake_refresh  # type: ignore[method-assign]
    tui._view = ListView.ENDED  # 直接设视图：一次喂 "\tr" 会竞态

    await _drive(tui, "r", expect_exit=False)

    # _drive 里 _prepare_start 有 Cookie 会先自动刷一次，按 r 之前的记录不算
    assert called, "按 r 必须真的刷新"
    assert called[-1] == "refresh", f"「已结束」视图的 r 就是刷新，实际 {called}"


async def test_r_on_the_following_view_refreshes_everything(tmp_path: Path):
    """别的视图按 r 走全量刷新，不会去翻 50 页历史。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    called: list[str] = []

    tui._start_ended_sync = lambda: called.append("ended")  # type: ignore[method-assign]

    async def fake_refresh(*a, **k):
        called.append("refresh")

    tui._do_refresh = fake_refresh  # type: ignore[method-assign]

    await _drive(tui, "r", expect_exit=False)
    assert tui._view is ListView.FOLLOWING
    # _drive 里 _prepare_start 有 Cookie 会先自动刷一次
    assert called[-1] == "refresh", f"关注中视图的 r 应走全量刷新，实际 {called}"
    assert "ended" not in called


def test_has_no_detail_only_when_no_price_and_no_failure_history():
    """判据是"价格没有 **且** 详情从没成功过"。

    只缺价格但详情拉到过（比如刚结束、成交价还没拿到）也算有详情了——进详情
    不该再自动打一次接口，那是后台轮询的活。
    """
    plain = _auction("A1", "从没拉到详情")
    assert Tui._has_no_detail(replace(plain, latest_bid=None, detail_failures=0)) is True
    assert Tui._has_no_detail(replace(plain, latest_bid=None, detail_failures=2)) is False
    assert Tui._has_no_detail(replace(plain, latest_bid=100.0, detail_failures=0)) is False


async def test_refresh_in_detail_reloads_observations(tmp_path: Path):
    """进详情后刷新，观测列表要跟着变。

    真实反馈：「进入详情之后刷新，观测列表不会变」。以前 ``_refresh_detail_view``
    只同步了 ``_detail`` 状态对象，观测是进屏那一刻读的一份拷贝，之后再也不动。
    """
    old = _auction(asin="A1")
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(old,))), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = old
    tui._detail_obs = [("2026-10-29T12:00:00", "list", "旧观测")]

    tui.ctx.observations.recent = lambda asin, n=5: [("2026-11-02T09:00:00", "detail", "新观测")]

    tui._refresh_detail_view()
    assert tui._detail_obs == [("2026-11-02T09:00:00", "detail", "新观测")]


def test_all_views_sort_by_end_time_descending(tmp_path: Path):
    """所有列表都按结束时间倒序（晚的在前），各视图翻页方向一致。

    真实反馈：「所有列表都是按结束时间倒序排序」。以前只有「已结束」倒序，
    关注中是"快结束的优先"，两个方向相反，切视图就得换脑子。
    """
    from datetime import timedelta

    from meruki_tracker.core.domain.models import DoorState, EndTime

    base = datetime(2026, 11, 1, tzinfo=UTC)
    soon = replace(
        _auction("SOON", "快结束"),
        end_time=EndTime.exact(base + timedelta(minutes=5), NOW_FOR_TESTS),
    )
    late = replace(
        _auction("LATE", "晚结束"),
        end_time=EndTime.exact(base + timedelta(days=40), NOW_FOR_TESTS),
    )
    ended_old = replace(
        _auction("EOLD", "早结束"),
        door_state=DoorState.ENDED,
        end_time=EndTime.exact(base - timedelta(days=30), NOW_FOR_TESTS),
    )
    ended_new = replace(
        _auction("ENEW", "刚结束"),
        door_state=DoorState.ENDED,
        end_time=EndTime.exact(base - timedelta(hours=1), NOW_FOR_TESTS),
    )
    pending = replace(_auction("PEND", "没有时间"), end_time=EndTime.pending())
    snap = Snapshot(auctions=(soon, late, ended_old, ended_new, pending))

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())

    tui._view = ListView.FOLLOWING
    tui._rebuild()
    assert [r.asin for r in tui._rows] == ["LATE", "SOON", "PEND"], "关注中也该是晚的在前"

    tui._view = ListView.ENDED
    tui._rebuild()
    assert [r.asin for r in tui._rows] == ["ENEW", "EOLD"], "已结束仍是最新在前"

    tui._view = ListView.ALL
    tui._rebuild()
    assert [r.asin for r in tui._rows] == ["LATE", "SOON", "ENEW", "EOLD", "PEND"]


# ------------------------------------------------- 详情屏的 r 只刷当前这一条


async def test_detail_refresh_note_names_what_changed(tmp_path: Path, monkeypatch):
    """刷完要说清楚**变了什么**，不是干巴巴一句"已刷新"。

    真实反馈：「在详情里面摁了刷新还有刷新完了你要加一个描述」。用户按 r 就是
    想知道有没有变，所以变化的字段要点名。
    """
    from dataclasses import replace as _replace

    from meruki_tracker.tui.tui import Tui as _Tui

    old = _replace(_auction(asin="A1"), latest_bid=1000.0)
    tui = _Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(old,))), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = tui._rows[0]

    async def fake_refresh_of(state):
        return _replace(state, latest_bid=8888.0)

    monkeypatch.setattr(tui.ctx.refresher, "refresh_detail_of", fake_refresh_of)
    tui._refresh_current_detail()
    # 按下的瞬间就得有反馈，否则详情屏画面不动，用户以为按键没生效
    assert any("正在刷新" in n for n in tui._notice), tui._notice
    await asyncio.sleep(0)

    assert any("已刷新" in n and "¥8,888" in n for n in tui._notice), tui._notice


async def test_detail_refresh_note_says_when_nothing_changed(tmp_path: Path, monkeypatch):
    """没变化也要明说，别让人怀疑根本没拉到。"""
    from meruki_tracker.tui.tui import Tui as _Tui

    old = _auction(asin="A1")
    tui = _Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(old,))), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = tui._rows[0]

    async def fake_refresh_of(state):
        return state

    monkeypatch.setattr(tui.ctx.refresher, "refresh_detail_of", fake_refresh_of)
    tui._refresh_current_detail()
    await asyncio.sleep(0)

    assert any("数据没有变化" in n for n in tui._notice), tui._notice


async def test_detail_r_refreshes_only_the_current_item(tmp_path: Path, monkeypatch):
    """详情屏按 r 只重拉这一条，**不整体刷新、不把用户甩回列表**。

    真实反馈：「详情页面里面 r 刷新之后 提示要我退回列表，我理解在详情里面的 r，
    应该仅刷新当前 id 对应的数据」。以前这里走 ``_start_refresh()``（整体刷新），
    按完就回到列表屏了——在详情页按刷新却被弹出去，动作和结果对不上。

    这里直接调 ``_refresh_current_detail``（r 键绑定的就是它）：走 ``_drive`` 的话
    ``_prepare_start()`` 会先跑一次整体刷新，把 DETAIL 屏覆盖掉，测不到目标行为。
    """
    from dataclasses import replace as _replace

    from meruki_tracker.tui.tui import Tui as _Tui

    old = _auction(asin="A1")
    snap = Snapshot(auctions=(old, _replace(old, asin="A2", name="另一个")))
    tui = _Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = next(r for r in tui._rows if r.asin == "A1")

    seen: list[str] = []

    async def fake_refresh_of(state):
        seen.append(state.asin)
        return _replace(state, latest_bid=4321.0)

    async def fake_do_refresh(*a, **k):  # 整体刷新不该被调用
        seen.append("<whole>")

    monkeypatch.setattr(tui.ctx.refresher, "refresh_detail_of", fake_refresh_of)
    monkeypatch.setattr(tui, "_do_refresh", fake_do_refresh)

    tui._refresh_current_detail()
    await asyncio.sleep(0)

    assert seen == ["A1"], f"只该拉当前这一条，实际 {seen}"
    assert tui._screen is Screen.DETAIL, "刷新后必须还停在详情屏"


async def test_detail_refresh_lands_and_stays_on_detail(tmp_path: Path, monkeypatch):
    """拉回来的新值要落到 _detail 与列表行，并且屏幕仍是详情。"""
    from dataclasses import replace as _replace

    from meruki_tracker.tui.tui import Tui as _Tui

    old = _auction(asin="A1")
    tui = _Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(old,))), Config())
    tui._rebuild()
    tui._screen = Screen.DETAIL
    tui._detail = tui._rows[0]

    async def fake_refresh_of(state):
        return _replace(state, latest_bid=4321.0)

    monkeypatch.setattr(tui.ctx.refresher, "refresh_detail_of", fake_refresh_of)
    tui._refresh_current_detail()
    await asyncio.sleep(0)

    assert tui._detail is not None and tui._detail.latest_bid == 4321.0
    assert tui._rows[0].latest_bid == 4321.0
    assert tui._screen is Screen.DETAIL
    assert tui._busy == ""


async def test_r_key_in_detail_is_bound_to_the_single_item_refresh(tmp_path: Path):
    """**真正按键**验证 r 绑定的是"只刷这一条"，不是整体刷新。

    为什么单独要这一条：上面的用例直接调 ``_refresh_current_detail``，验证的是
    方法本身；如果哪天有人把键绑定改回 ``_start_refresh()``，那个用例照样绿。
    这里靠 ``_drive`` 喂真实的 ``r``，才守得住"键绑在哪个方法上"。

    ``_prepare_start`` 被换掉：它一跑就会整体刷新，把 DETAIL 屏覆盖掉。
    """
    from dataclasses import replace as _replace

    from meruki_tracker.tui.tui import Tui as _Tui

    old = _auction(asin="A1")
    snap = Snapshot(auctions=(old, _replace(old, asin="A2", name="另一个")))
    tui = _Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    tui._prepare_start = lambda: None  # type: ignore[method-assign]
    tui._screen = Screen.DETAIL
    tui._detail = next(r for r in tui._rows if r.asin == "A1")

    seen: list[str] = []

    async def fake_refresh_of(state):
        seen.append(state.asin)
        return _replace(state, latest_bid=4321.0)

    async def fake_do_refresh(*a, **k):
        seen.append("<whole>")

    tui.ctx.refresher.refresh_detail_of = fake_refresh_of  # type: ignore[method-assign]
    tui._do_refresh = fake_do_refresh  # type: ignore[method-assign]

    await _drive(tui, "r", expect_exit=False)
    await asyncio.sleep(0)

    assert seen == ["A1"], f"详情屏的 r 只该拉当前这一条，实际 {seen}"
    assert tui._screen is Screen.DETAIL, "刷新后必须还停在详情屏"


# ------------------------------------------------- 刷新后界面按最新数据重排


def _sorted_item(asin: str, name: str, hours: float) -> AuctionState:
    """造一条「hours 小时后结束」的进行中拍品，用来验证排序。"""
    from datetime import timedelta

    now = datetime.now(UTC)
    return replace(
        _auction(asin=asin, name=name),
        end_time=EndTime.exact(now + timedelta(hours=hours), now),
        last_seen_at=now,
    )


async def test_refresh_keeps_the_cursor_on_the_same_item(tmp_path: Path, monkeypatch):
    """刷新重排后，光标要跟着**原来那条拍品**走，不是停在同一个下标。

    排序键是结束时间，一次刷新就可能把它挪走（自动延时会把它顶到最前）。
    只夹下标的话，用户盯着的那条会在眼皮底下换成另一条拍卖。
    """
    from meruki_tracker.tui.tui import Tui as _Tui

    before = [_sorted_item("A", "甲", 5), _sorted_item("B", "乙", 3), _sorted_item("C", "丙", 1)]
    tui = _Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=tuple(before))), Config())
    tui._rebuild()
    tui._cursor = 1
    focused = tui._rows[1].asin
    assert focused == "B"

    # C 被自动延长到 99 小时后：重排后它跑到最前，B 挪到第 3 位
    after = [_sorted_item("A", "甲", 5), _sorted_item("B", "乙", 3), _sorted_item("C", "丙", 99)]

    async def fake_refresh(ctx, snap, progress, **kwargs):
        return (
            Snapshot(auctions=tuple(after)),
            RefreshResult(ok=True),
        )

    monkeypatch.setattr(tui_mod, "do_refresh", fake_refresh)
    await tui._do_refresh()

    assert [r.asin for r in tui._rows] == ["C", "A", "B"]
    assert tui._rows[tui._cursor].asin == focused, "光标必须还在原来那条拍品上"


async def test_refresh_moves_the_cursor_when_the_item_left_the_view(tmp_path: Path, monkeypatch):
    """原来那条不在本视图了（比如刚结束），光标要夹到合法位置，不能指空。"""
    from meruki_tracker.tui.tui import Tui as _Tui

    before = [_sorted_item("A", "甲", 5), _sorted_item("B", "乙", 3)]
    tui = _Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=tuple(before))), Config())
    tui._rebuild()
    tui._cursor = 1
    gone = tui._rows[1].asin

    async def fake_refresh(ctx, snap, progress, **kwargs):
        # B 消失了，只剩 A
        return Snapshot(auctions=(_sorted_item("A", "甲", 5),)), RefreshResult(ok=True)

    monkeypatch.setattr(tui_mod, "do_refresh", fake_refresh)
    await tui._do_refresh()

    assert gone == "B"
    assert [r.asin for r in tui._rows] == ["A"]
    assert 0 <= tui._cursor < len(tui._rows), "光标必须落在合法范围内"


_DESC_RAW = {
    "goodsInfo": {"images": ["https://img/1.jpg", "https://img/2.jpg"]},
    "other": {"descText": "一行目\n二行目\n三行目\n四行目"},
}


def _detail_ctx(tmp_path: Path) -> _Ctx:
    ctx = _Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(_auction("A1", "尼康 F3"),)))
    ctx.mirror = _Mirror({"A1": _DESC_RAW})
    return ctx


async def test_detail_screen_shows_the_rule_flags_from_the_mirror(tmp_path: Path):
    """详情屏的「提前结束 / 自动延长」来自详情镜像，不是凭空编的。

    这两条是拍品的规则属性（详情 ``other.earlyTermination`` /
    ``goodsInfo.autoExtend``），不在 ``AuctionState`` 里，所以只能从镜像读。
    """
    from meruki_tracker.core.adapters.store.mirror import StoredDetail

    ctx = _detail_ctx(tmp_path)
    ctx.mirror = _Mirror(
        {"A1": _DESC_RAW},
        {
            "A1": StoredDetail(
                asin="A1",
                name="尼康 F3",
                end_at=None,
                ended=False,
                bid_price=12500.0,
                buy_now_price=None,
                auto_extend=True,
                early_termination=False,
                observed_at="2026-11-16T12:00:00+00:00",
            )
        },
    )
    tui = Tui(ctx, Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)

    out = _draw_body(tui)
    assert "提前结束" in out and "不会" in out
    assert "自动延长" in out


async def test_detail_screen_leaves_rule_flags_blank_without_a_mirror_row(tmp_path: Path):
    """镜像里没这行（还没拉过详情）就不画，免得把"不知道"画成"不会"。"""
    tui = Tui(_detail_ctx(tmp_path), Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)

    out = _draw_body(tui)
    assert "提前结束" not in out
    assert "自动延长" not in out


async def test_detail_screen_shows_the_description(tmp_path: Path):
    tui = Tui(_detail_ctx(tmp_path), Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)

    out = _draw_body(tui)
    assert "描述" in out
    assert "一行目" in out, "描述正文要显示出来"


async def test_detail_screen_does_not_show_images(tmp_path: Path):
    """图片要去掉：挖煤姬的图片地址需要认证，终端里点开也是 403。

    真实反馈：「把图片去了吧，要认证，看不了」。
    """
    tui = Tui(_detail_ctx(tmp_path), Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)

    out = _draw_body(tui)
    assert "图片" not in out
    assert "img/1.jpg" not in out


async def test_detail_screen_hints_that_the_full_text_is_behind_v(tmp_path: Path):
    """描述超过 3 行时要提示还有多少，否则用户以为就这几行。"""
    tui = Tui(_detail_ctx(tmp_path), Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)

    assert "按 v 看全文" in _draw_body(tui)


async def test_v_opens_the_full_description_screen(tmp_path: Path):
    tui = Tui(_detail_ctx(tmp_path), Config())
    tui._rebuild()
    await _drive(tui, "\rv", expect_exit=False)

    assert tui._screen is Screen.DETAIL_DESC
    out = _draw_body(tui)
    assert "四行目" in out, "全文屏要显示到最后一行的内容"


async def test_description_screen_scrolls_and_stops_at_the_bottom(tmp_path: Path):
    many = "\n".join(f"第{i}行" for i in range(60))
    ctx = _detail_ctx(tmp_path)
    ctx.mirror = _Mirror({"A1": {"other": {"descText": many}}})
    tui = Tui(ctx, Config())
    tui._rebuild()
    await _drive(tui, "\rv", expect_exit=False)

    await _drive(tui, "\x1b[B", expect_exit=False)
    assert tui._desc_scroll == 1
    await _drive(tui, "\x1b[A", expect_exit=False)
    assert tui._desc_scroll == 0, "已经在顶部，再往上不该变成负数"


async def test_escape_leaves_the_description_screen(tmp_path: Path):
    tui = Tui(_detail_ctx(tmp_path), Config())
    tui._rebuild()
    await _drive(tui, "\rv\x1b", expect_exit=False)

    assert tui._screen is Screen.DETAIL


async def test_t_toggles_translation_globally(tmp_path: Path):
    """t 是**全局开关**：关掉看原文，但译文仍留在缓存里（下次开不用重翻）。"""
    ctx = _detail_ctx(tmp_path)
    tui = Tui(ctx, Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)

    await _drive(tui, "t", expect_exit=False)
    assert ctx.api.translate_calls, "按 t 要真的发翻译请求"
    assert tui.cfg.translate.enable is True
    assert tui._translations["A1"][1].startswith("译:")
    assert "译:" in _draw_body(tui)

    await _drive(tui, "t", expect_exit=False)
    assert tui.cfg.translate.enable is False
    assert "A1" in tui._translations, "关掉翻译只是不显示，不该丢掉已翻好的译文"
    assert "译:" not in _draw_body(tui), "关掉之后要显示原文"


async def test_t_translates_the_title_too(tmp_path: Path):
    """翻译了描述却不翻标题只算做了一半——日文标题在中文界面里同样看不懂。"""
    ctx = _detail_ctx(tmp_path)
    tui = Tui(ctx, Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)
    assert "尼康 F3" in _draw_body(tui)

    await _drive(tui, "t", expect_exit=False)

    out = _draw_body(tui)
    assert "译:尼康 F3" in out, "标题要换成译文"


async def test_translation_failure_does_not_cache_a_blank(tmp_path: Path):
    """翻译失败不能留下"已翻译"的空壳，否则用户再按 t 就被切回原文且没有提示。"""
    ctx = _detail_ctx(tmp_path)

    from meruki_tracker.core.errors import NetworkError

    async def boom(**kwargs):
        raise NetworkError("网络炸了")

    ctx.api.translate = boom
    tui = Tui(ctx, Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)
    await _drive(tui, "t", expect_exit=False)

    assert tui.cfg.translate.enable is True, "开关本身要生效（只是没有译文）"
    assert "A1" not in tui._translations
    # 单条翻译失败走 _translate_one 的 None 分支，界面回落原文而不是空白
    assert "尼康 F3" in _draw_body(tui)


# ------------------------------------------------------------------ 全局翻译开关


async def test_t_on_the_list_translates_the_visible_items(tmp_path: Path):
    """列表按 t 也是同一个全局开关：翻完直接显示中文标题。"""
    ctx = _Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(_auction("A1", "ニコン F3"),)))
    tui = Tui(ctx, Config())
    tui._rebuild()
    await _drive(tui, "t", expect_exit=False)

    assert tui.cfg.translate.enable is True
    assert ctx.api.translate_calls, "列表按 t 要真的发翻译请求"
    assert tui._translations["A1"][0] == "译:ニコン F3"
    assert "译:ニコン F3" in _draw_body(tui), "列表要显示译文标题"


async def test_turning_translation_off_shows_the_original_titles(tmp_path: Path):
    ctx = _Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=(_auction("A1", "ニコン F3"),)))
    tui = Tui(ctx, Config())
    tui._rebuild()
    await _drive(tui, "t", expect_exit=False)
    await _drive(tui, "t", expect_exit=False)

    assert tui.cfg.translate.enable is False
    out = _draw_body(tui)
    assert "ニコン F3" in out
    assert "译:ニコン F3" not in out


async def test_opening_a_detail_translates_it_when_the_switch_is_on(tmp_path: Path):
    """开关打开后进详情自动翻，用户不该为"帮我翻一下"再按一次 t。

    cookie 留空是有意的：有 cookie 时 ``_prepare_start`` 会自动刷新一轮，
    刷新完成后的"补齐译文"会掩盖掉进详情这条路径，测试就测不出东西了。
    """
    ctx = _detail_ctx(tmp_path)
    ctx.credentials = _Cred("")
    tui = Tui(ctx, Config())
    tui._rebuild()
    # 直接开开关（等价于用户先按过 t），再进详情
    tui.cfg = apply_edit(tui.cfg, "translate.enable", "开")
    tui._translations = {}
    ctx.api.translate_calls.clear()
    await _drive(tui, "\r", expect_exit=False)

    assert ctx.api.translate_calls, "开翻译后进详情要自动翻"
    assert tui._translations["A1"][0] == "译:尼康 F3"
    assert "译:尼康 F3" in _draw_body(tui)


async def test_refreshing_a_detail_also_refreshes_the_translation(tmp_path: Path):
    """刷新详情要顺手重翻：卖家改标题是常事，缓存里的旧译文不该一直用。

    cookie 留空，理由同上：不让启动刷新那一路自动补译文把结果搅浑。
    """
    ctx = _detail_ctx(tmp_path)
    ctx.credentials = _Cred("")
    tui = Tui(ctx, Config())
    tui._rebuild()
    tui.cfg = apply_edit(tui.cfg, "translate.enable", "开")
    await _drive(tui, "\r", expect_exit=False)
    ctx.api.translate_calls.clear()

    await _drive(tui, "r", expect_exit=False)

    assert ctx.api.translate_calls, "刷新详情要带着调一次翻译"


async def test_settings_enter_flips_a_switch_instead_of_asking_for_text(tmp_path: Path):
    """开关项是二选一，按 Enter 直接翻面；弹编辑屏要用户敲"开"再回车是填空题。

    直接调 ``_begin_edit``（Enter 的处理函数）——走完整 pty 会被
    ``_prepare_start`` 抢先把屏幕改成登录/列表，测不到这一段。
    """
    ctx = _Ctx(tmp_path, cookie="sid=x")
    tui = Tui(ctx, Config())
    tui._rebuild()
    tui._screen = Screen.SETTINGS
    tui._settings_cursor = next(
        i for i, opt in enumerate(tui._visible_options()) if opt.key == "translate.enable"
    )

    tui._begin_edit()

    assert tui._screen is Screen.SETTINGS, "不该进编辑屏"
    assert tui.cfg.translate.enable is True
    assert ctx.settings.get("translate.enable") is True


# ------------------------------------------------------------------ 队列侧栏


def test_sidebar_hidden_below_80_columns(tmp_path: Path):
    """窄屏（<80 列）自动隐藏侧栏（用户拍板 4）。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen_size = lambda: (79, 24)
    assert tui._sidebar_visible() is False
    tui._screen_size = lambda: (80, 24)
    assert tui._sidebar_visible() is True
    tui._screen_size = lambda: (120, 24)
    assert tui._sidebar_visible() is True


def test_render_body_subtracts_sidebar_width(tmp_path: Path):
    """主体 Console 宽度必须扣掉侧栏，否则各屏按不存在的列数排版（文档集 07 的坑）。

    把 ``_draw`` 换成只记录 ``console.width`` 的探针：侧栏可见时应该是
    ``终端宽 - 18``，隐藏时是完整终端宽。
    """
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    seen: list[int] = []
    tui._draw = lambda console: seen.append(console.width)
    tui._screen_size = lambda: (100, 30)
    tui._render_body()
    tui._screen_size = lambda: (60, 30)
    tui._render_body()
    assert seen == [82, 60], seen


def test_noticebar_shows_only_the_two_hour_window():
    """侧栏只画未来 2 小时内将要通知的；放不下的用「还有 N 条」明说。"""
    from datetime import UTC, datetime, timedelta
    from io import StringIO

    from rich.console import Console

    from meruki_tracker.core.app.notices import KIND_ENDING, KIND_OUTBID, Notice
    from meruki_tracker.tui import screens

    now = datetime.now(UTC)
    notices = (
        Notice(
            kind=KIND_ENDING,
            asin="A1",
            title="15 分钟后结束",
            body="拍品一",
            due_at=now + timedelta(minutes=15),
            minutes_before_end=15.0,
        ),
        Notice(
            kind=KIND_OUTBID,
            asin="A2",
            title="出价被超过",
            body="拍品二（拍品 A2）",
            due_at=now + timedelta(minutes=50),
        ),
        Notice(
            kind=KIND_ENDING,
            asin="A3",
            title="15 分钟后结束",
            body="拍品三",
            due_at=now + timedelta(minutes=90),
            minutes_before_end=15.0,
        ),
    )

    buf = StringIO()
    screens.render_noticebar(
        Console(file=buf, width=18, height=24, force_terminal=False), notices, now=now, tz=TZ
    )
    out = buf.getvalue()
    assert "通知队列" in out
    assert "结束提醒" in out
    assert "出价被超过" in out
    assert "拍品一" in out

    for h in (1, 2, 3, 5, 7, 12, 24):
        buf = StringIO()
        screens.render_noticebar(
            Console(file=buf, width=18, height=h, force_terminal=False), notices, now=now, tz=TZ
        )
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) <= h, (h, lines)
    # 高度小到列不下时要明说，而不是静默消失
    buf = StringIO()
    screens.render_noticebar(
        Console(file=buf, width=18, height=3, force_terminal=False), notices, now=now, tz=TZ
    )
    assert "还有" in buf.getvalue() or "排不下" in buf.getvalue()


def test_noticebar_empty_state():
    """没有将到点的通知时明确写「暂无」，不留一块莫名其妙的空白。"""
    from datetime import UTC, datetime
    from io import StringIO

    from rich.console import Console

    from meruki_tracker.tui import screens

    buf = StringIO()
    screens.render_noticebar(
        Console(file=buf, width=18, height=10, force_terminal=False),
        (),
        now=datetime.now(UTC),
        tz=TZ,
    )
    out = buf.getvalue()
    assert "暂无" in out
    assert "0 条" in out


async def test_render_noticebar_reads_live_board(tmp_path: Path):
    """侧栏读的是真实通知板：注册一条结束提醒后立刻能在渲染结果里看到。"""
    from datetime import UTC, datetime, timedelta

    from meruki_tracker.core.app.notices import NoticeBoard

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen_size = lambda: (100, 30)
    board = tui.ctx.notices
    assert isinstance(board, NoticeBoard)
    now = datetime.now(UTC)
    board.add_outbid(msg_id=1, asin="A9", title="甲", now=now + timedelta(minutes=5))
    out = _ANSI_RE.sub("", str(tui._render_noticebar()))
    assert "出价被超过" in out, out
    # 第二行是「HH:MM 正文」；18 列侧栏下正文会被截，短名字必须完整活下来
    assert "甲" in out, out


async def test_fire_notices_flashes_and_delivers(tmp_path: Path):
    """到点的通知：投递给 Notifier、从板上收走、在界面闪一条提示。"""
    from datetime import UTC, datetime

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    board = tui.ctx.notices
    delivered: list = []

    class _Recorder:
        def notify(self, note) -> None:
            delivered.append(note)

    board._notifier = _Recorder()
    now = datetime.now(UTC)
    board.add_outbid(msg_id=7, asin="A1", title="拍品甲", now=now)

    tui._fire_notices()

    assert len(delivered) == 1
    assert delivered[0].kind == "outbid"
    assert any("▲" in line for line in tui._notice), tui._notice
    # 收走后不重复触发
    tui._fire_notices()
    assert len(delivered) == 1


# ------------------------------------------------- 用户动作 / 自动任务两层执行器


# ------------------------------------------------------------------ 设置：可选字符串可清空


def test_clearing_an_optional_string_option_is_allowed(tmp_path: Path):
    """地址/密码/令牌这类"可选字符串"要能清空——不然设过一次就再也去不掉。"""
    tui = Tui(_Ctx(tmp_path), Config())
    tui._screen = Screen.SETTING_EDIT
    tui._editing_key = "channels.webhook_url"
    tui._input_text = ""

    assert apply_edit(tui.cfg, "channels.webhook_url", "https://x/y").channels.webhook_url
    tui.cfg = apply_edit(tui.cfg, "channels.webhook_url", "https://x/y")
    tui._commit_edit()

    assert tui.cfg.channels.webhook_url == ""
    assert tui._screen is Screen.SETTINGS, "保存成功后回设置列表"


def test_clearing_a_required_option_still_complains(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path), Config())
    tui._screen = Screen.SETTING_EDIT
    tui._editing_key = "timezone"  # 必填项：清空要拦住
    tui._input_text = ""
    tui._commit_edit()
    assert "请输入新值" in tui._notice[0]


# ---------------------------------------------------------------- 扫码挂载


class _FakeProvider:
    kind = "wechat"
    label = "微信"

    def __init__(self) -> None:
        self.polls = 0
        self.fetches = 0
        self.closed = False

    def fetch(self):
        from meruki_tracker.core.app.qrlogin import QrChallenge

        self.fetches += 1
        return QrChallenge(kind="wechat", payload="http://x/1", hint="扫码", token="t")

    def poll(self, challenge):
        from meruki_tracker.core.app.qrlogin import STATE_PENDING, QrStatus

        del challenge
        self.polls += 1
        return QrStatus(STATE_PENDING, "等待扫码")

    def close(self) -> None:
        self.closed = True


def test_m_is_no_longer_a_list_shortcut(tmp_path: Path):
    """挂载只从「配置 → 通知」进，列表页不该再有 ``m``。

    同一件事在列表和配置页各开一个入口，用户会问"到底哪个才是正经入口"；
    配置页那行还是有状态的（显示"已挂载 102xxxxx"），列表页只是裸跳转。
    """
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT
    from meruki_tracker.tui import screens

    assert "m 挂载" not in screens.footer_text("list", width=200)
    # 动作项还在配置页里，一条路走到底
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    assert ACTION_MOUNT_WECHAT in tui._action_texts()


async def test_pressing_m_on_the_list_does_nothing(tmp_path: Path, monkeypatch):
    """键真的解绑了：喂一个 ``m`` 过去，屏幕和挂载任务都不该有任何变化。"""
    provider = _FakeProvider()
    monkeypatch.setattr(tui_mod, "build_provider", lambda channels, kind="": provider)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    await _drive(tui, "m", expect_exit=False)
    assert tui._screen is Screen.LIST, "m 不该再开挂载屏"
    assert tui._mount_task is None, "更不该偷偷去取码"
    assert provider.fetches == 0


async def test_mount_rows_stay_hidden_until_the_channel_is_on(tmp_path: Path):
    """渠道关着时「扫描…二维码」那一行根本不出现。

    以前是"摆一行、点进去才告诉你没开渠道"——扫码用的是**还没填**的凭据，
    渠道关着时它根本没有意义。现在打开开关，这行才长在凭据下面。
    """
    from meruki_tracker.core.config_options import (
        ACTION_MOUNT_QQ,
        ACTION_MOUNT_WECHAT,
        GROUP_NOTIFY,
    )

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._open_settings()
    tui._config_group = GROUP_NOTIFY
    keys = [opt.key for opt in tui._visible_options()]
    assert ACTION_MOUNT_WECHAT not in keys
    assert ACTION_MOUNT_QQ not in keys

    tui.cfg = cfg_with_channels(wechat_enable=True)
    keys = [opt.key for opt in tui._visible_options()]
    assert ACTION_MOUNT_WECHAT in keys, "微信开了就该出现微信扫码行"
    assert ACTION_MOUNT_QQ not in keys, "QQ 还关着，不该抢一行"


async def test_mount_action_without_channels_points_at_settings(tmp_path: Path):
    """就算硬调进来（比如配置刚从库里改过），也别开一张空屏。"""
    from meruki_tracker.core.app.qrlogin import KIND_WECHAT

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._open_settings()
    tui._open_mount(KIND_WECHAT)

    assert tui._screen is Screen.SETTINGS, "不开一张空白的二维码屏"
    assert any("还没开机器人渠道" in n for n in tui._notice)


async def test_mount_action_opens_the_mount_screen(tmp_path: Path, monkeypatch):
    """「配置 → 通知 → 扫描微信机器人二维码」按 Enter 就是原来那个挂载屏。"""
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT

    provider = _FakeProvider()
    monkeypatch.setattr(tui_mod, "build_provider", lambda channels, kind="": provider)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._open_settings()
    tui._run_action(ACTION_MOUNT_WECHAT)
    assert tui._screen is Screen.MOUNT
    # 码取回来了：取码是个后台任务，等它跑完
    await asyncio.sleep(0.1)
    assert tui._mount_challenge is not None
    assert tui._mount_challenge.payload == "http://x/1"
    await tui._shutdown()


async def test_escape_leaves_mount_and_cancels_polling(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr(tui_mod, "build_provider", lambda channels, kind="": provider)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._open_settings()
    tui._open_mount()
    await _drive(tui, "\x1b", expect_exit=False)
    assert tui._screen is Screen.SETTINGS, "挂载屏只从配置页进，Esc 回配置页"
    assert tui._mount_task is None
    assert provider.polls == 0, "Esc 之后不该再问网关"


async def test_mount_loop_reports_failure_instead_of_hanging(tmp_path: Path, monkeypatch):
    """网关连不上时要把原因写在屏上，而不是卡在"正在取码"。"""
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT
    from meruki_tracker.core.errors import NetworkError

    class _Boom(_FakeProvider):
        def fetch(self):
            raise NetworkError("连不上网关")

    monkeypatch.setattr(tui_mod, "build_provider", lambda channels, kind="": _Boom())
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._open_settings()
    tui._run_action(ACTION_MOUNT_WECHAT)
    await asyncio.sleep(0.2)
    assert tui._mount_state == "failed"
    assert "连不上网关" in tui._mount_message
    await tui._shutdown()


async def test_mount_success_persists_the_credential_and_starts_polling(tmp_path: Path):
    """扫码成功后要做两件事：把凭据存进设置，以及起收消息的循环。

    少了后者，之后「主动提醒」永远发不出去——context_token 只能从入站消息里来。
    """

    class _Ok(_FakeProvider):
        def poll(self, challenge):
            from meruki_tracker.core.app.qrlogin import STATE_SUCCESS, QrStatus

            del challenge
            return QrStatus(
                STATE_SUCCESS,
                "已挂载：bot-1",
                account="bot-1",
                credential="tok-new",
                base_url="https://ilink.example",
            )

    ctx = _Ctx(tmp_path, cookie="sid=x")
    started: list[str] = []

    class _Bot:
        async def run(self):
            started.append("run")

    ctx.wechat = _Bot()  # type: ignore[assignment]
    tui = Tui(ctx, Config())
    tui._mount_task = None
    tui._on_mounted(_Ok(), _Ok().poll(None))

    assert ctx.settings.get("channels.wechat_token") == "tok-new"
    assert ctx.settings.get("channels.wechat_base_url") == "https://ilink.example"
    assert any("已挂载" in line for line in tui._notice)
    assert tui._wechat_task is not None
    await tui._shutdown()


async def test_qq_mount_persists_app_id_and_secret(tmp_path: Path):
    """QQ 扫码换回的是两个值（AppID + AppSecret），两个都要落盘。

    只存一个等于没存：连 WebSocket 要 AppID 换 token，换 token 要 AppSecret。
    """
    import json as _json

    class _Qq(_FakeProvider):
        kind = "qq"
        label = "QQ"

        def fetch(self):
            from meruki_tracker.core.app.qrlogin import KIND_QQ, QrChallenge

            return QrChallenge(kind=KIND_QQ, payload="https://q.qq.com/x", hint="扫码")

    ctx = _Ctx(tmp_path, cookie="sid=x")
    tui = Tui(ctx, Config())
    tui._mount_task = None
    status = type(
        "S",
        (),
        {
            "state": "success",
            "message": "",
            "account": "1020001",
            "credential": _json.dumps({"app_id": "1020001", "app_secret": "sec-9"}),
            "base_url": "",
        },
    )()
    tui._on_mounted(_Qq(), status)

    assert ctx.settings.get("channels.qq_app_id") == "1020001"
    assert ctx.settings.get("channels.qq_app_secret") == "sec-9"
    assert any("QQ 机器人已挂载" in line for line in tui._notice)
    await tui._shutdown()


async def test_qq_mount_rejects_a_malformed_credential(tmp_path: Path):
    """凭据读不出来时不能假装成功——否则用户以为挂好了，提醒却发不出去。"""
    ctx = _Ctx(tmp_path, cookie="sid=x")
    tui = Tui(ctx, Config())
    tui._mount_task = None

    class _Qq(_FakeProvider):
        kind = "qq"

    status = type(
        "S", (), {"credential": "not-json", "account": "", "base_url": "", "message": ""}
    )()
    tui._on_mounted(_Qq(), status)

    assert ctx.settings.get("channels.qq_app_id") in (None, "")
    assert any("读不出来" in line for line in tui._notice)
    await tui._shutdown()


def test_qq_wins_over_wechat_when_both_are_on():
    """两个都开时先做 QQ：一步换回凭据，不用等手机确认。"""
    from meruki_tracker.core.app.qrlogin import KIND_QQ, build_provider
    from meruki_tracker.core.config import ChannelsConfig

    provider = build_provider(ChannelsConfig(qq_enable=True, wechat_enable=True))
    assert provider is not None
    assert provider.kind == KIND_QQ


# ----------------------------------------------------- 通知的节奏（notify.*）


def _ending_board(tmp_path: Path, *, points: tuple[float, ...]):
    """造一个只含"90 分钟后结束"拍品的 Tui，并按给的点同步提醒。"""
    from meruki_tracker.core.config import Config as _Config
    from meruki_tracker.core.config import NotifyConfig
    from meruki_tracker.core.domain.models import EndTime

    soon = replace(
        _auction("A1", "快结束的"),
        end_time=EndTime.exact(datetime.now(UTC) + timedelta(minutes=90), datetime.now(UTC)),
    )
    snap = Snapshot(auctions=(soon,))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), _Config())
    tui.cfg = replace(tui.cfg, notify=NotifyConfig(points_minutes=points))
    tui._sync_notices()
    return tui


def test_sync_notices_reads_the_points_and_nothing_else(tmp_path: Path):
    """提醒点只有 ``notify.points_minutes`` 一个来源，没有第二个旋钮能改它。"""
    tui = _ending_board(tmp_path, points=(60.0,))
    tui._sync_notices()
    board = tui.ctx.notices
    points = {n.minutes_before_end for n in board.snapshot(datetime.now(UTC))}
    assert points <= {60.0}, f"只该有 notify.points_minutes 那一个点，实际 {points}"


async def test_changing_the_notify_cadence_takes_effect_immediately(tmp_path: Path):
    """配置页改完不用等下一轮刷新：``_apply_config`` 里就把提醒重算一遍。"""
    from meruki_tracker.core.config import NotifyConfig

    tui = _ending_board(tmp_path, points=(60.0,))
    before = len(tui.ctx.notices.snapshot(datetime.now(UTC)))

    tui._apply_config(
        replace(
            tui.cfg,
            notify=NotifyConfig(points_minutes=(60.0, 40.0, 20.0), window_minutes=120),
        )
    )
    after = {n.minutes_before_end for n in tui.ctx.notices.snapshot(datetime.now(UTC))}
    assert after == {60.0, 40.0, 20.0}, "改完立刻按新点重算"
    assert before == 1
    await tui._shutdown()


async def test_changing_the_notify_window_takes_effect_immediately(tmp_path: Path):
    from meruki_tracker.core.config import NotifyConfig

    tui = _ending_board(tmp_path, points=(60.0,))
    tui._apply_config(replace(tui.cfg, notify=NotifyConfig(window_minutes=30)))
    assert tui.ctx.notices._window == timedelta(minutes=30)
    await tui._shutdown()


def test_notify_rows_show_up_in_the_notify_group(tmp_path: Path):
    from meruki_tracker.core.config_options import GROUP_NOTIFY

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    keys = [o.key for o in _open_group(tui, GROUP_NOTIFY)]
    for key in ("notify.points_minutes", "notify.window_minutes"):
        assert key in keys


# ----------------------------------------------------- 列表 ↔ 配置（s）与配置分组


async def test_tab_switches_config_groups(tmp_path: Path):
    from meruki_tracker.core.config_options import GROUP_ORDER

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    await _drive(tui, "s", expect_exit=False)  # 列表按 s 进配置（外层大分页已拆掉）
    assert tui._screen is Screen.SETTINGS
    first = GROUP_ORDER[0]
    assert tui._config_group == first
    await _drive(tui, "\t", expect_exit=False)
    assert tui._config_group == GROUP_ORDER[1]
    await _drive(tui, "\x1b[Z", expect_exit=False)
    assert tui._config_group == first


async def test_arrows_switch_config_groups(tmp_path: Path):
    """配置页上下箭头管光标，左右箭头空着——拿来切分组。"""
    from meruki_tracker.core.config_options import GROUP_ORDER

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    tui._open_settings()
    first = tui._config_group
    assert first == GROUP_ORDER[0]
    await _drive(tui, "\x1b[C", expect_exit=False)
    assert tui._config_group == GROUP_ORDER[1]
    await _drive(tui, "\x1b[D", expect_exit=False)
    assert tui._config_group == first


async def test_tab_still_switches_units_while_editing(tmp_path: Path):
    """编辑态里 Tab 仍是"切单位"。

    分组也绑了 Tab，但 ``_bind_settings_keys`` 最后注册——prompt_toolkit 同屏
    同键取最后注册的那个，所以编辑时不会误切成下一组。
    """
    from meruki_tracker.core.config_options import GROUP_REFRESH

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen = Screen.SETTING_EDIT
    tui._editing_key = "refresh.auto_seconds"
    tui._edit_unit = "分钟"
    tui._config_group = GROUP_REFRESH
    before_group = tui._config_group

    await _drive(tui, "\t", expect_exit=False)

    assert tui._edit_unit == "小时", "编辑态按 Tab 应该是换单位"
    assert tui._config_group == before_group, "编辑态按 Tab 不该换分组"


async def test_config_page_only_shows_the_current_group(tmp_path: Path):
    from meruki_tracker.core.config_options import OPTIONS

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    await _drive(tui, "s", expect_exit=False)
    visible = tui._visible_options()
    assert visible, "分组里总得有点东西"
    assert {o.group for o in visible} == {tui._config_group}
    assert len(visible) < len(OPTIONS), "一页不该是全部配置项"


async def test_mount_entry_lives_inside_the_config_zone(tmp_path: Path):
    """挂载唯一的入口在配置区：没开渠道时把人放在配置页并说明去哪开。"""
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT, GROUP_NOTIFY

    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    _open_group(tui, GROUP_NOTIFY)
    tui._run_action(ACTION_MOUNT_WECHAT)
    assert tui._screen is Screen.SETTINGS
    await tui._shutdown()


async def test_mount_skips_the_qr_when_credentials_already_exist(tmp_path: Path, monkeypatch):
    """手填了 AppID/AppSecret 就不该再逼人扫码。

    这是用户报的原话问题：QQ 的 id/secret 可以一个字一个字填进去，填完点进
    挂载页却又给一张新码——等于否认用户刚做的事。
    """
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT

    class _Qq(_FakeProvider):
        kind = "qq"
        label = "QQ"

        def fetch(self):
            from meruki_tracker.core.app.qrlogin import KIND_QQ, QrChallenge

            self.fetches += 1
            return QrChallenge(kind=KIND_QQ, payload="https://q.qq.com/x", hint="扫码")

    provider = _Qq()
    monkeypatch.setattr(tui_mod, "build_provider", lambda channels, kind="": provider)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui.ctx.settings.set("channels.qq_app_id", "888888888")
    tui.ctx.settings.set("channels.qq_app_secret", "S3CRET")

    tui._run_action(ACTION_MOUNT_WECHAT)

    assert tui._screen is Screen.MOUNT
    assert provider.fetches == 0, "已经有凭据就不该再去取码"
    assert tui._mount_task is None
    assert tui._mount_state == "success"
    assert "888888888" in tui._mount_message


async def test_r_still_fetches_a_new_qr_when_the_user_asks(tmp_path: Path, monkeypatch):
    """已经有凭据时按 r 才是"我要换一个"，这时候必须真取码。"""
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT

    class _Qq(_FakeProvider):
        kind = "qq"
        label = "QQ"

        def fetch(self):
            from meruki_tracker.core.app.qrlogin import KIND_QQ, QrChallenge

            self.fetches += 1
            return QrChallenge(kind=KIND_QQ, payload="https://q.qq.com/x", hint="扫码")

    provider = _Qq()
    monkeypatch.setattr(tui_mod, "build_provider", lambda channels, kind="": provider)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui.ctx.settings.set("channels.qq_app_id", "888888888")
    tui.ctx.settings.set("channels.qq_app_secret", "S3CRET")

    # 先走配置页的动作项进挂载屏（列表已经没有 m 了），再按 r 换码
    tui._open_settings()
    tui._run_action(ACTION_MOUNT_WECHAT)
    await _drive(tui, "r", expect_exit=False)

    assert provider.fetches >= 1, "r 是明确的「换一个账号」，要重新取码"
    assert tui._mount_state in ("pending", "scanned", "success")


def test_mount_action_text_says_already_mounted(tmp_path):
    """「扫描 QQ 机器人二维码」这一行要把已有凭据说出来，别让人以为没配。"""
    from meruki_tracker.core.config_options import ACTION_MOUNT_QQ

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui.ctx.settings.set("channels.qq_app_id", "888888888")
    tui.ctx.settings.set("channels.qq_app_secret", "S3CRET")
    tui.cfg = cfg_with_channels(qq_enable=True)
    assert "已挂载" in tui._action_texts()[ACTION_MOUNT_QQ]
    assert "888888888" in tui._action_texts()[ACTION_MOUNT_QQ]


def test_mount_action_text_asks_for_the_qr_when_nothing_is_mounted(tmp_path):
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui.cfg = cfg_with_channels(wechat_enable=True)
    assert tui._action_texts()[ACTION_MOUNT_WECHAT] == "Enter 打开二维码"


def test_mount_row_sits_right_under_its_own_channel(tmp_path: Path):
    """两行各自贴着自己那一组的凭据：位置本身就是"扫的是哪个渠道"的说明。"""
    from meruki_tracker.core.config_options import (
        ACTION_MOUNT_QQ,
        ACTION_MOUNT_WECHAT,
        GROUP_NOTIFY,
    )

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui.cfg = cfg_with_channels(wechat_enable=True, qq_enable=True)
    tui._open_settings()
    tui._config_group = GROUP_NOTIFY
    keys = [opt.key for opt in tui._visible_options()]

    wechat = keys.index(ACTION_MOUNT_WECHAT)
    assert keys[wechat - 1] == "channels.wechat_token", "微信扫码行紧跟在登录凭据下面"
    assert keys[wechat + 1] == "channels.wechat_base_url"

    qq = keys.index(ACTION_MOUNT_QQ)
    assert keys[qq - 1] == "channels.qq_app_secret", "QQ 扫码行紧跟在 AppSecret 下面"
    assert keys[qq + 1] == "channels.qq_base_url"


async def test_each_mount_row_scans_its_own_channel(tmp_path: Path, monkeypatch):
    """两个渠道都开着时点哪一行就扫哪个码，不会互相顶掉。"""
    from meruki_tracker.core.app.qrlogin import KIND_QQ
    from meruki_tracker.core.config_options import ACTION_MOUNT_QQ

    seen: list[str] = []

    def _builder(channels, kind=""):
        seen.append(kind)
        return _Qq()

    class _Qq(_FakeProvider):
        kind = "qq"
        label = "QQ"

    monkeypatch.setattr(tui_mod, "build_provider", _builder)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui.cfg = cfg_with_channels(wechat_enable=True, qq_enable=True)

    tui._run_action(ACTION_MOUNT_QQ)
    assert seen == [KIND_QQ], "QQ 那行只该取 QQ 的码"
    assert tui._screen is Screen.MOUNT
    await tui._shutdown()


async def test_mount_action_lives_on_the_config_page(tmp_path, monkeypatch):
    """挂载是配置区的动作项：「配置 → 通知」里按 Enter 就是它。"""
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT, GROUP_NOTIFY

    provider = _FakeProvider()
    monkeypatch.setattr(tui_mod, "build_provider", lambda channels, kind="": provider)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui.cfg = cfg_with_channels(wechat_enable=True)  # 渠道开着，扫码行才会出现

    options = _open_group(tui, GROUP_NOTIFY)
    assert any(opt.key == ACTION_MOUNT_WECHAT for opt in options), "通知组里要有扫码挂载这一行"
    tui._settings_cursor = [opt.key for opt in options].index(ACTION_MOUNT_WECHAT)
    tui._begin_edit()

    assert tui._screen is Screen.MOUNT


async def test_leaving_the_mount_screen_always_returns_to_the_config_page(tmp_path, monkeypatch):
    """挂载屏只从配置页进，所以退出来永远是配置页，与进来时在哪一页无关。"""
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT

    monkeypatch.setattr(tui_mod, "build_provider", lambda channels, kind="": _FakeProvider())
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._open_settings()
    tui._run_action(ACTION_MOUNT_WECHAT)
    assert tui._screen is Screen.MOUNT
    tui._leave_subscreen()
    assert tui._screen is Screen.SETTINGS, "挂载屏属于配置区"

    # 就算从列表屏直接调 _open_mount（比如以后又加了别的入口），退出也回配置页：
    # 挂载屏本身只在配置区存在。
    tui2 = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui2._screen = Screen.LIST
    tui2._open_mount()
    assert tui2._screen is Screen.MOUNT
    tui2._leave_subscreen()
    assert tui2._screen is Screen.SETTINGS


def cfg_with_channels(**kwargs):
    from meruki_tracker.core.config import ChannelsConfig

    base = Config()
    return replace(base, channels=replace(ChannelsConfig(), **kwargs))


def _draw_zone(tui, *, cols: int, rows: int) -> list[str]:
    """把 ``_draw`` 画进一个指定尺寸的 Console，按行返回（页脚不在内）。"""
    import io as _io

    from rich.console import Console as _Console

    buf = _io.StringIO()
    console = _Console(file=buf, width=cols, height=rows, force_terminal=False)
    tui._draw(console)
    return [line.rstrip() for line in buf.getvalue().splitlines()]


@pytest.mark.parametrize("cols", [120, 100, 70, 50, 40])
@pytest.mark.parametrize("rows", [8, 12, 24, 30])
def test_draw_fits_the_body_height(tmp_path: Path, cols: int, rows: int):
    """配置分组条也占行，所以主体必须**在扣掉条之后**仍然放得下。

    这是渲染里最容易出错的地方：预算按 ``console.height`` 算、条却画在同一块
    区域上，少减一行页脚就被顶出屏幕。
    """
    many = tuple(
        replace(_auction(f"A{i}", f"拍品{i}"), latest_bid=float(100 + i)) for i in range(30)
    )
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=Snapshot(auctions=many)), Config())
    # 真实运行时 console 高度就是终端高度，每页条数也按它算；测试里两个数
    # 必须一致，否则不是"能不能放下"，而是"测试造了个不存在的终端"。
    tui._screen_size = lambda: (cols, rows)
    tui._rebuild()
    for screen in (Screen.LIST, Screen.SETTINGS):
        tui._screen = screen
        tui._config_group = ""
        lines = _draw_zone(tui, cols=cols, rows=rows)
        assert len(lines) <= rows, f"{screen} 在 {cols}x{rows} 下画了 {len(lines)} 行"


async def test_launch_puts_the_task_in_the_tracked_set(tmp_path: Path):
    """删掉串行队列后，后台动作仍要被 self._tasks 收着，退出才能统一取消。"""
    from meruki_tracker.tui.tui import Tui

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())

    async def work():
        await asyncio.sleep(0)

    tui._launch("probe", work)
    assert len(tui._tasks) == 1
    assert next(iter(tui._tasks)).get_name() == "probe"
    await asyncio.gather(*tuple(tui._tasks))
    assert not tui._tasks, "done_callback 要把跑完的任务从集合里摘掉"
    await tui._shutdown()


def test_second_refresh_is_rejected_while_one_is_running(tmp_path: Path):
    """没有队列兜底了，重复刷新就靠 refresher 的 busy 守卫 + _busy 明确拒绝。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._busy = "刷新中 · 正在准备…"
    before = len(tui._tasks)
    tui._start_refresh()
    assert len(tui._tasks) == before, "已经在刷新时不能再起一个任务"
    assert any("已在刷新中" in n for n in tui._notice)

    # 换一个角度：让 refresher 说自己正在采集，_busy 为空也必须拦住。
    tui2 = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui2.ctx.refresher = type("R", (), {"running": True})()
    tui2._start_refresh()
    assert not tui2._tasks
    assert any("已在刷新中" in n for n in tui2._notice)


def test_footer_still_advertises_s_as_the_only_way_into_settings():
    """外层大分页拆掉后，进配置页只剩 s；页脚任何宽度都得把它说出来。"""
    from meruki_tracker.tui import screens

    for width in (200, 120, 100, 80, 60, 50, 40, 30, 20):
        text = screens.footer_text("list", width=width)
        assert "s 设置" in text, f"{width} 列丢了进配置的唯一入口"
        assert "Tab 换页" not in text, f"{width} 列还在宣传已删掉的外层分页"
        assert "q 退出" in text


def test_settings_has_a_group_bar_but_the_list_does_not(tmp_path: Path):
    """配置页画一根分组条，列表页一根都不画（外层大分页已拆掉）。"""
    from rich.console import Console as _Console

    from meruki_tracker.tui import screens

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())

    def bar_text(screen):
        tui._screen = screen
        console = _Console(file=__import__("io").StringIO(), width=100, height=30)
        before = console.height
        tui._draw_bars(console)
        return before - console.height

    assert bar_text(Screen.SETTINGS) == 1, "配置页的分组条要占一行并扣掉预算"
    assert bar_text(Screen.LIST) == 0, "列表页不该再有外层分页条"
    assert screens.tab_bar(["基本", "通知"], 0, width=100).plain


async def test_tab_no_longer_switches_the_outer_page(tmp_path: Path):
    """Tab 已经不绑外层换页：在列表页按它不该跑去别的屏。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot()), Config())
    tui._rebuild()
    with create_pipe_input() as pipe:
        app = tui._build_app(input=pipe, output=DummyOutput())
        tui._app = app
        task = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("\t")
        await asyncio.sleep(0.1)
        assert tui._screen is Screen.LIST
        app.exit()
        await asyncio.wait_for(task, timeout=5)
        tui._app = None
        await tui._shutdown()


def test_config_footer_no_longer_mentions_the_outer_tab():
    from meruki_tracker.tui import screens

    for screen in ("settings", "mount"):
        text = screens.footer_text(screen, width=200)
        assert "Tab 换页" not in text, f"{screen} 页脚还留着已删掉的外层分页"


# ------------------------------------------------------------------ 配置页里的动作


def _open_group(tui, group: str):
    """进配置页并把光标停在指定分组的某一项上（返回该项）。"""
    tui._open_settings()
    tui._config_group = group
    options = tui._visible_options()
    return options


def test_login_action_can_be_run_from_the_config_page(tmp_path):
    """登录也放进配置页的「登录」分组：按 Enter 就是那条登录路。"""
    from meruki_tracker.core.config_options import ACTION_LOGIN, GROUP_LOGIN

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    options = _open_group(tui, GROUP_LOGIN)
    assert options[0].key == ACTION_LOGIN
    tui._settings_cursor = 0
    tui._begin_edit()
    assert tui._screen is Screen.LOGIN_METHOD


def test_login_action_text_shows_the_current_state(tmp_path):
    from meruki_tracker.core.config_options import ACTION_LOGIN, ACTION_TEST_MESSAGE

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    texts = tui._action_texts()
    assert "已登录" in texts[ACTION_LOGIN]
    assert texts[ACTION_TEST_MESSAGE] == "Enter 发送"

    tui = Tui(_Ctx(tmp_path), Config())
    assert "未登录" in tui._action_texts()[ACTION_LOGIN]


async def test_test_message_action_sends_through_every_channel(tmp_path, monkeypatch):
    """配置页上按 Enter「发送测试消息」→ 真去调 ``send_test`` 并按结果提示。"""
    from meruki_tracker.core.config_options import ACTION_TEST_MESSAGE, GROUP_NOTIFY

    calls: list = []

    async def fake_send(channels, *, wechat=None, qq=None):
        calls.append((channels, wechat))
        return [("Webhook", True, "已发送"), ("邮件", False, "还差 收件人")]

    monkeypatch.setattr("meruki_tracker.core.app.notifications.send_test", fake_send)

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    options = _open_group(tui, GROUP_NOTIFY)
    index = [o.key for o in options].index(ACTION_TEST_MESSAGE)
    tui._settings_cursor = index
    tui._begin_edit()  # action → 起后台任务
    await asyncio.sleep(0.1)

    assert calls
    joined = "\n".join(tui._notice)
    assert "1/2 个渠道成功" in joined
    assert "✗ 邮件：还差 收件人" in joined


def test_test_message_action_says_so_when_nothing_is_enabled(tmp_path, monkeypatch):
    async def fake_send(channels, *, wechat=None, qq=None):
        return []

    monkeypatch.setattr("meruki_tracker.core.app.notifications.send_test", fake_send)
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())

    async def go() -> None:
        await tui._do_test_message()

    asyncio.run(go())
    assert "没有可用的通知渠道" in tui._notice[0]


def test_config_page_draws_action_rows_without_reading_a_value(tmp_path):
    """动作项没有值：渲染不能去读 Config（会 KeyError），也不能显示成空。"""
    from meruki_tracker.core.config_options import GROUP_NOTIFY

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    _open_group(tui, GROUP_NOTIFY)
    body = _draw_body(tui, cols=100, rows=30)
    assert "发送测试消息" in body
    assert "Enter 发送" in body


async def test_qq_gateway_loop_starts_at_boot(tmp_path: Path):
    """有通道就必须起长连：不连 WebSocket 永远拿不到 openid，也就永远发不出去。"""
    ctx = _Ctx(tmp_path, cookie="", snap=_mixed_snapshot())
    started: list[int] = []

    class _Bot:
        async def run(self) -> None:
            started.append(1)
            await asyncio.sleep(10)

    ctx.qq = _Bot()
    tui = Tui(ctx, Config())
    tui._prepare_start()
    try:
        await asyncio.sleep(0.05)
        assert started == [1]
    finally:
        await tui._shutdown()


async def test_test_message_action_passes_the_qq_channel(tmp_path: Path, monkeypatch):
    """发测试消息时必须把活的 QQ 通道传下去，否则永远报"连接还没起来"。"""
    import meruki_tracker.core.app.notifications as mod

    ctx = _Ctx(tmp_path, cookie="sid=x", snap=_mixed_snapshot())
    marker = object()
    ctx.qq = marker
    tui = Tui(ctx, Config())
    seen: dict[str, object] = {}

    async def _fake_send_test(channels, *, wechat=None, qq=None):
        seen["qq"] = qq
        return [("QQ", True, "已发送")]

    monkeypatch.setattr(mod, "send_test", _fake_send_test)
    await tui._do_test_message()
    assert seen["qq"] is marker


def _open_recheck(tui) -> None:
    """把光标挪到「结束前复查点」并进逐条编辑屏。

    详情那一组同形的配置已随"每轮全拉详情"删掉（2026-11-15），现在这组数只剩
    提醒点 ``notify.points_minutes``。
    """
    tui._screen = Screen.SETTINGS
    tui._config_group = GROUP_NOTIFY
    tui._settings_cursor = [o.key for o in OPTIONS if o.group == GROUP_NOTIFY].index(
        "notify.points_minutes"
    )
    tui._begin_edit()


async def test_recheck_points_open_the_per_point_editor(tmp_path: Path):
    """结束前复查点：按 Enter 进的是逐条增删屏，不是"填一行"的输入屏。

    用户 m22010 原话：「这种列表能不能改成一个一个加一个一个删啊？你现在本质上
    还是让我写一行然后去解析。」所以这里先钉死"进哪一屏"。
    """
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    _open_recheck(tui)  # 以前这里 ``display_unit`` 收到元组会抛 TypeError

    assert tui._screen is Screen.POINT_EDIT
    assert tui._editing_key == "notify.points_minutes"
    assert tui._edit_unit == "分钟"
    assert tui._point_cursor == 0
    assert not tui._point_input_on


async def test_a_point_can_be_added_one_at_a_time(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    _open_recheck(tui)

    tui._start_point_input()
    tui._point_input = "2 小时"
    tui._commit_point()

    assert tui.cfg.notify.points_minutes == (360.0, 120.0, 60.0, 15.0)
    assert tui.ctx.settings.get("notify.points_minutes") == (
        360.0,
        120.0,
        60.0,
        15.0,
    )
    # 加完停在原屏，还能接着加——不该每加一个点就被踢回列表
    assert tui._screen is Screen.POINT_EDIT


async def test_a_point_can_be_dropped_one_at_a_time(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    _open_recheck(tui)

    tui._point_cursor = 1  # 屏上按远近排是 6 小时 / 1 小时 / 15 分钟，选中间那个
    assert tuple(sorted(tui.cfg.notify.points_minutes, reverse=True)) == (360.0, 60.0, 15.0)
    tui._drop_current_point()

    assert tui.cfg.notify.points_minutes == (360.0, 15.0)
    assert tui.ctx.settings.get("notify.points_minutes") == (360.0, 15.0)
    assert tui._screen is Screen.POINT_EDIT


async def test_a_point_can_be_replaced_without_losing_the_others(tmp_path: Path):
    """改一个写错的数：结果里点的个数不变。"""
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    _open_recheck(tui)

    tui._start_point_input(replace=1)  # 60 分钟那个
    assert tui._point_input == "1 小时"  # 回填成原样的显示文案
    tui._point_input = "90 分钟"
    tui._commit_point()

    assert tui.cfg.notify.points_minutes == (360.0, 90.0, 15.0)


async def test_the_last_point_cannot_be_dropped(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    _open_recheck(tui)

    for _ in range(2):
        tui._point_cursor = len(tui.cfg.notify.points_minutes) - 1
        tui._drop_current_point()
    assert tui.cfg.notify.points_minutes == (360.0,)

    tui._point_cursor = 0
    tui._drop_current_point()
    # 一个都不剩就报错并保持原样
    assert tui.cfg.notify.points_minutes == (360.0,)
    assert "至少要留一个点" in "".join(tui._notice)


async def test_adding_a_bad_point_says_what_is_wrong(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    _open_recheck(tui)

    tui._start_point_input()
    tui._point_input = "abc"
    tui._commit_point()
    assert "不是数字" in "".join(tui._notice)
    assert tuple(sorted(tui.cfg.notify.points_minutes, reverse=True)) == (360.0, 60.0, 15.0)
    # 输入态保留：报错不该把用户敲的东西清掉
    assert tui._point_input_on
    assert tui._point_input == "abc"


async def test_escape_leaves_the_point_editor_in_two_steps(tmp_path: Path):
    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    _open_recheck(tui)

    tui._start_point_input()
    tui._point_input = "2 小时"
    tui._cancel_point_input()  # 第一步：只退出输入态
    assert tui._screen is Screen.POINT_EDIT
    assert tui._point_input == ""
    assert not tui._point_input_on

    tui._cancel_point_input()  # 第二步：回设置屏
    assert tui._screen is Screen.SETTINGS
    assert tui._editing_key == ""


async def test_point_editor_footer_lists_the_keys(tmp_path: Path):
    from meruki_tracker.tui import screens

    text = screens.footer_text("point-edit", width=200)
    assert "a 加一个" in text and "x 删一个" in text
    assert "Esc 返回" in text
    # 没有"保存"：每步都立刻落库，再摆一个保存键会让人以为不按就不算
    assert "保存" not in text
    # 窄屏下也必须保住唯一的返回路径
    assert "Esc" in screens.footer_text("point-edit", width=20)


# ------------------------------------------------- 侧栏「已发生」段（m24274）


def _recent_notice(kind: str, *, asin: str = "R1", body: str = "刚发生的事"):
    from datetime import UTC, datetime

    from meruki_tracker.core.app.notices import Notice

    return Notice(kind=kind, asin=asin, title="标题", body=body, due_at=datetime.now(UTC))


def test_noticebar_shows_recent_events_below_pending():
    """将要发生的在上、刚发生的在下——上面还来得及改行为，下面只是告知。"""
    from datetime import UTC, datetime, timedelta
    from io import StringIO

    from rich.console import Console

    from meruki_tracker.core.app.notices import KIND_ENDED, KIND_ENDING, Notice
    from meruki_tracker.tui import screens

    now = datetime.now(UTC)
    pending = (
        Notice(
            kind=KIND_ENDING,
            asin="A1",
            title="15 分钟后结束",
            body="拍品一",
            due_at=now + timedelta(minutes=15),
            minutes_before_end=15.0,
        ),
    )
    recent = (_recent_notice(KIND_ENDED, body="拍品二（成交价 ¥1,500）"),)

    buf = StringIO()
    screens.render_noticebar(
        Console(file=buf, width=18, height=24, force_terminal=False),
        pending,
        now=now,
        tz=TZ,
        recent=recent,
    )
    out = buf.getvalue()

    assert "已发生" in out
    assert "拍卖结束" in out
    assert out.index("结束提醒") < out.index("已发生"), out


def test_noticebar_drops_recent_before_pending():
    """行数不够时先牺牲「已发生」：将要发生的那段不能被挤掉。"""
    from datetime import UTC, datetime, timedelta
    from io import StringIO

    from rich.console import Console

    from meruki_tracker.core.app.notices import KIND_ENDED, KIND_ENDING, Notice
    from meruki_tracker.tui import screens

    now = datetime.now(UTC)
    pending = tuple(
        Notice(
            kind=KIND_ENDING,
            asin=f"A{i}",
            title="15 分钟后结束",
            body=f"拍品{i}",
            due_at=now + timedelta(minutes=15 + i),
            minutes_before_end=15.0,
        )
        for i in range(2)
    )
    recent = tuple(_recent_notice(KIND_ENDED, asin=f"R{i}") for i in range(3))

    buf = StringIO()
    screens.render_noticebar(
        Console(file=buf, width=18, height=6, force_terminal=False),
        pending,
        now=now,
        tz=TZ,
        recent=recent,
    )
    out = buf.getvalue()

    assert "结束提醒" in out
    assert "已发生" not in out, out


def test_noticebar_recent_never_exceeds_the_height():
    """加了「已发生」段之后，任何高度都不能溢出。"""
    from datetime import UTC, datetime, timedelta
    from io import StringIO

    from rich.console import Console

    from meruki_tracker.core.app.notices import KIND_ENDED, KIND_ENDING, Notice
    from meruki_tracker.tui import screens

    now = datetime.now(UTC)
    pending = tuple(
        Notice(
            kind=KIND_ENDING,
            asin=f"A{i}",
            title="15 分钟后结束",
            body=f"拍品{i}",
            due_at=now + timedelta(minutes=15),
            minutes_before_end=15.0,
        )
        for i in range(4)
    )
    recent = tuple(_recent_notice(KIND_ENDED, asin=f"R{i}") for i in range(6))

    for h in (1, 2, 3, 4, 5, 6, 8, 12, 20, 40):
        buf = StringIO()
        screens.render_noticebar(
            Console(file=buf, width=18, height=h, force_terminal=False),
            pending,
            now=now,
            tz=TZ,
            recent=recent,
        )
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) <= h, (h, lines)


def test_noticebar_labels_every_event_kind():
    """三种事件各有自己的图标和标签，不能都显示成 kind 原文。"""
    from datetime import UTC, datetime
    from io import StringIO

    from rich.console import Console

    from meruki_tracker.core.app.notices import KIND_ENDED, KIND_EXTENDED, KIND_SHORTENED
    from meruki_tracker.tui import screens

    buf = StringIO()
    screens.render_noticebar(
        Console(file=buf, width=18, height=20, force_terminal=False),
        (),
        now=datetime.now(UTC),
        tz=TZ,
        recent=(
            _recent_notice(KIND_SHORTENED),
            _recent_notice(KIND_EXTENDED),
            _recent_notice(KIND_ENDED),
        ),
    )
    out = buf.getvalue()

    assert "拍卖结束" in out
    assert "时间延后" in out
    assert "时间提前" in out
    for raw in ("ended", "extended", "shortened"):
        assert raw not in out, out


async def test_render_noticebar_reads_recent_from_the_live_board(tmp_path: Path):
    """侧栏的「已发生」段读的是真通知板，不是渲染层自己编的。"""
    from datetime import UTC, datetime

    from meruki_tracker.core.app.notices import KIND_ENDED, NoticeBoard

    tui = Tui(_Ctx(tmp_path, cookie="sid=x"), Config())
    tui._screen_size = lambda: (100, 30)
    board = tui.ctx.notices
    assert isinstance(board, NoticeBoard)
    board.add_event(
        key=(KIND_ENDED, "A7", ""),
        kind=KIND_ENDED,
        asin="A7",
        title="拍卖结束",
        body="甲（成交价 ¥1,500）",
        now=datetime.now(UTC),
    )

    out = _ANSI_RE.sub("", str(tui._render_noticebar()))

    assert "已发生" in out, out
    assert "拍卖结束" in out, out
    assert "甲" in out, out


async def test_sms_code_does_not_double_submit(tmp_path: Path):
    """连按两次 Enter 只发一次登录请求（发码同理）。

    2026-11-18 审计：短信三步（发码 / 登录 / 重发）都没有在飞守卫，连按两下
    Enter 就是两条计费短信、两次 ``login_by_sms``。列表 ``r`` 一直有 ``_busy``
    守卫，这几个入口漏了。
    """
    api = _SmsApi()
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_CODE
    tui._sms_mobile = "13800138000"
    tui._input_text = "123456"

    tui._submit_sms_code()
    tui._submit_sms_code()
    await asyncio.sleep(0.05)

    assert api.logins == [("13800138000", "123456")]
    await tui._shutdown()


async def test_late_sms_failure_does_not_pull_the_user_back(tmp_path: Path):
    """用户按 Esc 离开验证码屏后，晚到的失败结果不许把他拽回去。

    2026-11-18 审计：``_do_sms_login`` 每个分支都直接写 ``_screen``，没有校验
    用户还在不在这一屏——`Esc` 回手机号屏、几秒后请求失败回来，界面自己跳回
    验证码屏，看着像 Esc 没生效。
    """
    import asyncio as _asyncio

    class _SlowApi(_SmsApi):
        async def login_by_sms(self, mobile: str, code: str) -> str:
            await _asyncio.sleep(0.05)
            raise RuntimeError("慢请求才回来")

    api = _SlowApi()
    tui = Tui(_sms_ctx(tmp_path, api), Config())
    tui._screen = Screen.LOGIN_SMS_CODE
    tui._sms_mobile = "13800138000"
    tui._input_text = "123456"

    tui._submit_sms_code()
    tui._screen = Screen.LOGIN_SMS_MOBILE  # 用户按了 Esc
    await asyncio.sleep(0.15)

    assert tui._screen is Screen.LOGIN_SMS_MOBILE
    await tui._shutdown()


async def test_late_detail_result_for_another_item_does_not_overwrite_this_screen(
    tmp_path: Path,
):
    """A1 的详情结果晚到，不能把描述/徽标盖到用户正在看的 A2 上。

    2026-11-18 审计：``_do_fetch_this_detail`` 的 else 分支只对 ``_detail`` 做了
    asin 比对，紧接着的 ``_notice`` / ``_load_detail_raw`` / 两个规则标志位都是
    无条件写的。用户在 A1 触发自动补详情 → Esc → 点进 A2，等 A1 的慢请求回来，
    A2 的详情屏就会显示 A1 的描述（标题和价格仍是 A2 的）。
    """
    from meruki_tracker.core.adapters.store.mirror import StoredDetail

    def _stored(asin: str, *, auto: bool) -> StoredDetail:
        return StoredDetail(
            asin=asin,
            name=asin,
            end_at=None,
            ended=False,
            bid_price=100.0,
            buy_now_price=None,
            auto_extend=auto,
            early_termination=False,
            observed_at="2026-11-18T12:00:00+00:00",
        )

    raw_a1 = {"other": {"descText": "一的描述"}}
    raw_a2 = {"other": {"descText": "二的描述"}}
    ctx = _Ctx(
        tmp_path,
        cookie="sid=x",
        snap=Snapshot(auctions=(_auction("A1", "拍品一"), _auction("A2", "拍品二"))),
    )
    ctx.mirror = _Mirror(
        {"A1": raw_a1, "A2": raw_a2},
        {"A1": _stored("A1", auto=False), "A2": _stored("A2", auto=True)},
    )
    tui = Tui(ctx, Config())
    tui._rebuild()
    by_asin = {r.asin: r for r in tui._rows}
    tui._detail = by_asin["A1"]  # 用户在 A1
    tui._load_detail_raw("A1")

    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowRefresher:
        async def refresh_detail_of(self, st):
            started.set()
            await release.wait()
            return by_asin["A1"]

    ctx.refresher = _SlowRefresher()  # type: ignore[assignment]
    task = asyncio.create_task(tui._do_fetch_this_detail(by_asin["A1"]))
    await started.wait()

    # 用户切到 A2
    tui._detail = by_asin["A2"]
    tui._load_detail_raw("A2")
    assert tui._detail_auto_extend is True

    release.set()
    await task

    assert tui._detail is not None and tui._detail.asin == "A2"
    assert tui._detail_auto_extend is True, "A2 的自动延长被 A1 的结果盖掉了"
    assert "二的描述" in tui._desc_text()
    await tui._shutdown()


async def test_background_refresh_updates_the_open_detail_description(tmp_path: Path):
    """后台刷新后，详情屏的描述与规则标志也要跟着更新。

    2026-11-18 审计：``_refresh_detail_view`` 只换了 ``AuctionState``，没重读
    镜像里的描述与两个标志位——用户会看到"价格更新了、卖家新写的描述还是旧的"，
    得 Esc 出去再进来才对。
    """
    from meruki_tracker.core.adapters.store.mirror import StoredDetail

    ctx = _detail_ctx(tmp_path)
    tui = Tui(ctx, Config())
    tui._rebuild()
    await _drive(tui, "\r", expect_exit=False)
    assert "一行目" in tui._desc_text()

    # 镜像被后台刷新换了新描述 + 自动延长打开
    ctx.mirror = _Mirror(
        {"A1": {"other": {"descText": "新写的描述"}}},
        {
            "A1": StoredDetail(
                asin="A1",
                name="尼康 F3",
                end_at=None,
                ended=False,
                bid_price=999.0,
                buy_now_price=None,
                auto_extend=True,
                early_termination=False,
                observed_at="2026-11-18T12:00:00+00:00",
            )
        },
    )
    tui._refresh_detail_view()

    assert "新写的描述" in tui._desc_text()
    assert tui._detail_auto_extend is True
    await tui._shutdown()


# ------------------------------------------- 改配置要真的作用到正在跑的组件上


def test_editing_jitter_ratio_applies_to_the_running_api(tmp_path):
    """抖动比例改完要立刻作用到 ``MerukiApi``，否则「立即生效」就是假的。

    真踩过：``_apply_config`` 写了 ``timeout`` / ``_min_interval`` / ``_retry``，
    唯独漏了 ``_jitter``——它只在 ``build_context`` 构造时传过一次。用户在设置
    里把抖动从 20% 调到 50%（防风控最常改的一项），请求间隔照旧不变，直到重启。
    """
    from meruki_tracker.core.adapters.meruki.api import MerukiApi

    ctx = _Ctx(tmp_path)
    ctx.api = MerukiApi(object(), cookie="sid=x", jitter=0.2)
    tui = Tui(ctx, Config())

    tui._apply_config(apply_edit(tui.cfg, "fetch.jitter_ratio", "50", unit="%"))

    assert tui.cfg.fetch.jitter_ratio == 0.5
    assert ctx.api._jitter == 0.5


def test_enabling_a_channel_rebuilds_the_running_notifier(tmp_path):
    """在设置里打开 webhook 并填地址，投递器要立刻换掉（通知板和刷新器都换）。

    真踩过：``build_notifier`` 只在「扫码挂载成功」之后重建。用户手填 webhook
    地址、打开开关，界面回「已保存」，可到点的提醒仍走 ``NullNotifier``——
    一个字都发不出去，看起来像服务端没收到。
    """
    from meruki_tracker.core.app.notifications import NullNotifier, WebhookNotifier

    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())
    assert isinstance(ctx.notices._notifier, NullNotifier)

    cfg = apply_edit(tui.cfg, "channels.webhook_url", "http://127.0.0.1:9/hook")
    cfg = apply_edit(cfg, "channels.webhook_enable", "开")
    tui._apply_config(cfg)

    assert isinstance(ctx.notices._notifier, WebhookNotifier)
    # 刷新器也持有同一个投递器（站内消息那条路径直接用它发）
    assert ctx.refresher._notifier is ctx.notices._notifier


def test_channels_untouched_keeps_the_same_notifier(tmp_path):
    """渠道没改就不该重建：每次保存设置都换对象会丢掉正在重试的投递。"""
    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())
    before = ctx.notices._notifier

    tui._apply_config(replace(tui.cfg, fetch=replace(tui.cfg.fetch, retry=3)))

    assert ctx.notices._notifier is before


def test_turning_off_outbid_by_price_stops_the_price_check(tmp_path):
    """「用价格判断出价被超过」关掉要立刻停：它同样只在构造时传过一次。

    否则关掉只影响下次启动，而这一项存在的意义之一就是省掉每轮那次比对。
    """
    ctx = _Ctx(tmp_path)
    ctx.refresher._outbid_by_price = True
    tui = Tui(ctx, Config())

    tui._apply_config(apply_edit(tui.cfg, "notify.outbid_by_price", "关"))

    assert tui.cfg.notify.outbid_by_price is False
    assert ctx.refresher._outbid_by_price is False


def test_turning_off_messages_stops_polling_the_message_endpoint(tmp_path):
    """「出价被超过提醒」关掉要立刻停拉站内消息（每轮省一次请求）。"""
    ctx = _Ctx(tmp_path)
    ctx.refresher._messages_enabled = True
    tui = Tui(ctx, Config())

    tui._apply_config(apply_edit(tui.cfg, "messages.enable", "关"))

    assert tui.cfg.messages.enable is False
    assert ctx.refresher._messages_enabled is False


# ------------------------------------------------- 心跳类循环不能一拍出错就永久停


def test_auto_loop_survives_a_credential_read_error(tmp_path, monkeypatch):
    """自动刷新循环里读凭证失败不能把循环打死。

    ``CredentialStore.load()`` 第一轮审计后改成"读失败抛 ``OSError``"（以前静默
    返回空串）。数据库被锁/损坏时，自动刷新循环第一轮就异常退出，之后再也不会
    自动采集，界面上没有任何提示——这是修好一个静默 bug 之后新暴露的死法。
    """
    monkeypatch.setattr(tui_mod, "jittered_seconds", lambda seconds, ratio: 0.02)
    ctx = _Ctx(tmp_path)
    calls = {"n": 0}

    def boom() -> str:
        calls["n"] += 1
        raise OSError("database is locked")

    ctx.credentials.load = boom  # type: ignore[method-assign]
    tui = Tui(ctx, Config())

    async def run() -> None:
        task = asyncio.create_task(tui._auto_loop(1))
        await asyncio.sleep(0.3)
        # 断言必须在事件循环里做：``asyncio.run`` 返回时会把还没结束的任务
        # 统一取消，拿出去再看永远是 done。
        assert not task.done(), "一次读凭证失败就把自动刷新循环终止了"
        assert calls["n"] >= 3, f"循环只跑了 {calls['n']} 轮，说明它退出过"
        task.cancel()

    asyncio.run(run())


def test_tick_loop_survives_one_bad_beat(tmp_path):
    """心跳某一拍抛异常不能让倒计时/提醒永久停摆。"""
    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())
    beats = {"n": 0}

    def bad_fire() -> bool:
        beats["n"] += 1
        if beats["n"] == 2:
            raise RuntimeError("这一拍出了问题")
        return False

    tui._fire_notices = bad_fire  # type: ignore[method-assign]
    tui._has_countdown = lambda: False  # type: ignore[method-assign]

    async def run() -> None:
        task = asyncio.create_task(tui._tick_loop())
        await asyncio.sleep(3.4)
        assert not task.done(), "一拍异常就把心跳打死了"
        assert beats["n"] >= 3, f"心跳只跳了 {beats['n']} 拍"
        task.cancel()

    asyncio.run(run())


def test_ensure_ticker_restarts_a_dead_ticker(tmp_path):
    """已经结束的 ticker 要当成"没有"，否则死了就再也不会被重建。"""
    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())
    tui._app = types.SimpleNamespace(invalidate=lambda: None)
    tui._needs_tick = lambda: True  # type: ignore[method-assign]

    async def dead() -> None:
        raise RuntimeError("挂了")

    async def run() -> None:
        dead_task = asyncio.create_task(dead())
        with contextlib.suppress(RuntimeError):
            await dead_task
        tui._tick_task = dead_task
        tui._ensure_ticker()
        assert tui._tick_task is not dead_task, "死掉的 ticker 没有被换掉"
        assert not tui._tick_task.done()
        tui._tick_task.cancel()

    asyncio.run(run())


def _finished_task(result: object = None, *, boom: bool = False) -> asyncio.Task:
    """造一个"已经结束"的任务，用来模拟通道长连退出。"""

    async def run() -> object:
        if boom:
            raise RuntimeError("通道挂了")
        return result

    async def make() -> asyncio.Task:
        task = asyncio.create_task(run())
        with contextlib.suppress(RuntimeError):
            await task
        return task

    return asyncio.run(make())


def test_mount_row_says_the_channel_has_dropped(tmp_path, monkeypatch):
    """微信/QQ 的长连断了以后，配置页不能还写着「已挂载」。

    真实场景：微信会话过期（``errcode -14``）时 ``WeChatBot.run`` 直接 ``return``，
    通道就此停摆；而配置页那行一直是「已挂载 · Enter 查看」。用户以为提醒还在送，
    实际上一个字都送不出去——只有日志里那一行 warning。
    """
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT

    ctx = _Ctx(tmp_path)
    ctx.settings.set("channels.wechat_token", "tok")
    tui = Tui(ctx, Config())
    tui.cfg = cfg_with_channels(wechat_enable=True, wechat_token="tok")
    tui._wechat_task = _finished_task()

    text = tui._action_texts()[ACTION_MOUNT_WECHAT]
    assert "已挂载" in text
    assert "断开" in text or "重开" in text, f"通道都断了，这一行还是只写「{text}」"


def test_mount_row_stays_quiet_while_the_channel_is_alive(tmp_path):
    from meruki_tracker.core.config_options import ACTION_MOUNT_WECHAT

    ctx = _Ctx(tmp_path)
    ctx.settings.set("channels.wechat_token", "tok")
    tui = Tui(ctx, Config())
    tui.cfg = cfg_with_channels(wechat_enable=True, wechat_token="tok")

    async def build() -> asyncio.Task:
        return asyncio.create_task(asyncio.sleep(60))

    async def run() -> None:
        tui._wechat_task = await build()
        text = tui._action_texts()[ACTION_MOUNT_WECHAT]
        assert "已挂载" in text
        assert "断开" not in text
        tui._wechat_task.cancel()

    asyncio.run(run())


def test_channel_stopping_flashes_a_notice_on_the_list(tmp_path):
    """通道长连退出时要在列表上闪一句，别只写进日志。"""
    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())

    tui._channel_stopped("微信提醒通道", _finished_task())

    assert tui._notice, "通道断了却什么都没提示"
    assert "断开" in tui._notice[0]
    assert "微信" in tui._notice[0]


def test_channel_stopping_quietly_ignores_a_cancelled_task(tmp_path):
    """退出程序时任务是被 cancel 掉的，不要因此闪一条"通道断开"。"""
    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())

    async def run() -> None:
        task = asyncio.create_task(asyncio.sleep(60))
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        tui._channel_stopped("微信提醒通道", task)
        assert not tui._notice

    asyncio.run(run())


async def test_clean_refuses_while_a_refresh_is_running(tmp_path: Path):
    """刷新在跑时清理不算数——那一轮会用开工前的快照把记录写回来。

    实测过：清掉两条，刷新一结束它们就复活，界面却已经提示"✓ 已清理"。
    """
    from meruki_tracker.tui.views import ListView

    tui = _cleanable(tmp_path)
    tui._view = ListView.ENDED
    tui._rebuild()
    tui._tasks.add(_named_task("refresh"))
    tui._open_clean_confirm()
    assert tui._screen is Screen.LIST, "刷新没结束就不该进确认屏"
    assert any("刷新" in n for n in tui._notice)


async def test_clean_refuses_when_confirm_is_already_open_and_refresh_starts(tmp_path: Path):
    """确认屏开着的时候定时刷新起来了，按 Enter 也不能写。"""
    from meruki_tracker.tui.views import ListView

    tui = _cleanable(tmp_path)
    tui._view = ListView.ENDED
    tui._rebuild()
    tui._open_clean_confirm()
    assert tui._screen is Screen.CLEAN_CONFIRM
    tui._tasks.add(_named_task("refresh"))
    tui._do_clean()
    assert tui._screen is Screen.LIST
    assert tui.ctx.snapshots.written is None, "刷新在跑就不该写快照"
    assert any("刷新" in n for n in tui._notice)


async def test_clean_works_again_once_the_refresh_is_done(tmp_path: Path):
    from meruki_tracker.tui.views import ListView

    tui = _cleanable(tmp_path)
    tui._view = ListView.ENDED
    tui._rebuild()
    tui._open_clean_confirm()
    assert tui._screen is Screen.CLEAN_CONFIRM
    tui._do_clean()
    assert tui.ctx.snapshots.written is not None
    assert any("已清理" in n for n in tui._notice)


# ------------------------------------------------------------------ 详情写回与刷新竞态（审计遗留 1）


async def test_persist_detail_change_refuses_while_a_refresh_is_running(tmp_path: Path):
    """刷新进行中手工改的详情不能写回旧快照——会被刷新的整表覆盖盖掉。

    2026-11-18 审计遗留：``_persist_detail_change`` 用 ``self.snap``（旧快照）整表
    覆盖写回，而后台刷新也基于它开工时的旧快照写回。刷新中途手工写回的价格，
    会被刷新的结果盖掉，界面内存却停在手工值——库和界面短暂不一致。

    和清理一样：刷新只要几秒，宁可拒绝这次写回（下一轮会自动重拉详情），
    也不能让旧快照盖掉刚改的值。
    """

    old = _auction("A1")
    snap = Snapshot(auctions=(old,))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    new_state = replace(old, latest_bid=9999.0, name="手工改过的名字")

    tui._tasks.add(_named_task("refresh"))
    tui.ctx.snapshots.written = None
    tui._persist_detail_change(new_state)

    assert tui.ctx.snapshots.written is None, "刷新在跑就不该写快照"
    assert tui.snap.auctions == (old,), "内存快照也不该被改"
    assert any("刷新" in n for n in tui._notice)


async def test_persist_detail_change_writes_when_no_refresh_is_running(tmp_path: Path):
    """没有刷新在跑时，手工详情写回照常落库（别把正常路径也挡了）。"""

    old = _auction("A1")
    snap = Snapshot(auctions=(old,))
    tui = Tui(_Ctx(tmp_path, cookie="sid=x", snap=snap), Config())
    tui._rebuild()
    new_state = replace(old, latest_bid=9999.0, name="手工改过的名字")

    tui._persist_detail_change(new_state)

    assert tui.ctx.snapshots.written is not None
    assert tui.ctx.snapshots.written.auctions[0].latest_bid == 9999.0
    assert tui.snap.auctions[0].latest_bid == 9999.0
    assert tui.ctx.snapshots.written.auctions[0].name == "手工改过的名字"


def test_channel_stopped_auto_restarts_when_the_bot_says_it_can_retry(tmp_path):
    """通道说能重试就自动重连，别闪"断开"提示。"""
    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())

    class _Bot:
        def can_retry(self) -> bool:
            return True

    restarts: list[object] = []

    def _restart(bot: object) -> None:
        restarts.append(bot)

    tui._channel_stopped("微信提醒通道", _finished_task(), _Bot(), _restart)

    assert restarts == [_Bot] or len(restarts) == 1, "通道说能重试，却没有自动重连"
    assert not tui._notice, '都自动重连了，不该再闪"断开"'


def test_channel_stopped_shows_notice_when_the_bot_cannot_retry(tmp_path):
    """会话过期这类终态不能重连，要提示用户重新扫码。"""
    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())

    class _Bot:
        def can_retry(self) -> bool:
            return False

    restarts: list[object] = []

    def _restart(bot: object) -> None:
        restarts.append(bot)

    tui._channel_stopped("微信提醒通道", _finished_task(boom=True), _Bot(), _restart)

    assert not restarts, "通道说不能重试，却被重连了"
    assert tui._notice and "重新扫码" in tui._notice[0]


def test_channel_stopped_tolerates_a_bot_whose_can_retry_raises(tmp_path):
    """通道自己的 can_retry() 抛错不能拖垮界面，按不可重试处理。"""
    ctx = _Ctx(tmp_path)
    tui = Tui(ctx, Config())

    class _Bot:
        def can_retry(self) -> bool:
            raise RuntimeError("通道实现有 bug")

    restarts: list[object] = []

    def _restart(bot: object) -> None:
        restarts.append(bot)

    tui._channel_stopped("微信提醒通道", _finished_task(), _Bot(), _restart)

    assert not restarts
    assert tui._notice and "断开" in tui._notice[0]
