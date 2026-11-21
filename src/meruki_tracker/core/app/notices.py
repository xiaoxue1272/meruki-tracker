"""通知板：右侧「通知队列」侧栏的唯一数据源。

用户 2026-11-10 拍板（m14435 / m14439 / m14963）：

- **所有通知都注册进来**，两类来源：
  A. 结束提醒——按关注拍品的结束时间预注册「结束前 N 分钟提醒」，``due_at``
     在未来。N 由配置 ``notify.points_minutes`` 给，是一组**显式的点**
     （``6 小时 / 3 小时 / 1 小时 / 15 分钟`` 这样），不是"从某点起每隔多久
     算一次"。与详情复查点 ``detail.recheck_before_end_minutes`` 形态同构、
     用途不同（那个用来拉详情确认结束时间，这个用来提醒你）；
  B. 出价被超过——检测到的当下注册，``due_at`` = 检测时刻（立刻到期）。
- **侧栏只展示未来 ``notify.window_minutes`` 内将要通知的事件**
  （默认 :data:`NOTICE_WINDOW`，用户 m21460 之后可配）。
- 默认不往外发：到点只是把 :class:`~.notifications.Notification`
  交给 ``Notifier``（没启用渠道时是 ``NullNotifier``，什么都不发），界面闪一条 ``_notice``。

刻意**不起线程、不起定时器**：本模块只是带 ``due_at`` 的数据表 + 一个
``take_due`` 扫描。触发靠调用方周期扫描——TUI 现成的 1Hz tick 就够
（提醒精度是分钟级，1 秒误差无意义；毫秒级轮询只是白白多唤醒）。
到点触发也**绝不占用后台任务**：在那 ``sleep`` 两小时会把后面的动作全
堵死，违反「不影响系统自动操作」。
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..domain.models import AuctionState, DoorState, TimeState, UserQuery
from .notifications import Notification, Notifier, NullNotifier

log = logging.getLogger(__name__)

#: 侧栏窗口的默认值：只展示「未来 2 小时内将要通知」的事件（用户 m14435）。
#: 现在由 ``notify.window_minutes`` 覆盖，这里只是构造 :class:`NoticeBoard`
#: 时的兜底（测试与旧调用点仍按 2 小时）。
NOTICE_WINDOW = timedelta(hours=2)

#: 一条拍品最多注册多少个提醒点。配置是一组手填的数，理论上限无害，但
#: 一个人真往里塞几千个点时，界面与扫描都白白变慢；超出就**只留最接近结束
#: 的那批**（越近越值钱）。
MAX_ENDING_POINTS = 60

#: 「最近发生」最多留几条。它是给侧栏回看用的窗口，不是历史账本——真正的
#: 历史在 ``changes`` 表里。留太多只会把侧栏挤满。
RECENT_MAX = 12

#: 通知种类。
#:
#: 前两种是「将要发生」（``due_at`` 在未来，进侧栏的队列段）；
#: 后四种是「已经发生」（注册即到期，只闪一下并进侧栏的最近段）。
KIND_ENDING = "ending"  # 结束前 N 分钟提醒（A，预注册、due_at 在未来）
KIND_OUTBID = "outbid"  # 出价被超过（B，注册即到期）
KIND_ENDED = "ended"  # 拍卖结束，带成交价
KIND_EXTENDED = "extended"  # 截止时间被延后
KIND_SHORTENED = "shortened"  # 截止时间被提前

#: 已发生（立刻到期）的种类，界面据此决定画在侧栏哪一段。
EVENT_KINDS = (KIND_OUTBID, KIND_ENDED, KIND_EXTENDED, KIND_SHORTENED)


@dataclass(frozen=True, slots=True)
class Notice:
    """一条已注册的通知。

    Attributes:
        kind: :data:`KIND_ENDING` / :data:`KIND_OUTBID` / :data:`KIND_ENDED` /
            :data:`KIND_EXTENDED` / :data:`KIND_SHORTENED`。
        asin: 相关拍品。
        title: 一行标题（投递时直接用作标题/主题）。
        body: 正文（拍品名 + 事件）。
        due_at: 触发时刻（UTC）。结束提醒 = 结束时间 - N 分钟；
            事件类（出价被超过 / 结束 / 时间变化）= 检测时刻，立刻到期。
        minutes_before_end: 仅结束提醒有意义：提前多少分钟（用于界面文案）。
    """

    kind: str
    asin: str
    title: str
    body: str
    due_at: datetime
    minutes_before_end: float = 0.0


class NoticeBoard:
    """带 ``due_at`` 的通知表。只持数据，触发靠 :meth:`take_due` 被周期调用。

    幂等性靠三套键保证：结束提醒按 ``(asin, due_at)`` 去重（同一拍品同一
    复查点只有一条；``sync_ending`` 每轮全量重算，结束时间变了旧条目自然
    消失）；出价事件按 ``msg_id`` / ``("price", asin)`` 去重；状态变化事件
    按调用方给的 ``(kind, asin, 标记)`` 去重。
    """

    def __init__(
        self, *, window: timedelta = NOTICE_WINDOW, notifier: Notifier | None = None
    ) -> None:
        self._window = window
        self._notifier: Notifier = notifier or NullNotifier()
        self._ending: dict[tuple[str, datetime], Notice] = {}
        self._outbid: dict[int, Notice] = {}
        # 价格比对出来的「出价被超过」按 asin 去重（同一条拍品同时只留一条）。
        # 与站内信那条（按 msg_id）刻意分开：两条渠道可能对同一条拍品各报一次，
        # 去重责任在上游（refresher 记「已经因为哪个出价提醒过」），这里只保证
        # 同一轮不会重复注册。键用元组是为了和 _outbid 的 int 键区分开。
        self._price_outbid: dict[tuple[str, str], Notice] = {}
        # 状态变化类事件（结束 / 时间延后 / 时间提前）。键由调用方给
        # ``(kind, asin, 去重标记)``——同一件事只该响一次，而"同一件事"的定义
        # 只有调用方知道（延后到同一个新时间才算同一件事，再延一次是新的）。
        self._events: dict[tuple[str, str, str], Notice] = {}
        # 刚发生过的事件，给侧栏「最近」段回看。注意它存的是**副本引用**：
        # ``take_due`` 会把 _events 里的条目删掉，而这里仍留着——这正是
        # "已投递"和"还能回看"两件事的区别。
        self._recent: deque[Notice] = deque(maxlen=RECENT_MAX)

    def set_window(self, window: timedelta) -> None:
        """改侧栏窗口（``notify.window_minutes`` 在配置页里改完立刻生效）。"""
        self._window = window

    # ---------------------------------------------------------------- 注册

    def sync_ending(
        self,
        states: Iterable[AuctionState],
        *,
        points_minutes: Iterable[float],
        now: datetime,
    ) -> None:
        """按最新快照全量重算「结束前 N 分钟」提醒（A 类）。

        只保留还没到点的：到点的由 :meth:`take_due` 触发，错过的（程序没在
        跑）静默丢弃——补发一条"15 分钟前该提醒你"没有意义。每轮刷新后调
        一次即可，天然跟随结束时间变化 / 取关 / 提前结束。
        """
        points = normalize_points(points_minutes)
        rebuilt: dict[tuple[str, datetime], Notice] = {}
        for st in states:
            if st.user_query is not UserQuery.FOLLOWING:
                continue
            if st.door_state is not DoorState.ACTIVE:
                continue
            if st.end_time.state is not TimeState.EXACT or st.end_time.at is None:
                continue
            for minutes in points:
                due = st.end_time.at - timedelta(minutes=minutes)
                if due <= now:
                    continue
                key = (st.asin, due)
                rebuilt[key] = Notice(
                    kind=KIND_ENDING,
                    asin=st.asin,
                    title=f"{_fmt_minutes(minutes)}后结束",
                    body=st.name,
                    due_at=due,
                    minutes_before_end=minutes,
                )
        self._ending = rebuilt

    def add_outbid(self, *, msg_id: int, asin: str, title: str, now: datetime) -> None:
        """注册一条「出价被超过」（B 类）：立刻到期，下一次扫描就触发。"""
        if msg_id in self._outbid:
            return
        self._outbid[msg_id] = Notice(
            kind=KIND_OUTBID,
            asin=asin,
            title="出价被超过",
            body=f"{title}（拍品 {asin}）",
            due_at=now,
        )

    def add_price_outbid(
        self,
        *,
        asin: str,
        my_bid: float,
        current_bid: float | None,
        title: str,
        now: datetime,
    ) -> bool:
        """注册一条「价格比对」发现的出价被超过：立刻到期。

        与 :meth:`add_outbid`（站内信驱动）是**两条独立信号**，都要保留：

        - 站内信由服务端判定，最准，但**会漏发**——实测有订单最终价高于我的
          出价、log 里却只有「价格不足」，没有「出价被超过」；
        - 价格比对每轮都做，不会漏，但只能**正向**用（``当前价 > 我的出价``
          一定意味着被超过），不能反向推断"没被超过"——输掉也可能是因为被
          一口价买走、出价被判价格不足、卖家取消（实测 8 条输掉的里有 2 条
          最终价低于我的出价）。

        所以两边都只负责"报警"，不做"没被超过"的结论。

        Args:
            asin: 拍品号。
            my_bid: 我的出价（日元）。
            current_bid: 本轮详情里的当前价（日元）；没有详情时为 ``None``
                （例如"我只出价、没关注"的拍品——由订单状态判定的情况）。
            title: 拍品名（界面展示用）。
            now: 检测时刻。

        Returns:
            真的注册了新条目返回 ``True``（同一拍品已有未触发的条目 → ``False``）。
        """
        key = ("price", asin)
        if key in self._price_outbid:
            return False
        if current_bid is None:
            body = f"{title}（我的出价 ¥{my_bid:,.0f}，已被超过）"
        else:
            body = f"{title}（当前 ¥{current_bid:,.0f} / 我的出价 ¥{my_bid:,.0f}）"
        self._price_outbid[key] = Notice(
            kind=KIND_OUTBID,
            asin=asin,
            title="出价被超过",
            body=body,
            due_at=now,
        )
        return True

    def add_event(
        self,
        *,
        key: tuple[str, str, str],
        kind: str,
        asin: str,
        title: str,
        body: str,
        now: datetime,
    ) -> bool:
        """注册一条「已经发生」的事件（拍卖结束 / 截止时间延后 / 提前）。

        这类事件与「结束前 N 分钟提醒」的区别不是轻重，而是**时态**：前者是
        "已经这样了"，后者是"再过 N 分钟就要这样"。所以事件的 ``due_at``
        就是注册时刻，下一次 :meth:`take_due` 立刻投递并闪一下，同时进
        :meth:`recent` 供侧栏回看。

        去重键由调用方给：同一拍品结束只该响一次（终态，不会再来第二次），
        而「延后到 14:00」和「又延后到 15:00」是两件不同的事——只有调用方
        知道哪种算同一件事，所以不在这里猜。

        Args:
            key: 去重键 ``(kind, asin, 标记)``，标记通常放"新时间"或空串。
            kind: 事件种类，见 :data:`EVENT_KINDS`。
            asin: 拍品号。
            title: 一行标题。
            body: 正文。
            now: 事件发生（= 检测到）的时刻。

        Returns:
            真的注册了新条目返回 ``True``；同一 ``key`` 已经注册过 → ``False``。
        """
        if key in self._events:
            return False
        note = Notice(kind=kind, asin=asin, title=title, body=body, due_at=now)
        self._events[key] = note
        # 同时进「最近」：即便下一刻就被 take_due 收走，侧栏也还看得到。
        self._recent.append(note)
        return True

    # ---------------------------------------------------------------- 读取

    def recent(self) -> tuple[Notice, ...]:
        """刚发生过的通知，**新的在前**。

        与 :meth:`snapshot` 是两个方向：那个回答"接下来会发生什么"，这个回答
        "刚刚发生了什么"。混在一起会让"还有 15 分钟结束"和"已经结束了"看起来
        是同一种东西。
        """
        return tuple(reversed(self._recent))

    def snapshot(self, now: datetime) -> tuple[Notice, ...]:
        """未来 ``window`` 内**将要**通知的事件，按触发时刻升序。

        已过 ``due_at`` 但还没被 :meth:`take_due` 收走的不在这里——它们是
        "该响的"，不是"将要响的"，混进侧栏会把窗口语义搅浑。
        """
        horizon = now + self._window
        items = [n for n in self._all_registered() if now < n.due_at <= horizon]
        items.sort(key=lambda n: (n.due_at, n.asin))
        return tuple(items)

    def pending_count(self, now: datetime) -> int:
        """将要通知 + 已到点还没触发的总条数（供 ticker 判断要不要每秒扫）。"""
        horizon = now + self._window
        return sum(1 for n in self._all_registered() if n.due_at <= horizon)

    def take_due(self, now: datetime) -> tuple[Notice, ...]:
        """取走所有已到点的通知：逐条投递 ``Notifier`` 并返回，供界面闪提示。

        投递失败绝不影响刷新/界面（协议要求实现方吞异常，这里再兜一层）。
        """
        due = [n for n in self._all_registered() if n.due_at <= now]
        if not due:
            return ()
        for n in due:
            if n.kind == KIND_ENDING:
                self._ending.pop((n.asin, n.due_at), None)
            else:
                # 出价三张表 + 事件表键各不相同，统一按值反查一次即可。
                # （``_recent`` 不在这里删：它是"回看窗口"，收走不等于没发生。）
                for store in (self._outbid, self._price_outbid, self._events):
                    for key, stored in list(store.items()):
                        if stored is n:
                            del store[key]
                            break
            try:
                self._notifier.notify(
                    Notification(title=n.title, body=n.body, asin=n.asin, kind=n.kind)
                )
            except Exception:
                log.warning("通知投递失败（已忽略）: %s", n.asin)
        due.sort(key=lambda n: (n.due_at, n.asin))
        return tuple(due)

    def _all_registered(self) -> tuple[Notice, ...]:
        """所有注册表拍平成一个元组（读取/扫描口径只留一份）。

        ``_recent`` 故意不在里面：它装的是**已经收走**的条目，再参与一次
        ``take_due`` 就会重复投递。
        """
        return (
            *self._ending.values(),
            *self._outbid.values(),
            *self._price_outbid.values(),
            *self._events.values(),
        )


def normalize_points(points_minutes: Iterable[float]) -> tuple[float, ...]:
    """把一组"距结束多少分钟"归一成 **从远到近、无重复、不超过上限**。

    用户 2026-11-14 要求"按时间大小排序去重"：手填 ``6 分钟 / 15 分钟 /
    1 小时 / 3 小时 / 6 小时``，界面和提醒都按 ``6 小时 → 6 分钟`` 呈现。
    归一放在这里而不是只放在配置层，是因为调用方可能直接传元组（测试、
    refresher 的内存覆盖），口径必须只有一个。

    Args:
        points_minutes: 任意顺序、可能带重复或非正数的点。

    Returns:
        降序去重后的点；全是垃圾时返回 ``()``（调用方据此判"没有提醒点"）。
    """
    numbers = []
    for raw in points_minutes:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            numbers.append(round(value, 3))
    # set 去重后再排序：同一分钟被填两遍不该响两次。
    # 超上限时**先按升序截前 60 个**（= 最靠近结束的那批，越近越值钱），
    # 再翻转成降序给界面——不能反过来截，那会砍掉最该响的几个点。
    kept = sorted(set(numbers))[:MAX_ENDING_POINTS]
    return tuple(sorted(kept, reverse=True))


def _fmt_minutes(minutes: float) -> str:
    """把提前分钟数说成人话：15 → ``15 分钟``，360 → ``6 小时``。"""
    if minutes >= 60 and minutes % 60 == 0:
        return f"{int(minutes // 60)} 小时"
    return f"{int(minutes)} 分钟"
