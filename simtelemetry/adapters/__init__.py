"""适配器注册表 + 自动识别正在运行的游戏。"""
from __future__ import annotations

import logging

from .base import AdapterDisconnected, GameAdapter
from .procs import running_process_names

log = logging.getLogger(__name__)

GAMES = ("acc", "ac", "iracing", "sim")

# 游戏进程名（小写）。ACC 和初代 AC 用的是同名共享内存，只能靠进程区分。
# ACC、iRacing（DX11 版）、acs_x86.exe 有社区资料 / 官方支持页佐证；acs.exe（64 位 AC）和 iracingsim64.exe 还未核对。
# 自动识别不到时用 --game 手动指定，并核对游戏目录里实际的 exe 名。
PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "acc": ("ac2-win64-shipping.exe",),
    "ac": ("acs.exe", "acs_x86.exe"),
    "iracing": ("iracingsim64dx11.exe", "iracingsim64.exe"),
}


def create_adapter(game: str, sim_speed: float = 1.0, check_process: bool = True) -> GameAdapter:
    """check_process=False：不按进程名确认游戏在跑（配合 tools/fake_acc.py 之类的假数据源）。"""
    if game == "acc":
        from .acc import ACCAdapter
        return ACCAdapter(check_process=check_process)
    if game == "ac":
        from .ac import ACAdapter
        return ACAdapter(check_process=check_process)
    if game == "iracing":
        from .iracing import IRacingAdapter
        return IRacingAdapter(check_process=check_process)
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
