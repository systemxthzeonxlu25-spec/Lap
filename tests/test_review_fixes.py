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


def test_lmu_file_overwritten_with_other_car_updates_car(tmp_path):
    root = tmp_path / "Settings"
    f = root / "Spa" / "q.svm"
    f.parent.mkdir(parents=True)
    f.write_text(SVM, encoding="utf-8")
    lib = SetupLibrary(Storage(tmp_path / "t.db"), {"lmu": root})
    lib.scan()
    f.write_text(SVM.replace("Test_GT3_2024", "Other_Hyper_2025") + "\n", encoding="utf-8")
    lib.scan()
    (row,) = lib.list_files("lmu")
    assert row["car"] == "Other_Hyper_2025" and row["versions"] == 2


def test_lap_counter_reset_starts_new_session_uid():
    rec = LapRecorder()
    frame = lambda lc, t, tick: TelemetryFrame(100, 1, 0, 0, 3, 7000, lc, t, 0.5, False, True, None, True, tick=tick)
    rec.feed(SESSION, frame(3, 1000, 1), 0.0)
    first = rec.session_uid
    rec.feed(SESSION, frame(0, 100, 2), 1.0)                 # 同赛道同车重开：圈数归零
    assert rec.session_uid != first


@pytest.mark.parametrize("bad", ["4.2km", float("nan"), -5, 0])
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
