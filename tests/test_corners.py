"""分弯道对比 + 理论最快圈（合成赛道 9 个弯，其中 0.36 那个全油门通过，速度上找不到）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from simdrive import SESSION, drive

from simtelemetry.adapters.simulated import CORNERS
from simtelemetry.analysis import compare_laps
from simtelemetry.corners import corner_table, theoretical_best
from simtelemetry.live import LiveHub
from simtelemetry.server import create_app
from simtelemetry.storage import Storage

FLAT_OUT = 0.36                                   # 弯前车速还没到 210 km/h，一路加速通过
BRAKING_CORNERS = [c[0] for c in CORNERS if c[0] != FLAT_OUT]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    s = Storage(tmp_path_factory.mktemp("db") / "c.db")
    for lap in drive(60 * 16, seed=5):
        s.save_lap(lap)
    return s


def clean_laps(db):
    return [l for l in db.list_laps(*SESSION.combo) if l["is_clean"]]


def compare(db, ref, cmp, points=1500):
    return compare_laps(ref, db.get_samples(ref["id"]), cmp, db.get_samples(cmp["id"]), points)


def test_corners_match_synthetic_track(db):
    laps = clean_laps(db)
    assert len(laps) >= 3
    for ref in laps:
        ct = corner_table(compare(db, ref, laps[1] if ref is laps[0] else laps[0]))   # 弯道只看参考圈
        apexes = [s["apex"] for s in ct["segments"] if s["kind"] == "corner"]
        assert len(apexes) == ct["count"] == len(BRAKING_CORNERS)
        for got, want in zip(apexes, BRAKING_CORNERS):
            assert abs(got - want) < 0.01
        assert [s["name"] for s in ct["segments"] if s["kind"] == "corner"] == [f"T{i + 1}" for i in range(8)]


def test_segments_cover_lap_and_deltas_sum_to_final_delta(db):
    laps = clean_laps(db)
    all_laps = db.list_laps(*SESSION.combo)
    pairs = [(laps[0], laps[1]), (laps[1], laps[0]), (laps[0], all_laps[0]), (laps[2], all_laps[-1])]
    for ref, cmp in pairs:
        res = compare(db, ref, cmp, 700)
        ct = corner_table(res)
        segs = ct["segments"]
        assert segs[0]["start"] == 0 and segs[-1]["end"] == 1
        assert all(a["end"] == b["start"] for a, b in zip(segs, segs[1:]))
        assert all(s["start"] < s["end"] for s in segs)
        assert sum(s["ref"]["time_ms"] for s in segs) == ref["lap_time_ms"]
        assert sum(s["cmp"]["time_ms"] for s in segs) == cmp["lap_time_ms"]
        expect = cmp["lap_time_ms"] - ref["lap_time_ms"]
        assert ct["total_delta_ms"] == sum(s["delta_ms"] for s in segs) == expect
        assert abs(res["delta"][-1] * 1000 - expect) < 1e-6


def test_corner_fields(db):
    laps = clean_laps(db)
    ct = corner_table(compare(db, laps[0], laps[1]))
    for s in ct["segments"]:
        if s["kind"] != "corner":
            continue
        assert s["start"] < s["apex"] < s["end"]
        for side in ("ref", "cmp"):
            v = s[side]
            assert v["min_kmh"] < v["entry_kmh"] and v["min_kmh"] < v["exit_kmh"]
            assert s["start"] <= v["min_pos"] <= s["end"]
            assert v["brake_pos"] is not None and v["brake_pos"] < s["apex"]


def test_brake_dab_on_straight_is_not_the_brake_point(db):
    laps = clean_laps(db)
    res = compare(db, laps[0], laps[1])
    before = {s["name"]: s["cmp"]["brake_pos"] for s in corner_table(res)["segments"] if s["kind"] == "corner"}
    segs = corner_table(res)["segments"]
    k = next(i for i, s in enumerate(segs) if s["name"] == "T4")
    straight = segs[k - 1]
    assert straight["kind"] == "straight"
    x = res["x"]
    dab = next(i for i, v in enumerate(x) if v >= (straight["start"] + straight["end"]) / 2)
    for i in range(dab, dab + 3):                        # 直道中间轻点一下刹车
        res["cmp"]["brake"][i] = 0.4
    after = {s["name"]: s["cmp"]["brake_pos"] for s in corner_table(res)["segments"] if s["kind"] == "corner"}
    assert after == before


def test_theoretical_best(db):
    laps = clean_laps(db)
    tb = theoretical_best([(l, db.get_samples(l["id"])) for l in laps])
    best = min(l["lap_time_ms"] for l in laps)
    assert tb["best_ms"] == best
    assert tb["theoretical_ms"] == sum(s["best_ms"] for s in tb["segments"])
    assert 0 <= tb["gain_ms"] == best - tb["theoretical_ms"]
    ids = {l["id"] for l in laps}
    assert all(s["lap_id"] in ids for s in tb["segments"])
    assert all(s["best_ms"] <= s["fastest_ms"] for s in tb["segments"])
    assert sum(s["fastest_ms"] for s in tb["segments"]) == best
    assert theoretical_best([(laps[0], db.get_samples(laps[0]["id"]))]) is None


def _quicker(lap, s, new_id, a, b, ms):
    """复制一圈，在 [a, b] 这段里逐渐快 ms 毫秒（线性分摊，保证计时仍然单调）。"""
    def shift(p):
        return ms * min(1.0, max(0.0, (p - a) / (b - a)))
    t = [round(t - shift(p)) for p, t in zip(s["pos"], s["t_ms"])]
    return {**lap, "id": new_id, "lap_time_ms": lap["lap_time_ms"] - ms}, {**s, "t_ms": t}


def test_theoretical_best_takes_each_segment_from_its_fastest_lap(db):
    """B 在 T4 里快 300 ms，C 在 T1 里快 200 ms → 理论最快 = 原圈 − 500 ms，比最快圈（B）快 200 ms。"""
    lap = clean_laps(db)[0]
    s = db.get_samples(lap["id"])
    b = _quicker(lap, s, -1, 0.43, 0.47, 300)
    c = _quicker(lap, s, -2, 0.05, 0.10, 200)
    tb = theoretical_best([(lap, s), b, c])
    assert tb["best_ms"] == lap["lap_time_ms"] - 300 and tb["best_lap_id"] == -1
    assert tb["theoretical_ms"] == lap["lap_time_ms"] - 500 and tb["gain_ms"] == 200
    by_name = {seg["name"]: seg for seg in tb["segments"]}
    assert by_name["T1"]["lap_id"] == -2 and by_name["T4"]["lap_id"] == -1
    assert by_name["T1"]["fastest_ms"] - by_name["T1"]["best_ms"] == 200


def test_api_fields(db):
    with TestClient(create_app(db, LiveHub())) as c:
        laps = clean_laps(db)
        r = c.get("/api/compare", params={"ref": laps[0]["id"], "cmp": laps[1]["id"], "points": 400}).json()
        for k in ("x", "track_length_m", "ref", "cmp", "delta", "corners"):
            assert k in r
        assert "t" not in r["ref"] and "t" not in r["cmp"]
        assert r["corners"]["count"] == len(BRAKING_CORNERS)
        assert r["corners"]["total_delta_ms"] == laps[1]["lap_time_ms"] - laps[0]["lap_time_ms"]

        q = {"game": "sim", "track": SESSION.track, "car": SESSION.car}
        tb = c.get("/api/theoretical_best", params=q).json()
        assert tb["best_ms"] == min(l["lap_time_ms"] for l in laps)
        assert tb["theoretical_ms"] <= tb["best_ms"] and tb["gain_ms"] >= 0 and tb["segments"]

        none = c.get("/api/theoretical_best", params={**q, "car": "nope"}).json()
        assert none["best_ms"] is None and none["theoretical_ms"] is None and none["segments"] == []
