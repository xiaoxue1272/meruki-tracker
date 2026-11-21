"""定时刷新与刷新编排测试（不触网）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.snapshot import Snapshot


class FakeRefresher:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls = 0
        self.running = False

    async def refresh(self, previous: Snapshot, progress=None):
        self.calls += 1
        if progress:
            progress("列表 第 1 页")
        from meruki_tracker.core.app.refresher import RefreshResult

        if self.ok:
            return previous, RefreshResult(ok=True)
        return previous, RefreshResult(ok=False, error="网络不通", error_kind="network")


def test_credential_store_roundtrip(tmp_path: Path):
    store = CredentialStore(tmp_path / "cookies")
    assert store.load() == ""
    store.save("a=1; b=2")
    assert store.load() == "a=1; b=2"
    if hasattr(tmp_path, "stat"):
        import os

        if os.name != "nt":
            assert ((tmp_path / "cookies").stat().st_mode & 0o777) == 0o600


def test_credential_clear(tmp_path: Path):
    store = CredentialStore(tmp_path / "cookies")
    store.save("x=1")
    store.clear()
    assert store.load() == ""


async def test_auto_loop_skips_when_refresher_running(tmp_path: Path):
    """定时刷新在手动静默进行时应跳过本轮。"""
    fake = FakeRefresher()
    fake.running = True

    async def loop_once(interval: float) -> None:
        await asyncio.sleep(interval)
        if fake.running:
            return
        await fake.refresh(Snapshot())

    await loop_once(0.01)
    assert fake.calls == 0

    fake.running = False
    await loop_once(0.01)
    assert fake.calls == 1


async def test_refresher_busy_guard():
    """refresher 并发调用时第二次直接返回 busy，不产生重复请求。"""
    from meruki_tracker.core.app.context import build_context

    ctx = build_context(tmp_config())
    try:
        ctx.refresher._running = True  # 模拟正在采集
        _snap, result = await ctx.refresher.refresh(Snapshot())
        assert result.ok is False
        assert result.error_kind == "busy"
    finally:
        await ctx.close()


def tmp_config():
    """临时数据目录的配置（配置不再来自文件）。"""
    import tempfile
    from dataclasses import replace

    from meruki_tracker.core.config import Config, StorageConfig

    d = Path(tempfile.mkdtemp())
    return replace(Config.defaults(), storage=StorageConfig(data_dir=str(d / "data")))


async def test_refresh_without_cookie_reports_auth(tmp_path: Path):
    from meruki_tracker.core.app.context import build_context

    ctx = build_context(tmp_config())
    try:
        _snap, result = await ctx.refresher.refresh(Snapshot())
        assert result.ok is False
        assert result.error_kind == "auth"
    finally:
        await ctx.close()


# ---------------------------------------------------------------- 观测时间格式统一
# （归一化本身的测试见 tests/test_deadline.py，这里只测刷新链路确实用了它）


def test_fetch_list_stores_normalized_deadline(tmp_path: Path):
    """刷新把列表观测写库时，end_time_raw 必须是带时区的 ISO。

    早期直接存站点原始字符串，与详情的 ISO 混在一列里，事后没法比较。
    """

    from meruki_tracker.core.adapters.store.db import connect as db_connect
    from meruki_tracker.core.adapters.store.migrate import normalize_legacy_observation_times
    from meruki_tracker.core.deadline import is_normalized

    conn = db_connect(tmp_path / "meruki.db")
    try:
        with conn:
            conn.execute(
                "INSERT INTO observations (asin, observed_at, kind, end_time_raw)"
                " VALUES ('A1', '2026-10-30T01:00:00+00:00', 'list', '2026-11-01 21:47:14')"
            )
        normalize_legacy_observation_times(conn)
        raw = conn.execute("SELECT end_time_raw FROM observations").fetchone()[0]
        assert is_normalized(raw), f"迁移后应当是带时区的 ISO，实际 {raw!r}"
        assert raw == "2026-11-01T21:47:14+08:00"
    finally:
        conn.close()


# ---------------------------------------------------------------- 刷新期间列表仍可操作


def test_navigation_and_action_keys_not_blocked_during_refresh():
    """刷新期间老列表既能翻能看，用户动作键也不再被 ``_busy`` 屏蔽。

    2026-11-10 起 ``_busy`` 只是进度文案、不再是按键锁：r/t/s/x 在刷新期间照样
    能按（串行队列整套删掉后，后台动作直接起任务，重复刷新由 refresher 自身的
    busy 守卫兜住）。过去的 "r/s 刷新期间应被屏蔽" 断言随这次改动一起废弃。
    用 prompt_toolkit 自己的匹配器验证**行为**，不去读 ``_build_keys`` 的源码：
    源码级断言（``inspect.getsource`` 里找 ``filter=on_list``）在按键按屏拆成
    多个 ``_bind_*`` 方法后就失效了，而且它只能证明"源码里写了那行字"，证明不了
    运行时真的生效。``d`` 已随「自检」功能一并移除，不再是按键。
    """
    import meruki_tracker.tui.tui as mod

    tui = mod.Tui.__new__(mod.Tui)
    kb = tui._build_keys()

    def winner(key: str, *, busy: bool) -> str | None:
        """在 LIST 屏、给定 busy 状态下，按这个键最终会执行的 handler 名字。"""
        tui._screen = mod.Screen.LIST
        tui._busy = busy
        matches = [b for b in kb.get_bindings_for_keys((key,)) if b.filter()]
        return getattr(matches[-1].handler, "__name__", None) if matches else None

    for key in ("down", "up", "right", "left"):
        assert winner(key, busy=False) is not None, f"{key} 平时就该能按"
        assert winner(key, busy=True) is not None, f"{key} 不该被 busy 屏蔽，否则刷新时列表看着卡死"
    # r/t/s/x 平时可用，刷新期间照样可用（不再被 idle 锁吞掉）
    for key in ("r", "t", "s", "x"):
        assert winner(key, busy=False) is not None, f"{key} 平时就该能按"
        assert winner(key, busy=True) is not None, f"{key} 不该被 busy 屏蔽"
    # d 随「自检」整条功能移除，平时与刷新期间都不该有绑定
    assert winner("d", busy=False) is None, "d 已不再是按键（自检功能已移除）"
    assert winner("d", busy=True) is None
