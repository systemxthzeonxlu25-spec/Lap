"""初代 Assetto Corsa（共享内存，结构体见 ac_structs.py）。

和 ACC 的区别：
- 没有 isValidLap。AC 自己判定切弯是“超过 2 个轮胎出界”（社区资料；race.ini 的 ALLOWED_TYRES_OUT
  据说不生效），所以 physics numberOfTyresOut > 2 的帧记为无效，录制器会把整圈标成无效。
  这只是近似：服务器 / 赛道 mod 的规则可能更宽或更严。
- static trackSPlineLength 是真实的赛道长度（米），对比图横轴可以直接用米。
- 赛道 = static track，有布局（trackConfiguration，如 ks_nordschleife 的 endurance）时拼成 “track [layout]”。
- 挡位编码和 ACC 相同：0 = R、1 = N、2 = 1 挡。steerAngle 社区实测为 -1..1（不是角度）。
"""
from __future__ import annotations

from ..models import SessionInfo, TelemetryFrame
from .ac_structs import AC_LIVE, AC_OFF, AC_SESSION_TYPE, ACGraphics, ACPhysics, ACStatic
from .acc_structs import wchar_str
from .kunos import KunosAdapter, clamp, lap_ms_or_none

TYRES_OUT_LIMIT = 2                 # 出界轮胎数超过这个值 = 切弯


def track_name(track: str, layout: str) -> str:
    return f"{track} [{layout}]" if track and layout else track


class ACAdapter(KunosAdapter):
    name = "ac"
    display_name = "Assetto Corsa"
    physics_type = ACPhysics
    graphics_type = ACGraphics
    static_type = ACStatic

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
            lap_valid=int(p.numberOfTyresOut) <= TYRES_OUT_LIMIT,
            last_lap_ms=lap_ms_or_none(g.iLastTime),
            is_live=g.status == AC_LIVE,
            tick=int(p.packetId),
        )
        track = track_name(wchar_str(s.track), wchar_str(s.trackConfiguration))
        car = wchar_str(s.carModel)
        if g.status == AC_OFF or not track or not car:
            return None, frame
        length = float(s.trackSPlineLength)
        session = SessionInfo(
            game=self.name, track=track, car=car,
            track_length_m=length if length > 100 else None,
            session_type=AC_SESSION_TYPE.get(int(g.session), "unknown"),
        )
        return session, frame
