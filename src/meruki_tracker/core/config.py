"""配置的读取、校验与持久化。

**配置不再是外部文件**：所有可调项在程序内的「设置」界面里改，值存进
``meruki.db`` 的 ``settings`` 表（一行一个 JSON 值）。理由：

- 分发给别人用时，让人手写 YAML 是最容易出错的一环（缩进、键名、取值范围）；
- 改配置要重启，体验很差；现在改完立刻生效；
- 配置和数据放在同一个库里，备份/迁移只需要拷一个文件。

设计约束：
- **未知键报错**（防止"改了却不生效"的静默陷阱）；
- 错误信息带完整路径（如 ``notify.points_minutes``）；
- 范围/格式问题在这里拦下，绝不让非法值活到运行期（更不许变成裸 traceback）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError

#: login 配置节允许的取值范围（闭区间）
LOGIN_PORT_RANGE = (1, 65535)
WEBHOOK_TIMEOUT_RANGE = (1.0, 60.0)
#: 邮件 / OneBot 的超时范围（闭区间，秒）
NET_TIMEOUT_RANGE = (1.0, 120.0)
#: SMTP 端口范围
SMTP_PORT_RANGE = (1, 65535)
LOGIN_WAIT_RANGE = (0, 3600)


@dataclass(frozen=True, slots=True)
class AccountConfig:
    label: str = "me"


@dataclass(frozen=True, slots=True)
class StorageConfig:
    data_dir: str = "./data"
    data_stale_minutes: int = 30
    observations_retention_days: int = 90


@dataclass(frozen=True, slots=True)
class FetchConfig:
    page_size_expected: int = 10
    timeout_seconds: float = 20.0
    retry: int = 1
    min_request_interval: float = 0.5
    #: 请求间隔抖动比例（±）。固定节奏是脚本特征，加抖动可降低被风控识别的概率。
    jitter_ratio: float = 0.2


@dataclass(frozen=True, slots=True)
class DetailConfig:
    """详情采集。

    以前这里还有两个"多久拉一次详情"的旋钮（``recheck_before_end_minutes``
    复查点、``max_recheck_per_cycle`` 单轮兜底数）：后台轮询按复查点挑几条拉，
    省请求量。用户 2026-11-15 要求"每条挨个去请求详情，从详情判断是否结束"——
    既然每轮都把关注中的拍品全部拉一遍，那两个"挑谁"的旋钮就没有意义了，
    一起删掉（不再留着让人以为改了会生效）。
    """

    enable: bool = True


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    """通知的节奏：在哪些「结束前复查点」提醒、侧栏看多远。

    ``points_minutes`` 是一组"距结束多少分钟"的点，到点就提醒一次。用户
    2026-11-14 要求"提醒改成结束前复查点这个逻辑"，而不是"开始提醒 + 每隔多久
    重复"那种推算出来的节奏：显式列出三个点，比"从 60 分钟起每 30 分钟一次"
    好读，也不会因为算错而多响。

    以前它和详情的复查点（``detail.recheck_before_end_minutes``）形态同构、
    只是用途不同；2026-11-15 详情改成"每轮全拉"之后，那边那组点已经删了，
    只剩这里还在用"距结束多少分钟"这个说法。

    侧栏窗口写死 2 小时过，用户 m21460 要求能配。
    """

    #: 距结束这些分钟时各提醒一次（升序，内部按它注册）。
    points_minutes: tuple[float, ...] = (15.0, 60.0, 360.0)
    #: 「出价被超过」是否额外用**价格比对**判断（站内信之外的第二条信号）。
    #:
    #: 站内信由服务端判定，最准但会漏发（实测有订单最终价高于我的出价、
    #: log 里却没有「出价被超过」）。价格比对每轮都做，规则是
    #: ``详情当前价 > 我的出价`` → 被超过，正向可靠、不反向推断。
    #: 关掉它就只剩站内信这一条（更安静，但会漏）。
    outbid_by_price: bool = True
    #: 侧栏窗口：未来多少分钟内将要发生的事才列出来。
    window_minutes: float = 120.0
    #: 拍卖结束时提醒（带成交价）。
    ended: bool = True
    #: 截止时间被**延后**时提醒（雅虎的自动延长通常就是这样发生的）。
    extended: bool = True
    #: 截止时间被**提前**时提醒。与延后分成两个开关，是因为含义相反——
    #: 延后是多给时间，提前是要立刻出手，有人只想被后一种打扰。
    shortened: bool = True


@dataclass(frozen=True, slots=True)
class RefreshConfig:
    auto_seconds: int = 180


@dataclass(frozen=True, slots=True)
class MessagesConfig:
    """站内消息通道（「出价被超过」）的开关。

    服务端会在你出过价的拍品被别人超过时写一条站内消息，这是它**已经算好**的
    事实，比自己轮询详情比对价格准。但它只对出过价的拍品有效，所以不能替代
    常规轮询，只能叠加在上面当触发器。
    """

    #: 是否轮询站内消息。关掉后「出价被超过」的插队拉取不再生效，
    #: 价格完全靠常规复查点刷新。
    enable: bool = True


@dataclass(frozen=True, slots=True)
class TranslateConfig:
    """标题与描述的翻译开关。

    翻译是挖煤姬服务端提供的（``Translate.TranslateHtml``），译文与原文一起
    展示。默认**关**：它要额外打网络请求，而且大多数人看日文原标题也认得。
    打开后列表与详情统一生效，译文落库缓存，一个拍品只翻一次。
    """

    enable: bool = False
    #: 目标语言。实测支持 ``zh-CN`` / ``zh-TW`` / ``en``。
    target: str = "zh-CN"
    #: 源语言。挖煤姬的商品文案基本都是日文。
    source: str = "ja"


@dataclass(frozen=True, slots=True)
class TransportConfig:
    mode: Literal["curl", "httpx", "curl_cffi"] = "curl"
    curl_path: str = ""
    impersonate: str = "chrome110"
    force_http2: bool = True


@dataclass(frozen=True, slots=True)
class LoginConfig:
    """浏览器登录方式的相关参数。"""

    #: 拉起浏览器时打开的地址
    url: str = "https://www.meruki.cn/"
    #: CDP 调试端口（用于自动捕获 Cookie）
    debug_port: int = 9222
    #: 等待用户在浏览器里完成登录的秒数
    wait_seconds: int = 180


@dataclass(frozen=True, slots=True)
class ChannelsConfig:
    """通知渠道：**界面提醒之外**的额外投递方式。

    界面里的提醒（右侧「通知队列」+ 状态行闪现）不受这里影响，永远是本地免费
    的；这一段只是决定要不要**再**推一份到 webhook / 邮箱 / QQ。
    默认全关——通知属于"额外动作"，不该默认往外发东西。
    """

    #: 通用 webhook：POST 一份 JSON ``{title, body, asin, kind}``
    webhook_enable: bool = False
    #: webhook 地址（Bark / ntfy / 企业微信机器人 / 飞书都能用各自的路由地址）
    webhook_url: str = ""
    #: webhook 超时秒数
    webhook_timeout_seconds: float = 5.0

    # ---- 邮件（SMTP）----
    #: 发信开关
    email_enable: bool = False
    #: SMTP 服务器，如 ``smtp.gmail.com`` / ``smtp.qq.com``
    email_smtp_host: str = ""
    #: 端口：465 走隐式 SSL，其它端口尝试 STARTTLS（服务器不支持就退回明文）
    email_smtp_port: int = 587
    #: 登录用户名（多数服务商要求填完整邮箱地址）；留空表示不登录
    email_username: str = ""
    #: 登录密码 / 授权码。**QQ、163 等要用"授权码"而不是登录密码**
    email_password: str = ""
    #: 发件人地址；留空则用 ``email_username``
    email_from: str = ""
    #: 收件人，多个用逗号分隔
    email_to: str = ""
    #: SMTP 超时秒数
    email_timeout_seconds: float = 20.0

    # ---- OneBot v11（QQ 机器人）----
    #: 推送开关
    onebot_enable: bool = False
    #: OneBot 实现的 HTTP API 根地址，如 ``http://127.0.0.1:3000``
    onebot_url: str = ""
    #: 访问令牌；填了就带 ``Authorization: Bearer <token>``
    onebot_token: str = ""
    #: 推送目标：``private:QQ号`` 或 ``group:群号``（只写数字按私聊处理）
    onebot_target: str = ""
    #: OneBot 超时秒数
    onebot_timeout_seconds: float = 5.0

    # ---- 微信个人号（ClawBot / 腾讯官方 iLink）----
    #: 打开后才能扫码挂载；关着时挂载屏直接说"去设置里打开"
    wechat_enable: bool = False
    #: 扫码换来的 bot_token；挂在 iLink 上时所有请求都用它做 Bearer
    wechat_token: str = ""
    #: 接入点，默认官方域名；登录成功后服务端可能回一个别的 baseurl，以返回值为准
    wechat_base_url: str = "https://ilinkai.weixin.qq.com"
    #: HTTP 超时秒数（长轮询另按 45 秒算，不受这个限制）
    wechat_timeout_seconds: float = 15.0
    # ---- QQ 官方机器人 ----
    #: 打开后才能扫码挂载；扫码会把 AppID / AppSecret 换回来存下面两项
    qq_enable: bool = False
    qq_app_id: str = ""
    qq_app_secret: str = ""
    #: 绑定服务域名。官方留了一个测试域名 ``test.q.qq.com``，改这里就能切
    qq_base_url: str = "q.qq.com"
    #: openapi 基座（换 token / 取网关 / 发消息）。默认官方；本地联调可指向假服务
    qq_api_base: str = "https://api.bot.qq.com"
    #: HTTP 超时秒数
    qq_timeout_seconds: float = 10.0


#: 配置的各个段落：名字 -> 数据类。
#:
#: **加一段只改这里**。``from_mapping`` / ``to_mapping`` / 未知键校验都从它派生；
#: 以前是三张名单各维护一份，加一段要改三处，漏一处就是"刚加的配置项被当成
#: 未知键、程序起不来"（真实踩过）。``login`` 单独解析（有 back-compat 分支），
#: 所以不在表里。
_SECTIONS: dict[str, type] = {
    "account": AccountConfig,
    "storage": StorageConfig,
    "fetch": FetchConfig,
    "detail": DetailConfig,
    "notify": NotifyConfig,
    "refresh": RefreshConfig,
    "messages": MessagesConfig,
    "translate": TranslateConfig,
    "transport": TransportConfig,
    "channels": ChannelsConfig,
}


@dataclass(frozen=True, slots=True)
class Config:
    account: AccountConfig = field(default_factory=AccountConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    detail: DetailConfig = field(default_factory=DetailConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    refresh: RefreshConfig = field(default_factory=RefreshConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)
    login: LoginConfig = field(default_factory=LoginConfig)
    timezone: str = "Asia/Shanghai"
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    warnings: tuple[str, ...] = ()

    @property
    def data_dir(self) -> Path:
        return Path(self.storage.data_dir)

    # ---------------------------------------------------------------- 构造

    @classmethod
    def defaults(cls) -> Config:
        """全默认配置。"""
        return cls()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> Config:
        """从配置字典构造（``settings`` 表读出来的就是这种结构）。

        Args:
            raw: 形如 ``{"refresh": {"auto_seconds": 180}, "timezone": "..."}``。

        Returns:
            校验后的配置。

        Raises:
            ConfigError: 含未知键或字段非法。
        """
        if not raw:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError("配置根节点必须是映射")

        raw = dict(raw)
        warnings: list[str] = []

        kw: dict[str, Any] = {
            name: _section(cls_, raw.get(name), name) for name, cls_ in _SECTIONS.items()
        }
        kw["login"] = _login_section(raw.get("login"))
        kw["timezone"] = _timezone(raw)
        kw["warnings"] = tuple(warnings)
        _reject_unknown(raw, {*_SECTIONS, "login", "timezone"})
        cfg = cls(**kw)
        return validate_config(cfg)

    # ---------------------------------------------------------------- 序列化

    def __post_init__(self) -> None:
        # 手工构造时允许直接给 dict（老写法 / 测试），这里统一成类型化配置，
        # 保证 Config 无论怎么构造出来都是同一种形态。
        if isinstance(self.channels, dict):
            object.__setattr__(
                self, "channels", _section(ChannelsConfig, self.channels, "channels")
            )
        # JSON 没有 tuple：从库里读回来的是 list，这里统一归一化，
        # 保证 Config 无论怎么构造出来都是同一种形态（便于比较与测试）。
        #
        # 每加一组"多个数"的配置项都要在这里补一行：漏了的后果不是报错，而是
        # ``Config(...) == Config(...)`` 悄悄变成 False（list != tuple），
        # 存库读回来再比一次就"配置变了"，定时刷新被无谓重启。
        for section_name, field_name in (("notify", "points_minutes"),):
            section = getattr(self, section_name)
            points = getattr(section, field_name)
            if not isinstance(points, tuple):
                object.__setattr__(
                    self, section_name, replace(section, **{field_name: tuple(points)})
                )

    def to_mapping(self) -> dict[str, Any]:
        """导出成可 JSON 序列化的字典（存库 / 迁移用）。"""
        out: dict[str, Any] = {}
        for name, cls_ in (*_SECTIONS.items(), ("login", LoginConfig)):
            section = getattr(self, name)
            out[name] = {f.name: _jsonable(getattr(section, f.name)) for f in fields(cls_)}
        out["timezone"] = self.timezone
        return out

    def to_flat(self) -> dict[str, Any]:
        """摊平成 settings 表的结构：``{"refresh.auto_seconds": 180, ...}``。"""
        return _flatten(self.to_mapping())


def _flatten(mapping: dict[str, Any]) -> dict[str, Any]:
    """嵌套配置摊平成 settings 表的 key：``section.name`` / 顶层项原样。"""
    out: dict[str, Any] = {}
    for name, value in mapping.items():
        if isinstance(value, dict):
            for sub, sub_value in value.items():
                out[f"{name}.{sub}"] = _jsonable(sub_value)
        else:
            out[name] = _jsonable(value)
    return out


def _jsonable(value: Any) -> Any:
    """tuple -> list（JSON 没有 tuple）。"""
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def validate_config(cfg: Config) -> Config:
    """整体校验：跨字段 / 语义约束。

    单字段类型与范围由 :mod:`meruki_tracker.core.config_options` 在编辑时把关，
    这里兜住"构造出来的值是否整体可用"。

    Raises:
        ConfigError: 校验不通过。
    """
    # 时区
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(
            f"timezone: 非法时区 {cfg.timezone!r}（{exc}）\n建议: 使用 IANA 名称，如 Asia/Shanghai"
        ) from exc

    # 登录参数
    lo, hi = LOGIN_PORT_RANGE
    if not lo <= cfg.login.debug_port <= hi:
        raise ConfigError(
            f"login.debug_port: 必须在 {lo}..{hi} 之间（当前: {cfg.login.debug_port}）"
        )
    lo, hi = LOGIN_WAIT_RANGE
    if not lo <= cfg.login.wait_seconds <= hi:
        raise ConfigError(
            f"login.wait_seconds: 必须在 {lo}..{hi} 之间（当前: {cfg.login.wait_seconds}）"
        )
    parsed = urlparse(cfg.login.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"login.url: 必须是 http/https 开头的完整地址（当前: {cfg.login.url!r}）")

    # 通知的提醒点：一组"距结束多少分钟"，越远越先响
    npoints = cfg.notify.points_minutes
    if not npoints:
        raise ConfigError("notify.points_minutes: 至少要有一个提醒点（距结束多少分钟）")
    if any(p <= 0 for p in npoints):
        raise ConfigError("notify.points_minutes: 提醒点必须大于 0 分钟")
    if cfg.notify.window_minutes <= 0:
        raise ConfigError("notify.window_minutes: 必须大于 0（侧栏看未来多久）")

    # 关键数值范围
    if cfg.refresh.auto_seconds < 0:
        raise ConfigError("refresh.auto_seconds: 不能为负")
    if cfg.fetch.timeout_seconds <= 0:
        raise ConfigError("fetch.timeout_seconds: 必须大于 0")
    if cfg.fetch.min_request_interval < 0:
        raise ConfigError("fetch.min_request_interval: 不能为负")
    # 抖动只有一处配置（``fetch.jitter_ratio``）：请求间隔、队列间隔、自动刷新
    # 间隔都用它。以前三个字段各存一份同样的 0.2，改一个不生效是必然的坑。
    if cfg.fetch.jitter_ratio < 0:
        raise ConfigError("抖动比例不能为负\n建议: 0 表示关闭抖动，0.2 表示 ±20%")
    if cfg.fetch.jitter_ratio > 1:
        raise ConfigError("抖动比例不能大于 1（100%）\n建议: 0.2 表示 ±20%")
    if not cfg.storage.data_dir:
        raise ConfigError("storage.data_dir: 不能为空")

    _validate_channels(cfg.channels)
    return cfg


def _require_http_url(value: str, *, field: str, what: str) -> None:
    """校验"必须是 http/https 完整地址"，错误消息里带上字段名与当前值。"""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"channels.{field}: {what}必须填 http/https 开头的完整地址（当前: {value!r}）"
        )


def _require_timeout(value: float, *, field: str, lo: float, hi: float) -> None:
    if not lo <= value <= hi:
        raise ConfigError(f"channels.{field}: 必须在 {lo:g}..{hi:g} 秒之间（当前: {value}）")


def _validate_channels(ch: ChannelsConfig) -> None:
    """校验通知渠道。

    **只在启用时才要求填齐**：关着的渠道留空是合法的（用户可能先存着地址、
    过两天再开），否则"想先关掉"都得先清空字段。
    """
    lo, hi = WEBHOOK_TIMEOUT_RANGE
    if ch.webhook_enable:
        _require_http_url(ch.webhook_url, field="webhook_url", what="启用 webhook 时")
    _require_timeout(ch.webhook_timeout_seconds, field="webhook_timeout_seconds", lo=lo, hi=hi)

    nlo, nhi = NET_TIMEOUT_RANGE
    if ch.email_enable:
        if not ch.email_smtp_host.strip():
            raise ConfigError("channels.email_smtp_host: 启用邮件通知时必须填 SMTP 服务器")
        if not ch.email_to.strip():
            raise ConfigError("channels.email_to: 启用邮件通知时必须填至少一个收件人")
        sender = (ch.email_from or ch.email_username).strip()
        if not sender:
            raise ConfigError(
                "channels.email_from: 启用邮件通知时必须有发件人"
                "（留空则用 channels.email_username，两个都空就填一个）"
            )
        if ch.email_username.strip() and not ch.email_password:
            raise ConfigError("channels.email_password: 填了用户名就要填密码/授权码")
    plo, phi = SMTP_PORT_RANGE
    if not plo <= ch.email_smtp_port <= phi:
        raise ConfigError(
            f"channels.email_smtp_port: 必须在 {plo}..{phi} 之间（当前: {ch.email_smtp_port}）"
        )
    _require_timeout(ch.email_timeout_seconds, field="email_timeout_seconds", lo=nlo, hi=nhi)

    if ch.onebot_enable:
        _require_http_url(ch.onebot_url, field="onebot_url", what="启用 OneBot 时")
        try:
            parse_onebot_target(ch.onebot_target)
        except ValueError as exc:
            raise ConfigError(f"channels.onebot_target: {exc}") from None
    _require_timeout(ch.onebot_timeout_seconds, field="onebot_timeout_seconds", lo=nlo, hi=nhi)

    # 微信个人号：打开开关才要求有凭据（第一次挂载前本来就没 token，扫码会补上）。
    if ch.wechat_enable:
        _require_http_url(ch.wechat_base_url, field="wechat_base_url", what="打开微信机器人时")
    _require_timeout(ch.wechat_timeout_seconds, field="wechat_timeout_seconds", lo=nlo, hi=nhi)

    # QQ 官方机器人：扫码会自己把 AppID/AppSecret 填回来，所以打开开关不强制先填。
    _require_timeout(ch.qq_timeout_seconds, field="qq_timeout_seconds", lo=nlo, hi=nhi)
    if ch.qq_enable and not ch.qq_base_url.strip():
        raise ConfigError("配置错误: channels.qq_base_url: 打开 QQ 机器人时不能为空")


def parse_onebot_target(text: str) -> tuple[str, str]:
    """解析 OneBot 推送目标。

    支持 ``private:12345`` / ``group:67890``；只写数字当成私聊（最常见的用法）。

    Returns:
        ``("private" | "group", 号码)``

    Raises:
        ValueError: 格式不对（消息里说清正确写法）。
    """
    raw = text.strip()
    if not raw:
        raise ValueError('没填推送目标；写法是 "private:QQ号" 或 "group:群号"')
    kind, _, ident = raw.partition(":")
    if not ident:
        kind, ident = "private", raw
    kind = kind.strip().lower()
    ident = ident.strip()
    if kind not in ("private", "group"):
        raise ValueError(f'不认识的目标类型 {kind!r}；只支持 "private" 或 "group"')
    if not ident.isdigit():
        raise ValueError(f"号码必须是纯数字（当前: {ident!r}）")
    return kind, ident


def _require_str(raw: dict, key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(
            f"{key}: 必须是非空字符串（当前: {value!r}）\n建议: 删除该行使用默认值 {default!r}"
        )
    return value


def _timezone(raw: dict) -> str:
    """校验时区：必须是系统认识的 IANA 名称。

    非法时区如果放过，会在 ``ZoneInfo(cfg.timezone)`` 处变成裸
    ``ZoneInfoNotFoundError`` traceback——必须在这里就转成 ConfigError。
    """
    name = _require_str(raw, "timezone", "Asia/Shanghai")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(
            f"timezone: 非法时区 {name!r}（{exc}）\n建议: 使用 IANA 名称，如 Asia/Shanghai"
        ) from exc
    return name


def _login_section(raw: Any) -> LoginConfig:
    """login 节：通用校验 + 范围/URL 校验。"""
    cfg: LoginConfig = _section(LoginConfig, raw, "login")
    lo, hi = LOGIN_PORT_RANGE
    if not lo <= cfg.debug_port <= hi:
        raise ConfigError(
            f"login.debug_port: 必须在 {lo}..{hi} 之间（当前: {cfg.debug_port}）\n"
            "建议: 使用 Chrome 调试端口，如 9222"
        )
    lo, hi = LOGIN_WAIT_RANGE
    if not lo <= cfg.wait_seconds <= hi:
        raise ConfigError(
            f"login.wait_seconds: 必须在 {lo}..{hi} 之间（当前: {cfg.wait_seconds}）\n"
            "建议: 0 = 只尝试一次；常用的完整等待时长如 180"
        )
    parsed = urlparse(cfg.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"login.url: 必须是 http/https 开头的完整地址（当前: {cfg.url!r}）\n"
            "建议: 例如 https://www.meruki.cn/"
        )
    return cfg


def _section(cls: type, raw: Any, where: str) -> Any:
    """dict -> 冻结 dataclass，未知键报错。"""
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: 必须是映射")
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        paths = ", ".join(f"{where}.{k}" for k in sorted(unknown))
        raise ConfigError(f"{paths}: 未知配置项\n建议: 检查拼写，或删除该项使用默认值")
    for f in fields(cls):
        if f.name in raw:
            _check_type(f"{where}.{f.name}", raw[f.name], f.type)
    try:
        return cls(**raw)
    except TypeError as exc:
        raise ConfigError(f"{where}: 值非法: {exc}") from exc


def _reject_unknown(raw: dict, known: set[str]) -> None:
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(
            f"配置顶层含未知配置项 {sorted(unknown)}\n建议: 检查拼写（可用: {sorted(known)}）"
        )


def _check_type(path: str, value: Any, annotation: Any) -> None:
    """最小类型检查：int / float / bool / str / 一组数字。"""
    ann = str(annotation)
    if ann.startswith("tuple[") and ann.endswith("]"):
        # 一组值（目前只有 notify.points_minutes）。不查这里的后果不是报错，
        # 而是往后跑：``"abc"`` 被 ``tuple()`` 拆成 ``('a','b','c')``，到
        # ``validate_config`` 的 ``p <= 0`` 才炸成裸 TypeError，用户看到的
        # 是一个和配置无关的 traceback，而且进不了界面改回来。
        if not isinstance(value, (list, tuple)):
            raise ConfigError(
                f"{path}: 必须是一组值（当前: {value!r}）\n建议: 写成数组，如 [15, 60, 360]"
            )
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ConfigError(
                    f"{path}: 每一项都必须是数字（当前含 {item!r}）\n建议: 写成数组，如 [15, 60, 360]"
                )
        return
    if ann == "bool" and not isinstance(value, bool):
        raise ConfigError(f"{path}: 必须是布尔值（当前: {value!r}）\n建议: 使用 true 或 false")
    if ann == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ConfigError(f"{path}: 必须是整数（当前: {value!r}）\n建议: 填入整数，或删除该行")
    if ann == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ConfigError(f"{path}: 必须是数字（当前: {value!r}）\n建议: 填入数字，或删除该行")
    if ann == "str" and not isinstance(value, str):
        raise ConfigError(f"{path}: 必须是字符串（当前: {value!r}）")
