"""HTTP 接口 + WebSocket（用合成数据填库，不启动采集线程）。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from simdrive import SESSION, drive

from simtelemetry.collector import Collector
from simtelemetry.live import LiveHub
from simtelemetry.server import create_app
from simtelemetry.storage import Storage


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    s = Storage(tmp_path_factory.mktemp("db") / "t.db")
    for lap in drive(60 * 12):
        s.save_lap(lap)
    return s


def test_rest_endpoints(db):
    hub = LiveHub()
    with TestClient(create_app(db, hub)) as c:
        combos = c.get("/api/combos").json()
        assert combos[0]["game"] == "sim" and combos[0]["best_ms"] > 0

        r = c.get("/api/laps", params={"game": "sim", "track": SESSION.track, "car": SESSION.car}).json()
        laps, best_id = r["laps"], r["best_lap_id"]
        assert best_id and any(l["id"] == best_id and l["is_clean"] for l in laps)
        assert any(l["is_out_lap"] for l in laps)

        other = next(l["id"] for l in laps if l["id"] != best_id)
        cmp = c.get("/api/compare", params={"ref": best_id, "cmp": other, "points": 300}).json()
        assert len(cmp["x"]) == 300
        for side in ("ref", "cmp"):
            for ch in ("speed", "throttle", "brake", "steer", "gear"):
                assert len(cmp[side][ch]) == 300
        assert cmp["track_length_m"] == 4200.0

        assert c.get("/api/compare", params={"ref": 99999, "cmp": other}).status_code == 404
        assert c.get(f"/api/laps/{other}/samples").json()["t_ms"]
        assert c.get("/").status_code == 200 and c.get("/live").status_code == 200
        assert c.get("/vendor/uPlot.iife.min.js", follow_redirects=False).status_code in (200, 307)
        assert c.get("/vendor/evil.js").status_code == 404


def test_delete_lap(tmp_path):
    s = Storage(tmp_path / "d.db")
    lap_id = s.save_lap(drive(200)[0])
    with TestClient(create_app(s, LiveHub())) as c:
        assert c.delete(f"/api/laps/{lap_id}").status_code == 200
        assert c.delete(f"/api/laps/{lap_id}").status_code == 404
    assert s.get_samples(lap_id)["t_ms"] == []          # 级联删除采样点


def test_websocket_streams_collector_snapshots(tmp_path):
    s = Storage(tmp_path / "w.db")
    hub = LiveHub()
    col = Collector(s, hub, game="sim", sim_speed=1.0)
    with TestClient(create_app(s, hub, col)) as c:
        with c.websocket_connect("/ws/live?hz=50") as ws:
            seen = []
            deadline = time.time() + 5
            while time.time() < deadline:
                msg = ws.receive_json()
                seen.append(msg)
                if msg.get("state") == "live" and msg.get("speed") is not None:
                    break
            assert seen[-1]["state"] == "live"
            assert seen[-1]["game"] == "sim" and seen[-1]["track"] == SESSION.track
            assert "delta_ms" in seen[-1]
        st = c.get("/api/status").json()
        assert st["collector"]["connected"] and st["collector"]["game"] == "sim"
