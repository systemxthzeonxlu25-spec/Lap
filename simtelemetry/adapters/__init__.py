"""适配器注册表 + 自动识别正在运行的游戏。"""
from __future__ import annotations

import importlib
import importlib.util
import logging
from typing import TYPE_CHECKING

from .base import AdapterDisconnected, GameAdapter
from .procs import running_process_names

log = logging.getLogger(__name__)

# 游戏 → (模块, 类名)。模块不存在（还没写）的游戏照样列在这里，is_implemented() 会告诉调用方。
_REGISTRY: dict[str, tuple[str, str]] = {
    "acc": (".acc", "ACCAdapter"),
    "lmu": (".lmu", "LMUAdapter"),
    "ac": (".ac", "ACAdapter"),
    "iracing": (".iracing", "IRacingAdapter"),
    "sim": (".simulated", "SimulatedAdapter"),
}
GAMES = tuple(_REGISTRY)

if TYPE_CHECKING:
    # 不会执行；只是让 PyInstaller 看到按名字动态导入的适配器模块，打包时一起带上。新增适配器时在这里补一个。
    from . import acc, lmu, simulated  # noqa: F401

# 游戏进程名（小写），按优先级排列。ACC 和初代 AC 用的是同名共享内存，只能靠进程区分。
# TODO(核对)：进程名来自社区资料，如果自动识别不到你的游戏，用 --game 手动指定，并告诉我实际 exe 名。
PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "acc": ("ac2-win64-shipping.exe",),
    "lmu": ("le mans ultimate.exe",),     # 来自 pyLMUSharedMemory 的进程名常量，未在真机核对
    "ac": ("acs.exe", "acs_x86.exe"),
    "iracing": ("iracingsim64dx11.exe", "iracingsim64.exe"),
}


def is_implemented(game: str) -> bool:
    """该游戏的适配器模块是否存在。

    直接试着导入，而不是用 importlib.util.find_spec：打包成 exe（PyInstaller）后 find_spec 找不到冻结的模块。
    只有“这个模块本身不存在”才算没实现；它依赖的包缺失（比如 iRacing 的 pyirsdk）算已实现，
    让 create_adapter 报出真正的错误。
    """
    entry = _REGISTRY.get(game)
    if entry is None:
        return False
    full = importlib.util.resolve_name(entry[0], __name__)
    try:
        importlib.import_module(full)
    except ModuleNotFoundError as e:
        return e.name != full
    except Exception:
        return True
    return True


def create_adapter(game: str, sim_speed: float = 1.0) -> GameAdapter:
    if game not in _REGISTRY:
        raise ValueError(f"未知游戏：{game}（可选 {', '.join(GAMES)}）")
    if not is_implemented(game):
        raise NotImplementedError(f"{game} 的适配器还没有实现")
    module, cls_name = _REGISTRY[game]
    cls = getattr(importlib.import_module(module, __name__), cls_name)
    return cls(speed=sim_speed) if game == "sim" else cls()


def detect_running_games() -> list[str]:
    """按进程名判断正在运行的游戏，按优先级返回。"""
    procs = running_process_names()
    return [g for g, names in PROCESS_NAMES.items() if any(n in procs for n in names)]


def game_process_running(game: str) -> bool | None:
    procs = running_process_names()
    if not procs:
        return None
    return any(n in procs for n in PROCESS_NAMES.get(game, ()))


__all__ = ["AdapterDisconnected", "GameAdapter", "GAMES", "PROCESS_NAMES", "create_adapter",
           "detect_running_games", "game_process_running", "is_implemented"]
