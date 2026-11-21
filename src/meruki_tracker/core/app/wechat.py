"""微信个人号（ClawBot）接入：腾讯官方 iLink Bot API。

协议要点都是对着官方协议文档与参考 SDK 的线格式实现的，**别凭印象改**：

- 基座默认 ``https://ilinkai.weixin.qq.com``；登录成功后服务端可能回一个不同的
  ``baseurl``，之后所有业务请求都要用它，不能还用默认值。
- 登录是扫码：``POST /ilink/bot/get_bot_qrcode?bot_type=3`` 拿 ``qrcode`` 与
  ``qrcode_img_content``（后者才是要编码成二维码的字符串），再 ``GET
  /ilink/bot/get_qrcode_status?qrcode=...`` 轮询 ``wait → scaned → confirmed``
  （``expired`` 要重新取码，``scaned_but_redirect`` 要换 IDC host 继续等）。
- 收消息**不是 WebSocket，是长轮询**：``POST /ilink/bot/getupdates``，服务端挂
  约 35 秒才返回。``get_updates_buf`` 是不透明游标，每次把返回值原样带回去。
  ``ret``/``errcode`` 为 ``-14`` 表示会话过期，只能重新扫码。
- 每条入站消息带 ``context_token``，**发消息必须原样回传**，否则服务端不知道
  该投递到哪个会话。所以「主动推送提醒」的前提是对方先给机器人发过一条消息；
  这条约束是协议本身决定的，绕不过去。
- 所有 POST 头：``Content-Type``、``AuthorizationType: ilink_bot_token``、
  ``Authorization: Bearer <token>``、``X-WECHAT-UIN: base64(十进制随机 uint32)``；
  连 GET（取码/轮询）也要带 ``iLink-App-Id`` 与 ``iLink-App-ClientVersion``。

HTTP 用标准库 ``urllib`` 而不是 httpx：本机 ``no_proxy`` 里带 ``[::1]`` 会让
httpx 在构造 ``URLPattern`` 时就抛 ``InvalidURL``（transport 上踩过这个坑）。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..errors import MerukiTrackerError, NetworkError
from ..net import open_url as _open_url
from .notifications import Notification, Notifier, _dispatch

log = logging.getLogger("app.wechat")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
#: 服务端用 ``base_info`` 判断客户端代际；实测两个值都能用，这里跟官方协议文档。
CHANNEL_VERSION = "2.0.0"
BOT_AGENT = "meruki-tracker/1.0"
APP_ID = "bot"
CLIENT_VERSION = "256"
#: getupdates 是长轮询，服务端会挂 35 秒左右，超时必须比它长。
LONG_POLL_TIMEOUT = 45.0

STATE_WAIT = "wait"
STATE_SCANED = "scaned"
STATE_CONFIRMED = "confirmed"
STATE_EXPIRED = "expired"
STATE_REDIRECT = "scaned_but_redirect"
STATE_BOUND = "binded_redirect"
STATE_NEED_CODE = "need_verifycode"
STATE_CODE_BLOCKED = "verify_code_blocked"


class WeChatError(MerukiTrackerError):
    """微信通道的业务错误（网关返回 ret 不为 0 之类）。"""


class WeChatSessionExpired(WeChatError):
    """会话过期（``ret``/``errcode`` = -14），只能重新扫码。"""


def random_uin() -> str:
    """``X-WECHAT-UIN``：随机 uint32 → 十进制字符串 → base64。"""
    value = random.getrandbits(32)
    return base64.b64encode(str(value).encode()).decode()


def common_headers() -> dict[str, str]:
    return {"iLink-App-Id": APP_ID, "iLink-App-ClientVersion": CLIENT_VERSION}


def auth_headers(token: str) -> dict[str, str]:
    headers = common_headers()
    headers.update(
        {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {token}",
            "X-WECHAT-UIN": random_uin(),
        }
    )
    return headers


def _request(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """发一个 JSON 请求并解回 dict；错误统一包成 ``NetworkError``/``WeChatError``。"""
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _open_url(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise NetworkError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(f"连不上 {url}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        snippet = raw.decode("utf-8", "replace")[:200]
        raise NetworkError(f"返回的不是 JSON: {snippet}") from exc


def fetch_qr(
    base_url: str = DEFAULT_BASE_URL,
    *,
    local_tokens: tuple[str, ...] = (),
    timeout: float = 15.0,
) -> tuple[str, str]:
    """取登录二维码，返回 ``(qrcode, 二维码内容)``。

    ``local_tokens`` 把本地已存的 bot token 报给服务端：如果这个机器人已经绑定
    过当前客户端，服务端会回 ``binded_redirect`` 而不是再开一个重复会话。
    """
    url = f"{base_url.rstrip('/')}/ilink/bot/get_bot_qrcode?bot_type=3"
    data = _request(
        "POST",
        url,
        payload={"local_token_list": list(local_tokens)},
        headers=common_headers(),
        timeout=timeout,
    )
    qrcode = str(data.get("qrcode") or "")
    content = str(data.get("qrcode_img_content") or "")
    if not qrcode or not content:
        raise WeChatError("服务端没返回二维码内容")
    return qrcode, content


def poll_qr(
    base_url: str,
    qrcode: str,
    *,
    verify_code: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """查一次扫码状态。返回原始 dict（含 ``status`` 与登录成功后的凭据）。"""
    # 必须 ``safe=""``：二维码串里可能带 ``/``，默认的 quote 不转义它，拼进查询
    # 就变成路径分隔符了（参考实现用的是 QueryEscape，行为一致）。
    url = f"{base_url.rstrip('/')}/ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode, safe='')}"
    if verify_code:
        url += f"&verify_code={urllib.parse.quote(verify_code, safe='')}"
    return _request("GET", url, headers=common_headers(), timeout=timeout)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """一条入站消息：只需要发提醒的话，用户 id + 会话 token 就够了。"""

    user_id: str
    context_token: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class UpdatesResult:
    messages: tuple[IncomingMessage, ...] = ()
    cursor: str = ""
    session_expired: bool = False


def _text_of(msg: dict[str, Any]) -> str:
    items = msg.get("item_list") or []
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text_item = item.get("text_item")
        if isinstance(text_item, dict):
            text = text_item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


class IlinkClient:
    """登录之后的业务客户端：收消息 + 发消息。"""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
    ) -> None:
        self._token = token
        self._base = base_url.rstrip("/") or DEFAULT_BASE_URL
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base

    def _post(
        self, path: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        body = dict(payload)
        body.setdefault("base_info", {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT})
        data = _request(
            "POST",
            f"{self._base}{path}",
            payload=body,
            headers=auth_headers(self._token),
            timeout=self._timeout if timeout is None else timeout,
        )
        errcode = int(data.get("errcode") or 0)
        ret = int(data.get("ret") or 0)
        if errcode == -14 or ret == -14:
            raise WeChatSessionExpired("微信会话已过期，请重新扫码挂载")
        if errcode or ret:
            message = str(data.get("errmsg") or f"ret={ret}")
            raise WeChatError(f"微信接口返回错误: {message}")
        return data

    def get_updates(self, cursor: str = "") -> UpdatesResult:
        """长轮询一批新消息。服务端会挂 30 多秒，别在事件循环里直接调。"""
        data = self._post(
            "/ilink/bot/getupdates",
            {"get_updates_buf": cursor},
            timeout=max(self._timeout, LONG_POLL_TIMEOUT),
        )
        messages: list[IncomingMessage] = []
        for raw in data.get("msgs") or []:
            if not isinstance(raw, dict):
                continue
            user_id = str(raw.get("from_user_id") or "")
            context_token = str(raw.get("context_token") or "")
            if not user_id or not context_token:
                continue
            messages.append(
                IncomingMessage(user_id=user_id, context_token=context_token, text=_text_of(raw))
            )
        return UpdatesResult(
            messages=tuple(messages),
            cursor=str(data.get("get_updates_buf") or cursor),
        )

    def send_text(self, user_id: str, context_token: str, text: str) -> None:
        msg = {
            "from_user_id": "",
            "to_user_id": user_id,
            "client_id": str(uuid.uuid4()),
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        self._post("/ilink/bot/sendmessage", {"msg": msg})


@dataclass(slots=True)
class ContextStore:
    """``user_id → context_token``，落库，重启后还能继续给老会话发提醒。"""

    conn: Any
    _cache: dict[str, str] = field(default_factory=dict)

    def load(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT user_id, context_token FROM wechat_context ORDER BY updated_at DESC"
        ).fetchall()
        self._cache = {str(row[0]): str(row[1]) for row in rows}
        return dict(self._cache)

    def save(self, user_id: str, context_token: str, *, updated_at: str) -> None:
        self._cache[user_id] = context_token
        self.conn.execute(
            "INSERT INTO wechat_context (user_id, context_token, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET context_token = excluded.context_token, "
            "updated_at = excluded.updated_at",
            (user_id, context_token, updated_at),
        )
        self.conn.commit()

    def clear(self) -> None:
        self._cache.clear()
        self.conn.execute("DELETE FROM wechat_context")
        self.conn.commit()

    def tokens(self) -> dict[str, str]:
        if not self._cache:
            self.load()
        return dict(self._cache)


class WeChatBot:
    """收消息 + 主动发提醒。跑在 TUI 的后台任务里，不阻塞界面。"""

    def __init__(self, *, client: IlinkClient, store: ContextStore) -> None:
        self._client = client
        self._store = store
        self._cursor = ""
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    def can_retry(self) -> bool:
        """退出后能否自动重连。会话过期是终态——token 已作废，重连只会再吃
        ``errcode -14``，必须重新扫码；其它原因（尚未出现，但给新通道留口子）
        才算可重试。
        """
        return "会话已过期" not in self._last_error and "重新扫码" not in self._last_error

    def send_all(self, text: str) -> int:
        """给所有已知会话发同一条文本，返回成功条数。"""
        sent = 0
        for user_id, context_token in self._store.tokens().items():
            try:
                self._client.send_text(user_id, context_token, text)
            except WeChatError as exc:
                self._last_error = str(exc)
                log.warning("微信提醒发送失败（%s）: %s", user_id, exc)
                continue
            sent += 1
        return sent

    async def run(self) -> None:
        """长轮询循环。放到 ``asyncio.to_thread`` 里跑，别占住事件循环。"""
        import asyncio
        from datetime import UTC, datetime

        self._store.load()
        # 失败计数：长轮询偶尔断一次是常态（35 秒长连被中间设备掐掉），记 debug
        # 无所谓；但这里失败后每 5 秒重试且**永不放弃**，地址写错、网关持续 500、
        # token 被换掉都走这条路。一直失败必须是能在默认 INFO 级别查到的——
        # 否则微信通道静默死掉，用户只觉得"提醒怎么不来了"。
        failures = 0
        while True:
            try:
                result = await asyncio.to_thread(self._client.get_updates, self._cursor)
            except WeChatSessionExpired as exc:
                self._last_error = str(exc)
                log.warning("微信会话过期，提醒通道已停: %s", exc)
                return
            except Exception as exc:
                self._last_error = str(exc)
                failures += 1
                if failures == 1 or failures % 20 == 0:
                    log.warning("微信长轮询失败（第 %d 次），继续重试: %s", failures, exc)
                else:
                    log.debug("微信长轮询失败（第 %d 次）: %s", failures, exc)
                await asyncio.sleep(5.0)
                continue
            failures = 0
            self._cursor = result.cursor or self._cursor
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            for message in result.messages:
                self._store.save(message.user_id, message.context_token, updated_at=now)
            if not result.messages:
                # 服务端正常会挂住 35 秒；立刻返回说明碰到空闲边界，别空转。
                await asyncio.sleep(1.0)


class WeChatNotifier(Notifier):
    """把提醒丢给微信通道，投递失败不影响其它渠道。"""

    def __init__(self, bot: WeChatBot) -> None:
        self._bot = bot

    def notify(self, note: Notification) -> None:
        text = f"{note.title}\n{note.body}"
        _dispatch(lambda: __import__("asyncio").to_thread(self._bot.send_all, text), what="微信")


def configured_token(channels: Any) -> str:
    """从配置里取微信 bot token（``ChannelsConfig`` 或旧式 dict 都能吃）。"""
    if channels is None:
        return ""
    if isinstance(channels, dict):
        return str(channels.get("wechat_token") or "").strip()
    return str(getattr(channels, "wechat_token", "") or "").strip()


def configured_base_url(channels: Any) -> str:
    if channels is None:
        return DEFAULT_BASE_URL
    if isinstance(channels, dict):
        return str(channels.get("wechat_base_url") or "").strip() or DEFAULT_BASE_URL
    return str(getattr(channels, "wechat_base_url", "") or "").strip() or DEFAULT_BASE_URL


def context_store(conn: Any) -> ContextStore:
    return ContextStore(conn)


__all__ = [
    "DEFAULT_BASE_URL",
    "STATE_CODE_BLOCKED",
    "STATE_CONFIRMED",
    "STATE_EXPIRED",
    "STATE_NEED_CODE",
    "STATE_REDIRECT",
    "STATE_SCANED",
    "STATE_WAIT",
    "ContextStore",
    "IlinkClient",
    "IncomingMessage",
    "UpdatesResult",
    "WeChatBot",
    "WeChatError",
    "WeChatNotifier",
    "WeChatSessionExpired",
    "configured_base_url",
    "configured_token",
    "context_store",
    "fetch_qr",
    "poll_qr",
]
