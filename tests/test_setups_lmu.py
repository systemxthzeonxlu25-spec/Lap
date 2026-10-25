"""LMU .svm 调教：解析、分组、不可调项、对比、编码回退、调教库扫描。样本是自己写的，不是真实调教。"""
from __future__ import annotations

import pytest

from simtelemetry.setups import diff_setups, load_any, parse_setup
from simtelemetry.setups.library import SetupLibrary
from simtelemetry.setups.lmu import parse_svm
from simtelemetry.storage import Storage

SVM = r'''VehicleClassSetting="Test_GT3 GT3 WEC2025"
UpgradeSetting=(874165,0,0,0)
//VEH=C:\Games\Le Mans Ultimate\Installed\Vehicles\Test_GT3_2024\1.0\test.VEH
//Note: settings commented out if using the default

[GENERAL]
Notes="测试调教"
Symmetric=1
CGHeightSetting=0//Non-adjustable
FuelSetting=40//0.40

[REARWING]
RWSetting=5//2.0 deg

[SUSPENSION]
FrontAntiSwaySetting=2//P2
RearAntiSwaySetting=0//Detached
FrontToeInSetting=7//-0.117 deg

[CONTROLS]
RearBrakeSetting=37//47.8:52.2
TractionControlMapSetting=4//4

[DRIVELINE]
Gear1Setting=0//Fixed

[FRONTLEFT]
CamberSetting=40//-1.00 deg
PressureSetting=0//136 kPa

[FRONTRIGHT]
CamberSetting=40//-1.00 deg
PressureSetting=0//136 kPa

[REARLEFT]
CamberSetting=30//-1.50 deg
PressureSetting=2//140 kPa

[REARRIGHT]
CamberSetting=30//-1.50 deg
PressureSetting=2//140 kPa

[BASIC]
Downforce=0.500000
Custom=1
'''


def items(text=SVM):
    return {it.key: it for it in parse_svm(text).items()}


def test_parse_car_and_values():
    s = parse_svm(SVM)
    assert s.car == "Test_GT3_2024" and s.car_name == "Test GT3 2024" and s.notes == "测试调教"
    it = items()
    assert it["CamberSetting.0"].display == "-1.00 deg" and it["CamberSetting.0"].raw == 40
    assert it["CamberSetting.0"].label == "外倾角 左前" and it["PressureSetting.3"].display == "140 kPa"
    assert it["CONTROLS.RearBrakeSetting"].display == "47.8:52.2"
    assert it["SUSPENSION.RearAntiSwaySetting"].display == "Detached" and not it["SUSPENSION.RearAntiSwaySetting"].derived
    assert it["GENERAL.CGHeightSetting"].derived and it["DRIVELINE.Gear1Setting"].group == "不可调"
    assert it["BASIC.Downforce"].group == "简易设置"
    assert "GENERAL.Notes" not in it                      # 元信息不算设置项


def test_diff_uses_raw_and_skips_not_adjustable():
    b = SVM.replace("RWSetting=5//2.0 deg", "RWSetting=8//3.2 deg").replace(
        "CGHeightSetting=0//Non-adjustable", "CGHeightSetting=1//Non-adjustable")
    changes = diff_setups(parse_svm(SVM), parse_svm(b))
    assert [c.key for c in changes] == ["REARWING.RWSetting"]
    assert changes[0].clicks == 3 and changes[0].b.display == "3.2 deg"


def test_cp1252_fallback_and_rejects_other_files():
    raw = SVM.replace("测试调教", "Café").encode("cp1252", errors="replace")
    assert parse_svm(raw).notes.startswith("Caf")
    with pytest.raises(ValueError):
        parse_svm("[GENERAL]\nNotes=\"hi\"\n")
    with pytest.raises(ValueError):
        parse_setup("lmu", '{"carName": "x"}')


def test_load_any_by_extension(tmp_path):
    f = tmp_path / "q.svm"
    f.write_text(SVM, encoding="utf-8")
    assert load_any(f).car == "Test_GT3_2024"


def test_library_scans_lmu_and_never_writes(tmp_path):
    root = tmp_path / "Settings"
    f = root / "Spa" / "quali.svm"
    f.parent.mkdir(parents=True)
    f.write_bytes(SVM.encode("utf-8"))
    before = f.read_bytes(), f.stat().st_mtime_ns
    lib = SetupLibrary(Storage(tmp_path / "t.db"), {"lmu": root})
    assert lib.scan()["new_versions"] == 1
    (row,) = lib.list_files("lmu")
    assert row["car"] == "Test_GT3_2024" and row["track"] == "Spa" and row["car_name"] == "Test GT3 2024"
    v = lib.version_items(row["latest_version_id"])
    assert any(i["key"] == "CamberSetting.0" and i["display"] == "-1.00 deg" for i in v["items"])
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before
