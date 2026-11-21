"""领域模型：不可变值对象。

设计原则：
- 时间只有两种状态：EXACT（详情确认，精确到秒）/ PENDING（还没拿到详情）。
  不存在"大致准确"——区间模型已被证伪（详情一定能给精确时间）。
- 拍品状态由两个正交维度组成：用户操作态 × 拍卖结果。
  禁止合并成一个枚举，否则"已取关但拍卖还在进行"这类状态无法表达。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .text import scrub


class TimeState(StrEnum):
    """结束时间的二态。"""

    EXACT = "exact"
    """已从详情确认，精确到秒。"""

    PENDING = "pending"
    """还没拿到详情，结束时间未知。此时 at 必须为 None。"""


class UserQuery(StrEnum):
    """用户操作态：用户在网站上是否还关注这个拍品。"""

    FOLLOWING = "following"
    UNFOLLOWED = "unfollowed"


class DoorState(StrEnum):
    """拍卖本身的状态。"""

    ACTIVE = "active"
    ENDED = "ended"
    INVALID = "invalid"
    """详情连续失败达到阈值，无法确认。"""


class TransitionKind(StrEnum):
    """状态推进产生的事件种类（阶段 3 的推送事件源）。"""

    DISCOVERED = "discovered"
    UNFOLLOWED = "unfollowed"
    REFOLLOWED = "refollowed"
    RESURRECTED = "resurrected"
    ENDED = "ended"
    END_TIME_CHANGED = "end_time_changed"
    INVALIDATED = "invalidated"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class EndTime:
    """结束时间。EXACT 时 at 必有值，PENDING 时 at 必为 None。"""

    state: TimeState
    at: datetime | None = None
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state is TimeState.PENDING and self.at is not None:
            raise ValueError("PENDING 状态不允许携带具体时间")
        if self.state is TimeState.EXACT and self.at is None:
            raise ValueError("EXACT 状态必须携带具体时间")

    @classmethod
    def pending(cls) -> EndTime:
        return cls(TimeState.PENDING)

    @classmethod
    def exact(cls, at: datetime, confirmed_at: datetime) -> EndTime:
        return cls(TimeState.EXACT, at=at, confirmed_at=confirmed_at)


@dataclass(frozen=True, slots=True)
class AuctionState:
    """单个拍品的完整状态。"""

    asin: str
    name: str
    url: str
    user_query: UserQuery
    door_state: DoorState
    end_time: EndTime
    #: 当前价（拍卖价，日元）。一口价商品没有拍卖价 → None
    latest_bid: float | None = None
    #: 一口价（``buyNowPrice``，日元）。0 / None 都表示没有一口价
    buy_now_price: float | None = None
    detail_failures: int = 0
    last_seen_at: datetime | None = None
    unfollowed_at: datetime | None = None
    concluded_at: datetime | None = None

    def __post_init__(self) -> None:
        """把远端的文字洗干净再存进模型。

        ``name`` / ``url`` 是卖家自己填的，终端会把 ESC 之后的内容当指令执行
        （清屏、改标题…）。在**模型这一层**抹掉控制字符，是因为这里是所有
        显示路径（列表、详情、通知正文、导出文件名）唯一的共同上游——在每个
        渲染函数里各补一次，迟早漏一个。
        """
        object.__setattr__(self, "name", scrub(self.name))
        object.__setattr__(self, "url", scrub(self.url))

    @property
    def is_tracked(self) -> bool:
        """是否需要继续参与后台活动（补详情/复查）。

        只有"仍在关注"且"拍卖尚未终结"的拍品才需要。
        """
        return self.user_query is UserQuery.FOLLOWING and self.door_state in (
            DoorState.ACTIVE,
            DoorState.INVALID,
        )


@dataclass(frozen=True, slots=True)
class Observation:
    """一次列表采集看到的拍品。"""

    asin: str
    name: str
    url: str
    latest_bid: float | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Transition:
    """一次状态迁移的记录。

    Attributes:
        kind: 迁移种类。
        asin: 拍品号。
        detail: 给人看的说明（只用于日志）。
        old_end_at: 仅 :attr:`TransitionKind.END_TIME_CHANGED` 有意义：变化前
            的结束时间。
        new_end_at: 同上，变化后的结束时间。

    ``old_end_at`` / ``new_end_at`` 是**结构化**的另一半：``detail`` 里那串
    ``"<旧 iso> -> <新 iso>"`` 是为日志写的，格式随时可能调整。要判断"这是
    延后还是提前"，比对这两个 datetime 就行，不必去解析自己拼的字符串。
    """

    kind: TransitionKind
    asin: str
    detail: str = ""
    old_end_at: datetime | None = None
    new_end_at: datetime | None = None
