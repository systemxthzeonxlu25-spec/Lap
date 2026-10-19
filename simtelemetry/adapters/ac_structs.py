"""初代 Assetto Corsa（AC1）共享内存结构体。

AC1 和 ACC 用同名映射（Local\\acpmf_physics / graphics / static），ACC 的结构体是在 AC1 基础上往后追加的：
- physics：AC1 = ACC physics 从开头到 localVelocity（580 字节）；
- static：AC1 = ACC static 从开头到 PitWindowEnd（684 字节；ACC 的 deprecated_1/2 在 AC1 叫 airTemp/roadTemp）；
- graphics：前 252 字节（到 normalizedCarPosition）相同，之后 AC1 是单车 carCoordinates[3]、penaltyTime、
  flag、idealLineOn、isInPitLane……，ACC 则插入了 60 辆车的坐标表，所以这里单独定义。

来源（两份独立实现逐字段一致，均为 #pragma pack(4)、wchar_t = 2 字节）：
- albertowd/live-telemetry 的 apps/python/LiveTelemetry/lib/sim_info.py（标注 “updated to AC 1.14.3”）；
- mdjarv/assettocorsasharedmemory 的 Graphics.cs / Physics.cs / StaticInfo.cs（C#，带 “since 1.x” 版本注释）。
两者都引用 assettocorsa.net 论坛的 “Shared Memory Reference” 帖（官方帖本身云端没能打开）。
AC 1.16 之后的版本可能在结构体末尾追加了字段；末尾追加不影响这里的偏移，winshm 也只拷贝结构体大小。

NOTE：trackConfiguration 的长度两份来源不一致（sim_info.py 33、C# 15）。它是 static 里本项目读的最后一个字段，
起始偏移（524）不受影响；按 33 读、遇到第一个 \\0 截止，正常的赛道布局名都远短于 15 个字符。
"""
from __future__ import annotations

import ctypes
from ctypes import c_float, c_int32

from .acc_structs import SPageFilePhysics as _ACCPhysics
from .acc_structs import SPageFileStatic as _ACCStatic
from .acc_structs import _w

AC_OFF, AC_REPLAY, AC_LIVE, AC_PAUSE = 0, 1, 2, 3

AC_SESSION_TYPE = {
    -1: "unknown", 0: "practice", 1: "qualify", 2: "race", 3: "hotlap", 4: "timeattack",
    5: "drift", 6: "drag",
}


def _prefix(fields: list, last: str) -> list:
    names = [f[0] for f in fields]
    return list(fields[: names.index(last) + 1])


class ACPhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = _prefix(_ACCPhysics._fields_, "localVelocity")
    # 本项目用到的：packetId gas brake gear rpms steerAngle speedKmh numberOfTyresOut


class ACStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = _prefix(_ACCStatic._fields_, "PitWindowEnd")


class ACGraphics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int32),
        ("status", c_int32),                 # AC_STATUS
        ("session", c_int32),                # AC_SESSION_TYPE
        ("currentTime", _w(15)),
        ("lastTime", _w(15)),
        ("bestTime", _w(15)),
        ("split", _w(15)),
        ("completedLaps", c_int32),
        ("position", c_int32),
        ("iCurrentTime", c_int32),
        ("iLastTime", c_int32),
        ("iBestTime", c_int32),
        ("sessionTimeLeft", c_float),
        ("distanceTraveled", c_float),
        ("isInPit", c_int32),
        ("currentSectorIndex", c_int32),
        ("lastSectorTime", c_int32),
        ("numberOfLaps", c_int32),
        ("tyreCompound", _w(33)),
        ("replayTimeMultiplier", c_float),
        ("normalizedCarPosition", c_float),
        ("carCoordinates", c_float * 3),
        ("penaltyTime", c_float),
        ("flag", c_int32),
        ("idealLineOn", c_int32),
        ("isInPitLane", c_int32),            # since 1.5
        ("surfaceGrip", c_float),            # since 1.5
        ("mandatoryPitDone", c_int32),       # since 1.13
        ("windSpeed", c_float),              # sim_info.py 有、C# 版没有；在末尾，不影响前面的偏移
        ("windDirection", c_float),
    ]
