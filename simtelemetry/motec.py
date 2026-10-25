"""导入 ACC 导出的 MoTeC i2 日志（.ld，可选同名 .ldx）。

    python -m simtelemetry.motec 文件.ld [更多.ld ...] [--car ferrari_296_gt3] [--track cota] [--dry-run]

ACC 在调教的电子设置里把 “MoTeC 导出圈数”（telemetryLaps）设成 > 0 后，会在
“文档/Assetto Corsa Competizione/MoTeC/” 下写 .ld + .ldx。不用开实时采集，跑完把文件导进来就能对比。

.ld 二进制格式移植自 gotzl/ldparser（https://github.com/gotzl/ldparser，GPL-3.0，逆向得到），
.ldx 的圈标记读法参考 gotzl/acctelemetry（GPL-3.0）。

用你发来的 ACC 文件实测（不是文档写的）：
- SPEED 是 m/s；THROTTLE / BRAKE 是 0..100；STEERANGLE 是方向盘角度（度）；GEAR 0 = 空挡（未见倒挡）；
- LAP_BEACON 通道全是 0，没法用来切圈；
- TIME 是“本圈已用时间”（秒，50 Hz），每次过线归零——没有 .ldx 时就靠它切圈，圈时误差约一个采样间隔（20 ms）；
- 有 .ldx 时用里面的过线时间（微秒），圈时精确。
MoTeC 不记录本圈是否有效、是否在维修区：导入的圈记为有效；停车超过 3 s 的记为进站圈；
距离和其它圈的中位数差超过 3% 的记为不完整（实测：没有 .ldx 时 TIME 偶尔过线不归零，两圈连成一段）。首尾不完整的两段丢弃。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import SAMPLE_CHANNELS, LapRecord, SessionInfo
from .recorder import format_lap_time
from .setups.acc_tables import CARS
from .storage import DEFAULT_DB, Storage

# ldparser 的 ldHead：只用到通道链表指针、日期时间、车手、车辆、赛道
_HEAD = struct.Struct("<" + "I4x" "II" "20x" "I" "24x" "HHH" "I" "8s" "H" "H" "I" "4x"
                      "16s" "16x" "16s" "16x" "64s" "64s" "64x" "64s")
# ldparser 的 ldChan：prev next data_ptr n_data counter dtype_a dtype freq shift mul scale dec name short unit
_CHAN = struct.Struct("<IIIIHHHHhhhh32s8s12s")
_DTYPES = {(0x07, 2): "e", (0x07, 4): "f", (0x00, 2): "h", (0x03, 2): "h", (0x05, 2): "h",
           (0x00, 4): "i", (0x03, 4): "i", (0x05, 4): "i", (0x08, 8): "d"}

LAP_RESET_S = 1.0          # TIME 比上一个采样小这么多就是过线了
DEFAULT_STEER_HALF_LOCK = 270.0


def _s(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


@dataclass
class LdChannel:
    name: str
    unit: str
    freq: int
    data_ptr: int
    count: int
    fmt: str | None
    shift: int
    mul: int
    scale: int
    dec: int


@dataclass
class LdFile:
    path: Path
    driver: str
    vehicle: str
    venue: str
    started: datetime | None
    channels: dict[str, LdChannel]
    _buf: bytes = field(repr=False, default=b"")

    @classmethod
    def read(cls, path: str | Path) -> LdFile:
        path = Path(path)
        buf = path.read_bytes()
        if len(buf) < _HEAD.size:
            raise ValueError("文件太短，不是 MoTeC .ld")
        (_, meta_ptr, _data_ptr, _event_ptr, *_rest) = _HEAD.unpack_from(buf, 0)
        date, time_, driver, vehicle, venue = (_s(x) for x in _rest[-5:])
        started = None
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
            try:
                started = datetime.strptime(f"{date} {time_}", fmt)
                break
            except ValueError:
                pass
        channels: dict[str, LdChannel] = {}
        seen: set[int] = set()
        while meta_ptr and meta_ptr not in seen:                # 防止坏文件里的环形链表
            seen.add(meta_ptr)
            if meta_ptr + _CHAN.size > len(buf):
                raise ValueError("通道表越界，文件可能损坏")
            (_prev, nxt, data_ptr, n, _cnt, dtype_a, dtype, freq, shift, mul, scale, dec,
             name, _short, unit) = _CHAN.unpack_from(buf, meta_ptr)
            ch = LdChannel(_s(name), _s(unit), freq, data_ptr, n, _DTYPES.get((dtype_a, dtype)),
                           shift, mul or 1, scale or 1, dec)
            channels.setdefault(ch.name, ch)
            meta_ptr = nxt
        return cls(path, driver, vehicle, venue, started, channels, buf)

    def data(self, name: str) -> list[float]:
        ch = self.channels[name]
        if ch.fmt is None:
            raise ValueError(f"通道 {name} 的数据类型不认识")
        size = struct.calcsize(ch.fmt)
        if ch.data_ptr + size * ch.count > len(self._buf):
            raise ValueError(f"通道 {name} 的数据越界，文件可能不完整")
        raw = struct.unpack_from(f"<{ch.count}{ch.fmt}", self._buf, ch.data_ptr)
        k = 10.0 ** -ch.dec / ch.scale
        return [(x * k + ch.shift) * ch.mul for x in raw]


def read_ldx_markers(path: str | Path) -> list[float]:
    """.ldx 里的过线标记（相对日志开始的秒数，升序）。"""
    root = ET.parse(path).getroot()
    out = []
    for m in root.iter("Marker"):
        t = m.get("Time")
        if t is not None:
            try:
                out.append(float(t) * 1e-6)
            except ValueError:
                pass
    return sorted(out)


def guess_acc_car(vehicle: str) -> str | None:
    """MoTeC 里的车名（如 “296 GT3”）→ ACC 车型 ID（如 ferrari_296_gt3）。不唯一就返回 None。"""
    tokens = set(re.findall(r"[a-z0-9]+", vehicle.lower()))
    if not tokens:
        return None
    hits = [cid for cid, c in CARS.items()
            if tokens <= set(re.findall(r"[a-z0-9]+", f"{cid} {c['name']}".lower().replace("_", " ")))]
    if len(hits) > 1:                      # “296 GT3” 同时命中多个时，取 ID 最短的（基础款，不带 evo 之类）
        shortest = min(hits, key=len)
        if all(h.startswith(shortest) for h in hits):
            return shortest
        return None
    return hits[0] if hits else None


@dataclass
class ImportResult:
    session: SessionInfo
    laps: list[LapRecord]
    lap_source: str                        # 'ldx' / 'time'
    skipped: int                           # 丢弃的不完整段数


def import_acc_ld(path: str | Path, car: str | None = None, track: str | None = None) -> ImportResult:
    ld = LdFile.read(path)
    for need in ("SPEED", "TIME"):
        if need not in ld.channels:
            raise ValueError(f"缺少 {need} 通道，看起来不是 ACC 导出的文件")
    car = car or guess_acc_car(ld.vehicle) or ld.vehicle or "unknown"
    track = track or ld.venue.lower() or "unknown"
    session = SessionInfo(game="acc", track=track, car=car, session_type="motec")

    t_ch = ld.channels["TIME"]
    lap_t = ld.data("TIME")
    f_t = t_ch.freq or 50
    n = len(lap_t)

    ldx = Path(path).with_suffix(".ldx")
    markers = read_ldx_markers(ldx) if ldx.is_file() else []
    if markers:
        idx = [min(n, max(0, round(m * f_t))) for m in markers]
        bounds = [(idx[k], idx[k + 1], round((markers[k + 1] - markers[k]) * 1000)) for k in range(len(idx) - 1)]
        source, time_source = "ldx", "game"
    else:
        cuts = [i for i in range(1, n) if lap_t[i] < lap_t[i - 1] - LAP_RESET_S]
        bounds = []
        for a, b in zip(cuts, cuts[1:]):
            lap_ms = round((lap_t[b - 1] + 1.0 / f_t - lap_t[b]) * 1000)   # 过线发生在第 b 个采样之前 TIME[b] 秒
            bounds.append((a, b, lap_ms))
        source, time_source = "time", "estimated"
    skipped = 2 if bounds else (1 if n else 0)

    chans = {name: (ld.data(name), ld.channels[name].freq or f_t)
             for name in ("SPEED", "THROTTLE", "BRAKE", "STEERANGLE", "GEAR", "RPMS") if name in ld.channels}
    half_lock = (CARS.get(car, {}).get("steer_lock") or 2 * DEFAULT_STEER_HALF_LOCK) / 2

    def at(name: str, t: float, default: float = 0.0) -> float:
        if name not in chans:
            return default
        data, freq = chans[name]
        return data[min(len(data) - 1, max(0, round(t * freq)))] if data else default

    uid = "motec-" + hashlib.sha256(ld._buf).hexdigest()[:12]
    laps: list[LapRecord] = []
    for number, (a, b, lap_ms) in enumerate(bounds, start=1):
        if b - a < 10 or lap_ms <= 0:
            continue
        samples: dict[str, list] = {c: [] for c in SAMPLE_CHANNELS}
        dist, prev_t, prev_v = 0.0, None, 0.0
        dists = []
        for i in range(a, b):
            t = i / f_t
            v = max(0.0, at("SPEED", t))                       # m/s
            if prev_t is not None:
                dist += (v + prev_v) / 2 * (t - prev_t)
            prev_t, prev_v = t, v
            dists.append(dist)
            t_ms = round(lap_t[i] * 1000) if source == "time" else round((t - a / f_t) * 1000)
            samples["t_ms"].append(max(0, min(t_ms, lap_ms)))
            samples["speed"].append(round(v * 3.6, 2))
            samples["throttle"].append(round(min(1.0, max(0.0, at("THROTTLE", t) / 100)), 4))
            samples["brake"].append(round(min(1.0, max(0.0, at("BRAKE", t) / 100)), 4))
            samples["steer"].append(round(min(1.0, max(-1.0, at("STEERANGLE", t) / half_lock)), 4))
            samples["gear"].append(int(round(at("GEAR", t))))
            samples["rpm"].append(int(round(at("RPMS", t))))
            samples["in_pit"].append(0)
            samples["valid"].append(1)
        # 最后一个采样到过线还有一小段
        total = dist + prev_v * max(0.0, lap_ms / 1000 - samples["t_ms"][-1] / 1000)
        if total <= 0:
            continue
        samples["pos"] = [round(d / total, 6) for d in dists]
        laps.append(LapRecord(
            session=session, session_uid=uid, lap_number=number, lap_time_ms=lap_ms, time_source=time_source,
            is_valid=True, is_out_lap=False, is_in_lap=False, is_complete=True,
            recorded_at=(ld.started.astimezone() if ld.started else datetime.now().astimezone()),
            samples=samples, track_length_m=round(total, 1),
        ))
    _flag_suspicious(laps)
    return ImportResult(session, laps, source, skipped)


STOPPED_S = 3.0            # 一圈里停车超过这么久：多半进站了
LENGTH_TOL = 0.03          # 距离和其它圈的中位数差超过 3%：切圈切错了（比如 TIME 某次过线没归零，两圈连成一段）


def _flag_suspicious(laps: list[LapRecord]) -> None:
    """MoTeC 没有有效/进站信息，按数据推断，避免把错误的圈当成最快圈。"""
    for lap in laps:
        t, v = lap.samples["t_ms"], lap.samples["speed"]
        stopped = sum(max(0, t[i] - t[i - 1]) for i in range(1, len(t)) if v[i] < 3.6) / 1000
        if stopped >= STOPPED_S:
            lap.is_in_lap = True
    lengths = sorted(lap.track_length_m for lap in laps if not lap.is_in_lap and lap.track_length_m)
    if len(lengths) >= 2:
        median = lengths[(len(lengths) - 1) // 2]     # 偏小的中位数：连成一段的圈只会更长，两圈时也不会选中它
        for lap in laps:
            if lap.track_length_m and abs(lap.track_length_m - median) > median * LENGTH_TOL:
                lap.is_complete = False


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m simtelemetry.motec", description="导入 ACC 导出的 MoTeC .ld 文件")
    p.add_argument("files", nargs="+", help=".ld 文件（同目录下有同名 .ldx 时用它切圈，更准）")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--car", help="ACC 车型 ID，如 ferrari_296_gt3（默认按文件里的车名猜）")
    p.add_argument("--track", help="ACC 赛道 ID，如 cota（默认用文件里的赛道名）")
    p.add_argument("--dry-run", action="store_true", help="只显示会导入什么，不写数据库")
    args = p.parse_args(argv)

    storage = None if args.dry_run else Storage(args.db)
    for f in args.files:
        try:
            r = import_acc_ld(f, car=args.car, track=args.track)
        except (OSError, ValueError, struct.error, ET.ParseError) as e:
            print(f"{f}：跳过（{e}）")
            continue
        s = r.session
        how = "按 .ldx 过线时间切圈" if r.lap_source == "ldx" else "没有 .ldx，按 TIME 通道切圈（圈时误差约 20 ms）"
        print(f"{f}：{s.car} @ {s.track}，{len(r.laps)} 圈，{how}")
        if s.car not in CARS:
            print("  注意：没认出 ACC 车型，和实时采集的圈对不上；可以用 --car 指定")
        for lap in r.laps:
            tags = [t for t, on in (("干净", lap.is_clean), ("进站", lap.is_in_lap), ("不完整", not lap.is_complete)) if on]
            print(f"  第 {lap.lap_number} 圈 {format_lap_time(lap.lap_time_ms)}  {lap.track_length_m:.0f} m  {' '.join(tags)}")
        if storage is None:
            continue
        if r.laps and storage.has_session(r.laps[0].session_uid):
            print("  这个文件已经导入过，跳过")
            continue
        for lap in r.laps:
            storage.save_lap(lap)
        print(f"  已保存 {len(r.laps)} 圈")


if __name__ == "__main__":
    main()
