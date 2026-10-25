"""MoTeC .ld 导入：用合成车手生成一个最小的 .ld（布局同 ldparser），不依赖真实文件。"""
from __future__ import annotations

import struct

import pytest

from simtelemetry.adapters.simulated import SyntheticDriver
from simtelemetry.motec import _CHAN, _HEAD, LdFile, guess_acc_car, import_acc_ld, main
from simtelemetry.storage import Storage


def write_ld(path, channels: dict[str, tuple[int, list[float]]], vehicle="296 GT3", venue="cota"):
    names = list(channels)
    meta0 = _HEAD.size
    data_ptr = meta0 + _CHAN.size * len(names)
    head = _HEAD.pack(0x40, meta0, data_ptr, 0, 1, 0x4240, 0xF, 0, b"ADL", 420, 0, len(names),
                      b"23/05/2024", b"19:25:56", b"", vehicle.encode(), venue.encode())
    metas, blobs = [], []
    ptr = data_ptr
    for i, name in enumerate(names):
        freq, values = channels[name]
        blob = struct.pack(f"<{len(values)}f", *values)
        nxt = meta0 + _CHAN.size * (i + 1) if i < len(names) - 1 else 0
        metas.append(_CHAN.pack(0, nxt, ptr, len(values), 0, 0x07, 4, freq, 0, 1, 1, 0, name.encode(), b"", b""))
        blobs.append(blob)
        ptr += len(blob)
    path.write_bytes(head + b"".join(metas) + b"".join(blobs))


def drive_channels(seconds: float):
    """60 Hz 跑合成车手，生成 ACC 导出里的几个通道；返回 (通道, 每圈圈时, 过线时刻)。"""
    drv = SyntheticDriver(seed=4, laps_between_stops=99)
    hz, n = 60, int(seconds * 60)
    speed, thr, brk, steer, gear, rpm, lap_t, crossings, lap_ms = [], [], [], [], [], [], [], [], []
    last_count = 0
    for i in range(n):
        s = drv.step(1 / hz)
        if s.lap_count != last_count:
            crossings.append(i / hz - s.lap_time_ms / 1000)
            lap_ms.append(s.last_lap_ms)
            last_count = s.lap_count
        speed.append(s.speed_kmh / 3.6)
        thr.append(s.throttle * 100)
        brk.append(s.brake * 100)
        steer.append(s.steer * 400)
        gear.append(float(s.gear))
        rpm.append(float(s.rpm))
    t50 = [k / 50 for k in range(int(seconds * 50))]
    lap_time = []
    for t in t50:
        prev = max([c for c in crossings if c <= t], default=-drv.lap_time)    # 第一段：从日志开始算
        lap_time.append(t - prev if crossings and t >= crossings[0] else t + 30.0)
    chans = {"SPEED": (60, speed), "THROTTLE": (60, thr), "BRAKE": (60, brk), "STEERANGLE": (60, steer),
             "GEAR": (20, gear[::3]), "RPMS": (60, rpm), "TIME": (50, lap_time), "LAP_BEACON": (100, [0.0] * 10)}
    return chans, lap_ms, crossings


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    d = tmp_path_factory.mktemp("motec")
    chans, lap_ms, crossings = drive_channels(60 * 7)
    path = d / "run.ld"
    write_ld(path, chans)
    return path, lap_ms, crossings


def test_reads_header_and_channels(sample):
    path, _, _ = sample
    ld = LdFile.read(path)
    assert ld.vehicle == "296 GT3" and ld.venue == "cota" and ld.started.year == 2024
    assert ld.channels["SPEED"].freq == 60 and len(ld.data("TIME")) == 7 * 60 * 50


def test_split_by_time_channel(sample):
    path, lap_ms, _ = sample
    r = import_acc_ld(path)
    assert r.lap_source == "time" and r.session.car == "ferrari_296_gt3" and r.session.track == "cota"
    clean = [l for l in r.laps if l.is_clean]
    assert len(clean) >= 2
    for lap in r.laps:
        game = lap_ms[lap.lap_number]                     # 第 k 段结束于第 k+1 次过线
        assert abs(lap.lap_time_ms - game) <= 25          # 没有 .ldx：误差约一个 TIME 采样
        assert lap.time_source == "estimated"
    for lap in clean:
        assert abs(lap.track_length_m - 4200) < 4200 * 0.01
        assert lap.samples["pos"][0] == 0 and lap.samples["pos"][-1] > 0.99
        assert max(abs(x) for x in lap.samples["steer"]) <= 1


def test_split_by_ldx_is_exact(sample, tmp_path):
    path, lap_ms, crossings = sample
    ld = tmp_path / "run.ld"
    ld.write_bytes(path.read_bytes())
    markers = "".join(f'<Marker Version="100" ClassName="BCN" Name="Manual.{i}" Flags="77" Time="{c * 1e6:.6f}"/>'
                      for i, c in enumerate(crossings))
    (tmp_path / "run.ldx").write_text(
        '<?xml version="1.0"?><LDXFile><Layers><Layer><MarkerBlock><MarkerGroup Name="Beacons" Index="3">'
        f"{markers}</MarkerGroup></MarkerBlock></Layer></Layers></LDXFile>", encoding="utf-8")
    r = import_acc_ld(ld)
    assert r.lap_source == "ldx"
    for lap in r.laps:
        assert abs(lap.lap_time_ms - lap_ms[lap.lap_number]) <= 1 and lap.time_source == "game"


def test_flags_stopped_and_merged_laps(sample, tmp_path):
    path, _, _ = sample
    ld = LdFile.read(path)
    chans = {name: (ch.freq, ld.data(name)) for name, ch in ld.channels.items()}
    t = chans["TIME"][1]
    cuts = [i for i in range(1, len(t)) if t[i] < t[i - 1] - 1]
    merged = list(t)
    k = cuts[2]                               # 第三次过线时 TIME 没归零：两圈连成一段
    for i in range(k, cuts[3]):
        merged[i] = t[k - 1] + (i - k + 1) / 50
    chans["TIME"] = (50, merged)
    out = tmp_path / "merged.ld"
    write_ld(out, chans)
    r = import_acc_ld(out)
    long_lap = max(r.laps, key=lambda l: l.lap_time_ms)
    assert not long_lap.is_complete and not long_lap.is_clean


def test_guess_car():
    assert guess_acc_car("296 GT3") == "ferrari_296_gt3"
    assert guess_acc_car("992 GT3 R") == "porsche_992_gt3_r"
    assert guess_acc_car("") is None and guess_acc_car("Some Kart") is None


def test_cli_imports_once(sample, tmp_path, capsys):
    path, _, _ = sample
    db = tmp_path / "m.db"
    main([str(path), "--db", str(db)])
    main([str(path), "--db", str(db)])
    out = capsys.readouterr().out
    assert "已保存" in out and "已经导入过" in out
    laps = Storage(db).list_laps("acc", "cota", "ferrari_296_gt3")
    assert laps and all(l["session_type"] == "motec" for l in laps)


def test_rejects_garbage(tmp_path):
    bad = tmp_path / "x.ld"
    bad.write_bytes(b"not a motec file")
    with pytest.raises(ValueError):
        import_acc_ld(bad)
