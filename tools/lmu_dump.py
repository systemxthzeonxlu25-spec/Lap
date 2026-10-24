"""把 Le Mans Ultimate 的 “LMU_Data” 共享内存原样存成文件（只读，不改游戏的数据）。

    python tools/lmu_dump.py                 # 存一帧到 lmu_dump_<时间>.bin
    python tools/lmu_dump.py -n 50 -i 0.1    # 每 0.1 s 存一帧，共 50 帧（连续写进同一个文件）

LMU 数据看起来不对（日志里出现 “LMU 数据看起来不对”）时，开着游戏在赛道上跑一下，把生成的文件反馈过来，
就能离线复现：tools/fake_lmu.BufferPage(文件内容的一帧) 可以直接喂给 LMUAdapter。
每帧大小 = sizeof(LMUObjectOut)；文件里可能有赛道名、车名、玩家名和服务器名。
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simtelemetry.adapters.lmu_structs import LMU_MAP, LMUObjectOut  # noqa: E402
from simtelemetry.adapters.winshm import SharedMemoryPage  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("-n", type=int, default=1, help="帧数")
    p.add_argument("-i", type=float, default=0.1, help="两帧间隔（秒）")
    p.add_argument("-o", help="输出文件")
    args = p.parse_args()
    page = SharedMemoryPage(LMU_MAP, LMUObjectOut)
    if not page.open():
        raise SystemExit("打不开 LMU_Data：游戏没开，或者没在 设置 → Gameplay 打开 “Enable Plugins”")
    out = Path(args.o or time.strftime("lmu_dump_%Y%m%d_%H%M%S.bin"))
    try:
        with out.open("wb") as f:
            for k in range(max(1, args.n)):
                f.write(bytes(page.read_at(LMUObjectOut, 0)))
                if k + 1 < args.n:
                    time.sleep(args.i)
    finally:
        page.close()
    print(f"已保存 {args.n} 帧（每帧 {ctypes.sizeof(LMUObjectOut)} 字节）到 {out}")


if __name__ == "__main__":
    main()
