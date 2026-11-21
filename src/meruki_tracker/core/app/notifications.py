"""通知渠道：把「到点的提醒」投递到界面之外的地方。

界面里的提醒（右侧「通知队列」+ 状态行闪现）永远是本地的、免费的，不依赖
本模块。这里做的是**额外**投递：webhook、邮件（SMTP）、OneBot v11（QQ 机器人）。

**投递绝不影响主流程**

:class:`Notifier` 的约定是"自己吞掉异常"：采集数据的价值远高于一条通知，
通知失败也不能让刷新或界面出问题。每个渠道实现都遵守这条，``CompositeNotifier``
再兜一层——一个渠道炸了不影响其它渠道。

**为什么不在 ``notify()`` 里直接发请求**

``notify()`` 由 TUI 的 1Hz tick（或刷新循环）同步调用，跑在事件循环线程上。
发 HTTP、连 SMTP 都可能耗上几秒——同步做完会把界面卡住。所以所有渠道都把真正的
动作丢到后台（``asyncio.to_thread``），``notify()`` 立即返回。

**加一个渠道要做的事**

1. 写一个类实现 :class:`Notifier` 的 ``notify``；
2. 在 :func:`build_notifier` 里按配置加上它；
3. 在 ``config.ChannelsConfig`` 里加开关。

界面与刷新循环都不用改——它们只依赖协议。
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Protocol, runtime_checkable
from urllib import error as urlerror
from urllib import request as urlrequest

from ..config import ChannelsConfig, parse_onebot_target
from ..net import open_url as _open_url

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Notification:
    """一条待投递的通知。

    Attributes:
        title: 一行标题（邮件主题、IM 首行都用它）。
        body: 正文，说明发生了什么（通常是拍品名 + 事件）。
        asin: 相关拍品，方便通道做去重或点开。
        kind: 事件种类，当前有 ``ending``（即将结束）与 ``outbid``（出价被超过）。
    """

    title: str
    body: str
    asin: str = ""
    kind: str = "outbid"


@runtime_checkable
class Notifier(Protocol):
    """通知通道协议。

    **实现方必须吞掉自己的异常**：通知失败绝不能影响刷新结果——采集数据的
    价值远高于一条通知，何况通知本来就是"顺手"做的事。实在想报错请自己打日志。
    """

    def notify(self, note: Notification) -> None:
        """投递一条通知。失败不抛异常。"""
        ...


@runtime_checkable
class RetryableChannel(Protocol):
    """收消息长连的通道：退出时由**实现方**说这次退出值不值得自动重来。

    ``WeChatBot`` / ``QqBot`` 没有共同基类（它们只是恰好都有 ``run()`` /
    ``send_all()``），所以这里是 Protocol 而不是继承——硬造一个基类只会把
    两个本来独立的类绑在一起。TUI 的后台任务结束时问一句 ``can_retry()``：
    真能重来就自动重连，否则提示用户介入（重新扫码）。

    注意：两个 ``run()`` 内部**各自**已经处理了瞬时错误（微信 sleep(5) 重试、
    QQ 指数退避），能走到"任务退出"这一步的往往是终态（微信会话过期）或直接
    取消。``can_retry()`` 是给这些边缘情况和未来新增通道留的口子。
    """

    def can_retry(self) -> bool:
        """这次退出能否自动重启长连。False = 要用户介入（如重新扫码）。"""
        ...


class NullNotifier:
    """什么都不做的通道（没有任何渠道启用时的默认实现）。

    保留它是为了让调用方可以无条件调用 ``notifier.notify(...)``，不用到处判空——
    空判断散在各处才是真正的麻烦。
    """

    def notify(self, note: Notification) -> None:
        return None


class CompositeNotifier:
    """把一条通知扇出给多个渠道。

    **一个渠道失败不影响其它渠道**，也不影响调用方：每个 ``notify`` 单独兜
    ``Exception``。顺序按传入顺序，全部同步返回（真正的 IO 在各渠道内部异步化）。
    """

    def __init__(self, children: list[Notifier]) -> None:
        self._children = tuple(children)

    @property
    def children(self) -> tuple[Notifier, ...]:
        """实际生效的渠道（调试 / 测试用）。"""
        return self._children

    def notify(self, note: Notification) -> None:
        for child in self._children:
            try:
                child.notify(note)
            except Exception:  # 兜一层：渠道自己该吞，这里防它忘了
                log.warning("通知渠道 %s 抛异常（已忽略）", type(child).__name__, exc_info=True)


def _dispatch(make_coro: Callable[[], Awaitable[Any]], *, what: str) -> None:
    """把真正的投递丢到后台，避免卡住事件循环。

    ``notify()`` 是同步协议，调用点都在事件循环里。这里起一个 task 让它
    并行跑；没有正在运行的事件循环时（例如脚本里直接调用）就退化成
    "什么都不做 + 记一条 debug"——通知不该因此报错。

    参数是**零参工厂**而不是现成的协程对象：先判断有没有事件循环，再决定
    要不要创建协程。否则没有循环时那个协程对象没人 await，Python 会报
    ``coroutine ... was never awaited``（很吵，而且说明确实白造了对象）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("没有事件循环，%s 的通知被跳过", what)
        return

    task = loop.create_task(make_coro())
    task.add_done_callback(_swallow)


def _swallow(task: asyncio.Task[Any]) -> None:
    """后台任务的异常不能变成 "Task exception was never retrieved"。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("通知投递失败（已忽略）: %s", exc)


#: 同一个渠道、同一类错误在这个窗口内只记一次 warning（秒）。
_FAILURE_WINDOW = 600.0

_failure_logged: dict[str, float] = {}


def _log_failure(what: str, exc: BaseException) -> None:
    """投递失败记一条 **warning**，同一渠道同类错误十分钟内只说一次。

    为什么不能是 ``debug``：默认日志级别是 INFO（``setup_logging``），记 debug
    等于**永远看不到**——用户开了 webhook、地址写错，界面一切正常、日志一个字
    没有，只能靠猜。2026-11-18 审计实测：连发三次到没人监听的端口，INFO 及
    以上可见的日志是 0 条。

    为什么要节流：一次刷新可能同时提醒好几条，渠道挂了就是连珠炮；同一个渠道
    同类错误十分钟内只报第一下，够定位问题又不至于把日志刷满。
    """
    key = f"{what}:{type(exc).__name__}"
    now = time.monotonic()
    last = _failure_logged.get(key)
    if last is not None and now - last < _FAILURE_WINDOW:
        return
    _failure_logged[key] = now
    log.warning("%s投递失败（已忽略；同类错误 10 分钟内只报一次）: %s", what, exc, exc_info=True)


def _body_text(note: Notification) -> str:
    """把一条通知摊平成纯文本（邮件正文 / IM 消息共用）。

    ``body`` 里已经带上了拍品号就不要再补一行——「出价被超过」的正文本身就是
    ``名称（拍品 xxx）``，补了会变成同一个号出现两次，看着像写错了。
    """
    lines = [note.body]
    if note.asin and note.asin not in note.body:
        lines.append(f"拍品: {note.asin}")
    lines.append(f"时间: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}")
    return "\n".join(lines)


def _im_text(note: Notification) -> str:
    """IM 消息：标题 + 正文，尽量短。"""
    return f"{note.title}\n{note.body}"


class EmailNotifier:
    """邮件通知（SMTP）。

    **端口决定加密方式**：465 走隐式 SSL（``SMTP_SSL``），其它端口先建立明文
    连接再尝试 ``STARTTLS``；服务器不支持 STARTTLS 就退回明文（给局域网里的
    自建 MTA 留条路）。

    **密码**：QQ / 163 / Gmail 这类服务商要求的是"授权码"而不是账号密码，填错
    会收到 ``SMTPAuthenticationError``——那属于用户配置问题，这里只记日志。
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipients: tuple[str, ...],
        timeout: float = 20.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._recipients = recipients
        self._timeout = timeout

    def notify(self, note: Notification) -> None:
        _dispatch(lambda: self._post(note), what="邮件")

    async def _post(self, note: Notification) -> None:
        try:
            await asyncio.to_thread(self._send, note)
        except Exception as exc:
            _log_failure("邮件", exc)

    def _send(self, note: Notification) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[挖煤姬] {note.title}"
        msg["From"] = self._sender
        msg["To"] = ", ".join(self._recipients)
        # 显式用 base64：默认的 8bit 要求服务器宣告 8BITMIME，而且中文正文在
        # 8bit 下有些客户端会解成 \uXXXX 转义。base64 老服务器也认。
        msg.set_content(_body_text(note), cte="base64")

        if self._port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                self._host,
                self._port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with server:
            if self._port != 465:
                try:
                    server.starttls(context=ssl.create_default_context())
                except smtplib.SMTPNotSupportedError:
                    log.debug("SMTP 服务器不支持 STARTTLS，按明文发送")
            if self._username:
                server.login(self._username, self._password)
            server.send_message(msg, from_addr=self._sender, to_addrs=list(self._recipients))


class OneBotV11Notifier:
    """OneBot v11（QQ 机器人）的 HTTP API 推送。

    只依赖标准 HTTP 接口，所以 go-cqhttp / Lagrange.OneBot / NapCat 等任何
    遵循 OneBot v11 的实现都能用：

    - 私聊：``POST {base}/send_private_msg`` ``{"user_id": "...", "message": "..."}``
    - 群聊：``POST {base}/send_group_msg`` ``{"group_id": "...", "message": "..."}``
    - 配了 token 就带 ``Authorization: Bearer <token>``

    地址与目标由用户填（目标写法见 :func:`parse_onebot_target`）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        target: str = "",
        timeout: float = 5.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._kind, self._ident = parse_onebot_target(target)
        self._timeout = timeout

    def notify(self, note: Notification) -> None:
        _dispatch(lambda: self._post(note), what="OneBot")

    async def _post(self, note: Notification) -> None:
        try:
            await asyncio.to_thread(self._send, note)
        except Exception as exc:
            _log_failure("OneBot", exc)

    def api_path(self) -> str:
        """这条通知会打哪个接口（测试与排查时看）。"""
        return "send_group_msg" if self._kind == "group" else "send_private_msg"

    def _send(self, note: Notification) -> None:
        payload: dict[str, Any] = {"message": _im_text(note), "auto_escape": True}
        if self._kind == "group":
            payload["group_id"] = self._ident
        else:
            payload["user_id"] = self._ident
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urlrequest.Request(
            f"{self._base}/{self.api_path()}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with _open_url(req, timeout=self._timeout) as resp:
                resp.read(1)
        except urlerror.HTTPError as exc:
            detail = exc.read(200).decode("utf-8", "replace")
            # 抛出去而不是只记日志：``notify()`` 那条路本来就会吞异常，但
            # 「发送测试消息」需要知道到底成没成。
            raise RuntimeError(f"OneBot 返回 {exc.code}: {detail}") from exc


class WebhookNotifier:
    """通用 webhook：POST 一份 JSON。

    地址由用户自填，因此 Bark / ntfy / 企业微信机器人 / 飞书 / 自建服务都能接。
    请求体固定为::

        {"title": "...", "body": "...", "asin": "...", "kind": "ending"}

    **不用 httpx**：本机环境变量里的 ``no_proxy`` 带方括号（``[::1]``）会让
    httpx 在构造期就抛 ``InvalidURL``（同一个坑在 transport 上踩过）。标准库
    ``urllib`` 的代理匹配是纯字符串比较，不会崩，而且天然尊重系统代理设置。
    """

    def __init__(self, url: str, *, timeout: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout

    def notify(self, note: Notification) -> None:
        _dispatch(lambda: self._post(note), what="webhook")

    async def _post(self, note: Notification) -> None:
        payload = {
            "title": note.title,
            "body": note.body,
            "asin": note.asin,
            "kind": note.kind,
        }
        try:
            await asyncio.to_thread(self._send, payload)
        except Exception as exc:
            _log_failure("webhook", exc)

    def _send(self, payload: dict[str, str]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with _open_url(req, timeout=self._timeout) as resp:
            resp.read(1)  # 读一点就够，只是等它完成


def build_notifier(
    channels: ChannelsConfig | dict[str, Any] | None = None,
    *,
    wechat: Any = None,
    qq: Any = None,
) -> Notifier:
    """根据配置组装通知渠道。

    Args:
        channels: ``Config.channels``（也接受裸 dict，方便旧调用点与测试）。
        wechat: 已挂载的微信通道（:class:`~meruki_tracker.core.app.wechat.WeChatBot`）。
            传 ``None`` 表示还没挂载，微信渠道会被跳过。
        qq: 已建好的 QQ 通道（:class:`~meruki_tracker.core.app.qqchannel.QqBot`）。
            传 ``None`` 表示凭据不全，QQ 渠道会被跳过。

    Returns:
        启用的渠道组合；一个都没启用时返回 :class:`NullNotifier`
        （调用方永远不用判空）。
    """
    cfg = _as_channels(channels)
    children: list[Notifier] = []
    if cfg.webhook_enable and cfg.webhook_url:
        children.append(WebhookNotifier(cfg.webhook_url, timeout=cfg.webhook_timeout_seconds))
    if cfg.email_enable and cfg.email_smtp_host and cfg.email_to:
        recipients = tuple(part.strip() for part in cfg.email_to.split(",") if part.strip())
        sender = (cfg.email_from or cfg.email_username).strip()
        if not (recipients and sender):
            # 开了邮件却配不全：以前是**静默跳过**，界面看不出区别，用户以为
            # 通知发出去了。留一条 warning，至少日志里能查到。
            log.warning(
                "邮件通知已启用但配置不完整（收件人 %r / 发件人 %r），本次不投递",
                cfg.email_to,
                sender,
            )
        if recipients and sender:
            children.append(
                EmailNotifier(
                    host=cfg.email_smtp_host,
                    port=cfg.email_smtp_port,
                    username=cfg.email_username.strip(),
                    password=cfg.email_password,
                    sender=sender,
                    recipients=recipients,
                    timeout=cfg.email_timeout_seconds,
                )
            )
    if cfg.onebot_enable and cfg.onebot_url and cfg.onebot_target:
        try:
            children.append(
                OneBotV11Notifier(
                    base_url=cfg.onebot_url,
                    token=cfg.onebot_token,
                    target=cfg.onebot_target,
                    timeout=cfg.onebot_timeout_seconds,
                )
            )
        except ValueError:  # 目标格式不对：配置校验会拦住，这里只是别炸
            log.warning("OneBot 推送目标格式不对，已跳过（%r）", cfg.onebot_target)
    if cfg.wechat_enable and wechat is not None:
        from .wechat import WeChatNotifier

        children.append(WeChatNotifier(wechat))
    if cfg.qq_enable and qq is not None:
        from .qqchannel import QqNotifier

        children.append(QqNotifier(qq))
    if not children:
        return NullNotifier()
    if len(children) == 1:
        return children[0]
    return CompositeNotifier(children)


async def send_test(
    channels: ChannelsConfig | dict[str, Any] | None = None,
    *,
    wechat: Any = None,
    qq: Any = None,
) -> list[tuple[str, bool, str]]:
    """给每个**已启用**的渠道发一条测试消息，逐条报告成败。

    与 :func:`build_notifier` 的区别：这里要的是"到底通不通"，所以每条都
    ``await`` 真实发送并捕获异常，而不是丢到后台就返回。调用方是用户的
    主动操作（不是 1Hz tick），可以等。

    Args:
        channels: ``Config.channels``。
        wechat: 已挂载的微信通道；``None`` 表示还没挂载。

    Returns:
        ``[(渠道名, 是否成功, 说明)]``。一个都没启用时返回空列表——调用方据此
        提示用户去打开一个渠道，而不是显示"0/0 成功"。
    """
    cfg = _as_channels(channels)
    note = Notification(
        title="测试消息",
        body="这是一条来自 meruki-tracker 的测试通知；能看到它说明这条通道是通的。",
        kind="test",
    )
    results: list[tuple[str, bool, str]] = []

    async def _run(name: str, fn: Callable[[], Any]) -> None:
        try:
            await asyncio.to_thread(fn)
        except Exception as exc:  # 逐条报错，不因为一个渠道炸掉整次测试
            results.append((name, False, _explain(exc)))
        else:
            results.append((name, True, "已发送"))

    # webhook
    if cfg.webhook_enable:
        if not cfg.webhook_url:
            results.append(("Webhook", False, "还没填地址"))
        else:
            hook = WebhookNotifier(cfg.webhook_url, timeout=cfg.webhook_timeout_seconds)
            payload = {"title": note.title, "body": note.body, "asin": "", "kind": "test"}
            await _run("Webhook", lambda: hook._send(payload))

    # 邮件
    if cfg.email_enable:
        missing = []
        if not cfg.email_smtp_host:
            missing.append("SMTP 地址")
        if not cfg.email_to:
            missing.append("收件人")
        sender = (cfg.email_from or cfg.email_username).strip()
        if not sender:
            missing.append("发件人")
        recipients = tuple(part.strip() for part in cfg.email_to.split(",") if part.strip())
        if missing:
            results.append(("邮件", False, f"还差 {'、'.join(missing)}"))
        elif not recipients:
            results.append(("邮件", False, "收件人地址看不懂"))
        else:
            mailer = EmailNotifier(
                host=cfg.email_smtp_host,
                port=cfg.email_smtp_port,
                username=cfg.email_username.strip(),
                password=cfg.email_password,
                sender=sender,
                recipients=recipients,
                timeout=cfg.email_timeout_seconds,
            )
            await _run("邮件", lambda: mailer._send(note))

    # OneBot v11
    if cfg.onebot_enable:
        missing = []
        if not cfg.onebot_url:
            missing.append("HTTP 地址")
        if not cfg.onebot_target:
            missing.append("推送目标")
        if missing:
            results.append(("OneBot", False, f"还差 {'、'.join(missing)}"))
        else:
            try:
                bot = OneBotV11Notifier(
                    base_url=cfg.onebot_url,
                    token=cfg.onebot_token,
                    target=cfg.onebot_target,
                    timeout=cfg.onebot_timeout_seconds,
                )
            except ValueError as exc:
                results.append(("OneBot", False, str(exc)))
            else:
                await _run("OneBot", lambda: bot._send(note))

    # 微信（iLink）
    if cfg.wechat_enable:
        if wechat is None:
            results.append(("微信", False, "还没扫码挂载"))
        else:
            try:
                sent = await asyncio.to_thread(wechat.send_all, _im_text(note))
            except Exception as exc:
                results.append(("微信", False, _explain(exc)))
            else:
                if sent:
                    results.append(("微信", True, f"已发送给 {sent} 个会话"))
                else:
                    results.append(("微信", False, "没有可发的会话；先在手机上给机器人发一条消息"))

    # QQ 官方机器人（WebSocket 收 + openapi 发）
    if cfg.qq_enable:
        if not (cfg.qq_app_id and cfg.qq_app_secret):
            results.append(("QQ", False, "还没扫码挂载"))
        elif qq is None:
            results.append(("QQ", False, "凭据有了，但收消息的连接还没起来"))
        else:
            try:
                sent = await asyncio.to_thread(qq.send_all, _im_text(note))
            except Exception as exc:
                results.append(("QQ", False, _explain(exc)))
            else:
                if sent:
                    results.append(("QQ", True, f"已发送给 {sent} 个会话"))
                else:
                    results.append(("QQ", False, "没有可发的会话；先在 QQ 里给机器人发一条消息"))

    return results


def _explain(exc: BaseException) -> str:
    """把异常翻成一句人能读的失败原因。"""
    text = str(exc).strip()
    return text or type(exc).__name__


def _as_channels(channels: ChannelsConfig | dict[str, Any] | None) -> ChannelsConfig:
    if isinstance(channels, ChannelsConfig):
        return channels
    if not channels:
        return ChannelsConfig()
    try:
        known = set(ChannelsConfig.__dataclass_fields__)
        return ChannelsConfig(**{k: v for k, v in channels.items() if k in known})
    except TypeError:
        log.warning("channels 配置无法解析，按默认值处理", exc_info=True)
        return ChannelsConfig()
