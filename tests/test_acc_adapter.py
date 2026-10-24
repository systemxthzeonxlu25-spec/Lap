"""ACC 适配器：用 tools/fake_acc.encode() 生成的结构体代替真正的共享内存。"""
from __future__ import annotations

from simtelemetry.adapters import create_adapter, is_implemented
from simtelemetry.adapters.acc import ACCAdapter, translate
from simtelemetry.adapters.acc_structs import ACC_LIVE, ACC_OFF, ACC_PAUSE
from simtelemetry.adapters.simulated import SyntheticDriver
from simtelemetry.recorder import LapRecorder
from tools.fake_acc import CAR, TRACK, encode


class FakePage:
    def __init__(self, ok: bool = True):
        self.ok, self.struct, self.opened = ok, None, False

    def open(self) -> bool:
        self.opened = self.ok
        return self.ok

    def read(self):
        return self.struct

    def close(self) -> None:
        self.opened = False


def state(**kw):
    s = SyntheticDriver(seed=1).step(0.02)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_translate_maps_units_and_encodings():
    s = state(gear=3, speed_kmh=180.5, last_lap_ms=0, in_pit=False, in_pit_lane=True, lap_valid=False)
    session, f = translate(*encode(s, tick=42, session=1))
    assert session.game == "acc" and session.track == TRACK and session.car == CAR
    assert session.session_type == "qualify" and session.track_length_m is None
    assert f.gear == 3 and abs(f.speed_kmh - 180.5) < 1e-3 and f.tick == 42
    assert f.last_lap_ms is None                 # INT32_MAX → 没有上一圈
    assert f.in_pit and not f.lap_valid and f.is_live


def test_translate_reverse_neutral_and_states():
    _, f = translate(*encode(state(gear=-1), 1))
    assert f.gear == -1
    _, f = translate(*encode(state(gear=0), 1, status=ACC_PAUSE))
    assert f.gear == 0 and not f.is_live
    assert translate(*encode(state(), 1, status=ACC_OFF)) is None
    session, _ = translate(*encode(state(), 1, track=""))
    assert session is None                       # 赛道还没加载：不记录


def test_connect_is_all_or_nothing():
    pages = [FakePage(), FakePage(ok=False), FakePage()]
    a = ACCAdapter(*pages)
    assert not a.connect()
    assert not any(p.opened for p in pages)


def test_full_pipeline_through_adapter():
    pages = [FakePage(), FakePage(), FakePage()]
    adapter = ACCAdapter(*pages)
    assert adapter.connect()
    drv, rec, laps = SyntheticDriver(seed=3, laps_between_stops=4), LapRecorder(), []
    wall, dt = 0.0, 1 / 60
    for tick in range(int(60 * 60 * 9)):          # 9 分钟，60 Hz
        for page, struct in zip(pages, encode(drv.step(dt), tick)):
            page.struct = struct
        wall += dt
        session, frame = adapter.read()
        laps += rec.feed(session, frame, wall)
    laps += rec.flush(wall + 10)
    assert len(laps) >= 4
    assert laps[0].is_out_lap
    clean = [l for l in laps if l.is_clean]
    assert clean and all(l.time_source == "game" for l in clean)
    assert all(l.session.track == TRACK for l in laps)
    # ACC 不给赛道长度：完整圈按速度积分估算，合成赛道实际 4200 m
    for l in clean:
        assert l.track_length_m is not None and abs(l.track_length_m - 4200) < 4200 * 0.01


def test_registered():
    assert is_implemented("acc")
    assert isinstance(create_adapter("acc"), ACCAdapter)
