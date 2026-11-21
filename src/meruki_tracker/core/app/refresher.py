"""刷新用例：一次完整采集。

流程（设计文档 §4.1）：
1. 拉列表（分页，任一页失败即整体失败）
2. 状态推进 advance()
3. 按优先级补详情 apply_detail()
4. 原子写快照

失败于第 1 步 -> 只更新 last_attempt，不写 auctions。

列表完整性有两种"不完整"，语义不同：
- 字段完整但 items 为空：用户真的没关注（接口正常返回空就是真的空）→ 完整，照常取关
- 缺分页字段 / 翻页到上限：数据不可信 → 不完整，本轮不做取关判定
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..adapters.meruki.api import STATUS_ONGOING, DetailInfo, ListItem, MerukiApi, MyBid
from ..adapters.store.credentials import CredentialStore
from ..adapters.store.messages import MessageRow, MessageStore
from ..adapters.store.mirror import MirrorStore, StoredDetail, StoredListItem
from ..adapters.store.observations import ObservationStore
from ..adapters.store.snapshot import LastAttempt, SchemaVersionError, Snapshot, SnapshotStore
from ..deadline import SITE_TZ, normalize_deadline
from ..domain.detail import DetailResult, apply_detail
from ..domain.diff import Change, diff_detail_rounds, diff_list_rounds, summarize
from ..domain.lifecycle import advance
from ..domain.messages import MessageNotice, parse_bid_events, parse_messages, pick_new
from ..domain.models import (
    AuctionState,
    DoorState,
    Observation,
    Transition,
    TransitionKind,
    UserQuery,
)
from ..errors import (
    AuthExpiredError,
    MerukiTrackerError,
    NetworkError,
    ProtocolChangedError,
    WafBlockedError,
)
from ..logging_setup import get_logger
from .notices import (
    KIND_ENDED,
    KIND_EXTENDED,
    KIND_SHORTENED,
    NoticeBoard,
    normalize_points,
)
from .notifications import Notification, Notifier, NullNotifier

if TYPE_CHECKING:
    from ..config import Config

log = get_logger("app.refresher")

#: 「我的竞拍」最多翻几页（每页 10 条 → 50 个同时出价的拍品）。
#: 上限是为了别让一个异常响应的巨大 TotalPage 把一轮刷新的请求数拖爆。
_MY_BIDS_MAX_PAGES = 5

#: 站内消息最多回溯几页（每页 10 条 → 30 条）。只在"积压还没回溯到水位线"
#: 时才会多翻，正常每轮就是一次请求。
_MESSAGES_MAX_PAGES = 3


def _as_int(value: Any, default: int) -> int:
    """宽容地取一个整数字段：读不懂就用默认值（不抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fmt_clock(at: datetime) -> str:
    """按站点时区（+08:00）格式化成 ``MM-DD HH:MM``。

    通知正文里直接显示 UTC 会差 8 小时——用户看到的"延后到 06:00"和界面上
    列表里的"14:00"对不上，比不写时间更糟。
    """
    return at.astimezone(SITE_TZ).strftime("%m-%d %H:%M")


#: 观测历史清理节流间隔（与 docstring 一致：每天最多清理一次）
_PRUNE_INTERVAL = timedelta(days=1)

ProgressFn = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class DetailStats:
    """一轮详情拉取的结果。

    以前这里是裸的 ``(成功数, 失败数)``：四个方法都返回它，调用方一路
    ``ok += a; fail += b`` 手工累加。两个连续 ``int`` 的位置写反了编译器不吭声，
    读代码也看不出 ``(3, 1)`` 是"3 成 1 败"还是"3 败 1 成"。
    """

    ok: int = 0
    failed: int = 0

    def __add__(self, other: DetailStats) -> DetailStats:
        return DetailStats(self.ok + other.ok, self.failed + other.failed)


@dataclass(frozen=True, slots=True)
class RefreshStats:
    listed: int = 0
    details_ok: int = 0
    details_failed: int = 0
    transitions: tuple[Transition, ...] = ()


@dataclass(frozen=True, slots=True)
class RefreshResult:
    ok: bool
    error: str = ""
    error_kind: str = ""
    stats: RefreshStats = field(default_factory=RefreshStats)


@dataclass(frozen=True, slots=True)
class RefresherOptions:
    """决定「一次刷新怎么跑」的策略标量——全部来自 :class:`~core.config.Config`。

    以前这些是 ``Refresher.__init__`` 里十几个并列的关键字参数，读构造代码时
    分不清哪些属于同一类东西，也看不出「一次刷新由哪些配置决定」。收成一个类型
    后，"哪些配置真正影响刷新"变成可读的清单；协作者（api / 各种 store）仍单独传，
    不塞进来——它们是依赖，不是策略。

    顺手删掉了 ``retry_backoff_seconds``：它以前存进 ``self._backoff``，但全仓
    没有任何一处读取（审计 A4「存了但永久不用的死参数」）。重试只在 API 层做。
    """

    page_size_expected: int = 10
    detail_enabled: bool = True
    observations_retention_days: int = 90
    messages_enabled: bool = True
    #: 结束提醒的复查点（``notify.points_minutes``）：距结束这些分钟各提醒一次。
    notify_points_minutes: tuple[float, ...] = (15.0, 60.0, 360.0)
    #: 「出价被超过」是否额外用价格比对判断（``notify.outbid_by_price``）。
    outbid_by_price: bool = True
    #: 事件类通知的三个开关（``notify.ended`` / ``notify.extended`` /
    #: ``notify.shortened``）。分三个而不是一个总开关，是因为它们的打扰程度
    #: 差很多：结束是"该看成交价了"，延后多半是好消息，提前则要立刻出手——
    #: 有人只想要其中一个。
    notify_ended: bool = True
    notify_extended: bool = True
    notify_shortened: bool = True

    @classmethod
    def from_config(cls, cfg: Config) -> RefresherOptions:
        return cls(
            page_size_expected=cfg.fetch.page_size_expected,
            detail_enabled=cfg.detail.enable,
            observations_retention_days=cfg.storage.observations_retention_days,
            messages_enabled=cfg.messages.enable,
            notify_points_minutes=cfg.notify.points_minutes,
            outbid_by_price=cfg.notify.outbid_by_price,
            notify_ended=cfg.notify.ended,
            notify_extended=cfg.notify.extended,
            notify_shortened=cfg.notify.shortened,
        )


class Refresher:
    """单账号刷新编排——系统里唯一改变状态的入口。"""

    def __init__(
        self,
        *,
        api: MerukiApi,
        snapshots: SnapshotStore,
        credentials: CredentialStore,
        observations: ObservationStore,
        options: RefresherOptions | None = None,
        mirror: MirrorStore | None = None,
        messages: MessageStore | None = None,
        notifier: Notifier | None = None,
        notices: NoticeBoard | None = None,
    ) -> None:
        opt = options or RefresherOptions()
        self._api = api
        self._snapshots = snapshots
        self._credentials = credentials
        self._observations = observations
        # 接口镜像（全字段留证 + 逐轮比对）。None 时退化成旧行为，便于测试。
        self._mirror = mirror
        # 站内消息通道（「出价被超过」）——服务端算好的事实，比自己猜价格准。
        # messages 为 None 时整块跳过，旧测试不受影响。
        self._messages = messages
        self._messages_enabled = bool(opt.messages_enabled)
        # 通知通道：由 Config.channels 决定；没启用任何渠道时是 NullNotifier
        self._notifier: Notifier = notifier or NullNotifier()
        # 通知板（右侧「通知队列」侧栏的数据源）。None 时退回旧行为：
        # 「出价被超过」直接走 notifier，结束提醒不注册——旧测试不受影响。
        self._notices = notices
        self._page_size = opt.page_size_expected
        # 重试只在 API 层做（fetch.retry）：这一层再重试一次会把实际请求数放大成
        # 平方级，也把"一次刷新打了几次接口"变得无法估算。
        self._detail_enabled = opt.detail_enabled
        # 结束提醒的复查点（分钟，从远到近）。归一在这里做：调用方可能直接
        # 塞元组（配置/测试），口径只留一份。
        self._notify_points = normalize_points(opt.notify_points_minutes)
        # 价格比对开关（notify.outbid_by_price）。关掉后只剩站内信那一条信号。
        self._outbid_by_price = bool(opt.outbid_by_price)
        # 「已经因为我的哪个出价提醒过了」：asin -> 我的出价。
        # 每轮都满足 ``当前价 > 我的出价`` 时不能每轮都响；我改了出价（值变了）
        # 才允许再响一次。反过来，一旦不再被超过就清掉，下次被超还能响。
        self._price_outbid_seen: dict[str, float] = {}
        # 事件类通知的开关（notify.ended / extended / shortened）。
        self._notify_ended = bool(opt.notify_ended)
        self._notify_extended = bool(opt.notify_extended)
        self._notify_shortened = bool(opt.notify_shortened)
        self._retention = timedelta(days=max(1, int(opt.observations_retention_days)))
        self._last_prune: datetime | None = None
        self._running = False
        self._detail_urls: dict[str, str] = {}

    @property
    def running(self) -> bool:
        return self._running

    async def refresh(
        self,
        previous: Snapshot,
        progress: ProgressFn | None = None,
    ) -> tuple[Snapshot, RefreshResult]:
        """执行一次刷新。

        每轮都**把关注中的每一条都拉一遍详情**，结束与否只看详情（用户
        2026-11-15 定调）。以前的"按复查点挑几条"与"用已结束列表反查是否
        结束"两条旁证路线都已删掉——它们都是在猜，而详情里有权威答案。

        Args:
            previous: 上一次快照（失败时保留其 auctions）。
            progress: 进度回调。

        Returns:
            新快照与刷新结果。
        """
        if self._running:
            return previous, RefreshResult(ok=False, error="正在采集中", error_kind="busy")

        # 读凭证可能是**故障**（库表损坏 / 磁盘满 / 连接已关），不只是"没登录"。
        # 两者必须分开说：报成 auth 会把用户指去重新登录，而登录也会失败。
        try:
            cookie = self._credentials.load()
        except OSError as exc:
            log.warning("读取登录凭证失败: %s", exc)
            return previous, RefreshResult(ok=False, error=str(exc), error_kind="storage")
        if not cookie:
            return previous, RefreshResult(ok=False, error="还没有登录", error_kind="auth")

        self._running = True
        now = datetime.now(UTC)
        try:
            # 站内消息先查：它是「出价被超过」的权威信号（服务端算好的），
            # 拿到的拍品在本轮详情阶段会被强制拉一次，不用等常规复查点。
            # 放在列表之前，是因为它是**最便宜**的一步：一次请求 10 条消息。
            outbid_asins, outbid_rows, outbid_mark = await self._fetch_outbid()

            listed = await self._fetch_list(progress)
            states = {a.asin: a for a in previous.auctions}

            # 逐轮比对：上一轮的接口镜像 vs 本轮。产出的是**可回放的变更事件**，
            # 不是"猜出来"的状态迁移——"它从列表里不见了"在这里只记事件，
            # 到底结束了没有由详情回答（见下）。
            prev_items = self._mirror_items()
            cur_items = {it.asin: _item_mirror_row(it) for it in listed.raw_items}
            changes = diff_list_rounds(prev_items, cur_items, complete=listed.complete)

            new_state, transitions = advance(states, listed.items, now)

            self._detail_urls.update(listed.detail_urls)

            detail_rows: list[dict[str, Any]] = []
            stats = DetailStats()
            if self._detail_enabled:
                pulled: set[str] = set()
                # force：消息指出「有人超过你的出价」，这几个拍品插队拉详情
                stats += await self._fill_details(
                    new_state,
                    transitions,
                    progress,
                    detail_rows,
                    force=outbid_asins,
                    pulled=pulled,
                )
            # 第二条「出价被超过」信号：拿本轮刚拉到的当前价和我的出价比。
            # 站内信会漏发（实测有订单输了、log 里却只有「价格不足」），这条不会；
            # 代价是每轮多一次「我的竞拍」列表请求，且只能覆盖**你在关注**的拍品。
            await self._detect_outbid_by_price(detail_rows, new_state, now)
            prev_details = self._mirror_details()
            changes += diff_detail_rounds(
                prev_details, {r["asin"]: r for r in detail_rows if r.get("asin")}
            )

            snap = replace(
                previous,
                generated_at=now,
                last_attempt=LastAttempt(at=now, ok=True, error=""),
                auctions=tuple(sorted(new_state.values(), key=lambda a: a.asin)),
            )
            # 这里允许清空：用户把关注全部取消是合法状态，且本轮列表是**完整**
            # 观测（不完整时 advance 不会产生取关迁移，auctions 不会被清空）。
            self._snapshots.write(snap, allow_empty=True)
            self._write_mirror(listed, cur_items, detail_rows, changes, now)
            if self._messages is not None and (outbid_rows or outbid_mark):
                # 事件与水位线**同一个事务**：先写事件、再推水位线，中间崩了
                # 最多重复解析一次（唯一索引挡掉）；反过来推会永久丢消息。
                added = self._messages.save_round(outbid_rows, seen_at=now, watermark=outbid_mark)
                if added:
                    log.info("记录 %d 条出价事件（含「出价被超过」）", added)
            self._observations.commit()
            self._maybe_prune(now)
            # 通知板：出价事件注册（B 类，立刻到期）+ 按最新快照重算
            # 「结束前 N 分钟」提醒（A 类）。当前通道是空实现，绝不影响刷新。
            self._notify_outbid(outbid_rows, now=now)
            # 事件类通知（结束 / 时间延后 / 提前）：状态机的迁移记录本来就带着
            # 这两件事，以前只用来打日志计数。放在 sync_ending 之前，是因为
            # 结束的拍品下一秒就该从"将要提醒"里消失。
            self._notify_events(new_state, transitions, now, detail_rows=detail_rows)
            if self._notices is not None:
                self._notices.sync_ending(
                    new_state.values(),
                    points_minutes=self._notify_points,
                    now=now,
                )
            self._log_refresh_done(listed, changes, stats, transitions)
            return snap, RefreshResult(
                ok=True,
                stats=RefreshStats(
                    listed=len(listed.items),
                    details_ok=stats.ok,
                    details_failed=stats.failed,
                    transitions=tuple(transitions),
                ),
            )
        except MerukiTrackerError as exc:
            return self._refresh_failure(previous, now, exc)
        except Exception as exc:
            return self._refresh_failure(previous, now, exc)
        finally:
            self._running = False

    def _log_refresh_done(
        self,
        listed: _ListResult,
        changes: list[Change],
        stats: DetailStats,
        transitions: list[Transition],
    ) -> None:
        """拼装成功日志。主干只负责「什么时候记」，这里负责「记什么」。

        ``summarize(changes)`` 把本轮镜像比对的变更事件压成一句人话；它和
        ``stats``/``transitions`` 的计数只是展示，抽出来不影响任何状态迁移。
        """
        log.info(
            "刷新完成：列表 %d 条（%s），详情 成功%d/失败%d，迁移 %d 条",
            len(listed.items),
            summarize(changes),
            stats.ok,
            stats.failed,
            len(transitions),
        )

    def _refresh_failure(
        self, previous: Snapshot, now: datetime, exc: BaseException
    ) -> tuple[Snapshot, RefreshResult]:
        """把两个 ``except`` 分支共用的收尾集中到一处，**主干框架不动**。

        两条路径的落盘与返回语义有意不对称，必须原样保留：
        - ``MerukiTrackerError``：按类型分类（``_classify``）写 ``log.warning``，
          ``LastAttempt.error`` 就是 ``str(exc)``；
        - 其它异常：记 ``log.exception``（在 except 内调用仍能抓到原 traceback），
          ``LastAttempt.error`` 带 ``类型: 消息`` 前缀，但 ``RefreshResult.error``
          两条路径都只回 ``str(exc)``，不带前缀。
        失败态快照照旧交给 ``_write_failed_snapshot``，写盘失败不得顶替原始错误。
        """
        if isinstance(exc, MerukiTrackerError):
            kind = _classify(exc)
            log.warning("刷新失败(%s): %s", kind, exc)
            last_error = str(exc)
        else:
            kind = "internal"
            log.exception("刷新未预期异常")
            last_error = f"{type(exc).__name__}: {exc}"
        failed = replace(previous, last_attempt=LastAttempt(at=now, ok=False, error=last_error))
        self._write_failed_snapshot(failed)
        return failed, RefreshResult(ok=False, error=str(exc), error_kind=kind)

    def _write_failed_snapshot(self, failed: Snapshot) -> None:
        """落盘失败态快照；**写盘本身失败绝不能覆盖原始错误**。

        这里已经在 except 分支内：若 ``write`` 抛 OSError（磁盘满 / 只读 / 权限），
        它会把用户真正关心的失败原因（凭证失效、WAF、网络不通）替换成一个
        和排查无关的 IO 错误。所以写盘失败只记日志，让原始异常正常返回。
        """
        try:
            self._snapshots.write(failed)
        except (OSError, SchemaVersionError):
            # SchemaVersionError：库是更晚的版本建的，写请求被快照层挡下了。
            # 这里同样只记日志——真正该让用户看到的错误是原始那个。
            log.warning("失败态快照落盘失败（不影响本次错误上报）", exc_info=True)

    # ---------------------------------------------------------------- 接口镜像

    def _mirror_items(self) -> dict[str, dict[str, Any]]:
        """读上一轮**同一个分桶**的列表镜像（比对基准）。镜像层不可用时降级为空。

        只读「进行中」桶：本轮比对的是"这轮关注列表 vs 上轮关注列表"，而库里
        还躺着另一个桶（「已结束」，历史上翻到的几百条）。两边不按桶对齐的话，
        每个「已结束」条目都会被算成"上一轮有、这一轮没了"，于是**每一轮都
        刷出几百条假的 ``disappeared`` 事件**（实测 548 条/轮、248 轮共 13 万行）。
        """
        if self._mirror is None:
            return {}
        try:
            return {
                it.asin: _stored_item_row(it)
                for it in self._mirror.list_items(status=STATUS_ONGOING)
            }
        except Exception:  # 上一轮读不到不该让整轮刷新失败
            log.warning("读取列表镜像失败，本轮跳过比对", exc_info=True)
            return {}

    def _mirror_details(self) -> dict[str, dict[str, Any]]:
        if self._mirror is None:
            return {}
        try:
            return {a: _stored_detail_row(d) for a, d in self._mirror.detail_items().items()}
        except Exception:
            log.warning("读取详情镜像失败，本轮跳过比对", exc_info=True)
            return {}

    def _write_mirror(
        self,
        listed: _ListResult,
        cur_items: dict[str, dict[str, Any]],
        detail_rows: list[dict[str, Any]],
        changes: list[Change],
        now: datetime,
    ) -> None:
        """把本轮接口数据整体落进镜像表。

        镜像落库失败**不影响**本轮刷新的成功判定：快照（用户真正看的东西）
        已经写好了，镜像只是审计用的旁证。这里只记日志。

        Args:
            listed: 本轮列表结果（决定 status 分桶与是否整桶替换）。
            cur_items: asin -> 镜像行。
            detail_rows: 本轮拿到的详情行。
            changes: 本轮比对产出的变更事件。
            now: 采集时刻。
        """
        if self._mirror is None:
            return
        status = listed.pages[0].get("status") if listed.pages else None
        try:
            self._mirror.save_round(
                status=str(status or STATUS_ONGOING),
                pages=listed.pages,
                items=list(cur_items.values()),
                details=detail_rows,
                changes=[c.as_row() for c in changes],
                seen_at=now,
                # 列表不完整时**不能清空**：残缺列表不代表别的拍品消失了
                replace=listed.complete,
            )
        except Exception:
            log.warning("接口镜像落库失败（不影响刷新结果）", exc_info=True)

    # ---------------------------------------------------------------- 列表

    async def _fetch_list(self, progress: ProgressFn | None) -> _ListResult:
        """拉「进行中」列表（关注中的拍品都在这里）。

        **拉全量**：不设页数上限（用户 2026-11-15：「关注列表去拉取全量的拍品」）。
        以前有个 ``fetch.list_max_pages`` 上限，翻到上限就按"本轮不完整"处理、
        不做任何终态判定——那是在替用户猜"你关注太多，我只看到这些"。既然终态
        改成由详情回答，列表就必须看全，否则连"有这个拍品"都不知道。
        """
        return await self._fetch_bucket(STATUS_ONGOING, None, progress)

    async def _fetch_bucket(
        self, status: str, max_pages: int | None, progress: ProgressFn | None
    ) -> _ListResult:
        """拉一个 status 分桶的全部分页。

        接口按 status 分桶返回：实测只有「进行中」与「已结束」是真实分桶，
        其他值（"全部" / "未开始" / "已取消"）一律静默回落到「已结束」。
        所以只发这两个值，不做无用的尝试。

        Args:
            status: 本桶名（目前只有 ``STATUS_ONGOING``；拉「已结束」桶查证的
                那条路线已随「以详情判结束」一起删掉）。
            max_pages: 最多翻几页；``None`` = 不限（拉全量）。
            progress: 进度回调。

        Returns:
            该桶的领域观测、完整性标志、详情地址表、接口原始条目与分页元信息。
        """
        now = datetime.now(UTC)
        items: list[ListItem] = []
        urls: dict[str, str] = {}
        pages: list[dict[str, Any]] = []
        page = 1
        complete = False
        while max_pages is None or page <= max_pages:
            if progress:
                progress(f"{status} 第 {page} 页 · 已拉 {len(items)} 条")
            # 缺分页字段时 fetch_list_page 抛 ProtocolChangedError（重试无意义，_with_retry 会直抛），
            # 于是本轮刷新失败 → 不会走到 advance() → 不产生取关判定。
            pg = await self._with_retry(lambda p=page: self._api.fetch_list_page(p, status=status))
            # 分页元信息照原样留一份：接口顶层 5 个字段此前全被丢弃
            pages.append(
                {
                    "status": pg.status,
                    "page": pg.current_page or page,
                    "item_per_page": pg.item_per_page,
                    "show_page_num": pg.show_page_num,
                    "current_page": pg.current_page,
                    "total_item": pg.total_item,
                    "total_page": pg.total_page,
                    "returned": len(pg.items),
                }
            )
            # 空页永远意味着"到底了"，无论 total_page 报多少（防御服务端分页字段自相矛盾）
            if not pg.items:
                complete = True
                break
            items.extend(pg.items)
            for it in pg.items:
                urls[it.asin] = it.detail_url
            # "本页没满 ⇒ 到底了"要按**接口自报的**每页条数判，不能按配置值：
            # 服务端哪天改成每页 20 条，按配置的 10 判就会在第 1 页误判收工，
            # 后面的人才被当成 disappeared（2026-11-18 审计）。
            per_page = pg.item_per_page or self._page_size
            if page >= pg.total_page or len(pg.items) < per_page:
                complete = True
                break
            page += 1
        if not complete and max_pages is not None:
            log.warning("%s 翻页达到上限 %d 页，本轮按不完整处理", status, max_pages)

        # 一次遍历同时构造内存观测与落库，避免对 items 重复扫描
        observations = [
            Observation(
                asin=it.asin,
                name=it.name,
                url=it.url,
                latest_bid=None,
                observed_at=now,
            )
            for it in items
        ]
        self._observations.add_many(
            {
                "asin": it.asin,
                "observed_at": now,
                "kind": "list",
                "end_time_raw": normalize_deadline(it.bid_deadline),
            }
            for it in items
        )
        return _ListResult(observations, complete, urls, items, pages)

    # ---------------------------------------------------------------- 站内消息

    async def _fetch_outbid(self) -> tuple[set[str], list[MessageRow], int]:
        """轮询站内消息，找出**新出现的**「出价被超过」候选。

        这是本项目最便宜的一条"价格变了"信号：一次 ``GetMessageWeb`` 拿 10 条
        消息，而拉一个拍品的详情要 22KB。服务端已经替我们判断了"有人超过你"。

        但**它不是唯一信号**：站内信会漏（实测 ``DEMOORD00003`` 订单的 log 只有
        「价格不足」，最终价却高于我的出价）。所以 2026-11-16 起还有一条用详情
        当前价比对的旁路（:meth:`_detect_outbid_by_price`），两条取或。

        **只对有出价的拍品有效**：没出过价就永远收不到这类消息。所以它不能
        替代常规轮询，只能作为额外触发器叠加在上面。

        失败一律降级为"这轮没有消息"——消息是锦上添花，不能因为它让整轮刷新
        失败（和 ``_probe_ended`` 同样的态度）。

        **水位线不在这里推进**：必须等事件行真的落库之后再推，否则崩在中间
        就会永久丢掉那几条消息（水位线过了、库里有没记录）。所以这里把该推
        到哪个值一并返回，由调用方在 ``save_events`` 之后推进。

        Returns:
            ``(需要立刻拉详情的 asin 集合, 待落库的事件行, 新的水位线)``。
            水位线为 0 表示不需要推进（本模块不可用 / 请求失败）。
        """
        if not self._messages_enabled or self._messages is None:
            return set(), [], 0

        since = self._messages.watermark()
        notices: list[MessageNotice] = []
        for page in range(1, _MESSAGES_MAX_PAGES + 1):
            try:
                data = await self._api.fetch_messages_raw(page)
            except MerukiTrackerError as exc:
                if page == 1:
                    log.warning("站内消息拉取失败，本轮跳过出价事件: %s", exc)
                    return set(), [], 0
                log.warning(
                    "站内消息第 %d 页拉取失败，按已拿到的 %d 条处理: %s", page, len(notices), exc
                )
                break
            rows = parse_messages(data)
            if not rows:
                break
            notices.extend(rows)
            # 已经回溯到水位线（或更早）就没必要再翻：水位线之下的消息本轮不看。
            # ``since <= 0`` 是首次接入，它只要最新那条 id，不回溯。
            oldest = min(n.msg_id for n in rows)
            if since <= 0 or oldest <= since + 1:
                break
            if page >= _as_int(data.get("TotalPage"), 1):
                break
        else:
            # 翻到上限还没回溯到水位线：只记一笔，别假装全看到了
            if notices and since > 0:
                log.warning(
                    "站内消息积压超过 %d 页，本轮只回溯到 id=%d（水位线 %d），更早的跳过",
                    _MESSAGES_MAX_PAGES,
                    min(n.msg_id for n in notices),
                    since,
                )

        if not notices:
            return set(), [], 0

        newest = max((n.msg_id for n in notices), default=0)
        if since <= 0:
            # 首次运行：只把现有最大 id 记成起点，**不回溯历史**。
            # 否则第一次刷新就会去拉几十个订单详情，纯属自找风控。
            log.info("站内消息首次接入，水位线从 %d 开始（不回溯历史消息）", newest)
            return set(), [], newest

        fresh = pick_new([n for n in notices if n.is_outbid], since_id=since)
        if not fresh:
            # 没有新的出价消息也要推进水位线：否则那些不关心的消息（交易/通知类）
            # 会让 pick_new 每次都要重新扫一遍，而且水位线永远不动。
            return set(), [], newest

        rows, affected = await self._resolve_outbid(fresh)
        return affected, rows, newest

    async def _resolve_outbid(
        self, notices: list[MessageNotice]
    ) -> tuple[list[MessageRow], set[str]]:
        """把「出价被超过」消息翻译成 asin，并顺带取回订单的完整事件流。

        标题里的 12 位是**订单短 ID**，不是 asin。要靠订单详情的 ``url``
        （hex 编码的雅虎地址）反解——这是唯一途径，``MessageDetailH5``
        返回的是 HTML 页面，没法当数据用。

        同一个订单可能连发好几条（实测一次竞拍里被超 6 次就有 6 条），
        所以先按 order_id 去重再请求，省下重复的订单详情请求。

        Args:
            notices: 新的出价消息（从旧到新）。

        Returns:
            ``(待落库的事件行, 受影响的 asin 集合)``。
        """
        rows: list[MessageRow] = []
        affected: set[str] = set()
        # 同一订单只查一次详情：连发多条时省掉重复请求
        resolved: dict[str, str] = {}

        for notice in notices:
            order_id = notice.order_id
            if not order_id:
                # 标题里抠不到订单号：只留一条没有 asin 的记录，供事后排查
                rows.append(
                    MessageRow(
                        msg_id=notice.msg_id,
                        event="出价被超过",
                        category=notice.category,
                        title=notice.title,
                        created_at=notice.created_at,
                    )
                )
                continue
            if order_id not in resolved:
                try:
                    detail = await self._api.fetch_order_detail_raw(order_id)
                except MerukiTrackerError as exc:
                    log.warning("订单 %s 详情拉取失败，这条出价消息先跳过: %s", order_id, exc)
                    resolved[order_id] = ""
                    continue
                try:
                    events = parse_bid_events(order_id, detail)
                except Exception:
                    # 一条订单解析崩了不能带走整轮刷新：水位线不推进的话，下一轮
                    # 还会撞同一订单，变成永久刷新失败循环。
                    log.warning("订单 %s 的事件解析失败，这条先跳过", order_id, exc_info=True)
                    resolved[order_id] = ""
                    continue
                if not events:
                    log.debug("订单 %s 解析不出事件（url 为空或 log 缺失）", order_id)
                    resolved[order_id] = ""
                    continue
                resolved[order_id] = events[0].asin
                # 订单的完整事件流一起落库：比只有一条「被超过」更有信息量，
                # 也能看出价格是怎么一路被抬上去的
                for ev in events:
                    rows.append(
                        MessageRow(
                            msg_id=notice.msg_id,
                            order_id=order_id,
                            asin=ev.asin,
                            event=ev.event,
                            happened_at=ev.happened_at,
                            category=notice.category,
                            title=notice.title,
                            created_at=notice.created_at,
                        )
                    )
                affected.add(events[0].asin)
                continue
            # 同一订单的第二条及以后：只记事件，不再请求
            asin = resolved[order_id]
            if asin:
                affected.add(asin)

        return rows, affected

    def _notify_outbid(self, events: list[MessageRow], *, now: datetime) -> None:
        """把出价事件交给通知板（有板）或直接交给通知通道（无板，旧行为）。

        有通知板时**只注册不投递**：注册即到期（``due_at = now``），下一次
        TUI tick 扫描 ``take_due`` 时才交给 ``Notifier`` 并在界面闪提示——
        「所有通知都要注册进入队列」（用户 m14435）。

        **没有启用渠道时是 NullNotifier——什么都不发**；启用了就由它投递。
        "先预留口子，后面统一做通知"。投递失败也绝不会影响刷新：协议要求
        实现方自己吞异常，这里再兜一层。
        """
        for row in events:
            if row.event != "出价被超过" or not row.asin:
                continue
            if self._notices is not None:
                self._notices.add_outbid(msg_id=row.msg_id, asin=row.asin, title=row.title, now=now)
                continue
            try:
                self._notifier.notify(
                    Notification(
                        title="出价被超过",
                        body=f"{row.title}（拍品 {row.asin}）",
                        asin=row.asin,
                        kind="outbid",
                    )
                )
            except Exception:
                log.warning("通知投递失败（已忽略）: %s", row.asin)

    # ------------------------------------------------------------ 事件类通知

    def _notify_events(
        self,
        states: dict[str, AuctionState],
        transitions: list[Transition],
        now: datetime,
        *,
        detail_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        """把状态变化汇报成通知：拍卖结束、截止时间延后 / 提前。

        这三件事此前**全都被算出来了**（:class:`TransitionKind` 的 ``ENDED`` 与
        ``END_TIME_CHANGED``），但只用来打一行日志、加一个计数。用户 m24274 的
        原话是「现在通知只有结束前提醒……还应该有结束提醒、拍卖时间延长提醒」。

        延后与提前必须分开说：延后是多给时间（可以慢慢来），提前是要立刻出手。
        方向比对的是 :attr:`Transition.old_end_at` / ``new_end_at`` 两个
        datetime，不去解析 ``detail`` 里给日志看的那串文本。

        去重键：
        - 结束 → ``(KIND_ENDED, asin, "")``：结束是终态，一次就够；
        - 时间变化 → ``(KIND_xxx, asin, 新时间的 iso)``：延到 14:00 和又延到
          15:00 是两件事，各自该响一次，但同一轮重复检测不会重复响。

        ``detail_rows`` 用来判断延后是不是雅虎的自动延长（``autoExtend``）。
        实测（2026-11-16）：``autoExtend`` 的 ``autoExtendHelp`` 原文是"结束前
        五分钟内当前价格有所上涨，结束时间将延长五分钟，可重复进行"。所以
        "延后 + 这个拍品开了自动延长"基本就是有人在抢——这跟卖家改主意完全是
        两件事，值得分开说。缺这一行数据时不标注，不猜。
        """
        auto_extend = {
            row["asin"]: row.get("auto_extend") for row in (detail_rows or ()) if row.get("asin")
        }
        for trans in transitions:
            if trans.kind is TransitionKind.ENDED:
                if not self._notify_ended:
                    continue
                state = states.get(trans.asin)
                name = state.name if state is not None else trans.asin
                price = state.latest_bid if state is not None else None
                if price:
                    body = f"{name}（成交价 ¥{price:,.0f}）"
                else:
                    # 没成交价是正常情况：流拍、卖家取消、或者详情还没回填价格。
                    # 不能因为拿不到价格就不提醒"它结束了"。
                    body = f"{name}（已结束，没有成交价）"
                self._add_event(
                    key=(KIND_ENDED, trans.asin, ""),
                    kind=KIND_ENDED,
                    asin=trans.asin,
                    title="拍卖结束",
                    body=body,
                    now=now,
                )
                continue
            if trans.kind is not TransitionKind.END_TIME_CHANGED:
                continue
            old_at, new_at = trans.old_end_at, trans.new_end_at
            if old_at is None or new_at is None:
                # 老调用方（或手工构造的 Transition）只有 detail 文本，没法判方向。
                # 宁可不报，也不猜。
                continue
            if new_at > old_at:
                if not self._notify_extended:
                    continue
                kind, title = KIND_EXTENDED, "截止时间延后"
            else:
                if not self._notify_shortened:
                    continue
                kind, title = KIND_SHORTENED, "截止时间提前"
            state = states.get(trans.asin)
            name = state.name if state is not None else trans.asin
            moved = f"{_fmt_clock(old_at)} → {_fmt_clock(new_at)}"
            # auto_extend 为 True 且往后退：雅虎的自动延长（结束前 5 分钟有人
            # 抬价就再延 5 分钟）。提前的情况不可能由自动延长造成，不标注。
            if kind is KIND_EXTENDED and auto_extend.get(trans.asin) is True:
                moved = f"{moved}，自动延长"
            body = f"{name}（{moved}）"
            self._add_event(
                key=(kind, trans.asin, new_at.isoformat()),
                kind=kind,
                asin=trans.asin,
                title=title,
                body=body,
                now=now,
            )

    def _add_event(
        self,
        *,
        key: tuple[str, str, str],
        kind: str,
        asin: str,
        title: str,
        body: str,
        now: datetime,
    ) -> None:
        """交给通知板（有板）或直接投递（无板，旧行为）。"""
        if self._notices is not None:
            self._notices.add_event(key=key, kind=kind, asin=asin, title=title, body=body, now=now)
            return
        try:
            self._notifier.notify(Notification(title=title, body=body, asin=asin, kind=kind))
        except Exception:
            log.warning("通知投递失败（已忽略）: %s %s", kind, asin)

    # ------------------------------------------------- 出价被超过（价格比对）

    async def _fetch_my_bids(self) -> dict[str, MyBid]:
        """拉「我的竞拍 · 竞拍中」，返回 ``asin -> MyBid``（含我的出价）。

        这是"我拍了什么、出了多少"的权威来源，也是价格比对的基准。接口每页
        10 条，**必须翻完**：只在第 1 页里找的话，同时出价超过 10 个拍品时
        第 11 个之后的基准就是缺失的，那些拍品会被静默跳过比对（``mine is
        None`` → 只留一行 warning），用户永远收不到提醒。

        失败一律降级：第 1 页就失败 → 空字典（这轮不做价格比对）；后面某页
        失败 → 保留已拿到的部分。价格比对是**锦上添花**的第二条信号，不能
        因为它让整轮刷新失败——和站内消息同样的态度。
        """
        out: dict[str, MyBid] = {}
        page = 1
        while page <= _MY_BIDS_MAX_PAGES:
            try:
                data = await self._api.fetch_my_bids(page=page)
            except MerukiTrackerError as exc:
                if page == 1:
                    log.warning("我的竞拍列表拉取失败，本轮跳过价格比对: %s", exc)
                else:
                    log.warning(
                        "我的竞拍第 %d 页拉取失败，按已拿到的 %d 条比对: %s", page, len(out), exc
                    )
                break
            for bid in data.items:
                out[bid.asin] = bid
            if page >= data.total_page or not data.items:
                break
            page += 1
        return out

    async def _detect_outbid_by_price(
        self,
        detail_rows: list[dict[str, Any]],
        states: dict[str, AuctionState],
        now: datetime,
    ) -> set[str]:
        """用本轮详情的当前价判断「出价被超过」，命中就注册通知。

        两条信号取或：

        1. **价格**：本轮详情的当前价 vs 我的出价（下面的规则）；
        2. **订单状态**：``StatusStr`` 里直接写着「价格被超过」——服务端判定，
           而且能覆盖"我只出价、没关注"的拍品（那种拍品没有详情可拉）。
           2026-11-16 真机抓到它从「预约出价」变成「价格被超过」的全过程。

        规则（2026-11-16 用 8 条历史输掉的订单验证）：

        - ``当前价 > 我的出价`` → **一定**被超过（6/6 无例外，正向可靠）；
        - ``当前价 <= 我的出价`` → 什么都不能说明。输掉也可能是因为被一口价
          买走、出价被判「价格不足」、卖家取消（8 条里有 2 条最终价低于我的
          出价），所以**绝不反向推断"没被超过"**。

        两个来源互为补充：站内信最准但会漏（``DEMOORD00003`` 订单 log 只有
        「价格不足」，最终价 5001 > 我 5000）；价格比对不会漏，但只覆盖
        本轮真的拉了详情的拍品（即你在关注、还没结束的那些）。

        Args:
            detail_rows: 本轮详情镜像行（``bid_price`` 现成有，零额外请求）。
            states: 最新状态（取拍品名做通知文案）。
            now: 检测时刻。

        Returns:
            本轮因价格被判定「被超过」的 asin 集合。
        """
        if not self._outbid_by_price:
            return set()

        bids = await self._fetch_my_bids()
        if not bids:
            return set()

        current = {
            str(row["asin"]): row.get("bid_price")
            for row in detail_rows
            if row.get("asin") and row.get("bid_price") is not None
        }
        hit: set[str] = set()
        for asin, bid in bids.items():
            mine = bid.jpy_price
            price = current.get(asin)

            # 信号一（价格）：当前价高于我的出价 → 一定被超过（6/6 无例外）。
            by_price = mine is not None and price is not None and price > mine
            # 信号二（订单状态）：服务端直接写「价格被超过」。这条能覆盖
            # "我只出价、没关注"的拍品——那种拍品本轮根本不会去拉详情。
            # 但价格更新鲜：有价格且还没被超过时，以价格为准，别拿可能滞后的
            # 状态误报（我刚加价、订单状态还没来得及更新时就会出现这种滞后）。
            stale_status = mine is not None and price is not None and price <= mine
            by_status = "超过" in bid.status and not stale_status
            if not (by_price or by_status):
                if mine is not None and price is not None:
                    # 明确还领先 → 清掉记录，下次真被超还能再响
                    self._price_outbid_seen.pop(asin, None)
                continue
            if mine is None:
                # 状态说被超过，但拿不到我的出价金额：报不出"被超了多少钱"，
                # 不如不响（这也说明订单列表结构变了，值得留意日志）
                log.warning("出价被超过，但订单里没有出价金额: %s", asin)
                continue
            if self._price_outbid_seen.get(asin) == mine:
                continue  # 同一个出价已经提醒过，别每轮都响
            self._price_outbid_seen[asin] = mine

            st = states.get(asin)
            title = (st.name if st is not None and st.name else "") or bid.name or asin
            log.info(
                "出价被超过（%s 判定）：%s 当前价 %s / 我的出价 %s",
                "价格" if by_price else "订单状态",
                asin,
                f"{price:,.0f}" if price is not None else "未知",
                f"{mine:,.0f}",
            )
            self._notify_price_outbid(
                asin=asin, my_bid=mine, current_bid=price, title=title, now=now
            )
            hit.add(asin)
        return hit

    def _notify_price_outbid(
        self,
        *,
        asin: str,
        my_bid: float,
        current_bid: float | None,
        title: str,
        now: datetime,
    ) -> None:
        """把价格比对出来的「出价被超过」交给通知板（无板时直接走 Notifier）。"""
        if self._notices is not None:
            self._notices.add_price_outbid(
                asin=asin, my_bid=my_bid, current_bid=current_bid, title=title, now=now
            )
            return
        if current_bid is None:
            body = f"{title}（我的出价 ¥{my_bid:,.0f}，已被超过）"
        else:
            body = f"{title}（当前 ¥{current_bid:,.0f} / 我的出价 ¥{my_bid:,.0f}）"
        try:
            self._notifier.notify(
                Notification(title="出价被超过", body=body, asin=asin, kind="outbid")
            )
        except Exception:
            log.warning("通知投递失败（已忽略）: %s", asin)

    # ---------------------------------------------------------------- 详情

    async def _fill_details(
        self,
        states: dict[str, AuctionState],
        transitions: list[Transition],
        progress: ProgressFn | None,
        mirror_rows: list[dict[str, Any]] | None = None,
        force: Collection[str] = (),
        pulled: set[str] | None = None,
    ) -> DetailStats:
        """把**所有还在关注、还没结束**的拍品详情都拉一遍。

        用户 2026-11-15 定调：「根据我们现在已有的数据每条挨个去请求详情，从详情
        判断是否结束」。所以这里不再挑优先级——以前那套"到点待确认 / 时间待确认 /
        收盘前复查点 / 兜底补漏"是在决定"少拉几条"，而结束与否只能由详情回答，
        少拉一条就等于让它在「进行中」里多停一轮（或者永远不停）。

        Args:
            states: 拍品状态（原地更新）。
            transitions: 迁移记录（原地追加）。
            progress: 进度回调。
            mirror_rows: 传了就把接口原始详情追加进来，供镜像落库。
            force: 本轮**排在最前面**的 asin（来自「出价被超过」消息）——不是
                为了"少拉几条"，只是让用户最关心的那条早一点出结果。
            pulled: 本轮已发过请求的 asin 集合（跨阶段去重）。

        Returns:
            (成功数, 失败数)。
        """
        forced = [asin for asin in dict.fromkeys(force) if _needs_detail(states.get(asin))]
        forced_set = set(forced)
        rest = [asin for asin, st in states.items() if asin not in forced_set and _needs_detail(st)]
        return await self._pull_details(
            forced + rest, states, transitions, progress, mirror_rows, pulled
        )

    async def _pull_details(
        self,
        targets: Collection[str],
        states: dict[str, AuctionState],
        transitions: list[Transition],
        progress: ProgressFn | None,
        mirror_rows: list[dict[str, Any]] | None = None,
        pulled: set[str] | None = None,
    ) -> DetailStats:
        """把给定的 asin 逐个拉详情、落观测、推进状态。

        三段调用方（按优先级挑、手动补齐、刚结束补成交价）除了**挑谁**以外
        行为完全一样，所以循环只写一遍。``pulled`` 记录本轮真的发过请求的
        asin，后面的阶段据此跳过，避免同一条拍品被拉两次。
        """
        ok = fail = 0
        for idx, asin in enumerate(targets, start=1):
            st = states.get(asin)
            if st is None:
                continue
            if progress:
                progress(f"详情 {idx}/{len(targets)}")
            url_hex = self._detail_urls.get(asin, "") or self._stored_detail_url(asin)
            if not url_hex:
                continue
            if pulled is not None:
                pulled.add(asin)
            try:
                info = await self._api.fetch_detail(url_hex)
                result = _to_detail_result(info)
                if result.failed:
                    fail += 1
                else:
                    ok += 1
                self._observations.add(
                    asin=asin,
                    observed_at=datetime.now(UTC),
                    kind="detail",
                    end_time_raw=info.end_at.isoformat() if info.end_at else "",
                    latest_bid=info.bid_price,
                )
                if mirror_rows is not None:
                    mirror_rows.append(_detail_mirror_row(info))
            except AuthExpiredError:
                raise
            except MerukiTrackerError as exc:
                log.debug("详情失败 %s: %s", asin, exc)
                result = DetailResult(failed=True)
                fail += 1
            new_state, trans = apply_detail(st, result, datetime.now(UTC))
            states[asin] = new_state
            if trans is not None:
                transitions.append(trans)
        return DetailStats(ok, fail)

    def _stored_detail_url(self, asin: str) -> str:
        """从镜像表里找详情地址（内存字典重启后是空的）。

        详情地址只出现在列表响应里，而列表只给「进行中」——一条已经结束、
        从没在本进程里被列表命中的拍品，内存里永远没有它的地址。可用户点进
        详情就想看到价格，不能要求他先去把列表刷一遍。
        """
        if self._mirror is None:
            return ""
        try:
            return self._mirror.detail_urls().get(asin, "")
        except Exception:
            log.debug("从镜像表读详情地址失败 %s", asin, exc_info=True)
            return ""

    async def refresh_detail_of(self, state: AuctionState) -> AuctionState:
        """拉一个拍品的详情：观测落库 + 镜像全字段落库 + 合并进状态。

        这是"用户主动要看这个拍品"的唯一入口（详情屏按 r、Enter 进详情时若无
        详情则自动补一次）。它**不看复查点**——用户主动要看最新值，就该立刻去拉。

        除了合并状态，还会把详情的**全字段**写进 ``detail_items`` 镜像表：
        以前只有后台批量采集（``_fill_details``）会写镜像，手动看详情只写观测，
        于是"我看过的拍品"在镜像里没有记录、原始 JSON 也丢了。现在两条路走同一套。

        Args:
            state: 当前拍品状态。

        Returns:
            合并后的新状态（失败时返回带失败计数的状态，不抛异常）。

        Raises:
            AuthExpiredError: 凭证失效（需要重新登录，不该静默）。
            MerukiTrackerError: 其他错误（调用方转成界面提示）。
        """
        url_hex = self._detail_urls.get(state.asin, "") or self._stored_detail_url(state.asin)
        if not url_hex:
            raise MerukiTrackerError("还没有这个拍品的详情地址；回列表按 r 刷新一次再进来")
        try:
            info = await self._api.fetch_detail(url_hex)
        except AuthExpiredError:
            raise
        except MerukiTrackerError as exc:
            log.debug("手动刷新详情失败 %s: %s", state.asin, exc)
            new_state, _ = apply_detail(state, DetailResult(failed=True), datetime.now(UTC))
            return new_state

        result = _to_detail_result(info)
        now = datetime.now(UTC)
        try:
            self._observations.add(
                asin=state.asin,
                observed_at=now,
                kind="detail",
                end_time_raw=info.end_at.isoformat() if info.end_at else "",
                latest_bid=info.bid_price,
            )
            self._observations.commit()
        except Exception:  # 观测落库失败不该让"看到最新值"这件事失败
            log.warning("手动刷新详情后写观测失败", exc_info=True)
        self._save_detail_mirror(info, now)
        new_state, _ = apply_detail(state, result, now)
        return new_state

    def _save_detail_mirror(self, info: DetailInfo, now: datetime) -> None:
        """把一次详情响应**全字段**写进镜像表（失败不影响主流程）。

        为什么单独包一层：手动看详情以前根本不写镜像，用户"看过"的拍品在
        ``detail_items`` 里没有行、原始 JSON 也没留存。现在统一走这里。
        """
        if self._mirror is None:
            return
        try:
            self._mirror.save_details([_detail_mirror_row(info)], seen_at=now)
        except Exception:
            log.warning("详情写入镜像表失败 %s", info.asin, exc_info=True)

    async def _with_retry(self, fn: Callable[[], Awaitable[object]]):
        """调用 ``fn``。

        重试**统一由 API 层负责**（``MerukiApi._rpc`` 按 ``fetch.retry`` 做指数退避，
        且对 WAF 有更长的冷却）。这里若再套一层重试，最坏会放大成
        ``retry × retry`` 次请求——对一个有风控的站点是有害的，也会让超时语义失真。

        保留本方法是为了统一"哪些错误不重试"的判断点（鉴权/协议类错误连 API 层
        都不该重试，若将来有绕过 API 的调用也走同一规则）。
        """
        try:
            return await fn()
        except (AuthExpiredError, ProtocolChangedError):
            raise

    # ---------------------------------------------------------------- 维护

    def _maybe_prune(self, now: datetime) -> None:
        """按配置的保留天数清理观测历史，每天最多一次。"""
        if self._last_prune is not None and now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        _prune_observations(self._observations, retention=self._retention)
        # 镜像里的流水（每页的分页元信息、每轮比出的变更）跟着同一个保留期清，
        # 否则它们只增不减。
        if self._mirror is not None:
            _prune_mirror(self._mirror, retention=self._retention)


@dataclass(frozen=True, slots=True)
class _ListResult:
    items: list[Observation]
    complete: bool
    detail_urls: dict[str, str]
    #: 接口原始条目（去镜像落库用；``items`` 是给 advance 的领域观测）
    raw_items: list[ListItem]
    #: 每页的分页元信息（含 ItemPerPage / ShowPageNum）
    pages: list[dict[str, Any]]


def _needs_detail(state: AuctionState | None) -> bool:
    """这条拍品这一轮要不要拉详情。

    判据只有两条：**还在关注**、**还没结束**。以前还要看"到没到复查点"，
    现在每轮都全拉，只看这两条就够——已结束的成交价不会再变，取关的也不用管。
    """
    if state is None:
        return False
    return state.user_query is UserQuery.FOLLOWING and state.door_state is not DoorState.ENDED


def _prune_observations(store: ObservationStore, *, retention: timedelta) -> None:
    """按保留天数清理观测历史。"""
    cutoff = datetime.now(UTC) - retention
    try:
        removed = store.prune(cutoff)
    except Exception:
        log.debug("观测历史清理失败", exc_info=True)
        return
    if removed:
        log.debug("观测历史清理 %d 行（早于 %s）", removed, cutoff.date())


def _prune_mirror(store: MirrorStore, *, retention: timedelta) -> None:
    """按同一个保留期清理镜像流水（分页元信息 + 变更事件）。

    Args:
        store: 接口镜像。
        retention: 保留天数对应的时长。
    """
    cutoff = datetime.now(UTC) - retention
    try:
        removed = store.prune(cutoff)
    except Exception:
        log.debug("镜像流水清理失败", exc_info=True)
        return
    if removed:
        log.debug("镜像流水清理 %s（早于 %s）", removed, cutoff.date())


def _classify(exc: MerukiTrackerError) -> str:
    if isinstance(exc, AuthExpiredError):
        return "auth"
    if isinstance(exc, WafBlockedError):
        return "waf"
    if isinstance(exc, ProtocolChangedError):
        return "protocol"
    if isinstance(exc, NetworkError):
        return "network"
    return "internal"


def _to_detail_result(info: DetailInfo) -> DetailResult:
    # ``hasCollect=False`` 本身就是一条有效结论：即使这次没解析出结束时间
    # （商品下架 / 响应缺字段），也不能把它当成"白拉了一次"，否则取关就永远
    # 识别不出来、每轮继续拉下去。
    if info.end_at is None and not info.ended and info.collected is not False:
        return DetailResult(failed=True)
    # 当前价与一口价**分开传**，不在这里合并（见 domain/detail.py 的说明）：
    # 一口价商品 bidPrice 恒为 0，而普通拍卖可能两个都有，合并就分不清了。
    return DetailResult(
        failed=False,
        ended=info.ended,
        end_at=info.end_at,
        current_bid=info.bid_price,
        # 只有已结束才谈"成交价"，未结束时同一次请求里的 bidPrice 就是当前价
        final_bid=info.bid_price if info.ended else None,
        buy_now_price=info.buy_now_price,
        # 有值就说明字段一定在（测试里的假响应也只给值不给标记）。
        buy_now_price_present=info.buy_now_price_present or info.buy_now_price is not None,
        collected=info.collected,
    )


# ------------------------------------------------------------------ 镜像行转换
#
# 比对层（domain/diff.py）刻意不依赖存储类型：两边都换成同一种 dict，
# 这样上一轮（从库里读出来）和本轮（接口刚拿到）能直接用同一套逻辑比。

#: 镜像行里的字段顺序（也是 diff 的比较键顺序之一部分）
_MIRROR_FIELDS = (
    "asin",
    "item_id",
    "name",
    "url",
    "detail_url",
    "bid_deadline",
    "bid_deadline_str",
    "created_time",
    "image_url",
    "user_id",
)


def _item_mirror_row(item: ListItem) -> dict[str, Any]:
    """接口条目 → 比对/落库用的扁平 dict。"""
    return {
        "asin": item.asin,
        "item_id": item.item_id,
        "name": item.name,
        "url": item.url,
        "detail_url": item.detail_url,
        "bid_deadline": item.bid_deadline,
        "bid_deadline_str": item.bid_deadline_str,
        "created_time": item.created_time,
        "image_url": item.image_url,
        "user_id": item.user_id,
        "raw": dict(item.raw),
    }


def _stored_item_row(stored: StoredListItem) -> dict[str, Any]:
    """库里的镜像行 → 与 ``_item_mirror_row`` 同形的 dict（比对基准）。"""
    return {name: getattr(stored, name) for name in _MIRROR_FIELDS}


def _detail_mirror_row(info: DetailInfo) -> dict[str, Any]:
    return {
        "asin": info.asin,
        "name": info.name,
        "end_at": info.end_at.isoformat() if info.end_at else "",
        "ended": info.ended,
        "bid_price": info.bid_price,
        "buy_now_price": info.buy_now_price,
        "auto_extend": info.auto_extend,
        "early_termination": info.early_termination,
        "raw": dict(info.raw),
    }


def _stored_detail_row(stored: StoredDetail) -> dict[str, Any]:
    return {
        "asin": stored.asin,
        "name": stored.name,
        "end_at": stored.end_at,
        "ended": stored.ended,
        "bid_price": stored.bid_price,
        "buy_now_price": stored.buy_now_price,
        "auto_extend": stored.auto_extend,
        "early_termination": stored.early_termination,
    }
