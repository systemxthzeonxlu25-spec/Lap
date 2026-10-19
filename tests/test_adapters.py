"""游戏适配器的数据转换（不需要 Windows / 游戏：共享内存页和 pyirsdk 都用假的代替）。"""
from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from fake_acc import CAR_ID, TRACK_ID, FakeACC, put_wstr  # noqa: E402

from simtelemetry.adapters import AdapterDisconnected  # noqa: E402
from simtelemetry.adapters.ac import ACAdapter  # noqa: E402
from simtelemetry.adapters.ac_structs import AC_LIVE, AC_PAUSE, ACGraphics, ACPhysics, ACStatic  # noqa: E402
from simtelemetry.adapters.acc import ACCAdapter  # noqa: E402
from simtelemetry.adapters.acc_structs import (GRAPHICS_MAP, PHYSICS_MAP, STATIC_MAP,  # noqa: E402
                                               SPageFileGraphic, SPageFilePhysics, SPageFileStatic)
from simtelemetry.adapters.iracing import IRacingAdapter, parse_track_length_m  # noqa: E402
from simtelemetry.collector import Collector  # noqa: E402
from simtelemetry.live import LiveHub  # noqa: E402
from simtelemetry.recorder import LapRecorder  # noqa: E402
from simtelemetry.storage import Storage  # noqa: E402


class FakeShm:
    """假的命名共享内存：name → 当前结构体。"""

    def __init__(self):
        self.pages: dict[str, ctypes.Structure] = {}

    def factory(self, name, struct_type):
        shm = self

        class Page:
            is_open = False

            def open(self):
                self.is_open = name in shm.pages
                return self.is_open

            def read(self):
                return struct_type.from_buffer_copy(bytes(shm.pages[name])[: ctypes.sizeof(struct_type)])

            def close(self):
                self.is_open = False

        return Page()


def test_acc_adapter_end_to_end_with_line_lag():
    shm = FakeShm()
    adapter = ACCAdapter(check_process=False, page_factory=shm.factory)
    assert not adapter.connect()                          # 游戏没开：映射不存在

    fake = FakeACC(seed=3, laps_between_stops=3, line_lag=3)
    shm.pages[PHYSICS_MAP], shm.pages[GRAPHICS_MAP], shm.pages[STATIC_MAP] = fake.step(0.02)
    assert adapter.connect()

    rec = LapRecorder()
    laps, gears = [], set()
    wall = 0.0
    for _ in range(int(60 * 9 / 0.02)):
        p, g, s = fake.step(0.02)
        shm.pages[PHYSICS_MAP], shm.pages[GRAPHICS_MAP] = p, g
        wall += 0.02
        session, frame = adapter.read()
        assert session.game == "acc" and session.track == TRACK_ID and session.car == CAR_ID
        assert session.track_length_m is None and session.session_type == "practice"
        if g.iLastTime == 2**31 - 1:
            assert frame.last_lap_ms is None
        gears.add(frame.gear)
        laps += rec.feed(session, frame, wall)
    laps += rec.flush(wall + 10)
    adapter.close()

    assert 0 in gears and 1 in gears and -1 not in gears   # N 和 1 挡（ACC 原始值 1 / 2）
    assert len(laps) >= 4
    clean = [lap for lap in laps if lap.is_clean]
    assert clean
    for lap in clean:
        assert lap.time_source == "game"                  # 圈时来自 iLastTime，不是估算
        assert lap.samples["pos"][0] <= 0.03 and lap.samples["pos"][-1] >= 0.97
    assert laps[0].is_out_lap


def test_acc_convert_menu_and_pause():
    a = ACCAdapter(check_process=False, page_factory=FakeShm().factory)
    p, g, s = SPageFilePhysics(), SPageFileGraphic(), SPageFileStatic()
    g.status = 0                                          # ACC_OFF：菜单
    session, frame = a.convert(p, g, s)
    assert session is None and not frame.is_live
    put_wstr(s.track, "monza")
    put_wstr(s.carModel, "porsche_991ii_gt3_r")
    g.status, g.session, p.gear, g.iLastTime = 3, 1, 0, 0  # 暂停、排位、倒挡、还没有圈时
    session, frame = a.convert(p, g, s)
    assert session.track == "monza" and session.session_type == "qualify"
    assert not frame.is_live and frame.gear == -1 and frame.last_lap_ms is None


def _ac_pages(tyres_out=0, layout="endurance"):
    p, g, s = ACPhysics(), ACGraphics(), ACStatic()
    p.packetId, p.speedKmh, p.gear, p.rpms, p.gas, p.steerAngle = 42, 180.5, 5, 7200, 1.2, -0.3
    p.numberOfTyresOut = tyres_out
    g.status, g.session = AC_LIVE, 0
    g.completedLaps, g.iCurrentTime, g.iLastTime, g.normalizedCarPosition = 3, 45_000, 418_250, 0.25
    put_wstr(s.track, "ks_nordschleife")
    put_wstr(s.trackConfiguration, layout)
    put_wstr(s.carModel, "ks_porsche_911_gt3_r_2016")
    s.trackSPlineLength = 25_378.0
    return p, g, s


def test_ac_convert():
    a = ACAdapter(check_process=False, page_factory=FakeShm().factory)
    session, frame = a.convert(*_ac_pages())
    assert session.game == "ac" and session.track == "ks_nordschleife [endurance]"
    assert session.track_length_m == 25_378.0
    assert frame.gear == 4 and frame.throttle == 1.0 and frame.steer == pytest.approx(-0.3)
    assert frame.lap_valid and frame.is_live and frame.tick == 42 and frame.last_lap_ms == 418_250
    assert not a.convert(*_ac_pages(tyres_out=3))[1].lap_valid      # 3 个轮胎出界 = 切弯
    assert a.convert(*_ac_pages(tyres_out=2))[1].lap_valid
    assert a.convert(*_ac_pages(layout=""))[0].track == "ks_nordschleife"
    p, g, s = _ac_pages()
    g.status = AC_PAUSE
    assert not a.convert(p, g, s)[1].is_live


class FakeIRSDK:
    def __init__(self, values: dict, yaml: dict):
        self.values, self.yaml = values, yaml
        self.is_initialized = self.is_connected = False
        self.session_info_update = 1
        self.frozen = 0

    def startup(self):
        print("Failed to connect to sim: 这行应该被吞掉")
        self.is_initialized = self.is_connected = True
        return True

    def shutdown(self):
        self.is_initialized = self.is_connected = False

    def freeze_var_buffer_latest(self):
        self.frozen += 1

    def unfreeze_var_buffer_latest(self):
        pass

    def __getitem__(self, key):
        return self.values[key] if key in self.values else self.yaml.get(key)


IR_YAML = {
    "WeekendInfo": {"TrackName": "spa up", "TrackDisplayName": "Circuit de Spa-Francorchamps",
                    "TrackConfigName": "Grand Prix Pits", "TrackLength": "6.93 km"},
    "DriverInfo": {"DriverCarIdx": 7, "Drivers": [
        {"CarIdx": 0, "CarScreenName": "Pace Car"},
        {"CarIdx": 7, "CarScreenName": "Porsche 911 GT3 R (992)", "CarPath": "porsche992rgt3"}]},
    "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": "Practice"},
                                 {"SessionNum": 1, "SessionType": "Lone Qualify"}]},
}
IR_VALUES = {
    "Speed": 50.0, "Throttle": 0.8, "Brake": 0.0, "SteeringWheelAngle": 1.2, "SteeringWheelAngleMax": 4.8,
    "Gear": 4, "RPM": 7500.4, "LapCompleted": 2, "LapCurrentLapTime": 61.2346, "LapDistPct": 0.41,
    "LapLastLapTime": 137.891, "OnPitRoad": False, "PlayerCarInPitStall": False, "PlayerTrackSurface": 3,
    "IsOnTrack": True, "IsReplayPlaying": False, "SessionTick": 12345, "SessionNum": 1,
}


def test_iracing_adapter(capsys):
    ir = FakeIRSDK(dict(IR_VALUES), IR_YAML)
    a = IRacingAdapter(check_process=False, sdk_factory=lambda: ir)
    assert a.connect()
    assert capsys.readouterr().out == ""                  # pyirsdk 的 print 被吞掉
    session, f = a.read()
    assert session.track == "Circuit de Spa-Francorchamps [Grand Prix Pits]"
    assert session.car == "Porsche 911 GT3 R (992)" and session.session_type == "lone qualify"
    assert session.track_length_m == pytest.approx(6930)
    assert f.speed_kmh == pytest.approx(180) and f.steer == pytest.approx(0.25) and f.gear == 4
    assert f.lap_time_ms == 61_235 and f.last_lap_ms == 137_891 and f.tick == 12345
    assert f.lap_valid and f.is_live and not f.in_pit and ir.frozen == 1

    ir.values.update(PlayerTrackSurface=0, OnPitRoad=True, LapLastLapTime=-1.0, IsReplayPlaying=True)
    _, f = a.read()
    assert not f.lap_valid and f.in_pit and f.last_lap_ms is None and not f.is_live

    ir.is_connected = False                               # 游戏退出
    with pytest.raises(AdapterDisconnected):
        a.read()
    a.close()


def test_parse_track_length():
    assert parse_track_length_m("5.79 km") == pytest.approx(5790)
    assert parse_track_length_m("2.50 mi") == pytest.approx(4023.36)
    assert parse_track_length_m(None) is None and parse_track_length_m("") is None


def test_collector_survives_adapter_creation_errors(tmp_path, monkeypatch, caplog):
    import simtelemetry.collector as col_mod

    def boom(game, **kw):
        raise ImportError("No module named 'irsdk'")

    monkeypatch.setattr(col_mod, "create_adapter", boom)
    col = Collector(Storage(tmp_path / "c.db"), LiveHub(), game="iracing")
    with caplog.at_level(logging.ERROR):
        assert col._try_connect() is None
        assert col._try_connect() is None
    assert sum("无法创建 iracing 适配器" in r.message for r in caplog.records) == 1   # 只报一次
