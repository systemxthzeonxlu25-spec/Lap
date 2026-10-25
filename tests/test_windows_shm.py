"""只在 Windows 上跑：真的创建命名共享内存，走 OpenFileMapping → MapViewOfFile → 结构体 → 切圈 的完整路径。

Linux 上这些代码路径（winshm / procs）只能降级，测不到；CI 的 windows-latest 任务会跑这个文件。
映射名和游戏的一样（Local\\acpmf_*、LMU_Data）——CI 机器上没有游戏，不会冲突；本地有游戏在跑时自动跳过。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 命名共享内存")

if sys.platform == "win32":
    import mmap

    from simtelemetry.adapters.acc import ACCAdapter
    from simtelemetry.adapters.acc_structs import (GRAPHICS_MAP, PHYSICS_MAP, STATIC_MAP, SPageFileGraphic,
                                                   SPageFilePhysics, SPageFileStatic)
    from simtelemetry.adapters.lmu import LMUAdapter
    from simtelemetry.adapters.lmu_structs import LMU_MAP, LMUObjectOut
    from simtelemetry.adapters.procs import running_process_names
    from simtelemetry.adapters.simulated import SyntheticDriver
    from simtelemetry.adapters.winshm import SharedMemoryPage, mapping_exists
    from simtelemetry.collector import Collector
    from simtelemetry.live import LiveHub
    from simtelemetry.storage import Storage
    from tools.fake_acc import encode
    from tools.fake_lmu import FakeLMU

SPEEDUP = 12          # 合成车手每步最多走 0.2 s，60 Hz 写入时最快 12 倍速：一圈 ~92 s → ~8 s


def _maps(names_sizes):
    taken = [n for n, _ in names_sizes if mapping_exists(n)]
    if taken:
        pytest.skip(f"共享内存 {taken} 已存在（本机有游戏或工具在运行）")
    return [mmap.mmap(-1, size, tagname=name) for name, size in names_sizes]


def _write(m, data: bytes) -> None:
    m.seek(0)
    m.write(data)


class Writer(threading.Thread):
    """60 Hz 往映射里写合成数据，模拟游戏。"""

    def __init__(self, step):
        super().__init__(daemon=True)
        self.step, self.stop = step, threading.Event()

    def run(self):
        while not self.stop.is_set():
            self.step()
            time.sleep(1 / 60)


def _collect(game: str, tmp_path, seconds: float):
    storage = Storage(tmp_path / f"{game}.db")
    col = Collector(storage, LiveHub(), game=game, rate_hz=50, retry_s=0.2)
    col.start()
    try:
        time.sleep(seconds)
    finally:
        col.stop()
    return storage


def test_process_list_contains_python():
    names = running_process_names()
    assert any(n.startswith("python") for n in names)


def test_missing_mapping_is_not_created():
    page = SharedMemoryPage("Local\\simtelemetry_test_does_not_exist", SPageFilePhysics)
    assert not page.open()
    assert not mapping_exists("Local\\simtelemetry_test_does_not_exist")   # 只读打开，绝不顺手创建


def test_acc_reads_real_mapping_and_cuts_laps(tmp_path):
    sizes = (ctypes.sizeof(SPageFilePhysics), ctypes.sizeof(SPageFileGraphic), ctypes.sizeof(SPageFileStatic))
    maps = _maps(list(zip((PHYSICS_MAP, GRAPHICS_MAP, STATIC_MAP), sizes)))
    driver, tick = SyntheticDriver(seed=3, laps_between_stops=4), [0]

    def step():
        tick[0] += 1
        for m, struct in zip(maps, encode(driver.step(SPEEDUP / 60), tick[0])):
            _write(m, bytes(struct))

    step()
    adapter = ACCAdapter()
    assert adapter.connect()
    session, frame = adapter.read()
    assert session.game == "acc" and session.track == "synthetic_ring" and frame.tick == tick[0]
    adapter.close()

    w = Writer(step)
    w.start()
    try:
        storage = _collect("acc", tmp_path, 45)
    finally:
        w.stop.set()
        w.join()
        for m in maps:
            m.close()
    laps = storage.list_laps("acc", "synthetic_ring", "ferrari_296_gt3")
    assert len(laps) >= 3, laps
    assert any(l["is_clean"] for l in laps)


def test_lmu_reads_real_mapping_and_cuts_laps(tmp_path):
    (m,) = _maps([(LMU_MAP, ctypes.sizeof(LMUObjectOut))])
    fake, driver = FakeLMU(scoring_every=10), SyntheticDriver(seed=3, laps_between_stops=4)

    def step():
        dt = SPEEDUP / 60
        fake.write(driver.step(dt), dt)
        _write(m, bytes(fake.page.buf))

    step()
    adapter = LMUAdapter()
    assert adapter.connect()
    res = adapter.read()
    assert res is not None and res[0].game == "lmu" and res[0].track_length_m == 4200.0
    adapter.close()

    w = Writer(step)
    w.start()
    try:
        storage = _collect("lmu", tmp_path, 45)
    finally:
        w.stop.set()
        w.join()
        m.close()
    combos = storage.list_combos()
    assert combos and combos[0]["game"] == "lmu" and combos[0]["laps"] >= 3, combos
    assert combos[0]["clean_laps"] >= 1
