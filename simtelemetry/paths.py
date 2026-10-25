"""程序文件和数据放在哪：源码运行时都在项目根目录；打包成 exe（PyInstaller）后分开放。

- 资源（web/）：exe 运行时 PyInstaller 把它们解压到临时目录 sys._MEIPASS；
- 数据（data/telemetry.db）：放在 exe 旁边，不能放临时目录（每次运行都会清掉）。
"""
from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
_SOURCE_ROOT = Path(__file__).resolve().parent.parent

RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", _SOURCE_ROOT))
DATA_ROOT = Path(sys.executable).resolve().parent if FROZEN else _SOURCE_ROOT
