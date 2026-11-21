"""逐轮比对：拿上一轮的接口镜像和这一轮比，产出"变了什么"。

**为什么改成比对**

以前靠 ``lifecycle.advance`` 做推断式判定：不在列表里 + 结束时间已过 → 判为
结束，否则判为取关。这是**猜**——列表接口只回「进行中」，拍卖一结束就从列表
消失，"消失"这个事实本身不区分"结束"和"取关"，只能靠时间兜。猜错了整整一类
拍品（见 ``migrate.repair_mislabeled_unfollowed``）。

现在换成比对：每一轮把接口原样存下来，和上一轮逐字段 diff，产出显式的事件
（``appeared`` / ``disappeared`` / ``field_changed``）。事件是**可回放的记录**，
不是推测的结论；到底该判成"结束"还是"取关"由调用方结合服务端事实（例如
「已结束」列表里有没有它）决定，而不是由这里武断下结论。

本模块是纯函数、零 I/O，方便单测。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 参与比对的字段。``bid_deadline_str`` 是倒计时（每次都在变，纯噪音）除外。
COMPARED_FIELDS: tuple[str, ...] = (
    "name",
    "url",
    "detail_url",
    "bid_deadline",
    "created_time",
    "image_url",
    "user_id",
    "item_id",
)

#: 字段名 → 中文标签（落 changes 表时给人看）
FIELD_LABELS: dict[str, str] = {
    "name": "标题",
    "url": "链接",
    "detail_url": "详情地址",
    "bid_deadline": "结束时间",
    "created_time": "关注时间",
    "image_url": "图片",
    "user_id": "卖家",
    "item_id": "收藏序号",
    "bid_price": "当前价",
    "buy_now_price": "一口价",
    "end_at": "结束时间(详情)",
    "ended": "已结束标记",
}

#: 事件种类
KIND_APPEARED = "appeared"
KIND_DISAPPEARED = "disappeared"
KIND_FIELD_CHANGED = "field_changed"
KIND_DETAIL_REFRESHED = "detail_refreshed"


@dataclass(frozen=True, slots=True)
class Change:
    """一条变更事件。"""

    asin: str
    kind: str
    field: str = ""
    old: str = ""
    new: str = ""

    @property
    def label(self) -> str:
        return FIELD_LABELS.get(self.field, self.field or self.kind)

    def as_row(self) -> dict[str, Any]:
        """转成 ``MirrorStore.save_round(changes=...)`` 需要的形状。"""
        return {
            "asin": self.asin,
            "kind": self.kind,
            "field": self.field,
            "old_value": self.old,
            "new_value": self.new,
        }


def _field(item: Any, name: str) -> str:
    value = item.get(name, "")
    return "" if value is None else str(value)


def diff_list_rounds(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    complete: bool,
) -> list[Change]:
    """比对本轮与上一轮的列表镜像。

    Args:
        previous: 上一轮，``asin -> 条目``（现存的 ``list_items``）。
        current: 本轮，``asin -> 条目``。
        complete: 本轮列表是否完整拉到最后一页。**不完整时不做 ``disappeared``
            判定**——残缺列表不能作为"这个拍品没了"的证据。

    Returns:
        变更事件列表（新出现的 → 字段变化 → 消失，顺序稳定便于测试）。
    """
    changes: list[Change] = []

    for asin in sorted(current):
        cur = current[asin]
        prev = previous.get(asin)
        if prev is None:
            changes.append(Change(asin=asin, kind=KIND_APPEARED))
            continue
        for field in COMPARED_FIELDS:
            old_v, new_v = _field(prev, field), _field(cur, field)
            if old_v != new_v:
                changes.append(
                    Change(asin=asin, kind=KIND_FIELD_CHANGED, field=field, old=old_v, new=new_v)
                )

    if complete:
        for asin in sorted(set(previous) - set(current)):
            changes.append(Change(asin=asin, kind=KIND_DISAPPEARED))

    return changes


def diff_detail_rounds(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> list[Change]:
    """比对本轮与上一轮的详情镜像（价格 / 结束时间 / 结束标记）。

    用独立的 ``detail_refreshed`` 事件类型而不是复用 ``field_changed``：
    详情与列表来自两个不同接口，查历史时"这条变更出自哪次请求"是必须的。
    """
    fields = ("name", "end_at", "ended", "bid_price", "buy_now_price")
    changes: list[Change] = []
    for asin in sorted(current):
        cur = current[asin]
        prev = previous.get(asin)
        if prev is None:
            continue  # 第一次拿到详情不算"变化"
        for field in fields:
            old_v, new_v = _field(prev, field), _field(cur, field)
            if old_v != new_v:
                changes.append(
                    Change(asin=asin, kind=KIND_DETAIL_REFRESHED, field=field, old=old_v, new=new_v)
                )
    return changes


def summarize(changes: list[Change]) -> str:
    """给界面/日志用的一句话摘要。"""
    if not changes:
        return "无变化"
    appeared = sum(1 for c in changes if c.kind == KIND_APPEARED)
    gone = sum(1 for c in changes if c.kind == KIND_DISAPPEARED)
    parts: list[str] = []
    if appeared:
        parts.append(f"{appeared} 个新出现")
    if gone:
        parts.append(f"{gone} 个消失")
    others = len(changes) - appeared - gone
    if others:
        parts.append(f"{others} 处字段变化")
    return "、".join(parts)
