"""把 ACC 共享内存结构体的大小和关键偏移固定下来（数值来自官方文档 v1.8.12，见 acc_structs.py）。

改结构体时如果这里挂了，说明后面的字段整体错位了——游戏里会读到一堆乱码数字。
"""
from __future__ import annotations

import ctypes

import pytest

from simtelemetry.adapters.acc_structs import (SPageFileGraphic, SPageFilePhysics, SPageFileStatic,
                                               wchar_str)


@pytest.mark.parametrize("struct, size", [
    (SPageFilePhysics, 800), (SPageFileGraphic, 1588), (SPageFileStatic, 820),
])
def test_struct_sizes(struct, size):
    assert ctypes.sizeof(struct) == size


@pytest.mark.parametrize("struct, field, offset", [
    (SPageFileGraphic, "completedLaps", 132),
    (SPageFileGraphic, "iCurrentTime", 140),
    (SPageFileGraphic, "normalizedCarPosition", 248),
    (SPageFileGraphic, "isInPitLane", 1236),
    (SPageFileGraphic, "isValidLap", 1408),
    (SPageFileStatic, "carModel", 68),
    (SPageFileStatic, "track", 134),
])
def test_field_offsets(struct, field, offset):
    assert getattr(struct, field).offset == offset


def test_wchar_str_stops_at_nul():
    s = SPageFileStatic()
    raw = "monza".encode("utf-16-le")
    ctypes.memmove(ctypes.addressof(s) + SPageFileStatic.track.offset, raw, len(raw))
    assert wchar_str(s.track) == "monza"
