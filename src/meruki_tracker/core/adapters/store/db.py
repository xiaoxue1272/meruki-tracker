"""统一数据库：一个 ``meruki.db`` 装下全部持久状态。

**为什么收敛到一个库**

以前 ``data/`` 下散着四套存储（observations.sqlite / state.json / cookies /
chrome-profile），各自按"当时最省事"的技术选型：观测要查询所以用 sqlite，
状态要原子替换所以用 json 文件，Cookie 要 0600 所以用裸文件。
后果是备份要拷一堆、权限要管几遍、迁移没有统一入口。

现在统一为 ``data/meruki.db``（WAL + busy_timeout + 0600）：

===================  ==============================================
表                    存什么
===================  ==============================================
``meta``              schema 版本等元信息
``settings``          程序内可调的配置项（JSON 值）
``credentials``       当前 Cookie（单行，靠 CHECK 保证唯一）
``snapshot``          当前快照的"头"（采集时间 / 上次采集结果 / 账号）
``auctions``          由接口数据比对得出的派生状态（UI 直接读这张）
``list_items``        列表接口返回的**每个字段**（接口镜像，真相来源）
``list_pages``        每次翻页的分页元信息（含 ItemPerPage / ShowPageNum）
``detail_items``      详情接口返回的**每个字段**（接口镜像）
``changes``           逐轮比对产出的变更事件
``bid_events``        站内消息里的「出价被超过」（服务端算好的事实）
``observations``      每轮采集的原始观测（历史留痕，用于追溯）
===================  ==============================================

**为什么多了 list_items / detail_items**

以前只存"我们理解的字段"，接口多给的（``BidDeadlineStr`` / ``CreatedTime`` /
``ImageUrl`` / ``UserId`` / ``ItemPerPage`` …）直接丢在内存里。结果是出了问题
只能看日志、没法回答"服务端当时到底回了什么"。现在接口给什么就存什么，
外加 ``raw_json`` 兜住未来新增的字段（服务端加字段不必改表）。

``auctions`` 从"自维护的状态机"降级为**派生投影**：它现在是
``list_items`` + ``detail_items`` 比对的产物，不再是独立的事实来源。

**一致性语义**

旧 ``state.json`` 靠"整份原子替换"保证"失败不覆盖已有数据"。改成库之后
用**事务**实现同等语义：写快照时在同一个事务里清空并重写 ``snapshot`` 与
``auctions``，事务提交前崩溃则回滚，旧数据完好；写完再提交，读者要么看到
旧快照、要么看到新快照，不会看到半份。

**权限**

库文件与 WAL/SHM 附属文件都收紧到 0600（打开后立刻 chmod），并且把
``data/`` 目录本身设为 0700——目录不可进，里面的文件权限才有意义。
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 程序内配置（一个配置项一行）。value 存 JSON，便于存数字/列表/字符串。
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Cookie 凭证：只保留一行（id 恒为 1），写入即替换
CREATE TABLE IF NOT EXISTS credentials (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    cookie     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 快照头：同样只保留一行
CREATE TABLE IF NOT EXISTS snapshot (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    generated_at TEXT,
    attempt_at   TEXT,
    attempt_ok   INTEGER NOT NULL DEFAULT 1,
    attempt_err  TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    nickname     TEXT NOT NULL DEFAULT '',
    cookie_state TEXT NOT NULL DEFAULT ''
);

-- 快照里的拍品（整份替换，不是增量）
CREATE TABLE IF NOT EXISTS auctions (
    asin           TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    user_query     TEXT NOT NULL,
    door_state     TEXT NOT NULL,
    time_state     TEXT NOT NULL,
    end_at         TEXT,
    confirmed_at   TEXT,
    latest_bid     REAL,
    buy_now_price  REAL,
    detail_failures INTEGER NOT NULL DEFAULT 0,
    last_seen_at   TEXT,
    unfollowed_at  TEXT,
    concluded_at   TEXT
);

-- 观测历史（保留用于追溯）
CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asin         TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    end_time_raw TEXT,
    latest_bid   REAL
);
CREATE INDEX IF NOT EXISTS ix_obs_asin_time ON observations(asin, observed_at);
CREATE INDEX IF NOT EXISTS ix_obs_time ON observations(observed_at);

-- ============================================================================
-- 以下为 schema v2：接口镜像 + 逐轮比对
-- ============================================================================

-- 列表接口返回的每条拍品，字段与接口 Data[] 一一对应。
-- 主键 (status, asin)：同一个拍品理论上只会出现在一个分桶里，但按 status
-- 分开存可以容忍"服务端把已结束的也塞进进行中"这种抖动，也方便按桶查询。
CREATE TABLE IF NOT EXISTS list_items (
    status            TEXT NOT NULL,
    asin              TEXT NOT NULL,
    item_id           TEXT NOT NULL DEFAULT '',
    name              TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL DEFAULT '',
    detail_url        TEXT NOT NULL DEFAULT '',
    bid_deadline      TEXT NOT NULL DEFAULT '',
    bid_deadline_str  TEXT NOT NULL DEFAULT '',
    created_time      TEXT NOT NULL DEFAULT '',
    image_url         TEXT NOT NULL DEFAULT '',
    user_id           TEXT NOT NULL DEFAULT '',
    -- 接口原始 JSON：服务端加字段时不用改表也能留证
    raw_json          TEXT NOT NULL DEFAULT '{}',
    -- 哪一轮采集看到的（= snapshot.generated_at，同一轮共用一个值）
    seen_at           TEXT NOT NULL,
    -- 第几页看到的，便于排查分页错乱
    page              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (status, asin)
);
CREATE INDEX IF NOT EXISTS ix_list_items_seen ON list_items(seen_at);
CREATE INDEX IF NOT EXISTS ix_list_items_item_id ON list_items(item_id);

-- 每次翻页的分页元信息。接口顶层 5 个字段一个不丢。
CREATE TABLE IF NOT EXISTS list_pages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    status         TEXT NOT NULL,
    page           INTEGER NOT NULL,
    item_per_page  INTEGER NOT NULL DEFAULT 0,
    show_page_num  INTEGER NOT NULL DEFAULT 0,
    current_page   INTEGER NOT NULL DEFAULT 0,
    total_item     INTEGER NOT NULL DEFAULT 0,
    total_page     INTEGER NOT NULL DEFAULT 0,
    -- 本页实际返回条数（与 item_per_page 对照可发现服务端少给）
    returned       INTEGER NOT NULL DEFAULT 0,
    fetched_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_list_pages_time ON list_pages(fetched_at);

-- 详情接口返回的字段（列表给不全的价格/精确时间在这里）。
CREATE TABLE IF NOT EXISTS detail_items (
    asin              TEXT PRIMARY KEY,
    name              TEXT NOT NULL DEFAULT '',
    end_at            TEXT,
    ended             INTEGER,
    bid_price         REAL,
    buy_now_price     REAL,
    auto_extend       INTEGER,
    early_termination INTEGER,
    raw_json          TEXT NOT NULL DEFAULT '{}',
    observed_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_detail_items_time ON detail_items(observed_at);

-- 逐轮比对产出的变更事件（替代过去"推断式"的状态迁移，可回放）
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    field       TEXT NOT NULL DEFAULT '',
    old_value   TEXT NOT NULL DEFAULT '',
    new_value   TEXT NOT NULL DEFAULT '',
    detected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_changes_time ON changes(detected_at);
CREATE INDEX IF NOT EXISTS ix_changes_asin ON changes(asin, detected_at);

-- v3：站内消息里「出价被超过」这类事件。服务端已经替我们算好了"有人超过你"，
-- 比自己轮询详情比对价格准得多；但它只有订单短 ID，asin 要靠订单详情解出来。
CREATE TABLE IF NOT EXISTS bid_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 站内消息的自增 id，同一个 id 只处理一次（跨轮去重靠它）
    msg_id      INTEGER NOT NULL,
    -- 12 位订单短 ID（标题里抠出来的），用于回查订单详情
    order_id    TEXT NOT NULL DEFAULT '',
    -- 从订单详情 url 解出来的雅虎拍品号，与 auctions.asin 同一体系
    asin        TEXT NOT NULL DEFAULT '',
    event       TEXT NOT NULL DEFAULT '',
    happened_at TEXT NOT NULL DEFAULT '',
    -- 消息自带的分类与时间（诊断用）
    category    TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    -- 通知投递状态：0 未投递 / 1 已投递（投递逻辑本轮只预留，恒为 0）
    notified    INTEGER NOT NULL DEFAULT 0,
    seen_at     TEXT NOT NULL
);
-- 一条消息会带出订单的**完整** log（价格不足 → 出价成功 → 出价被超过 →
-- 竞拍失败），所以唯一性不能只建在 msg_id 上——那样只会留下第一条，恰好
-- 丢掉最该看的「出价被超过」。改用 (msg_id, event, happened_at) 去重：
-- 同一条消息的同一个事件只落一次，事件流本身保持完整。
CREATE UNIQUE INDEX IF NOT EXISTS ux_bid_events_msg_event
    ON bid_events(msg_id, event, happened_at);
CREATE INDEX IF NOT EXISTS ix_bid_events_asin ON bid_events(asin, created_at);
CREATE INDEX IF NOT EXISTS ix_bid_events_time ON bid_events(seen_at);

-- v6：标题与描述的译文缓存。
-- 翻译是网络请求（一次一条，还要带节流），列表里十几条如果每次都翻就是十几倍
-- 的请求量。译文本身也不怎么变（标题偶尔改、描述基本不动），所以落库缓存，
-- 一个拍品只翻一次。``source`` / ``target`` 一起存，将来换语言能识别出旧译文。
CREATE TABLE IF NOT EXISTS translations (
    asin       TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    desc       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'ja',
    target     TEXT NOT NULL DEFAULT 'zh-CN',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_translations_time ON translations(updated_at);
-- 微信个人号（ClawBot）的会话凭据：每条入站消息带 context_token，发提醒必须回传它。
-- 单独一张表而不是塞进 settings：这是每用户一行、会随消息刷新的运行时状态。
CREATE TABLE IF NOT EXISTS qq_sessions (
    kind       TEXT NOT NULL,
    openid     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, openid)
);

CREATE TABLE IF NOT EXISTS wechat_context (
    user_id       TEXT PRIMARY KEY,
    context_token TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def _upgrade(conn: sqlite3.Connection) -> None:
    """把老库的 ``schema_version`` 升到当前版本。

    本项目没有编号迁移脚本，只认**"当前代码能读"**这一个目标：老版本的库结构
    都是当前版本的子集（v2 只新增镜像表、v3 只新增 ``bid_events``，全是
    ``CREATE TABLE IF NOT EXISTS``，上面 ``executescript`` 已经把表补齐了；
    v4 只是放宽了 ``bid_events`` 的唯一索引），
    所以历史版本 → 当前版本的数据迁移是空操作，只需要把版本号改过来，免得
    ``SnapshotStore.read`` 一直报"不支持的版本"。

    未来若出现真正需要搬数据的版本，在这里按版本号分支出 ``if ver < N`` 分支
    即可（幂等 + 单个事务，和 ``adapters/store/migrate.py`` 的作法一致）。
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return
    try:
        current = int(str(row[0]))
    except ValueError:
        # 版本号本身坏了：不猜，交给 SnapshotStore.read 报错
        return
    if current < 4:
        # v4：bid_events 的唯一索引从 (msg_id) 放宽到 (msg_id, event, happened_at)。
        # 旧的太严，同一订单的整条 log 只会留下第一条事件。老库里已有的行保留，
        # 只把旧索引删掉（新索引由上面的 executescript 建好）。
        conn.execute("DROP INDEX IF EXISTS ux_bid_events_msg")
    if current < 5:
        # v5：auctions 加 buy_now_price（一口价）。``CREATE TABLE IF NOT EXISTS``
        # 只对**新库**生效，老库必须 ALTER 补列——不补的话读写都会报
        # "no such column: buy_now_price"。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(auctions)")}
        if "buy_now_price" not in cols:
            conn.execute("ALTER TABLE auctions ADD COLUMN buy_now_price REAL")
    if current < SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
        )


def connect(path: Path) -> sqlite3.Connection:
    """打开（或创建）统一数据库，建表并收紧权限。

    多个 store 共享同一个路径时会各自持有连接；sqlite 的 WAL 模式下多连接
    读写是安全的，``busy_timeout`` 让并发写等待而不是立刻报 locked。
    """
    # 只收**自己新建**的目录：各 store 的默认路径是 ``Path("meruki.db")``，
    # 它的 parent 就是 ``Path(".")``——无条件 chmod 会把用户当前工作目录改成
    # 0700（实测 0755→0700），同机其他用户/服务立刻进不去，而且没有任何日志。
    created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 目录 0700：否则下面的 0600 只是"文件本人可读"，同机他人仍可进目录
    if created:
        with contextlib.suppress(OSError):
            path.parent.chmod(0o700)

    # check_same_thread=False：同一个库被 TUI 主线程与后台刷新线程共享
    # （例如 to_thread 里读 Firefox 库之外的写入路径）。sqlite3 默认会直接抛
    # ProgrammingError；这里所有写操作都走事务，并发由 busy_timeout 兜住。
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    # 只在新库里写入版本号；已存在的库**不覆盖**，而是走下面的升级
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    _upgrade(conn)
    conn.commit()

    # WAL 模式会额外产生 -wal / -shm，三个文件都要收紧
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            path.with_name(path.name + suffix).chmod(0o600)
    return conn
