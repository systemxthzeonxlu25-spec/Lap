"""录制 / 回放：回放切出来的圈和实时采集的完全一样；截断的文件能放到截断处；文件头 / 版本 / 命令行参数检查。"""
from __future__ import annotations

import gzip
import json
import math
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from simtelemetry import __main__ as cli
from simtelemetry import replay as replay_mod
from simtelemetry.adapters import acc as acc_mod
from simtelemetry.adapters import lmu as lmu_mod
from simtelemetry.adapters.acc import ACCAdapter
from simtelemetry.adapters.base import AdapterDisconnected
from simtelemetry.adapters.lmu import LMUAdapter
from simtelemetry.adapters.simulated import SimulatedAdapter, SyntheticDriver
from simtelemetry.collector import Collector
from simtelemetry.live import LiveHub
from simtelemetry.recorder import LapRecorder
from simtelemetry.recording import MAGIC, Recording, RecordingError, RecordingReader
from simtelemetry.replay import ReplayAdapter, replay_laps
from simtelemetry.storage import Storage
from tools.fake_acc import encode
from tools.fake_lmu import FakeLMU

DT = 0.02
ROOT = Path(__file__).resolve().parent.parent


def lap_key(lap) -> tuple:
    if isinstance(lap, dict):                    # 数据库里读出来的
        get = lap.__getitem__
    else:
        get = lambda k: getattr(lap, k)          # noqa: E731
    return (get("lap_number"), get("lap_time_ms"), get("time_source"), bool(get("is_valid")),
            bool(get("is_out_lap")), bool(get("is_in_lap")), bool(get("is_complete")), get("sample_count"))


def db_laps(storage: Storage) -> list[tuple]:
    rows = [lap for c in storage.list_combos() for lap in storage.list_laps(c["game"], c["track"], c["car"])]
    return [lap_key(r) for r in sorted(rows, key=lambda r: r["id"])]


class _Page:
    """假的 ACC 共享内存页。"""
    struct = None

    def open(self) -> bool:
        return True

    def read(self):
        return self.struct

    def close(self) -> None:
        pass


def record_synthetic(path: Path, game: str, minutes: float, raw: bool = True) -> list[tuple]:
    """固定步长跑合成车手（ACC 用 fake_acc.encode，LMU 用 FakeLMU），边录边按采集线程的规则切圈，返回切出的圈。"""
    drv = SyntheticDriver(seed=3, laps_between_stops=2)
    if game == "acc":
        pages = [_Page(), _Page(), _Page()]
        adapter = ACCAdapter(*pages)
        tick = [0]

        def step():
            tick[0] += 1
            for pg, s in zip(pages, encode(drv.step(DT), tick[0])):
                pg.struct = s
    else:
        fake = FakeLMU(scoring_every=10)
        adapter = LMUAdapter(fake.page)

        def step():
            fake.write(drv.step(DT), DT)

    t = [1000.0]
    rec = Recording(path, raw=raw, clock=lambda: t[0])
    a = rec.wrap(adapter)
    assert a.connect() and a.raw == raw
    lr, laps = LapRecorder(), []
    for _ in range(int(minutes * 60 / DT)):
        step()
        t[0] += DT
        now = rec.clock()
        res = a.read()
        if res is None or res[0] is None:
            laps += lr.flush(now)
        else:
            laps += lr.feed(*res, now)
    a.close()
    laps += lr.reset("采集停止")
    rec.close()
    return [lap_key(lap) for lap in laps]


@pytest.fixture(scope="module")
def recordings(tmp_path_factory):
    """每个游戏录一次 7 分钟的 raw 录制，模块里共用：game → (文件, 实时切出的圈)。"""
    d, cache = tmp_path_factory.mktemp("rec"), {}

    def get(game: str):
        if game not in cache:
            cache[game] = (d / f"{game}.strec", record_synthetic(d / f"{game}.strec", game, 7))
        return cache[game]
    return get


@pytest.fixture(scope="module")
def acc_raw(recordings):
    return recordings("acc")


# ---------------------------------------------------------------- 一致性
class FlakySim(SimulatedAdapter):
    """合成数据；跑到一半断开一次（采集线程会丢掉未完成的圈，回放必须在同一处丢掉）。"""

    def __init__(self):
        super().__init__(speed=60)
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.reads == 1500:
            raise AdapterDisconnected("模拟断开")
        return super().read()


def test_frames_mode_collector_live_vs_replay(tmp_path):
    rec = Recording(tmp_path / "sim.strec")
    live_db = Storage(tmp_path / "live.db")
    sim = FlakySim()
    col = Collector(live_db, LiveHub(), game="sim", rate_hz=500, retry_s=0.05, adapters=lambda: [sim],
                    wrap=rec.wrap, clock=rec.clock)
    col.start()
    try:
        deadline = time.monotonic() + 20
        while len(live_db.list_combos()) == 0 or live_db.list_combos()[0]["laps"] < 2:
            assert time.monotonic() < deadline, "20 秒内没跑出两圈"
            time.sleep(0.1)
    finally:
        col.stop()
        rec.close()
    live = db_laps(live_db)
    assert len(live) >= 2 and sim.reads > 1500

    r = replay_laps(tmp_path / "sim.strec")
    assert [lap_key(lap) for lap in r.laps] == live
    assert r.header["game"] == "sim" and r.header["mode"] == "frames" and not r.truncated

    # 同一个文件当数据源接进采集线程（尽快回放），存出来的圈也一样
    rp = ReplayAdapter(tmp_path / "sim.strec", speed=None)
    rp_db = Storage(tmp_path / "rp.db")
    col = Collector(rp_db, LiveHub(), game="replay", rate_hz=math.inf, retry_s=0.05, adapters=lambda: [rp],
                    clock=rp.clock)
    col.start()
    try:
        deadline = time.monotonic() + 20
        while not rp.finished:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        rp.cancel()
        col.stop()
    assert db_laps(rp_db) == live


@pytest.mark.parametrize("game", ["acc", "lmu"])
def test_raw_mode_replays_through_real_translate(game, recordings, monkeypatch):
    path, live = recordings(game)
    assert len(live) >= 3 and any(k[4] for k in live) and any(k[5] for k in live)   # 有出站圈和进站圈

    mod = acc_mod if game == "acc" else lmu_mod
    real, calls = mod.translate, []

    def counting(*a):
        calls.append(1)
        return real(*a)

    monkeypatch.setattr(mod, "translate", counting)
    r = replay_laps(path)
    assert r.header["mode"] == "raw" and r.header["game"] == game
    assert [lap_key(lap) for lap in r.laps] == live
    assert len(calls) == r.frames == 7 * 60 / DT

    # 模拟一个字段映射 bug：回放用的是当前代码的 translate()，所以能看出差别
    def buggy(*a):
        res = real(*a)
        if res is not None:
            res[1].in_pit = False                      # 比如把维修区标记读错了位置
        return res

    monkeypatch.setattr(mod, "translate", buggy)
    assert [lap_key(lap) for lap in replay_laps(path).laps] != live


def test_original_timing_and_speed(acc_raw, tmp_path):
    src, _ = acc_raw
    rp = ReplayAdapter(src, speed=100)                    # 前 3 秒的数据，100 倍速 ≈ 0.03 s
    assert rp.connect()
    t0, first = time.monotonic(), rp.clock()
    while rp.clock() - first < 3:
        rp.read()
    assert 0.02 < time.monotonic() - t0 < 1.0
    assert rp.name == "acc" and "回放" in rp.display_name


# ---------------------------------------------------------------- 截断 / 损坏
def test_truncated_file_replays_up_to_cut(acc_raw, tmp_path):
    src, live = acc_raw
    full = replay_laps(src)
    data = src.read_bytes()
    cut = tmp_path / "cut.strec"
    cut.write_bytes(data[: len(data) * 6 // 10])
    r = replay_laps(cut)
    assert r.truncated and 0 < r.frames < full.frames
    got = [lap_key(lap) for lap in r.laps]
    assert len(got) >= 2
    assert got[:-1] == live[:len(got) - 1]                 # 最后一圈可能在截断时还在等圈时


def test_cut_in_the_middle_of_a_record(acc_raw, tmp_path):
    src, _ = acc_raw
    raw = gzip.decompress(src.read_bytes())
    n_full = sum(1 for _ in RecordingReader(src))
    pos, count = len(MAGIC), 0
    while pos < len(raw) * 0.4:                            # 找到 40% 附近一条记录的开头，再往里切 7 字节
        size = struct.unpack_from("<I", raw, pos + 1)[0]
        pos += 5 + size
        count += 1
    p = tmp_path / "mid.strec"
    p.write_bytes(gzip.compress(raw[:pos + 7]))
    reader = RecordingReader(p)
    events = list(reader)
    assert reader.truncated and len(events) == count - 1 < n_full     # 文件头不算在事件里


def test_killed_process_leaves_readable_file(tmp_path):
    path = tmp_path / "killed.strec"
    code = f"""
import sys, time
sys.path[:0] = [{str(ROOT)!r}]
from simtelemetry.adapters.simulated import SimulatedAdapter
from simtelemetry.recording import Recording
rec = Recording({str(path)!r}, flush_every_s=0.05)
a = rec.wrap(SimulatedAdapter(speed=20))
a.connect()
n = 0
while True:
    rec.clock(); a.read(); n += 1
    if n == 2000:
        print("ready", flush=True)
    time.sleep(0.0005)
"""
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(0.2)
    finally:
        proc.kill()                                       # 相当于任务管理器强制结束
        proc.wait(10)
    r = replay_laps(path)
    assert r.truncated and r.frames >= 2000 and r.header["game"] == "sim"


# ---------------------------------------------------------------- 文件头
def _write_stream(path: Path, header: dict | None, magic: bytes = MAGIC) -> None:
    body = magic
    if header is not None:
        data = struct.pack("<d", 0.0) + json.dumps(header).encode()
        body += b"H" + struct.pack("<I", len(data)) + data
    path.write_bytes(gzip.compress(body))


def test_bad_files_give_clear_errors(tmp_path):
    p = tmp_path / "x.strec"
    p.write_text("hello")
    with pytest.raises(RecordingError, match="不是 simtelemetry 录制文件"):
        ReplayAdapter(p)
    _write_stream(p, {"format": 1}, magic=b"NOTTHIS\n")
    with pytest.raises(RecordingError, match="不是 simtelemetry 录制文件"):
        RecordingReader(p)
    _write_stream(p, {"format": 99, "app_version": "9.0"})
    with pytest.raises(RecordingError, match="格式版本是 99"):
        replay_laps(p)
    p.unlink()
    Recording(p).close()                                  # 游戏一直没连上：只有魔数
    with pytest.raises(RecordingError, match="没有文件头"):
        RecordingReader(p)


def test_recording_refuses_to_overwrite(tmp_path):
    p = tmp_path / "x.strec"
    p.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        Recording(p)
    assert p.read_bytes() == b"precious"


def test_frames_mode_for_adapter_without_pages(tmp_path):
    rec = Recording(tmp_path / "s.strec", raw=True)
    a = rec.wrap(SimulatedAdapter())
    assert not a.raw and a.connect()
    a.read()
    a.close()
    rec.close()
    assert RecordingReader(tmp_path / "s.strec").header["mode"] == "frames"


# ---------------------------------------------------------------- 命令行
@pytest.mark.parametrize("argv", [
    ["--record", "{tmp}/new.strec", "--no-collect"],
    ["--replay", "{rec}", "--no-collect"],
    ["--record", "{tmp}/new.strec", "--replay", "{rec}"],
    ["--record-raw", "--no-web"],
    ["--replay-fast", "--no-web"],
    ["--replay-speed", "2", "--no-web"],
    ["--replay", "{rec}", "--replay-speed", "0", "--no-web"],
    ["--replay", "{rec}", "--replay-speed", "2", "--replay-fast"],
    ["--replay", "{rec}", "--game", "sim", "--no-web"],
    ["--replay", "{tmp}/missing.strec", "--no-web"],
    ["--replay", "{tmp}/bad.strec", "--no-web"],
    ["--record", "{rec}", "--game", "sim", "--no-web"],          # 已存在：不覆盖
])
def test_cli_rejects_bad_record_replay_args(argv, acc_raw, tmp_path):
    (tmp_path / "bad.strec").write_text("not a recording")
    argv = [a.format(tmp=tmp_path, rec=acc_raw[0]) for a in argv]
    with pytest.raises(SystemExit) as e:
        cli.main([*argv, "--db", str(tmp_path / "x.db")])
    assert e.value.code == 2
    assert not (tmp_path / "new.strec").exists()


def test_cli_replay_no_web_saves_laps(acc_raw, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)          # 别改掉 pytest 的 Ctrl+C 处理
    src, live = acc_raw
    db = tmp_path / "cli.db"
    cli.main(["--replay", str(src), "--replay-fast", "--no-web", "--db", str(db), "--log-level", "WARNING"])
    assert db_laps(Storage(db)) == live


def test_cli_record_no_web(tmp_path, monkeypatch):
    handlers = []
    monkeypatch.setattr(cli.signal, "signal", lambda sig, h: handlers.append(h))
    timer = threading.Timer(0.6, lambda: handlers[0]())          # 0.6 s 后相当于按了 Ctrl+C
    timer.start()
    out = tmp_path / "cli.strec"
    try:
        cli.main(["--game", "sim", "--sim-speed", "20", "--record", str(out), "--no-web",
                  "--db", str(tmp_path / "c.db"), "--log-level", "WARNING"])
    finally:
        timer.cancel()
    reader = RecordingReader(out)
    assert reader.header["game"] == "sim" and reader.header["app_version"]
    events = list(reader)
    assert not reader.truncated and events[-1].kind == "close"
    assert sum(e.kind == "frame" for e in events) > 5


def test_replay_tool_dry_run_and_db(acc_raw, tmp_path, capsys):
    src, live = acc_raw
    replay_mod.main([str(src), "--dry-run"])
    out = capsys.readouterr().out
    assert "acc，raw 模式" in out and "第 1 圈" in out and "出站" in out and f"共 {len(live)} 圈" in out
    db = tmp_path / "tool.db"
    replay_mod.main([str(src), "--db", str(db)])
    replay_mod.main([str(src), "--db", str(db)])
    assert "已经存进" in capsys.readouterr().out
    assert db_laps(Storage(db)) == live
    with pytest.raises(SystemExit):
        replay_mod.main([str(tmp_path / "missing.strec")])
