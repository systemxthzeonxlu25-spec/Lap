"""Le Mans Ultimate：读游戏自带的 “LMU_Data” 共享内存（不需要装 rF2 插件 DLL）。

前提：游戏里 设置 → Gameplay → “Enable Plugins” 打开（改完要重启游戏），否则游戏不创建这块映射。

和 ACC 一样分两层：LMUAdapter 只管从映射里拷出需要的几段，translate() 是纯函数。
只拷玩家那辆车的遥测（1888 字节）、计分总信息和玩家的计分条目，不拷整块 325 KB。

字段映射：
- 速度 = |mLocalVel| × 3.6；油门/刹车/方向 = mUnfiltered*（方向 -1..1，文档写明“左到右”）；挡位 -1 R / 0 N / 1..
- 本圈用时 = 遥测 mElapsedTime - mLapStartET；上一圈 = 计分 mLastLapTime（≤ 0 = 还没有）
- 已完成圈数 = 计分 mTotalLaps；维修区 = 计分 mInPits；本圈无效 = 遥测 mLapInvalidated
- 赛道长度 = 计分 mLapDist（米，rF2 系直接给），位置 = 车的 mLapDist / 赛道长度
- 只有 mInRealtime、非暂停（mGamePhase != 9）、玩家自己在开（mControl == 0）才记录
- tick = 遥测 mElapsedTime（毫秒）；暂停时不变
计分数据更新得比遥测慢（rF2 系大约 5 Hz），两次计分更新之间位置按速度外推；过线后从本圈开始时间重新算。
"""
from __future__ import annotations

import ctypes
import logging
import math
from typing import Protocol

from ..models import SessionInfo, TelemetryFrame
from . import game_process_running
from .base import GameAdapter
from .lmu_structs import (LMU_MAP, MAX_MAPPED_VEHICLES, LMUObjectOut, LMUScoringData, LMUScoringInfo,
                          LMUTelemetryData, LMUVehicleScoring, LMUVehicleTelemetry)
from .winshm import SharedMemoryPage

log = logging.getLogger(__name__)

OFF_SCORING_INFO = LMUObjectOut.scoring.offset + LMUScoringData.scoringInfo.offset
OFF_VEH_SCORING = LMUObjectOut.scoring.offset + LMUScoringData.vehScoringInfo.offset
OFF_TELEMETRY = LMUObjectOut.telemetry.offset
OFF_TELEM_INFO = OFF_TELEMETRY + LMUTelemetryData.telemInfo.offset
SIZE_VEH_SCORING = ctypes.sizeof(LMUVehicleScoring)
SIZE_TELEM = ctypes.sizeof(LMUVehicleTelemetry)

GAME_PHASE_PAUSED = 9


class _TelemHead(ctypes.Structure):
    _pack_ = 4
    _fields_ = [("activeVehicles", ctypes.c_uint8), ("playerVehicleIdx", ctypes.c_uint8),
                ("playerHasVehicle", ctypes.c_bool)]


def session_type(code: int) -> str:
    if code == 0:
        return "testday"
    if 1 <= code <= 4:
        return "practice"
    if 5 <= code <= 8:
        return "qualify"
    if code == 9:
        return "warmup"
    if 10 <= code <= 13:
        return "race"
    return "unknown"


def text(raw: bytes) -> str:
    return bytes(raw).split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()   # LMU 的名字是 UTF-8


def sanity_problem(info: LMUScoringInfo, tel: LMUVehicleTelemetry) -> str | None:
    """结构体布局只有一个来源，没有独立实测——读出来的数不合理就别记录。"""
    track_s, track_t = text(info.mTrackName), text(tel.mTrackName)
    if track_s and track_t and track_s != track_t:
        return f"计分和遥测里的赛道名不一致（{track_s!r} / {track_t!r}）"
    if not (300 < info.mLapDist < 40000):
        return f"赛道长度 {info.mLapDist:.1f} m 不合理"
    if not (-1 <= tel.mGear <= 12):
        return f"挡位 {tel.mGear} 不合理"
    if not (-0.01 <= tel.mUnfilteredThrottle <= 1.01 and -0.01 <= tel.mUnfilteredBrake <= 1.01):
        return "油门/刹车不在 0..1"
    if not (0 <= tel.mEngineRPM < 30000):
        return f"转速 {tel.mEngineRPM:.0f} 不合理"
    return None


def translate(info: LMUScoringInfo, veh: LMUVehicleScoring | None, tel: LMUVehicleTelemetry
              ) -> tuple[SessionInfo | None, TelemetryFrame]:
    v = tel.mLocalVel
    speed = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
    track_len = float(info.mLapDist)
    et = float(tel.mElapsedTime)

    track = text(info.mTrackName) or text(tel.mTrackName)
    car = text(tel.mVehicleModel) or text(tel.mVehicleName)
    session = None
    if track and car and veh is not None and track_len > 0:
        session = SessionInfo(game="lmu", track=track, car=car, track_length_m=round(track_len, 1),
                              session_type=session_type(info.mSession))

    pos, lap_count, in_pit, last = 0.0, int(tel.mLapNumber), False, None
    if veh is not None:
        lap_count, in_pit = int(veh.mTotalLaps), bool(veh.mInPits)
        if veh.mLastLapTime > 0:
            last = round(veh.mLastLapTime * 1000)
        if track_len > 0:
            if tel.mLapStartET > info.mCurrentET:        # 上次计分更新之后才过的线：从本圈开始时间算
                dist = speed * (et - tel.mLapStartET)
            else:
                dist = veh.mLapDist + speed * max(0.0, et - info.mCurrentET)
            pos = max(0.0, min(1.0, dist / track_len))

    live = bool(info.mInRealtime) and info.mGamePhase != GAME_PHASE_PAUSED and (veh is None or veh.mControl == 0)
    frame = TelemetryFrame(
        speed_kmh=speed * 3.6,
        throttle=max(0.0, min(1.0, tel.mUnfilteredThrottle)),
        brake=max(0.0, min(1.0, tel.mUnfilteredBrake)),
        steer=max(-1.0, min(1.0, tel.mUnfilteredSteering)),
        gear=int(tel.mGear),
        rpm=max(0, int(tel.mEngineRPM)),
        lap_count=lap_count,
        lap_time_ms=max(0, round((et - tel.mLapStartET) * 1000)),
        pos=pos,
        in_pit=in_pit,
        lap_valid=not tel.mLapInvalidated,
        last_lap_ms=last,
        is_live=live,
        tick=round(et * 1000),
    )
    return session, frame


class Page(Protocol):
    def open(self) -> bool: ...
    def read_at(self, struct_type, offset: int): ...
    def close(self) -> None: ...


class LMUAdapter(GameAdapter):
    name = "lmu"
    display_name = "Le Mans Ultimate"

    def __init__(self, page: Page | None = None):
        self._page = page or SharedMemoryPage(LMU_MAP, LMUObjectOut)
        self._veh_idx: int | None = None
        self._warned: str | None = None
        self._hinted = False

    def connect(self) -> bool:
        if self._page.open():
            return True
        if not self._hinted and game_process_running("lmu"):
            self._hinted = True
            log.warning("LMU 在运行，但没有 LMU_Data 共享内存：请在 设置 → Gameplay 打开 “Enable Plugins” 并重启游戏")
        return False

    def _player_scoring(self, info: LMUScoringInfo, slot_id: int) -> LMUVehicleScoring | None:
        def at(i: int) -> LMUVehicleScoring:
            return self._page.read_at(LMUVehicleScoring, OFF_VEH_SCORING + i * SIZE_VEH_SCORING)

        if self._veh_idx is not None:
            veh = at(self._veh_idx)
            if veh.mIsPlayer and veh.mID == slot_id:
                return veh
        n = max(0, min(int(info.mNumVehicles), MAX_MAPPED_VEHICLES))
        for i in range(n):
            veh = at(i)
            if veh.mIsPlayer:
                self._veh_idx = i
                return veh
        self._veh_idx = None
        return None

    def read(self):
        head = self._page.read_at(_TelemHead, OFF_TELEMETRY)
        if not head.playerHasVehicle or head.playerVehicleIdx >= MAX_MAPPED_VEHICLES:
            return None                                  # 菜单里 / 还没上车
        off = OFF_TELEM_INFO + head.playerVehicleIdx * SIZE_TELEM
        tel = self._page.read_at(LMUVehicleTelemetry, off)
        for _ in range(3):                               # 读前后时间戳一致才认为没读到写了一半的数据
            again = self._page.read_at(LMUVehicleTelemetry, off)
            if again.mElapsedTime == tel.mElapsedTime:
                break
            tel = again
        info = self._page.read_at(LMUScoringInfo, OFF_SCORING_INFO)
        problem = sanity_problem(info, tel)
        if problem:
            if problem != self._warned:
                self._warned = problem
                log.warning("LMU 数据看起来不对，暂不记录：%s。可能是游戏更新改了共享内存布局，"
                            "请用 tools/lmu_dump.py 存一份数据反馈", problem)
            return None
        self._warned = None
        return translate(info, self._player_scoring(info, tel.mID), tel)

    def close(self) -> None:
        self._page.close()
        self._veh_idx = None

    def is_running(self) -> bool | None:
        return game_process_running("lmu")
