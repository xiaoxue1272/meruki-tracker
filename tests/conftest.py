"""测试全局夹具。

**rich 的旧式 Windows 控制台补偿必须关掉。**

rich 在 `detect_legacy_windows()` 为真时会把 `Console(width=N)` 的可用宽度
偷偷减 1（给行尾的回车留位）。GitHub 的 windows runner 恰好被判成 legacy，
于是我们所有"按给定宽度渲染"的断言在 Windows 上集体差一列——列表的当前价
列被整列丢弃、页脚被 ellipsis 截断、主体宽度算成 81/59 而不是 82/60。

这些是纯渲染单测：传入多少宽度就该有多少宽度，不该由宿主 OS 决定。全局把
`detect_legacy_windows` 钉成 False，让富文本布局在各平台可复现。

生产代码里宽度来自 prompt_toolkit 的屏幕尺寸、由我们自己算好，已在
`tui.py` / `screens/detail.py` 的 Console 上显式传 `legacy_windows=False`。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _rich_exact_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rich.console.detect_legacy_windows", lambda: False)
