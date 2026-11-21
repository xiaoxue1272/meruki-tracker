"""结束时间字符串的归一化。

**为什么单独一个模块**：列表接口和详情接口给的格式不同，而写入观测、
迁移历史数据这两处都要用它。放在这里避免 ``adapters`` 反向依赖 ``app``。

- 列表接口 ``BidDeadline``：``"2026-11-01 21:47:14"``——站点本地时间，
  **不带时区标注**（挖煤姬面向国内用户，即 UTC+8）。
- 详情接口 ``deadTimeWithZone``：带时区的 ISO，或直接是 UTC。

两种格式混在同一个列里，事后回溯时无法比较；统一成带时区的 ISO 之后
才能做时间序分析。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: 站点本地时区（挖煤姬的 BidDeadline 就是这个时区，且不写出来）
SITE_TZ = timezone(timedelta(hours=8))

#: 见过的列表接口时间格式
_LIST_DEADLINE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


def parse_deadline(raw: str) -> datetime | None:
    """把列表接口的结束时间解析成带时区的 ``datetime``。

    列表给的是**站点本地时间、不带时区**（``2026-11-01 21:22:11``，东京时间），
    按 :data:`SITE_TZ` 解释。解析不出来返回 None——调用方要能区分"没有时间"和
    "时间是 1970"，所以不能给个默认值糊过去。

    Args:
        raw: 原始时间字符串。

    Returns:
        带时区的 ``datetime``，或 None。
    """
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in _LIST_DEADLINE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SITE_TZ)
        except ValueError:
            continue
    return None


def normalize_deadline(raw: str) -> str:
    """把结束时间统一成带时区的 ISO 字符串。

    解析不出来就**原样返回**：观测是历史留痕，宁可不规范也不能丢原始值。

    Args:
        raw: 原始时间字符串。

    Returns:
        带时区的 ISO 字符串（如 ``2026-11-01T21:47:14+08:00``），
        或无法解析时的原始值。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    parsed = parse_deadline(text)
    return parsed.isoformat() if parsed is not None else text


def is_normalized(raw: str) -> bool:
    """是否已经是"能被解析出时区"的形式（用于判断要不要迁移）。"""
    text = (raw or "").strip()
    if not text:
        return True  # 空值无所谓格式
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.utcoffset() is not None
