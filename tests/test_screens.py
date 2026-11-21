"""TUI 各屏幕的渲染测试。

旧版这些断言针对 ``ui.overlays`` 里「打印到终端 + input() 等待」的函数；
重构后所有屏幕都是**纯渲染函数**（只往传入的 Console 画一屏），所以测试也
变成"画一次、看输出"，不再需要 monkeypatch 终端输出。
"""

from __future__ import annotations

import io
import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from rich.console import Console
from rich.text import Text

from meruki_tracker.core.adapters.store.snapshot import Account, LastAttempt, Snapshot
from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery
from meruki_tracker.tui import screens
from meruki_tracker.tui.views import ListView

TZ = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")


def _st(
    name: str = "测试拍品", *, asin: str = "A1", end_time: EndTime | None = None
) -> AuctionState:
    return AuctionState(
        asin=asin,
        name=name,
        url="https://x/A1",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=end_time or EndTime.exact(datetime(2026, 11, 1, tzinfo=UTC), datetime.now(UTC)),
        latest_bid=100.0,
    )


def _draw(fn, **kwargs) -> str:
    """调用渲染函数并返回其输出文本（无 ANSI 转义）。"""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    fn(console, **kwargs)
    return buf.getvalue()


# ------------------------------------------------------------------ 列表屏


def test_list_renders_rows_and_hints():
    out = _draw(
        screens.render_list,
        snap=Snapshot(auctions=(_st(),)),
        rows=[_st()],
        cursor=0,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
    )
    assert "挖煤姬拍卖" in out
    assert "测试拍品" in out
    assert "q 退出" in screens.footer_text("list")


def test_list_empty_state():
    out = _draw(
        screens.render_list,
        snap=Snapshot(),
        rows=[],
        cursor=0,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
    )
    assert "还没有采集过数据" in out


def test_list_shows_busy_over_status():
    out = _draw(
        screens.render_list,
        snap=Snapshot(),
        rows=[],
        cursor=0,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
        status="旧状态",
        busy="采集：列表 第 1 页",
    )
    assert "采集：列表 第 1 页" in out
    assert "旧状态" not in out


def test_list_shows_notice_lines():
    out = _draw(
        screens.render_list,
        snap=Snapshot(),
        rows=[],
        cursor=0,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
        notice=("✗ 登录失败：Cookie 无效", "按 s 进配置重新登录"),
    )
    assert "登录失败" in out
    assert "按 s 进配置重新登录" in out


def test_list_shows_stale_and_last_error():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    out = _draw(
        screens.render_list,
        snap=Snapshot(
            auctions=(_st(),),
            # 90 分钟前的数据（默认 30 分钟算过期）
            generated_at=now - timedelta(minutes=90),
            last_attempt=LastAttempt(at=now, ok=False, error="网络不通"),
        ),
        rows=[_st()],
        cursor=0,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
    )
    assert "数据已过期" in out, "过期要在新鲜度行明确标出"
    # 采集时间用统一的 YYYY-MM-DD HH:MM:SS，别只给个 HH:MM:SS 看不出哪天
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", out), "过期时也要显示采集时间"


def test_list_marks_cursor_row():
    a, b = _st("第一件", asin="A1"), _st("第二件", asin="A2")
    out = _draw(
        screens.render_list,
        snap=Snapshot(auctions=(a, b)),
        rows=[a, b],
        cursor=1,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
    )
    line_with_second = next(line for line in out.splitlines() if "第二件" in line)
    assert "❯" in line_with_second


# ------------------------------------------------------------------ markup 安全（崩溃红线）


HOSTILE_NAMES = [
    "Nintendo Switch[/]本体",
    "[bold]x",
    "[/]",
    "[[",
    "]]",
    "日本[限定]",
    "[red]a[/red][blue]b",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_list_survives_markup_in_name(name):
    st = _st(name)
    out = _draw(
        screens.render_list,
        snap=Snapshot(auctions=(st,)),
        rows=[st],
        cursor=0,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
    )
    # 原样保留，不被当作 markup 解析掉
    assert name.replace("\n", "") in out.replace("\n", "")


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_detail_survives_markup_in_name(name):
    st = _st(name)
    out = _draw(
        screens.render_detail,
        st=st,
        snapshot=Snapshot(),
        observations=[],
        tz=TZ,
    )
    assert "拍品详情" in out


@pytest.mark.parametrize("raw", ["[bold]raw[/]", "[/]", "[red]a[/red]"])
def test_detail_survives_markup_in_observations(raw):
    out = _draw(
        screens.render_detail,
        st=_st(),
        snapshot=Snapshot(),
        observations=[("2026-10-29T12:00:00", "list", raw)],
        tz=TZ,
    )
    assert "最近观测" in out


# ------------------------------------------------------------------ 详情屏


def test_detail_shows_fields():
    out = _draw(
        screens.render_detail,
        st=_st(),
        snapshot=Snapshot(),
        observations=[],
        tz=TZ,
    )
    for fragment in ("拍品号", "链接", "当前价", "剩余", "结束时间"):
        assert fragment in out


def test_detail_without_observations_says_so():
    out = _draw(screens.render_detail, st=_st(), snapshot=Snapshot(), observations=[], tz=TZ)
    assert "还没有观测记录" in out


# ------------------------------------------------------------------ 登录屏


def test_login_method_mentions_server_tip():
    out = _draw(screens.render_login_method, has_cookie=False)
    assert "浏览器登录" in out
    assert "手动粘贴" in out
    assert "1" in out and "2" in out


def test_login_method_warns_about_overwrite():
    out = _draw(screens.render_login_method, has_cookie=True)
    assert "覆盖" in out


def test_login_paste_shows_masked_input():
    out = _draw(screens.render_login_paste, masked="sid=ab******(10)", count=14)
    assert "已输入" in out
    assert "14" in out
    assert "Enter 提交" in screens.footer_text("login-paste")


def test_login_paste_empty_input():
    out = _draw(screens.render_login_paste, masked="", count=0)
    assert "空" in out


def test_cookie_confirm_defaults_to_masked():
    """渲染默认必须脱敏：明文不能出现在屏幕上。"""
    out = _draw(
        screens.render_cookie_confirm,
        cookie="sid=secretvalue123",
        source="cdp",
        reveal=False,
    )
    assert "secretvalue123" not in out
    assert "已脱敏" in out
    assert "Enter 登录" in screens.footer_text("cookie-confirm")


def test_cookie_confirm_reveal_shows_plaintext():
    out = _draw(
        screens.render_cookie_confirm,
        cookie="sid=secretvalue123",
        source="cdp",
        reveal=True,
    )
    assert "secretvalue123" in out
    assert "Cookie 全文" in out


def test_login_wait_shows_status_and_hints():
    out = _draw(
        screens.render_login_wait,
        title="浏览器登录",
        status="等待你在浏览器里完成登录…",
        hints=("程序会启动独立 Chrome",),
    )
    assert "等待你在浏览器里完成登录" in out
    assert "独立 Chrome" in out


# ------------------------------------------------------------------ Cookie 脱敏


def test_mask_cookie_short_values_fully_masked():
    """短值不能再泄露前 2 个字符：信息量已接近明文。"""
    assert screens.mask_cookie("a=xyz") == "a=***"
    assert screens.mask_cookie("k=v") == "k=***"
    assert screens.mask_cookie("n=abcd") == "n=***"
    assert "xy" not in screens.mask_cookie("a=xyz")


def test_mask_cookie_long_values_keep_prefix_and_length():
    """长值只留前 2 字符 + 长度：定长输出，不随真实 Cookie 长度膨胀。"""
    assert screens.mask_cookie("sid=abcdefgh") == "sid=ab***(len=8)"


def test_mask_cookie_empty_value_marked():
    assert screens.mask_cookie("e=") == "e=(空)"
    assert screens.mask_cookie("e=; sid=abcdefgh") == "e=(空); sid=ab***(len=8)"


def test_mask_cookie_drops_malformed_parts():
    masked = screens.mask_cookie("novalue; sid=abcdefgh; ;")
    assert "novalue" not in masked
    assert masked == "sid=ab***(len=8)"


def test_login_method_mentions_server_usage():
    """在服务器上运行的场景：引导用户在本机登录后复制 Cookie。"""
    out = _draw(screens.render_login_method, has_cookie=False)
    assert "服务器" in out


def test_cookie_confirm_tells_where_plaintext_is_saved():
    """脱敏上屏，但要告诉用户全文明文落在哪，方便拿去服务器用。"""
    out = _draw(
        screens.render_cookie_confirm,
        cookie="sid=secretvalue123",
        source="cdp",
        reveal=False,
        cookie_path="data/cookies",
    )
    assert "secretvalue123" not in out
    assert "data/cookies" in out


def test_cookie_confirm_without_path_still_renders():
    out = _draw(
        screens.render_cookie_confirm,
        cookie="sid=secretvalue123",
        source="cdp",
        reveal=False,
    )
    assert "已脱敏" in out


# ------------------------------------------------------------------ 布局自适应


def _draw_at(fn, width: int, height: int = 24, **kwargs) -> str:
    import io

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, height=height)
    fn(console, **kwargs)
    return buf.getvalue()


def _many_states(n: int = 8):
    return [_st(f"ニコン Nikon AF-S 70-200mm f2.8 商品{i}", asin=f"A{i}") for i in range(n)]


@pytest.mark.parametrize("width", [40, 52, 60, 72, 100, 120])
def test_list_never_exceeds_terminal_width(width):
    """任何常见宽度都不该出现超宽行（否则会折行/错位）。"""
    rows = _many_states()
    out = _draw_at(
        screens.render_list,
        width,
        snap=Snapshot(auctions=tuple(rows)),
        rows=rows,
        cursor=0,
        page=0,
        page_size=6,
        tz=TZ,
        stale_minutes=30,
    )
    overflow = [line for line in out.splitlines() if len(line) > width]
    assert not overflow, overflow


def test_list_narrow_keeps_countdown_column():
    """窄终端可以砍"当前价/结束时间"，但**倒计时必须留着**（产品核心）。"""
    rows = _many_states(2)
    out = _draw_at(
        screens.render_list,
        44,
        snap=Snapshot(auctions=tuple(rows)),
        rows=rows,
        cursor=0,
        page=0,
        page_size=6,
        tz=TZ,
        stale_minutes=30,
    )
    assert "剩余" in out
    assert "当前价" not in out, "窄屏应先砍当前价，而不是倒计时"


def test_list_long_notice_is_truncated_not_wrapping():
    rows = _many_states(1)
    out = _draw_at(
        screens.render_list,
        60,
        snap=Snapshot(auctions=tuple(rows)),
        rows=rows,
        cursor=0,
        page=0,
        page_size=6,
        tz=TZ,
        stale_minutes=30,
        notice=("✗ " + "非常长的错误信息" * 10,),
    )
    assert all(len(line) <= 60 for line in out.splitlines())


# ------------------------------------------------------------------ 审计修复回归


def test_list_footer_shows_numeric_page_count():
    """页脚不能把方法对象当页码（曾渲染成 <bound method ...>）。"""
    text = screens.footer_text("list", page=0, page_count=2)
    assert "1/2" in text
    assert "bound method" not in text


def test_list_long_name_does_not_wrap_or_hide_cursor():
    """长标题不能把倒计时列挤折行、更不能用光标消失当代价。"""
    rows = [_st("あ" * 40 + " Nintendo Switch 本体 セット", asin=f"A{i}") for i in range(6)]
    out = _draw_at(
        screens.render_list,
        80,
        snap=Snapshot(auctions=tuple(rows)),
        rows=rows,
        cursor=0,
        page=0,
        page_size=6,
        tz=TZ,
        stale_minutes=30,
    )
    lines = out.splitlines()
    # 表头 + 6 条数据 + 标题 + 页脚 ≈ 9 行；若列被挤折行会明显变多
    assert len(lines) <= 10, lines
    assert "❯" in out, "光标必须可见"
    assert "当前价" in out


def test_list_remaining_column_fits_longest_countdown(monkeypatch):
    """剩余列必须容得下最长倒计时（曾宽 11 < 12 导致每行折两行）。"""
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from meruki_tracker.tui.screens import list as list_screen

    # render_list 内部也取 datetime.now(UTC)。必须把它和这里钉成同一固定时刻：
    # 两次真实取值的差决定倒计时分钟数，时钟分辨率粗的 runner 上落在同一 tick
    # 会得到精确 10时30分00秒，细分辨率上则是 10时29分59秒——写死某一分钟
    # 会偶发失败。真正要验的是"列宽容得下最长倒计时且不被截断"。
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)

    class _FixedDateTime:
        @staticmethod
        def now(tz):
            return now

    monkeypatch.setattr(list_screen, "datetime", _FixedDateTime)
    st = replace(_st(), end_time=EndTime.exact(now + timedelta(hours=10, minutes=30), now))
    out = _draw_at(
        screens.render_list,
        80,
        snap=Snapshot(auctions=(st,)),
        rows=[st],
        cursor=0,
        page=0,
        page_size=6,
        tz=TZ,
        stale_minutes=30,
    )
    data_line = next(line for line in out.splitlines() if "❯" in line)
    # 倒计时精确到秒（钉死后固定为 10时30分00秒），且不会被截断成 10时30分…
    assert "10时30分00秒" in data_line, data_line
    assert "…" not in data_line
    assert len(out.splitlines()) <= 6


def test_cookie_summary_is_bounded_for_huge_cookie():
    """真实 Cookie 上千字符，摘要必须定长，不能把屏幕撑爆。"""
    huge = "MKT_CKID=" + "x" * 2000 + "; uid=12345; PHPSESSID=abcdef123456"
    head, note = screens.cookie_summary(huge)
    assert len(head) < 120, "摘要不能跟着 Cookie 长度线性变长"
    assert "条" in note and "字符" in note


def test_cookie_confirm_stays_within_one_screen_for_huge_cookie():
    huge = "; ".join(f"k{i}=" + "y" * 200 for i in range(11))
    out = _draw_at(
        screens.render_cookie_confirm,
        80,
        24,
        cookie=huge,
        source="cdp",
        reveal=False,
        cookie_path="data/cookies",
    )
    lines = out.splitlines()
    assert len(lines) <= 24, f"{len(lines)} 行超过屏高"


def test_login_paste_stays_short_while_typing():
    out = _draw_at(
        screens.render_login_paste,
        80,
        24,
        masked=screens.cookie_summary("k=" + "z" * 2000)[0],
        count=2005,
        busy="正在验证 Cookie…",
        notice=("登录失败：凭证无效",),
    )
    lines = out.splitlines()
    assert len(lines) <= 24
    assert "正在验证" in out, "验证中要有提示"
    assert "登录失败" in out, "错误要当场显示，而不是等回列表"


def test_login_wait_title_is_not_markup():
    """标题是外部字符串：含 [ ] 也不能触发 MarkupError。"""
    out = _draw_at(
        screens.render_login_wait,
        80,
        24,
        title="[/]",
        status="等待中",
        hints=("hint[/]",),
    )
    assert "等待中" in out


def test_detail_maps_enums_to_chinese_and_price_unit():
    from dataclasses import replace
    from datetime import UTC, datetime

    st = replace(
        _st(),
        user_query=UserQuery.UNFOLLOWED,
        door_state=DoorState.ENDED,
        latest_bid=12345.0,
    )
    out = _draw_at(
        screens.render_detail,
        80,
        24,
        st=st,
        snapshot=Snapshot(generated_at=datetime(2026, 10, 30, 1, 0, tzinfo=UTC)),
        observations=[("2026-10-29T12:00:00", "list", "x")],
        tz=TZ,
    )
    assert "已离开列表" in out and "已结束" in out
    assert "unfollowed" not in out and "ended" not in out
    assert "¥12,345" in out
    # 12:00 UTC = 20:00 Asia/Shanghai
    assert "20:00" in out
    assert "数据时间" in out


def test_detail_limits_observations_on_short_terminal():
    st = _st()
    obs = [("2026-10-29T12:00:00", "list", f"raw{i}") for i in range(5)]
    out = _draw_at(
        screens.render_detail, 80, 16, st=st, snapshot=Snapshot(), observations=obs, tz=TZ
    )
    lines = out.splitlines()
    assert len(lines) <= 16, f"{len(lines)} 行超过屏高"


def test_tiny_terminal_still_shows_item_name():
    """极窄屏可以去掉标记列，但必须保留拍品名——只有倒计时没名字等于没用。"""
    rows = _many_states(3)
    out = _draw_at(
        screens.render_list,
        30,
        snap=Snapshot(auctions=tuple(rows)),
        rows=rows,
        cursor=0,
        page=0,
        page_size=6,
        tz=TZ,
        stale_minutes=30,
    )
    assert "剩余" in out
    assert "ニコン" in out or "Nikon" in out
    assert all(len(line) <= 30 for line in out.splitlines())


# ------------------------------------------------------------------ Cookie 确认屏的"怎么拿到服务器上用"


def test_cookie_confirm_points_at_two_ways_to_export():
    """要同时告诉用户「复制文本」和「拷 db 文件」两条路，且说明写入的是数据库。"""
    out = _draw_at(
        screens.render_cookie_confirm,
        88,
        24,
        cookie="MKT_CKID=abc123; uid=1",
        source="浏览器",
        reveal=False,
        cookie_path="data/meruki.db",
    )
    assert "确认后写入数据库 data/meruki.db" in out
    assert "按 v 显示完整文本后复制" in out
    assert "拷这个 db 文件" in out or "把这个 db 文件拷" in out


def test_cookie_confirm_says_copy_when_already_revealed():
    """已经按 v 显示了全文：直接告诉用户选中复制，不用再让他按 v。"""
    out = _draw_at(
        screens.render_cookie_confirm,
        88,
        24,
        cookie="MKT_CKID=abc123; uid=1",
        source="浏览器",
        reveal=True,
        cookie_path="data/meruki.db",
    )
    assert "直接选中复制" in out


def test_cookie_confirm_suggests_db_copy_when_full_text_too_long():
    """全文放不下屏幕时：先尽量多显示，再如实说还有多少没显示。"""
    out = _draw_at(
        screens.render_cookie_confirm,
        88,
        24,
        cookie="MKT_CKID=" + "a" * 1800,
        source="浏览器",
        reveal=True,
        cookie_path="data/meruki.db",
    )
    assert "行没显示" in out, "放不下时必须如实说还有多少没显示，不能假装完整"
    assert "拷这个 db 文件过去" in out
    assert "按 v 显示后复制" not in out


def test_cookie_confirm_wraps_long_cookie_instead_of_truncating():
    """几百字符的 Cookie（真实常态）必须折行显示完整，不能截成一行加省略号。

    截断等于让用户拿不到能复制的文本——那和"不显示"没区别。
    """
    cookie = "kmgSessionNO03HxWh=" + "A" * 120 + "; PHPSESSID=" + "B" * 60 + "; uid=12345"
    out = _draw_at(
        screens.render_cookie_confirm,
        80,
        24,
        cookie=cookie,
        source="cdp",
        reveal=True,
        cookie_path="data/meruki.db",
    )
    shown = "".join(
        line.split("│", 1)[1].rsplit("│", 1)[0].strip() for line in out.splitlines() if "│" in line
    )
    assert cookie.replace(" ", "") in shown.replace(" ", ""), "全文必须完整出现"
    assert "还有" not in out, "放得下就不该报截断"
    assert "直接选中复制" in out


def test_cookie_confirm_never_exceeds_height_for_any_length():
    """任意长度 × 任意尺寸都不许溢出——prompt_toolkit 的 Window 不滚动。

    这里按真实路径模拟：``tui._render_body()`` 用 ``Console(height=终端行数-1)``。
    """
    import io

    for n in (0, 60, 222, 957, 2000):
        cookie = "k=" + "A" * max(1, n - 2)
        for cols, rows in ((40, 15), (60, 20), (80, 24), (120, 40)):
            buf = io.StringIO()
            console = Console(
                file=buf,
                force_terminal=False,
                width=cols,
                height=max(1, rows - 1),
            )
            screens.render_cookie_confirm(
                console,
                cookie=cookie,
                source="cdp",
                reveal=True,
                cookie_path="data/meruki.db",
            )
            lines = buf.getvalue().splitlines()
            assert len(lines) <= rows - 1, f"{cols}x{rows} len={len(cookie)} 溢出"


# ------------------------------------------------------------------ 方向键与登录措辞


def _footer(width: int = 100, *, page_count: int = 2) -> str:
    return screens.footer_text("list", page=0, page_count=page_count)


def test_footer_uses_n_and_p_for_paging():
    """翻页只认 n / p：左右箭头已经让给"切视图"了。"""
    out = _footer()
    assert "n/p 翻页 1/2" in out
    assert "↑↓ 选择" in out
    assert "←→" not in out, "左右箭头不再是翻页键"


def test_footer_hides_paging_when_there_is_only_one_page():
    """只有一页时"翻页 1/1"是纯噪音，还占着页脚最紧张的位置。"""
    assert "翻页" not in _footer(page_count=1)
    assert "翻页" in _footer(page_count=2)


def test_footer_no_longer_advertises_a_login_key():
    """登录挪进「配置 → 登录」后，列表页脚不该再占一格挂 ``l``。"""
    out = _footer()
    assert "l 换号" not in out
    assert "l 登录" not in out


def test_login_method_screen_retitles_when_logged_in():
    logged_in = _draw(screens.render_login_method, has_cookie=True)
    logged_out = _draw(screens.render_login_method, has_cookie=False)
    assert "切换账号" in logged_in
    assert "覆盖现有 Cookie" in logged_in
    assert "切换账号" not in logged_out
    assert "选择登录方式" in logged_out


def test_detail_price_flows_into_state():
    """详情拿到的当前价必须写进状态（列表接口不返回价格）。"""
    from datetime import UTC, datetime

    from meruki_tracker.core.domain.detail import DetailResult, apply_detail

    now = datetime.now(UTC)
    st = _st()
    st = __import__("dataclasses").replace(st, latest_bid=None)

    new, _ = apply_detail(
        st,
        DetailResult(
            failed=False,
            ended=False,
            end_at=now,
            current_bid=3300.0,
        ),
        now,
    )
    assert new.latest_bid == 3300.0


def test_detail_failure_keeps_previous_price():
    from datetime import UTC, datetime

    from meruki_tracker.core.domain.detail import DetailResult, apply_detail

    now = datetime.now(UTC)
    st = __import__("dataclasses").replace(_st(), latest_bid=2500.0)
    new, _ = apply_detail(st, DetailResult(failed=True), now)
    assert new.latest_bid == 2500.0


def test_price_visible_in_list_and_detail():
    """列表和详情都要能看到价格。"""
    from dataclasses import replace

    st = replace(_st(), latest_bid=12800.0)
    rows = [st]
    out_list = _draw_at(
        screens.render_list,
        100,
        snap=Snapshot(auctions=(st,)),
        rows=rows,
        cursor=0,
        page=0,
        page_size=6,
        tz=TZ,
        stale_minutes=30,
    )
    assert "12,800" in out_list, "列表要显示价格"

    out_detail = _draw_at(
        screens.render_detail, 100, 26, st=st, snapshot=Snapshot(), observations=[], tz=TZ
    )
    assert "12,800" in out_detail, "详情要显示价格"


def test_login_screen_states_browser_requirement():
    """没有 Chromium 系浏览器时，必须说明该方式不可用，而不是让用户点了才失败。"""
    out = _draw(screens.render_login_method, has_cookie=False, browser_available=False)
    assert "没找到" in out and "不可用" in out


def test_login_screen_mentions_browser_profile_cost():
    """浏览器登录会占空间，分发给别人用时必须提前说明。"""
    out = _draw(screens.render_login_method, has_cookie=False, browser_available=True)
    assert "20-30MB" in out or "空间" in out


def test_login_screen_marks_sms_as_recommended():
    """Safari / Firefox 用户用不了浏览器登录，手机验证码是通用主路径。"""
    out = _draw(screens.render_login_method, has_cookie=False, browser_available=True)
    assert "推荐" in out


def test_login_paste_mentions_safari_and_firefox():
    """粘贴方式不要求 Chromium，这一点要写清楚。"""
    out = _draw(screens.render_login_method, has_cookie=False, browser_available=False)
    assert "Safari" in out and "Firefox" in out


def test_list_footer_puts_detail_before_low_frequency_keys():
    """高频操作排前面：选择 → 切视图 → 详情；刷新/设置/退出放后面。"""
    footer = screens.footer_text("list", page=0, page_count=1)
    order = [footer.index(k) for k in ("↑↓", "Tab 切视图", "Enter 详情", "r 刷新", "q 退出")]
    assert order == sorted(order), f"页脚顺序不对: {footer}"


def test_list_footer_keeps_clean_key_when_space_is_tight():
    """宽度紧张时也不能把 x 清理挤掉——它是本视图独有的功能，别处没有。"""
    for width in (120, 108, 100, 90):
        footer = screens.footer_text("list", page=0, page_count=1, can_clean=True, width=width)
        assert "x 清理" in footer, f"{width} 列下丢了清理键: {footer}"


def test_list_footer_has_no_bulk_fetch_key():
    """页脚不许再出现 u——批量拉详情已经整个去掉了。

    真实反馈：「所有页面的 u 详情全部移除」。详情只在两处触发：进详情且没有
    详情时自动补一次、详情屏按 r 重拉当前这一条。
    """
    for width in (200, 120, 100, 80):
        footer = screens.footer_text("list", page=0, page_count=1, can_clean=True, width=width)
        assert "u" not in footer.replace("q 退出", ""), footer


def test_list_footer_advertises_settings_entry():
    """设置入口必须出现在页脚，否则用户根本找不到。"""
    footer = screens.footer_text("list", page=0, page_count=1)
    assert "s 设置" in footer, footer


def test_cookie_confirm_offers_enter_to_login():
    """确认登录用 Enter（最自然的"确认"键），不要只给一个 y。"""
    footer = screens.footer_text("cookie-confirm")
    assert "Enter 登录" in footer
    assert "y 使用它登录" not in footer


def test_detail_labels_align_by_display_width():
    """标签列必须按**显示列**对齐，不能按字符数。

    中文标签 2 字符占 4 列：「结束时间」是 4 字符(8 列)，「链接」是 2 字符(4 列)。
    用 f"{label:<7}" 按字符补齐，两者的值会差 2 列——看起来像表格错位。
    """
    from rich.cells import cell_len

    out = _draw(
        screens.render_detail,
        st=_st(),
        snapshot=Snapshot(),
        observations=[],
        tz=TZ,
    )
    starts = set()
    for line in out.splitlines():
        # 取面板内部文本（去掉边框与左侧空格）
        body = line.strip("│ ").rstrip()
        for label in ("拍品号", "链接", "状态", "剩余", "结束时间", "当前价", "数据时间"):
            if body.startswith(label):
                # 值起始列 = 标签（补齐后）的显示宽度
                value = body[len(label) :]
                starts.add(cell_len(body) - cell_len(value.lstrip()))
                break
    assert len(starts) == 1, f"标签列没对齐，值起始列有 {sorted(starts)} 种"


# ------------------------------------------------------------------ 设置屏滚动


@pytest.mark.parametrize("term_height", [24, 20, 16, 12, 8])
@pytest.mark.parametrize("cursor", [0, 6, 13])
def test_settings_never_overflows_body(term_height, cursor):
    """设置屏在小终端必须放得下：多画一行就把固定页脚顶出屏幕。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import OPTIONS

    body = max(1, term_height - 1)  # 主体区 = 终端行数 - 页脚 1 行
    buf = io.StringIO()
    console = Console(file=buf, width=90, height=body, force_terminal=False)
    screens.render_settings(console, options=OPTIONS, cursor=cursor, cfg=Config.defaults())
    lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(lines) <= body, f"终端 {term_height} 行渲染出 {len(lines)} 行"


@pytest.mark.parametrize("term_height", [24, 20, 16, 12, 8])
@pytest.mark.parametrize("cursor", [0, 5, 13])
def test_settings_keeps_cursor_visible(term_height, cursor):
    """滚动窗口的**唯一目的**：选中项永远看得见。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import OPTIONS

    body = max(1, term_height - 1)
    buf = io.StringIO()
    console = Console(file=buf, width=90, height=body, force_terminal=False)
    screens.render_settings(console, options=OPTIONS, cursor=cursor, cfg=Config.defaults())
    selected = [ln for ln in buf.getvalue().splitlines() if "❯" in ln]
    assert len(selected) == 1, f"选中项没渲染出来（cursor={cursor}）"
    # 渲染出来的第 cursor 项必须正好是它
    assert OPTIONS[cursor].label in selected[0]


def test_settings_says_how_many_are_hidden():
    """裁掉选项时必须说还有多少项，别让人以为配置就这么点。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import OPTIONS

    buf = io.StringIO()
    console = Console(file=buf, width=90, height=12, force_terminal=False)
    screens.render_settings(console, options=OPTIONS, cursor=13, cfg=Config.defaults())
    out = buf.getvalue()
    assert "上面还有" in out


# ------------------------------------------------------------------ 全屏不溢出（所有屏 × 所有尺寸）


def test_settings_narrow_width_does_not_wrap():
    """窄终端下选项行不能折行：折行会让"每项 1 行"的行预算失效、顶掉页脚。

    值右对齐的 padding 固定 22 列，40 列终端里「自动刷新间隔」+ padding 就已经
    超过内宽，rich 会折行——必须被截断成一行。
    """
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import OPTIONS

    for width in (40, 30):
        body = 29
        buf = io.StringIO()
        console = Console(file=buf, width=width, height=body, force_terminal=False)
        screens.render_settings(console, options=OPTIONS, cursor=0, cfg=Config.defaults())
        lines = buf.getvalue().rstrip("\n").splitlines()
        assert len(lines) <= body, f"{width} 列渲染出 {len(lines)} 行，超出 {body}"


@pytest.mark.parametrize(
    "width,height", [(120, 15), (100, 15), (80, 15), (60, 20), (40, 20), (30, 24)]
)
def test_login_method_fits_screen(width, height):
    """登录方式屏有 9 行选项 + 说明，矮终端曾整屏溢出到看不清 Esc。"""
    body = max(1, height - 1)  # 主体区扣掉固定页脚
    buf = io.StringIO()
    console = Console(file=buf, width=width, height=body, force_terminal=False)
    screens.render_login_method(console, has_cookie=False, browser_available=True)
    lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(lines) <= body, f"{width}x{height} 渲染出 {len(lines)} 行（容量 {body}）"
    # 选项本身永不被裁：三个方式 + Esc 必须都在
    out = buf.getvalue()
    for must in ("浏览器登录", "手机验证码登录", "手动粘贴 Cookie", "Esc"):
        assert must in out, f"{width}x{height} 丢了「{must}」"


@pytest.mark.parametrize("width,height", [(50, 15), (40, 15), (30, 15), (30, 10)])
def test_cookie_confirm_fits_screen(width, height):
    """确认屏曾被长 Cookie 撑出屏幕，而它承载"确认/取消"两个唯一出口。"""
    body = max(1, height - 1)
    buf = io.StringIO()
    console = Console(file=buf, width=width, height=body, force_terminal=False)
    screens.render_cookie_confirm(
        console,
        cookie="kmgSession=" + "a" * 1800,
        source="cdp",
        reveal=False,
        cookie_path="/tmp/cookies.db",
    )
    lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(lines) <= body, f"{width}x{height} 渲染出 {len(lines)} 行（容量 {body}）"
    # 脱敏摘要与"这是凭证"的警示不能被裁（窄屏下后者只能保住开头，会被省略号截断）
    out = buf.getvalue()
    assert "kmgSession=" in out
    assert "这是你的登录凭证" in out


@pytest.mark.parametrize("width,height", [(120, 8), (80, 6), (40, 5), (30, 4)])
def test_tiny_screens_never_exceed_height(width, height):
    """极端小终端：宁可少画内容，也不能把固定页脚顶出屏幕。"""
    body = max(1, height - 1)
    cases = [
        (screens.render_login_method, dict(has_cookie=False, browser_available=True)),
        (
            screens.render_login_paste,
            dict(masked="kmgSession=ab***(len=8)", count=8),
        ),
        (screens.render_sms_mobile, dict(mobile="13800138000")),
        (screens.render_sms_code, dict(mobile="13800138000", code="123456")),
        (screens.render_login_wait, dict(title="浏览器登录", status="正在等待")),
    ]
    for fn, kwargs in cases:
        buf = io.StringIO()
        console = Console(file=buf, width=width, height=body, force_terminal=False)
        fn(console, **kwargs)
        lines = buf.getvalue().rstrip("\n").splitlines()
        assert len(lines) <= body, f"{fn.__name__} 在 {width}x{height} 渲染出 {len(lines)} 行"


@pytest.mark.parametrize("width,height", [(80, 3), (40, 4), (120, 5)])
def test_list_survives_absurd_height(width, height):
    """列表屏在只放得下几行的终端里也不能溢出。"""
    body = max(1, height - 1)
    buf = io.StringIO()
    console = Console(file=buf, width=width, height=body, force_terminal=False)
    screens.render_list(
        console,
        snap=Snapshot(auctions=(_st(),)),
        rows=[_st()],
        cursor=0,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
    )
    lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(lines) <= body, f"{width}x{height} 渲染出 {len(lines)} 行（容量 {body}）"


def test_panel_drops_optional_lines_before_required_ones():
    """面板超高的取舍顺序：先丢说明，绝不丢选项本身。"""
    lines = [
        (Text("必显 A"), 0),
        (Text("说明 1"), 2),
        ("", 1),
        (Text("必显 B"), 0),
        (Text("说明 2"), 2),
    ]
    kept = [t.plain if isinstance(t, Text) else t for t in screens._shrink(lines, 3)]
    assert "必显 A" in kept and "必显 B" in kept
    assert len(kept) == 3


def test_panel_truncates_long_line_instead_of_wrapping():
    """超宽行必须截断：折行会多吃一行，行预算就算错了。"""
    buf = io.StringIO()
    console = Console(file=buf, width=30, height=10, force_terminal=False)
    screens.render_login_wait(console, title="T", status="状" * 100)
    lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(lines) <= 9


@pytest.mark.parametrize("width,height", [(120, 6), (80, 8), (60, 10), (40, 5)])
def test_clean_confirm_fits_screen(width, height):
    """清理确认屏：条目再多也不能顶掉页脚（页脚是唯一"确认/取消"入口）。"""
    body = max(1, height - 1)
    targets = [_st(f"拍品{i}", asin=f"A{i}") for i in range(40)]
    buf = io.StringIO()
    console = Console(file=buf, width=width, height=body, force_terminal=False)
    screens.render_clean_confirm(console, view=ListView.ENDED, targets=targets, tz=TZ)
    lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(lines) <= body, f"{width}x{height} 渲染出 {len(lines)} 行（容量 {body}）"


def test_clean_confirm_keeps_key_warning():
    """删数据前必须留住「只删本地记录，不等于取关」。"""
    targets = [_st(f"拍品{i}", asin=f"A{i}") for i in range(40)]
    buf = io.StringIO()
    console = Console(file=buf, width=60, height=8, force_terminal=False)
    screens.render_clean_confirm(console, view=ListView.ENDED, targets=targets, tz=TZ)
    out = buf.getvalue()
    assert "只删本地记录" in out
    # 折叠时要说清有多少条没列出来（窗口太小放不下时用另一句话说明）
    assert "还有" in out or "列不下" in out


def test_invalid_clean_confirm_warns_it_will_come_back():
    """「无法确认」不是终态：确认屏必须说清清理不是永久删除。

    拍品还在「进行中」列表里的话，下一轮刷新会重新发现它并从零重试——
    这句话不能省，否则用户以为"清了就再也不会烦我"。
    """
    bad = replace(_st("详情拉不到"), door_state=DoorState.INVALID, detail_failures=3)
    out = _draw(screens.render_clean_confirm, view=ListView.INVALID, targets=[bad], tz=TZ)
    assert "清理 1 个无法确认的拍品" in out
    assert "下轮刷新会重新出现" in out
    assert "从零重试" in out
    # 常规两说明也不能因为多了一行而丢
    assert "只删本地记录" in out


@pytest.mark.parametrize("width,height", [(120, 6), (80, 8), (60, 10), (40, 5)])
def test_invalid_clean_confirm_fits_screen(width, height):
    """多一行说明也不能顶掉页脚；条目多时照旧折叠。"""
    body = max(1, height - 1)
    targets = [
        replace(_st(f"拍品{i}", asin=f"A{i}"), door_state=DoorState.INVALID, detail_failures=3)
        for i in range(40)
    ]
    buf = io.StringIO()
    console = Console(file=buf, width=width, height=body, force_terminal=False)
    screens.render_clean_confirm(console, view=ListView.INVALID, targets=targets, tz=TZ)
    lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(lines) <= body, f"{width}x{height} 渲染出 {len(lines)} 行（容量 {body}）"


def test_view_bar_shows_four_tabs():
    """新增「无法确认」后视图栏要有四档、且只有选中项反显。"""
    from meruki_tracker.tui.views import counts

    rows = [
        _st("A1"),
        replace(_st("A2"), door_state=DoorState.INVALID),
    ]
    c = counts(rows)
    text = screens.view_bar(ListView.INVALID, c)
    plain = text.plain
    assert "无法确认 1" in plain
    assert "关注中 1" in plain
    assert "已结束 0" in plain
    assert "全部 2" in plain
    assert plain.count("·") == 3, f"四档之间该有 3 个分隔符：{plain!r}"
    item_styles = [
        span.style for span in text.spans if text.plain[span.start : span.end].strip() != "·"
    ]
    assert item_styles.count("reverse") == 1


def test_every_screen_uses_the_same_time_format():
    """同一个时刻在不同屏上不能长成三个样子（曾经 %m-%d / %H:%M:%S / %m-%d %H:%M 混用）。"""
    now = datetime(2026, 11, 2, 12, 34, 56, tzinfo=UTC)
    a = _st("拍品", asin="A1", end_time=EndTime.exact(now, now))
    snap = Snapshot(
        generated_at=now, last_attempt=LastAttempt(at=now, ok=True, error=""), auctions=(a,)
    )

    outs = [
        _draw(
            screens.render_list,
            snap=snap,
            rows=[a],
            cursor=0,
            page=0,
            page_size=5,
            tz=TZ,
            stale_minutes=30,
        ),
        _draw(screens.render_detail, st=a, snapshot=snap, observations=[], tz=TZ),
        _draw(screens.render_clean_confirm, view=ListView.ENDED, targets=[a], tz=TZ),
    ]
    for out in outs:
        # 统一格式：19 列、带年份
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", out), out
        # 不带年份的短日期（11-02 20:34）一律不许出现
        assert not re.search(r"(?<!\d)\d{2}-\d{2} \d{2}:\d{2}", out.replace("2026-11-02", "")), out


def test_setting_edit_shows_the_selectable_units():
    """带单位的项要把可选单位都列出来，否则用户不知道 Tab 能换成什么。"""
    out = _draw(
        lambda c: screens.render_setting_edit(
            c,
            label="自动刷新间隔",
            help_text="后台多久自动采集一次",
            current="3 分钟",
            text="10",
            unit="分钟",
            units=("秒", "分钟", "小时", "天"),
        ),
    )
    for unit in ("秒", "分钟", "小时", "天"):
        assert unit in out
    assert "Tab 切换" in out
    assert "10" in out


def test_setting_edit_hides_the_switch_hint_for_fixed_units():
    """只有一个单位的项不该假装能切。"""
    out = _draw(
        lambda c: screens.render_setting_edit(
            c,
            label="失败重试次数",
            help_text="网络类失败自动重试几次",
            current="1 次",
            text="2",
            unit="次",
            units=(),
        ),
    )
    assert "次" in out
    assert "Tab 切换" not in out


@pytest.mark.parametrize("width,height", [(120, 6), (60, 8), (40, 5)])
def test_setting_edit_fits_screen(width, height):
    """编辑屏必须永远看得见「当前值 / 新值」，否则等于盲改。"""
    body = max(1, height - 1)
    buf = io.StringIO()
    console = Console(file=buf, width=width, height=body, force_terminal=False)
    screens.render_setting_edit(
        console,
        label="自动刷新间隔",
        help_text="设为 0 表示关闭自动刷新；建议 3-15 分钟" * 3,
        current="3 分钟",
        text="120",
        hint="Enter 保存 · Esc 取消",
    )
    lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(lines) <= body, f"{width}x{height} 渲染出 {len(lines)} 行（容量 {body}）"
    out = buf.getvalue()
    assert "当前值" in out


def test_shrink_prefers_more_optional_lines_first():
    """同级内从后往前丢：面板尾部多是补充说明，先丢不伤信息。"""
    lines = [
        (Text("必显"), 0),
        (Text("说明 A"), 1),
        (Text("说明 B"), 1),
    ]
    kept = [t.plain for t in screens._shrink(lines, 2)]
    assert kept == ["必显", "说明 A"]


def test_remaining_column_shrinks_to_its_content():
    """剩余列要贴着内容定宽，不能固定 17 列留一大片空白。

    固定 17 列时，值大多只有 8~12 列，右对齐后表头「剩余」在列首、数值在列尾，
    中间空一大截，看着像两列东西。真实反馈：「剩余这一列的排版有点问题」。
    """
    from datetime import timedelta

    from rich.cells import cell_len

    now = datetime.now(UTC)

    def mk(asin: str, minutes: int) -> AuctionState:
        return AuctionState(
            asin=asin,
            name=f"拍品{asin}",
            url=f"https://x/{asin}",
            user_query=UserQuery.FOLLOWING,
            door_state=DoorState.ACTIVE,
            end_time=EndTime.exact(now + timedelta(minutes=minutes), now),
            latest_bid=None,
        )

    rows = [mk("A", 3), mk("B", 120)]  # 02分59秒 / 1时59分59秒，最宽 11
    snap = Snapshot(
        generated_at=now,
        last_attempt=LastAttempt(at=now, ok=True, error=""),
        account=Account(nickname="t", cookie_state="valid"),
        auctions=tuple(rows),
    )
    out = _draw_at(
        screens.render_list,
        100,
        snap=snap,
        rows=rows,
        cursor=0,
        page=0,
        page_size=5,
        tz=UTC,
        stale_minutes=30,
    )
    header = next(line for line in out.splitlines() if "剩余" in line)
    # 列宽要贴着最长值（"1时59分59秒" = 11 列 + padding 2），不是固定的 17。
    # 阈值取 12 才有区分度：固定 17 列时实测 gap = 16。
    gap = header.index("结束时间") - header.index("剩余")
    assert gap <= cell_len("1时59分59秒") + 1, (
        f"剩余列没按内容收窄（表头到下一列 {gap} 列）: {header!r}"
    )


def test_detail_never_exceeds_the_terminal_height():
    """详情屏以前完全没有高度预算，窄/矮终端下观测表被整块截掉。

    prompt_toolkit 的 Window 不滚动，超出的行静默消失——用户既看不到观测记录，
    也看不到"还有更多"的提示。真实踩过 40x12 渲染出 21 行。
    """
    from datetime import UTC, datetime, timedelta

    from meruki_tracker.core.adapters.store.snapshot import Account, LastAttempt
    from meruki_tracker.core.domain.models import DoorState, EndTime, UserQuery

    now = datetime.now(UTC)
    long_name = (
        "Nikon F3 本体 ブラック ボディ のみ ジャンク カメラ フィルム 一眼レフ "
        "昭和 レトロ コレクション ジャンク品 当時物"
    )
    st = AuctionState(
        asin="l1000000003",
        name=long_name,
        url="https://auctions.yahoo.co.jp/jp/auction/l1000000003",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ENDED,
        end_time=EndTime.exact(now - timedelta(days=1), now),
        latest_bid=23800.0,
        last_seen_at=now,
        unfollowed_at=now,
        concluded_at=now,
        detail_failures=2,
    )
    snap = Snapshot(
        generated_at=now,
        last_attempt=LastAttempt(now, True, ""),
        account=Account(),
        auctions=(st,),
    )
    obs = [("2026-10-29T12:00:00", "list", "x")] * 5

    for width in (160, 120, 100, 80, 60, 50, 40, 30, 24, 20):
        for term_height in (40, 30, 24, 20, 16, 12, 10, 8, 6, 5, 4, 3):
            body = max(1, term_height - 1)
            buf = io.StringIO()
            console = Console(file=buf, width=width, height=body, force_terminal=False)
            screens.render_detail(console, st=st, snapshot=snap, observations=obs, tz=TZ)
            got = len(buf.getvalue().splitlines())
            assert got <= body, f"{width}x{term_height}: 渲染 {got} 行，可用 {body}"


def test_detail_keeps_the_essentials_when_cramped():
    """再矮也要保住"还剩多久/什么时候结束/多少钱"——那是这屏存在的理由。"""
    from datetime import UTC, datetime, timedelta

    from meruki_tracker.core.adapters.store.snapshot import Account, LastAttempt
    from meruki_tracker.core.domain.models import DoorState, EndTime, UserQuery

    now = datetime.now(UTC)
    st = AuctionState(
        asin="A1",
        name="尼康 F3",
        url="https://auctions.yahoo.co.jp/jp/auction/A1",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(now + timedelta(hours=2), now),
        latest_bid=12500.0,
        last_seen_at=now,
    )
    buf = io.StringIO()
    console = Console(file=buf, width=60, height=11, force_terminal=False)
    screens.render_detail(
        console,
        st=st,
        snapshot=Snapshot(
            generated_at=now, last_attempt=LastAttempt(now, True, ""), account=Account()
        ),
        observations=[],
        tz=TZ,
    )
    out = buf.getvalue()
    assert "剩余" in out
    assert "结束时间" in out
    assert "当前价" in out
    # 抄送信息可以牺牲
    assert "拍品号" not in out or "链接" not in out, "矮终端下该先丢抄送信息，而不是关键字段"


def test_footer_always_keeps_a_way_out():
    """再窄也要留一条退出路径。

    rich 的 ellipsis 砍行尾，而 `q 退出` 恰好排最后——不主动降级的话，22 列时
    会显示 `↑↓ · Tab · Enter · q  …`，16 列时连 q 都没了。真实踩过。
    """
    from rich.text import Text

    for width in (120, 60, 40, 30, 24, 22, 20, 16, 14, 12, 10, 8, 6, 4, 2, 1):
        text = screens.footer_text("list", page=0, page_count=1, can_clean=True, width=width)
        buf = io.StringIO()
        console = Console(file=buf, width=width, height=1, force_terminal=False)
        console.print(
            Text(text, style="reverse"), no_wrap=True, overflow="ellipsis", highlight=False
        )
        shown = buf.getvalue().rstrip("\n")
        assert "q" in shown, f"{width} 列下连 q 都没了: {shown!r}"
        assert "…" not in shown, f"{width} 列下页脚被截断（退出键可能被吃掉）: {shown!r}"


def test_refresh_overlay_never_exceeds_the_terminal_height():
    """刷新遮罩以前在矮终端下必溢出：`pad` 没扣边框、`trailing` 把超发掩盖成 0。"""
    payloads = (
        "刷新中 · 进行中 第 1 页 · 已拉 10 条",
        "刷新中（自动）· " + "很长的进度文本" * 20,
        "",
    )
    for width in (160, 100, 60, 40, 30, 20):
        for term_height in (50, 30, 20, 12, 9, 8, 6, 5, 4, 3, 2, 1):
            for busy in payloads:
                for page_count in (1, 7):
                    body = max(1, term_height - 1)
                    buf = io.StringIO()
                    console = Console(file=buf, width=width, height=body, force_terminal=False)
                    screens.render_refreshing(console, busy=busy, page_count=page_count)
                    got = len(buf.getvalue().splitlines())
                    assert got <= body, (
                        f"{width}x{term_height} pc={page_count}: 渲染 {got} 行，可用 {body}"
                    )


def test_refresh_overlay_still_says_it_is_refreshing():
    """压缩不能把"在刷新"这个结论压没了。"""
    buf = io.StringIO()
    console = Console(file=buf, width=80, height=20, force_terminal=False)
    screens.render_refreshing(console, busy="刷新中 · 进行中 第 1 页 · 已拉 10 条", page_count=7)
    out = buf.getvalue()
    assert "刷新中" in out
    assert "第 1 页" in out


# ------------------------------------------------------ 当前价 / 一口价分开显示


def _list_draw(rows, *, width=120, height=20, caption=None):
    """画列表屏。``_draw`` 的宽高是位置参数，这里包一层省得每次写全 kwargs。"""
    caption = caption or Snapshot(auctions=tuple(rows))
    return _draw_at(
        screens.render_list,
        width,
        height,
        snap=caption,
        rows=list(rows),
        cursor=0,
        page=0,
        page_size=10,
        tz=TZ,
        stale_minutes=30,
    )


def test_list_shows_current_price_and_buy_now_in_separate_columns():
    """两个价格各占一列。真实反馈：「你分开两个字段展示吧，就当前价和一口价」。"""
    st = replace(_st(), latest_bid=1300.0, buy_now_price=3000.0)
    out = _list_draw([st])
    assert "当前价" in out and "一口价" in out
    assert "¥1,300" in out
    assert "¥3,000" in out


def test_fixed_price_item_shows_dash_for_current_price():
    """一口价商品没有拍卖价：当前价 —，一口价显示真实金额。"""
    st = replace(_st(), latest_bid=None, buy_now_price=1980.0)
    out = _list_draw([st])
    assert "¥1,980" in out
    assert "—" in out


def test_narrow_list_drops_the_buy_now_column_first():
    """窄屏先砍一口价，保留当前价——当前价是主信息。"""
    st = replace(_st(), latest_bid=1300.0, buy_now_price=3000.0)
    out = _list_draw([st], width=80)
    assert "当前价" in out
    assert "一口价" not in out
    assert "¥1,300" in out


def test_detail_shows_both_prices():
    st = replace(_st(), latest_bid=1300.0, buy_now_price=3000.0)
    out = _draw(screens.render_detail, st=st, snapshot=Snapshot(), observations=[], tz=TZ)
    assert "当前价" in out and "一口价" in out
    assert "¥1,300" in out and "¥3,000" in out


def test_detail_fixed_price_item_shows_dash_then_amount():
    st = replace(_st(), latest_bid=None, buy_now_price=1980.0)
    out = _draw(screens.render_detail, st=st, snapshot=Snapshot(), observations=[], tz=TZ)
    assert "¥1,980" in out
    assert "—" in out


@pytest.mark.parametrize("width", [160, 100, 60, 40, 30, 20])
@pytest.mark.parametrize("height", [50, 24, 12, 8, 4, 3])
def test_description_screen_never_exceeds_the_terminal_height(width: int, height: int):
    """描述全文屏在任何尺寸下都不许顶掉页脚（那是唯一的退路）。

    真实踩过：极矮终端里"标题 + 忙碌行 + 正文 + 滚动提示"一起超出去，
    ``room = max(1, height - 3)`` 强行给正文留 1 行，height=3 时渲染 4 行。
    """

    from meruki_tracker.tui import screens as scr

    body = max(1, height - 1)  # 页脚固定占 1 行
    for text in ("", "短", "\n".join(f"第{i}行" for i in range(50))):
        for busy in ("", "翻译中…"):
            buf = io.StringIO()
            console = Console(
                file=buf, width=width, height=body, force_terminal=False, no_color=True
            )
            scr.render_detail_text(
                console,
                st=_st(),
                text=text,
                translated=False,
                scroll=0,
                busy=busy,
            )
            assert len(buf.getvalue().splitlines()) <= body, f"{width}x{height} 溢出"


def test_setting_edit_masks_secret_input():
    """密码/令牌：输入中也不回显明文，只看到圆点。"""
    out = _draw(
        screens.render_setting_edit,
        label="SMTP 密码",
        help_text="填的是「授权码」",
        current="（已设置）",
        text="hunter2",
        secret=True,
    )
    assert "hunter2" not in out, "明文密码不该出现在屏幕上"
    assert "•" * len("hunter2") in out


def test_setting_edit_shows_plain_text_when_not_secret():
    out = _draw(
        screens.render_setting_edit,
        label="收件人",
        help_text="多个用逗号分隔",
        current="（未设置）",
        text="a@b.c",
    )
    assert "a@b.c" in out


# ---------------------------------------------------------------- 扫码挂载


def _mount_console(width: int, height: int) -> tuple[io.StringIO, Console]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, height=height)
    return buf, console


def test_qr_matrix_has_a_quiet_zone():
    """静区必须是白的：少了它扫码枪常常认不出边缘。"""
    matrix = screens.qr_matrix("hello")
    size = len(matrix)
    assert all(len(row) == size for row in matrix)
    zone = screens.QR_QUIET_ZONE
    assert not any(matrix[0])
    assert not any(matrix[-1])
    assert not any(row[0] for row in matrix)
    assert zone <= len(matrix)


def test_qr_lines_are_half_the_matrix_height():
    matrix = screens.qr_matrix("hello")
    lines = screens.qr_lines("hello")
    assert len(lines) == (len(matrix) + 1) // 2
    assert all(len(line.plain) == len(matrix[0]) for line in lines)


def test_qr_uses_only_block_characters():
    """半块字符画：只允许空白 / ▀ / ▄ / █ 四种，否则上下半格会画错。"""
    payload = "http://weixin.qq.com/x/4dmHZZMtoLbHoLZwd1wE"
    for line in screens.qr_lines(payload):
        assert set(line.plain) <= {" ", "▀", "▄", "█"}


def test_qr_is_black_on_white_so_any_theme_works():
    """颜色必须显式给：只靠前景色在浅色主题下会整片反相。"""
    lines = screens.qr_lines("hello")
    styles = {span.style for line in lines for span in line.spans}
    assert styles == {"black on white"}


def test_render_mount_shows_status_and_hint():
    from meruki_tracker.core.app.qrlogin import QrChallenge

    buf, console = _mount_console(100, 30)
    screens.render_mount(
        console,
        challenge=QrChallenge(kind="wechat", payload="http://x/1", hint="用微信扫码"),
        state="pending",
        label="微信",
    )
    out = buf.getvalue()
    assert "挂载微信机器人" in out
    assert "等待扫码" in out
    assert "用微信扫码" in out


def test_render_mount_says_how_big_a_window_it_needs():
    """二维码放不下时要说清楚，绝不画半个让人扫不上。"""
    from meruki_tracker.core.app.qrlogin import QrChallenge

    buf, console = _mount_console(40, 12)
    screens.render_mount(
        console,
        challenge=QrChallenge(kind="wechat", payload="http://x/1", hint=""),
        state="pending",
    )
    out = buf.getvalue()
    assert "放得下" in out or "只有" in out


def test_render_mount_without_a_payload_says_so():
    """理论上不该发生（微信一定给码），但真没码时要说清楚，别画空白框。"""
    from meruki_tracker.core.app.qrlogin import QrChallenge

    buf, console = _mount_console(100, 24)
    screens.render_mount(
        console,
        challenge=QrChallenge(kind="", payload="", hint="去手机上看"),
        state="failed",
        message="没拿到码",
    )
    out = buf.getvalue()
    assert "没有二维码可扫" in out
    assert "没拿到码" in out


@pytest.mark.parametrize("width,height", [(120, 40), (100, 30), (80, 24), (60, 20), (40, 10)])
def test_render_mount_never_exceeds_height(width, height):
    from meruki_tracker.core.app.qrlogin import QrChallenge

    buf, console = _mount_console(width, height)
    screens.render_mount(
        console,
        challenge=QrChallenge(kind="wechat", payload="http://weixin.qq.com/x/XYZ", hint="扫码"),
        state="scanned",
        message="已扫码，等待手机上确认",
        notice=("提示一", "提示二"),
    )
    assert len(buf.getvalue().splitlines()) <= height


def test_qr_rendering_is_lossless():
    """把画出来的字符反解回矩阵，必须和编码器给的逐位相同。

    这是离线能给出的最强证据：终端里每个字符格覆盖上下两个模块，只要映射
    是双射，屏幕上看到的就**就是**那个二维码，不是"大概像"。
    """
    payload = "http://weixin.qq.com/x/4dmHZZMtoLbHoLZwd1wE"
    matrix = screens.qr_matrix(payload)
    reverse = {" ": (False, False), "█": (True, True), "▀": (True, False), "▄": (False, True)}
    rebuilt: list[list[bool]] = []
    for line in screens.qr_lines(payload):
        top = [reverse[ch][0] for ch in line.plain]
        bottom = [reverse[ch][1] for ch in line.plain]
        rebuilt.append(top)
        rebuilt.append(bottom)
    assert rebuilt[: len(matrix)] == matrix


def test_qr_has_finder_patterns():
    """左上角必须是一个 7x7 的回字形定位块——扫不出来时先看这个。"""
    matrix = screens.qr_matrix("hello")
    # 静区之后的第一行定位块：7 个黑
    row = matrix[screens.QR_QUIET_ZONE]
    assert row[screens.QR_QUIET_ZONE : screens.QR_QUIET_ZONE + 7] == [True] * 7
    # 第二行是"黑白白黑黑黑白"
    inner = matrix[screens.QR_QUIET_ZONE + 1]
    assert inner[screens.QR_QUIET_ZONE : screens.QR_QUIET_ZONE + 7] == [
        True,
        False,
        False,
        False,
        False,
        False,
        True,
    ]


# ---------------------------------------------------------------- 配置分组条


def test_tab_bar_shows_every_label_and_the_current_one():
    text = screens.tab_bar(["基本", "通知", "登录"], 1)
    assert text.plain.count("·") == 2
    assert [s.style for s in text.spans].count("reverse") == 1
    assert "bold" not in "".join(s.style for s in text.spans)


def test_tab_bar_falls_back_to_the_current_label_when_too_narrow():
    text = screens.tab_bar(["基本", "通知", "登录"], 2, compact=True)
    assert "登录" in text.plain
    assert "基本" not in text.plain


def test_tab_bar_with_no_labels_draws_nothing():
    assert screens.tab_bar([], 0).plain == ""


@pytest.mark.parametrize("width", [120, 100, 80, 70, 60, 50, 40, 36, 30, 24, 20, 16])
def test_list_footer_always_advertises_the_settings_entry(width: int):
    """``s`` 是进配置页的唯一入口（外层大分页已拆掉），任何宽度都不能裁掉。

    曾经它和 ``t 翻译`` 一样被当成"低频键"，宽度一紧就最先消失——那样窄屏
    用户永远发现不了配置页。
    """
    text = screens.footer_text("list", page=0, page_count=2, can_clean=True, width=width)
    assert "s 设置" in text, f"{width} 列下看不到 s 设置：{text}"
    assert "q" in text


@pytest.mark.parametrize("width", [120, 80, 60, 40])
def test_subscreen_footers_no_longer_mention_the_outer_tab(width: int):
    """外层大分页拆掉后，子屏页脚不该再宣传 Tab 换页。"""
    for screen in ("settings", "mount"):
        text = screens.footer_text(screen, width=width)
        assert "Tab 换页" not in text, f"{screen} 的页脚还留着已删掉的分页：{text}"


# ---------------------------------------------------------------- 页脚文案准确性


def test_view_bar_highlights_without_changing_the_font_weight():
    """视图栏的标签长度本来就不同（「全部」两字，「关注中」三字），

    所以这里抓的不是宽度一致，而是**只有一种强调方式**：反显。以前选中项
    又加粗又反显、未选中压暗，切换时看起来像选中项"变大了一圈"。
    """
    from meruki_tracker.tui.views import ListView

    counts = {ListView.FOLLOWING: 3, ListView.ENDED: 0, ListView.ALL: 3}
    text = screens.view_bar(ListView.FOLLOWING, counts)
    item_styles = [
        span.style for span in text.spans if text.plain[span.start : span.end].strip() != "·"
    ]
    assert item_styles.count("reverse") == 1
    assert "bold" not in "".join(item_styles), "加粗会让选中项看起来更大"
    assert all(style == "" for style in item_styles if style != "reverse"), (
        f"未选中的视图项不该压暗/加粗：{item_styles}"
    )


def test_footer_says_back_to_list_where_q_does_not_quit():
    """挂载屏 / 配置页按 q 是返回上一页，不是退出程序——页脚不能说"q 退出"。"""
    for screen in ("mount", "settings"):
        text = screens.footer_text(screen, width=100)
        assert "退出" not in text, f"{screen} 的 q 其实是返回：{text}"
        assert "返回" in text
        assert "Esc" in text, f"{screen} 没写 Esc：{text}"


def test_detail_footer_distinguishes_its_refresh_from_the_list_one():
    text = screens.footer_text("detail", width=100)
    assert "刷新这一条" in text, text


def test_setting_edit_footer_mentions_units_only_when_available():
    without = screens.footer_text("setting-edit", width=100)
    assert "切单位" not in without
    with_units = screens.footer_text("setting-edit", edit_units=True, width=100)
    assert "Tab 切单位" in with_units


def test_list_footer_uses_the_same_words_as_the_bars():
    """页脚说的 Tab 要和视图栏的实际行为一致；进配置页只有 s 这一句话。"""
    text = screens.footer_text("list", width=200, page=0, page_count=2)
    assert "Tab 切视图" in text
    assert "s 设置" in text
    assert "Tab 换页" not in text


def test_compact_bars_have_exactly_one_space_after_the_marker():
    """``◧`` 后面只能有一个空格——标签本身已经带了前导空格。"""
    from meruki_tracker.tui.views import ListView

    for text in (
        screens.tab_bar(["基本", "通知"], 0, compact=True),
        screens.view_bar(ListView.FOLLOWING, {ListView.FOLLOWING: 6}, compact=True),
    ):
        assert text.plain.startswith("◧ "), text.plain
        assert not text.plain.startswith("◧  "), f"多了一个空格：{text.plain!r}"


EVERY_SCREEN = [
    "list",
    "clean-confirm",
    "settings",
    "setting-edit",
    "detail",
    "mount",
    "detail-desc",
    "login-method",
    "login-paste",
    "login-sms-mobile",
    "login-sms-code",
    "login-wait",
    "cookie-confirm",
]


@pytest.mark.parametrize("screen", EVERY_SCREEN)
@pytest.mark.parametrize("width", [140, 120, 80, 60, 44, 40, 30, 24, 20, 14, 10])
def test_every_footer_fits_the_width_it_is_given(screen, width):
    """非列表屏以前直接把整串丢给 rich 的 ellipsis，窄屏会把「返回列表」切掉。

    页脚的尾巴是"怎么退出"，被砍掉比少一个快捷键难受得多，所以每一屏都必须
    自己按宽度降级，保证渲染出来的宽度真的不超过给定宽度。
    """
    from rich.cells import cell_len

    text = screens.footer_text(screen, width=width, page=0, page_count=3)
    assert cell_len(text) <= width, f"{screen}@{width} 溢出：{cell_len(text)} {text!r}"


@pytest.mark.parametrize("screen", EVERY_SCREEN)
def test_every_footer_keeps_a_way_out_when_very_narrow(screen):
    """再窄也要留下 Esc 或 q——否则用户不知道按什么能离开这一屏。"""
    text = screens.footer_text(screen, width=20, page=0, page_count=3)
    assert "Esc" in text or "q" in text, f"{screen}: {text!r}"


def test_footer_prefers_dropping_middle_hints_over_the_exit():
    """降级顺序：先丢中间那些"锦上添花"的按键，退出提示必须在。"""
    wide = screens.footer_text("settings", width=100)
    narrow = screens.footer_text("settings", width=34)
    assert "Tab 切分组" in wide
    assert "Tab 切分组" not in narrow
    assert "返回列表" in narrow


# ------------------------------------------------------------------ 提醒点竖排


def _point_options(cfg):
    """只取那条一组数的配置项（``notify.points_minutes``）。

    详情那一组同形的配置已随"每轮全拉详情"删掉（2026-11-15），现在只剩提醒点。
    """
    from meruki_tracker.core.config_options import OPTIONS, option_visible

    return [o for o in OPTIONS if o.kind == "minutes_list" and option_visible(o, cfg)]


def _points_cfg(points):
    from meruki_tracker.core.config import Config

    base = Config.defaults()
    return replace(base, notify=replace(base.notify, points_minutes=points))


def test_recheck_points_are_listed_one_per_line():
    """用户 m21656：一组数要竖排成 ``- 6 小时`` 列表，不是一行横排。

    横排 ``6 小时 / 3 小时 / 1 小时 / 15 分钟 / 6 分钟`` 在窄终端会被截掉尾巴，
    而被截掉的恰好是最靠近结束、最该看见的那个点。
    """
    cfg = _points_cfg((6.0, 15.0, 60.0, 180.0, 360.0))
    buf = io.StringIO()
    console = Console(file=buf, width=90, height=40, force_terminal=False)
    screens.render_settings(console, options=_point_options(cfg), cursor=0, cfg=cfg)
    body = [ln.strip() for ln in buf.getvalue().splitlines()]
    bullets = [ln for ln in body if ln.startswith("- ")]
    assert bullets == ["- 6 小时", "- 3 小时", "- 1 小时", "- 15 分钟", "- 6 分钟"]
    # 标题行与明细行是分开的两行，且标题后面不再跟一长串
    title = [ln for ln in body if "结束前复查点" in ln]
    assert len(title) == 1 and "/" not in title[0]


def test_recheck_points_are_sorted_far_to_near_and_deduped():
    """顺序从远到近、重复合并——库里/手填的顺序都不能左右显示。"""
    cfg = _points_cfg((15.0, 360.0, 15.0, 60.0))
    buf = io.StringIO()
    console = Console(file=buf, width=90, height=40, force_terminal=False)
    screens.render_settings(console, options=_point_options(cfg), cursor=0, cfg=cfg)
    bullets = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip().startswith("- ")]
    assert bullets == ["- 6 小时", "- 1 小时", "- 15 分钟"]


def test_settings_row_budget_counts_point_lines():
    """行预算必须算上竖排明细：少算一行就把固定页脚顶出屏幕。"""
    cfg = _points_cfg(tuple(range(5, 65, 5)))
    options = _point_options(cfg)
    for height in (30, 24, 18, 14, 10, 6, 4):
        buf = io.StringIO()
        console = Console(file=buf, width=90, height=height, force_terminal=False)
        screens.render_settings(console, options=options, cursor=0, cfg=cfg)
        got = buf.getvalue().rstrip("\n").splitlines()
        assert len(got) <= height, f"height={height} 渲染出 {len(got)} 行"


def test_tiny_terminal_still_shows_the_first_points():
    """连一整项都放不下时，也要把头几个点写出来——只剩标题等于什么都没说。"""
    cfg = _points_cfg((6.0, 15.0, 60.0, 180.0, 360.0))
    options = _point_options(cfg)
    buf = io.StringIO()
    console = Console(file=buf, width=80, height=4, force_terminal=False)
    screens.render_settings(console, options=options, cursor=0, cfg=cfg)
    got = buf.getvalue().rstrip("\n").splitlines()
    assert len(got) <= 4, got
    joined = "\n".join(got)
    assert "结束前复查点" in joined
    assert "- 6 小时" in joined


def test_editing_points_shows_natural_units_for_the_current_value():
    """编辑屏的"当前值"要写 ``6 小时 / 1 小时 / 15 分钟``，不是 ``360 分钟 / 60 分钟``。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import OPTIONS_BY_KEY

    opt = OPTIONS_BY_KEY["notify.points_minutes"]
    cfg = Config.defaults()
    assert opt.format(opt.read(cfg)) == "6 小时 / 1 小时 / 15 分钟"


# ------------------------------------------------- 逐条增删复查点（用户 m22010）


def _draw_points(fn, width=100, height=30):
    buf = io.StringIO()
    console = Console(file=buf, width=width, height=height, force_terminal=True, color_system=None)
    fn(console)
    return buf.getvalue()


def _point_lines(text: str) -> list[str]:
    """只留下"点"那几行，便于断言顺序与选中态。"""
    return [line for line in text.splitlines() if line.strip().startswith(("-", "❯", "  -"))]


def test_point_editor_lists_each_point_on_its_own_line():
    from meruki_tracker.tui.screens import render_point_edit

    out = _draw_points(
        lambda c: render_point_edit(
            c,
            label="结束前复查点",
            help_text="距结束多少分钟各拉一次详情",
            points=("6 小时", "1 小时", "15 分钟"),
            cursor=0,
        ),
        height=20,
    )
    assert "修改：结束前复查点" in out
    # 一行一个：不能挤成"6 小时 / 1 小时 / 15 分钟"
    assert "/" not in out
    assert "- 6 小时" in out and "- 1 小时" in out and "- 15 分钟" in out
    # 选中项有个独立的指示符
    assert "❯" in out


def test_point_editor_marks_only_the_cursor():
    from meruki_tracker.tui.screens import render_point_edit

    out = _draw_points(
        lambda c: render_point_edit(
            c,
            label="结束前复查点",
            help_text="",
            points=("6 小时", "1 小时", "15 分钟"),
            cursor=1,
        ),
        height=20,
    )
    assert out.count("❯") == 1
    marked = next(line for line in out.splitlines() if "❯" in line)
    assert "- 1 小时" in marked


def test_point_editor_shows_the_input_row_only_while_typing():
    from meruki_tracker.tui.screens import render_point_edit

    idle = _draw_points(
        lambda c: render_point_edit(
            c, label="结束前复查点", help_text="", points=("1 小时",), cursor=0
        ),
        height=20,
    )
    assert "新点" not in idle

    typing = _draw_points(
        lambda c: render_point_edit(
            c,
            label="结束前复查点",
            help_text="",
            points=("1 小时",),
            cursor=0,
            text="2 小时",
            editing=True,
            unit="分钟",
            units=("分钟", "小时"),
        ),
        height=20,
    )
    assert "新点" in typing and "2 小时" in typing
    assert "Tab 切换" in typing


def test_point_editor_fits_every_height():
    """行数随点数变化，最容易把页脚顶出去。"""
    from meruki_tracker.tui.screens import render_point_edit

    points = tuple(f"- {n} 小时" for n in range(20, 0, -1))
    for height in (30, 24, 20, 15, 12, 10, 8, 6, 5, 4, 3, 2, 1):
        for cursor in (0, 5, 19):
            lines = _draw_points(
                lambda c, cur=cursor: render_point_edit(
                    c,
                    label="结束前复查点",
                    help_text="距结束这些时间点各提醒一次",
                    points=points,
                    cursor=cur,
                    notice=("✓ 已加上一个点", "另一条提示"),
                ),
                width=44,
                height=height,
            ).splitlines()
            assert len(lines) <= height, f"height={height} cursor={cursor} 画了 {len(lines)} 行"


def test_point_editor_keeps_the_cursor_visible():
    from meruki_tracker.tui.screens import render_point_edit

    points = tuple(f"- {n} 小时" for n in range(20, 0, -1))
    for cursor in range(len(points)):
        out = _draw_points(
            lambda c, cur=cursor: render_point_edit(
                c, label="结束前复查点", help_text="", points=points, cursor=cur
            ),
            width=60,
            height=12,
        )
        marked = [line for line in out.splitlines() if "❯" in line]
        assert marked, f"cursor={cursor} 时选中项没画出来"
        assert f"- {points[cursor]}" in marked[0]


def test_point_editor_says_how_many_points_are_hidden():
    from meruki_tracker.tui.screens import render_point_edit

    points = tuple(f"- {n} 小时" for n in range(20, 0, -1))
    out = _draw_points(
        lambda c: render_point_edit(
            c, label="结束前复查点", help_text="", points=points, cursor=10
        ),
        width=60,
        height=12,
    )
    assert "上面还有" in out
    assert "下面还有" in out


def test_point_editor_sorts_nothing_and_trusts_its_caller():
    """屏幕只负责画：排序去重在 ``normalize_points`` 里做过了，别画的时候再改顺序。"""
    from meruki_tracker.tui.screens import render_point_edit

    out = _draw_points(
        lambda c: render_point_edit(
            c, label="结束前复查点", help_text="", points=("2 小时", "6 小时"), cursor=0
        ),
        height=20,
    )
    first = next(line for line in out.splitlines() if "- 2 小时" in line)
    second = next(line for line in out.splitlines() if "- 6 小时" in line)
    assert out.index(first) < out.index(second)


def test_point_editor_does_not_wrap_a_long_line():
    from meruki_tracker.tui.screens import render_point_edit

    long_help = "距结束这些时间点各提醒一次；" * 12
    lines = _draw_points(
        lambda c: render_point_edit(
            c, label="结束前复查点", help_text=long_help, points=("1 小时",), cursor=0
        ),
        width=40,
        height=20,
    ).splitlines()
    assert len(lines) <= 20
    assert max(len(line) for line in lines) <= 40


def test_point_editor_empty_list_has_a_hint():
    from meruki_tracker.tui.screens import render_point_edit

    out = _draw_points(
        lambda c: render_point_edit(c, label="结束前复查点", help_text="", points=(), cursor=0),
        height=20,
    )
    assert "按 a 加一个" in out


# ------------------------------------------------- 详情屏：拍品的规则属性


def _detail_screen_text(*, auto_extend=None, early_termination=None, height=30) -> str:
    """在固定尺寸下渲染详情屏，返回纯文本（无 ANSI）。"""
    from datetime import UTC, datetime, timedelta

    from meruki_tracker.core.adapters.store.snapshot import Account, LastAttempt
    from meruki_tracker.core.domain.models import DoorState, EndTime, UserQuery

    now = datetime.now(UTC)
    st = AuctionState(
        asin="A1",
        name="尼康 F3",
        url="https://auctions.yahoo.co.jp/jp/auction/A1",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(now + timedelta(hours=2), now),
        latest_bid=12500.0,
        last_seen_at=now,
    )
    buf = io.StringIO()
    console = Console(file=buf, width=100, height=height, force_terminal=False)
    screens.render_detail(
        console,
        st=st,
        snapshot=Snapshot(
            generated_at=now, last_attempt=LastAttempt(now, True, ""), account=Account()
        ),
        observations=[],
        tz=TZ,
        auto_extend=auto_extend,
        early_termination=early_termination,
    )
    return buf.getvalue()


def test_detail_shows_the_two_rule_flags():
    """提前结束 / 自动延长 是详情里的规则属性，得在详情屏上看得见。

    依据详情接口 ``other.earlyTerminationHelp`` / ``autoExtendHelp``：
    前者是"卖家可以在原定结束前提前结束并卖给别人"，后者是"结束前五分钟内
    有人抬价就再延五分钟"。它们不改变状态机的任何值，但直接决定"要不要现在
    出手"，所以必须出现在决策屏上。
    """
    out = _detail_screen_text(auto_extend=True, early_termination=True)
    assert "提前结束" in out
    assert "自动延长" in out


def test_detail_hides_rule_flags_when_unknown():
    """没拉过详情时不画这两行：``—`` 和 ``不会`` 在屏幕上一眼分不清。"""
    out = _detail_screen_text()
    assert "提前结束" not in out
    assert "自动延长" not in out


def test_detail_rule_flags_say_no_in_words():
    """``False`` 要写成"不会"，不能留空或写成破折号。"""
    out = _detail_screen_text(auto_extend=False, early_termination=False)
    assert "提前结束" in out and "不会" in out
    assert "自动延长" in out


def test_mount_screen_keeps_hints_inside_the_height():
    """矮终端上「按 r 重新取码」这条提示不能被裁掉。

    2026-11-18 审计：``render_mount`` 的提示行只判 ``if height > 0``（height 被
    ``max(3, …)`` 兜住恒真），80×6 实测画出 7 行，被 prompt_toolkit 从底部裁掉
    ——砍掉的正好是唯一那条"还能换码"的提示。
    """
    from rich.text import Text

    from meruki_tracker.tui.screens.qr import QrChallenge, render_mount

    challenge = QrChallenge(kind="wechat", payload="x" * 200, hint="用微信扫一扫，并在手机上确认")
    for height in (4, 6, 8, 12, 24):
        buf = io.StringIO()
        console = Console(file=buf, width=80, height=height)
        render_mount(console, challenge=challenge, notice=("按 r 重新取码，换一个账号",))
        lines = buf.getvalue().rstrip("\n").split("\n")
        assert len(lines) <= height, f"height={height} 画了 {len(lines)} 行"
    assert isinstance(Text(""), Text)  # 保持 import 有用（类型注解在别处复用）


def test_point_edit_input_row_survives_a_two_line_terminal():
    """正在输入「新点」时，那一行不能被「N 个点放不下」顶出屏幕。

    2026-11-18 审计：提示行在预算之外，body=2 时它占掉最后一行，用户在盲打。
    """
    from meruki_tracker.tui.screens import settings as settings_screen

    for body in (2, 3, 4):
        buf = io.StringIO()
        console = Console(file=buf, width=60, height=body)
        settings_screen.render_point_edit(
            console,
            label="结束前复查点",
            help_text="上下选择，Enter 改",
            points=("360 分钟", "60 分钟", "15 分钟"),
            cursor=0,
            text="6",
            editing=True,
            unit="分钟",
            units=("分钟", "小时"),
        )
        out = buf.getvalue()
        lines = out.rstrip("\n").split("\n")
        assert len(lines) <= body, f"body={body} 画了 {len(lines)} 行"
        assert "新点" in out, "正在输入的那一行必须看得见"
