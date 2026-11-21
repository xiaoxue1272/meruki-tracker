"""商品描述、图片、成色这些"详情里才有的文字"的提取。

详情接口的原始 JSON 里，有用的文字散在三处，而且都不好直接用：

- ``other.descText``：**纯文本**描述，最省事的一处。
- ``other.goodsDesc``：带 HTML 的描述（卖家自己写的版式、``<SPAN>`` 拆字），
  ``descText`` 缺失时才退回到它，必须把标签剥掉。
- ``goodsInfo.images``：图片 URL 列表，通常 8~10 张。

这些函数是纯函数（输入 dict，输出 str / list），不碰网络也不碰数据库——
翻译、截断、剥 HTML 都能单独测。
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

from .text import scrub

#: 翻译接口单次能吃的上限。前端自己也会截到 9980，超了服务端直接 500。
#: 描述再长也只是"看看卖家写了什么"，截断不损失决策信息。
TRANSLATE_LIMIT = 9_900

_TAG_RE = re.compile(r"<[^>]*>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
#: 连续空白（含换行）压成一个空格，但**保留换行**——描述是一段段写的，
#: 全压成一行会变成一坨，读起来比不显示还难受。
_BLANK_LINES_RE = re.compile(r"[ \t\u3000]+")
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")


def strip_html(raw: str) -> str:
    """把 HTML 片段变成可读纯文本。

    ``descText`` 缺失时要退回 ``goodsDesc``，而后者是带样式的 HTML
    （``<style>`` 块、``<SPAN>`` 拆字、``<BR>`` 换行）。直接塞进终端会显示成
    一堆尖括号，所以先剥标签、解实体、再把行首尾空白整理掉。

    ``<br>`` / ``</p>`` / ``</div>`` 换成换行，否则整段会挤成一行。
    """
    if not raw:
        return ""
    text = _SCRIPT_STYLE_RE.sub("", raw)
    # 块级结束标签换成换行。放在剥标签之前，否则信息就丢了。
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    # &nbsp; 解出来是 U+00A0（不换行空格），不是普通空格。它占一列但看着像空，
    # 行尾留着会让文本多出莫名其妙的空白，所以统一归一。
    text = text.replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 行内连续空格压成一个（中日文描述常有一长串全角空格做缩进）
    text = _BLANK_LINES_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _MANY_NEWLINES_RE.sub("\n\n", text)
    # 描述同样是卖家写的，可能带 ESC 之类的控制字符——终端会把它们当指令
    # 执行（清屏 / 改标题），而不是显示成文字。
    return scrub(text.strip())


def description_of(raw: Mapping[str, Any] | None) -> str:
    """取商品描述（纯文本）。``descText`` 优先，退回剥过标签的 ``goodsDesc``。"""
    other = _mapping((raw or {}).get("other"))
    for key in ("descText", "goodsDesc"):
        text = strip_html(str(other.get(key) or ""))
        if text:
            return text
    return ""


def clip_for_translation(text: str, limit: int = TRANSLATE_LIMIT) -> str:
    """按字符数截断，供翻译接口用。

    接口超过 ~10000 字符直接 500，所以**必须**在发出去之前截。用字符数而不是
    字节数：截断点落在多字节字符中间会得到半个字，JSON 编码时更难看。
    """
    if len(text) <= limit:
        return text
    return text[:limit]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
