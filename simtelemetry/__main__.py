"""命令行入口。

    python -m simtelemetry                    # 采集（自动识别游戏）+ Web 面板，监听 0.0.0.0:8000
    python -m simtelemetry --game acc         # 指定游戏：acc / ac / iracing / sim（模拟数据）
    python -m simtelemetry --no-collect       # 只开面板看历史数据
    python -m simtelemetry --no-web           # 只采集，不开网页
"""
from __future__ import annotations

import argparse
import logging
import signal
import socket
import threading
from pathlib import Path

from .adapters import GAMES
from .collector import Collector
from .live import LiveHub
from .storage import Storage

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "telemetry.db"


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
    p.add_argument("--no-process-check", action="store_true",
                   help="连接前不检查游戏进程（配合 tools/fake_acc.py 测试；ACC/AC 同名映射，平时别用）")
    p.add_argument("--no-collect", action="store_true", help="不采集，只提供网页")
    p.add_argument("--no-web", action="store_true", help="只采集，不提供网页")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("simtelemetry")

    storage = Storage(args.db)
    hub = LiveHub()
    collector = None if args.no_collect else Collector(
        storage, hub, game=args.game, rate_hz=args.rate, sim_speed=args.sim_speed,
        check_process=not args.no_process_check)
    log.info("数据库：%s", Path(args.db).resolve())

    if args.no_web:
        if collector is None:
            p.error("--no-web 和 --no-collect 不能同时使用")
        collector.start()
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        log.info("只采集模式，按 Ctrl+C 退出")
        while not stop.wait(0.5):
            pass
        collector.stop()
        return

    import uvicorn

    from .server import create_app

    app = create_app(storage, hub, collector, ws_hz=args.ws_hz)
    log.info("圈对比：http://127.0.0.1:%d/   实时面板：http://127.0.0.1:%d/live", args.port, args.port)
    if args.host in ("0.0.0.0", "::"):
        for ip in lan_addresses():
            log.info("手机（同一局域网）打开：http://%s:%d/live", ip, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
