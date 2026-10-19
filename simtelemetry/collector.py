"""后台采集：约 50 Hz 读游戏 → 切圈 → 存库；同时给实时面板喂数据。

- 游戏没开：安静等待，每 retry_s 秒重试一次（只在状态变化时打一条日志）。
- 存库在单独的写线程里做，避免一圈几千行的 INSERT 卡住 50 Hz 采样。
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from .adapters import create_adapter, detect_running_games
from .adapters.base import AdapterDisconnected, GameAdapter
from .analysis import LiveReference
from .live import LiveHub
from .models import LapRecord, SessionInfo
from .recorder import LapRecorder
from .storage import Storage

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, storage: Storage, hub: LiveHub, game: str = "auto", rate_hz: float = 50.0,
                 retry_s: float = 3.0, sim_speed: float = 1.0, stale_s: float = 5.0,
                 check_process: bool = True):
        self.storage = storage
        self.hub = hub
        self.game = game
        self.period = 1.0 / rate_hz
        self.retry_s = retry_s
        self.sim_speed = sim_speed
        self.stale_s = stale_s
        self.check_process = check_process

        self.recorder = LapRecorder()
        self.adapter: GameAdapter | None = None
        self._ref: LiveReference | None = None
        self._ref_combo: tuple | None = None
        self._session: SessionInfo | None = None
        self._last_saved: dict | None = None
        self._create_failed: set[str] = set()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._save_q: queue.Queue[LapRecord | None] = queue.Queue()
        self._writer: threading.Thread | None = None

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        self._stop.clear()
        self._writer = threading.Thread(target=self._write_loop, name="lap-writer", daemon=True)
        self._writer.start()
        self._thread = threading.Thread(target=self._run, name="collector", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self._save_q.put(None)
        if self._writer:
            self._writer.join(timeout)

    # ------------------------------------------------------------------- loop
    def _candidates(self) -> list[GameAdapter]:
        games = [self.game] if self.game != "auto" else detect_running_games()
        out: list[GameAdapter] = []
        for g in games:
            try:
                out.append(create_adapter(g, sim_speed=self.sim_speed,
                                          check_process=self.check_process))
            except Exception as e:                  # 缺依赖（如 pyirsdk）等：只报一次，不拖垮采集线程
                if g not in self._create_failed:
                    self._create_failed.add(g)
                    log.error("无法创建 %s 适配器：%s", g, e)
        return out

    def _try_connect(self) -> GameAdapter | None:
        for adapter in self._candidates():
            try:
                if adapter.connect():
                    return adapter
            except Exception:                       # 适配器自身的 bug 不能拖垮采集线程
                log.exception("连接 %s 时出错", adapter.display_name)
            adapter.close()
        return None

    def _run(self) -> None:
        waiting_logged = False
        next_t = time.perf_counter()
        last_tick_change = time.monotonic()
        last_tick = None
        while not self._stop.is_set():
            if self.adapter is None:
                self.adapter = self._try_connect()
                if self.adapter is None:
                    if not waiting_logged:
                        log.info("未检测到游戏，等待中（每 %.0f 秒重试）…", self.retry_s)
                        waiting_logged = True
                        self.hub.replace(state="waiting", game=None)
                    self._stop.wait(self.retry_s)
                    continue
                waiting_logged = False
                log.info("已连接：%s", self.adapter.display_name)
                self.hub.replace(state="connected", game=self.adapter.name,
                                 game_name=self.adapter.display_name)
                last_tick_change = time.monotonic()
                last_tick = None
                next_t = time.perf_counter()

            now = time.monotonic()
            try:
                res = self.adapter.read()
            except AdapterDisconnected as e:
                self._disconnect(f"游戏断开：{e}")
                continue
            except Exception:
                log.exception("读取 %s 数据出错，断开后重连", self.adapter.display_name)
                self._disconnect("读取出错")
                continue

            if res is None:
                self._queue_laps(self.recorder.flush(now))
                self.hub.update(state="connected", live=False)
            else:
                session, frame = res
                if frame.tick != last_tick:
                    last_tick, last_tick_change = frame.tick, now
                elif now - last_tick_change > self.stale_s:
                    # 数据长时间不更新：可能是暂停/菜单，也可能游戏已退出而我们还拿着映射
                    # （映射在所有句柄关闭前不会消失，内容停在最后一帧）。查一下进程。
                    last_tick_change = now
                    running = self.adapter.is_running()
                    if running is False or (running is None and frame.is_live):
                        self._disconnect("游戏已退出（数据停止更新）")
                        continue
                if session is None:                 # 已连接，但还在菜单里 / 赛道车型未就绪
                    self._queue_laps(self.recorder.flush(now))
                    self._publish_frame(None, frame, now)
                else:
                    if session != self._session:
                        self._session = session
                        self._load_reference(session)
                        log.info("会话：%s / %s / %s", session.game, session.track, session.car)
                    self._queue_laps(self.recorder.feed(session, frame, now))
                    self._publish_frame(session, frame, now)

            next_t += self.period
            delay = next_t - time.perf_counter()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -1.0:                      # 落后太多（比如电脑卡顿），重新对齐节拍
                next_t = time.perf_counter()

        self._queue_laps(self.recorder.reset("采集停止"))
        if self.adapter:
            self.adapter.close()
            self.adapter = None

    def _disconnect(self, reason: str) -> None:
        log.info("%s", reason)
        self._queue_laps(self.recorder.reset(reason))
        if self.adapter:
            self.adapter.close()
        self.adapter = None
        self._session = None
        self.hub.replace(state="waiting", game=None)

    # ------------------------------------------------------------- reference
    def _load_reference(self, session: SessionInfo) -> None:
        self._ref = None
        self._ref_combo = session.combo
        try:
            best = self.storage.best_lap(*session.combo)
            if best:
                self._ref = LiveReference(best, self.storage.get_samples(best["id"]))
        except Exception:
            log.exception("加载参考圈失败")

    def _queue_laps(self, laps: list[LapRecord]) -> None:
        for lap in laps:
            self._save_q.put(lap)

    def _write_loop(self) -> None:
        while True:
            lap = self._save_q.get()
            if lap is None:
                return
            try:
                lap_id = self.storage.save_lap(lap)
            except Exception:
                log.exception("保存第 %d 圈失败", lap.lap_number)
                continue
            self._last_saved = {
                "id": lap_id, "lap_number": lap.lap_number, "lap_time_ms": lap.lap_time_ms,
                "is_valid": lap.is_valid, "is_out_lap": lap.is_out_lap, "is_in_lap": lap.is_in_lap,
                "is_complete": lap.is_complete, "is_clean": lap.is_clean,
            }
            ref = self._ref
            if lap.is_clean and lap.session.combo == self._ref_combo and \
                    (ref is None or lap.lap_time_ms < ref.lap_time_ms):
                lap_row = self.storage.get_lap(lap_id)
                if lap_row:
                    self._ref = LiveReference(lap_row, self.storage.get_samples(lap_id))
                    log.info("新的个人最快圈，实时 delta 参考已更新")

    # ---------------------------------------------------------------- publish
    def _publish_frame(self, session: SessionInfo | None, f, now: float) -> None:
        ref = self._ref if session is not None else None
        lap_state = self.recorder.current_lap_state() if session is not None else None
        delta = None
        if ref is not None and f.is_live and session is not None and session.combo == self._ref_combo:
            delta = round(ref.delta_ms(f.pos, f.lap_time_ms))
        usable = bool(delta is not None and lap_state and not lap_state["pit_seen"]
                      and not lap_state["partial"] and not f.in_pit)
        self.hub.update(
            state="live" if f.is_live else "connected",
            live=f.is_live,
            game=self.adapter.name if self.adapter else None,
            game_name=self.adapter.display_name if self.adapter else None,
            track=session.track if session else None,
            car=session.car if session else None,
            speed=round(f.speed_kmh, 1),
            gear=f.gear,
            rpm=f.rpm,
            throttle=round(f.throttle, 3),
            brake=round(f.brake, 3),
            steer=round(f.steer, 3),
            lap=lap_state["lap_number"] if lap_state else f.lap_count + 1,
            lap_time_ms=f.lap_time_ms,
            pos=round(f.pos, 5),
            in_pit=f.in_pit,
            lap_valid=bool(f.lap_valid and not (lap_state and lap_state["invalid"])),
            last_lap_ms=f.last_lap_ms or None,
            delta_ms=delta,
            delta_usable=usable,
            ref_lap_ms=ref.lap_time_ms if ref else None,
            ref_lap_id=ref.lap_id if ref else None,
            predicted_ms=(ref.lap_time_ms + delta) if (ref and delta is not None) else None,
            last_saved=self._last_saved,
        )

    # ----------------------------------------------------------------- status
    def status(self) -> dict:
        s = self._session
        return {
            "game_option": self.game,
            "connected": self.adapter is not None,
            "game": self.adapter.name if self.adapter else None,
            "game_name": self.adapter.display_name if self.adapter else None,
            "track": s.track if s else None,
            "car": s.car if s else None,
            "reference_lap_id": self._ref.lap_id if self._ref else None,
        }
