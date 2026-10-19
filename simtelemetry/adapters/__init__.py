"""适配器注册表 + 自动识别正在运行的游戏。"""
from __future__ import annotations

import logging

from .base import AdapterDisconnected, GameAdapter
from .procs import running_process_names

log = logging.getLogger(__name__)

GAMES = ("acc", "ac", "iracing", "sim")

# 游戏进程名（小写）。ACC 和初代 AC 用的是同名共享内存，只能靠进程区分。
# TODO(核对)：进程名来自社区资料，如果自动识别不到你的游戏，用 --game 手动指定，并告诉我实际 exe 名。
PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "acc": ("ac2-win64-shipping.exe",),
    "ac": ("acs.exe", "acs_x86.exe"),
    "iracing": ("iracingsim64dx11.exe", "iracingsim64.exe"),
}


def create_adapter(game: str, sim_speed: float = 1.0) -> GameAdapter:
    if game == "acc":
        from .acc import ACCAdapter
        return ACCAdapter()
    if game == "ac":
        from .ac import ACAdapter
        return ACAdapter()
    if game == "iracing":
        from .iracing import IRacingAdapter
        return IRacingAdapter()
    if game == "sim":
        from .simulated import SimulatedAdapter
        return SimulatedAdapter(speed=sim_speed)
    raise ValueError(f"未知游戏：{game}（可选 {', '.join(GAMES)}）")


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
           "detect_running_games", "game_process_running"]
