"""命令行入口。

    python -m simtelemetry                    # 采集（自动识别游戏）+ Web 面板，监听 0.0.0.0:8000
    python -m simtelemetry --game acc         # 指定游戏：acc / ac / iracing / sim（模拟数据）
    python -m simtelemetry --no-collect       # 只开面板看历史数据
    python -m simtelemetry --no-web           # 只采集，不开网页
    python -m simtelemetry --record x.strec   # 采集的同时录制（反馈问题用，见 recording.py；--record-raw 存原始字节）
    python -m simtelemetry --replay x.strec   # 用录制文件代替游戏（--replay-speed N 倍速 / --replay-fast 尽快）
"""
from __future__ import annotations

import argparse
import logging
import math
import signal
import socket
import threading
import time
from pathlib import Path

from .adapters import GAMES, is_implemented
from .collector import Collector
from .live import LiveHub
from .recording import PRIVACY_NOTE, Recording, RecordingError
from .replay import ReplayAdapter
from .setups.library import SetupLibrary, default_acc_setups_dir, default_lmu_setups_dir
from .storage import DEFAULT_DB, Storage


def lan_addresses() -> list[str]:
    addrs: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))       # 不会真的发包，只是让系统选出默认网卡的地址
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="simtelemetry", description="模拟赛车遥测：采集 + 圈对比 + 实时面板")
    p.add_argument("--game", default="auto", choices=("auto", *GAMES),
                   help="auto = 按进程自动识别；sim = 合成数据（不开游戏试用面板）")
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite 文件（默认 {DEFAULT_DB}）")
    p.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = 允许局域网内手机访问")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--rate", type=float, default=50.0, help="采样频率 Hz")
    p.add_argument("--ws-hz", type=float, default=20.0, help="实时面板推送频率 Hz")
    p.add_argument("--sim-speed", type=float, default=1.0, help="--game sim 时的时间倍速")
    p.add_argument("--no-collect", action="store_true", help="不采集，只提供网页")
    p.add_argument("--no-web", action="store_true", help="只采集，不提供网页")
    p.add_argument("--acc-setups", metavar="DIR",
                   help="ACC 调教目录（默认自动找“文档/Assetto Corsa Competizione/Setups”）")
    p.add_argument("--lmu-setups", metavar="DIR",
                   help="LMU 调教目录（默认在 Steam 库里找“Le Mans Ultimate/UserData/player/Settings”）")
    p.add_argument("--no-setups", action="store_true", help="不扫描调教目录")
    p.add_argument("--record", metavar="FILE", help="采集的同时把每帧数据录进 FILE（反馈问题、离线复现用；不覆盖已有文件）")
    p.add_argument("--record-raw", action="store_true",
                   help="录制共享内存原始字节（ACC / LMU），字段映射的问题也能复现；文件更大，里面可能有玩家名/服务器名")
    p.add_argument("--replay", metavar="FILE", help="用录制文件代替游戏当数据源（圈存进 --db，建议另用一个数据库）")
    speed = p.add_mutually_exclusive_group()
    speed.add_argument("--replay-speed", type=float, metavar="N", help="回放倍速（默认 1 = 按录制时的节奏）")
    speed.add_argument("--replay-fast", action="store_true", help="回放时不等待，尽快放完")
    p.add_argument("--log-level", default="INFO", type=str.upper,
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = p.parse_args(argv)

    if args.no_web and args.no_collect:
        p.error("--no-web 和 --no-collect 不能同时使用")
    if args.rate <= 0 or args.ws_hz <= 0 or args.sim_speed <= 0:
        p.error("--rate / --ws-hz / --sim-speed 必须大于 0")
    if args.no_collect and (args.record or args.replay):
        p.error("--record / --replay 需要采集，不能和 --no-collect 一起用")
    if args.record and args.replay:
        p.error("--record 和 --replay 不能同时使用")
    if args.record_raw and not args.record:
        p.error("--record-raw 要和 --record 一起用")
    if (args.replay_speed is not None or args.replay_fast) and not args.replay:
        p.error("--replay-speed / --replay-fast 要和 --replay 一起用")
    if args.replay_speed is not None and not (0 < args.replay_speed < math.inf):
        p.error("--replay-speed 必须大于 0")
    if args.replay and args.game != "auto":
        p.error("--replay 时游戏由录制文件决定，不能再指定 --game")
    if args.record and Path(args.record).exists():
        p.error(f"{args.record} 已存在，换个文件名（不会覆盖已有的录制）")
    if not args.no_collect and not args.replay and args.game != "auto" and not is_implemented(args.game):
        p.error(f"{args.game} 的适配器还没有实现（目前可用：sim、auto）")
    replay = None
    if args.replay:
        try:
            replay = ReplayAdapter(args.replay, speed=None if args.replay_fast else (args.replay_speed or 1.0))
        except (OSError, RecordingError) as e:
            p.error(f"--replay：{e}")

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("simtelemetry")

    storage = Storage(args.db)
    hub = LiveHub()
    recording = None
    if args.no_collect:
        collector = None
    elif replay is not None:
        h = replay.header
        log.info("回放 %s：%s，%s 模式，录于 %s（simtelemetry %s）", args.replay, h.get("game"), h.get("mode"),
                 h.get("created_at"), h.get("app_version"))
        collector = Collector(storage, hub, game="replay", rate_hz=math.inf, adapters=lambda: [replay],
                              clock=replay.clock)
    else:
        if args.record:
            try:
                recording = Recording(args.record, raw=args.record_raw)
            except OSError as e:
                p.error(f"--record：{e}")
            log.info("录制到 %s（%s 模式）。注意：%s", args.record, "raw" if args.record_raw else "frames", PRIVACY_NOTE)
        collector = Collector(storage, hub, game=args.game, rate_hz=args.rate, sim_speed=args.sim_speed,
                              wrap=recording.wrap if recording else None,
                              clock=recording.clock if recording else time.monotonic)
    log.info("数据库：%s", Path(args.db).resolve())
    try:
        _serve(args, log, storage, hub, collector, replay)
    finally:
        if replay is not None:
            replay.cancel()
        if collector is not None and (replay or recording):
            collector.stop()            # 网页模式下 lifespan 已经停过一次，再停一次无妨
        if recording is not None:
            recording.close()
            log.info("录制已保存：%s（%d 帧）", args.record, recording.frames)


def _serve(args, log, storage, hub, collector, replay) -> None:
    if args.no_web:
        assert collector is not None
        collector.start()
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        log.info("只采集模式，按 Ctrl+C 退出")
        while not stop.wait(0.5):
            if replay is not None and replay.finished:
                break
        collector.stop()
        return

    import uvicorn

    from .server import create_app

    setups = None
    if not args.no_setups:
        roots = {
            "acc": Path(args.acc_setups) if args.acc_setups else default_acc_setups_dir(),
            "lmu": Path(args.lmu_setups) if args.lmu_setups else default_lmu_setups_dir(),
        }
        for game, flag in (("acc", "--acc-setups"), ("lmu", "--lmu-setups")):
            if roots[game] is None:
                log.info("调教库：没找到 %s 调教目录，可以用 %s 指定", game.upper(), flag)
            else:
                log.info("调教库：扫描 %s（只读）", roots[game])
        roots = {g: d for g, d in roots.items() if d is not None}
        if roots:
            setups = SetupLibrary(storage, roots)

    app = create_app(storage, hub, collector, ws_hz=args.ws_hz, setups=setups)
    log.info("圈对比：http://127.0.0.1:%d/   实时面板：http://127.0.0.1:%d/live", args.port, args.port)
    if args.host in ("0.0.0.0", "::"):
        for ip in lan_addresses():
            log.info("手机（同一局域网）打开：http://%s:%d/live", ip, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
