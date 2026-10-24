"""调教库：扫描、版本、缺失标记、API；以及“绝不改动游戏目录”。"""
from __future__ import annotations

import copy
import json
import os
import sqlite3

from fastapi.testclient import TestClient
from test_setups_acc import SAMPLE

from simtelemetry.live import LiveHub
from simtelemetry.server import create_app
from simtelemetry.setups.library import SetupLibrary
from simtelemetry.storage import Storage


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def snapshot(root):
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()}


def bump_mtime(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def make(tmp_path):
    root = tmp_path / "Setups"
    f = root / "ferrari_296_gt3" / "monza" / "q1.json"
    write(f, SAMPLE)
    lib = SetupLibrary(Storage(tmp_path / "t.db"), {"acc": root})
    return root, f, lib


def test_scan_versions_and_missing(tmp_path):
    root, f, lib = make(tmp_path)
    assert lib.scan() == {"new_files": 1, "new_versions": 1, "missing": 0}
    assert lib.scan()["new_versions"] == 0                       # 没变：不重复存

    changed = copy.deepcopy(SAMPLE)
    changed["advancedSetup"]["aeroBalance"]["rearWing"] = 10
    write(f, changed)
    bump_mtime(f)
    assert lib.scan()["new_versions"] == 1

    files = lib.list_files("acc")
    assert len(files) == 1 and files[0]["versions"] == 2
    assert files[0]["car_name"] == "Ferrari 296 GT3 2023" and files[0]["track"] == "monza"

    f.write_text('{"carName": "ferrari_296_gt3", "basicSe', encoding="utf-8")   # 游戏写到一半
    bump_mtime(f)
    assert lib.scan()["new_versions"] == 0

    f.unlink()
    assert lib.scan()["missing"] == 1
    assert lib.list_files()[0]["missing"] and lib.list_files()[0]["versions"] == 2   # 历史版本还在


def test_never_touches_game_directory(tmp_path):
    root, f, lib = make(tmp_path)
    before = snapshot(root)
    lib.scan()
    lib.update_file(lib.list_files()[0]["id"], note="排位用", tags=["干地"])
    assert snapshot(root) == before


def test_api(tmp_path):
    root, f, lib = make(tmp_path)
    changed = copy.deepcopy(SAMPLE)
    lib.scan()
    changed["basicSetup"]["tyres"]["tyrePressure"][0] = 53
    write(f, changed)
    bump_mtime(f)
    lib.scan()
    with TestClient(create_app(lib.storage, LiveHub(), setups=lib)) as c:
        r = c.get("/api/setups").json()
        assert r["enabled"] and len(r["files"]) == 1
        fid = r["files"][0]["id"]
        detail = c.get(f"/api/setups/files/{fid}").json()
        new, old = (v["id"] for v in detail["versions"])
        items = {it["key"]: it for it in c.get(f"/api/setups/versions/{new}").json()["items"]}
        assert items["tyre_pressure.0"]["value"] == 25.6 and items["tyre_pressure.0"]["display"] == "25.6 psi"
        diff = c.get("/api/setups/diff", params={"a": old, "b": new}).json()
        assert [ch["key"] for ch in diff["changes"]] == ["tyre_pressure.0"] and diff["changes"][0]["clicks"] == 3
        upd = c.patch(f"/api/setups/files/{fid}", json={"note": "排位", "tags": ["干地", " 干地 ", ""]}).json()
        assert upd["note"] == "排位" and upd["tags"] == ["干地"]
        raw = c.get(f"/api/setups/versions/{old}/raw")
        assert raw.json()["carName"] == "ferrari_296_gt3" and "attachment" in raw.headers["content-disposition"]
        assert c.get("/api/setups/versions/999").status_code == 404
        assert c.post("/api/setups/scan").json()["new_versions"] == 0


def test_api_when_disabled(tmp_path):
    with TestClient(create_app(Storage(tmp_path / "t.db"), LiveHub())) as c:
        assert c.get("/api/setups").json() == {"enabled": False, "roots": {}, "files": []}
        assert c.get("/api/setups/files/1").status_code == 404


def test_old_database_gets_new_tables(tmp_path):
    path = tmp_path / "v1.db"
    Storage(path)
    con = sqlite3.connect(path)
    con.executescript("DROP TABLE setup_versions; DROP TABLE setup_files; PRAGMA user_version = 1;")
    con.close()
    lib = SetupLibrary(Storage(path), {})
    assert lib.list_files() == []
