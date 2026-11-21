"""扫码挂载：把机器人挂到聊天软件上。

两条路都是官方协议，不是第三方网关：

- **微信个人号**：腾讯 iLink（ClawBot），扫码拿 ``bot_token``，之后能收发消息。
  线格式在 :mod:`meruki_tracker.core.app.wechat`。
- **QQ 官方机器人**：开放平台的扫码绑定，手机 QQ 扫码后**选一个机器人**，就换回
  它的 AppID / AppSecret。线格式在 :mod:`meruki_tracker.core.app.qqbot`。

QQ 以前没有做，是因为我以为它只能 AppID+AppSecret 手填；读了官方
``@tencent-connect/qqbot-connector`` 的实现才发现扫码绑定一直是有的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from . import qqbot
from .wechat import (
    DEFAULT_BASE_URL,
    STATE_BOUND,
    STATE_CODE_BLOCKED,
    STATE_CONFIRMED,
    STATE_EXPIRED,
    STATE_NEED_CODE,
    STATE_REDIRECT,
    STATE_SCANED,
    STATE_WAIT,
    WeChatError,
    configured_base_url,
    configured_token,
    fetch_qr,
    poll_qr,
)

KIND_WECHAT = "wechat"
KIND_QQ = "qq"

STATE_PENDING = "pending"
STATE_SCANNED = "scanned"
STATE_SUCCESS = "success"
STATE_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class QrChallenge:
    """一次登录需要扫的码。

    ``payload`` 是要编码成二维码的字符串，``token`` 是渠道自己的会话句柄
    （iLink 里就是 ``qrcode``）。
    """

    kind: str
    payload: str
    token: str = ""
    hint: str = ""


@dataclass(frozen=True, slots=True)
class QrStatus:
    state: str
    message: str = ""
    account: str = ""
    #: 挂载成功后拿到的长期凭据，界面负责存进配置。微信是 ``bot_token``；
    #: QQ 是 ``{"app_id": .., "app_secret": ..}`` 的 JSON（要存两个值）。
    credential: str = ""
    base_url: str = ""

    @property
    def done(self) -> bool:
        return self.state in (STATE_SUCCESS, STATE_FAILED)


class QrProvider(Protocol):
    kind: str
    label: str

    def fetch(self) -> QrChallenge: ...

    def poll(self, challenge: QrChallenge) -> QrStatus: ...

    def close(self) -> None: ...


class IlinkProvider:
    """微信个人号：iLink 扫码登录。"""

    kind = KIND_WECHAT
    label = "微信"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        existing_token: str = "",
        timeout: float = 15.0,
    ) -> None:
        self._base = base_url.rstrip("/") or DEFAULT_BASE_URL
        self._poll_base = self._base
        self._existing_token = existing_token
        self._timeout = timeout

    def fetch(self) -> QrChallenge:
        tokens = (self._existing_token,) if self._existing_token else ()
        qrcode, content = fetch_qr(self._base, local_tokens=tokens, timeout=self._timeout)
        self._poll_base = self._base
        return QrChallenge(
            kind=self.kind,
            payload=content,
            token=qrcode,
            hint="用微信扫码，手机上确认挂载",
        )

    def poll(self, challenge: QrChallenge) -> QrStatus:
        data = poll_qr(self._poll_base, challenge.token, timeout=self._timeout)
        status = str(data.get("status") or STATE_WAIT)

        if status == STATE_CONFIRMED:
            bot_token = str(data.get("bot_token") or "")
            if not bot_token:
                return QrStatus(STATE_FAILED, "扫码成功但服务端没给凭据，请重新取码")
            account = str(data.get("ilink_bot_id") or data.get("ilink_user_id") or "微信")
            return QrStatus(
                STATE_SUCCESS,
                f"已挂载：{account}",
                account=account,
                credential=bot_token,
                base_url=str(data.get("baseurl") or self._base),
            )
        if status == STATE_SCANED:
            return QrStatus(STATE_SCANNED, "已扫码，请在手机上确认")
        if status == STATE_REDIRECT:
            host = str(data.get("redirect_host") or "")
            if host:
                self._poll_base = f"https://{host}"
            return QrStatus(STATE_SCANNED, "已扫码，正在切换接入点…")
        if status == STATE_EXPIRED:
            return QrStatus(STATE_FAILED, "二维码已过期，按 r 重新取码")
        if status == STATE_BOUND:
            return QrStatus(
                STATE_FAILED,
                "这个微信已经绑在别的客户端上了；先在那边退出，或换个微信号",
            )
        if status == STATE_NEED_CODE:
            return QrStatus(
                STATE_FAILED,
                "服务端要求在微信里输入配对码（暂不支持），请重新取码再试",
            )
        if status == STATE_CODE_BLOCKED:
            return QrStatus(STATE_FAILED, "配对码错误次数过多，按 r 重新取码")
        return QrStatus(STATE_PENDING, "等待扫码")

    def close(self) -> None:
        """没有长连接要断，留个空实现让调用方可以无条件关。"""


class QqProvider:
    """QQ 官方机器人：手机 QQ 扫码，选一个机器人，换回 AppID / AppSecret。"""

    kind = KIND_QQ
    label = "QQ"

    def __init__(self, *, host: str = qqbot.DEFAULT_HOST, timeout: float = 10.0) -> None:
        self._host = host or qqbot.DEFAULT_HOST
        self._timeout = timeout
        #: 每次取码都要留着 key——成功时用它解 AppSecret，丢了就只能重扫。
        self._keys: dict[str, str] = {}

    def fetch(self) -> QrChallenge:
        task = qqbot.create_bind_task(host=self._host, timeout=self._timeout)
        self._keys[task.task_id] = task.key
        return QrChallenge(
            kind=self.kind,
            payload=qqbot.connect_url(task.task_id, host=self._host),
            token=task.task_id,
            hint="用手机 QQ 扫码，选一个机器人绑定",
        )

    def poll(self, challenge: QrChallenge) -> QrStatus:
        try:
            result = qqbot.poll_bind_task(challenge.token, host=self._host, timeout=self._timeout)
        except qqbot.QqBindError as exc:
            return QrStatus(STATE_FAILED, str(exc))
        if result.status == qqbot.STATUS_COMPLETED:
            key = self._keys.pop(challenge.token, "")
            if not result.app_id or not result.encrypt_secret:
                return QrStatus(STATE_FAILED, "扫码成功但服务端没给完整凭据，请重新取码")
            try:
                secret = qqbot.decrypt_secret(result.encrypt_secret, key) if key else ""
            except qqbot.QqBindError as exc:
                return QrStatus(STATE_FAILED, str(exc))
            if not secret:
                return QrStatus(STATE_FAILED, "凭据解不开（本次会话的密钥丢了），请重新取码")
            payload = json.dumps({"app_id": result.app_id, "app_secret": secret})
            return QrStatus(
                STATE_SUCCESS,
                f"已挂载：{result.app_id}",
                account=result.app_id,
                credential=payload,
            )
        if result.status == qqbot.STATUS_EXPIRED:
            return QrStatus(STATE_FAILED, "二维码已过期，按 r 重新取码")
        if result.status == qqbot.STATUS_PENDING:
            return QrStatus(STATE_SCANNED, "已扫码，请在手机上选择机器人并确认")
        return QrStatus(STATE_PENDING, "等待扫码")

    def close(self) -> None:
        self._keys.clear()


def build_provider(channels: Any, kind: str = "") -> QrProvider | None:
    """造一个可扫码的渠道；渠道没开就返回 ``None``（界面负责解释）。

    Args:
        kind: 指定渠道（``"wechat"`` / ``"qq"``）。配置页里每个渠道有**自己的**
            扫码行，点哪行就只该取那一个渠道的码——指定了却还没开，返回 ``None``。
            留空时按启用情况挑一个，QQ 优先（它的码一步就能换回 AppID/AppSecret，
            微信那条要手机确认）。
    """
    if channels is None:
        return None

    def flag(name: str, default: Any = False) -> Any:
        if isinstance(channels, dict):
            return channels.get(name, default)
        return getattr(channels, name, default)

    if kind and not flag(f"{kind}_enable"):
        # 指定了渠道却没打开：不偷偷给你扫另一个渠道的码
        return None
    if kind == KIND_QQ or (not kind and flag("qq_enable")):
        return QqProvider(
            host=str(flag("qq_base_url", qqbot.DEFAULT_HOST) or qqbot.DEFAULT_HOST),
            timeout=float(flag("qq_timeout_seconds", 10.0)),
        )
    if not flag("wechat_enable"):
        return None
    return IlinkProvider(
        base_url=configured_base_url(channels),
        existing_token=configured_token(channels),
        timeout=float(
            channels.get("wechat_timeout_seconds", 15.0)
            if isinstance(channels, dict)
            else getattr(channels, "wechat_timeout_seconds", 15.0)
        ),
    )


__all__ = [
    "KIND_QQ",
    "KIND_WECHAT",
    "STATE_FAILED",
    "STATE_PENDING",
    "STATE_SCANNED",
    "STATE_SUCCESS",
    "IlinkProvider",
    "QqProvider",
    "QrChallenge",
    "QrProvider",
    "QrStatus",
    "WeChatError",
    "build_provider",
]
