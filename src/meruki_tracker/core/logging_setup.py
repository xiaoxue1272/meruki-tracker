"""日志配置 + 强制脱敏。

RedactionFilter 挂在 handler 上：不依赖调用方自觉，日志里永远看不到 Cookie。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from pathlib import Path

#: 值的结束符：空白、分隔符、以及 **引号/括号**。
#:
#: 以前只排除了空白和分号，于是 ``headers={"Cookie": "a=1; uid=998877"}`` 这种
#: JSON / dict repr 里，``uid=`` 的值会把结尾的 ``"}`` 一起吞掉——脱敏的同时
#: 把半行日志也啃没了。引号与括号必须排除在外。
_VALUE = r"[^\s;&\"'\}\]\)]+"

_REDACT_PATTERNS = [
    # 整条 Cookie 头：值要吞到头尾（含 "a=1; b=2" 里的后续条目），但保留 "Cookie:"
    # 前缀让人看得出原本是什么。终点是行尾、或两个以上连续空格——后者是日志里
    # 「Cookie: ...  后面的调试文字」的分界，不能把独立的调试信息也一起吞掉。
    # 用两个空格而不是一个：Cookie 值里本来就可能带单个空格（如 "a=1; b=2"）。
    re.compile(r"(Cookie\s*[:=]\s*)\S.*?(?=[\r\n]| {2,}|$)", re.IGNORECASE),
    re.compile(r"(Set-Cookie\s*[:=]\s*)\S.*?(?=[\r\n]| {2,}|$)", re.IGNORECASE),
    # 按**键名**脱敏，不看值的长度：``password=abc`` 也是密码。
    re.compile(
        rf"((?:token|password|passwd|secret|auth)\s*[=:]\s*)({_VALUE})",
        re.IGNORECASE,
    ),
    # ``Authorization: Bearer eyJ...``：头里没有 = 号，得单独认
    re.compile(r"(Bearer\s+)([A-Za-z0-9._~+/-]{8,}=*)", re.IGNORECASE),
    # 敏感键名单独出现时也要遮盖（如日志里只打了 "PHPSESSID=..."）。
    # ``kmgSession`` 是本项目真实在用的会话 Cookie，漏掉它等于没脱敏。
    re.compile(
        r"\b((?:PHPSESSID|JSESSIONID|sessionid|session_id|sessid|sid|uid|userid|user_id|"
        r"kmgSession\d*|MKT_CKID|MKT_[A-Z_]+|_ga|_gid)\s*=\s*)(" + _VALUE + r")",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:kmg[A-Za-z0-9]{10,}|[A-Za-z0-9_-]{40,})\b"),
]


#: 单个日志文件的大小上限：超过即滚动（月度滚动之外的第二道闸）
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
#: 保留的历史归档数（按月滚动，12 个 ≈ 一年）
DEFAULT_BACKUP_COUNT = 12


class MonthlySizeRotatingFileHandler(logging.FileHandler):
    """**按月或超过 max_bytes** 即滚动的日志 handler。

    为什么不用标准库：

    - ``RotatingFileHandler`` 只看大小，不按月；
    - ``TimedRotatingFileHandler`` 只按固定间隔（S/M/H/D），没有"月"这个单位。

    归档文件命名 ``<base>.<YYYY-MM>``；同一个月内因超限多次滚动时追加序号
    ``<base>.<YYYY-MM>.1``、``.2`` ……。只保留最近 ``backup_count`` 个归档
    （按修改时间），避免日志无限堆积。

    全部操作失败都不外抛：这是 TUI 程序，写日志出问题绝不能把 traceback
    糊到界面上（调用方还会把 ``logging.raiseExceptions`` 关掉）。
    """

    def __init__(
        self,
        filename: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        encoding: str = "utf-8",
    ) -> None:
        # delay=True：首次 emit 才真正打开文件，构造失败不会在启动期抛异常
        super().__init__(filename, mode="a", encoding=encoding, delay=True)
        self.max_bytes = max(0, max_bytes)
        self.backup_count = max(0, backup_count)

    # ---------------------------------------------------------------- 判定

    def should_rollover(self, record: logging.LogRecord) -> bool:
        path = Path(self.baseFilename)
        if not path.exists():
            return False
        try:
            mtime = path.stat().st_mtime
            size = path.stat().st_size
        except OSError:
            return False

        # 条件一：跨月（活动文件是上个月的）
        if time.strftime("%Y-%m", time.localtime(mtime)) != time.strftime(
            "%Y-%m", time.localtime(time.time())
        ):
            return True

        # 条件二：加上本条后超过 50MB
        if self.max_bytes <= 0:
            return False
        try:
            payload = len((self.format(record) + self.terminator).encode(self.encoding or "utf-8"))
        except Exception:
            return False
        return size + payload > self.max_bytes

    # ---------------------------------------------------------------- 滚动

    def do_rollover(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None

        path = Path(self.baseFilename)
        if not path.exists():
            return
        try:
            stamp = time.strftime("%Y-%m", time.localtime(path.stat().st_mtime))
        except OSError:
            stamp = time.strftime("%Y-%m", time.localtime())

        target = path.with_name(f"{path.name}.{stamp}")
        index = 0
        while target.exists():  # 同月多次滚动：.1 / .2 ...
            index += 1
            target = path.with_name(f"{path.name}.{stamp}.{index}")
        try:
            os.replace(path, target)
        except OSError:
            return
        self._prune(path)

    def _prune(self, base: Path) -> None:
        if self.backup_count <= 0:
            return
        try:
            archives = sorted(
                (p for p in base.parent.glob(f"{base.name}.*") if p != base),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for old in archives[self.backup_count :]:
            with contextlib.suppress(OSError):
                old.unlink()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.should_rollover(record):
                self.do_rollover()
            super().emit(record)
        except Exception:  # 日志失败绝不影响主流程
            self.handleError(record)


class RedactionFilter(logging.Filter):
    """把日志里的敏感值替换为 ***。

    实现要点：**先按原始参数格式化出最终文本，再对文本脱敏**，最后清空 ``args``。

    早期版本把 ``record.args`` 里每一项 ``str()`` 后塞回去，看似等价，实则破坏了
    所有带格式说明符的日志：httpx 用 ``'HTTP Request: %s %s "%s %d %s"'`` 记录状态码，
    ``%d`` 拿到字符串就抛 TypeError，logging 随即把整段 "--- Logging error ---"
    traceback 打到终端，直接糊掉 TUI 界面。先格式化再脱敏可以避开这个坑。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 格式化失败也不能让日志把进程搅乱
            message = str(record.msg)
        record.msg = _redact(message)
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """在**最终文本**（含 traceback）上再脱敏一次。

    ``RedactionFilter`` 只能改 ``record.msg`` / ``record.args``，而 traceback 是
    ``Formatter.format()`` 事后拼上去的——异常消息里带 Cookie 时会原样落盘
    （实测：``log.exception`` 抛一个含 ``kmgSession=...`` 的异常，日志里完整可见）。
    所以格式化结果必须再过一遍。
    """

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def _redact(text: str) -> str:
    """把文本里的敏感值替换成 ``***``，保留键名/前缀以便看懂日志上下文。"""
    for pattern in _REDACT_PATTERNS:
        if pattern.groups >= 1:
            text = pattern.sub(lambda m: m.group(1) + "***", text)
        else:
            text = pattern.sub("***", text)
    return text


_configured = False


def setup_logging(data_dir: Path, level: int = logging.INFO) -> None:
    """配置根 logger：文件 + 控制台（TUI 模式下由调用方禁用控制台）。"""
    global _configured
    if _configured:
        return
    _configured = True

    # 这是 TUI 程序：日志出任何岔子都不许把 traceback 打到终端上糊掉界面。
    # 关掉 logging 的"处理异常即报错"行为，让写日志失败只静默丢弃。
    logging.raiseExceptions = False

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    # 第三方库的逐请求 INFO 日志（httpx 会为每个请求打一行）对排查没帮助，
    # 却会把日志文件刷满。压到 WARNING。
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    fmt = RedactingFormatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    redactor = RedactionFilter()

    try:
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = MonthlySizeRotatingFileHandler(
            log_dir / "meruki-tracker.log",
            max_bytes=DEFAULT_MAX_BYTES,
            backup_count=DEFAULT_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.addFilter(redactor)
        root.addHandler(fh)
    except OSError:
        pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
