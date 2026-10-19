"""采集线程 → WebSocket 的实时数据中转。

采集线程以 50 Hz 调 update()；每个 WebSocket 连接按自己的节奏（默认 20 Hz）读 snapshot()，
只在 seq 变化时推送。快照是 dict，每次整体替换引用，读方拿到的永远是完整的一份。
"""
from __future__ import annotations

import threading
import time


class LiveHub:
    def __init__(self) -> None:
        self._snap: dict = {"seq": 0, "state": "starting"}
        self._seq = 0
        self._lock = threading.Lock()

    def update(self, **fields) -> None:
        with self._lock:
            self._seq += 1
            snap = dict(self._snap)
            snap.update(fields)
            snap["seq"] = self._seq
            snap["ts"] = time.time()
            self._snap = snap

    def replace(self, **fields) -> None:
        """整体替换（断开 / 切换游戏时用，避免残留上一局的数据）。"""
        with self._lock:
            self._seq += 1
            self._snap = {**fields, "seq": self._seq, "ts": time.time()}

    def snapshot(self) -> dict:
        return self._snap
