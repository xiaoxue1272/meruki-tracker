"""QQ 官方机器人：扫码绑定，换回 AppID / AppSecret。

协议不是猜的，是从官方 npm 包 ``@tencent-connect/qqbot-connector@1.2.0`` 的
``dist/esm/qqbot-session.js`` 里读出来的：

1. ``POST https://q.qq.com/lite/create_bind_task`` body ``{"key": base64(32 随机字节)}``
   → ``{"retcode":0,"data":{"task_id":...}}``
2. 二维码内容 ``https://q.qq.com/qqbot/openclaw/connect.html?task_id=<id>&source=<源>&_wv=2``
   —— 手机 QQ 扫这张码，会列出你名下的机器人让你**选一个**绑定。
3. ``POST https://q.qq.com/lite/poll_bind_result`` body ``{"task_id":...}``
   轮询 2 秒一次；``status``：0 空 / 1 待扫 / 2 完成 / 3 过期。
4. 完成时返回 ``bot_appid`` 与 **加密的** ``bot_encrypt_secret``：把密文按
   ``iv(12) + ciphertext + tag(16)`` 切分，用第 1 步那个 ``key`` 做 AES-256-GCM 解密，
   得到明文 AppSecret。

为什么要自己实现：官方只发了 Node 包，而本项目是 Python，装一个 Node 运行时只为扫码
不值当。HTTP 接口本身很简单，加密也就一段 AES-GCM。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..net import open_url as _open_url

DEFAULT_HOST = "q.qq.com"
TEST_HOST = "test.q.qq.com"

#: 轮询间隔：官方 SDK 写死 2 秒，跟着它。
POLL_INTERVAL = 2.0
#: 二维码里的 ``_wv=2`` 是官方 SDK 带的，缺了会落到网页版视图。
CONNECT_PATH = "/qqbot/openclaw/connect.html"

# poll_bind_result 的 status
STATUS_NONE = 0
STATUS_PENDING = 1
STATUS_COMPLETED = 2
STATUS_EXPIRED = 3

_IV_LEN = 12
_TAG_LEN = 16


class QqBindError(Exception):
    """绑定流程里的可读错误（网络、协议、解密都会收敛成这个）。"""


@dataclass(frozen=True, slots=True)
class BindTask:
    task_id: str
    #: 解密 AppSecret 用的对称密钥，只活在内存里，不落盘。
    key: str


@dataclass(frozen=True, slots=True)
class BindResult:
    status: int
    app_id: str = ""
    encrypt_secret: str = ""
    user_openid: str = ""


def generate_key() -> str:
    """32 字节随机数的 base64——同时是 AES-256 的密钥。"""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _base_url(host: str) -> str:
    """``host`` 既可以是域名（拼成 https），也可以带协议——测试要打本地 http 服务。"""
    host = (host or DEFAULT_HOST).rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    return f"https://{host}"


def _post(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urlrequest.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with _open_url(req, timeout=timeout) as resp:
            raw = resp.read()
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read()[:200].decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise QqBindError(f"HTTP {exc.code}: {detail}".strip()) from exc
    except (urlerror.URLError, OSError) as exc:
        raise QqBindError(f"连不上 {url}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QqBindError(f"返回的不是 JSON: {raw[:200]!r}") from exc


def create_bind_task(*, host: str = DEFAULT_HOST, timeout: float = 10.0) -> BindTask:
    url = f"{_base_url(host)}/lite/create_bind_task"
    key = generate_key()
    data = _post(url, {"key": key}, timeout=timeout)
    if data.get("retcode") != 0:
        raise QqBindError(str(data.get("msg") or "create_bind_task 失败"))
    task_id = str(
        ((data.get("data") or {}) if isinstance(data.get("data"), dict) else {}).get("task_id")
        or ""
    )
    if not task_id:
        raise QqBindError("create_bind_task 没给 task_id")
    return BindTask(task_id=task_id, key=key)


def connect_url(task_id: str, *, source: str = "", host: str = DEFAULT_HOST) -> str:
    """二维码要编码的地址。手机 QQ 打开后让人选一个机器人绑定。"""
    from urllib.parse import quote

    return (
        f"{_base_url(host)}{CONNECT_PATH}"
        f"?task_id={quote(task_id, safe='')}&source={quote(source, safe='')}&_wv=2"
    )


def poll_bind_task(task_id: str, *, host: str = DEFAULT_HOST, timeout: float = 10.0) -> BindResult:
    url = f"{_base_url(host)}/lite/poll_bind_result"
    data = _post(url, {"task_id": task_id}, timeout=timeout)
    if data.get("retcode") != 0:
        raise QqBindError(str(data.get("msg") or "poll_bind_result 失败"))
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    payload = payload or {}
    return BindResult(
        status=int(payload.get("status") or STATUS_NONE),
        app_id=str(payload.get("bot_appid") or ""),
        encrypt_secret=str(payload.get("bot_encrypt_secret") or ""),
        user_openid=str(payload.get("user_openid") or ""),
    )


def decrypt_secret(encrypt_secret: str, key: str) -> str:
    """AES-256-GCM 解出明文 AppSecret。

    密文布局与官方 SDK 一致：``iv(12) || ciphertext || tag(16)``，密钥就是当初
    ``create_bind_task`` 发上去的那串 base64。
    """
    try:
        blob = base64.b64decode(encrypt_secret)
        aes_key = base64.b64decode(key)
    except (binascii.Error, ValueError) as exc:
        raise QqBindError(f"凭据解不开：{exc}") from exc
    if len(blob) <= _IV_LEN + _TAG_LEN:
        raise QqBindError("凭据太短，可能是服务端换了格式")
    iv = blob[:_IV_LEN]
    tag = blob[-_TAG_LEN:]
    ciphertext = blob[_IV_LEN:-_TAG_LEN]
    try:
        plain = AESGCM(aes_key).decrypt(iv, ciphertext + tag, None)
    except Exception as exc:
        raise QqBindError(f"凭据解不开（密钥不匹配？）：{exc}") from exc
    try:
        return plain.decode()
    except UnicodeDecodeError as exc:
        raise QqBindError("凭据不是文本") from exc


def encrypt_secret_for_test(app_secret: str, key: str) -> str:
    """只给测试用：按同样的布局造一段密文，验证解密路径。"""
    aes_key = base64.b64decode(key)
    iv = os.urandom(_IV_LEN)
    sealed = AESGCM(aes_key).encrypt(iv, app_secret.encode(), None)
    return base64.b64encode(iv + sealed).decode()


__all__ = [
    "DEFAULT_HOST",
    "POLL_INTERVAL",
    "STATUS_COMPLETED",
    "STATUS_EXPIRED",
    "STATUS_NONE",
    "STATUS_PENDING",
    "TEST_HOST",
    "BindResult",
    "BindTask",
    "QqBindError",
    "connect_url",
    "create_bind_task",
    "decrypt_secret",
    "generate_key",
    "poll_bind_task",
]
