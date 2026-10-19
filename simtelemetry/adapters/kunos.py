"""Kunos 系游戏（ACC / 初代 AC）共用的共享内存适配器骨架。

两款游戏用的是**同名**映射（Local\\acpmf_physics / graphics / static），结构体却不一样，
所以连接前先按进程名确认是哪个游戏在跑；映射本身由 winshm 只读打开、绝不创建。
子类只需要给出三个结构体类型，并实现 convert()：原始结构体 → (SessionInfo | None, TelemetryFrame)。
"""
from __future__ import annotations

import ctypes
from typing import Callable

from ..models import SessionInfo, TelemetryFrame
from .acc_structs import GRAPHICS_MAP, PHYSICS_MAP, STATIC_MAP
from .base import GameAdapter
from .winshm import SharedMemoryPage, read_consistent

INT32_MAX = 2**31 - 1

PageFactory = Callable[[str, type], SharedMemoryPage]


def clamp(x: float, lo: float, hi: float) -> float:
    x = float(x)
    if x != x:                      # NaN
        x = 0.0
    return lo if x < lo else hi if x > hi else x


def lap_ms_or_none(ms: int) -> int | None:
    """iLastTime 之类：还没有圈时的时候是 INT32_MAX（ACC 实测）或 0。"""
    return None if ms <= 0 or ms >= INT32_MAX else int(ms)


class KunosAdapter(GameAdapter):
    physics_type: type[ctypes.Structure]
    graphics_type: type[ctypes.Structure]
    static_type: type[ctypes.Structure]

    def __init__(self, check_process: bool = True, page_factory: PageFactory = SharedMemoryPage):
        self.check_process = check_process
        self._physics = page_factory(PHYSICS_MAP, self.physics_type)
        self._graphics = page_factory(GRAPHICS_MAP, self.graphics_type)
        self._static = page_factory(STATIC_MAP, self.static_type)

    def _pages(self):
        return (self._physics, self._graphics, self._static)

    def connect(self) -> bool:
        # 映射在所有句柄关闭前不会消失（比如 SimHub 还开着），而且 ACC/AC 同名：先看进程
        if self.is_running() is False:
            return False
        if all(p.open() for p in self._pages()):
            return True
        self.close()
        return False

    def read(self) -> tuple[SessionInfo | None, TelemetryFrame] | None:
        phys = read_consistent(self._physics)
        gfx = read_consistent(self._graphics)
        stat = self._static.read()          # 只在加载会话时变化；820 字节，每帧拷贝一次也不贵
        return self.convert(phys, gfx, stat)

    def convert(self, phys, gfx, stat) -> tuple[SessionInfo | None, TelemetryFrame]:
        raise NotImplementedError

    def close(self) -> None:
        for p in self._pages():
            p.close()

    def is_running(self) -> bool | None:
        if not self.check_process:
            return None
        from . import game_process_running
        return game_process_running(self.name)
