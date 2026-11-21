"""日志脱敏与日志健壮性测试。

这些用例针对两个真实发生过的故障：

1. **脱敏过滤器把日志参数全部 ``str()`` 化**，破坏了 ``%d`` 这类格式说明符。
   httpx 用 ``'HTTP Request: %s %s "%s %d %s"'`` 记录状态码，``503`` 被转成
   ``'503'`` 后格式化抛 TypeError，logging 于是把整段 "--- Logging error ---"
   traceback 打到终端，糊掉了 TUI 界面。
2. 脱敏不彻底：``Cookie: a=1; b=2`` 只遮盖了第一段，后面的 uid / session 仍会落盘。
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import pytest

import meruki_tracker.core.logging_setup as ls
from meruki_tracker.core.logging_setup import RedactionFilter, _redact

# ------------------------------------------------------------------ 记录构造


def _record(msg: str, args: tuple | dict | None = None, name: str = "httpx") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=msg,
        args=args if args is not None else (),
        exc_info=None,
    )


# ------------------------------------------------------------------ 1. 格式化不被破坏


def test_filter_keeps_int_format_args_working():
    """回归：带 %d 的第三方日志不能让 filter 抛错。"""
    rec = _record(
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", "http://127.0.0.1:9222/json/list", "HTTP/1.1", 503, "Service Unavailable"),
    )

    assert RedactionFilter().filter(rec) is True
    # filter 之后仍能正常取出消息（原实现这里会抛 TypeError）
    text = rec.getMessage()
    assert "503" in text
    assert "Service Unavailable" in text


@pytest.mark.parametrize(
    ("msg", "args"),
    [
        ("retry in %d s", (3,)),
        ("float %f", (1.5,)),
        ("%s and %r", ("a", "b")),
        ("dict %(code)d", ({"code": 500},)),
        ("no args at all", ()),
        ("percent literal 100%%", ()),
    ],
)
def test_filter_never_raises_on_any_format(msg, args):
    rec = _record(msg, args)
    RedactionFilter().filter(rec)
    # 取消息本身不抛，说明参数没被破坏
    rec.getMessage()


def test_filter_can_be_emitted_through_real_handler():
    """端到端：经过真实 handler 的 emit 不能触发 logging 错误。"""
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactionFilter())

    logger = logging.getLogger("test.redaction.emit")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        'HTTP Request: %s %s "%s %d %s"',
        "GET",
        "http://127.0.0.1:9222/json/list",
        "HTTP/1.1",
        503,
        "Service Unavailable",
    )

    out = stream.getvalue()
    assert "503" in out
    assert "Logging error" not in out


# ------------------------------------------------------------------ 2. 脱敏仍然有效


def test_filter_redacts_cookie_in_args():
    rec = _record("Cookie: %s", ("MKT_CKID=secret123456; uid=10086",))
    RedactionFilter().filter(rec)

    text = rec.getMessage()
    assert "secret123456" not in text
    assert "10086" not in text
    assert text == "Cookie: ***"


def test_filter_redacts_cookie_inside_formatted_message():
    """敏感值是 msg 的一部分（不是 arg）时也要遮盖。"""
    rec = _record("请求头 Cookie: sid=deadbeef; PHPSESSID=abc123", ())
    RedactionFilter().filter(rec)

    text = rec.getMessage()
    assert "deadbeef" not in text
    assert "abc123" not in text


@pytest.mark.parametrize(
    "text",
    [
        "Cookie: MKT_CKID=abcdef123456; uid=10086; PHPSESSID=xyz789",
        "Set-Cookie: sid=abc; Path=/; HttpOnly",
        "token=secretvalue123",
        "PHPSESSID=xyz789",
        "MKT_CKID=abcdef123456",
    ],
)
def test_redact_removes_known_secret_shapes(text):
    out = _redact(text)
    for secret in ("abcdef123456", "xyz789", "secretvalue123", "10086", "abc; Path"):
        assert secret not in out


@pytest.mark.parametrize(
    "text",
    [
        "正常日志：刷新完成，列表 10 条",
        "刷新失败(network): 网络不通",
        "详情探测：goodsInfo 缺失",
    ],
)
def test_redact_leaves_normal_text_alone(text):
    assert _redact(text) == text


# ------------------------------------------------------------------ 3. setup_logging 的健壮性


@pytest.fixture
def restore_logging():
    """备份并还原全局 logging 状态，避免污染其他测试。"""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_raise = logging.raiseExceptions
    noisy = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}

    yield

    for h in root.handlers[:]:
        if h not in saved_handlers:
            root.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)
    logging.raiseExceptions = saved_raise
    for name, level in noisy.items():
        logging.getLogger(name).setLevel(level)


def test_setup_logging_suppresses_emit_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ls, "_configured", False)
    ls.setup_logging(tmp_path)

    # TUI 程序里日志出错绝不能把 traceback 打到终端
    assert logging.raiseExceptions is False


def test_setup_logging_quiets_noisy_third_party(tmp_path: Path, monkeypatch, restore_logging):
    monkeypatch.setattr(ls, "_configured", False)
    ls.setup_logging(tmp_path)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_setup_logging_writes_file(tmp_path: Path, monkeypatch, restore_logging):
    monkeypatch.setattr(ls, "_configured", False)
    ls.setup_logging(tmp_path)

    from meruki_tracker.core.logging_setup import MonthlySizeRotatingFileHandler

    root = logging.getLogger()
    assert any(isinstance(h, MonthlySizeRotatingFileHandler) for h in root.handlers)
    logging.getLogger("test.redaction.file").warning("Cookie: sid=leakedvalue123")
    for h in root.handlers:
        h.flush()

    log_file = tmp_path / "logs" / "meruki-tracker.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "leakedvalue123" not in content  # 脱敏在文件里也生效


# ------------------------------------------------------------------ 日志滚动：按月或超过 50MB


def _handler(tmp_path: Path, **kwargs):
    from meruki_tracker.core.logging_setup import MonthlySizeRotatingFileHandler

    handler = MonthlySizeRotatingFileHandler(tmp_path / "app.log", **kwargs)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _emit(handler, text: str) -> None:
    record = logging.LogRecord("t", logging.INFO, "", 0, text, (), None)
    handler.handle(record)


def test_default_thresholds_are_50mb_and_monthly(tmp_path: Path):
    from meruki_tracker.core.logging_setup import (
        DEFAULT_BACKUP_COUNT,
        DEFAULT_MAX_BYTES,
        MonthlySizeRotatingFileHandler,
    )

    handler = _handler(tmp_path)
    assert isinstance(handler, MonthlySizeRotatingFileHandler)
    assert DEFAULT_MAX_BYTES == 50 * 1024 * 1024, "上限应为 50MB"
    assert DEFAULT_BACKUP_COUNT >= 1
    handler.close()


def test_rotates_when_size_exceeded(tmp_path: Path):
    handler = _handler(tmp_path, max_bytes=200)
    try:
        for i in range(20):
            _emit(handler, f"第 {i:02d} 条日志，" + "x" * 40)
        handler.flush()
    finally:
        handler.close()

    active = tmp_path / "app.log"
    archives = sorted(p.name for p in tmp_path.glob("app.log.*"))
    assert archives, "超过上限必须产生归档"
    assert active.exists(), "滚动后应新建活动日志"
    # 归档名带月份
    import time

    month = time.strftime("%Y-%m", time.localtime())
    assert any(a.startswith(f"app.log.{month}") for a in archives), archives


def test_size_rolls_same_month_get_sequence_suffix(tmp_path: Path):
    handler = _handler(tmp_path, max_bytes=120)
    try:
        for i in range(30):
            _emit(handler, f"line {i} " + "y" * 40)
        handler.flush()
    finally:
        handler.close()

    import re

    names = sorted(p.name for p in tmp_path.glob("app.log.*"))
    assert len(names) >= 2, names
    # 同月多次滚动：应有带序号后缀的归档（最早的会被 backup_count 裁掉）
    assert any(re.fullmatch(r"app\.log\.\d{4}-\d{2}\.\d+", n) for n in names), names


def test_rotates_when_month_changes(tmp_path: Path, monkeypatch):
    """活动文件属于上个月 → 下一条日志写入前先归档。"""
    import os
    import time

    handler = _handler(tmp_path, max_bytes=0)  # 关掉大小条件，只测按月
    try:
        _emit(handler, "十月的日志")
        handler.flush()

        # 把活动文件的 mtime 伪造成上个月
        active = tmp_path / "app.log"
        prev = time.time() - 40 * 24 * 3600
        os.utime(active, (prev, prev))

        _emit(handler, "十一月的新日志")
        handler.flush()
    finally:
        handler.close()

    prev_month = time.strftime("%Y-%m", time.localtime(time.time() - 40 * 24 * 3600))
    archived = tmp_path / f"app.log.{prev_month}"
    assert archived.exists(), f"应按旧月份归档为 {archived.name}"
    assert "十月的日志" in archived.read_text(encoding="utf-8")
    assert "十一月的新日志" in (tmp_path / "app.log").read_text(encoding="utf-8")


def test_prunes_old_archives(tmp_path: Path):
    handler = _handler(tmp_path, max_bytes=80, backup_count=2)
    try:
        for i in range(60):
            _emit(handler, f"line {i} " + "z" * 40)
        handler.flush()
    finally:
        handler.close()

    archives = list(tmp_path.glob("app.log.*"))
    assert len(archives) <= 2, f"只应保留 {2} 个归档，实际 {len(archives)}"


def test_setup_logging_uses_monthly_size_handler(tmp_path: Path, monkeypatch):
    from meruki_tracker.core.logging_setup import MonthlySizeRotatingFileHandler

    monkeypatch.setattr(ls, "_configured", False)
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    saved_raise = logging.raiseExceptions
    try:
        ls.setup_logging(tmp_path)
        handlers = [h for h in root.handlers if isinstance(h, MonthlySizeRotatingFileHandler)]
        assert handlers, "应挂上按月+大小滚动的 handler"
        assert handlers[0].max_bytes == ls.DEFAULT_MAX_BYTES
        assert handlers[0].backup_count == ls.DEFAULT_BACKUP_COUNT
    finally:
        for h in root.handlers[:]:
            if h not in saved:
                root.removeHandler(h)
                with contextlib.suppress(Exception):
                    h.close()
        for h in saved:
            if h not in root.handlers:
                root.addHandler(h)
        root.setLevel(saved_level)
        logging.raiseExceptions = saved_raise


# ------------------------------------------------------------------ 3. traceback 与 JSON 形态


def test_traceback_text_is_redacted_too():
    """``log.exception`` 的 traceback 里也不能留 Cookie。

    ``RedactionFilter`` 只改 ``record.msg`` / ``args``；traceback 是
    ``Formatter.format()`` 事后拼上去的，所以异常消息里的会话串会原样落盘
    （实测确认过）。脱敏必须发生在**最终文本**上。
    """
    try:
        raise RuntimeError("抓到 kmgSession=ABC123XYZ789 和 PHPSESSID=deadbeefcafe")
    except RuntimeError:
        rec = logging.LogRecord(
            name="probe",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="登录失败",
            args=(),
            exc_info=True,
        )

    # exc_info 里要带上真实异常，否则没有 traceback 可脱敏
    import sys

    try:
        raise RuntimeError("抓到 kmgSession=ABC123XYZ789 和 PHPSESSID=deadbeefcafe")
    except RuntimeError:
        rec.exc_info = sys.exc_info()

    out = ls.RedactingFormatter("%(message)s").format(rec)
    assert "ABC123XYZ789" not in out
    assert "deadbeefcafe" not in out
    assert "登录失败" in out


def test_json_style_cookie_loses_the_secret_but_keeps_the_shape():
    """JSON / dict repr 里的 Cookie：值要盖掉，结尾的引号和花括号不能一起吞。"""
    out = _redact('headers={"Cookie": "kmgSession=ABC123XYZ789; uid=998877"}')
    assert "ABC123XYZ789" not in out
    assert "998877" not in out
    assert out.endswith('"}'), out


@pytest.mark.parametrize(
    "text",
    [
        "kmgSession=SHORT",
        "password=abc",
        '{"cookie": "sid=abcdef123456"}',
    ],
)
def test_short_or_json_secrets_are_still_redacted(text):
    out = _redact(text)
    for secret in ("SHORT", "abc", "abcdef123456"):
        assert secret not in out, out


def test_cookie_header_keeps_trailing_debug_prose_after_a_double_space():
    """Cookie 头脱敏不该把同一行后面的调试文字也吞掉。

    旧规则吞到行尾：``Cookie: sid=abc  retry #3`` → ``Cookie: ***``，后面的
    ``retry #3`` 没了。会话串必须盖住，但独立的调试信息应该留着。
    """
    out = _redact("Cookie: sid=abc123456; uid=998877  retry attempt #3 failed")
    assert "abc123456" not in out
    assert "998877" not in out
    assert "retry attempt #3 failed" in out, out
