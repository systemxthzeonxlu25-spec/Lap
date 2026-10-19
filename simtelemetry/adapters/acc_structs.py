"""ACC 共享内存结构体。

来源：Kunos 官方 “ACC Shared Memory Documentation v1.8.12”（ACCSharedMemoryDocumentationV1.8.12.pdf，
2022-03-23，assettocorsa.net 论坛 “ACC Shared Memory Documentation” 帖子的附件）。
字段顺序、类型、数组长度逐个按文档抄写；文档里没写、或与官方示例头文件 SharedFileOut.h
不一致的地方用 NOTE / TODO 标注。

- 打包方式：#pragma pack(4)（来自官方示例头文件 SharedFileOut.h，文档正文没写）。
- wchar_t 在 Windows 上是 2 字节 UTF-16。这里统一用 c_uint16 数组表示，读字符串用 wchar_str()，
  这样 ctypes 不会在第一个 \\0 处截断，也不依赖平台的 wchar_t 宽度。
- 文档标为 “not used in ACC” 的字段照样定义——它们仍然占位置，删掉会让后面的偏移全部错位。
- 按文档计算的结构体大小：physics 800、graphics 1588、static 820 字节（tests/test_acc_layout.py 校验）。
"""
from __future__ import annotations

import ctypes
from ctypes import c_float, c_int32, c_uint16

PHYSICS_MAP = "Local\\acpmf_physics"
GRAPHICS_MAP = "Local\\acpmf_graphics"
STATIC_MAP = "Local\\acpmf_static"


def _w(n: int):
    """wchar_t[n]"""
    return c_uint16 * n


def wchar_str(arr) -> str:
    raw = bytes(arr)
    s = raw.decode("utf-16-le", errors="replace")
    return s.split("\x00", 1)[0].strip()


# ---- 枚举（文档 “Enums” 一节；底层都是 4 字节 int）---------------------------------
ACC_OFF, ACC_REPLAY, ACC_LIVE, ACC_PAUSE = 0, 1, 2, 3          # ACC_STATUS

ACC_SESSION_TYPE = {
    -1: "unknown", 0: "practice", 1: "qualify", 2: "race", 3: "hotlap", 4: "timeattack",
    5: "drift", 6: "drag", 7: "hotstint", 8: "hotstint_superpole",
}

ACC_FLAG_TYPE = {
    0: "none", 1: "blue", 2: "yellow", 3: "black", 4: "white", 5: "checkered", 6: "penalty",
    7: "green", 8: "orange",
}


class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int32),
        ("gas", c_float),                    # 油门 0..1
        ("brake", c_float),                  # 刹车 0..1
        ("fuel", c_float),
        ("gear", c_int32),                   # 文档只写 “Current gear”；社区实测 0=R 1=N 2=1挡（见 acc.py）
        ("rpms", c_int32),                   # 文档写作 rpm，示例头文件写作 rpms
        ("steerAngle", c_float),             # 方向盘输入 -1..1（文档未说明左/右的正负）
        ("speedKmh", c_float),
        ("velocity", c_float * 3),
        ("accG", c_float * 3),
        ("wheelSlip", c_float * 4),
        ("wheelLoad", c_float * 4),          # not used in ACC
        ("wheelPressure", c_float * 4),
        ("wheelAngularSpeed", c_float * 4),
        ("tyreWear", c_float * 4),           # not used in ACC
        ("tyreDirtyLevel", c_float * 4),     # not used in ACC
        ("tyreCoreTemp", c_float * 4),
        ("camberRAD", c_float * 4),          # not used in ACC
        ("suspensionTravel", c_float * 4),
        ("drs", c_float),                    # not used in ACC
        ("tc", c_float),
        ("heading", c_float),
        ("pitch", c_float),
        ("roll", c_float),
        ("cgHeight", c_float),               # not used in ACC
        ("carDamage", c_float * 5),
        ("numberOfTyresOut", c_int32),       # not used in ACC
        ("pitLimiterOn", c_int32),
        ("abs", c_float),
        ("kersCharge", c_float),             # not used in ACC
        ("kersInput", c_float),              # not used in ACC
        ("autoshifterOn", c_int32),
        ("rideHeight", c_float * 2),         # not used in ACC
        ("turboBoost", c_float),
        ("ballast", c_float),                # not used in ACC
        ("airDensity", c_float),             # not used in ACC
        ("airTemp", c_float),
        ("roadTemp", c_float),
        ("localAngularVel", c_float * 3),
        ("finalFF", c_float),
        ("performanceMeter", c_float),       # not used in ACC
        ("engineBrake", c_int32),            # not used in ACC
        ("ersRecoveryLevel", c_int32),       # not used in ACC
        ("ersPowerLevel", c_int32),          # not used in ACC
        ("ersHeatCharging", c_int32),        # not used in ACC
        ("ersIsCharging", c_int32),          # not used in ACC
        ("kersCurrentKJ", c_float),          # not used in ACC
        ("drsAvailable", c_int32),           # not used in ACC
        ("drsEnabled", c_int32),             # not used in ACC
        ("brakeTemp", c_float * 4),
        ("clutch", c_float),
        ("tyreTempI", c_float * 4),          # not used in ACC
        ("tyreTempM", c_float * 4),          # not used in ACC
        ("tyreTempO", c_float * 4),          # not used in ACC
        ("isAIControlled", c_int32),
        ("tyreContactPoint", (c_float * 3) * 4),
        ("tyreContactNormal", (c_float * 3) * 4),
        ("tyreContactHeading", (c_float * 3) * 4),
        ("brakeBias", c_float),
        ("localVelocity", c_float * 3),
        ("P2PActivation", c_int32),          # not used in ACC
        ("P2PStatus", c_int32),              # not used in ACC
        # NOTE: 文档写 float，示例头文件写 int；都是 4 字节，不影响偏移。本项目不读这个字段。
        ("currentMaxRpm", c_float),          # not used in ACC
        ("mz", c_float * 4),                 # not used in ACC
        ("fx", c_float * 4),                 # not used in ACC
        ("fy", c_float * 4),                 # not used in ACC
        ("slipRatio", c_float * 4),
        ("slipAngle", c_float * 4),
        ("tcinAction", c_int32),             # not used in ACC
        ("absInAction", c_int32),            # not used in ACC
        ("suspensionDamage", c_float * 4),   # not used in ACC
        ("tyreTemp", c_float * 4),           # not used in ACC
        ("waterTemp", c_float),
        ("brakePressure", c_float * 4),
        ("frontBrakeCompound", c_int32),
        ("rearBrakeCompound", c_int32),
        ("padLife", c_float * 4),
        ("discLife", c_float * 4),
        ("ignitionOn", c_int32),
        ("starterEngineOn", c_int32),
        ("isEngineRunning", c_int32),
        ("kerbVibration", c_float),
        ("slipVibrations", c_float),
        ("gVibrations", c_float),
        ("absVibrations", c_float),
    ]


class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int32),
        ("status", c_int32),                 # ACC_STATUS
        ("session", c_int32),                # ACC_SESSION_TYPE
        ("currentTime", _w(15)),
        ("lastTime", _w(15)),
        ("bestTime", _w(15)),
        ("split", _w(15)),
        ("completedLaps", c_int32),
        ("position", c_int32),
        ("iCurrentTime", c_int32),           # 本圈用时 ms
        ("iLastTime", c_int32),              # 上一圈 ms（社区实测：没有时为 INT32_MAX）
        ("iBestTime", c_int32),
        ("sessionTimeLeft", c_float),
        ("distanceTraveled", c_float),
        ("isInPit", c_int32),                # 在维修位
        ("currentSectorIndex", c_int32),
        ("lastSectorTime", c_int32),
        ("numberOfLaps", c_int32),
        ("tyreCompound", _w(33)),
        ("replayTimeMultiplier", c_float),   # not used in ACC
        ("normalizedCarPosition", c_float),  # 赛道归一化位置 0..1
        ("activeCars", c_int32),
        ("carCoordinates", (c_float * 3) * 60),
        ("carID", c_int32 * 60),
        ("playerCarID", c_int32),
        ("penaltyTime", c_float),
        ("flag", c_int32),                   # ACC_FLAG_TYPE
        ("penalty", c_int32),                # ACC_PENALTY_TYPE（示例头文件：enum class PenaltyShortcut : int）
        ("idealLineOn", c_int32),
        ("isInPitLane", c_int32),            # 在维修区车道
        ("surfaceGrip", c_float),
        ("mandatoryPitDone", c_int32),
        ("windSpeed", c_float),
        ("windDirection", c_float),
        ("isSetupMenuVisible", c_int32),
        ("mainDisplayIndex", c_int32),
        ("secondaryDisplayIndex", c_int32),  # 文档拼写 secondaryDisplyIndex
        ("TC", c_int32),
        ("TCCut", c_int32),
        ("EngineMap", c_int32),
        ("ABS", c_int32),
        ("fuelXLap", c_float),
        ("rainLights", c_int32),
        ("flashingLights", c_int32),
        ("lightsStage", c_int32),
        ("exhaustTemperature", c_float),
        ("wiperLV", c_int32),
        ("driverStintTotalTimeLeft", c_int32),
        ("driverStintTimeLeft", c_int32),
        ("rainTyres", c_int32),
        ("sessionIndex", c_int32),
        ("usedFuel", c_float),
        ("deltaLapTime", _w(15)),
        ("iDeltaLapTime", c_int32),
        ("estimatedLapTime", _w(15)),
        ("iEstimatedLapTime", c_int32),
        ("isDeltaPositive", c_int32),
        ("iSplit", c_int32),
        ("isValidLap", c_int32),             # 本圈是否有效（社区实测：比赛/hotstint 会话里压线后也常保持 1）
        ("fuelEstimatedLaps", c_float),
        ("trackStatus", _w(33)),
        ("missingMandatoryPits", c_int32),
        ("Clock", c_float),
        ("directionLightsLeft", c_int32),
        ("directionLightsRight", c_int32),
        ("GlobalYellow", c_int32),
        ("GlobalYellow1", c_int32),
        ("GlobalYellow2", c_int32),
        ("GlobalYellow3", c_int32),
        ("GlobalWhite", c_int32),
        ("GlobalGreen", c_int32),
        ("GlobalChequered", c_int32),
        ("GlobalRed", c_int32),
        ("mfdTyreSet", c_int32),
        ("mfdFuelToAdd", c_float),
        ("mfdTyrePressureLF", c_float),
        ("mfdTyrePressureRF", c_float),
        ("mfdTyrePressureLR", c_float),
        ("mfdTyrePressureRR", c_float),
        ("trackGripStatus", c_int32),        # ACC_TRACK_GRIP_STATUS
        ("rainIntensity", c_int32),          # ACC_RAIN_INTENSITY
        ("rainIntensityIn10min", c_int32),
        ("rainIntensityIn30min", c_int32),
        ("currentTyreSet", c_int32),
        ("strategyTyreSet", c_int32),
        ("gapAhead", c_int32),
        ("gapBehind", c_int32),
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", _w(15)),
        ("acVersion", _w(15)),
        ("numberOfSessions", c_int32),
        ("numCars", c_int32),
        ("carModel", _w(33)),                # 车型 ID，如 porsche_991ii_gt3_r
        ("track", _w(33)),                   # 赛道 ID，如 monza
        ("playerName", _w(33)),
        ("playerSurname", _w(33)),
        ("playerNick", _w(33)),
        ("sectorCount", c_int32),
        ("maxTorque", c_float),              # not used in ACC
        ("maxPower", c_float),               # not used in ACC
        ("maxRpm", c_int32),
        ("maxFuel", c_float),
        ("suspensionMaxTravel", c_float * 4),  # not used in ACC
        ("tyreRadius", c_float * 4),         # not used in ACC
        ("maxTurboBoost", c_float),          # not used in ACC
        ("deprecated_1", c_float),
        ("deprecated_2", c_float),
        ("penaltiesEnabled", c_int32),
        ("aidFuelRate", c_float),
        ("aidTireRate", c_float),
        ("aidMechanicalDamage", c_float),
        # NOTE: 文档写 float AllowTyreBlankets，示例头文件写 int aidAllowTyreBlankets；同为 4 字节。
        ("aidAllowTyreBlankets", c_int32),
        ("aidStability", c_float),
        ("aidAutoClutch", c_int32),
        ("aidAutoBlip", c_int32),
        ("hasDRS", c_int32),                 # not used in ACC
        ("hasERS", c_int32),                 # not used in ACC
        ("hasKERS", c_int32),                # not used in ACC
        ("kersMaxJ", c_float),               # not used in ACC
        ("engineBrakeSettingsCount", c_int32),  # not used in ACC
        ("ersPowerControllerCount", c_int32),   # not used in ACC
        ("trackSPlineLength", c_float),      # not used in ACC（实测为 0）
        # TODO(核对)：文档里这一行写的是 “wchar_t trackConfiguration”，没写数组长度。
        # 官方示例头文件和所有社区实现都是 [33]（论坛 #339 反馈写成 15 会让后面字段全部错位）。
        # 本项目只读到 trackSPlineLength 之前的字段，这里即使不对也不影响采集。
        ("trackConfiguration", _w(33)),      # not used in ACC
        ("ersMaxJ", c_float),                # not used in ACC
        ("isTimedRace", c_int32),            # not used in ACC
        ("hasExtraLap", c_int32),            # not used in ACC
        ("carSkin", _w(33)),                 # not used in ACC
        ("reversedGridPositions", c_int32),  # not used in ACC
        ("PitWindowStart", c_int32),
        ("PitWindowEnd", c_int32),
        ("isOnline", c_int32),
        ("dryTyresName", _w(33)),
        ("wetTyresName", _w(33)),
    ]
