"""命令行入口。

保持 ``breeze.cli:app`` 作为 pyproject 的脚本入口；具体实现位于
``breeze.cli.main``。
"""

from breeze.cli.main import app

__all__ = ["app"]
