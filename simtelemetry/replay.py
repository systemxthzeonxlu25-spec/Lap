"""回放录制文件（recording.py 写的）：代替游戏当数据源，或者不开网页直接切圈。

    python -m simtelemetry --replay acc.strec [--replay-speed 4 | --replay-fast]   # 当成“游戏”接进采集 + 网页
    python -m simtelemetry.replay acc.strec --dry-run                             # 维护者：只打印切出来的圈
    python -m simtelemetry.replay acc.strec --db repro.db                         # 存进数据库（同一文件不会重复存）

- 时间戳用录制时的，所以不管按原速、倍速还是尽快回放，切出来的圈（圈号、圈时、标记、采样数）都和当时一样。
- raw 模式的录制会重新走该游戏真正的 read() + translate()：改了 adapters/acc.py、lmu.py 之后回放一下就能验证。
- 录制时的断开 / 读取出错 / is_running() 结果也按原样重现（采集线程会在同样的地方丢弃未完成的圈）。
- ReplayAdapter 没有登记在适配器注册表里（不能 --game replay），只能用 --replay 选。
录制文件里可能有玩家名 / 服务器名（见 recording.py），本模块只读本地文件，不上传任何东西。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .adapters import create_adapter
from .adapters.base import AdapterDisconnected, GameAdapter
from .models import LapRecord, SessionInfo
from .recording import Event, RecordingError, RecordingReader
from .recorder import LapRecorder, format_lap_time
from .storage import DEFAULT_DB, Storage

log = logging.getLogger(__name__)

MAX_WAIT_S = 2.0          # 原速回放时两条记录之间最多等这么久（录制时游戏关着的那几分钟不用干等）
_UNTIMED = ("session", "running")


class _ReplayPage:
    """代替共享内存页：按顺序吐出这一帧录下的字节。适配器多读了没录过的东西时用上次的值，再没有就补 0。"""

    def __init__(self, name: str, struct_type=None):
        self.name, self.struct_type = name, struct_type
        self.queue: list[tuple[int, bytes]] = []
        self._last: dict[int, bytes] = {}
        self._warned: set = set()

    def open(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def read(self):
        return self.read_at(self.struct_type, -1)

    def read_at(self, struct_type, offset: int):
        size = ctypes.sizeof(struct_type)
        k = next((i for i, (off, _) in enumerate(self.queue) if off == offset), None)
        if k is not None:
            data = self.queue.pop(k)[1]
            self._last[offset] = data
        else:
            data = self._last.get(offset)
            if data is None:
                self._warn(("missing", offset), "回放：%s 偏移 %d 没有录到，按 0 处理", self.name, offset)
                data = b""
        if len(data) != size:
            self._warn(("size", offset), "回放：%s 偏移 %d 录的是 %d 字节，%s 现在是 %d 字节（结构体定义变了？），"
                       "多的截掉、少的补 0", self.name, offset, len(data), struct_type.__name__, size)
            data = data[:size].ljust(size, b"\0")
        return struct_type.from_buffer_copy(data)

    def _warn(self, key, msg, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)


class ReplayAdapter(GameAdapter):
    """把录制文件当成游戏。speed = 倍速（1 = 原速），None = 不等待、尽快回放。

    给 Collector 用时传 adapters=lambda: [replay] 和 clock=replay.clock（rate_hz 设成 inf，节拍由 clock() 决定）。
    """

    name = "replay"
    display_name = "回放"

    def __init__(self, path: str | Path, speed: float | None = 1.0):
        self.path = Path(path)
        self.reader = RecordingReader(self.path)             # 不是录制文件 / 版本不对：RecordingError
        self.header = self.reader.header
        self.name = self.header.get("game") or "replay"
        self.speed = speed
        self.frames = 0
        self.finished = False
        self.error: RecordingError | None = None           # 没法继续回放的原因（比如 raw 录制的游戏在这里没有适配器）
        self._events = iter(self.reader)
        self._ahead: deque[Event] = deque()
        self._cancel = threading.Event()
        self._session: SessionInfo | None = None
        self._inner: GameAdapter | None = None
        self._pages: list[_ReplayPage] = []
        self._segment = False
        self._ts = 0.0
        self._wall0: float | None = None
        self._ts0 = 0.0

    @property
    def truncated(self) -> bool:
        return self.reader.truncated

    # ------------------------------------------------------------ 事件队列
    def _peek(self, i: int = 0) -> Event | None:
        while len(self._ahead) <= i:
            ev = next(self._events, None)
            if ev is None:
                return None
            self._ahead.append(ev)
        return self._ahead[i]

    def _pop(self) -> Event | None:
        ev = self._peek()
        if ev is not None:
            self._ahead.popleft()
        return ev

    def cancel(self) -> None:
        """让正在等待的 clock() 立刻返回，之后 connect() 返回 False。"""
        self._cancel.set()

    def clock(self) -> float:
        """下一条记录录制时的时间戳；原速 / 倍速回放时先等到该放它的时候。"""
        i = 0
        while (ev := self._peek(i)) is not None and ev.kind in _UNTIMED:
            i += 1
        if ev is not None:
            self._ts = ev.ts
        if self.speed and ev is not None and not self._cancel.is_set():
            now = time.monotonic()
            if self._wall0 is None:
                self._wall0, self._ts0 = now, ev.ts
            wait = self._wall0 + (ev.ts - self._ts0) / self.speed - now
            if wait > MAX_WAIT_S:
                self._wall0 -= wait - MAX_WAIT_S
                wait = MAX_WAIT_S
            if wait > 0:
                self._cancel.wait(wait)
        return self._ts

    # ------------------------------------------------------------ 适配器接口
    def connect(self) -> bool:
        if self.finished or self._cancel.is_set():
            return False
        ev = self._peek()
        if ev is None:
            self._finish()
            return False
        try:
            if ev.kind == "connect":
                self._pop()
                self._start_segment(ev.data)
            elif not self._segment:                           # 没有连接记录（不应该出现）：按文件头当 frames
                self._start_segment({"game": self.name, "display_name": self.name, "mode": "frames", "pages": []})
        except RecordingError as e:
            self._fail(e)
            return False
        return True

    def _fail(self, e: RecordingError) -> None:
        log.error("无法继续回放 %s：%s", self.path, e)
        self.error = e
        self._finish()

    def _start_segment(self, d: dict) -> None:
        self._segment = True
        self.name = d.get("game") or self.name
        self.display_name = f"回放：{d.get('display_name') or self.name}（{self.path.name}）"
        self._session = None
        if self._inner is not None:
            self._inner.close()
        self._inner, self._pages = None, []
        if d.get("mode") != "raw":
            return
        try:
            inner = create_adapter(self.name)
        except (ValueError, NotImplementedError) as e:
            raise RecordingError(f"raw 录制需要 {self.name} 的适配器：{e}") from e
        names = list(d.get("pages") or [])
        by_name: dict[str, _ReplayPage] = {}

        def wrap(name, page):
            by_name[name] = _ReplayPage(name, getattr(page, "struct_type", None))
            return by_name[name]

        if not inner.wrap_pages(wrap):
            raise RecordingError(f"{self.name} 的适配器不支持 raw 回放")
        missing = [n for n in names if n not in by_name]
        if missing:
            raise RecordingError(f"录制文件里的页 {missing} 在当前 {self.name} 适配器里不存在")
        self._pages = [by_name[n] for n in names]
        inner.connect()
        self._inner = inner

    def read(self):
        while True:
            ev = self._pop()
            if ev is None:
                self._finish()
                raise AdapterDisconnected("回放结束")
            if ev.kind == "connect":                           # 没有断开记录就又连上了（不应该出现）
                try:
                    self._start_segment(ev.data)
                except RecordingError as e:
                    self._fail(e)
                    raise AdapterDisconnected(str(e)) from e
            elif ev.kind == "session":
                self._session = ev.data
            elif ev.kind == "close":
                if self._peek() is None:                       # 录制结束时那次正常的关闭
                    self._finish()
                    raise AdapterDisconnected("回放结束")
                raise AdapterDisconnected("录制时在这里断开过")
            elif ev.kind == "error":
                self._raise(ev)
            elif ev.kind in ("frame", "none", "raw"):
                break
        self.frames += 1
        if ev.kind == "none":
            res = None
        elif ev.kind == "frame":
            frame, has_session = ev.data
            res = (self._session if has_session else None, frame)
        else:
            if self._inner is None:
                self._fail(RecordingError("raw 记录前面没有 raw 模式的连接记录"))
                raise AdapterDisconnected("回放失败")
            for page in self._pages:
                page.queue.clear()
            for idx, off, data in ev.data:
                if idx < len(self._pages):
                    self._pages[idx].queue.append((off, data))
            try:
                res = self._inner.read()                       # 真正的 read() + translate()
            except Exception:
                nxt = self._peek()
                if nxt is not None and nxt.kind == "error":
                    self._pop()                                # 录制时也在这里出错了
                raise
        nxt = self._peek()
        if nxt is not None and nxt.kind == "error":
            self._pop()
            self._raise(nxt)
        return res

    @staticmethod
    def _raise(ev: Event):
        msg = ev.data.get("message", "")
        if ev.data.get("type") == "disconnected":
            raise AdapterDisconnected(msg)
        raise RuntimeError(f"录制时读取出错：{msg}")

    def _finish(self) -> None:
        if not self.finished:
            self.finished = True
            log.info("回放结束：%d 帧%s", self.frames, "（文件不完整，只放到截断处）" if self.truncated else "")

    def is_running(self) -> bool | None:
        ev = self._peek()
        if ev is not None and ev.kind == "running":
            self._pop()
            return ev.data
        return True                                            # 录制时没有问过：数据还在来，就当游戏还开着

    def close(self) -> None:
        ev = self._peek()
        if ev is not None and ev.kind == "close":
            self._pop()                                        # 和录制时那次断开对应
        if self._inner is not None:
            self._inner.close()


# ------------------------------------------------------------------ 不开网页直接切圈
@dataclass
class ReplayResult:
    header: dict
    laps: list[LapRecord]
    frames: int
    truncated: bool
    duration_s: float


def replay_laps(path: str | Path) -> ReplayResult:
    """尽快回放整个文件，按采集线程（collector.py）的同样规则喂给 LapRecorder，返回切出来的圈。"""
    rp = ReplayAdapter(path, speed=None)
    rec, laps = LapRecorder(), []
    first = last = None
    while rp.connect():
        while True:
            now = rp.clock()
            first = now if first is None else first
            last = now
            try:
                res = rp.read()
            except AdapterDisconnected as e:
                laps += rec.reset(str(e))
                break
            except Exception:
                log.exception("回放时读取出错，按断开处理")
                laps += rec.reset("读取出错")
                break
            if res is None or res[0] is None:
                laps += rec.flush(now)
            else:
                laps += rec.feed(res[0], res[1], now)
        rp.close()
    if rp.error is not None:
        raise rp.error
    laps += rec.reset("回放结束")
    return ReplayResult(rp.header, laps, rp.frames, rp.truncated, (last - first) if first is not None else 0.0)


def stable_session_uids(laps: list[LapRecord], path: str | Path) -> None:
    """session_uid 换成由文件内容决定的值：同一个文件存两次能认出来。"""
    digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()
    mapping: dict[str, str] = {}
    for lap in laps:
        if lap.session_uid not in mapping:
            mapping[lap.session_uid] = hashlib.sha1(f"{digest}:{len(mapping)}".encode()).hexdigest()[:12]
        lap.session_uid = mapping[lap.session_uid]


def lap_tags(lap: LapRecord) -> str:
    tags = [t for t, on in (("干净", lap.is_clean), ("无效", not lap.is_valid), ("出站", lap.is_out_lap),
                            ("进站", lap.is_in_lap), ("不完整", not lap.is_complete)) if on]
    return " ".join(tags)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m simtelemetry.replay",
                                description="回放录制文件（--record 录的），离线切圈：复现“某游戏切圈不对”的问题")
    p.add_argument("file", help="录制文件")
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"把切出来的圈存进这个数据库（默认 {DEFAULT_DB}，建议另用一个）")
    p.add_argument("--dry-run", action="store_true", help="只打印切出来的圈，不写数据库")
    p.add_argument("-v", "--verbose", action="store_true", help="显示切圈过程的日志（丢弃了哪圈、为什么）")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)-7s %(name)s: %(message)s")

    try:
        r = replay_laps(args.file)
    except (OSError, RecordingError) as e:
        raise SystemExit(f"{args.file}：{e}")
    h = r.header
    print(f"{args.file}：{h.get('game')}，{h.get('mode')} 模式，simtelemetry {h.get('app_version')}，"
          f"录于 {h.get('created_at')}；{r.frames} 帧，{r.duration_s / 60:.1f} 分钟")
    if r.truncated:
        print("  注意：文件不完整（录制被中断），只回放到截断处")
    session = None
    for lap in r.laps:
        if lap.session != session:
            session = lap.session
            print(f"  {session.game} / {session.track} / {session.car}（{session.session_type or '未知会话'}）")
        print(f"  第 {lap.lap_number} 圈 {format_lap_time(lap.lap_time_ms)}（{lap.time_source}）"
              f"  采样 {lap.sample_count}  {lap_tags(lap)}")
    print(f"  共 {len(r.laps)} 圈")
    if args.dry_run or not r.laps:
        return
    stable_session_uids(r.laps, args.file)
    storage = Storage(args.db)
    if storage.has_session(r.laps[0].session_uid):
        print(f"  这个文件已经存进 {args.db} 了，跳过")
        return
    for lap in r.laps:
        storage.save_lap(lap)
    print(f"  已保存 {len(r.laps)} 圈到 {args.db}")


if __name__ == "__main__":
    main()
