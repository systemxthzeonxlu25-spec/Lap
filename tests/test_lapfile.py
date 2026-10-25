"""单圈文件导出 / 导入：往返一致、去重、不算个人最快圈、坏文件、接口和命令行。"""
from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient
from simdrive import SESSION, drive

from simtelemetry.lapfile import export_lap, import_lap, main, parse_lap_file
from simtelemetry.live import LiveHub
from simtelemetry.server import create_app
from simtelemetry.storage import Storage


@pytest.fixture(scope="module")
def laps():
    return drive(60 * 10)


def own_db(path, laps):
    s = Storage(path)
    ids = [s.save_lap(l) for l in laps]
    return s, ids


def test_roundtrip_and_dedupe(tmp_path, laps):
    src, ids = own_db(tmp_path / "a.db", laps)
    best = src.best_lap(*SESSION.combo)
    data = export_lap(src, best["id"], note="  张三 @ 车队  ")

    dst = Storage(tmp_path / "b.db")
    r = import_lap(dst, data)
    assert not r["duplicate"] and r["note"] == "张三 @ 车队"
    lap = dst.get_lap(r["id"])
    assert lap["is_imported"] and lap["lap_time_ms"] == best["lap_time_ms"] and lap["is_clean"]
    assert dst.get_samples(r["id"]) == src.get_samples(best["id"])
    assert import_lap(dst, data)["duplicate"] and len(dst.list_laps(*SESSION.combo)) == 1


def test_imported_lap_is_not_personal_best(tmp_path, laps):
    src, _ = own_db(tmp_path / "a.db", laps)
    fast = src.best_lap(*SESSION.combo)
    mine, _ = own_db(tmp_path / "b.db", [l for l in laps if l.lap_time_ms > fast["lap_time_ms"]])
    my_best = mine.best_lap(*SESSION.combo)
    imported = import_lap(mine, export_lap(src, fast["id"]))
    assert mine.best_lap(*SESSION.combo)["id"] == my_best["id"]          # 别人更快的圈不顶替我的最快圈
    combo = mine.list_combos()[0]
    assert combo["best_ms"] == my_best["lap_time_ms"]
    assert any(l["id"] == imported["id"] for l in mine.list_laps(*SESSION.combo))


@pytest.mark.parametrize("data, msg", [
    (b"not gzip", "不是 .lap.gz"),
    (gzip.compress(b"[1, 2]"), "不是 simtelemetry"),
    (gzip.compress(json.dumps({"format": "simtelemetry-lap", "version": 99}).encode()), "版本"),
    (gzip.compress(json.dumps({"format": "simtelemetry-lap", "version": 1, "lap": {}, "samples": {}}).encode()), "缺少"),
])
def test_rejects_bad_files(data, msg):
    with pytest.raises(ValueError, match=msg):
        parse_lap_file(data)


def test_rejects_ragged_samples(tmp_path, laps):
    src, ids = own_db(tmp_path / "a.db", laps[:2])
    doc = json.loads(gzip.decompress(export_lap(src, ids[1])))
    doc["samples"]["speed"].pop()
    with pytest.raises(ValueError, match="采样点"):
        parse_lap_file(gzip.compress(json.dumps(doc).encode()))


def test_api(tmp_path, laps):
    src, ids = own_db(tmp_path / "a.db", laps[:3])
    dst = Storage(tmp_path / "b.db")
    with TestClient(create_app(src, LiveHub())) as c:
        r = c.get(f"/api/laps/{ids[1]}/export", params={"note": "测试"})
        assert r.status_code == 200 and ".lap.gz" in r.headers["content-disposition"]
        assert c.get("/api/laps/9999/export").status_code == 404
        data = r.content
    with TestClient(create_app(dst, LiveHub())) as c:
        r = c.post("/api/laps/import", content=data, headers={"Content-Type": "application/gzip"})
        assert r.status_code == 200 and r.json()["note"] == "测试" and r.json()["lap"]["is_imported"]
        assert c.post("/api/laps/import", content=b"junk").status_code == 400
        laps_json = c.get("/api/laps", params=dict(zip(("game", "track", "car"), SESSION.combo))).json()
        assert laps_json["best_lap_id"] is None                         # 只有导入的圈：没有“个人最快圈”


def test_cli(tmp_path, laps, capsys):
    src, ids = own_db(tmp_path / "a.db", laps[:2])
    out = tmp_path / "x.lap.gz"
    main(["--db", str(src.path), "export", str(ids[1]), "-o", str(out), "--note", "hi"])
    main(["--db", str(tmp_path / "b.db"), "import", str(out), str(out), str(tmp_path / "missing.gz")])
    text = capsys.readouterr().out
    assert "已导出" in text and "已导入" in text and "已经导入过" in text and "跳过" in text
