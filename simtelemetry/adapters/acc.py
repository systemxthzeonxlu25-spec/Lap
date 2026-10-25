"""Assetto Corsa Competizione：读官方共享内存（physics / graphics / static 三个页面）。

分两层：
- ACCAdapter：打开 / 读取 / 关闭共享内存页（只能在 Windows 上真正连上）；
- translate()：纯函数，把三个结构体换成统一的 SessionInfo + TelemetryFrame，在任何平台都能测。

字段映射（依据见 acc_structs.py 和 PROGRESS.md）：
- 挡位：ACC 0 = R、1 = N、2 = 1 挡（社区实测，文档没写）→ 统一编码 -1 / 0 / 1..
- 上一圈 iLastTime：没有时是 INT32_MAX 或 ≤ 0 → None
- 维修区：isInPit（维修位）或 isInPitLane（维修区车道）
- 只有 status == ACC_LIVE 才算在跑（暂停 / 回放不记录）；ACC_OFF = 还在菜单，没有数据
- tick 用 physics 的 packetId（暂停时不变，采集循环据此判断“没有新数据”）
"""
from __future__ import annotations

from typing import Protocol

from ..models import SessionInfo, TelemetryFrame
from . import game_process_running
from .acc_structs import (ACC_LIVE, ACC_OFF, ACC_SESSION_TYPE, GRAPHICS_MAP, PHYSICS_MAP, STATIC_MAP,
                          SPageFileGraphic, SPageFilePhysics, SPageFileStatic, wchar_str)
from .base import GameAdapter
from .winshm import SharedMemoryPage, read_consistent

INT32_MAX = 2**31 - 1


class Page(Protocol):
    def open(self) -> bool: ...
    def read(self): ...
    def close(self) -> None: ...


def translate(phys: SPageFilePhysics, gfx: SPageFileGraphic, stat: SPageFileStatic
              ) -> tuple[SessionInfo | None, TelemetryFrame] | None:
    if gfx.status == ACC_OFF:
        return None
    track, car = wchar_str(stat.track), wchar_str(stat.carModel)
    session = None
    if track and car:
        length = float(stat.trackSPlineLength)
        session = SessionInfo(game="acc", track=track, car=car,
                              track_length_m=length if length > 0 else None,   # ACC 实测为 0
                              session_type=ACC_SESSION_TYPE.get(gfx.session, "unknown"))
    last = int(gfx.iLastTime)
    frame = TelemetryFrame(
        speed_kmh=max(0.0, float(phys.speedKmh)),
        throttle=_unit(phys.gas),
        brake=_unit(phys.brake),
        steer=max(-1.0, min(1.0, float(phys.steerAngle))),
        gear=int(phys.gear) - 1,
        rpm=max(0, int(phys.rpms)),
        lap_count=int(gfx.completedLaps),
        lap_time_ms=max(0, int(gfx.iCurrentTime)),
        pos=_unit(gfx.normalizedCarPosition),
        in_pit=bool(gfx.isInPit or gfx.isInPitLane),
        lap_valid=bool(gfx.isValidLap),
        last_lap_ms=last if 0 < last < INT32_MAX else None,
        is_live=gfx.status == ACC_LIVE,
        tick=int(phys.packetId),
    )
    return session, frame


def _unit(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


class ACCAdapter(GameAdapter):
    name = "acc"
    display_name = "Assetto Corsa Competizione"

    def __init__(self, physics: Page | None = None, graphics: Page | None = None, static: Page | None = None):
        # 测试时可以传入假的页面对象；默认读真正的共享内存
        self._physics = physics or SharedMemoryPage(PHYSICS_MAP, SPageFilePhysics)
        self._graphics = graphics or SharedMemoryPage(GRAPHICS_MAP, SPageFileGraphic)
        self._static = static or SharedMemoryPage(STATIC_MAP, SPageFileStatic)

    def _pages(self) -> tuple[Page, Page, Page]:
        return self._physics, self._graphics, self._static

    def wrap_pages(self, wrap) -> bool:
        self._physics, self._graphics, self._static = (
            wrap(n, pg) for n, pg in zip(("physics", "graphics", "static"), self._pages()))
        return True

    def connect(self) -> bool:
        if all(p.open() for p in self._pages()):
            return True
        self.close()                     # 只打开了一部分：全部关掉，下次重试
        return False

    def read(self):
        phys = read_consistent(self._physics)
        gfx = read_consistent(self._graphics)
        return translate(phys, gfx, self._static.read())

    def close(self) -> None:
        for p in self._pages():
            p.close()

    def is_running(self) -> bool | None:
        return game_process_running("acc")
