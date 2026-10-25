"""v0.1.0 合并后 Codex 审查指出的问题的回归测试。"""
from __future__ import annotations

import copy
import gzip
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from simdrive import SESSION, drive
from test_setups_lmu import SVM

from simtelemetry import server
from simtelemetry.lapfile import export_lap, parse_lap_file
from simtelemetry.live import LiveHub
from simtelemetry.models import TelemetryFrame
from simtelemetry.recorder import LapRecorder
from simtelemetry.setups.library import SetupLibrary
from simtelemetry.storage import Storage


def test_import_limit_applies_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "IMPORT_LIMIT", 1000)

    def chunked():                                   # 生成器 → 分块上传，没有 Content-Length
        for _ in range(10):
            yield b"x" * 500

    with TestClient(server.create_app(Storage(tmp_path / "t.db"), LiveHub())) as c:
        assert c.post("/api/laps/import", content=chunked()).status_code == 413
        assert c.post("/api/laps/import", content=b"x" * 2000).status_code == 413
        assert c.post("/api/laps/import", content=b"junk").status_code == 400


def test_lmu_raw_download_keeps_svm(tmp_path):
    root = tmp_path / "Settings"
    (root / "Spa").mkdir(parents=True)
    (root / "Spa" / "q.svm").write_text(SVM, encoding="utf-8")
    lib = SetupLibrary(Storage(tmp_path / "t.db"), {"lmu": root})
    lib.scan()
    vid = lib.list_files("lmu")[0]["latest_version_id"]
    with TestClient(server.create_app(lib.storage, LiveHub(), setups=lib)) as c:
        r = c.get(f"/api/setups/versions/{vid}/raw")
    assert r.headers["content-disposition"].endswith('q.svm"') and r.text.startswith("VehicleClassSetting")


def test_lap_counter_reset_starts_new_session_uid():
    rec = LapRecorder()
    frame = lambda lc, t, tick: TelemetryFrame(100, 1, 0, 0, 3, 7000, lc, t, 0.5, False, True, None, True, tick=tick)
    rec.feed(SESSION, frame(3, 1000, 1), 0.0)
    first = rec.session_uid
    rec.feed(SESSION, frame(0, 100, 2), 1.0)                 # 同赛道同车重开：圈数归零
    assert rec.session_uid != first


@pytest.mark.parametrize("bad", ["4.2km", float("nan"), -5, 0, True])
def test_imported_track_length_must_be_positive_number(tmp_path, bad):
    s = Storage(tmp_path / "t.db")
    lap_id = s.save_lap(drive(200)[0])
    doc = json.loads(gzip.decompress(export_lap(s, lap_id)))
    doc["lap"]["track_length_m"] = bad
    with pytest.raises(ValueError, match="赛道长度"):
        parse_lap_file(gzip.compress(json.dumps(doc).encode()))


def test_imported_track_length_numeric_string_is_converted(tmp_path):
    s = Storage(tmp_path / "t.db")
    lap_id = s.save_lap(drive(200)[0])
    doc = json.loads(gzip.decompress(export_lap(s, lap_id)))
    doc["lap"]["track_length_m"] = "4200.5"
    rec, _ = parse_lap_file(gzip.compress(json.dumps(doc).encode()))
    assert rec.track_length_m == 4200.5


def test_lmu_file_overwritten_with_other_car_keeps_old_versions_on_old_car(tmp_path):
    root = tmp_path / "Settings"
    f = root / "Spa" / "q.svm"
    f.parent.mkdir(parents=True)
    f.write_text(SVM, encoding="utf-8")
    lib = SetupLibrary(Storage(tmp_path / "t.db"), {"lmu": root})
    lib.scan()
    old_vid = lib.list_files("lmu")[0]["latest_version_id"]
    f.write_text(SVM.replace("Test_GT3_2024", "Other_Hyper_2025") + "\n", encoding="utf-8")
    lib.scan()
    assert lib.get_version(old_vid)["car"] == "Test_GT3_2024"          # 旧版本不能被改挂到新车上
    live = [r for r in lib.list_files("lmu") if not r["missing"]]
    assert [(r["car"], r["rel_path"], r["versions"]) for r in live] == [("Other_Hyper_2025", "Spa/q.svm", 1)]
    lib.scan()                                                          # 再扫一遍不会多出东西
    assert len(lib.list_files("lmu")) == 2


def test_reconnect_starts_new_session_uid():
    rec = LapRecorder()
    frame = lambda lc, t, tick: TelemetryFrame(100, 1, 0, 0, 3, 7000, lc, t, 0.5, False, True, None, True, tick=tick)
    rec.feed(SESSION, frame(3, 1000, 1), 0.0)
    first = rec.session_uid
    rec.reset("游戏断开")                                              # 采集器断线时调用
    rec.feed(SESSION, frame(0, 100, 2), 5.0)                          # 重连后同赛道同车
    assert rec.session_uid not in ("", first)


def test_lmu_raw_download_returns_original_bytes(tmp_path):
    root = tmp_path / "Settings"
    (root / "Spa").mkdir(parents=True)
    raw = SVM.encode("cp1252", errors="replace") + b"// Caf\xe9\r\n"         # 游戏按本地编码写的文件，不是 UTF-8
    (root / "Spa" / "q.svm").write_bytes(raw)
    lib = SetupLibrary(Storage(tmp_path / "t.db"), {"lmu": root})
    lib.scan()
    vid = lib.list_files("lmu")[0]["latest_version_id"]
    with TestClient(server.create_app(lib.storage, LiveHub(), setups=lib)) as c:
        assert c.get(f"/api/setups/versions/{vid}/raw").content == raw


def test_old_database_gets_raw_column(tmp_path):
    import sqlite3
    db = tmp_path / "t.db"
    Storage(db)
    con = sqlite3.connect(db)                       # 造一个 v3 的库：setup_versions 没有 raw 列
    con.executescript("DROP TABLE session_setups; DROP TABLE setup_versions; CREATE TABLE setup_versions ("
                      "id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, sha256 TEXT NOT NULL, content TEXT NOT NULL, "
                      "file_mtime TEXT NOT NULL, captured_at TEXT NOT NULL); PRAGMA user_version = 3;")
    con.close()
    with Storage(db).connection() as c:
        assert "raw" in {r["name"] for r in c.execute("PRAGMA table_info(setup_versions)")}


def test_imported_track_length_too_large_is_rejected(tmp_path):
    s = Storage(tmp_path / "t.db")
    lap_id = s.save_lap(drive(200)[0])
    doc = json.loads(gzip.decompress(export_lap(s, lap_id)))
    doc["lap"]["track_length_m"] = 10 ** 400
    with pytest.raises(ValueError, match="赛道长度"):
        parse_lap_file(gzip.compress(json.dumps(doc).encode()))


def test_lmu_reencoded_file_updates_raw_without_new_version(tmp_path):
    root = tmp_path / "Settings"
    f = root / "Spa" / "q.svm"
    f.parent.mkdir(parents=True)
    base = SVM.encode("ascii", errors="replace")                          # 只留 Café 一处非 ASCII
    f.write_bytes(base + "// Café\r\n".encode("utf-8"))
    lib = SetupLibrary(Storage(tmp_path / "t.db"), {"lmu": root})
    lib.scan()
    f.write_bytes(base + "// Café\r\n".encode("cp1252"))   # 文字一样，编码变了
    lib.scan()
    (row,) = lib.list_files("lmu")
    assert row["versions"] == 1
    assert lib.get_version(row["latest_version_id"])["raw"] == f.read_bytes()


@pytest.mark.parametrize("field,value", [("lap", "LAP_FIELDS"), ("samples", "CHANNELS"), ("samples", "INT")])
def test_imported_lap_with_wrong_container_types_is_rejected(tmp_path, field, value):
    from simtelemetry.lapfile import _LAP_FIELDS
    from simtelemetry.models import SAMPLE_CHANNELS
    s = Storage(tmp_path / "t.db")
    lap_id = s.save_lap(drive(200)[0])
    doc = json.loads(gzip.decompress(export_lap(s, lap_id)))
    if value == "INT":
        doc["samples"]["speed_kmh" if "speed_kmh" in SAMPLE_CHANNELS else SAMPLE_CHANNELS[1]] = 5
    else:
        doc[field] = list(_LAP_FIELDS if value == "LAP_FIELDS" else SAMPLE_CHANNELS)
    with pytest.raises(ValueError):
        parse_lap_file(gzip.compress(json.dumps(doc).encode()))


@pytest.mark.parametrize("value", ["fast", None, [1], 10 ** 400])
def test_imported_samples_must_be_numbers(tmp_path, value):
    s = Storage(tmp_path / "t.db")
    lap_id = s.save_lap(drive(200)[0])
    doc = json.loads(gzip.decompress(export_lap(s, lap_id)))
    doc["samples"]["speed"][5] = value
    with pytest.raises(ValueError, match="speed"):
        parse_lap_file(gzip.compress(json.dumps(doc).encode()))


def test_imported_lap_time_overflow_is_rejected(tmp_path):
    s = Storage(tmp_path / "t.db")
    lap_id = s.save_lap(drive(200)[0])
    raw = gzip.decompress(export_lap(s, lap_id))
    doc = json.loads(raw)
    raw = raw.replace(f'"lap_time_ms":{doc["lap"]["lap_time_ms"]}'.encode(), b'"lap_time_ms":1e400')   # → inf
    with pytest.raises(ValueError):
        parse_lap_file(gzip.compress(raw))
