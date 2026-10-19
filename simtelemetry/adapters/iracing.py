"""iRacing（通过 pyirsdk 读官方 SDK 的内存映射）。

变量含义来自 iRacing SDK 自带的变量说明（goirsdk 仓库 variables.md 里的完整转储）：
- Speed m/s（×3.6）、Throttle / Brake 0..1、Gear -1 = R / 0 = N / 1..、RPM
- steer ← SteeringWheelAngle / SteeringWheelAngleMax（两者都是弧度）→ -1..1；
  iRacing 的方向盘角左转为正（待实车确认），与本项目约定（+ = 左）一致
- lap_count ← LapCompleted（“Laps completed count”）；lap_time_ms ← LapCurrentLapTime（F3 计时框里的估算值）
- pos ← LapDistPct；last_lap_ms ← LapLastLapTime（社区反馈过线后会晚一会儿才更新，录制器最多等 3 s）
- in_pit ← OnPitRoad or PlayerCarInPitStall
- lap_valid ← PlayerTrackSurface != OffTrack（四轮出界）。iRacing 没有“本圈有效”变量，这是近似
- is_live ← IsOnTrack and not IsReplayPlaying；tick ← SessionTick
- 赛道 / 车型 / 赛道长度 / 会话类型来自会话信息 YAML（WeekendInfo / DriverInfo / SessionInfo）

pyirsdk 的两个坑：
- startup() 用 mmap.mmap(0, size, tagname) 打开映射——在 Windows 上这会在映射不存在时**创建**它
  （winshm.py 文件头解释了为什么这很糟），所以先用 OpenFileMapping 确认映射存在再调 startup()；
- 连不上时它会直接 print 到 stdout，这里吞掉。
"""
from __future__ import annotations

import contextlib
import io
import re
from typing import Any, Callable

from ..models import SessionInfo, TelemetryFrame
from .base import AdapterDisconnected, GameAdapter
from .kunos import clamp
from .winshm import mapping_exists

MEMMAP_NAME = "Local\\IRSDKMemMapFileName"
TRK_OFF_TRACK = 0                  # irsdk_TrkLoc：-1 不在场上 / 0 出界 / 1 维修位 / 2 接近维修区 / 3 赛道上

VARS = (
    "Speed", "Throttle", "Brake", "SteeringWheelAngle", "SteeringWheelAngleMax", "Gear", "RPM",
    "LapCompleted", "LapCurrentLapTime", "LapDistPct", "LapLastLapTime", "OnPitRoad",
    "PlayerCarInPitStall", "PlayerTrackSurface", "IsOnTrack", "IsReplayPlaying", "SessionTick",
    "SessionNum",
)


def parse_track_length_m(text: Any) -> float | None:
    """WeekendInfo.TrackLength 形如 “6.93 km”（也见过 “4.30 mi”）。"""
    if not isinstance(text, str):
        return None
    m = re.match(r"\s*([\d.]+)\s*(km|mi)?", text)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v * (1609.344 if m.group(2) == "mi" else 1000.0) if v > 0 else None


def session_from_yaml(weekend: dict | None, drivers: dict | None, sessions: dict | None,
                      session_num: int | None) -> SessionInfo | None:
    if not weekend or not drivers:
        return None
    track = str(weekend.get("TrackDisplayName") or weekend.get("TrackName") or "").strip()
    layout = str(weekend.get("TrackConfigName") or "").strip()
    if track and layout:
        track = f"{track} [{layout}]"
    car = ""
    me = drivers.get("DriverCarIdx")
    for d in drivers.get("Drivers") or ():
        if d.get("CarIdx") == me:
            car = str(d.get("CarScreenName") or d.get("CarPath") or "").strip()
            break
    if not track or not car:
        return None
    session_type = ""
    for s in (sessions or {}).get("Sessions") or ():
        if s.get("SessionNum") == session_num:
            session_type = str(s.get("SessionType") or "").strip().lower()
            break
    return SessionInfo(game="iracing", track=track, car=car,
                       track_length_m=parse_track_length_m(weekend.get("TrackLength")),
                       session_type=session_type)


class IRacingAdapter(GameAdapter):
    name = "iracing"
    display_name = "iRacing"

    def __init__(self, check_process: bool = True, sdk_factory: Callable[[], Any] | None = None):
        self.check_process = check_process
        if sdk_factory is None:
            import irsdk                       # 没装 pyirsdk 时在这里报错，由采集线程记一次日志
            sdk_factory = irsdk.IRSDK
            self._need_mapping_check = True
        else:
            self._need_mapping_check = False   # 测试注入的假 SDK
        self._factory = sdk_factory
        self._ir = None
        self._session: SessionInfo | None = None
        self._session_key: tuple | None = None

    def connect(self) -> bool:
        if self.is_running() is False:
            return False
        if self._need_mapping_check and not mapping_exists(MEMMAP_NAME):
            return False
        ir = self._factory()
        with contextlib.redirect_stdout(io.StringIO()):
            ok = ir.startup()
        if not ok or not ir.is_connected:
            ir.shutdown()
            return False
        self._ir = ir
        self._session, self._session_key = None, None
        return True

    def read(self) -> tuple[SessionInfo | None, TelemetryFrame] | None:
        ir = self._ir
        if ir is None or not ir.is_initialized or not ir.is_connected:
            raise AdapterDisconnected("iRacing 已退出或断开")
        ir.freeze_var_buffer_latest()
        try:
            v = {k: ir[k] for k in VARS}
        finally:
            ir.unfreeze_var_buffer_latest()

        key = (ir.session_info_update, v["SessionNum"])
        if key != self._session_key:
            self._session_key = key
            self._session = session_from_yaml(ir["WeekendInfo"], ir["DriverInfo"], ir["SessionInfo"],
                                              v["SessionNum"])
        return self._session, self.convert(v)

    @staticmethod
    def convert(v: dict) -> TelemetryFrame:
        def num(k, default=0.0):
            x = v.get(k)
            return default if x is None else x

        steer_max = float(num("SteeringWheelAngleMax"))
        steer = float(num("SteeringWheelAngle")) / steer_max if steer_max > 0 else 0.0
        last = float(num("LapLastLapTime"))
        return TelemetryFrame(
            speed_kmh=max(0.0, float(num("Speed")) * 3.6),
            throttle=clamp(num("Throttle"), 0.0, 1.0),
            brake=clamp(num("Brake"), 0.0, 1.0),
            steer=clamp(steer, -1.0, 1.0),
            gear=int(num("Gear", 0)),
            rpm=max(0, int(num("RPM"))),
            lap_count=int(num("LapCompleted", 0)),
            lap_time_ms=max(0, int(round(float(num("LapCurrentLapTime")) * 1000))),
            pos=clamp(num("LapDistPct"), 0.0, 1.0),
            in_pit=bool(v.get("OnPitRoad") or v.get("PlayerCarInPitStall")),
            lap_valid=int(num("PlayerTrackSurface", -1)) != TRK_OFF_TRACK,
            last_lap_ms=int(round(last * 1000)) if last > 0 else None,
            is_live=bool(v.get("IsOnTrack")) and not v.get("IsReplayPlaying"),
            tick=int(num("SessionTick", 0)),
        )

    def close(self) -> None:
        if self._ir is not None:
            self._ir.shutdown()
            self._ir = None

    def is_running(self) -> bool | None:
        if not self.check_process:
            return None
        from . import game_process_running
        return game_process_running(self.name)
