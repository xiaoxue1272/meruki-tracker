"""PyInstaller 入口：用绝对 import 载入 CLI。

不能直接用 ``src/meruki_tracker/__main__.py``——它用的是相对 import
(``from . import __version__``)，被 PyInstaller 当顶层脚本跑时会失败。
这里从包里取 main，行为与 ``mt`` 控制台脚本一致。
"""

import sys

from meruki_tracker.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
