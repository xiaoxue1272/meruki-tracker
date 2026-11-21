"""商品描述 / 图片 / 成色标签的提取，以及翻译接口的接线。

真实反馈：「还能不能在详情里展示下商品描述啊，图片地址啊啥的呢？」
「最好还能翻译一下，app 里是有翻译功能的」。

详情接口的原始 JSON 里这些文字散在三处，而且最省事的那处（``descText``）
不一定有——所以退回 ``goodsDesc`` 时必须剥干净 HTML，否则终端里会显示一堆
尖括号。
"""

from __future__ import annotations

import json
from typing import Any

from meruki_tracker.core.adapters.meruki.api import DetailInfo, MerukiApi
from meruki_tracker.core.domain.goods import (
    TRANSLATE_LIMIT,
    clip_for_translation,
    description_of,
    strip_html,
)

RAW: dict[str, Any] = {
    "goodsInfo": {
        "name": "Konica IIB",
        "images": [
            "https://img/1.jpg",
            "https://img/2.jpg",
            "https://img/2.jpg",  # 重复项应被去掉
            "",
        ],
    },
    "other": {
        "descText": "1行目\n2行目",
        "goodsDesc": "<div><b>HTML 版</b></div>",
        "property": ["二手", "易碎品", ""],
    },
}


def test_description_prefers_plain_text():
    assert description_of(RAW) == "1行目\n2行目"


def test_description_falls_back_to_html_when_plain_text_is_missing():
    raw = {"other": {"goodsDesc": "<div>一行<br>二行</div>"}}
    # <br> 与 </div> 都换成换行，否则整段会挤成一行
    assert description_of(raw) == "一行\n二行"


def test_description_is_empty_for_a_bare_raw():
    assert description_of({}) == ""
    assert description_of(None) == ""


def test_strip_html_removes_style_and_spans():
    html = "<style>#x{color:red}</style><div id='d'><SPAN>20</SPAN>年と<SPAN>3</SPAN>枚</div>"
    assert strip_html(html) == "20年と3枚"


def test_strip_html_unescapes_entities():
    # &nbsp; 解出来是不换行空格 U+00A0，必须归一成普通空格——否则 termin 里
    # 它占一列但看着像空，行尾还会被它顶出奇怪的空白。
    assert strip_html("a &amp; b &lt;tag&gt; &nbsp;c") == "a & b <tag> c"


def test_clip_for_translation_keeps_short_text():
    assert clip_for_translation("短文本") == "短文本"


def test_clip_for_translation_truncates_at_the_limit():
    long_text = "あ" * (TRANSLATE_LIMIT + 500)
    clipped = clip_for_translation(long_text)
    assert len(clipped) == TRANSLATE_LIMIT
    # 按字符截断，不能截出半个多字节字符
    assert clipped == "あ" * TRANSLATE_LIMIT


class _StubTransport:
    """只回一个固定响应，用来检查 translate() 发的参数和解析。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, *, headers=None, json_body=None, timeout=20.0):
        from meruki_tracker.core.adapters.meruki.transport import Response

        self.requests.append({"method": method, "url": url, "json": json_body})
        body = json.dumps({"code": 200, "data": self.payload}, ensure_ascii=False).encode()
        return Response(status=200, headers=(), body=body)

    async def aclose(self) -> None:
        return None


async def test_translate_sends_both_texts_and_returns_pair():
    transport = _StubTransport({"title": "标题译", "desc": "描述译"})
    api = MerukiApi(transport, cookie="", retry=0, min_interval=0.0, jitter=0.0)

    title, desc = await api.translate(title="標題", desc="説明")

    assert (title, desc) == ("标题译", "描述译")
    body = transport.requests[0]["json"]
    assert body["titleText"] == "標題"
    assert body["descText"] == "説明"
    assert body["targetLanguage"] == "zh-CN"
    assert body["formatType"] == "text"


async def test_translate_raises_when_both_sides_are_empty():
    transport = _StubTransport({"title": "", "desc": ""})
    api = MerukiApi(transport, cookie="", retry=0, min_interval=0.0, jitter=0.0)

    from meruki_tracker.core.errors import ProtocolChangedError

    try:
        await api.translate(title="x", desc="y")
    except ProtocolChangedError:
        return
    raise AssertionError("两侧都空时应该报协议异常，否则界面会显示一屏空白")


def test_detail_info_carries_the_raw_payload():
    """描述和图片都从 raw 里取，所以它必须原样带上。"""
    from meruki_tracker.core.adapters.meruki.api import _map_detail

    info = _map_detail(
        {
            "goodsInfo": {"asin": "A1", "name": "n", "bidPrice": 0, "images": ["u"]},
            "other": {"descText": "描述"},
            "over": False,
        }
    )
    assert isinstance(info, DetailInfo)
    assert description_of(info.raw) == "描述"
