"""Le Mans Ultimate 调教文件（<游戏目录>/UserData/player/Settings/<赛道>/*.svm）：解析、对比。

.svm 是 rF2 系的文本格式，每项写成 “键=原始格数//游戏显示的值”，例如：
    RearBrakeSetting=37//47.8:52.2
    CamberSetting=40//-1.00 deg
所以和 ACC 不同，这里不需要换算表：显示值直接用游戏自己写的（SetupItem.text），对比按原始格数。
显示为 Non-adjustable / N/A / Fixed 的项是这辆车不能调的，归到“不可调”，对比时默认不列。
车型取自 “//VEH=…\\Vehicles\\<车型目录>\\…” 这一行，没有时用 VehicleClassSetting。

格式依据：91 个公开的 LMU 调教文件（GitHub seralaci/Le-Mans-Ultimate-Setups）全部符合上面的写法；
有 13 个不是合法 UTF-8（多半是备注里的字符），按 cp1252 解码。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .base import Change, SetupItem, diff_setups

__all__ = ["Change", "LmuSetup", "SetupItem", "diff_setups", "load_lmu_setup", "parse_svm"]

NOT_ADJUSTABLE = {"non-adjustable", "n/a", "fixed"}
WHEEL_SECTIONS = {"FRONTLEFT": 0, "FRONTRIGHT": 1, "REARLEFT": 2, "REARRIGHT": 3}
WHEELS = ("左前", "右前", "左后", "右后")
GROUPS = {
    "GENERAL": "常规", "FRONTWING": "空力", "REARWING": "空力", "BODYAERO": "空力", "LEFTFENDER": "空力",
    "RIGHTFENDER": "空力", "SUSPENSION": "悬挂", "CONTROLS": "电子与刹车", "ENGINE": "引擎", "DRIVELINE": "传动",
    "FRONTLEFT": "轮胎与车轮", "FRONTRIGHT": "轮胎与车轮", "REARLEFT": "轮胎与车轮", "REARRIGHT": "轮胎与车轮",
    "BASIC": "简易设置",
}
GROUP_ORDER = ("轮胎与车轮", "悬挂", "空力", "电子与刹车", "引擎", "传动", "常规", "简易设置", "其他", "不可调")

# 常见设置项的中文名；没登记的用键名（去掉 Setting 后缀）
LABELS = {
    "FuelSetting": "燃油", "FuelCapacitySetting": "油箱容量", "VirtualEnergySetting": "虚拟能量",
    "NumPitstopsSetting": "进站次数", "FWSetting": "前翼", "RWSetting": "尾翼",
    "WaterRadiatorSetting": "水箱开度", "OilRadiatorSetting": "油冷开度", "BrakeDuctSetting": "前刹车风道",
    "BrakeDuctRearSetting": "后刹车风道", "FrontAntiSwaySetting": "前防倾杆", "RearAntiSwaySetting": "后防倾杆",
    "FrontToeInSetting": "前束（前）", "RearToeInSetting": "前束（后）", "LeftCasterSetting": "主销后倾 左",
    "RightCasterSetting": "主销后倾 右", "Front3rdSpringSetting": "前第三弹簧", "Rear3rdSpringSetting": "后第三弹簧",
    "Front3rdPackerSetting": "前第三弹簧垫片", "Rear3rdPackerSetting": "后第三弹簧垫片",
    "Front3rdSlowBumpSetting": "前第三 慢速压缩", "Front3rdFastBumpSetting": "前第三 快速压缩",
    "Front3rdSlowReboundSetting": "前第三 慢速回弹", "Front3rdFastReboundSetting": "前第三 快速回弹",
    "Rear3rdSlowBumpSetting": "后第三 慢速压缩", "Rear3rdFastBumpSetting": "后第三 快速压缩",
    "Rear3rdSlowReboundSetting": "后第三 慢速回弹", "Rear3rdFastReboundSetting": "后第三 快速回弹",
    "SteerLockSetting": "转向锁止角", "RearBrakeSetting": "刹车平衡（前:后）", "BrakeMigrationSetting": "刹车迁移",
    "BrakePressureSetting": "刹车压力", "TractionControlMapSetting": "TC", "TCPowerCutMapSetting": "TC 断油",
    "TCSlipAngleMapSetting": "TC 滑移", "AntilockBrakeSystemMapSetting": "ABS", "RevLimitSetting": "转速上限",
    "EngineBoostSetting": "引擎增压", "RegenerationMapSetting": "动能回收", "ElectricMotorMapSetting": "电机模式",
    "EngineMixtureSetting": "燃油混合比", "EngineBrakingMapSetting": "引擎制动", "FinalDriveSetting": "终传比",
    "RatioSetSetting": "齿比组", "DiffPowerSetting": "差速器 加速锁止", "DiffCoastSetting": "差速器 滑行锁止",
    "DiffPreloadSetting": "差速器预载", "DiffPumpSetting": "差速器泵",
    "CamberSetting": "外倾角", "PressureSetting": "胎压", "PackerSetting": "垫片", "SpringSetting": "弹簧",
    "TenderSpringSetting": "辅助弹簧", "TenderTravelSetting": "辅助弹簧行程", "RideHeightSetting": "车高",
    "SlowBumpSetting": "慢速压缩", "FastBumpSetting": "快速压缩", "SlowReboundSetting": "慢速回弹",
    "FastReboundSetting": "快速回弹", "BrakeDiscSetting": "刹车盘厚度", "BrakePadSetting": "刹车片",
    "CompoundSetting": "胎型", "TCSetting": "TC 可用", "ABSSetting": "ABS 可用",
    "GearAutoUpShiftSetting": "自动升挡", "GearAutoDownShiftSetting": "自动降挡", "RearSplitSetting": "驱动分配",
    "FrontDiffPowerSetting": "前差速器 加速锁止", "FrontDiffCoastSetting": "前差速器 滑行锁止",
    "FrontDiffPreloadSetting": "前差速器预载", "FrontDiffPumpSetting": "前差速器泵",
    "Front3rdTenderSpringSetting": "前第三 辅助弹簧", "Front3rdTenderTravelSetting": "前第三 辅助弹簧行程",
    "Rear3rdTenderSpringSetting": "后第三 辅助弹簧", "Rear3rdTenderTravelSetting": "后第三 辅助弹簧行程",
    "Downforce": "下压力", "Balance": "平衡", "Ride": "悬挂软硬", "Gearing": "齿比", "Custom": "自定义",
}
_GEAR = re.compile(r"Gear(\d+)Setting")
_SETTING = re.compile(r"^(\w+)=(-?\d+)//(.*)$")
_KV = re.compile(r"^(\w+)=(.*)$")


def _label(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    m = _GEAR.fullmatch(key)
    if m:
        return f"{m.group(1)} 挡齿比"
    return key[:-7] if key.endswith("Setting") else key


@dataclass(slots=True)
class LmuSetup:
    car: str                                   # 车型目录名，如 BMW_M4_LMGT3_2023
    vehicle_class: str                         # VehicleClassSetting，如 "BMW_M4_LMGT3 GT3 WEC2025"
    sections: dict[str, dict[str, str]] = field(default_factory=dict)   # 节 → 键 → 原文（“37//47.8:52.2”）
    path: Path | None = None

    @property
    def car_name(self) -> str:
        return self.car.replace("_", " ")

    @property
    def has_conversion(self) -> bool:
        return True                            # 显示值是游戏自己写的，不需要换算表

    @property
    def notes(self) -> str:
        return self.sections.get("GENERAL", {}).get("Notes", "").strip('"')

    def items(self) -> list[SetupItem]:
        out: list[SetupItem] = []
        for sec, entries in self.sections.items():
            wheel = WHEEL_SECTIONS.get(sec)
            group = GROUPS.get(sec, "其他")
            for key, value in entries.items():
                m = _SETTING.match(f"{key}={value}")
                if not m:
                    if key in ("Notes", "VehicleClassSetting", "UpgradeSetting", "Symmetric"):
                        continue                 # 元信息，不算设置项
                    out.append(SetupItem(f"{sec}.{key}", group if sec == "BASIC" else "其他", _label(key), value, None,
                                         text=value))
                    continue
                raw, shown = int(m.group(2)), m.group(3).strip()
                fixed = shown.lower() in NOT_ADJUSTABLE
                if wheel is not None:
                    item_key, label = f"{key}.{wheel}", f"{_label(key)} {WHEELS[wheel]}"
                else:
                    item_key, label = f"{sec}.{key}", _label(key)
                out.append(SetupItem(item_key, "不可调" if fixed else group, label, raw, None,
                                     derived=fixed, text=shown))
        out.sort(key=lambda it: GROUP_ORDER.index(it.group) if it.group in GROUP_ORDER else len(GROUP_ORDER))
        return out


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse_svm(source: str | bytes, path: Path | None = None) -> LmuSetup:
    text = _decode(source) if isinstance(source, bytes) else source
    sections: dict[str, dict[str, str]] = {"": {}}
    sec = ""
    veh_dir = ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("//"):
            m = re.match(r"//VEH=.*[\\/]Vehicles[\\/]([^\\/]+)[\\/]", s, re.I)
            if m:
                veh_dir = m.group(1)
            continue
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1].strip().upper()
            sections.setdefault(sec, {})
            continue
        m = _KV.match(s)
        if m:
            sections[sec][m.group(1)] = m.group(2).strip()
    head = sections.pop("", {})
    vehicle_class = head.get("VehicleClassSetting", "").strip('"')
    if not vehicle_class and not veh_dir:
        raise ValueError("不是 LMU 调教文件（缺少 VehicleClassSetting）")
    if not any(_SETTING.match(f"{k}={v}") for entries in sections.values() for k, v in entries.items()):
        raise ValueError("不是 LMU 调教文件（没有任何 “键=数字//显示值” 的设置项）")
    car = veh_dir or vehicle_class.split()[0]
    return LmuSetup(car=car, vehicle_class=vehicle_class, sections=sections, path=path)


def load_lmu_setup(path: str | Path) -> LmuSetup:
    p = Path(path)
    return parse_svm(p.read_bytes(), p)
