"""测试共用：用合成车手跑一段时间，返回切好的圈。"""
from __future__ import annotations

from simtelemetry.adapters.simulated import SyntheticDriver
from simtelemetry.models import LapRecord, SessionInfo
from simtelemetry.recorder import LapRecorder

SESSION = SessionInfo(game="sim", track="Synthetic Ring", car="Demo GT3", track_length_m=4200.0)
DT = 0.02


def drive(seconds: float, seed: int = 3) -> list[LapRecord]:
    drv = SyntheticDriver(seed=seed, laps_between_stops=4)
    rec = LapRecorder()
    laps: list[LapRecord] = []
    wall = 0.0
    for tick in range(int(seconds / DT)):
        f = drv.step(DT).to_frame(tick)
        wall += DT
        laps += rec.feed(SESSION, f, wall)
    laps += rec.flush(wall + 10)
    return laps
