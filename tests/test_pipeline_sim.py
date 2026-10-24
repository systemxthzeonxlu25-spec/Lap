"""合成车手 → 切圈 → 存库 → 对比，全流程（不依赖游戏、不依赖 Windows）。"""
from __future__ import annotations

from simdrive import SESSION, drive

from simtelemetry.analysis import LiveReference, compare_laps
from simtelemetry.models import TelemetryFrame
from simtelemetry.recorder import LapRecorder
from simtelemetry.storage import Storage


def test_laps_are_split_and_flagged(tmp_path):
    laps = drive(60 * 14)
    assert len(laps) >= 6
    first = laps[0]
    assert first.is_out_lap and not first.is_clean           # 从维修区出发
    assert any(l.is_in_lap for l in laps)                     # 跑了 4 圈后进站
    clean = [l for l in laps if l.is_clean]
    assert clean, "至少应有一圈干净圈"
    for l in clean:
        assert l.time_source == "game"
        assert 60_000 < l.lap_time_ms < 200_000
        pos = l.samples["pos"]
        assert pos[0] <= 0.03 and pos[-1] >= 0.97
        # 游戏圈时与最后一个采样点的本圈计时只差一个采样间隔左右
        assert 0 <= l.lap_time_ms - l.samples["t_ms"][-1] <= 60

    db = Storage(tmp_path / "t.db")
    ids = [db.save_lap(l) for l in laps]
    combos = db.list_combos()
    assert combos[0]["laps"] == len(laps)
    best = db.best_lap(*SESSION.combo)
    assert best["lap_time_ms"] == min(l.lap_time_ms for l in clean)

    other = next(i for i in ids if i != best["id"])
    res = compare_laps(best, db.get_samples(best["id"]), db.get_lap(other), db.get_samples(other), 500)
    assert len(res["x"]) == 500 and len(res["delta"]) == 500
    assert res["delta"][0] == 0
    expect = (db.get_lap(other)["lap_time_ms"] - best["lap_time_ms"]) / 1000
    assert abs(res["delta"][-1] - expect) < 1e-6

    ref = LiveReference(best, db.get_samples(best["id"]))
    assert ref.delta_ms(1.0, best["lap_time_ms"]) == 0
    assert ref.delta_ms(0.99, 500) == 500 - ref.time_at(0.0)    # 计时已归零但位置还是 0.99


def test_duplicate_ticks_and_pause_are_ignored():
    rec = LapRecorder()
    f = TelemetryFrame(100, 1, 0, 0, 3, 7000, 0, 1000, 0.1, False, True, 0, True, tick=1)
    rec.feed(SESSION, f, 0.0)
    rec.feed(SESSION, f, 0.02)                                      # 同一 tick
    paused = TelemetryFrame(100, 1, 0, 0, 3, 7000, 0, 1020, 0.1, False, True, 0, False, tick=2)
    rec.feed(SESSION, paused, 0.04)
    assert len(rec._buf) == 1


def test_lap_counter_reset_discards_partial_lap():
    rec = LapRecorder()
    for i in range(200):
        rec.feed(SESSION, TelemetryFrame(100, 1, 0, 0, 3, 7000, 3, i * 20, i / 1000, False, True, 0,
                                         True, tick=i), i * 0.02)
    out = rec.feed(SESSION, TelemetryFrame(0, 0, 0, 0, 0, 900, 0, 0, 0.9, True, True, 0, True,
                                           tick=999), 5.0)
    assert out == [] and rec.current_lap_number == 1
