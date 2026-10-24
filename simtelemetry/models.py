"""与游戏无关的统一数据模型。

所有适配器都把各自游戏的原始数据转换成这里的单位/编码，
存储层、分析层和前端只认这一套：

- speed_kmh   km/h
- throttle    0..1
- brake       0..1
- steer       -1..1（方向盘输入，归一化）
- gear        -1 = R, 0 = N, 1.. = 前进挡
- lap_time_ms 本圈已用时间（毫秒，以游戏计时为准）
- pos         赛道归一化位置 0..1（0 = 起终点线）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SessionInfo:
    game: str                      # 'acc' | 'ac' | 'iracing' | 'sim'
    track: str
    car: str
    track_length_m: float | None = None
    session_type: str = ""        # 'practice' / 'qualify' / 'race' / ...

    @property
    def combo(self) -> tuple[str, str, str]:
        return (self.game, self.track, self.car)


@dataclass(slots=True)
class TelemetryFrame:
    speed_kmh: float
    throttle: float
    brake: float
    steer: float
    gear: int
    rpm: int
    lap_count: int                 # 游戏报告的已完成圈数（会话内单调递增）
    lap_time_ms: int               # 本圈已用时间
    pos: float                     # 0..1
    in_pit: bool                   # 在维修区（含维修区车道）
    lap_valid: bool                # 游戏认为本圈目前仍有效
    last_lap_ms: int | None        # 游戏报告的上一圈圈时（0/None = 未知）
    is_live: bool                  # False = 暂停 / 回放 / 菜单，此时不记录
    tick: int                      # 游戏数据包序号；与上一帧相同 = 没有新数据


# 每个采样点保存的通道（顺序即 SQLite samples 表的列顺序）
SAMPLE_CHANNELS: tuple[str, ...] = (
    "t_ms", "pos", "speed", "throttle", "brake", "steer", "gear", "rpm", "in_pit", "valid",
)


def is_clean_lap(is_valid: bool, is_complete: bool, is_out_lap: bool, is_in_lap: bool) -> bool:
    """“干净圈”：可以参与最快圈比较。改这里时同步改 storage.CLEAN_SQL。"""
    return bool(is_valid and is_complete and not is_out_lap and not is_in_lap)


@dataclass(slots=True)
class LapRecord:
    session: SessionInfo
    session_uid: str
    lap_number: int                # 第几圈（1 起）
    lap_time_ms: int
    time_source: str               # 'game' = 游戏给出的圈时；'estimated' = 根据采样估算
    is_valid: bool
    is_out_lap: bool
    is_in_lap: bool
    is_complete: bool              # 起终点之间完整覆盖、没有瞬移/缺口
    recorded_at: datetime
    samples: dict[str, list] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        return is_clean_lap(self.is_valid, self.is_complete, self.is_out_lap, self.is_in_lap)

    @property
    def sample_count(self) -> int:
        return len(self.samples.get("t_ms", ()))
