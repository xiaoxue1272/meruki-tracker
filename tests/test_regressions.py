"""历史 bug 的回归测试（刷新落盘、鉴权判定、重试、异常命名）。

文件名里的 ``selfcheck`` 是历史遗留：这些用例与已被移除的「自检」功能无关，
测的是 refresher / api / errors 的真实缺陷，故改名保留。

覆盖四个曾经真实存在的缺陷：

1. 刷新失败时"落盘失败态快照"本身抛 OSError，会顶替掉用户真正关心的错误原因。
2. 业务 msg 的凭证失效判定大小写敏感，``Unauthorized`` 漏判。
3. ``refresher`` 与 ``api`` 双层重试，最坏放大成 retry×retry 次请求。
4. 异常根类旧名 ``MamError`` 与包名 ``meruki_tracker`` 不一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meruki_tracker.core.adapters.meruki.api import _looks_like_auth_msg, _parse_body
from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.observations import ObservationStore
from meruki_tracker.core.adapters.store.snapshot import Snapshot, SnapshotStore
from meruki_tracker.core.app.refresher import Refresher, RefresherOptions
from meruki_tracker.core.errors import (
    AuthExpiredError,
    MamError,
    MerukiTrackerError,
    NetworkError,
    ProtocolChangedError,
)

# ------------------------------------------------------------------ 1. 落盘失败不能顶替原始错误


def _creds(tmp_path: Path) -> CredentialStore:
    store = CredentialStore(tmp_path / "cookies")
    store.save("sid=test")
    return store


class _ExplodingSnapshotStore:
    """write() 必定抛 OSError，模拟磁盘满 / 只读目录。"""

    def __init__(self) -> None:
        self.calls = 0

    def write(self, snap: Snapshot) -> None:
        self.calls += 1
        raise OSError(28, "No space left on device")


class _FailingApi:
    """所有请求都抛指定异常。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def fetch_list_page(self, page: int, *, status: str = "进行中"):
        raise self._exc

    async def aclose(self) -> None:
        return None


def _build_refresher(tmp_path: Path, api, snapshots) -> Refresher:
    creds = CredentialStore(tmp_path / "cookies")
    creds.save("sid=test")  # 无 Cookie 时 refresh 会提前返回，不会发请求
    return Refresher(
        api=api,
        snapshots=snapshots,
        credentials=creds,
        observations=ObservationStore(tmp_path / "obs.sqlite"),
        options=RefresherOptions(detail_enabled=False),
    )


async def test_write_failure_does_not_mask_original_error(tmp_path: Path):
    """磁盘满时仍应上报"网络不通"，而不是 OSError。"""
    store = _ExplodingSnapshotStore()
    refresher = _build_refresher(tmp_path, _FailingApi(NetworkError("网络不通")), store)

    _snap, result = await refresher.refresh(Snapshot())

    assert result.ok is False
    assert result.error_kind == "network"
    assert "网络不通" in result.error
    assert "space" not in result.error.lower()
    # 确实尝试过落盘
    assert store.calls == 1


async def test_write_failure_does_not_mask_internal_error(tmp_path: Path):
    """未预期异常路径同样不能被写盘失败顶替。"""
    store = _ExplodingSnapshotStore()
    refresher = _build_refresher(tmp_path, _FailingApi(RuntimeError("boom")), store)

    _snap, result = await refresher.refresh(Snapshot())

    assert result.ok is False
    assert result.error_kind == "internal"
    assert "boom" in result.error


async def test_write_failure_keeps_previous_snapshot(tmp_path: Path):
    """写盘失败时返回的快照应是失败态，且不影响调用方。"""
    store = _ExplodingSnapshotStore()
    refresher = _build_refresher(tmp_path, _FailingApi(NetworkError("x")), store)

    snap, result = await refresher.refresh(Snapshot())

    assert result.ok is False
    assert snap.last_attempt.ok is False


async def test_normal_write_still_used_on_failure(tmp_path: Path):
    """正常的 store 在失败时仍然写入失败态（确认修复没有把正常路径一起关掉）。"""
    real = SnapshotStore(tmp_path / "meruki.db")
    refresher = _build_refresher(tmp_path, _FailingApi(NetworkError("网络不通")), real)

    await refresher.refresh(Snapshot())

    written = real.read()
    assert written is not None
    assert written.last_attempt.ok is False
    assert "网络不通" in written.last_attempt.error


# ------------------------------------------------------------------ 2. 凭证失效判定大小写


@pytest.mark.parametrize(
    "msg",
    ["Unauthorized", "UNAUTHORIZED", "unauthorized", "UnAuThOrIzEd"],
)
def test_auth_msg_is_case_insensitive(msg):
    assert _looks_like_auth_msg(msg)


@pytest.mark.parametrize("msg", ["请登录", "未登录", "登录已失效", "会话已过期"])
def test_auth_msg_chinese_still_detected(msg):
    assert _looks_like_auth_msg(msg)


@pytest.mark.parametrize("msg", ["", "系统繁忙", "参数错误", "ok"])
def test_auth_msg_rejects_unrelated(msg):
    assert not _looks_like_auth_msg(msg)


class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text
        self.headers: tuple = ()


def test_parse_body_uppercase_unauthorized_is_auth_error():
    """端到端：服务端返回大写 Unauthorized 也要判成凭证失效。"""
    with pytest.raises(AuthExpiredError):
        _parse_body(_Resp(200, '{"code":500,"msg":"Unauthorized"}'))


def test_parse_body_mixed_case_unauthorized_is_auth_error():
    with pytest.raises(AuthExpiredError):
        _parse_body(_Resp(200, '{"code":999,"msg":"Session UnAuthorized"}'))


# ------------------------------------------------------------------ 3. 重试只在一层


async def test_refresher_does_not_duplicate_retries(tmp_path: Path):
    """refresher 不再自己重试：一次刷新只应产生一次列表请求。

    真正的重试次数由 API 层按 fetch.retry 决定。这里用 retry=5 显式验证
    refresher 层不会把它放大。
    """
    calls = {"n": 0}

    class CountingApi(_FailingApi):
        async def fetch_list_page(self, page: int, *, status: str = "进行中"):
            calls["n"] += 1
            raise NetworkError("boom")

    refresher = Refresher(
        api=CountingApi(NetworkError("boom")),
        snapshots=SnapshotStore(tmp_path / "meruki.db"),
        credentials=_creds(tmp_path),
        observations=ObservationStore(tmp_path / "obs.sqlite"),
        options=RefresherOptions(detail_enabled=False),
    )

    await refresher.refresh(Snapshot())

    assert calls["n"] == 1, f"refresher 不应重试，实际请求 {calls['n']} 次"


async def test_auth_error_is_not_retried_by_refresher(tmp_path: Path):
    calls = {"n": 0}

    class AuthApi:
        async def fetch_list_page(self, page: int, *, status: str = "进行中"):
            calls["n"] += 1
            raise AuthExpiredError("请登录")

    refresher = Refresher(
        api=AuthApi(),
        snapshots=SnapshotStore(tmp_path / "meruki.db"),
        credentials=_creds(tmp_path),
        observations=ObservationStore(tmp_path / "obs.sqlite"),
        options=RefresherOptions(detail_enabled=False),
    )

    _, result = await refresher.refresh(Snapshot())
    assert calls["n"] == 1
    assert result.error_kind == "auth"


# ------------------------------------------------------------------ 4. 异常根类命名


def test_error_root_has_new_name():
    assert issubclass(NetworkError, MerukiTrackerError)
    assert issubclass(AuthExpiredError, MerukiTrackerError)
    assert issubclass(ProtocolChangedError, MerukiTrackerError)


def test_legacy_alias_still_works():
    """旧名保留为别名，外部按旧名捕获仍然有效。"""
    assert MamError is MerukiTrackerError
    with pytest.raises(MamError):
        raise NetworkError("x")


# ------------------------------------------------------------------ 5. 本机地址不走代理


def test_loopback_hosts_are_recognised():
    """``localhost`` / ``127.0.0.0/8`` / ``::1`` 都算本机。"""
    from meruki_tracker.core.net import is_loopback_host

    for host in ("127.0.0.1", "127.1.2.3", "localhost", "LOCALHOST", "::1", "[::1]"):
        assert is_loopback_host(host), host
    for host in ("q.qq.com", "192.168.0.5", "10.0.0.1", "", "example.com"):
        assert not is_loopback_host(host), host


def test_only_loopback_urls_bypass_the_proxy():
    """外网地址必须照常走代理：把代理一起关掉是另一种错。"""
    from meruki_tracker.core.net import bypasses_proxy

    assert bypasses_proxy("http://127.0.0.1:1/x")
    assert bypasses_proxy("http://localhost:3000/")
    assert not bypasses_proxy("https://q.qq.com/qqbot/openclaw/connect.html")
    assert not bypasses_proxy("https://api.bot.qq.com/app/getAppAccessToken")


def test_loopback_request_never_goes_through_a_system_proxy(monkeypatch):
    """真机踩过：macOS 系统代理的排除列表不含 127.0.0.1，把本机服务请求送去

    代理，代理再连自己的回环 → 拿到 503 而不是「连不上」，报错完全指不到病根。
    这里把 ``urlopen`` 换成"一旦被调用就失败"，逼架构走直连那条路。
    """
    import urllib.request as urlrequest

    from meruki_tracker.core.net import open_url

    def _boom(*args, **kwargs):
        raise AssertionError("本机地址不该经过 urllib.request.urlopen（它会用系统代理）")

    monkeypatch.setattr(urlrequest, "urlopen", _boom)
    req = urlrequest.Request("http://127.0.0.1:1/x", data=b"{}", method="POST")
    with pytest.raises(Exception) as caught:
        open_url(req, timeout=1.0)
    text = str(caught.value)
    # 两条都要守：不能走 urlopen（否则 _boom 触发），也不能被代理拦出 503
    assert "本机地址不该经过" not in text, f"还是走了 urlopen: {text}"
    assert "503" not in text, text


def test_qqbot_unreachable_host_reports_a_connection_error():
    """同一条链路从业务侧看：连不上就是「连不上」，不是 HTTP 503。"""
    from meruki_tracker.core.app import qqbot

    with pytest.raises(qqbot.QqBindError, match="连不上"):
        qqbot.create_bind_task(host="http://127.0.0.1:1", timeout=2.0)
