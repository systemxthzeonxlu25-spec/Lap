"""把逐帧遥测切分成“圈”，并给每圈打标记。

规则：
- 跨圈 = 游戏的已完成圈数恰好 +1。触发跨圈的那一帧属于新的一圈。
- 已完成圈数减少或一次跳了不止 1：视为会话重置（重开、换赛段等），丢弃当前未完成的圈。
- 本圈计时明显倒退（同一圈内 lap_time 回退 > 1 s）：视为重开/传送，丢弃当前圈。
- 暂停 / 回放 / 菜单（frame.is_live 为 False）以及重复帧（tick 未变）不记录。
- 圈时优先用游戏给出的“上一圈圈时”（ACC 的 iLastTime 可能比圈数晚一两帧更新，
  所以跨圈后最多等待 LAST_LAP_WAIT_S 秒），等不到就用采样估算，并标记 time_source。

标记：
- is_valid    本圈所有采样点（跳过开头 VALIDITY_GRACE_MS 的过渡期）游戏都认为有效
- is_out_lap  本圈从维修区出发，或圈内有“在维修区 → 不在维修区”的变化
- is_in_lap   本圈在维修区结束，或圈内有“不在维修区 → 在维修区”的变化
- is_complete 位置从起终点附近开始、到起终点附近结束，中间没有瞬移/缺口
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .analysis import estimate_track_length
from .models import SAMPLE_CHANNELS, LapRecord, SessionInfo, TelemetryFrame

log = logging.getLogger(__name__)

LAST_LAP_WAIT_S = 3.0          # 跨圈后等待游戏更新“上一圈圈时”的最长时间
VALIDITY_GRACE_MS = 500        # 新圈开头这段时间内忽略“无效”标记（游戏可能晚一帧才复位）
MIN_SAMPLES = 100              # 少于这么多采样点（约 2 s）的圈直接丢弃
COVERAGE_TOL = 0.03            # 起点 ≤ 3%、终点 ≥ 97% 才算覆盖完整
MAX_POS_JUMP = 0.05            # 相邻两个采样点位置跳变超过 5% 视为瞬移/缺口
GAME_TIME_TOLERANCE_MS = 5000  # 游戏圈时与采样估算相差超过这个值就不信任游戏圈时
LAP_TIME_RESET_MS = 1000       # 同一圈内计时倒退超过这个值视为重开
HOLD_MAX_FRAMES = 25           # 起终点附近计时先归零时，最多等这么多帧让圈数 +1


@dataclass
class _LapBuffer:
    lap_number: int
    data: dict[str, list] = field(default_factory=lambda: {c: [] for c in SAMPLE_CHANNELS})
    last_wall: float = 0.0
    last_t_ms: int = 0
    teleported: bool = False
    held_frames: int = 0
    pit_seen: bool = False
    invalid_seen: bool = False

    def append(self, f: TelemetryFrame, wall: float) -> None:
        if f.in_pit:
            self.pit_seen = True
        if not f.lap_valid and f.lap_time_ms >= VALIDITY_GRACE_MS:
            self.invalid_seen = True
        d = self.data
        d["t_ms"].append(int(f.lap_time_ms))
        d["pos"].append(float(f.pos))
        d["speed"].append(round(float(f.speed_kmh), 2))
        d["throttle"].append(round(float(f.throttle), 4))
        d["brake"].append(round(float(f.brake), 4))
        d["steer"].append(round(float(f.steer), 4))
        d["gear"].append(int(f.gear))
        d["rpm"].append(int(f.rpm))
        d["in_pit"].append(1 if f.in_pit else 0)
        d["valid"].append(1 if f.lap_valid else 0)
        self.last_wall = wall
        self.last_t_ms = int(f.lap_time_ms)

    def __len__(self) -> int:
        return len(self.data["t_ms"])


@dataclass
class _PendingLap:
    buf: _LapBuffer
    session: SessionInfo
    session_uid: str
    prev_last_lap_ms: int | None
    closed_wall: float
    estimated_ms: int


class LapRecorder:
    """喂入 (SessionInfo, TelemetryFrame)，产出完成的 LapRecord。非线程安全，由采集线程独占。"""

    def __init__(self) -> None:
        self.session: SessionInfo | None = None
        self.session_uid = ""
        self._buf: _LapBuffer | None = None
        self._pending: list[_PendingLap] = []
        self._last_lap_count: int | None = None
        self._last_tick: int | None = None
        self._last_last_lap_ms: int | None = None

    # ------------------------------------------------------------------ public
    @property
    def current_lap_number(self) -> int | None:
        return self._buf.lap_number if self._buf else None

    def current_lap_state(self) -> dict | None:
        """实时面板用：当前这一圈到目前为止的标记。"""
        b = self._buf
        if b is None:
            return None
        return {
            "lap_number": b.lap_number,
            "pit_seen": b.pit_seen,          # 出站/进站圈，delta 参考意义不大
            "invalid": b.invalid_seen,
            "partial": len(b) > 0 and b.data["pos"][0] > COVERAGE_TOL and b.data["pos"][0] < 1 - COVERAGE_TOL,
        }

    def reset(self, reason: str = "") -> list[LapRecord]:
        """游戏断开等情况：已经跨过线、只差圈时的圈照样保存，未完成的圈丢弃。"""
        done = self._flush_pending(force=True)
        if self._buf is not None and len(self._buf) > 0 and reason:
            log.info("丢弃未完成的第 %s 圈（%s）", self._buf.lap_number, reason)
        self._buf = None
        self._last_lap_count = None
        self._last_tick = None
        self._last_last_lap_ms = None
        return done

    def feed(self, session: SessionInfo, f: TelemetryFrame, wall: float) -> list[LapRecord]:
        """wall: time.monotonic() 秒。返回本次调用中完成（可保存）的圈。"""
        out: list[LapRecord] = []

        if session != self.session:
            out += self.reset("赛道/车型/会话变化" if self.session else "")
            self.session = session
            self.session_uid = uuid.uuid4().hex[:12]

        # 先处理等待圈时的已结束圈（需要看到新帧里的 last_lap_ms）
        out += self._flush_pending(frame=f, wall=wall)

        if not f.is_live:
            return out
        if self._last_tick is not None and f.tick == self._last_tick:
            return out

        lc = int(f.lap_count)
        if self._last_lap_count is None:
            self._start_lap(lc, wall)
        elif lc == self._last_lap_count + 1:
            self._close_lap(f, wall)
            self._start_lap(lc, wall)
        elif lc != self._last_lap_count:
            out += self.reset(f"圈数从 {self._last_lap_count} 变为 {lc}")
            self._start_lap(lc, wall)
        elif self._buf is not None and len(self._buf) > 0 and \
                f.lap_time_ms < self._buf.last_t_ms - LAP_TIME_RESET_MS:
            near_line = f.pos >= 1 - COVERAGE_TOL or f.pos <= COVERAGE_TOL
            if near_line and self._buf.held_frames < HOLD_MAX_FRAMES:
                # 计时先于圈数归零：先不记录这几帧，等圈数 +1 再按正常跨圈处理
                self._buf.held_frames += 1
                self._last_tick = f.tick
                return out
            log.info("第 %s 圈计时倒退（%d → %d ms），视为重开，丢弃", self._buf.lap_number,
                     self._buf.last_t_ms, f.lap_time_ms)
            self._buf = None
            self._start_lap(lc, wall)

        self._last_tick = f.tick
        self._last_lap_count = lc
        self._last_last_lap_ms = f.last_lap_ms

        buf = self._buf
        assert buf is not None
        buf.held_frames = 0
        if len(buf) > 0:
            jump = abs(f.pos - buf.data["pos"][-1])
            if MAX_POS_JUMP < jump < 1 - MAX_POS_JUMP:   # 排除跨起终点线的 0.99 → 0.01
                buf.teleported = True
        buf.append(f, wall)
        return out

    def flush(self, wall: float) -> list[LapRecord]:
        """没有新帧时也要定期调用，让超时的待定圈落盘。"""
        return self._flush_pending(wall=wall)

    # ----------------------------------------------------------------- private
    def _start_lap(self, lap_count: int, wall: float) -> None:
        self._buf = _LapBuffer(lap_number=lap_count + 1, last_wall=wall)

    def _close_lap(self, f: TelemetryFrame, wall: float) -> None:
        buf = self._buf
        self._buf = None
        if buf is None or len(buf) < MIN_SAMPLES:
            if buf is not None:
                log.info("第 %s 圈只有 %d 个采样点，丢弃", buf.lap_number, len(buf))
            return
        # 估算圈时：最后一个旧圈采样 + 距离跨线的墙钟时间 - 新圈已经走过的时间
        gap_ms = max(0, int((wall - buf.last_wall) * 1000) - int(f.lap_time_ms))
        estimated = buf.last_t_ms + min(gap_ms, 200)
        self._pending.append(_PendingLap(buf, self.session, self.session_uid, self._last_last_lap_ms,
                                         wall, estimated))

    def _flush_pending(self, frame: TelemetryFrame | None = None, wall: float | None = None,
                       force: bool = False) -> list[LapRecord]:
        if not self._pending:
            return []
        out: list[LapRecord] = []
        keep: list[_PendingLap] = []
        for p in self._pending:
            game_ms: int | None = None
            if frame is not None and frame.last_lap_ms and frame.last_lap_ms > 0 \
                    and frame.last_lap_ms != p.prev_last_lap_ms:
                game_ms = int(frame.last_lap_ms)
            timed_out = wall is not None and wall - p.closed_wall >= LAST_LAP_WAIT_S
            if game_ms is not None or timed_out or force:
                out.append(self._finalize(p, game_ms))
            else:
                keep.append(p)
        self._pending = keep
        return out

    def _finalize(self, p: _PendingLap, game_ms: int | None) -> LapRecord:
        if game_ms is not None and abs(game_ms - p.estimated_ms) <= GAME_TIME_TOLERANCE_MS:
            lap_ms, source = game_ms, "game"
        else:
            if game_ms is not None:
                log.warning("第 %s 圈：游戏圈时 %d ms 与估算 %d ms 相差过大，使用估算值",
                            p.buf.lap_number, game_ms, p.estimated_ms)
            lap_ms, source = p.estimated_ms, "estimated"

        d = p.buf.data
        d["pos"] = unwrap_positions(d["pos"])
        t, pos, pit, valid = d["t_ms"], d["pos"], d["in_pit"], d["valid"]

        is_valid = all(v for tt, v in zip(t, valid) if tt >= VALIDITY_GRACE_MS) and any(
            tt >= VALIDITY_GRACE_MS for tt in t)
        pit_on = any(a and not b for a, b in zip(pit, pit[1:]))   # 在维修区 → 出维修区
        pit_in = any(b and not a for a, b in zip(pit, pit[1:]))   # 进入维修区
        is_out = bool(pit[0]) or pit_on
        is_in = bool(pit[-1]) or pit_in
        is_complete = (not p.buf.teleported and pos[0] <= COVERAGE_TOL and pos[-1] >= 1 - COVERAGE_TOL)
        length = p.session.track_length_m
        if length is None and is_complete:
            length = estimate_track_length(pos, t, d["speed"])

        rec = LapRecord(
            session=p.session,
            session_uid=p.session_uid,
            lap_number=p.buf.lap_number,
            lap_time_ms=int(lap_ms),
            time_source=source,
            is_valid=is_valid,
            is_out_lap=is_out,
            is_in_lap=is_in,
            is_complete=is_complete,
            recorded_at=datetime.now(timezone.utc),
            samples=d,
            track_length_m=length,
        )
        log.info("完成第 %d 圈 %s（%s）有效=%s 出站=%s 进站=%s 完整=%s 采样=%d",
                 rec.lap_number, format_lap_time(rec.lap_time_ms), source, is_valid, is_out,
                 is_in, is_complete, rec.sample_count)
        return rec


def unwrap_positions(pos: list[float]) -> list[float]:
    """处理起终点线附近的位置回绕。

    游戏的“圈数 +1”和“位置归零”不一定在同一帧发生，所以一圈的开头可能还有几个 0.99x，
    结尾可能已经出现 0.00x。开头的这些减 1，结尾的这些加 1，保证整圈位置大致单调。
    """
    n = len(pos)
    if n == 0:
        return pos
    out = list(pos)
    window = max(5, n // 10)
    k = next((i for i, p in enumerate(out) if p < 0.5), None)
    if k is not None and 0 < k <= window:
        for i in range(k):
            out[i] -= 1.0
    m = next((i for i in range(n - 1, -1, -1) if out[i] > 0.5), None)
    if m is not None and m < n - 1 and (n - 1 - m) <= window:
        for i in range(m + 1, n):
            out[i] += 1.0
    return out


def format_lap_time(ms: int | None) -> str:
    if ms is None or ms <= 0:
        return "--:--.---"
    m, rem = divmod(int(ms), 60_000)
    s, milli = divmod(rem, 1000)
    return f"{m}:{s:02d}.{milli:03d}"
