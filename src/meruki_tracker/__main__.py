"""程序入口：唯一命令 meruki-tracker，无子命令。

启动流程：
1. 从库里读出配置（配置在程序内的「设置」里改，没有外部配置文件）
2. 装配依赖
3. 交给 :class:`meruki_tracker.tui.tui.Tui`：单应用常驻，屏幕状态机切换
   （列表 / 详情 / 登录 / 设置），首次无 Cookie 时直接进登录屏
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from .core.app.context import build_context, load_config
from .core.config import Config
from .core.errors import ConfigError
from .core.logging_setup import get_logger, setup_logging
from .tui.tui import Tui

log = get_logger("main")


async def _run(cfg: Config) -> int:
    ctx = build_context(cfg)
    try:
        return await Tui(ctx, cfg).run()
    finally:
        await ctx.close()


def main(argv: list[str] | None = None) -> int:
    """程序入口。"""
    parser = argparse.ArgumentParser(
        prog="meruki-tracker",
        description="挖煤姬拍卖监视器。启动即进入 TUI；所有操作在界面内完成。",
    )
    parser.add_argument("--version", action="version", version=f"meruki-tracker v{__version__}")
    parser.parse_args(argv)  # 只为处理 --version/-h

    # 数据目录是唯一的"启动参数"，且它本身也只能从默认值开始——
    # 配在库里的话就成了鸡生蛋问题。放到库里之后再由「设置」界面改。
    data_dir = Path(Config.defaults().data_dir)
    try:
        cfg = load_config(data_dir)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "当前不是交互式终端，无法启动 TUI。\n请在真实终端（或 ssh 会话）中运行 meruki-tracker。",
            file=sys.stderr,
        )
        return 2

    setup_logging(cfg.data_dir)
    try:
        return asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
