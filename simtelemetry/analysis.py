"""按“赛道距离”（归一化位置）对齐两圈，并计算 delta。

核心是把一圈表示成 t(d)：第一次到达距离 d 时的本圈用时。
- 位置先截到 [0, 1]，再做“累计最大值”，让它单调不减（打滑倒车、噪声不会让距离倒退）；
- 首尾补上 (0, 0) 和 (1, 圈时)，保证 delta 在终点正好等于两圈圈时差；
- delta(d) = t_对比(d) - t_参考(d)，正数 = 对比圈更慢。
"""
from __future__ import annotations

from bisect import bisect_right

# 线性插值的通道；gear 是离散值，取“该距离之前最后一个采样”的挡位
LINEAR_CHANNELS = ("speed", "throttle", "brake", "steer", "rpm")
STEP_CHANNELS = ("gear",)


def distance_profile(pos: list[float], t_ms: list[int], lap_time_ms: int) -> tuple[list[float], list[float]]:
    """返回严格递增的 (d, t) 序列，用于 t(d) 插值。"""
    d_out: list[float] = [0.0]
    t_out: list[float] = [0.0]
    run_max = 0.0
    for p, t in zip(pos, t_ms):
        p = min(1.0, max(0.0, p))
        if p <= run_max:
            continue                  # 没有前进：保留第一次到达该距离的时间
        if t < t_out[-1]:
            continue                  # 计时异常（不应出现），丢弃
        run_max = p
        d_out.append(p)
        t_out.append(min(float(t), float(lap_time_ms)))
    if d_out[-1] < 1.0:
        d_out.append(1.0)
        t_out.append(float(lap_time_ms))
    else:
        t_out[-1] = float(lap_time_ms)
    return d_out, t_out


def interp(xs: list[float], ys: list[float], x: float) -> float:
    """xs 递增。范围外取端点值。"""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x)
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _monotonic_channel(pos: list[float], values: list) -> tuple[list[float], list]:
    """按位置排序并去掉位置重复/倒退的点，供通道插值。"""
    xs: list[float] = []
    ys: list = []
    run_max = -1.0
    for p, v in zip(pos, values):
        p = min(1.0, max(0.0, p))
        if p <= run_max:
            continue
        run_max = p
        xs.append(p)
        ys.append(v)
    return xs, ys


def resample_lap(samples: dict[str, list], lap_time_ms: int, grid: list[float]) -> dict[str, list]:
    pos = samples["pos"]
    out: dict[str, list] = {}
    if not pos:
        for c in LINEAR_CHANNELS + STEP_CHANNELS + ("t",):
            out[c] = [None] * len(grid)
        return out

    d, t = distance_profile(pos, samples["t_ms"], lap_time_ms)
    out["t"] = [interp(d, t, x) for x in grid]

    for c in LINEAR_CHANNELS:
        xs, ys = _monotonic_channel(pos, samples[c])
        out[c] = [round(interp(xs, ys, x), 4) for x in grid] if xs else [None] * len(grid)

    for c in STEP_CHANNELS:
        xs, ys = _monotonic_channel(pos, samples[c])
        vals = []
        for x in grid:
            i = bisect_right(xs, x) - 1
            vals.append(ys[max(i, 0)] if xs else None)
        out[c] = vals
    return out


def make_grid(points: int) -> list[float]:
    points = max(50, min(int(points), 10000))
    return [i / (points - 1) for i in range(points)]


def compare_laps(ref: dict, ref_samples: dict, cmp: dict, cmp_samples: dict, points: int = 1500) -> dict:
    """ref/cmp 是 laps 表的行（dict），*_samples 是列式采样点。"""
    grid = make_grid(points)
    r = resample_lap(ref_samples, ref["lap_time_ms"], grid)
    c = resample_lap(cmp_samples, cmp["lap_time_ms"], grid)
    delta = [round((ct - rt) / 1000.0, 4) if ct is not None and rt is not None else None
             for ct, rt in zip(c["t"], r["t"])]
    length = ref.get("track_length_m") or cmp.get("track_length_m")
    return {
        "x": grid,
        "track_length_m": length,
        "ref": {"lap": ref, **{k: v for k, v in r.items() if k != "t"}},
        "cmp": {"lap": cmp, **{k: v for k, v in c.items() if k != "t"}},
        "delta": delta,
    }


class LiveReference:
    """实时 delta 用的参考圈：O(log n) 查询“到达当前位置时参考圈用了多久”。"""

    def __init__(self, lap: dict, samples: dict[str, list]):
        self.lap = lap
        self.lap_id = lap["id"]
        self.lap_time_ms = lap["lap_time_ms"]
        self._d, self._t = distance_profile(samples["pos"], samples["t_ms"], self.lap_time_ms)

    def time_at(self, pos: float) -> float:
        return interp(self._d, self._t, min(1.0, max(0.0, pos)))

    def delta_ms(self, pos: float, lap_time_ms: int) -> float:
        # 起终点线附近，位置和计时不一定同一帧归零：按计时判断车在线的哪一侧
        half = self.lap_time_ms / 2
        if pos > 0.95 and lap_time_ms < half:
            pos = 0.0
        elif pos < 0.05 and lap_time_ms > half:
            pos = 1.0
        return lap_time_ms - self.time_at(pos)
