"""分层红线：core 不许依赖界面。

``src/meruki_tracker/core/__init__.py`` 里写着这条约束，这里把它变成可执行的检查。
为什么要测：分层本身不会自己维持——某天有人为了省事在 ``core/app/refresher.py``
里 ``from ..tui.screens import format_remaining``，代码照样跑得通、测试照样绿，
但 core 就再也拆不出去了。

检查用 AST 而不是字符串匹配：注释和 docstring 里提到 ``meruki_tracker.tui``
是正常的（说明文字），只有真正 ``import`` 才算违规。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "src" / "meruki_tracker" / "core"
UI_LIBS = ("rich", "prompt_toolkit")


def _core_files() -> list[Path]:
    return sorted(CORE.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> list[str]:
    """收集一个模块里所有被 import 的模块名（含 ``from X import y`` 的 X）。"""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        # 相对 import 的 module 可能为 None（``from . import x``）
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append("." * node.level + node.module)
    return out


def _resolves_to_tui(name: str) -> bool:
    """这个 import 名字是不是指向界面层。

    相对 import 在本文件里写作 ``..tui.xxx``（core 内的模块离包根两层），
    绝对 import 写作 ``meruki_tracker.tui.xxx``——两种都要认。
    """
    if name.startswith("..tui") or name.startswith(".tui"):
        return True
    return name == "meruki_tracker.tui" or name.startswith("meruki_tracker.tui.")


@pytest.mark.parametrize("path", _core_files(), ids=lambda p: p.name)
def test_core_does_not_import_the_ui_layer(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [n for n in _imported_modules(tree) if _resolves_to_tui(n)]
    assert not offenders, f"{path} 依赖了界面层：{offenders}"


@pytest.mark.parametrize("path", _core_files(), ids=lambda p: p.name)
def test_core_does_not_import_ui_libraries(path: Path):
    """rich / prompt_toolkit 只该出现在 tui 里。

    这不是洁癖：core 要是自己渲染，就意味着"换界面"得改业务代码，
    而且这两个库一旦进 core，非交互场景（定时采集、脚本）也得拖上它们。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [n for n in _imported_modules(tree) if n.split(".")[0] in UI_LIBS]
    assert not offenders, f"{path} 引入了界面库：{offenders}"


def test_the_two_layers_both_exist():
    """分层真的落地了——不是只改了目录名却把文件留在原地。"""
    assert CORE.is_dir(), "core 层不存在"
    assert (CORE / "domain" / "models.py").is_file()
    assert (CORE / "app" / "context.py").is_file()
    tui = CORE.parent / "tui"
    assert (tui / "tui.py").is_file()
    # screens 按渲染函数族拆成了包（列表/详情/登录/设置/队列侧栏/公共小工具）
    assert (tui / "screens" / "__init__.py").is_file()
    assert (tui / "views.py").is_file()
