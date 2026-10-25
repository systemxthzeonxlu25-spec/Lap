"""原始录制：把适配器每次 read() 拿到的东西连同时间戳存进一个文件，用来把“某游戏切圈不对”离线复现成回归测试。

    python -m simtelemetry --game acc --record acc.strec                # 正常采集，同时录制
    python -m simtelemetry --game lmu --record lmu.strec --record-raw   # 另存共享内存原始字节
    python -m simtelemetry.replay acc.strec --dry-run                   # 维护者：不开网页，直接看切出来的圈

两种模式：
- frames（默认）：存 read() 返回的 SessionInfo + TelemetryFrame（或 None）。切圈 / 标记（recorder.py）的问题都能复现。
- raw：读共享内存的游戏，存这一帧适配器实际拷出来的每个结构体的原始字节
  （ACC：physics / graphics / static 三页；LMU：遥测头、玩家遥测、计分总信息、玩家计分条目），
  回放时重新走该游戏真正的 read() + translate()，所以字段映射 / 结构体布局的问题也能复现，修好后直接验证。
  不读共享内存的适配器（sim）在 raw 模式下照常按 frames 存。
  限制：只存了“拷过的那几段”。如果修 bug 需要读别的偏移、或者结构体大小变了，回放时缺的部分补 0 并警告——
  那种情况请用 tools/lmu_dump.py 存整块映射。

原始字节怎么抓：适配器只实现 GameAdapter.wrap_pages()（把自己的页对象换成包装后的），read() / translate() 一行不改，
也不用在适配器里保存“上一帧的原始数据”。录制时包一层“记下每次拷出的字节”的页，回放时换成“按顺序吐出录下的字节”的页。
页是适配器和共享内存之间唯一的边界，所以录下来的正好是 translate() 的全部输入（包括 read_consistent 的重读、
LMU 找玩家计分条目时扫过的条目）。

除了帧，还记录连接 / 断开 / 读取出错 / is_running() 的结果（采集线程据此断开重连、丢弃未完成的圈）；
帧的时间戳就是采集循环切圈用的那个 time.monotonic()（Collector 的 clock 参数），所以回放切出来的圈和当时完全一样。

文件格式（版本 1）：整个文件是 gzip；解压后 = 魔数 b"SIMTREC\\n" + 一串记录，每条 = 1 字节类型 + 4 字节小端长度 + 内容，
内容以 8 字节时间戳（double，monotonic 秒）开头。第一条是 JSON 头：格式版本、游戏、程序版本、创建时间、模式。
只追加写，每秒做一次 zlib 同步刷新：进程被杀掉也能读到最后一次刷新为止（最多丢 1 秒），读到写了一半的记录就停。
raw 模式下同一页同一偏移的字节和上一帧相同只记一个标记，不同就存和上一帧的异或（大部分是 0，压缩后很小）。
实测大小（50 Hz，合成车手 + tools/fake_acc、fake_lmu）：frames 约 115 KB/分钟；raw：ACC 约 125 KB/分钟、
LMU 约 145 KB/分钟；30 分钟的会话 3.5–4.5 MB。真游戏的数据噪声不同，可能有出入。

隐私：录制文件里有赛道名、车型；raw 模式下 ACC 的 static 页里有玩家姓名 / 昵称，LMU 的结构体里有玩家名、服务器名，
找玩家计分条目时扫过的条目里还有其他车手的名字。本程序没有任何上传功能；把文件发给别人之前请自己确认。
"""
from __future__ import annotations

import gzip
import json
import logging
import struct
import threading
import time
import zlib
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

from . import __version__
from .adapters.base import AdapterDisconnected, GameAdapter
from .models import SessionInfo, TelemetryFrame

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
MAGIC = b"SIMTREC\n"
FLUSH_EVERY_S = 1.0
PRIVACY_NOTE = "录制文件里有赛道/车型，raw 模式下还有玩家名、服务器名等；本程序不会上传，分享前请自己确认"

_REC = struct.Struct("<cI")                 # 类型 + 长度
_TS = struct.Struct("<d")
# speed throttle brake steer gear rpm lap_count lap_time_ms pos last_lap_ms tick flags
_FRAME = struct.Struct("<ddddiiiidiqB")
_READ = struct.Struct("<BiBI")              # 页序号、偏移（-1 = 整页 read()）、编码、长度
_COUNT = struct.Struct("<H")
F_IN_PIT, F_VALID, F_LIVE, F_NO_LAST, F_NO_SESSION = 1, 2, 4, 8, 16
ENC_FULL, ENC_SAME, ENC_XOR = 0, 1, 2

# 文件里的 1 字节类型 → 事件名
KINDS = {b"H": "header", b"C": "connect", b"S": "session", b"F": "frame", b"N": "none", b"R": "raw",
         b"E": "error", b"P": "running", b"X": "close"}
_CODES = {v: k for k, v in KINDS.items()}


class RecordingError(Exception):
    """不是录制文件 / 格式版本不对。"""


class Event(NamedTuple):
    kind: str          # KINDS 的值
    ts: float
    data: Any          # frame: (TelemetryFrame, 有没有 session)；raw: [(页序号, 偏移, bytes)]；其余见 _decode


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(len(a), "little")


def encode_frame(f: TelemetryFrame, has_session: bool) -> bytes:
    flags = ((F_IN_PIT if f.in_pit else 0) | (F_VALID if f.lap_valid else 0) | (F_LIVE if f.is_live else 0)
             | (F_NO_LAST if f.last_lap_ms is None else 0) | (0 if has_session else F_NO_SESSION))
    return _FRAME.pack(f.speed_kmh, f.throttle, f.brake, f.steer, int(f.gear), int(f.rpm), int(f.lap_count),
                       int(f.lap_time_ms), f.pos, int(f.last_lap_ms or 0), int(f.tick), flags)


def decode_frame(body: bytes) -> tuple[TelemetryFrame, bool]:
    speed, thr, brk, steer, gear, rpm, lc, lt, pos, last, tick, flags = _FRAME.unpack(body)
    f = TelemetryFrame(speed_kmh=speed, throttle=thr, brake=brk, steer=steer, gear=gear, rpm=rpm, lap_count=lc,
                       lap_time_ms=lt, pos=pos, in_pit=bool(flags & F_IN_PIT), lap_valid=bool(flags & F_VALID),
                       last_lap_ms=None if flags & F_NO_LAST else last, is_live=bool(flags & F_LIVE), tick=tick)
    return f, not flags & F_NO_SESSION


# ------------------------------------------------------------------ 写
class Recording:
    """一个录制文件。采集线程写，主线程关闭；写出错只打一条日志然后停止录制，绝不影响采集。"""

    def __init__(self, path: str | Path, raw: bool = False, clock: Callable[[], float] = time.monotonic,
                 flush_every_s: float = FLUSH_EVERY_S):
        self.path = Path(path)
        self.raw = raw
        self._clock = clock
        self._flush_every = flush_every_s
        self._f = self.path.open("xb")                       # 已存在就报错，不覆盖别人的录制
        self._gz = gzip.GzipFile(filename="", fileobj=self._f, mode="wb", compresslevel=6, mtime=0)
        self._lock = threading.Lock()
        self._header_written = False
        self._closed = False
        self._pending_ts: float | None = None
        self._last_flush = clock()
        self.frames = 0
        self._gz.write(MAGIC)

    def clock(self) -> float:
        """给 Collector 当 clock 用：记下这次取的时间，紧接着的 read() 就用它当时间戳。"""
        t = self._clock()
        self._pending_ts = t
        return t

    def take_ts(self) -> float:
        t, self._pending_ts = self._pending_ts, None
        return self._clock() if t is None else t

    def now(self) -> float:
        return self._clock()

    def wrap(self, adapter: GameAdapter) -> GameAdapter:
        """给 Collector 当 wrap 用。"""
        return RecordingAdapter(adapter, self)

    def write(self, kind: str, ts: float, body: bytes = b"") -> None:
        data = _TS.pack(ts) + body
        with self._lock:
            if self._closed:
                return
            try:
                self._gz.write(_REC.pack(_CODES[kind], len(data)) + data)
                if kind in ("frame", "none", "raw"):
                    self.frames += 1
                now = self._clock()
                if now - self._last_flush >= self._flush_every:
                    self._last_flush = now
                    self._gz.flush(zlib.Z_SYNC_FLUSH)        # 之前写的都能解出来：被杀掉也只丢最后不到 1 秒
            except Exception:
                log.exception("写录制文件 %s 失败，停止录制（采集不受影响）", self.path)
                try:
                    self._close_locked()
                except Exception:                            # 比如磁盘满了，关闭时也写不进去
                    pass

    def write_json(self, kind: str, ts: float, obj) -> None:
        self.write(kind, ts, json.dumps(obj, ensure_ascii=False).encode())

    def write_header(self, game: str, mode: str) -> None:
        if self._header_written:
            return
        self._header_written = True
        self.write_json("header", self.now(), {
            "format": FORMAT_VERSION, "app": "simtelemetry", "app_version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "game": game, "mode": mode,
        })

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._gz.close()
        finally:
            self._f.close()


class _RecordingPage:
    """包在适配器的页外面：照常读，顺便把拷出来的字节交给 RecordingAdapter。"""

    def __init__(self, page, idx: int, owner: RecordingAdapter):
        self._page, self._idx, self._owner = page, idx, owner

    def open(self) -> bool:
        return self._page.open()

    def close(self) -> None:
        self._page.close()

    def read(self):
        obj = self._page.read()
        self._owner._log_read(self._idx, -1, obj)
        return obj

    def read_at(self, struct_type, offset: int):
        obj = self._page.read_at(struct_type, offset)
        self._owner._log_read(self._idx, offset, obj)
        return obj

    def __getattr__(self, name):
        return getattr(self._page, name)


class RecordingAdapter(GameAdapter):
    """包装任意 GameAdapter：每次 read() 把结果（raw 模式：拷出的原始字节）写进 Recording。"""

    def __init__(self, inner: GameAdapter, rec: Recording):
        self.inner, self._rec = inner, rec
        self.name, self.display_name = inner.name, inner.display_name
        self._pages: list[str] = []
        self._reads: list[tuple[int, int, bytes]] | None = None
        self._prev: dict[tuple[int, int], bytes] = {}
        self._session: SessionInfo | None = None
        self._connected = False
        self.raw = rec.raw and inner.wrap_pages(self._wrap_page)
        if rec.raw and not self.raw:
            log.info("%s 不读共享内存，按 frames 模式录制", inner.display_name)

    def _wrap_page(self, name: str, page):
        self._pages.append(name)
        return _RecordingPage(page, len(self._pages) - 1, self)

    def _log_read(self, idx: int, offset: int, obj) -> None:
        if self._reads is not None:
            self._reads.append((idx, offset, bytes(obj)))

    def connect(self) -> bool:
        ok = self.inner.connect()
        if ok:
            mode = "raw" if self.raw else "frames"
            self._rec.write_header(self.name, mode)
            self._rec.write_json("connect", self._rec.now(), {
                "game": self.name, "display_name": self.display_name, "mode": mode, "pages": self._pages})
            self._connected, self._prev, self._session = True, {}, None
        return ok

    def read(self):
        rec = self._rec
        ts = rec.take_ts()
        self._reads = [] if self.raw else None
        try:
            res = self.inner.read()
        except Exception as e:
            self._write_raw(ts)
            rec.write_json("error", ts, {"type": "disconnected" if isinstance(e, AdapterDisconnected) else "error",
                                         "message": str(e) or type(e).__name__})
            raise
        if self.raw:
            self._write_raw(ts)
        elif res is None:
            rec.write("none", ts)
        else:
            session, frame = res
            if session is not None and session != self._session:
                self._session = session
                rec.write_json("session", ts, asdict(session))
            try:
                body = encode_frame(frame, session is not None)
            except (struct.error, TypeError, ValueError):     # 适配器给了奇怪的类型：这一帧不录，采集照常
                log.debug("这一帧无法录制", exc_info=True)
            else:
                rec.write("frame", ts, body)
        return res

    def _write_raw(self, ts: float) -> None:
        if self._reads is not None:
            self._rec.write("raw", ts, self._encode_reads(self._reads))
            self._reads = None

    def _encode_reads(self, reads: list[tuple[int, int, bytes]]) -> bytes:
        out = [_COUNT.pack(len(reads))]
        for idx, off, data in reads:
            prev = self._prev.get((idx, off))
            if prev == data:
                out.append(_READ.pack(idx, off, ENC_SAME, 0))
            elif prev is not None and len(prev) == len(data):
                out += (_READ.pack(idx, off, ENC_XOR, len(data)), _xor(data, prev))
            else:
                out += (_READ.pack(idx, off, ENC_FULL, len(data)), data)
            self._prev[(idx, off)] = data
        return b"".join(out)

    def is_running(self) -> bool | None:
        v = self.inner.is_running()
        if self._connected:
            self._rec.write("running", self._rec.now(), bytes([{None: 2, False: 0, True: 1}[v]]))
        return v

    def close(self) -> None:
        self.inner.close()
        if self._connected:
            self._connected = False
            self._rec.write("close", self._rec.now())

    def wrap_pages(self, wrap) -> bool:
        return False


# ------------------------------------------------------------------ 读
class RecordingReader:
    """逐条读出事件。文件被截断 / 尾部损坏时读到最后一条完整的记录为止，并把 truncated 设为 True。"""

    def __init__(self, path: str | Path, chunk: int = 1 << 16):
        self.path = Path(path)
        self.truncated = False
        self._chunk = chunk
        self._f = self.path.open("rb")
        if self._f.read(2) != b"\x1f\x8b":
            self._f.close()
            raise RecordingError(f"{self.path} 不是 simtelemetry 录制文件（不是 gzip）")
        self._f.seek(0)
        self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._buf = bytearray()
        self._eof = False
        self._prev: dict[tuple[int, int], bytes] = {}
        if not self._fill(len(MAGIC)) or bytes(self._buf[:len(MAGIC)]) != MAGIC:
            self.close()
            raise RecordingError(f"{self.path} 不是 simtelemetry 录制文件（开头不对）")
        del self._buf[:len(MAGIC)]
        first = self._next()
        if first is None or first.kind != "header":
            self.close()
            raise RecordingError(f"{self.path} 没有文件头（可能是录制刚开始就中断了，游戏一直没连上）")
        self.header: dict = first.data
        v = self.header.get("format")
        if v != FORMAT_VERSION:
            self.close()
            raise RecordingError(f"{self.path} 的格式版本是 {v}，本程序（simtelemetry {__version__}）"
                                 f"只能读版本 {FORMAT_VERSION}；请用录制它的版本"
                                 f"（{self.header.get('app_version', '未知')}）回放")
        if self.header.get("app_version") != __version__:
            log.warning("录制文件来自 simtelemetry %s，当前是 %s：raw 模式下结构体定义可能已经变了",
                        self.header.get("app_version"), __version__)

    def _fill(self, n: int) -> bool:
        """解压到缓冲区里至少有 n 字节；不够了返回 False。"""
        while len(self._buf) < n and not self._eof:
            data = self._f.read(self._chunk)
            if not data:
                self._eof = True
                if not self._z.eof:
                    self.truncated = True               # 没有 gzip 结尾：录制时被中断
                break
            try:
                self._buf += self._z.decompress(data)
                while self._z.eof and self._z.unused_data:   # 多段 gzip 首尾相接
                    rest = self._z.unused_data
                    self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    self._buf += self._z.decompress(rest)
            except zlib.error as e:
                log.warning("%s 在这里损坏了（%s），只读到这里", self.path, e)
                self.truncated = self._eof = True
        return len(self._buf) >= n

    def _next(self) -> Event | None:
        if not self._fill(_REC.size):
            if self._buf:
                self.truncated = True
            return None
        code, size = _REC.unpack_from(self._buf)
        if not self._fill(_REC.size + size):
            self.truncated = True                           # 写了一半的记录
            return None
        body = bytes(self._buf[_REC.size:_REC.size + size])
        del self._buf[:_REC.size + size]
        kind = KINDS.get(code)
        if kind is None or size < _TS.size:
            log.warning("%s 里有不认识的记录（%r），停止读取", self.path, code)
            self.truncated = True
            return None
        (ts,) = _TS.unpack_from(body)
        return Event(kind, ts, self._decode(kind, body[_TS.size:]))

    def _decode(self, kind: str, body: bytes):
        if kind in ("header", "connect", "error"):
            obj = json.loads(body)
            if kind == "connect":
                self._prev = {}
            return obj
        if kind == "session":
            return SessionInfo(**json.loads(body))
        if kind == "frame":
            return decode_frame(body)
        if kind == "running":
            return {0: False, 1: True}.get(body[0])
        if kind == "raw":
            (n,), pos, reads = _COUNT.unpack_from(body), _COUNT.size, []
            for _ in range(n):
                idx, off, enc, size = _READ.unpack_from(body, pos)
                pos += _READ.size
                data = body[pos:pos + size]
                pos += size
                prev = self._prev.get((idx, off), b"")
                if enc == ENC_SAME:
                    data = prev
                elif enc == ENC_XOR:
                    data = _xor(data, prev)
                self._prev[(idx, off)] = data
                reads.append((idx, off, data))
            return reads
        return None

    def __iter__(self) -> Iterator[Event]:
        while True:
            ev = self._next()
            if ev is None:
                self.close()
                return
            yield ev

    def close(self) -> None:
        self._f.close()
