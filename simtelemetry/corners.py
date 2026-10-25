"""按弯道分段：找弯、逐弯对比、理论最快圈（纯函数，不读写数据库）。

分段规则（都在归一化位置 0..1 的网格上）：
- 弯道从参考圈的速度曲线里找：速度的局部极小值，而且比之前的最高速度低了足够多
  （≥ 15 km/h，或者低速段 ≥ 8%，至少 8 km/h），并且出弯后速度要回升同样多才算下一个弯；
  两个弯心离得太近（< 60 m，未知赛道长度时 < 1.5%）就合并成一个，取更慢的那个弯心；
- 弯道段起点 = 刹车点：弯前最高速度之后、速度开始明显下降（比最高速度低 2 km/h）的位置；
- 弯道段终点 = 弯心到下一个弯刹车点的中点，但不超过两弯之间的最高速度点；最后一个弯不超过终点线；
- 弯与弯之间（以及起点到 T1、最后一个弯到终点）是直道段。所有段首尾相接覆盖整圈，
  所以各段用时加起来正好等于圈时，各段用时差加起来正好等于终点 delta。
- 每段用时 = t(终点) − t(起点)，t(d) 来自 analysis.distance_profile（每圈都按它自己的归一化位置对齐），
  边界时间先取整到毫秒再相减，求和时不会有舍入误差累积。
- 只看速度：全油门、速度不降的弯（合成赛道 0.36 处那个 210 km/h 的弯就是）不算弯，归进直道段；
- 跨起终点线的弯会被切成两半（起点前一段算最后一个弯，起点后一段算 T1），对总和没有影响。
"""
from __future__ import annotations

from .analysis import distance_profile, interp, make_grid, resample_lap

MIN_DROP_KMH = 15.0        # 速度下降这么多一定算弯
MIN_DROP_FRAC = 0.08       # 低速段：下降 8% 也算
MIN_DROP_FLOOR_KMH = 8.0   # 但至少 8 km/h，免得噪声被当成弯
START_TOL_KMH = 2.0        # 刹车点：比弯前最高速度低这么多的位置
MERGE_M = 60.0             # 弯心离得比这近就合并
MERGE_FRAC = 0.015         # 未知赛道长度时按圈长比例
BRAKE_ON = 0.1             # 刹车 > 10% 算踩下
BRAKE_GAP_M = 30.0         # 松开刹车不到这么远又踩下，算同一次刹车
BRAKE_GAP_FRAC = 0.007


def _need(v: float) -> float:
    return max(MIN_DROP_FLOOR_KMH, min(MIN_DROP_KMH, MIN_DROP_FRAC * v))


def _apexes(v: list[float]) -> list[int]:
    """带回差地找速度谷底，返回网格下标。"""
    out: list[int] = []
    peak, valley = 0, None
    for i, s in enumerate(v):
        if valley is None:
            if s > v[peak]:
                peak = i
            elif v[peak] - s >= _need(v[peak]):
                valley = i
        else:
            if s < v[valley]:
                valley = i
            elif s - v[valley] >= _need(v[valley]):
                out.append(valley)
                peak, valley = i, None
    if valley is not None:            # 最后一个弯的出弯在终点线之后
        out.append(valley)
    return out


def _argmax(v: list[float], lo: int, hi: int) -> int:
    """v[lo..hi] 里最大值的下标（相同取第一个）。"""
    best = lo
    for i in range(lo, hi + 1):
        if v[i] > v[best]:
            best = i
    return best


def detect_corners(ref_resampled: dict, x_grid: list[float], track_length_m: float | None) -> list[dict]:
    """在参考圈（resample_lap 的结果）上找弯，按赛道顺序编号 T1..Tn。

    返回 [{name, apex, apex_i, start, start_i, end, end_i, min_kmh}]，位置是归一化位置，*_i 是网格下标。
    """
    raw = ref_resampled.get("speed") or []
    if len(raw) < 3 or any(s is None for s in raw):
        return []
    v = [float(s) for s in raw]
    n = len(v)
    gap = MERGE_M / track_length_m if track_length_m else MERGE_FRAC

    apexes: list[int] = []
    for a in _apexes(v):
        if apexes and x_grid[a] - x_grid[apexes[-1]] < gap:
            if v[a] < v[apexes[-1]]:
                apexes[-1] = a
            continue
        apexes.append(a)

    corners: list[dict] = []
    for k, a in enumerate(apexes):
        lo = apexes[k - 1] if k else 0
        top = _argmax(v, lo, a)
        s = top
        while s + 1 < a and v[s + 1] >= v[top] - START_TOL_KMH:
            s += 1
        corners.append({"apex_i": a, "start_i": s})
    for k, c in enumerate(corners):
        a = c["apex_i"]
        if k + 1 < len(corners):
            nxt = corners[k + 1]
            top = _argmax(v, a, nxt["apex_i"])
            mid = (a + nxt["start_i"]) // 2
            e = max(a, min(mid, top, nxt["start_i"]))
        else:
            top = _argmax(v, a, n - 1)
            mid = (a + n - 1) // 2
            e = max(a, min(mid, top))
        c["end_i"] = e

    return [{
        "name": f"T{k + 1}",
        "apex": x_grid[c["apex_i"]], "apex_i": c["apex_i"],
        "start": x_grid[c["start_i"]], "start_i": c["start_i"],
        "end": x_grid[c["end_i"]], "end_i": c["end_i"],
        "min_kmh": round(v[c["apex_i"]], 1),
    } for k, c in enumerate(corners)]


def build_segments(corners: list[dict], x_grid: list[float]) -> list[dict]:
    """弯道段 + 中间的直道段，首尾相接覆盖 [0, 1]。长度为 0 的直道省略。"""
    last = len(x_grid) - 1
    segs: list[dict] = []
    cur = 0
    for k, c in enumerate(corners):
        if c["start_i"] > cur:
            name = f"T{k}–{c['name']}" if k else f"起点–{c['name']}"
            segs.append({"name": name, "kind": "straight", "start_i": cur, "end_i": c["start_i"]})
        segs.append({"name": c["name"], "kind": "corner", "start_i": max(cur, c["start_i"]),
                     "end_i": c["end_i"], "apex_i": c["apex_i"]})
        cur = c["end_i"]
    if cur < last or not segs:
        name = f"{corners[-1]['name']}–终点" if corners else "整圈"
        segs.append({"name": name, "kind": "straight", "start_i": cur, "end_i": last})
    for s in segs:
        s["start"], s["end"] = x_grid[s["start_i"]], x_grid[s["end_i"]]
    return segs


def _side(lap: dict, x: list[float], seg: dict, brake_from: int, gap: float) -> dict:
    """一圈在一个分段里的数值。lap 是 compare_laps 结果里的 ref / cmp。"""
    i0, i1 = seg["start_i"], seg["end_i"]
    speed, brake, t = lap.get("speed"), lap.get("brake"), lap.get("t")
    ok = t and t[i0] is not None and t[i1] is not None        # 没有采样点的圈全是 None
    out: dict = {"time_ms": int(round(t[i1])) - int(round(t[i0])) if ok else None}
    if speed and speed[i0] is not None:
        lo = min(range(i0, i1 + 1), key=lambda i: speed[i])
        out.update(entry_kmh=round(speed[i0], 1), min_kmh=round(speed[lo], 1), min_pos=x[lo],
                   exit_kmh=round(speed[i1], 1))
    if seg["kind"] == "corner":
        hit = None
        if brake and brake[i0] is not None:
            hit = _brake_onset(brake, x, brake_from, seg.get("apex_i", i1), gap)
        out["brake_pos"] = hit
    return out


def _brake_onset(brake: list[float], x: list[float], lo: int, apex: int, gap: float) -> float | None:
    """进弯刹车点：从弯心往回找，连着的那段刹车（中间松开不到 gap 的算同一段）从哪里开始。

    不直接取“窗口里第一次踩刹车”，否则直道上点一下刹车（比如全油门弯前的轻点）会被当成刹车点。
    """
    on = [i for i in range(lo, apex + 1) if brake[i] > BRAKE_ON]
    if not on:
        return None
    start = on[-1]
    for i in reversed(on[:-1]):
        if x[start] - x[i] > gap:
            break
        start = i
    return x[start]


def corner_table(compare_result: dict) -> dict:
    """逐段对比（compare_laps 的结果，需要其中 ref/cmp 的 "t"，单位 ms）。

    分段用参考圈的速度曲线定。刹车点在本段起点（前面是直道时从直道起点）到弯心之间找，
    这样对比圈比参考圈刹得早也能找到；取进弯那一段连续刹车的起点（见 _brake_onset）。delta_ms = 对比圈 − 参考圈，正数 = 对比圈更慢。
    """
    x = compare_result["x"]
    ref, cmp = compare_result["ref"], compare_result["cmp"]
    length = compare_result.get("track_length_m")
    corners = detect_corners(ref, x, length)
    segs = build_segments(corners, x)
    gap = BRAKE_GAP_M / length if length else BRAKE_GAP_FRAC
    rows = []
    for k, s in enumerate(segs):
        prev = segs[k - 1] if k else None
        brake_from = prev["start_i"] if prev and prev["kind"] == "straight" else s["start_i"]
        r, c = _side(ref, x, s, brake_from, gap), _side(cmp, x, s, brake_from, gap)
        row = {"name": s["name"], "kind": s["kind"], "start": s["start"], "end": s["end"],
               "ref": r, "cmp": c,
               "delta_ms": None if None in (r["time_ms"], c["time_ms"]) else c["time_ms"] - r["time_ms"]}
        if s["kind"] == "corner":
            row["apex"] = x[s["apex_i"]]
        rows.append(row)
    return {
        "count": len(corners),
        "segments": rows,
        "total_delta_ms": sum(r["delta_ms"] or 0 for r in rows),
    }


def _boundary_times(lap: dict, samples: dict, bounds: list[float]) -> list[int]:
    d, t = distance_profile(samples["pos"], samples["t_ms"], lap["lap_time_ms"])
    out = [int(round(interp(d, t, b))) for b in bounds]
    out[0], out[-1] = 0, int(lap["lap_time_ms"])
    return out


def theoretical_best(laps: list[tuple[dict, dict]], points: int = 2000) -> dict | None:
    """同一赛道+车型的若干干净圈 [(laps 表的行, 采样点)] → 理论最快圈。

    分段边界在最快那一圈上找（detect_corners），再按归一化位置套到每一圈上：
    每圈各自用 distance_profile 求 t(边界)，取每段最快的那一圈，加起来就是理论最快圈。
    因为最快圈自己的各段加起来就是它的圈时，理论最快圈一定 ≤ 最快圈。少于 2 圈时返回 None。
    """
    laps = [(lap, s) for lap, s in laps if s.get("pos")]
    if len(laps) < 2:
        return None
    fastest, fs = min(laps, key=lambda p: (p[0]["lap_time_ms"], p[0]["id"]))
    grid = make_grid(points)
    r = resample_lap(fs, fastest["lap_time_ms"], grid)
    segs = build_segments(detect_corners(r, grid, fastest.get("track_length_m")), grid)
    bounds = [segs[0]["start"]] + [s["end"] for s in segs]
    times = [(lap, _boundary_times(lap, s, bounds)) for lap, s in laps]
    ft = next(tb for lap, tb in times if lap is fastest)

    out = []
    for k, seg in enumerate(segs):
        best_lap, best_ms = None, None
        for lap, tb in times:
            ms = tb[k + 1] - tb[k]
            if best_ms is None or ms < best_ms:
                best_lap, best_ms = lap, ms
        out.append({"name": seg["name"], "kind": seg["kind"], "start": seg["start"], "end": seg["end"],
                    "best_ms": best_ms, "lap_id": best_lap["id"], "lap_number": best_lap["lap_number"],
                    "fastest_ms": ft[k + 1] - ft[k]})
    theo = sum(s["best_ms"] for s in out)
    return {
        "best_ms": int(fastest["lap_time_ms"]),
        "best_lap_id": fastest["id"],
        "theoretical_ms": theo,
        "gain_ms": int(fastest["lap_time_ms"]) - theo,
        "laps": len(laps),
        "segments": out,
    }
