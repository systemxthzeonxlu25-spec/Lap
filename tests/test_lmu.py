"""LMU：结构体布局、字段映射、合理性检查、低频计分下的整条链路（tools/fake_lmu 充当共享内存）。"""
from __future__ import annotations

import ctypes

import pytest

from simtelemetry.adapters import create_adapter, is_implemented
from simtelemetry.adapters.lmu import (OFF_SCORING_INFO, OFF_TELEM_INFO, OFF_TELEMETRY, LMUAdapter, sanity_problem,
                                       session_type, translate)
from simtelemetry.adapters.lmu_structs import (LMUObjectOut, LMUScoringData, LMUScoringInfo, LMUVehicleScoring,
                                               LMUVehicleTelemetry)
from simtelemetry.adapters.simulated import SyntheticDriver
from simtelemetry.recorder import LapRecorder
from tools.fake_lmu import CAR, TRACK, BufferPage, FakeLMU

# 数值来自 pyLMUSharedMemory 的 lmu_data.py（ctypes 计算），KoDriver issue #1421 用同一来源得到相同的 scoringStream 偏移
SIZES = {LMUVehicleTelemetry: 1888, LMUVehicleScoring: 584, LMUScoringInfo: 548, LMUScoringData: 126832,
         LMUObjectOut: 324820}


@pytest.mark.parametrize("struct, size", SIZES.items(), ids=lambda x: getattr(x, "__name__", str(x)))
def test_struct_sizes(struct, size):
    assert ctypes.sizeof(struct) == size


def test_key_offsets():
    assert (LMUObjectOut.scoring.offset, LMUObjectOut.telemetry.offset) == (1632, 128464)
    assert LMUObjectOut.scoring.offset + LMUScoringData.scoringStream.offset == 62928
    assert OFF_SCORING_INFO == 1632 and OFF_TELEMETRY == 128464 and OFF_TELEM_INFO == 128468
    T, S = LMUVehicleTelemetry, LMUVehicleScoring
    assert (T.mElapsedTime.offset, T.mLapStartET.offset, T.mLocalVel.offset, T.mGear.offset) == (12, 24, 184, 352)
    assert (T.mUnfilteredThrottle.offset, T.mLapInvalidated.offset, T.mVehicleModel.offset) == (388, 745, 796)
    assert (S.mTotalLaps.offset, S.mLapDist.offset, S.mLastLapTime.offset, S.mInPits.offset) == (100, 104, 168, 198)


def step_states(n, seed=1):
    drv = SyntheticDriver(seed=seed)
    return [drv.step(1 / 60) for _ in range(n)]


def snapshot(fake: FakeLMU):
    p = fake.page
    return (p.read_at(LMUScoringInfo, OFF_SCORING_INFO), p.read_at(LMUVehicleScoring, 1632 + 560),
            p.read_at(LMUVehicleTelemetry, OFF_TELEM_INFO))


def test_translate_mapping():
    fake = FakeLMU(scoring_every=1, session=6)
    s = step_states(200)[-1]
    s.gear, s.speed_kmh, s.lap_valid = 3, 180.0, False
    fake.write(s, 1 / 60)
    session, f = translate(*snapshot(fake))
    assert session.game == "lmu" and session.track == TRACK and session.car == CAR
    assert session.track_length_m == 4200.0 and session.session_type == "qualify"
    assert f.gear == 3 and abs(f.speed_kmh - 180) < 1e-6 and not f.lap_valid and f.is_live
    assert f.tick == round(fake.et * 1000) and f.last_lap_ms is None


def test_not_live_when_paused_or_ai_driving():
    fake = FakeLMU(scoring_every=1)
    fake.paused = True
    fake.write(step_states(1)[0], 1 / 60)
    assert not translate(*snapshot(fake))[1].is_live
    fake.paused = False
    fake.write(step_states(1)[0], 1 / 60)
    info, veh, tel = snapshot(fake)
    veh.mControl = 1
    assert not translate(info, veh, tel)[1].is_live


def test_sanity_check_rejects_garbage():
    fake = FakeLMU(scoring_every=1)
    fake.write(step_states(1)[0], 1 / 60)
    info, _, tel = snapshot(fake)
    assert sanity_problem(info, tel) is None
    info.mLapDist = 0
    assert "赛道长度" in sanity_problem(info, tel)
    info.mLapDist, tel.mTrackName = 4200.0, b"Somewhere Else"
    assert "赛道名" in sanity_problem(info, tel)
    page = BufferPage(bytes([0xFF]) * ctypes.sizeof(LMUObjectOut))   # 布局错了大概就是这种乱码
    a = LMUAdapter(page)
    assert a.connect() and a.read() is None


def test_menu_returns_none():
    a = LMUAdapter(BufferPage())
    assert a.connect() and a.read() is None


def test_full_pipeline_with_slow_scoring():
    fake = FakeLMU(scoring_every=10)            # 计分 6 Hz，遥测 60 Hz
    adapter = LMUAdapter(fake.page)
    assert adapter.connect()
    drv, rec, laps, wall = SyntheticDriver(seed=3, laps_between_stops=4), LapRecorder(), [], 0.0
    for _ in range(int(60 * 60 * 9)):
        fake.write(drv.step(1 / 60), 1 / 60)
        wall += 1 / 60
        res = adapter.read()
        if res:
            laps += rec.feed(*res, wall)
    laps += rec.flush(wall + 10)
    clean = [l for l in laps if l.is_clean]
    assert len(laps) >= 4 and laps[0].is_out_lap and clean
    for lap in clean:
        assert lap.time_source == "game" and lap.track_length_m == 4200.0
        pos = lap.samples["pos"]
        assert pos[0] <= 0.03 and pos[-1] >= 0.97
        assert all(b >= a - 0.01 for a, b in zip(pos, pos[1:]))     # 外推的位置大致单调


def test_registered():
    assert session_type(11) == "race" and session_type(99) == "unknown"
    assert is_implemented("lmu") and isinstance(create_adapter("lmu"), LMUAdapter)
