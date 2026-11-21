"""QQ 官方机器人通道：扫码换来的 AppID/AppSecret → WebSocket 收消息 → 主动推提醒。

**扫码只解决"拿到凭据"**，拿到之后要能推送还得走这条链路：
``getAppAccessToken`` 换 token → ``GET /gateway`` 拿 wss 地址 → 连上 Identify
（只订阅 ``GROUP_AND_C2C_EVENT``）→ 收 ``C2C_MESSAGE_CREATE`` /
``GROUP_AT_MESSAGE_CREATE``，把对方的 openid 落库 → 之后用
``POST /v2/users/{openid}/messages``（或 ``/v2/groups/{group_openid}/messages``）
主动发提醒。

**为什么要把 openid 落库**

和微信的 ``context_token`` 同理：主动消息必须知道发给谁，而 openid 只出现在
用户先说话的那条事件里。用户随手给机器人发一句"你好"，我们就记下了；之后
不用他再说话也能收到提醒。不跑 WebSocket 就永远拿不到这个 id——这也是为什么
只做扫码是不够的。

**协议来源**

全部取自官方文档（``bot.q.qq.com/wiki/develop/api-v2``），不是猜的：
鉴权 ``api-use.html``、事件订阅 ``event-emit.html``、发送单聊 ``autogen/api/
v2_users_user_openid_messages.post.html``、单聊事件 ``autogen/event/
c2c_message_create.html``、网关 ``openapi/wss/url_get.html``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from ..errors import MerukiTrackerError
from ..net import open_url as _open_url
from .notifications import Notification, Notifier, _dispatch, _im_text

log = logging.getLogger(__name__)

#: 开放平台统一地址（不是 ``api.sgroup.qq.com``，那是旧频道 API 的域名）
DEFAULT_API = "https://api.bot.qq.com"

#: 只订阅群聊与单聊事件：频道相关的 intents 没申请权限会被直接掐断连接
INTENT_GROUP_AND_C2C = 1 << 25

#: token 提前这么多秒换新的（官方文档：过期前 60 秒内请求会给新的）
TOKEN_MARGIN = 60.0

#: 断线重连的基础等待秒数（指数退避到 60 秒封顶）
RECONNECT_BASE = 2.0
RECONNECT_MAX = 60.0

EVENT_C2C = "C2C_MESSAGE_CREATE"
EVENT_GROUP = "GROUP_AT_MESSAGE_CREATE"

KIND_C2C = "c2c"
KIND_GROUP = "group"


class QqBotError(MerukiTrackerError):
    """QQ 机器人接口返回的错误（网络、鉴权、频控都归这一类）。"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """发一次 JSON 请求并解析响应；错误统一翻成 :class:`QqBotError`。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with _open_url(req, timeout=timeout) as resp:
            raw = resp.read()
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # 读错误体失败不影响报错
            detail = ""
        raise QqBotError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except (urlerror.URLError, OSError) as exc:
        raise QqBotError(f"连不上 {url}: {exc}") from exc
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QqBotError(f"返回的不是 JSON: {raw[:200]!r}") from exc
    return parsed if isinstance(parsed, dict) else {}


@dataclass(slots=True)
class QqBotClient:
    """QQ openapi 的最小客户端：换 token、取网关、发消息。

    token 自己缓存：官方文档说每次请求不会刷新 token，而 7200 秒里可能发几十条
    提醒，每条都去换一次纯属浪费；``TOKEN_MARGIN`` 内视为过期，提前换新的。
    """

    app_id: str
    app_secret: str
    api_base: str = DEFAULT_API
    timeout: float = 10.0
    _token: str = field(default="", repr=False)
    _expires_at: float = field(default=0.0, repr=False)

    def access_token(self) -> str:
        """取（或复用）access_token。"""
        now = time.monotonic()
        if self._token and now < self._expires_at:
            return self._token
        data = _request_json(
            "POST",
            f"{self.api_base.rstrip('/')}/app/getAppAccessToken",
            payload={"appId": self.app_id, "clientSecret": self.app_secret},
            timeout=self.timeout,
        )
        token = str(data.get("access_token") or "").strip()
        if not token:
            reason = data.get("message") or data.get("code") or "响应里没有 access_token"
            raise QqBotError(f"换 token 失败：{reason}")
        try:
            expires = float(data.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires = 7200.0
        self._token = token
        self._expires_at = now + max(1.0, expires - TOKEN_MARGIN)
        return token

    def invalidate_token(self) -> None:
        """丢掉缓存的 token，下一次取用会重新换。

        服务端**提前**作废 token 是真实发生过的（换过 AppSecret、控制台重置）：
        这时自有缓存还认为它在有效期内，会一直拿着坏 token 重连/发消息，直到
        两小时后 ``expires_in`` 到期——期间所有提醒都发不出去。
        """
        self._token = ""
        self._expires_at = 0.0

    def _maybe_invalidate(self, exc: BaseException) -> None:
        """错误像"token 无效/过期"时丢掉缓存，让下一次重试换新的。"""
        text = str(exc)
        low = text.lower()
        looks_auth = (
            "401" in low
            or "accesstoken" in low
            or "access token" in low
            or "无效" in text
            or "过期" in text
        )
        if looks_auth:
            log.warning("QQ access_token 被服务端拒绝，已丢弃缓存，下次重试换新的: %s", exc)
            self.invalidate_token()

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"QQBot {self.access_token()}"}

    def gateway(self) -> str:
        """取 wss 接入点。"""
        # 先取 header 再发请求：换 token 本身失败（appid 无效之类）应该原样抛出，
        # 不能被下面的"像是 token 被拒"判断收走，否则日志会指错方向。
        headers = self._auth()
        try:
            data = _request_json(
                "GET",
                f"{self.api_base.rstrip('/')}/gateway",
                headers=headers,
                timeout=self.timeout,
            )
        except QqBotError as exc:
            self._maybe_invalidate(exc)
            raise
        url = str(data.get("url") or "").strip()
        if not url:
            raise QqBotError("没拿到网关地址")
        return url

    def send_c2c(self, openid: str, text: str) -> None:
        """给单聊用户发一条文本（主动消息，不需要 msg_id）。"""
        self._send(f"{self.api_base.rstrip('/')}/v2/users/{openid}/messages", text)

    def send_group(self, group_openid: str, text: str) -> None:
        """给群里发一条文本。"""
        self._send(f"{self.api_base.rstrip('/')}/v2/groups/{group_openid}/messages", text)

    def _send(self, url: str, text: str) -> None:
        headers = self._auth()
        try:
            data = _request_json(
                "POST",
                url,
                payload={"content": text, "msg_type": 0},
                headers=headers,
                timeout=self.timeout,
            )
        except QqBotError as exc:
            self._maybe_invalidate(exc)
            raise
        # 官方接口出错时也可能回 200 + code/message，不能只看 HTTP 状态
        code = data.get("code")
        if code not in (None, 0):
            raise QqBotError(f"发送失败 {code}: {data.get('message') or ''}")


@dataclass(slots=True)
class QqSessionStore:
    """``(kind, openid)`` 落库：谁跟机器人说过话，之后就能收提醒。"""

    conn: Any
    _cache: dict[tuple[str, str], str] = field(default_factory=dict)

    def load(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT kind, openid FROM qq_sessions ORDER BY updated_at DESC"
        ).fetchall()
        self._cache = {(str(row[0]), str(row[1])): "" for row in rows}
        return list(self._cache)

    def save(self, kind: str, openid: str, *, updated_at: str = "") -> None:
        stamp = updated_at or _now_iso()
        if not kind or not openid:
            return
        self._cache[(kind, openid)] = stamp
        self.conn.execute(
            "INSERT INTO qq_sessions (kind, openid, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(kind, openid) DO UPDATE SET updated_at = excluded.updated_at",
            (kind, openid, stamp),
        )
        self.conn.commit()

    def clear(self) -> None:
        self._cache.clear()
        self.conn.execute("DELETE FROM qq_sessions")
        self.conn.commit()

    def sessions(self) -> list[tuple[str, str]]:
        if not self._cache:
            self.load()
        return sorted(self._cache)


class QqBot:
    """收消息（WebSocket 长连）+ 主动发提醒。跑在 TUI 的后台任务里。"""

    def __init__(self, *, client: QqBotClient, store: QqSessionStore) -> None:
        self._client = client
        self._store = store
        self._last_error = ""
        self._online = False

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def online(self) -> bool:
        """WebSocket 是否在线（QQ 要求机器人在线才肯收消息接口的请求）。"""
        return self._online

    def can_retry(self) -> bool:
        """QQ 网关的 ``run()`` 自带无限指数退避重连，只有被 cancel 才退出；
        能走到任务结束基本就是程序在关，不该再自动重连。
        """
        return False

    def send_all(self, text: str) -> int:
        """给所有已知会话发同一条文本，返回成功条数。"""
        sent = 0
        for kind, openid in self._store.sessions():
            try:
                if kind == KIND_GROUP:
                    self._client.send_group(openid, text)
                else:
                    self._client.send_c2c(openid, text)
            except QqBotError as exc:
                self._last_error = str(exc)
                log.warning("QQ 提醒发送失败（%s %s）: %s", kind, openid, exc)
                continue
            sent += 1
        return sent

    async def run(self, *, connect: Any = None) -> None:
        """网关主循环：连上、心跳、收事件，断了就退避重连。"""
        delay = RECONNECT_BASE
        while True:
            try:
                await self._session(connect)
                delay = RECONNECT_BASE  # 正常断开（比如 op 7）也不必久等
            except asyncio.CancelledError:
                self._online = False
                raise
            except Exception as exc:
                self._last_error = str(exc)
                log.warning("QQ 网关断开，%.0f 秒后重连: %s", delay, exc)
                self._online = False
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX, delay * 2)

    async def _session(self, connect: Any) -> None:
        """一次连接的完整生命周期。"""
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect

            connect = ws_connect
        url = await asyncio.to_thread(self._client.gateway)
        token = await asyncio.to_thread(self._client.access_token)
        async with connect(url, open_timeout=15.0, close_timeout=5.0) as ws:
            # 用一格列表装 s：心跳任务要读"最新值"，闭包绑变量会被 ruff 判成
            # 绑循环变量（B023），绑值又会把首次的 None 冻住
            seq_box: list[int | None] = [None]
            heartbeat: asyncio.Task[None] | None = None
            try:
                while True:
                    raw = await ws.recv()
                    try:
                        payload = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        log.debug("QQ 网关收到非 JSON 帧，忽略")
                        continue
                    op = payload.get("op")
                    if payload.get("s") is not None:
                        seq_box[0] = payload["s"]
                    if op == 10:  # Hello
                        interval = float(
                            (payload.get("d") or {}).get("heartbeat_interval") or 45000
                        )
                        if heartbeat is not None:
                            heartbeat.cancel()
                        heartbeat = asyncio.create_task(
                            self._heartbeat(ws, interval / 1000.0, lambda: seq_box[0])
                        )
                        await ws.send(
                            json.dumps(
                                {
                                    "op": 2,
                                    "d": {
                                        "token": f"QQBot {token}",
                                        "intents": INTENT_GROUP_AND_C2C,
                                        "shard": [0, 1],
                                        "properties": {
                                            "$os": "linux",
                                            "$browser": "meruki-tracker",
                                            "$device": "meruki-tracker",
                                        },
                                    },
                                }
                            )
                        )
                    elif op == 0:  # Dispatch
                        event = str(payload.get("t") or "")
                        self._online = True
                        self._handle(event, payload.get("d"))
                    elif op in (7, 9):  # 要求重连 / 会话失效
                        log.info("QQ 网关要求重连（op %s）", op)
                        return
                    elif op == 11:  # 心跳 ACK
                        continue
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()

    async def _heartbeat(self, ws: Any, interval: float, seq_of: Any) -> None:
        try:
            while True:
                # 服务器说多久就多久：卡一个最小值会让心跳比要求慢，连接会被踢
                await asyncio.sleep(max(0.1, interval))
                await ws.send(json.dumps({"op": 1, "d": seq_of()}))
        except asyncio.CancelledError:
            raise
        except Exception:  # 连接断了由外层重连，这里静默退出
            log.debug("QQ 心跳发送失败", exc_info=True)

    def _handle(self, event: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if event == EVENT_C2C:
            author = data.get("author") or {}
            openid = str(author.get("user_openid") or author.get("id") or "").strip()
            if openid:
                self._store.save(KIND_C2C, openid)
                log.info("QQ 单聊会话已记录（%s）", openid)
        elif event == EVENT_GROUP:
            group_openid = str(data.get("group_openid") or "").strip()
            if group_openid:
                self._store.save(KIND_GROUP, group_openid)
                log.info("QQ 群会话已记录（%s）", group_openid)


class QqNotifier(Notifier):
    """把提醒丢给 QQ 通道，投递失败不影响其它渠道。"""

    def __init__(self, bot: QqBot) -> None:
        self._bot = bot

    def notify(self, note: Notification) -> None:
        text = _im_text(note)
        _dispatch(lambda: asyncio.to_thread(self._bot.send_all, text), what="QQ")


def configured(client: Any) -> bool:
    """凭据齐了没有（``ChannelsConfig`` 或裸 dict 都能吃）。"""
    if client is None:
        return False
    if isinstance(client, dict):
        return bool(str(client.get("qq_app_id") or "").strip()) and bool(
            str(client.get("qq_app_secret") or "").strip()
        )
    return bool(str(getattr(client, "qq_app_id", "") or "").strip()) and bool(
        str(getattr(client, "qq_app_secret", "") or "").strip()
    )


def build_client(channels: Any) -> QqBotClient | None:
    """按配置造客户端；凭据不全返回 ``None``（不是错误，只是还没挂载）。"""
    if channels is None:
        return None

    def _get(name: str, default: str = "") -> str:
        if isinstance(channels, dict):
            return str(channels.get(name) or default).strip()
        return str(getattr(channels, name, default) or default).strip()

    app_id = _get("qq_app_id")
    secret = _get("qq_app_secret")
    if not app_id or not secret:
        return None
    return QqBotClient(
        app_id=app_id,
        app_secret=secret,
        api_base=_get("qq_api_base", DEFAULT_API) or DEFAULT_API,
        timeout=float(getattr(channels, "qq_timeout_seconds", 10.0) or 10.0)
        if not isinstance(channels, dict)
        else float(channels.get("qq_timeout_seconds") or 10.0),
    )


def session_store(conn: Any) -> QqSessionStore:
    return QqSessionStore(conn)


__all__ = [
    "DEFAULT_API",
    "EVENT_C2C",
    "EVENT_GROUP",
    "INTENT_GROUP_AND_C2C",
    "KIND_C2C",
    "KIND_GROUP",
    "QqBot",
    "QqBotClient",
    "QqBotError",
    "QqNotifier",
    "QqSessionStore",
    "build_client",
    "configured",
    "session_store",
]
