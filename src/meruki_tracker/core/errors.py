"""异常体系：按失败域分类（设计文档 §6）。

每个域有独立的用户提示与后续动作，绝不共用重试/冷却机制。
"""

from __future__ import annotations


class MerukiTrackerError(Exception):
    """所有领域异常的根。"""


#: 历史名称（项目曾叫 ``mam``）。保留为别名，避免外部调用方按旧名捕获时失效。
MamError = MerukiTrackerError


class NetworkError(MerukiTrackerError):
    """网络层失败：连接失败 / 超时 / DNS。可重试，不冷却。"""


class WafBlockedError(MerukiTrackerError):
    """疑似被 WAF 拦截：连接被重置或非常规拒绝。指数退避，不判凭证失效。"""


class AuthExpiredError(MerukiTrackerError):
    """凭证失效：401 / 返回登录页 / "请登录"。停止自动请求，提示重新登录。"""


class ProtocolChangedError(MerukiTrackerError):
    """接口结构不符合预期：站点可能改版。停止自动重试，提示导出探测。"""


class ConfigError(MerukiTrackerError):
    """配置缺失或非法。"""


__all__ = [
    "AuthExpiredError",
    "ConfigError",
    "MamError",
    "MerukiTrackerError",
    "NetworkError",
    "ProtocolChangedError",
    "WafBlockedError",
]
