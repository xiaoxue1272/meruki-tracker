"""真实接口联调测试（默认跳过，需设置 MAM_LIVE_TEST=1 且本地有 Cookie）。

这类测试不进 CI：依赖真实账号与网络，但它们是"程序真的能用"的唯一证据。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meruki_tracker.core.app.context import build_context, do_refresh, load_snapshot
from meruki_tracker.core.config import Config
from meruki_tracker.core.domain.models import TimeState

LIVE = os.environ.get("MAM_LIVE_TEST") == "1"
COOKIE = os.environ.get("MAM_LIVE_COOKIE", "")


@pytest.mark.skipif(not LIVE, reason="设置 MAM_LIVE_TEST=1 才运行真实接口测试")
async def test_live_refresh_gets_exact_times(tmp_path: Path):
    """真实账号跑一轮：所有在拍拍品都应拿到精确时间。"""
    (tmp_path / "cookies").write_text(COOKIE, encoding="utf-8")
    (tmp_path / "cookies").chmod(0o600)
    cfg_path = tmp_path / "meruki-tracker.config.yaml"
    cfg_path.write_text(f"storage:\n  data_dir: {tmp_path}\n", encoding="utf-8")

    ctx = build_context(Config.load(cfg_path))
    try:
        snap = load_snapshot(ctx)
        snap, result = await do_refresh(ctx, snap)
        assert result.ok, result.error
        assert result.stats.listed > 0
        assert result.stats.details_failed == 0
        following = [a for a in snap.auctions if a.user_query.value == "following"]
        assert following
        assert all(a.end_time.state is TimeState.EXACT for a in following)
    finally:
        await ctx.close()
