"""ACC 调教文件（Documents/Assetto Corsa Competizione/Setups/<车>/<赛道>/*.json）：解析、换算、对比。

setup.json 里存的大多是“格数”（第几格），不是游戏界面上显示的物理值；换算规则在 acc_tables.py
（移植自 Race Element）。staticCamber / toeOutLinear / rodLength 这类小数是游戏根据其它设置算出来的，
标为 derived，对比时默认不当成“改了调教”。

文件里出现、但这里没登记的字段不会被丢掉：一律按原始值放进“其他”组，游戏更新加了新字段也能对比出来。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .acc_tables import CARS
from .base import Change, SetupItem, diff_setups

__all__ = ["AccSetup", "Change", "SetupItem", "apply_rule", "diff_setups", "load_setup"]

WHEELS = ("左前", "右前", "左后", "右后")
FRONT_REAR = ("前", "后")


@dataclass(slots=True)
class AccSetup:
    car: str                  # setup.json 的 carName，如 ferrari_296_gt3
    data: dict
    path: Path | None = None

    @property
    def car_name(self) -> str:
        info = CARS.get(self.car)
        return info["name"] if info else self.car

    @property
    def car_class(self) -> str | None:
        info = CARS.get(self.car)
        return info["class"] if info else None

    @property
    def has_conversion(self) -> bool:
        return self.car in CARS

    def items(self) -> list[SetupItem]:
        return _items(self)


def load_setup(source: str | Path | dict) -> AccSetup:
    if isinstance(source, dict):
        data, path = source, None
    else:
        path = Path(source)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or "carName" not in data:
        raise ValueError("不是 ACC 调教文件（缺少 carName）")
    return AccSetup(car=str(data["carName"]), data=data, path=path)


# ---------------------------------------------------------------------------- 换算
def apply_rule(rule, raw_values: list | int, own: int | None = None) -> float | None:
    """rule 见 acc_tables.py；raw_values 是该字段的原始值（数组或标量），own 是数组里自己的下标。"""
    if rule is None:
        return None
    kind, *rest = rule
    index = rest[-1]
    if isinstance(raw_values, list):
        i = index if index is not None else own
        if i is None or i >= len(raw_values):
            return None
        raw = raw_values[i]
    else:
        raw = raw_values
    if not isinstance(raw, int):
        return None
    if kind == "lin":
        a, b, ndigits = rest[0], rest[1], rest[2]
        v = a + b * raw
    elif kind == "table":
        values, ndigits = rest[0], rest[1]
        if not values:
            return None
        v = values[max(0, min(raw, len(values) - 1))]
    else:
        raise ValueError(f"未知的规则类型：{kind}")
    return round(v, ndigits) if ndigits is not None else v


def _side_rule(car: dict | None, field: str, side: str):
    if car is None:
        return None
    r = car.get(field)
    return r[side] if isinstance(r, dict) else r


# 字段登记：JSON 路径 → (key, 组, 名称, 单位, 换算字段, 形状)
# 形状："wheels" = 4 个车轮；"axles" = 前/后两个；"scalar"；"derived4" = 4 个派生值
_FIELDS: list[tuple[tuple[str, ...], str, str, str, str, str | None, str]] = [
    (("basicSetup", "tyres", "tyreCompound"), "tyre_compound", "轮胎", "胎型（0 干 / 1 湿）", "", None, "scalar"),
    (("basicSetup", "tyres", "tyrePressure"), "tyre_pressure", "轮胎", "胎压", "psi", "tyre_pressure", "wheels"),
    (("basicSetup", "alignment", "camber"), "camber", "定位", "外倾角", "°", "camber", "wheels"),
    (("basicSetup", "alignment", "toe"), "toe", "定位", "前束", "°", "toe", "wheels"),
    (("basicSetup", "alignment", "casterLF"), "caster.lf", "定位", "主销后倾 左前", "°", "caster", "scalar"),
    (("basicSetup", "alignment", "casterRF"), "caster.rf", "定位", "主销后倾 右前", "°", "caster", "scalar"),
    (("basicSetup", "alignment", "steerRatio"), "steer_ratio", "定位", "转向比", "", "steer_ratio", "scalar"),
    (("basicSetup", "alignment", "staticCamber"), "static_camber", "派生", "静态外倾", "°", None, "derived4"),
    (("basicSetup", "alignment", "toeOutLinear"), "toe_out_linear", "派生", "线性前束", "", None, "derived4"),
    (("basicSetup", "electronics", "tC1"), "tc1", "电子", "TC", "", None, "scalar"),
    (("basicSetup", "electronics", "tC2"), "tc2", "电子", "TC2（切断）", "", None, "scalar"),
    (("basicSetup", "electronics", "abs"), "abs", "电子", "ABS", "", None, "scalar"),
    (("basicSetup", "electronics", "eCUMap"), "ecu_map", "电子", "引擎图（原始值）", "", None, "scalar"),
    (("basicSetup", "electronics", "fuelMix"), "fuel_mix", "电子", "燃油混合比", "", None, "scalar"),
    (("basicSetup", "electronics", "telemetryLaps"), "telemetry_laps", "电子", "MoTeC 导出圈数", "", None, "scalar"),
    (("basicSetup", "strategy", "fuel"), "fuel", "策略", "燃油", "L", None, "scalar"),
    (("basicSetup", "strategy", "fuelPerLap"), "fuel_per_lap", "策略", "每圈油耗", "L", None, "scalar"),
    (("basicSetup", "strategy", "nPitStops"), "pit_stops", "策略", "进站次数", "", None, "scalar"),
    (("basicSetup", "strategy", "tyreSet"), "tyre_set", "策略", "胎组", "", None, "scalar"),
    (("basicSetup", "strategy", "frontBrakePadCompound"), "pad_front", "策略", "前刹车片", "", None, "scalar"),
    (("basicSetup", "strategy", "rearBrakePadCompound"), "pad_rear", "策略", "后刹车片", "", None, "scalar"),
    (("advancedSetup", "mechanicalBalance", "aRBFront"), "arb_front", "机械", "前防倾杆", "", "arb_front", "scalar"),
    (("advancedSetup", "mechanicalBalance", "aRBRear"), "arb_rear", "机械", "后防倾杆", "", "arb_rear", "scalar"),
    (("advancedSetup", "mechanicalBalance", "wheelRate"), "wheel_rate", "机械", "轮心刚度", "N/m", "wheel_rate", "wheels"),
    (("advancedSetup", "mechanicalBalance", "bumpStopRateUp"), "bumpstop_rate", "机械", "限位块刚度", "N",
     "bumpstop_rate", "wheels"),
    (("advancedSetup", "mechanicalBalance", "bumpStopRateDn"), "bumpstop_rate_dn", "机械", "下限位块（原始值）", "",
     None, "wheels"),
    (("advancedSetup", "mechanicalBalance", "bumpStopWindow"), "bumpstop_range", "机械", "限位块行程", "",
     "bumpstop_range", "wheels"),
    (("advancedSetup", "mechanicalBalance", "brakeTorque"), "brake_power", "机械", "刹车力度", "%", "brake_power", "scalar"),
    (("advancedSetup", "mechanicalBalance", "brakeBias"), "brake_bias", "机械", "刹车平衡", "%", "brake_bias", "scalar"),
    (("advancedSetup", "dampers", "bumpSlow"), "bump_slow", "阻尼", "慢速压缩", "", "bump_slow", "wheels"),
    (("advancedSetup", "dampers", "bumpFast"), "bump_fast", "阻尼", "快速压缩", "", "bump_fast", "wheels"),
    (("advancedSetup", "dampers", "reboundSlow"), "rebound_slow", "阻尼", "慢速回弹", "", "rebound_slow", "wheels"),
    (("advancedSetup", "dampers", "reboundFast"), "rebound_fast", "阻尼", "快速回弹", "", "rebound_fast", "wheels"),
    (("advancedSetup", "aeroBalance", "rideHeight"), "ride_height", "空力", "车高", "mm", "ride_height", "ride_height"),
    (("advancedSetup", "aeroBalance", "rodLength"), "rod_length", "派生", "推杆长度", "", None, "derived4"),
    (("advancedSetup", "aeroBalance", "splitter"), "splitter", "空力", "前铲", "", "splitter", "scalar"),
    (("advancedSetup", "aeroBalance", "rearWing"), "rear_wing", "空力", "尾翼", "", "rear_wing", "scalar"),
    (("advancedSetup", "aeroBalance", "brakeDuct"), "brake_duct", "空力", "刹车风道", "", "brake_duct", "axles"),
    (("advancedSetup", "drivetrain", "preload"), "preload", "传动", "差速器预载", "Nm", "preload", "scalar"),
    (("trackBopType",), "track_bop", "其他", "赛道 BoP 类型", "", None, "scalar"),
]
GROUP_ORDER = ("轮胎", "定位", "电子", "机械", "阻尼", "空力", "传动", "策略", "派生", "其他")
_KNOWN_PATHS = {f[0] for f in _FIELDS} | {("carName",)}


def _get(data: dict, path: tuple[str, ...]):
    for k in path:
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return data


def _items(setup: AccSetup) -> list[SetupItem]:
    car = CARS.get(setup.car)
    out: list[SetupItem] = []
    for path, key, group, label, unit, field, shape in _FIELDS:
        raw = _get(setup.data, path)
        if raw is None:
            continue
        if shape == "scalar":
            out.append(SetupItem(key, group, label, raw, apply_rule(_side_rule(car, field, "front"), raw)
                                 if field else None, unit))
        elif shape in ("wheels", "derived4") and isinstance(raw, list):
            for i, v in enumerate(raw):
                side = "front" if i < 2 else "rear"
                name = WHEELS[i] if i < 4 else str(i)
                value = apply_rule(_side_rule(car, field, side), raw, i) if field else None
                out.append(SetupItem(f"{key}.{i}", group, f"{label} {name}", v, value, unit,
                                     derived=shape == "derived4"))
        elif shape == "axles" and isinstance(raw, list):
            for i, v in enumerate(raw):
                side = "front" if i == 0 else "rear"
                name = FRONT_REAR[i] if i < 2 else str(i)
                out.append(SetupItem(f"{key}.{i}", group, f"{label} {name}", v,
                                     apply_rule(_side_rule(car, field, side), raw, i), unit))
        elif shape == "ride_height" and isinstance(raw, list):
            # Race Element 只用 [0]（前）和 [2]（后）；[1]、[3] 含义未知，按原始值列出
            used = set()
            for side, name in (("front", "前"), ("rear", "后")):
                rule = _side_rule(car, field, side)
                idx = rule[-1] if rule and rule[-1] is not None else (0 if side == "front" else 2)
                if idx < len(raw):
                    used.add(idx)
                    out.append(SetupItem(f"{key}.{side}", group, f"{label} {name}", raw[idx],
                                         apply_rule(rule, raw), unit))
            for i, v in enumerate(raw):
                if i not in used:
                    out.append(SetupItem(f"{key}.raw{i}", "其他", f"rideHeight[{i}]（含义未知）", v, None))
        else:
            out.append(SetupItem(key, "其他", ".".join(path), raw, None))
    out.extend(_unknown_leaves(setup.data, ()))
    out.sort(key=lambda it: GROUP_ORDER.index(it.group))     # 稳定排序：组内保持登记顺序
    return out


def _unknown_leaves(node, path: tuple[str, ...]) -> list[SetupItem]:
    if path in _KNOWN_PATHS:
        return []
    if isinstance(node, dict):
        return [it for k, v in node.items() for it in _unknown_leaves(v, (*path, str(k)))]
    if isinstance(node, list) and any(isinstance(v, (dict, list)) for v in node):
        return [it for i, v in enumerate(node) for it in _unknown_leaves(v, (*path, str(i)))]
    key = ".".join(path)
    group = _PREFIX_GROUPS.get(path[:2], "其他")
    label = ".".join(path[2:]) if path[:2] in _PREFIX_GROUPS else key
    return [SetupItem(f"other.{key}", group, label, node, None)]


_PREFIX_GROUPS = {("basicSetup", "strategy"): "策略", ("basicSetup", "electronics"): "电子"}
