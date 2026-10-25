"""假的 Le Mans Ultimate：把合成车手的数据写成 “LMU_Data” 共享内存的布局。

    python tools/fake_lmu.py               # Windows：创建 LMU_Data 映射并以 60 Hz 写入（映射已存在时拒绝运行）
    python -m simtelemetry --game lmu      # 另开一个窗口，走真正的 LMU 读取路径

BufferPage / encode() 不需要 Windows，测试和 tools/lmu_dump.py 存下的数据回放都用它们。
计分数据默认每 10 帧（6 Hz）才更新一次，模拟游戏里计分比遥测慢的情况。
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simtelemetry.adapters.lmu import (OFF_SCORING_INFO, OFF_TELEM_INFO, OFF_TELEMETRY,  # noqa: E402
                                       OFF_VEH_SCORING, _TelemHead)
from simtelemetry.adapters.lmu_structs import (LMU_MAP, LMUObjectOut, LMUScoringInfo,  # noqa: E402
                                               LMUVehicleScoring, LMUVehicleTelemetry)
from simtelemetry.adapters.simulated import TRACK_LENGTH_M, DriverState, SyntheticDriver  # noqa: E402

TRACK = "Synthetic Ring"
CAR = "Ferrari 499P"
SLOT_ID = 7


class BufferPage:
    """用一块内存（或 lmu_dump 存下的文件内容）充当共享内存页。"""

    def __init__(self, buf: bytearray | bytes | None = None):
        self.buf = bytearray(buf) if buf is not None else bytearray(ctypes.sizeof(LMUObjectOut))
        self.opened = False

    def open(self) -> bool:
        self.opened = True
        return True

    def read_at(self, struct_type, offset: int):
        size = ctypes.sizeof(struct_type)
        chunk = bytes(self.buf[offset:offset + size]).ljust(size, b"\0")
        return struct_type.from_buffer_copy(chunk)

    def close(self) -> None:
        self.opened = False

    def write_at(self, offset: int, struct) -> None:
        data = bytes(struct)
        self.buf[offset:offset + len(data)] = data


class FakeLMU:
    """维护一个持续前进的“游戏时间”，把每一步的车手状态写进 BufferPage。"""

    def __init__(self, page: BufferPage | None = None, scoring_every: int = 10, session: int = 1):
        self.page = page or BufferPage()
        self.scoring_every = scoring_every
        self.session = session
        self.et = 100.0
        self.lap_start_et = self.et
        self.lap_count = 0
        self.frame = 0
        self.scoring_et = self.et
        self.paused = False

    def write(self, s: DriverState, dt: float) -> None:
        self.frame += 1
        if not self.paused:
            self.et += dt
        if s.lap_count != self.lap_count:                       # 过线：本圈开始时间 = 现在 - 新圈已走的时间
            self.lap_count = s.lap_count
            self.lap_start_et = self.et - s.lap_time_ms / 1000
        page = self.page

        head = _TelemHead(activeVehicles=1, playerVehicleIdx=0, playerHasVehicle=True)
        page.write_at(OFF_TELEMETRY, head)
        tel = LMUVehicleTelemetry()
        tel.mID = SLOT_ID
        tel.mElapsedTime = self.et
        tel.mLapStartET = self.lap_start_et
        tel.mLapNumber = s.lap_count + 1
        tel.mVehicleName = f"{CAR} #50:LM".encode()
        tel.mVehicleModel = CAR.encode()
        tel.mTrackName = TRACK.encode()
        tel.mLocalVel.z = -s.speed_kmh / 3.6                    # rF2 坐标系里前进方向是 -z，取模后无所谓
        tel.mGear = s.gear
        tel.mEngineRPM = s.rpm
        tel.mUnfilteredThrottle, tel.mUnfilteredBrake, tel.mUnfilteredSteering = s.throttle, s.brake, s.steer
        tel.mLapInvalidated = not s.lap_valid
        page.write_at(OFF_TELEM_INFO, tel)

        if self.frame % self.scoring_every == 1 or self.scoring_every == 1:
            self.scoring_et = self.et
            info = LMUScoringInfo()
            info.mTrackName = TRACK.encode()
            info.mSession = self.session
            info.mCurrentET = self.scoring_et
            info.mLapDist = TRACK_LENGTH_M
            info.mNumVehicles = 1
            info.mGamePhase = 9 if self.paused else 5
            info.mInRealtime = True
            page.write_at(OFF_SCORING_INFO, info)
            veh = LMUVehicleScoring()
            veh.mID = SLOT_ID
            veh.mIsPlayer = True
            veh.mControl = 0
            veh.mTotalLaps = s.lap_count
            veh.mLapDist = s.pos * TRACK_LENGTH_M
            veh.mInPits = s.in_pit or s.in_pit_lane
            veh.mLastLapTime = s.last_lap_ms / 1000 if s.last_lap_ms else 0.0
            page.write_at(OFF_VEH_SCORING, veh)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("只能在 Windows 上运行（需要创建命名共享内存）")
    import mmap

    from simtelemetry.adapters.winshm import mapping_exists

    if mapping_exists(LMU_MAP):
        raise SystemExit(f"共享内存 {LMU_MAP} 已存在（LMU 或其它工具在运行），拒绝写入")
    m = mmap.mmap(-1, ctypes.sizeof(LMUObjectOut), tagname=LMU_MAP)
    fake, driver = FakeLMU(), SyntheticDriver()
    last = time.monotonic()
    print("正在写入假的 LMU 数据，按 Ctrl+C 停止")
    try:
        while True:
            now = time.monotonic()
            dt, last = now - last, now
            fake.write(driver.step(dt), dt)
            m.seek(0)
            m.write(bytes(fake.page.buf))
            time.sleep(1 / 60)
    except KeyboardInterrupt:
        pass
    finally:
        m.close()


if __name__ == "__main__":
    main()
