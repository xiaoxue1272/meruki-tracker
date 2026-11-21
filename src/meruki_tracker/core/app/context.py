"""依赖装配：唯一知道具体实现的地方。"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

    from .notices import NoticeBoard
    from .qqchannel import QqBot
    from .refresher import ProgressFn, Refresher, RefreshResult
    from .translator import Translator
    from .wechat import WeChatBot

from ..adapters.meruki.api import MerukiApi
from ..adapters.meruki.transport import create_transport
from ..adapters.store.credentials import CredentialStore
from ..adapters.store.db import connect as db_connect
from ..adapters.store.messages import MessageStore
from ..adapters.store.migrate import (
    drop_ended_history,
    drop_obsolete_settings,
    drop_unfollowed_state,
    normalize_legacy_observation_times,
    repair_mislabeled_unfollowed,
    split_price_fields,
)
from ..adapters.store.mirror import MirrorStore
from ..adapters.store.observations import ObservationStore
from ..adapters.store.settings import SettingsStore
from ..adapters.store.snapshot import Account, SchemaVersionError, Snapshot, SnapshotStore
from ..adapters.store.translations import TranslationStore
from ..config import Config
from ..errors import (
    AuthExpiredError,
    MerukiTrackerError,
    NetworkError,
    ProtocolChangedError,
    WafBlockedError,
)
from ..logging_setup import get_logger
from .notifications import build_notifier

log = get_logger("app.context")

#: 统一数据库文件名（凭证 / 快照 / 观测都在这一个库里）
DB_NAME = "meruki.db"


@dataclass
class AppContext:
    """所有用例共享的依赖束。"""

    cfg: Config
    data_dir: Path
    snapshots: SnapshotStore
    credentials: CredentialStore
    observations: ObservationStore
    mirror: MirrorStore
    messages: MessageStore
    translations: TranslationStore
    translator: Translator
    settings: SettingsStore
    api: MerukiApi
    refresher: Refresher
    #: 通知板（「通知队列」侧栏数据源）；测试替身上下文可能没有
    notices: NoticeBoard | None = None
    #: 微信个人号通道（收消息 + 发提醒）；没挂载或测试替身时为 None
    wechat: WeChatBot | None = None
    #: QQ 官方机器人通道（WebSocket 收消息 + openapi 发提醒）；凭据不全时为 None
    qq: QqBot | None = None
    #: 统一数据库连接（各 store 共用，只在这里关一次）
    db_conn: sqlite3.Connection | None = None
    _closed: bool = field(default=False, repr=False)

    async def close(self) -> None:
        """释放全部资源。运输层关闭失败也不能漏关数据库。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self.api.aclose()
        finally:
            # 三个 store 共用同一条连接，只关一次
            with contextlib.suppress(Exception):
                self.db_conn.close()


def load_config(data_dir: Path) -> Config:
    """先开库把配置读出来（配置本身也存在库里）。

    Args:
        data_dir: 数据目录（配置库就在这里）。

    Returns:
        校验后的配置；库里没有配置时返回默认值。

    Raises:
        ConfigError: 库里的配置非法。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = db_connect(data_dir / DB_NAME)
    try:
        store = SettingsStore(conn)
        # 先清掉已退役的配置项，再解析。顺序不能反：老库里残留的旧键会让
        # Config.from_mapping 报"未知配置项"直接启动失败（真实踩过）。
        retired = drop_obsolete_settings(conn)
        if retired:
            log.info("已清理 %d 个已退役的配置项", retired)
        raw = store.load_all()
        # 库里存的是"一个配置项一行"，先还原成嵌套结构再校验
        cfg = Config.from_mapping(_nest(raw)) if raw else Config.defaults()
        # 把缺失的配置项用**当前生效值**补齐（不覆盖用户改过的）。
        # 这样 SELECT * FROM settings 就是完整配置，而不是"库 + 代码默认值"
        # 两头拼；新版本加的配置项也会自动出现。
        added = store.ensure_defaults(cfg.to_flat())
        if added:
            log.info("已初始化 %d 个默认配置项", added)
    finally:
        conn.close()

    # 数据目录**以实际打开的库为准**，不用库里记的值来反推。
    # 否则手工改库（或旧版本界面）把 storage.data_dir 写成别处，下次启动
    # 就会在另一个目录建新库——凭证和快照分裂成两份，排查起来极其困惑。
    actual = str(data_dir)
    if not _same_dir(cfg.storage.data_dir, actual):
        # 库里记的与实际不一致（例如手工改过库、或从相对路径换成了绝对路径）。
        # 以实际为准，并把纠正后的值写回库，免得每次启动都提这件事。
        log.info("数据目录以实际位置为准：%s → %s", cfg.storage.data_dir, actual)
        cfg = replace(cfg, storage=replace(cfg.storage, data_dir=actual))
        with contextlib.suppress(OSError):
            conn = db_connect(data_dir / DB_NAME)
            try:
                SettingsStore(conn).set("storage.data_dir", actual)
            finally:
                conn.close()
    return cfg


def _same_dir(a: str, b: str) -> bool:
    """两个路径字符串是否指向同一个目录（相对路径先解析成绝对路径）。"""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError):
        return a == b


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    """``{"refresh.auto_seconds": 180}`` → ``{"refresh": {"auto_seconds": 180}}``。"""
    out: dict[str, Any] = {}
    for key, value in flat.items():
        section, _, name = key.partition(".")
        if name:
            out.setdefault(section, {})[name] = value
        else:
            out[key] = value
    return out


def build_context(cfg: Config) -> AppContext:
    """根据配置装配全部依赖。

    Args:
        cfg: 已校验的配置。

    Returns:
        可用的依赖束。
    """
    data_dir = cfg.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    transport = create_transport(cfg.transport)

    # 全部持久状态共用一个库：凭证 / 快照 / 观测都在 meruki.db
    db_path = data_dir / DB_NAME
    db_conn = db_connect(db_path)
    credentials = CredentialStore(db_path, conn=db_conn)
    api = MerukiApi(
        transport,
        cookie=credentials.load(),
        timeout=cfg.fetch.timeout_seconds,
        min_interval=cfg.fetch.min_request_interval,
        # 请求间隔抖动：固定间隔是最好识别的脚本特征
        jitter=cfg.fetch.jitter_ratio,
        # 重试统一由 API 层负责（含指数退避与 WAF 冷却），refresher 不再重复重试
        retry=cfg.fetch.retry,
    )
    # 历史观测里可能混着旧格式的结束时间（无时区），一次性规范化
    fixed = normalize_legacy_observation_times(db_conn)
    if fixed:
        log.info("已规范化 %d 条历史观测的结束时间格式", fixed)

    # 旧逻辑把"拍卖结束"误判成"用户取关"，一次性改回（幂等）
    repaired = repair_mislabeled_unfollowed(db_conn)
    if repaired:
        log.info("已修正 %d 个被误标为「已取关」的已结束拍品", repaired)

    # 「已取关」这个状态本身被取消了，残留的记录并入「已结束」（幂等）
    dropped = drop_unfollowed_state(db_conn)
    if dropped:
        log.info("已把 %d 条「已取关」记录并入「已结束」", dropped)

    # 「已结束」不再靠翻 50 页同步，旧机制灌进来的历史一次性清掉（幂等）
    purged = drop_ended_history(db_conn)
    if purged:
        log.info("已清空 %d 条旧的「已结束」历史记录", purged)

    # 一口价与当前价拆成两个字段（含把当年塞进 latest_bid 的一口价挪回去）
    split = split_price_fields(db_conn)
    if split:
        log.info("已把 %d 个拍品的一口价从「当前价」拆回独立字段", split)

    snapshots = SnapshotStore(db_path, conn=db_conn)
    observations = ObservationStore(db_path, conn=db_conn)
    mirror = MirrorStore(db_path, conn=db_conn)
    messages = MessageStore(db_path, conn=db_conn)
    translations = TranslationStore(db_path, conn=db_conn)
    settings = SettingsStore(db_conn)

    from .notices import NoticeBoard
    from .qqchannel import QqBot, build_client, session_store
    from .refresher import Refresher, RefresherOptions
    from .wechat import IlinkClient, WeChatBot, configured_base_url, configured_token, context_store

    # 微信个人号通道：只有已经扫码拿到 bot_token 才建客户端；没挂载时是 None，
    # build_notifier 会跳过它。
    wechat_bot: WeChatBot | None = None
    token = configured_token(cfg.channels)
    if token:
        wechat_bot = WeChatBot(
            client=IlinkClient(
                token,
                base_url=configured_base_url(cfg.channels),
                timeout=cfg.channels.wechat_timeout_seconds,
            ),
            store=context_store(db_conn),
        )

    # QQ 官方机器人：AppID/AppSecret 可能在设置里手填，也可能是扫码换来的。
    # 有凭据就建客户端——发提醒不需要先收到消息，收消息才需要 WebSocket 在线。
    qq_client = build_client(cfg.channels)
    qq_bot = QqBot(client=qq_client, store=session_store(db_conn)) if qq_client else None

    # 通知板：右侧「通知队列」侧栏的数据源。通道由 cfg.channels 决定
    # ；没启用任何渠道时就只闪界面提示。
    notifier = build_notifier(cfg.channels, wechat=wechat_bot, qq=qq_bot)
    # 侧栏窗口也来自配置（notify.window_minutes）：以前写死 2 小时。
    notices = NoticeBoard(
        window=timedelta(minutes=cfg.notify.window_minutes),
        notifier=notifier,
    )

    refresher = Refresher(
        api=api,
        snapshots=snapshots,
        credentials=credentials,
        observations=observations,
        mirror=mirror,
        options=RefresherOptions.from_config(cfg),
        # 站内消息里的「出价被超过」——服务端算好的事实，比自己猜价格准
        messages=messages,
        # 通知通道：webhook / 邮件 / OneBot / 微信，都从这里扇出
        notifier=notifier,
        notices=notices,
    )

    from .translator import Translator

    translator = Translator(
        api, translations, target=cfg.translate.target, source=cfg.translate.source
    )

    return AppContext(
        cfg=cfg,
        data_dir=data_dir,
        db_conn=db_conn,
        snapshots=snapshots,
        credentials=credentials,
        observations=observations,
        mirror=mirror,
        messages=messages,
        translations=translations,
        translator=translator,
        settings=settings,
        api=api,
        refresher=refresher,
        notices=notices,
        wechat=wechat_bot,
        qq=qq_bot,
    )


def load_snapshot(ctx: AppContext) -> Snapshot:
    """读取已有快照；没有则返回空快照。"""
    try:
        snap = ctx.snapshots.read()
    except SchemaVersionError as exc:
        log.error("快照版本不支持: %s（将重新采集）", exc)
        snap = None
    except ValueError as exc:
        # 版本不符已由上面单独接住；剩下只可能是行内容读不懂，措辞别误导成版本问题
        log.error("快照内容读不懂: %s（将重新采集）", exc)
        snap = None
    if snap is not None:
        return snap
    return Snapshot(account=Account(label=ctx.cfg.account.label))


async def verify_cookie(ctx: AppContext, cookie: str) -> tuple[bool, str]:
    """验证 Cookie 并返回昵称。

    Returns:
        (是否有效, 昵称或错误原因)
    """
    ctx.api.cookie = cookie
    try:
        nickname = await ctx.api.verify()
        return True, nickname
    except AuthExpiredError as exc:
        return False, f"凭证无效：{exc}"
    except NetworkError as exc:
        return False, f"网络不通：{exc}"
    except WafBlockedError as exc:
        return False, f"被拦截：{exc}"
    except ProtocolChangedError as exc:
        return False, f"接口可能已改版：{exc}"
    except MerukiTrackerError as exc:
        return False, str(exc)


async def do_refresh(
    ctx: AppContext,
    snap: Snapshot,
    progress: ProgressFn | None = None,
) -> tuple[Snapshot, RefreshResult]:
    """执行一次刷新（凭证热更新 + 委托 refresher）。

    手动与定时现在是同一条路：每轮都把关注中的每一条详情拉一遍（结束只看详情），
    所以没有"这次要不要连详情"的开关了。
    """
    ctx.api.cookie = ctx.credentials.load()
    return await ctx.refresher.refresh(snap, progress)
