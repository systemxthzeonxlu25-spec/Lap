"""圈 ↔ 调教：名字匹配、按时间自动猜、手动指定 / “没有” / 回到自动、导入的圈不猜、接口。"""
from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from simdrive import drive
from test_setups_acc import SAMPLE

from simtelemetry.lapfile import export_lap, import_lap
from simtelemetry.live import LiveHub
from simtelemetry.models import SessionInfo
from simtelemetry.server import create_app
from simtelemetry.setups.library import SetupLibrary
from simtelemetry.setups.linking import annotate_laps, names_match, set_session_setup
from simtelemetry.storage import Storage

ACC = SessionInfo(game="acc", track="monza", car="ferrari_296_gt3")


def test_names_match():
    assert names_match("ferrari_296_gt3", "ferrari_296_gt3")
    assert names_match("BMW_M4_LMGT3_2023", "BMW M4 LMGT3")                   # LMU：目录名带年份
    assert names_match("Bahrain", "Bahrain International Circuit")
    assert not names_match("BMW_M4_LMGT3_2023", "BMW M Hybrid V8")
    assert not names_match("", "monza") and not names_match("spa", "monza")


def write_setup(root, car, track, name, age_h, wing):
    data = copy.deepcopy(SAMPLE)
    data["carName"] = car
    data["advancedSetup"]["aeroBalance"]["rearWing"] = wing
    p = root / car / track / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    t = time.time() - age_h * 3600
    os.utime(p, (t, t))


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Setups"
    write_setup(root, "ferrari_296_gt3", "monza", "old", 48, 5)
    write_setup(root, "ferrari_296_gt3", "monza", "latest", 2, 9)
    write_setup(root, "ferrari_296_gt3", "monza", "future", -2, 12)          # 会话开始之后才存的：不算
    write_setup(root, "ferrari_296_gt3", "spa", "other_track", 1, 3)
    write_setup(root, "porsche_992_gt3_r", "monza", "other_car", 1, 3)
    storage = Storage(tmp_path / "t.db")
    lib = SetupLibrary(storage, {"acc": root})
    lib.scan()
    laps = [replace(l, session=ACC) for l in drive(60 * 6)]
    for l in laps:
        storage.save_lap(l)
    return storage, lib, laps


def setups_of(storage, session=ACC):
    return {l["setup"]["name"] if l["setup"] and l["setup"].get("name") else None
            for l in annotate_laps(storage, storage.list_laps(*session.combo))}


def test_auto_guess_picks_last_saved_before_session(env):
    storage, _, _ = env
    laps = annotate_laps(storage, storage.list_laps(*ACC.combo))
    assert {l["setup"]["name"] for l in laps} == {"latest"}
    assert all(l["setup"]["source"] == "auto" for l in laps)


def test_manual_none_and_back_to_auto(env):
    storage, lib, laps = env
    uid = laps[0].session_uid
    old = next(f for f in lib.list_files("acc") if f["name"] == "old")["latest_version_id"]
    set_session_setup(storage, uid, old)
    (lap, *_) = annotate_laps(storage, storage.list_laps(*ACC.combo))
    assert lap["setup"]["name"] == "old" and lap["setup"]["source"] == "manual"
    set_session_setup(storage, uid, None)
    assert setups_of(storage) == {None}
    set_session_setup(storage, uid, None, auto=True)
    assert setups_of(storage) == {"latest"}


def test_no_guess_without_matching_setup_or_for_imported(env, tmp_path):
    storage, _, laps = env
    other = Storage(tmp_path / "o.db")
    for l in laps[:2]:
        other.save_lap(replace(l, session=replace(ACC, track="imola")))
    assert setups_of(other, replace(ACC, track="imola")) == {None}           # 没有 imola 的调教：不猜
    friend = import_lap(storage, export_lap(storage, storage.list_laps(*ACC.combo)[0]["id"]))
    lap = next(l for l in annotate_laps(storage, storage.list_laps(*ACC.combo)) if l["id"] == friend["id"])
    assert lap["setup"] is None


def test_api(env):
    storage, lib, laps = env
    with TestClient(create_app(storage, LiveHub(), setups=lib)) as c:
        params = dict(zip(("game", "track", "car"), ACC.combo))
        r = c.get("/api/laps", params=params).json()
        assert r["laps"][0]["setup"]["name"] == "latest"
        choices = c.get("/api/setup_choices", params=params).json()
        names = [x["name"] for x in choices]
        assert "other_car" not in names and names.index("other_track") > names.index("latest")
        uid = laps[0].session_uid
        vid = next(x["version_id"] for x in choices if x["name"] == "old")
        assert c.put(f"/api/sessions/{uid}/setup", json={"version_id": vid}).status_code == 200
        assert c.get("/api/laps", params=params).json()["laps"][0]["setup"]["name"] == "old"
        assert c.put("/api/sessions/nope/setup", json={"version_id": vid}).status_code == 404
        assert c.put(f"/api/sessions/{uid}/setup", json={"version_id": 99999}).status_code == 404
        assert c.put(f"/api/sessions/{uid}/setup", json={"auto": True}).status_code == 200
        assert c.get("/api/laps", params=params).json()["laps"][0]["setup"]["source"] == "auto"
