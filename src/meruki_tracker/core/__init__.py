"""core：与界面无关的一切——领域模型、适配器、应用流程、配置。

这一层**不许 import ``meruki_tracker.tui``**：界面可以换（TUI / CLI / Web），
业务逻辑不该跟着重写。有一条测试（``tests/test_architecture.py``）盯着这条线。

同理，这一层也不该 import ``rich`` / ``prompt_toolkit``——它们只在 ``tui`` 里出现。
"""
