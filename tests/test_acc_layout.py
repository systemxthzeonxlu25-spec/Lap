"""把共享内存结构体的大小和关键偏移钉死：改结构体时一旦错位，这里立刻失败。

ACC 的期望值按官方文档 v1.8.12 手算，并与 6 个以上独立实现核对过（见 PROGRESS.md）；
AC1 的期望值来自 live-telemetry 的 sim_info.py 和 mdjarv 的 C# 实现（见 ac_structs.py 文件头）。
"""
import ctypes

import pytest

from simtelemetry.adapters.ac_structs import ACGraphics, ACPhysics, ACStatic
from simtelemetry.adapters.acc_structs import SPageFileGraphic, SPageFilePhysics, SPageFileStatic


@pytest.mark.parametrize("struct, size", [
    (SPageFilePhysics, 800), (SPageFileGraphic, 1588), (SPageFileStatic, 820),
    (ACPhysics, 580), (ACGraphics, 296), (ACStatic, 684),
])
def test_struct_sizes(struct, size):
    assert ctypes.sizeof(struct) == size


@pytest.mark.parametrize("struct, field, offset", [
    # ACC physics
    (SPageFilePhysics, "packetId", 0), (SPageFilePhysics, "gas", 4), (SPageFilePhysics, "brake", 8),
    (SPageFilePhysics, "gear", 16), (SPageFilePhysics, "rpms", 20), (SPageFilePhysics, "steerAngle", 24),
    (SPageFilePhysics, "speedKmh", 28), (SPageFilePhysics, "numberOfTyresOut", 244),
    (SPageFilePhysics, "localVelocity", 568),
    # ACC graphics
    (SPageFileGraphic, "status", 4), (SPageFileGraphic, "session", 8),
    (SPageFileGraphic, "completedLaps", 132), (SPageFileGraphic, "iCurrentTime", 140),
    (SPageFileGraphic, "iLastTime", 144), (SPageFileGraphic, "isInPit", 160),
    (SPageFileGraphic, "normalizedCarPosition", 248), (SPageFileGraphic, "isInPitLane", 1236),
    (SPageFileGraphic, "isValidLap", 1408),
    # ACC static
    (SPageFileStatic, "carModel", 68), (SPageFileStatic, "track", 134),
    (SPageFileStatic, "trackSPlineLength", 520), (SPageFileStatic, "trackConfiguration", 524),
    # AC1：physics / static 与 ACC 前缀相同，graphics 在 normalizedCarPosition 之后分叉
    (ACPhysics, "numberOfTyresOut", 244), (ACPhysics, "speedKmh", 28),
    (ACGraphics, "completedLaps", 132), (ACGraphics, "iCurrentTime", 140), (ACGraphics, "iLastTime", 144),
    (ACGraphics, "isInPit", 160), (ACGraphics, "normalizedCarPosition", 248),
    (ACGraphics, "carCoordinates", 252), (ACGraphics, "isInPitLane", 276),
    (ACStatic, "carModel", 68), (ACStatic, "track", 134),
    (ACStatic, "trackSPlineLength", 520), (ACStatic, "trackConfiguration", 524),
])
def test_field_offsets(struct, field, offset):
    assert getattr(struct, field).offset == offset


def test_ac1_prefix_matches_acc():
    for ac, acc in ((ACPhysics, SPageFilePhysics), (ACStatic, SPageFileStatic)):
        for name, _ in ac._fields_:
            assert getattr(ac, name).offset == getattr(acc, name).offset, name
            assert getattr(ac, name).size == getattr(acc, name).size, name
