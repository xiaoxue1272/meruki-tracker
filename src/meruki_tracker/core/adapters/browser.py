"""跨平台拉起系统默认浏览器。

只做"打开一个 URL"这一件事，不依赖任何第三方库：

- macOS  : ``open <url>``
- Windows: ``os.startfile``（回退 ``start``）
- Linux  : ``xdg-open``（回退 ``gio open`` / ``sensible-browser``）

设计要点：

1. **绝不阻塞事件循环**：子进程用 ``asyncio.create_subprocess_exec`` 启动；
   Windows 的 ``os.startfile`` 是同步 API，用 ``asyncio.to_thread`` 包起来跑。
2. **失败要能被上层感知**：返回 :class:`OpenResult`，而不是静默吞掉——
   在没有图形环境的服务器上必须让用户知道"打不开浏览器"，以便回退到粘贴。
3. **不在本模块做任何 Cookie 相关的事**：拦截/捕获在 :mod:`.cookie_capture`，
   两者失败语义独立，互不牵连。
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .cookie_capture import wait_for_debug_port


@dataclass(frozen=True, slots=True)
class OpenResult:
    """拉起浏览器的结果。"""

    ok: bool
    #: 实际执行的命令（便于诊断；Windows 的 startfile 记为 "os.startfile"）
    command: str = ""
    #: 失败原因（ok=False 时非空）
    error: str = ""
    #: 补充提示，无论成败都可能非空（例如"服务器无图形环境"）
    hints: tuple[str, ...] = field(default_factory=tuple)


def _candidates() -> list[list[str]]:
    """按平台给出候选命令，前面的优先。"""
    if sys.platform == "darwin":
        return [["open"]]
    if os.name == "nt":
        # Windows 用 startfile，命令列表只作占位
        return [["os.startfile"]]
    # 其余按 Linux/BSD 处理
    return [["xdg-open"], ["gio", "open"], ["sensible-browser"]]


def has_graphical_session() -> bool:
    """粗略判断当前是否有图形会话。

    - macOS / Windows：假定有（真在无头环境里 open 会自己失败）。
    - Linux：DISPLAY 或 WAYLAND_DISPLAY 至少一个存在才算有。

    这只是一个"提前给出友好提示"的启发式，**不用它来阻止启动浏览器**——
    该失败的总会失败，交给 :func:`open_url` 如实回报。
    """
    if sys.platform == "darwin" or os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


async def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, stderr 摘录)。

    超时：先 kill 再 ``await proc.wait()`` 回收，避免僵尸进程与
    "subprocess still running" 资源警告。
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
        raise
    # returncode 为 None 说明进程还没退（理论上 communicate 后不会），
    # 不能把 None 当成功：用 -1 表示"未知/未正常结束"。
    return (proc.returncode if proc.returncode is not None else -1), (stderr or b"").decode(
        "utf-8", "replace"
    ).strip()


async def open_url(url: str, *, timeout: float = 10.0, total_timeout: float = 15.0) -> OpenResult:
    """用系统默认浏览器打开 ``url``。

    Args:
        url: 要打开的地址。
        timeout: 单次尝试的等待上限（秒）。浏览器进程通常立刻返回，
            这个上限只是防止某些平台上的僵死。
        total_timeout: 所有候选命令合计的等待上限（秒）。Linux 上有 3 条候选，
            不设总上限最坏要等 ``3 × timeout``；超限直接按失败返回，
            让上层尽快回退到手动粘贴。

    Returns:
        结果对象；``ok=False`` 时 ``error`` 说明每次尝试的失败原因。
    """
    hints: list[str] = []
    if not has_graphical_session():
        hints.append("未检测到图形会话（无 DISPLAY/WAYLAND_DISPLAY），浏览器可能打不开")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    errors: list[str] = []

    for cmd in _candidates():
        remaining = deadline - loop.time()
        if remaining <= 0:
            errors.append(f"{cmd[0]}: 跳过（已达总等待上限 {total_timeout:g} 秒）")
            continue

        if cmd == ["os.startfile"]:
            # os.startfile 是同步调用：包进线程，兑现"绝不阻塞事件循环"的承诺
            try:
                await asyncio.to_thread(os.startfile, url)  # type: ignore[attr-defined]
                return OpenResult(ok=True, command="os.startfile", hints=tuple(hints))
            except OSError as exc:
                errors.append(f"os.startfile: {exc}")
                continue

        try:
            code, err = await _run([*cmd, url], min(timeout, remaining))
        except FileNotFoundError:
            errors.append(f"{cmd[0]}: 未找到该命令")
            continue
        except (TimeoutError, OSError) as exc:
            errors.append(f"{cmd[0]}: {exc}")
            continue

        if code == 0:
            return OpenResult(ok=True, command=" ".join(cmd), hints=tuple(hints))
        errors.append(f"{cmd[0]}: 退出码 {code}{(' - ' + err) if err else ''}")

    return OpenResult(
        ok=False,
        error="；".join(errors) or "没有可用的浏览器启动命令",
        hints=tuple(hints),
    )


# --------------------------------------------------------------- 带调试端口启动


#: 已启动的浏览器进程。**必须持有引用**：asyncio 的 SubprocessTransport 在回收时
#: 会 kill 掉仍在运行的子进程（"unclosed transport"），表现就是浏览器窗口一闪就关。
_LAUNCHED: list[asyncio.subprocess.Process] = []
#: 回收任务同样要持有引用，否则任务本身被 GC 会取消回收。
_REAPERS: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class BrowserLaunch:
    """启动浏览器以支持 CDP 抓取的结果。

    ``mode`` 含义：

    - ``debug``：已用独立配置目录 + ``--remote-debugging-port`` 启动，CDP 可用；
    - ``plain``：只能普通方式打开（没找到 Chrome 系浏览器），**CDP 不可用**，
      自动获取 Cookie 必然失败，调用方应立即回退而不是白白等待。
    """

    ok: bool
    mode: str = ""
    executable: str = ""
    error: str = ""
    hints: tuple[str, ...] = field(default_factory=tuple)


#: macOS 上支持 CDP（--remote-debugging-port）的 Chromium 系浏览器。
#: Safari 与 Firefox 不在其中：它们不提供 CDP，无法自动读取 Cookie。
_MAC_CHROMIUM_APPS: list[tuple[str, str]] = [
    ("Google Chrome", "Google Chrome"),
    ("Google Chrome Canary", "Google Chrome Canary"),
    ("Chromium", "Chromium"),
    ("Microsoft Edge", "Microsoft Edge"),
    ("Brave Browser", "Brave Browser"),
    ("Vivaldi", "Vivaldi"),
    ("Arc", "Arc"),
    ("Opera", "Opera"),
]


def _mac_chrome_paths() -> list[str]:
    """macOS 上 Chromium 系浏览器的可执行文件路径（按偏好排序）。

    同时查 /Applications 与 ~/Applications：很多用户把浏览器装在用户目录下。
    """
    roots = [
        "/Applications",
        str(Path.home() / "Applications"),
        # Homebrew Cask 也支持装到这些位置（尤其是非管理员用户）
        str(Path.home() / ".local" / "Applications"),
        "/opt/homebrew/Caskroom",
        "/usr/local/Caskroom",
        "/opt/homebrew/Applications",
        "/usr/local/Applications",
    ]
    paths: list[str] = []
    for app, binary in _MAC_CHROMIUM_APPS:
        for root in roots:
            if root.endswith("Caskroom"):
                # Caskroom 里的布局是 <Caskroom>/<app>/<version>/<app>.app/...
                paths.append(f"{root}/{app}/*/{app}.app/Contents/MacOS/{binary}")
            else:
                paths.append(f"{root}/{app}.app/Contents/MacOS/{binary}")

    # PATH 里的命令行版本（brew install chromium / chromium-browser）
    for name in ("chromium", "google-chrome", "chrome", "brave-browser"):
        found = shutil.which(name)
        if found:
            paths.append(found)
    return paths


#: Windows 上各 Chromium 系浏览器的相对安装路径（相对 PROGRAMFILES / LOCALAPPDATA）。
_WIN_CHROMIUM_RELS: list[str] = [
    r"Google\Chrome\Application\chrome.exe",
    r"Google\Chrome Beta\Application\chrome.exe",
    r"Google\Chrome SxS\Application\chrome.exe",  # Canary
    r"Microsoft\Edge\Application\msedge.exe",
    r"Microsoft\Edge Beta\Application\msedge.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
    r"Vivaldi\Application\vivaldi.exe",
    r"Opera\launcher.exe",
    r"Arc\Application\Arc.exe",
]


def _win_chrome_paths() -> list[str]:
    """Windows 上 Chromium 系浏览器的可执行文件路径。

    除程序目录外还查 ``LOCALAPPDATA``：Chrome / Brave / Vivaldi 等默认
    装在用户目录下（``%LOCALAPPDATA%\\Google\\Chrome\\Application``）。
    """
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    return [os.path.join(r, rel) for r in roots if r for rel in _WIN_CHROMIUM_RELS]


def find_chrome() -> str:
    """定位 Chrome 系浏览器可执行文件；找不到返回空串。

    只有 Chrome 系支持 CDP 抓 Cookie（且要求独立配置目录 + 调试端口），
    Firefox/Safari 不走这条路径。
    """
    if sys.platform == "darwin":
        candidates = _mac_chrome_paths()
    elif os.name == "nt":
        candidates = _win_chrome_paths()
    else:
        candidates = []
        names = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
            "brave-browser",
            "vivaldi",
            "opera",
            "opera-stable",
        )
        for name in names:
            found = shutil.which(name)
            if found:
                candidates.append(found)

        # 发行版打包方式：snap / flatpak 都不会进 PATH 的标准位置
        candidates += [
            "/snap/bin/chromium",
            "/var/lib/flatpak/exports/bin/com.google.Chrome",
            "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
            str(Path.home() / ".local/share/flatpak/exports/bin/com.google.Chrome"),
            str(Path.home() / ".local/share/flatpak/exports/bin/org.chromium.Chromium"),
        ]

    for path in candidates:
        if not path:
            continue
        if "*" in path:
            # 带版本号的路径用 glob 展开（Homebrew Caskroom）
            for hit in sorted(glob.glob(path), reverse=True):
                if Path(hit).exists():
                    return hit
            continue
        if Path(path).exists():
            return path
    return ""


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """等浏览器进程退出后回收，并从引用表里移除。"""
    try:
        await proc.wait()
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - 回收失败不影响主流程
        pass
    finally:
        with contextlib.suppress(ValueError):
            _LAUNCHED.remove(proc)


async def launch_debug_browser(
    url: str,
    port: int,
    profile_dir: str | Path,
    *,
    wait_port: float = 10.0,
) -> BrowserLaunch:
    """用调试端口 + 独立配置目录启动 Chrome，供 CDP 读取 Cookie。

    为什么必须这样做（而不是 ``open <url>``）：

    - 普通方式启动的 Chrome 不会开放 CDP，程序永远抓不到 Cookie；
    - 新版 Chrome 在**默认**配置目录下会拒绝开启远程调试，必须指定
      ``--user-data-dir``；
    - 配置目录固定下来（默认在项目 ``data/chrome-profile``），下次运行时
      浏览器里仍是已登录状态，可以立刻拿到 Cookie。

    Args:
        url: 启动后要打开的地址。
        port: 调试端口。
        profile_dir: 独立配置目录（持久化，这样"已登录"能跨次运行保留）。
        wait_port: 等待调试端口就绪的秒数。

    Returns:
        :class:`BrowserLaunch`。``ok=False`` 表示浏览器没起来。
    """
    exe = find_chrome()
    if not exe:
        opened = await open_url(url)
        if opened.ok:
            return BrowserLaunch(
                ok=True,
                mode="plain",
                hints=(
                    "没找到 Chromium 系浏览器（Chrome / Edge / Brave / Arc / Vivaldi / Opera）",
                    "已用系统默认浏览器打开；Safari / Firefox 不提供调试接口，无法自动读取 Cookie",
                    "登录后请回到这里选择「手动粘贴 Cookie」",
                    "装一个 Chromium 系浏览器即可恢复自动登录",
                ),
            )
        return BrowserLaunch(ok=False, error=opened.error, hints=opened.hints)

    profile = Path(profile_dir)
    try:
        profile.mkdir(parents=True, exist_ok=True)
        # 配置目录里会有登录态，权限收紧到仅本人可读
        with contextlib.suppress(OSError):
            os.chmod(profile, 0o700)
    except OSError as exc:
        return BrowserLaunch(ok=False, error=f"无法创建浏览器配置目录 {profile}: {exc}")

    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    try:
        # start_new_session：浏览器不随 TUI 的 Ctrl+C 一起死，用户可以继续用。
        # 必须把 Process 存起来，否则 GC 时它的 transport 会 kill 掉浏览器进程。
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        _LAUNCHED.append(proc)
        # 浏览器退出后回收，避免僵尸；任务引用必须留存（见 _REAPERS 注释）
        reaper = asyncio.create_task(_reap(proc))
        _REAPERS.add(reaper)
        reaper.add_done_callback(_REAPERS.discard)
    except OSError as exc:
        return BrowserLaunch(
            ok=False,
            executable=exe,
            error=f"启动浏览器失败：{exc}",
            hints=("请手动运行 mt 提示里的 Chrome 命令，或改用手动粘贴 Cookie",),
        )

    if not await wait_for_debug_port(port, timeout=wait_port):
        return BrowserLaunch(
            ok=False,
            mode="debug",
            executable=exe,
            error=f"浏览器已启动，但 {wait_port:.0f}s 内调试端口 {port} 没有就绪",
            hints=(
                "可能是已有的 Chrome 实例占用了该配置目录或端口",
                "请完全退出 Chrome 后重试，或改用手动粘贴 Cookie",
            ),
        )

    return BrowserLaunch(ok=True, mode="debug", executable=exe)
