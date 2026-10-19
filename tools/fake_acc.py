"""假 ACC：把合成车手的数据写进和 ACC 同名的共享内存，端到端测试真实的读取路径（写映射部分仅 Windows）。

    python tools/fake_acc.py                                   # 写 Local\\acpmf_*，Ctrl+C 退出
    python -m simtelemetry --game acc --no-process-check       # 另一个终端：照常采集 + 开网页

安全：映射已经存在（ACC / AC 正在运行，或者 SimHub 之类的程序还开着）时拒绝运行——
那时 CreateFileMapping 拿到的是游戏那块内存，往里写会把游戏和其它遥测软件的数据写坏。

模拟的 ACC 时序（社区实测，见 PROGRESS.md）：
- 跨线时 iCurrentTime / 位置先归零，completedLaps 和 iLastTime 晚 --line-lag 帧才更新（两者同一帧）；
- 第一圈跑完之前 iLastTime = INT32_MAX；挡位按 0 = R、1 = N、2 = 1 挡编码。
FakeACC 本身跨平台，tests/test_acc_adapter.py 用它在 Linux 上测试 ACCAdapter 的转换和切圈。
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simtelemetry.adapters.acc_structs import (ACC_LIVE, GRAPHICS_MAP, PHYSICS_MAP, STATIC_MAP,  # noqa: E402
                                               SPageFileGraphic, SPageFilePhysics, SPageFileStatic)
from simtelemetry.adapters.kunos import INT32_MAX  # noqa: E402
from simtelemetry.adapters.simulated import TRACK_LENGTH_M, SyntheticDriver  # noqa: E402

TRACK_ID = "synthetic_ring"
CAR_ID = "demo_gt3"


def put_wstr(arr, text: str) -> None:
    """写 wchar_t[n]（UTF-16LE，结尾补 \\0）。"""
    codes = list(text.encode("utf-16-le"))
    units = [codes[i] | (codes[i + 1] << 8) for i in range(0, len(codes), 2)][: len(arr) - 1]
    for i in range(len(arr)):
        arr[i] = units[i] if i < len(units) else 0


class FakeACC:
    def __init__(self, seed: int = 7, laps_between_stops: int = 4, line_lag: int = 2):
        self.driver = SyntheticDriver(seed=seed, laps_between_stops=laps_between_stops)
        self.line_lag = max(0, line_lag)
        self.packet = 0
        self._lagged: deque[tuple[int, int]] = deque()
        self.static = SPageFileStatic()
        put_wstr(self.static.smVersion, "1.8")
        put_wstr(self.static.acVersion, "fake")
        put_wstr(self.static.track, TRACK_ID)
        put_wstr(self.static.carModel, CAR_ID)
        self.static.trackSPlineLength = 0.0        # 和真 ACC 一样不给赛道长度
        self.static.maxRpm = 8500

    def step(self, dt: float) -> tuple[SPageFilePhysics, SPageFileGraphic, SPageFileStatic]:
        s = self.driver.step(dt)
        self.packet += 1
        last = s.last_lap_ms if s.last_lap_ms > 0 else INT32_MAX
        self._lagged.append((s.lap_count, last))
        while len(self._lagged) > self.line_lag + 1:
            self._lagged.popleft()
        laps, last = self._lagged[0]

        p = SPageFilePhysics()
        p.packetId = self.packet
        p.gas, p.brake, p.steerAngle, p.speedKmh = s.throttle, s.brake, s.steer, s.speed_kmh
        p.gear = s.gear + 1
        p.rpms = s.rpm

        g = SPageFileGraphic()
        g.packetId = self.packet
        g.status = ACC_LIVE
        g.session = 0                              # practice
        g.completedLaps = laps
        g.iCurrentTime = s.lap_time_ms
        g.iLastTime = last
        g.iBestTime = s.best_lap_ms or INT32_MAX
        g.normalizedCarPosition = s.pos
        g.distanceTraveled = (s.lap_count + s.pos) * TRACK_LENGTH_M
        g.isInPit = int(s.in_pit)
        g.isInPitLane = int(s.in_pit_lane)
        g.isValidLap = int(s.lap_valid)
        return p, g, self.static


# ----------------------------------------------------------------- Windows 映射
def _win_api():
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileMappingW.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.LPCWSTR)
    k32.CreateFileMappingW.restype = wintypes.HANDLE
    k32.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_size_t)
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32


INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PAGE_READWRITE = 0x04
FILE_MAP_WRITE = 0x0002
ERROR_ALREADY_EXISTS = 183


class WritableMapping:
    def __init__(self, k32, name: str, size: int):
        self.k32, self.name, self.size = k32, name, size
        h = k32.CreateFileMappingW(INVALID_HANDLE_VALUE, None, PAGE_READWRITE, 0, size, name)
        err = ctypes.get_last_error()
        if not h:
            raise OSError(f"CreateFileMappingW({name}) 失败，错误码 {err}")
        if err == ERROR_ALREADY_EXISTS:           # 拿到的是别人（多半是游戏）的映射：立刻放手
            k32.CloseHandle(h)
            raise FileExistsError(name)
        addr = k32.MapViewOfFile(h, FILE_MAP_WRITE, 0, 0, size)
        if not addr:
            err = ctypes.get_last_error()
            k32.CloseHandle(h)
            raise OSError(f"MapViewOfFile({name}) 失败，错误码 {err}")
        self.handle, self.addr = h, addr

    def write(self, struct: ctypes.Structure) -> None:
        ctypes.memmove(self.addr, ctypes.addressof(struct), self.size)

    def close(self) -> None:
        self.k32.UnmapViewOfFile(self.addr)
        self.k32.CloseHandle(self.handle)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rate", type=float, default=60.0, help="写入频率 Hz（ACC 物理页大约 300 Hz，这里够用即可）")
    ap.add_argument("--speed", type=float, default=1.0, help="时间倍速")
    ap.add_argument("--line-lag", type=int, default=2, help="圈数比计时晚几帧更新")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        print("fake_acc 需要 Windows（命名共享内存）。Linux / macOS 上用 python -m simtelemetry --game sim。")
        return 2

    from simtelemetry.adapters.winshm import mapping_exists
    names = (PHYSICS_MAP, GRAPHICS_MAP, STATIC_MAP)
    busy = [n for n in names if mapping_exists(n)]
    if busy:
        print(f"拒绝运行：{', '.join(busy)} 已存在（ACC / AC 在运行，或其它程序还开着这块内存）。")
        return 1

    k32 = _win_api()
    maps: list[WritableMapping] = []
    try:
        for name, st in zip(names, (SPageFilePhysics, SPageFileGraphic, SPageFileStatic)):
            maps.append(WritableMapping(k32, name, ctypes.sizeof(st)))
    except FileExistsError as e:
        print(f"拒绝运行：{e} 在检查之后被别的程序创建了。")
        for m in maps:
            m.close()
        return 1

    fake = FakeACC(seed=args.seed, line_lag=args.line_lag)
    period = 1.0 / args.rate
    print(f"假 ACC 运行中（赛道 {TRACK_ID} / 车 {CAR_ID}），Ctrl+C 退出。")
    print("另一个终端运行：python -m simtelemetry --game acc --no-process-check")
    last_lap = 0
    try:
        next_t = time.perf_counter()
        prev = time.monotonic()
        while True:
            now = time.monotonic()
            phys, gfx, stat = fake.step((now - prev) * args.speed)
            prev = now
            maps[2].write(stat)
            maps[1].write(gfx)
            maps[0].write(phys)
            if gfx.completedLaps != last_lap:
                last_lap = gfx.completedLaps
                ms = gfx.iLastTime
                print(f"第 {last_lap} 圈完成：{ms // 60000}:{ms % 60000 / 1000:06.3f}")
            next_t += period
            time.sleep(max(0.0, next_t - time.perf_counter()))
    except KeyboardInterrupt:
        pass
    finally:
        for m in maps:
            m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
