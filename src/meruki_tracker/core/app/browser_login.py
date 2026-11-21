"""浏览器登录编排：启动可调试的浏览器 → 等待登录 → 捕获并验证 Cookie。

分三层，失败语义各自独立：

1. :mod:`meruki_tracker.core.adapters.browser` —— 负责"把浏览器启动起来"（进程层）。
2. :mod:`meruki_tracker.core.adapters.cookie_capture` —— 负责"尽力拿到 Cookie"（CDP 层）。
3. 本模块 —— 把两步按用户能理解的节奏串起来，并在失败时**明确回退**。

关键设计：

- **先探测再用**：如果浏览器已经开着调试端口且已登录（例如上一次运行留下的
  会话），直接取 Cookie，不用让用户再等一轮。
- **必须由我们启动带调试端口的浏览器**：普通方式打开的 Chrome 不开放 CDP，
  程序永远抓不到 Cookie，只会白等到超时。
- **拿到 Cookie 还要验证**：浏览器里可能有匿名 Cookie，未登录时也能读到。
  只有 ``verify`` 通过才算登录成功，否则继续等（用户可能正在输入密码）。
- 浏览器拉不起来不是致命错误：服务器/无图形环境下仍可手动粘贴，所以一律
  返回结构化结果交给 UI 决定，而不是抛异常中断。

本函数跑在 TUI 主循环里，任何冒泡的异常都会让界面崩掉。因此除
``CancelledError``（必须让 Ctrl+C 生效）外，所有异常都折成结果对象。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..adapters.browser import BrowserLaunch, launch_debug_browser
from ..adapters.cookie_capture import CaptureFailure, CaptureResult, capture_cookie

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    #: 验证回调：拿到 Cookie 后确认它是否真的处于登录态，返回 (是否有效, 昵称/原因)
    VerifyFn = Callable[[str], Awaitable[tuple[bool, str]]]

_LOG = logging.getLogger(__name__)

#: 轮询节奏（秒）。用户在浏览器里登录需要时间，所以要轮询；
#: 前密后疏：登录完成后能很快被发现，又不至于空转到结束。
_POLL_DELAYS = (1.5, 2.0, 3.0, 3.0, 5.0, 5.0, 8.0, 8.0, 10.0)

#: 等待调试端口就绪的上限（秒）。Chrome 通常 1 秒内就绪，给足余量。
_PORT_WAIT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class BrowserLoginResult:
    """浏览器登录的最终结果。

    Attributes:
        ok: 是否拿到**并通过验证**的 Cookie。
        cookie: 成功时的完整 Cookie 头。
        nickname: 验证通过时返回的昵称（可能为空串）。
        mode: ``debug``（新启动调试浏览器）/ ``plain``（退化成普通浏览器，无法自动抓取）。
        capture: 捕获详情，便于诊断。
        hints: 失败时的可操作建议。
    """

    ok: bool
    cookie: str = ""
    nickname: str = ""
    mode: str = ""
    capture: CaptureResult | CaptureFailure | None = None
    hints: tuple[str, ...] = field(default_factory=tuple)

    @property
    def source(self) -> str:
        return self.capture.source if isinstance(self.capture, CaptureResult) else ""


async def browser_login(
    url: str,
    port: int,
    wait_seconds: int,
    *,
    profile_dir: str | Path,
    verify: VerifyFn | None = None,
    progress: Callable[[str], None] | None = None,
) -> BrowserLoginResult:
    """执行浏览器登录编排。

    Args:
        url: 浏览器要打开的地址。
        port: CDP 调试端口。
        wait_seconds: 最长等待秒数（硬上限）；<=0 表示只尝试一次不轮询。
        profile_dir: 浏览器独立配置目录。持久化后，下次运行能直接复用登录态。
        verify: 验证回调；提供时只有验证通过的 Cookie 才算成功。
        progress: 进度回调。

    Returns:
        结构化结果；``CancelledError`` 原样上抛以支持 Ctrl+C。
    """
    say = progress or (lambda _msg: None)

    # ---- 1. 用调试端口启动浏览器（普通方式启动的浏览器抓不到 Cookie）
    launch = await _safe_launch(url, port, profile_dir)
    if not launch.ok:
        return BrowserLoginResult(
            ok=False,
            hints=(f"无法启动浏览器：{launch.error}", *launch.hints),
        )
    for h in launch.hints:
        say(h)

    if launch.mode == "plain":
        # 没有调试端口 → 自动获取不可能成功，立刻回退，别让用户白等
        return BrowserLoginResult(
            ok=False,
            mode="plain",
            hints=(
                "已用默认浏览器打开页面，但它不提供调试接口，无法自动抓取 Cookie",
                "请在浏览器里登录后回到这里，选择「手动粘贴 Cookie」",
            ),
        )

    # ---- 2. 轮询：登录完成后尽快捕获并验证（第一轮立即执行，
    #         所以「浏览器里本来就登录着」会被马上识别，不用等）
    outcome = await _poll(port, wait_seconds, say, verify)
    if isinstance(outcome, tuple):
        capture, nickname = outcome
        return BrowserLoginResult(
            ok=True, cookie=capture.cookie, nickname=nickname, mode="debug", capture=capture
        )

    return BrowserLoginResult(
        ok=False,
        mode="debug",
        capture=outcome,
        hints=_failure_hints(outcome),
    )


async def _poll(
    port: int,
    wait_seconds: int,
    say: Callable[[str], None],
    verify: VerifyFn | None,
) -> tuple[CaptureResult, str] | CaptureFailure:
    """轮询直到拿到**通过验证**的 Cookie，或到达 hard deadline。

    Returns:
        成功返回 ``(capture, nickname)``，失败返回最后一次 :class:`CaptureFailure`。
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + max(float(wait_seconds), 0.0)
    idx = 0
    last: CaptureFailure = CaptureFailure(reason="还没有获取到 Cookie")

    while True:
        last = await _probe(port, verify)
        if isinstance(last, tuple):
            return last
        if wait_seconds <= 0:
            return last

        now = loop.time()
        if now >= deadline:
            say("等待超时")
            return last

        elapsed = now - started
        say(f"等待你在浏览器里完成登录…（已等待 {elapsed:.0f}s / {wait_seconds}s，Ctrl+C 可中止）")

        delay = max(float(_POLL_DELAYS[min(idx, len(_POLL_DELAYS) - 1)]), 0.0)
        idx += 1
        # 步长必须为正（否则永不推进），且不得越过 deadline
        step = min(max(delay, 0.01), deadline - now)
        await asyncio.sleep(step)


async def _probe(port: int, verify: VerifyFn | None) -> tuple[CaptureResult, str] | CaptureFailure:
    """捕获一次并（可选）验证。"""
    capture = await _safe_capture(port)
    if isinstance(capture, CaptureFailure):
        return capture
    if verify is None:
        return capture, ""

    ok, message = await _safe_verify(verify, capture.cookie)
    if ok:
        return capture, message
    # 浏览器里可能已有匿名 Cookie，但还没真正登录 —— 继续等，别误报失败
    return CaptureFailure(
        reason=f"已读到 Cookie，但尚未登录成功（{message}）",
        hints=("请在浏览器里完成登录，程序会自动继续检测",),
    )


async def _safe_verify(verify: VerifyFn, cookie: str) -> tuple[bool, str]:
    try:
        return await verify(cookie)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOG.debug("验证 Cookie 时抛出异常: %r", exc)
        return False, f"验证时出错（{_exc_name(exc)}）"


async def _safe_launch(url: str, port: int, profile_dir: str | Path) -> BrowserLaunch:
    try:
        return await launch_debug_browser(url, port, profile_dir, wait_port=_PORT_WAIT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOG.debug("launch_debug_browser 抛出异常: %r", exc)
        return BrowserLaunch(ok=False, error=f"启动浏览器时出错（{_exc_name(exc)}）")


async def _safe_capture(port: int) -> CaptureResult | CaptureFailure:
    """调用捕获并**吞掉一切非取消异常**，折成 :class:`CaptureFailure`。

    底层可能抛出 ``UnicodeDecodeError`` / ``sqlite3.Error`` 之类的意外异常，
    在这个位置冒泡会让 TUI 直接崩掉，而用户其实只想知道"没拿到，重试或粘贴"。
    """
    try:
        return await capture_cookie(port)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOG.debug("capture_cookie 抛出异常: %r", exc)
        return CaptureFailure(reason=f"捕获 Cookie 时出错（{_exc_name(exc)}）")


def _exc_name(exc: BaseException) -> str:
    return type(exc).__name__


def _failure_hints(capture: CaptureFailure | None) -> tuple[str, ...]:
    if isinstance(capture, CaptureFailure):
        return (capture.reason, *capture.hints)
    return ("未能在限定时间内获取到 Cookie",)
