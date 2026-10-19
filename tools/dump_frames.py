"""把适配器读到的逐帧数据原样录成 CSV，用来实车核对还没确认的细节：

- 转向正负：直道上往左打一下方向，看 steer 是正还是负（本项目约定 + = 左）；
- 挡位编码：挂倒挡 / 空挡，看 gear 是不是 -1 / 0；
- 跨线时序：过起终点线前后几帧 lap_count / lap_time_ms / last_lap_ms / pos 的变化；
- iRacing：从维修区出发第一次过线时 lap_count（LapCompleted）有没有 +1。

    python tools/dump_frames.py --game acc --seconds 120            # 写到 data/dump_acc_<时间>.csv
    python tools/dump_frames.py --game iracing --only-changes       # 只记录 tick 变化的帧
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from simtelemetry.adapters import GAMES, create_adapter  # noqa: E402
from simtelemetry.models import TelemetryFrame  # noqa: E402

FRAME_FIELDS = [f.name for f in fields(TelemetryFrame)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="逐帧录制适配器输出（调试用）")
    ap.add_argument("--game", required=True, choices=GAMES)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--rate", type=float, default=50.0)
    ap.add_argument("--out", help="CSV 路径（默认 data/dump_<game>_<时间>.csv）")
    ap.add_argument("--only-changes", action="store_true", help="tick 没变的帧不写")
    ap.add_argument("--no-process-check", action="store_true")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else \
        ROOT / "data" / f"dump_{args.game}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    adapter = create_adapter(args.game, check_process=not args.no_process_check)
    print(f"等待 {adapter.display_name}…（Ctrl+C 退出）")
    try:
        while not adapter.connect():
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 1
    print(f"已连接，录制 {args.seconds:.0f} 秒 → {out}")

    period = 1.0 / args.rate
    n = 0
    last_tick = None
    t0 = time.monotonic()
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t_s", "track", "car", "session_type", "track_length_m", *FRAME_FIELDS])
        try:
            while (now := time.monotonic()) - t0 < args.seconds:
                res = adapter.read()
                if res is not None:
                    session, f = res
                    if not (args.only_changes and f.tick == last_tick):
                        last_tick = f.tick
                        s = session
                        w.writerow([f"{now - t0:.3f}", s.track if s else "", s.car if s else "",
                                    s.session_type if s else "", s.track_length_m if s else "",
                                    *(getattr(f, k) for k in FRAME_FIELDS)])
                        n += 1
                time.sleep(max(0.0, period - (time.monotonic() - now)))
        except KeyboardInterrupt:
            pass
        finally:
            adapter.close()
    print(f"写了 {n} 帧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
