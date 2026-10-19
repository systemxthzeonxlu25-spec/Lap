from simtelemetry.analysis import LiveReference, distance_profile, interp, make_grid, resample_lap
from simtelemetry.recorder import unwrap_positions


def test_distance_profile_is_monotonic_and_pinned():
    pos = [0.001, 0.2, 0.19, 0.5, 0.5, 0.8, 0.999]       # 0.19 = 倒车/噪声，0.5 重复
    t = [10, 2000, 2100, 5000, 5100, 8000, 9990]
    d, tt = distance_profile(pos, t, 10_000)
    assert d == sorted(d) and len(set(d)) == len(d)
    assert d[0] == 0.0 and tt[0] == 0.0
    assert d[-1] == 1.0 and tt[-1] == 10_000
    assert interp(d, tt, 0.5) == 5000                      # 取第一次到达 0.5 的时间


def test_unwrap_positions_handles_line_crossing_lag():
    raw = [0.998, 0.999] + [i / 100 for i in range(1, 100)] + [0.0005, 0.001]
    out = unwrap_positions(raw)
    assert out[0] < 0 and out[1] < 0
    assert out[-1] > 1 and out[-2] > 1
    assert out[2:-2] == raw[2:-2]


def test_unwrap_leaves_partial_out_lap_alone():
    raw = [0.9 + i * 0.0009 for i in range(100)]            # 维修区出发，只跑了线前一小段
    assert unwrap_positions(raw) == raw


def test_resample_gear_is_step_and_lengths_match():
    samples = {
        "pos": [0.0, 0.25, 0.5, 0.75, 0.99],
        "t_ms": [0, 1000, 2000, 3000, 3960],
        "speed": [100, 200, 150, 220, 210],
        "throttle": [1, 1, 0, 1, 1], "brake": [0, 0, 1, 0, 0], "steer": [0, 0.1, -0.2, 0, 0],
        "gear": [3, 4, 3, 5, 5], "rpm": [7000, 8000, 6000, 8500, 8000],
    }
    grid = make_grid(101)
    r = resample_lap(samples, 4000, grid)
    assert all(len(v) == 101 for v in r.values())
    assert r["gear"][49] == 4 and r["gear"][50] == 3       # 0.49 → 前一个采样的挡位
    assert r["speed"][25] == 200 and r["t"][-1] == 4000


def test_live_reference_wraps_near_line():
    lap = {"id": 1, "lap_time_ms": 100_000}
    ref = LiveReference(lap, {"pos": [i / 1000 for i in range(1000)],
                              "t_ms": [i * 100 for i in range(1000)]})
    assert abs(ref.delta_ms(0.5, 51_000) - 1000) < 1e-6
    assert ref.delta_ms(0.995, 300) == 300                  # 计时已归零、位置未归零 → 当作 0
    assert ref.delta_ms(0.002, 100_500) == 500              # 位置已归零、计时未归零 → 当作终点
