"""假的 ACC：把合成车手（adapters/simulated.py）的数据写成 ACC 的三个共享内存页。

    python tools/fake_acc.py                 # Windows：创建 Local\\acpmf_* 映射并以 60 Hz 写入
    python -m simtelemetry --game acc        # 另开一个窗口：走真正的 ACC 读取路径

用来在不开游戏的情况下端到端测试 ACCAdapter（OpenFileMapping → MapViewOfFile → 结构体 → 切圈）。
安全：如果映射已经存在（ACC 或别的工具开着），直接拒绝运行——往游戏的共享内存里写东西会把它搞坏。

encode() 是纯函数，测试也用它（不需要 Windows）。
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simtelemetry.adapters.acc_structs import (ACC_LIVE, GRAPHICS_MAP, PHYSICS_MAP, STATIC_MAP,  # noqa: E402
                                               SPageFileGraphic, SPageFilePhysics, SPageFileStatic)
from simtelemetry.adapters.simulated import DriverState, SyntheticDriver  # noqa: E402

TRACK = "synthetic_ring"
CAR = "ferrari_296_gt3"


def _set_wstr(arr, text: str) -> None:
    data = list(text.encode("utf-16-le"))
    words = [data[i] | data[i + 1] << 8 for i in range(0, len(data), 2)][: len(arr) - 1]
    for i in range(len(arr)):
        arr[i] = words[i] if i < len(words) else 0


def encode(s: DriverState, tick: int, status: int = ACC_LIVE, session: int = 0,
           track: str = TRACK, car: str = CAR) -> tuple[SPageFilePhysics, SPageFileGraphic, SPageFileStatic]:
    phys, gfx, stat = SPageFilePhysics(), SPageFileGraphic(), SPageFileStatic()
    phys.packetId = tick
    phys.gas, phys.brake, phys.steerAngle = s.throttle, s.brake, s.steer
    phys.speedKmh = s.speed_kmh
    phys.gear = s.gear + 1                    # 统一编码 → ACC：-1 R / 0 N / 1.. → 0 / 1 / 2..
    phys.rpms = s.rpm

    gfx.packetId = tick
    gfx.status = status
    gfx.session = session
    gfx.completedLaps = s.lap_count
    gfx.iCurrentTime = s.lap_time_ms
    gfx.iLastTime = s.last_lap_ms if s.last_lap_ms > 0 else 2**31 - 1
    gfx.iBestTime = s.best_lap_ms if s.best_lap_ms > 0 else 2**31 - 1
    gfx.normalizedCarPosition = s.pos
    gfx.isInPit = int(s.in_pit)
    gfx.isInPitLane = int(s.in_pit_lane)
    gfx.isValidLap = int(s.lap_valid)

    _set_wstr(stat.smVersion, "1.9")
    _set_wstr(stat.track, track)
    _set_wstr(stat.carModel, car)
    stat.trackSPlineLength = 0.0              # 和真实 ACC 一样不给赛道长度
    return phys, gfx, stat


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("只能在 Windows 上运行（需要创建命名共享内存）")
    import mmap

    from simtelemetry.adapters.winshm import mapping_exists

    names = (PHYSICS_MAP, GRAPHICS_MAP, STATIC_MAP)
    taken = [n for n in names if mapping_exists(n)]
    if taken:
        raise SystemExit(f"共享内存 {', '.join(taken)} 已存在（ACC 或其它遥测工具在运行），拒绝写入")
    sizes = (ctypes.sizeof(SPageFilePhysics), ctypes.sizeof(SPageFileGraphic), ctypes.sizeof(SPageFileStatic))
    maps = [mmap.mmap(-1, size, tagname=name) for name, size in zip(names, sizes)]

    driver = SyntheticDriver()
    tick, last = 0, time.monotonic()
    print("正在写入假的 ACC 数据，按 Ctrl+C 停止")
    try:
        while True:
            now = time.monotonic()
            state = driver.step(now - last)
            last = now
            tick += 1
            for m, struct in zip(maps, encode(state, tick)):
                m.seek(0)
                m.write(bytes(struct))
            time.sleep(1 / 60)
    except KeyboardInterrupt:
        pass
    finally:
        for m in maps:
            m.close()


if __name__ == "__main__":
    main()
