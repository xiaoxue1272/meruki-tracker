"""HTTP 传输层：只负责发请求，不做错误语义判断。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# header 名/值中出现即视为注入企图的字符（CRLF 注入 + 空字节截断）。
_FORBIDDEN_HEADER_CHARS = ("\r", "\n", "\x00")


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "ignore")


class Transport(Protocol):
    """HTTP 传输接口。实现必须把底层异常包装成 errors.NetworkError / WafBlockedError。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Response: ...

    async def aclose(self) -> None: ...


def validate_header(name: str, value: str) -> None:
    """校验单个 header：名称与值都不得含 CR / LF / NUL（防 CRLF 头注入）。

    Raises:
        ValueError: 名称或值非法。
    """
    if not isinstance(name, str) or not isinstance(value, str):
        raise ValueError("header 名称与值必须是字符串")
    for label, text in (("名称", name), ("值", value)):
        for bad in _FORBIDDEN_HEADER_CHARS:
            if bad in text:
                shown = bad.encode("unicode_escape").decode("ascii")
                raise ValueError(f"header {label}含非法字符 {shown}")


def check_headers(headers: dict[str, str] | None) -> None:
    """批量校验 header，任何一项非法立即失败（绝不静默发送）。"""
    for name, value in (headers or {}).items():
        validate_header(name, value)


class HttpxTransport:
    """基于 httpx 的传输实现（默认）。"""

    def __init__(self) -> None:
        import httpx

        from ...errors import NetworkError

        # http2 依赖可选的 h2 包（pyproject 里的 httpx 未带 [http2]）。
        # 无条件写 http2=True 会在没装 h2 的机器上于**构造期**抛 ImportError，
        # 把"配置 transport.mode: httpx"变成启动即崩。
        http2 = importlib.util.find_spec("h2") is not None

        # trust_env=False：不读 HTTP_PROXY / NO_PROXY 等环境变量。
        # 真实踩过——某台机器的 NO_PROXY 里写了带方括号的 "[::1]"，
        # httpx 把它当 URLPattern 解析，在**构造期**抛 InvalidURL: Invalid port ':1]'，
        # 应用启动即崩，且错误信息完全看不出跟代理有关。
        # 本项目只访问固定域名，走系统代理没有意义，直连行为也更可预测。
        try:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                http2=http2,
                timeout=30.0,
                trust_env=False,
            )
        except Exception as exc:  # 构造失败一律转成可读网络错误
            raise NetworkError(f"HTTP 客户端初始化失败: {exc}") from exc

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Response:
        import httpx

        from ...errors import NetworkError, WafBlockedError

        check_headers(headers)
        try:
            r = await self._client.request(
                method, url, headers=headers, json=json_body, timeout=timeout
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            raise NetworkError(f"网络连接失败: {exc}") from exc
        except httpx.RemoteProtocolError as exc:
            msg = str(exc).lower()
            if "reset" in msg or "disconnected" in msg:
                raise WafBlockedError(f"连接被服务端重置（疑似拦截）: {exc}") from exc
            raise NetworkError(f"协议错误: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"请求失败: {exc}") from exc
        return Response(
            status=r.status_code,
            headers=tuple(r.headers.items()),
            body=r.content,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class CurlSubprocessTransport:
    """通过系统 curl 子进程请求。

    实测（2026-10-29，macOS）：`web.meruki.cn` 的 WAF 会拦截 Python 原生 TLS 握手
    （LibreSSL/OpenSSL 指纹），而系统 curl 使用原生 TLS 栈可正常握手。
    因此 curl 子进程是当前唯一可用的通道。

    响应读取方式：body 由 curl 直接写入临时文件（-o），状态码与 header 走 -D 的临时文件。
    curl 的 -D 输在跟随重定向/100-continue 时会有多段，因此按"最后一段非 1xx"取最终响应。
    """

    #: 除 --max-time 外额外等待 curl 自行退出的余量（秒）
    KILL_MARGIN = 2.0

    def __init__(self, curl_path: str | None = None) -> None:
        import shutil

        self._curl = curl_path or shutil.which("curl")
        if not self._curl:
            from ...errors import NetworkError

            raise NetworkError("未找到系统 curl，无法建立连接")

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Response:
        return await asyncio.to_thread(self._request_sync, method, url, headers, json_body, timeout)

    def _request_sync(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        json_body: dict[str, Any] | None,
        timeout: float,
    ) -> Response:
        from ...errors import NetworkError, WafBlockedError

        with tempfile.TemporaryDirectory(prefix="meruki-curl-") as tmpdir:
            body_path = Path(tmpdir) / "body"
            head_path = Path(tmpdir) / "headers"

            cmd = [
                self._curl,
                "-s",
                "-S",
                # 跟重定向。**不加 -L 时 curl 把 30x 当终态**（httpx 那边是
                # follow_redirects=True，两条通道行为必须一致）：cookie 过期被
                # 302 到登录页、站点做 http→https 或区域跳转时，拿到的就是一个
                # 空 body + 302，_parse_body 只能判"响应不是合法 JSON"，用户看到
                # 的是「站点改版」而不是「登录过期」。
                "-L",
                "--max-redirs",
                "3",
                # 30x 之后保持原来的方法（否则 curl 会把 POST 降级成 GET，
                # 所有 RPC 都会变成"参数丢了"）
                "--post301",
                "--post302",
                "--post303",
                "--max-time",
                f"{max(float(timeout), 0.001):g}",
                "-X",
                method.upper(),
                "-D",
                str(head_path),
                "-o",
                str(body_path),
            ]
            check_headers(headers)
            for k, v in (headers or {}).items():
                cmd += ["-H", f"{k}: {v}"]
            payload: bytes | None = None
            if json_body is not None:
                payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                cmd += ["--data-binary", "@-"]
            cmd.append(url)

            try:
                proc = subprocess.run(
                    cmd,
                    input=payload,
                    capture_output=True,
                    timeout=max(float(timeout), 0.0) + self.KILL_MARGIN,
                )
            except subprocess.TimeoutExpired as exc:
                raise NetworkError(f"请求超时（{timeout}s）") from exc
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "ignore").strip()
                low = err.lower()
                if "reset" in low or "recv failure" in low or "(56)" in low or "(35)" in low:
                    raise WafBlockedError(f"连接被重置（疑似 WAF）: {err[:120]}")
                raise NetworkError(f"请求失败: {err[:120]}")

            body = body_path.read_bytes() if body_path.exists() else b""
            raw_head = head_path.read_bytes() if head_path.exists() else b""

        status, hdrs = _parse_last_response_head(raw_head)
        if status == 0:
            raise NetworkError("curl 响应格式异常（缺少 HTTP 状态行）")
        return Response(status=status, headers=tuple(hdrs), body=body)

    async def aclose(self) -> None:
        return None


def _parse_last_response_head(raw: bytes) -> tuple[int, list[tuple[str, str]]]:
    """解析 curl -D 的输出，返回最后一个非 1xx 响应块的状态码与 header。

    应对三种形态：
    - 单响应：``HTTP/1.1 200 OK\\r\\n...``
    - 302 + 200：两次状态行，取后一段
    - 100-continue + 200：取最后一个非 1xx 段

    Args:
        raw: -D 文件内容（可能被截断，允许不完整）。

    Returns:
        (status, headers)；完全没有状态行时 status 为 0。
    """
    text = raw.decode("utf-8", "ignore")
    blocks: list[list[str]] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("HTTP/"):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)

    status = 0
    hdrs: list[tuple[str, str]] = []
    for block in blocks:
        try:
            code = int(block[0].split()[1])
        except (IndexError, ValueError):
            continue
        if 100 <= code < 200:
            continue  # 1xx 是中间响应，不是最终结果
        status = code
        hdrs = []
        for line in block[1:]:
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            hdrs.append((k.strip(), v.strip()))
    return status, hdrs


class CurlCffiTransport:
    """基于 curl_cffi 的传输实现（浏览器 TLS 指纹，WAF 拦截时启用）。"""

    def __init__(self, impersonate: str = "chrome110", force_http2: bool = True) -> None:
        from curl_cffi.requests import AsyncSession

        self._session = AsyncSession(impersonate=impersonate)
        self._force_http2 = force_http2

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Response:
        from curl_cffi import CurlHttpVersion
        from curl_cffi.requests.exceptions import RequestException

        from ...errors import NetworkError, WafBlockedError

        check_headers(headers)

        kwargs: dict[str, Any] = {"headers": headers or {}, "timeout": timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        if self._force_http2:
            kwargs["http_version"] = CurlHttpVersion.V2_0
        try:
            r = await self._session.request(method, url, **kwargs)  # type: ignore[arg-type]
        except RequestException as exc:
            msg = str(exc).lower()
            if "reset" in msg or "(56)" in msg or "(35)" in msg:
                raise WafBlockedError(f"连接被重置（疑似 WAF）: {exc}") from exc
            raise NetworkError(f"传输失败: {exc}") from exc
        return Response(
            status=r.status_code,
            headers=tuple(r.headers.multi_items()),
            body=r.content,
        )

    async def aclose(self) -> None:
        await self._session.close()


def create_transport(cfg: Any) -> Transport:
    """按配置创建传输实现。

    Args:
        cfg: TransportConfig。

    Returns:
        传输实现。
    """
    if cfg.mode == "curl_cffi":
        return CurlCffiTransport(impersonate=cfg.impersonate, force_http2=cfg.force_http2)
    if cfg.mode == "curl":
        return CurlSubprocessTransport(cfg.curl_path)
    return HttpxTransport()
