"""配置分组条。

配置页按 ``config_options.GROUP_ORDER`` 分组，``[`` / ``]`` 横切，这根条显示
"我在哪一页、还有哪些页"。**不会折行**：窄到放不下时只显示当前项，而不是把
一行切一半——切一半的标签既看不懂也点不准。
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text


def tab_bar(labels: list[str], current: int, *, width: int = 0, compact: bool = False) -> Text:
    """配置分组条。``labels`` 与 ``current`` 由调用方给，计数也拼在 label 里。

    窄屏只留当前项：这个条的作用是"我知道还有别的页、以及我在哪一页"，
    全塞不下时先保住后半句。
    """
    if not labels:
        return Text("")
    current = max(0, min(current, len(labels) - 1))
    if compact or (width and width < 40):
        return Text.assemble(("◧", "dim"), (f" {labels[current]} ", "reverse"))
    out = Text()
    for i, label in enumerate(labels):
        if i:
            out.append("·", style="dim")
        out.append(f" {label} ", style="reverse" if i == current else "")
    if width and cell_len(out.plain) > width:
        return Text.assemble(("◧", "dim"), (f" {labels[current]} ", "reverse"))
    return out
