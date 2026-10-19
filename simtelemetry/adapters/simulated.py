"""合成数据：不开游戏也能试用整套面板（--game sim），同时给 tools/fake_acc.py 提供“车手”。

赛道是一条 4.2 km 的虚构赛道；每圈对弯心速度、刹车点加一点随机扰动，圈时会有 ±1 s 左右的差异，
偶尔压线导致本圈无效；每隔几圈进一次站（产生进站圈和出站圈）。
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from ..models import SessionInfo, TelemetryFrame
from .base import GameAdapter

TRACK_LENGTH_M = 4200.0
V_TOP = 275 / 3.6                 # m/s
A_BRAKE = 14.0                    # m/s²
PIT_SPEED = 60 / 3.6
PIT_ENTRY_POS = 0.93              # 进站：从这里开始进维修区车道
PIT_EXIT_POS = 0.06               # 出站：到这里离开维修区车道
PIT_BOX_POS = 0.015
PIT_STOP_S = 4.0

# (位置, 弯心速度 km/h, 方向 +1 左 / -1 右, 弯道长度 m)
CORNERS = [
    (0.08, 95, +1, 120), (0.17, 160, -1, 150), (0.26, 70, +1, 90), (0.36, 210, -1, 200),
    (0.45, 120, +1, 130), (0.55, 85, -1, 100), (0.66, 180, +1, 180), (0.76, 110, -1, 120),
    (0.86, 140, +1, 150),
]
GEAR_TOP_KMH = [0, 80, 115, 150, 185, 220, 255, 300]   # 1..7 挡的换挡速度


def _accel(v: float) -> float:
    return max(1.2, 9.5 * (1 - v / V_TOP))


@dataclass
class DriverState:
    pos: float
    speed_kmh: float
    throttle: float
    brake: float
    steer: float
    gear: int
    rpm: int
    lap_count: int
    lap_time_ms: int
    last_lap_ms: int
    best_lap_ms: int
    in_pit: bool
    in_pit_lane: bool
    lap_valid: bool


class SyntheticDriver:
    def __init__(self, seed: int = 7, laps_between_stops: int = 6):
        self.rng = random.Random(seed)
        self.laps_between_stops = laps_between_stops
        self.n = int(TRACK_LENGTH_M)
        self.dist = PIT_BOX_POS * TRACK_LENGTH_M
        self.lap_count = 0
        self.lap_time = 0.0
        self.last_lap_ms = 0
        self.best_lap_ms = 0
        self.mode = "stopped"             # stopped → pit_out → flying → pit_in → stopped ...
        self.stop_left = 2.0
        self.flying_laps = 0
        self.valid = True
        self.cut_at: float | None = None
        self._new_lap_plan()

    # ---------------------------------------------------------------- profile
    def _new_lap_plan(self) -> None:
        rng = self.rng
        skill = rng.gauss(1.0, 0.012)
        lim = [V_TOP] * self.n
        self.steer_profile = [0.0] * self.n
        for pos, apex_kmh, direction, length in CORNERS:
            apex = apex_kmh / 3.6 * skill * rng.gauss(1.0, 0.015)
            c = int(pos * self.n)
            half = int(length / 2)
            amp = min(0.75, max(0.12, 55.0 / apex_kmh))
            for k in range(-half, half + 1):
                i = (c + k) % self.n
                shape = math.cos(k / half * math.pi / 2)
                lim[i] = min(lim[i], apex / max(0.35, shape) ** 0.15 if shape > 0 else V_TOP)
                self.steer_profile[i] = direction * amp * shape
        v = lim[:]
        brake_scale = rng.gauss(1.0, 0.04)
        for _ in range(2):   # 两遍处理首尾相接
            for i in range(1, self.n):
                v[i] = min(v[i], math.sqrt(v[i - 1] ** 2 + 2 * _accel(v[i - 1])))
            v[0] = min(v[0], math.sqrt(v[-1] ** 2 + 2 * _accel(v[-1])))
            for i in range(self.n - 2, -1, -1):
                v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * A_BRAKE * brake_scale))
            v[-1] = min(v[-1], math.sqrt(v[0] ** 2 + 2 * A_BRAKE * brake_scale))
        self.v = v
        self.cut_at = None
        if self.mode == "flying" and rng.random() < 0.18:
            self.cut_at = rng.choice(CORNERS)[0]

    # ------------------------------------------------------------------- step
    def step(self, dt: float) -> DriverState:
        dt = max(0.0, min(dt, 0.2))
        pos = self.dist / TRACK_LENGTH_M
        i = int(self.dist) % self.n
        in_lane = self.mode in ("pit_out", "pit_in", "stopped")

        if self.mode == "stopped":
            speed = 0.0
            self.stop_left -= dt
            if self.stop_left <= 0:
                self.mode = "pit_out"
        elif in_lane:
            speed = min(PIT_SPEED, self.v[i])
        else:
            speed = self.v[i] * (1 + self.rng.gauss(0, 0.002))

        # 状态迁移
        if self.mode == "pit_out" and PIT_EXIT_POS <= pos < 0.5:
            self.mode = "flying"
        elif self.mode == "flying" and pos >= PIT_ENTRY_POS and self.flying_laps >= self.laps_between_stops:
            self.mode = "pit_in"

        prev_lap_time = self.lap_time
        self.lap_time += dt
        self.dist += speed * dt
        if self.mode == "pit_in" and PIT_BOX_POS <= (self.dist / TRACK_LENGTH_M) % 1.0 < 0.5:
            # 已经跨过起终点线、到达维修位：停车
            self.dist = (1.0 if self.dist >= TRACK_LENGTH_M else 0.0) * TRACK_LENGTH_M \
                + PIT_BOX_POS * TRACK_LENGTH_M
            self.mode, self.stop_left, self.flying_laps = "stopped", PIT_STOP_S, 0

        if self.dist >= TRACK_LENGTH_M:                     # 跨线
            over = (self.dist - TRACK_LENGTH_M) / max(speed, 1e-3)
            lap_ms = int(round((self.lap_time - over) * 1000))
            self.dist -= TRACK_LENGTH_M
            self.lap_count += 1
            self.last_lap_ms = lap_ms
            if self.valid and (self.best_lap_ms == 0 or lap_ms < self.best_lap_ms):
                self.best_lap_ms = lap_ms
            self.lap_time = over
            if self.mode == "flying":
                self.flying_laps += 1
            self.valid = True
            self._new_lap_plan()
        elif self.cut_at is not None and prev_lap_time > 0 and abs(pos - self.cut_at) < 0.005:
            self.valid = False

        # 驾驶输入：由速度曲线的斜率反推
        j = int(self.dist) % self.n
        nxt = self.v[(j + 15) % self.n]
        cur = self.v[j]
        throttle, brake = 0.0, 0.0
        if self.mode == "stopped":
            pass
        elif in_lane:
            throttle = 0.35
        elif nxt < cur - 0.3:
            brake = min(1.0, (cur - nxt) / 6.0 + 0.25)
        elif nxt > cur + 0.2 or cur >= V_TOP * 0.995:
            throttle = 1.0
        else:
            throttle = 0.45
        kmh = speed * 3.6
        gear = 1
        while gear < 7 and kmh > GEAR_TOP_KMH[gear]:
            gear += 1
        lo, hi = GEAR_TOP_KMH[gear - 1], GEAR_TOP_KMH[gear]
        rpm = int(3500 + (kmh - lo) / max(1, hi - lo) * 4800) if speed > 0 else 1100
        steer = self.steer_profile[j] + self.rng.gauss(0, 0.004)

        return DriverState(
            pos=self.dist / TRACK_LENGTH_M, speed_kmh=kmh, throttle=throttle, brake=brake,
            steer=max(-1.0, min(1.0, steer)), gear=0 if speed == 0 else gear, rpm=rpm,
            lap_count=self.lap_count, lap_time_ms=int(self.lap_time * 1000),
            last_lap_ms=self.last_lap_ms, best_lap_ms=self.best_lap_ms,
            in_pit=self.mode == "stopped", in_pit_lane=in_lane, lap_valid=self.valid,
        )


class SimulatedAdapter(GameAdapter):
    name = "sim"
    display_name = "模拟数据"

    def __init__(self, speed: float = 1.0, seed: int = 7):
        self.speed = speed
        self.seed = seed
        self._driver: SyntheticDriver | None = None
        self._last = 0.0
        self._tick = 0
        self.session = SessionInfo(game="sim", track="Synthetic Ring", car="Demo GT3",
                                   track_length_m=TRACK_LENGTH_M, session_type="practice")

    def connect(self) -> bool:
        if self._driver is None:
            self._driver = SyntheticDriver(seed=self.seed)
            self._last = time.monotonic()
        return True

    def read(self):
        assert self._driver is not None
        now = time.monotonic()
        dt, self._last = (now - self._last) * self.speed, now
        s = self._driver.step(dt)
        self._tick += 1
        frame = TelemetryFrame(
            speed_kmh=s.speed_kmh, throttle=s.throttle, brake=s.brake, steer=s.steer, gear=s.gear,
            rpm=s.rpm, lap_count=s.lap_count, lap_time_ms=s.lap_time_ms, pos=s.pos,
            in_pit=s.in_pit or s.in_pit_lane, lap_valid=s.lap_valid, last_lap_ms=s.last_lap_ms,
            is_live=True, tick=self._tick,
        )
        return self.session, frame

    def close(self) -> None:
        self._driver = None
