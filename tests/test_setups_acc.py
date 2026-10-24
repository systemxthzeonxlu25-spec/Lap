"""ACC 调教：换算表、解析、对比、命令行。样本是按 setup.json 结构自己编的数据，不是真实调教。"""
from __future__ import annotations

import copy
import json

import pytest

from simtelemetry.setups import diff_setups, load_setup
from simtelemetry.setups.__main__ import main as setups_cli
from simtelemetry.setups.acc import apply_rule
from simtelemetry.setups.acc_tables import CARS
from tools.gen_acc_setup_tables import body_rules

SAMPLE = {
    "carName": "ferrari_296_gt3",
    "basicSetup": {
        "tyres": {"tyreCompound": 0, "tyrePressure": [50, 52, 44, 46]},
        "alignment": {"camber": [2, 2, 1, 1], "toe": [30, 30, 40, 40],
                      "staticCamber": [-4.1, -4.1, -5.0, -5.0], "toeOutLinear": [0.0001, 0.0001, 0.0, 0.0],
                      "casterLF": 10, "casterRF": 10, "steerRatio": 1},
        "electronics": {"tC1": 4, "tC2": 0, "abs": 3, "eCUMap": 0, "fuelMix": 0, "telemetryLaps": 0},
        "strategy": {"fuel": 20, "nPitStops": 0, "tyreSet": 1, "frontBrakePadCompound": 0,
                     "rearBrakePadCompound": 0, "pitStrategy": [], "fuelPerLap": 2.5},
    },
    "advancedSetup": {
        "mechanicalBalance": {"aRBFront": 4, "aRBRear": 3, "wheelRate": [2, 2, 4, 4],
                              "bumpStopRateUp": [1, 1, 2, 2], "bumpStopRateDn": [0, 0, 0, 0],
                              "bumpStopWindow": [10, 10, 10, 10], "brakeTorque": 20, "brakeBias": 30},
        "dampers": {"bumpSlow": [5, 5, 5, 5], "bumpFast": [2, 2, 2, 2],
                    "reboundSlow": [7, 7, 7, 7], "reboundFast": [4, 4, 4, 4]},
        "aeroBalance": {"rideHeight": [3, 7, 12, 20], "rodLength": [35.0, 35.0, 45.0, 45.0],
                        "splitter": 0, "rearWing": 8, "brakeDuct": [2, 2]},
        "drivetrain": {"preload": 6},
    },
    "trackBopType": 1,
}


def items(data):
    return {it.key: it for it in load_setup(data).items()}


def test_tables_match_race_element_source():
    # 对照 Race Element 的 Ferrari296GT3.cs 手工核对过的值
    car = CARS["ferrari_296_gt3"]
    assert car["class"] == "GT3" and car["name"] == "Ferrari 296 GT3 2023"
    assert car["brake_bias"] == ("lin", 50, 0.2, 2, None)
    assert car["camber"] == {"front": ("lin", -4, 0.1, 2, None), "rear": ("lin", -3.5, 0.1, 2, None)}
    assert car["ride_height"] == {"front": ("lin", 50, 1, None, 0), "rear": ("lin", 50, 1, None, 2)}
    assert car["wheel_rate"]["front"][1][0] == 163769
    assert len(CARS) >= 50
    for name, c in CARS.items():                       # 每辆车每个字段都有规则
        assert {"tyre_pressure", "camber", "brake_bias", "ride_height", "wheel_rate"} <= set(c), name


def test_converted_values():
    it = items(SAMPLE)
    assert it["tyre_pressure.0"].value == 25.3 and it["tyre_pressure.0"].unit == "psi"
    assert it["camber.0"].value == -3.8 and it["camber.2"].value == -3.4
    assert it["brake_bias"].value == 56.0
    assert it["ride_height.front"].value == 53 and it["ride_height.rear"].value == 62
    assert it["ride_height.raw1"].group == "其他"         # [1]、[3] 含义未知，只给原始值
    assert it["wheel_rate.0"].value == 176367
    assert it["preload"].value == 80
    assert it["static_camber.0"].derived


def test_unknown_car_and_unknown_fields_keep_raw_values():
    data = copy.deepcopy(SAMPLE)
    data["carName"] = "future_car_gt3"
    data["advancedSetup"]["newThing"] = {"knob": 3}
    it = items(data)
    assert it["tyre_pressure.0"].value is None and it["tyre_pressure.0"].raw == 50
    assert it["other.advancedSetup.newThing.knob"].raw == 3
    assert not load_setup(data).has_conversion


def test_apply_rule_table_clamps():
    rule = ("table", [1.0, 2.0, 3.0], None, None)
    assert apply_rule(rule, 99) == 3.0 and apply_rule(rule, -5) == 1.0
    assert apply_rule(("lin", 50, 1, None, 2), [0, 9, 4, 9]) == 54     # 固定取 [2]


def test_diff_skips_derived_and_counts_clicks():
    b = copy.deepcopy(SAMPLE)
    b["advancedSetup"]["mechanicalBalance"]["aRBRear"] = 1
    b["basicSetup"]["alignment"]["staticCamber"] = [-4.2, -4.2, -5.0, -5.0]
    changes = diff_setups(load_setup(SAMPLE), load_setup(b))
    assert [c.key for c in changes] == ["arb_rear"] and changes[0].clicks == -2
    with_derived = diff_setups(load_setup(SAMPLE), load_setup(b), include_derived=True)
    assert {"static_camber.0", "static_camber.1"} <= {c.key for c in with_derived}


def test_rejects_non_setup_json():
    with pytest.raises(ValueError):
        load_setup({"hello": 1})


def test_cli_diff(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(SAMPLE), encoding="utf-8")
    changed = copy.deepcopy(SAMPLE)
    changed["basicSetup"]["tyres"]["tyrePressure"][0] = 53
    b.write_text(json.dumps(changed), encoding="utf-8")
    setups_cli(["diff", str(a), str(b)])
    out = capsys.readouterr().out
    assert "胎压 左前" in out and "25.3 psi" in out and "25.6 psi" in out and "+3 格" in out


@pytest.mark.parametrize("body, expect", [
    ("return rawValue;", ("lin", 0, 1, None, None)),
    ("return Math.Round(13d + rawValue, 2);", ("lin", 13, 1, 2, None)),
    ("switch (GetPosition(wheel)) { case Position.Front: return Math.Round(-4 + 0.1 * rawValue[(int)wheel], 2);"
     " case Position.Rear: return Math.Round(-3.5 + 0.1 * rawValue[(int)wheel], 2); default: return -1; }",
     {"front": ("lin", -4, 0.1, 2, None), "rear": ("lin", -3.5, 0.1, 2, None)}),
    ("return position switch { Position.Front => 122 + rawValue[0], Position.Rear => 140 + rawValue[2], _ => -1, };",
     {"front": ("lin", 122, 1, None, 0), "rear": ("lin", 140, 1, None, 2)}),
    ("if (rawValue > casters.Length - 1) return casters[^1]; return Math.Round(casters[rawValue], 2);",
     ("table", [8.5, 8.7], 2, None)),
])
def test_generator_understands_race_element_patterns(body, expect):
    assert body_rules(body, {"casters": [8.5, 8.7]}) == expect


def test_generator_refuses_unknown_code():
    with pytest.raises(ValueError):
        body_rules("return System.IO.File.Delete(x);", {})
