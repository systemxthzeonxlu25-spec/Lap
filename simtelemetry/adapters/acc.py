"""Assetto Corsa Competizione（共享内存，结构体见 acc_structs.py）。

字段映射：
- speed / throttle / brake / steer / rpm ← physics speedKmh / gas / brake / steerAngle / rpms
- gear ← physics gear - 1（ACC：0 = R、1 = N、2 = 1 挡，社区实测，文档没写）
- lap_count / lap_time_ms / pos ← graphics completedLaps / iCurrentTime / normalizedCarPosition
- in_pit ← graphics isInPit or isInPitLane；lap_valid ← graphics isValidLap
  （比赛 / hotstint 会话里压线后 isValidLap 也常保持 1，那些会话的“有效”不可信，见 PROGRESS.md）
- last_lap_ms ← graphics iLastTime（INT32_MAX 或 ≤ 0 = 还没有）
- is_live ← graphics status == ACC_LIVE；tick ← physics packetId（暂停 / 菜单时不再增长）
- track / car ← static track / carModel；session_type ← graphics session
- 赛道长度：static trackSPlineLength 在 ACC 里实测为 0，留空，由对比接口用速度积分估算

steerAngle 的正负方向文档没写，原样记录（-1..1），等实车记录确认后再决定是否取反。
"""
from __future__ import annotations

from ..models import SessionInfo, TelemetryFrame
from .acc_structs import (ACC_LIVE, ACC_OFF, ACC_SESSION_TYPE, SPageFileGraphic, SPageFilePhysics,
                          SPageFileStatic, wchar_str)
from .kunos import KunosAdapter, clamp, lap_ms_or_none


class ACCAdapter(KunosAdapter):
    name = "acc"
    display_name = "Assetto Corsa Competizione"
    physics_type = SPageFilePhysics
    graphics_type = SPageFileGraphic
    static_type = SPageFileStatic

    def convert(self, p, g, s) -> tuple[SessionInfo | None, TelemetryFrame]:
        frame = TelemetryFrame(
            speed_kmh=max(0.0, float(p.speedKmh)),
            throttle=clamp(p.gas, 0.0, 1.0),
            brake=clamp(p.brake, 0.0, 1.0),
            steer=clamp(p.steerAngle, -1.0, 1.0),
            gear=int(p.gear) - 1,
            rpm=max(0, int(p.rpms)),
            lap_count=int(g.completedLaps),
            lap_time_ms=max(0, int(g.iCurrentTime)),
            pos=clamp(g.normalizedCarPosition, 0.0, 1.0),
            in_pit=bool(g.isInPit or g.isInPitLane),
            lap_valid=bool(g.isValidLap),
            last_lap_ms=lap_ms_or_none(g.iLastTime),
            is_live=g.status == ACC_LIVE,
            tick=int(p.packetId),
        )
        track, car = wchar_str(s.track), wchar_str(s.carModel)
        if g.status == ACC_OFF or not track or not car:
            return None, frame
        length = float(s.trackSPlineLength)
        session = SessionInfo(
            game=self.name, track=track, car=car,
            track_length_m=length if length > 100 else None,
            session_type=ACC_SESSION_TYPE.get(int(g.session), "unknown"),
        )
        return session, frame
