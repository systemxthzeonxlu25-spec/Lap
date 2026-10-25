"""圈 ↔ 调教：这一圈用的是哪套调教。

共享内存里读不到“当前加载的是哪个调教文件”，所以：
- 手动：用户在圈列表里给某个会话选一套调教（或者选“没有/不记得”），存在 session_setups 表里，优先；
- 自动猜：同一游戏、车型和赛道对得上的调教里，取会话开始前最后修改（文件修改时间）的那个版本。
  猜测在读取时现算、不落库，所以猜错不会留下脏数据，调教库扫到新文件后也会自动更新。

车型 / 赛道名匹配：ACC 的调教目录名和共享内存里的 ID 一样，直接相等；LMU 的目录名和游戏报的名字不一样
（如 “BMW_M4_LMGT3_2023” / “BMW M4 LMGT3”，“Bahrain” / “Bahrain International Circuit”），
所以按“拆成小写词后，一边的词全部包含在另一边里”匹配。匹配不上就不猜——宁可不猜也别猜错。
导入的圈（别人的）不猜。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..storage import IMPORT_PREFIX, Storage

SLACK = timedelta(minutes=5)       # 会话开始后几分钟内改的调教也算（进赛道前在车库里存了一下）


def _tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", name.lower()))


def names_match(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def setup_candidates(storage: Storage, game: str, car: str, track: str | None = None) -> list[dict]:
    """这个游戏里车型对得上的所有调教版本（新的在前）；track 给了时赛道也要对得上。"""
    with storage.connection() as con:
        rows = con.execute(
            "SELECT v.id AS version_id, v.file_id, v.file_mtime, v.captured_at, f.name, f.car, f.track, f.missing "
            "FROM setup_versions v JOIN setup_files f ON f.id = v.file_id WHERE f.game = ? "
            "ORDER BY v.file_mtime DESC, v.id DESC", (game,)).fetchall()
    return [dict(r) for r in rows
            if names_match(r["car"], car) and (track is None or names_match(r["track"], track))]


def guess_setup(storage: Storage, game: str, car: str, track: str, started: datetime) -> dict | None:
    for c in setup_candidates(storage, game, car, track):
        mtime = _parse(c["file_mtime"])
        if mtime is not None and mtime <= started + SLACK:
            return c
    return None


def set_session_setup(storage: Storage, session_uid: str, version_id: int | None, auto: bool = False) -> None:
    """auto=True：删掉手动选择，回到自动猜。version_id=None 且非 auto：明确表示“没有/不记得”。"""
    with storage.connection() as con:
        if auto:
            con.execute("DELETE FROM session_setups WHERE session_uid = ?", (session_uid,))
        else:
            con.execute("INSERT INTO session_setups (session_uid, setup_version_id, set_at) VALUES (?, ?, ?) "
                        "ON CONFLICT (session_uid) DO UPDATE SET setup_version_id = excluded.setup_version_id, "
                        "set_at = excluded.set_at",
                        (session_uid, version_id, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def version_info(storage: Storage, version_id: int) -> dict | None:
    with storage.connection() as con:
        row = con.execute("SELECT v.id AS version_id, v.file_id, v.file_mtime, f.name, f.car, f.track "
                          "FROM setup_versions v JOIN setup_files f ON f.id = v.file_id WHERE v.id = ?",
                          (version_id,)).fetchone()
    return dict(row) if row else None


def annotate_laps(storage: Storage, laps: list[dict]) -> list[dict]:
    """给 list_laps 的每一圈加上 lap["setup"]：{version_id, file_id, name, track, source: manual|auto} 或 None。"""
    sessions: dict[str, list[dict]] = {}
    for lap in laps:
        sessions.setdefault(lap["session_uid"], []).append(lap)
    if not sessions:
        return laps
    with storage.connection() as con:
        q = ",".join("?" * len(sessions))
        manual = {r["session_uid"]: r["setup_version_id"] for r in
                  con.execute(f"SELECT session_uid, setup_version_id FROM session_setups WHERE session_uid IN ({q})",
                              list(sessions))}
    for uid, group in sessions.items():
        setup = None
        if uid in manual:
            info = version_info(storage, manual[uid]) if manual[uid] is not None else None
            setup = {**info, "source": "manual"} if info else {"version_id": None, "source": "manual"}
        elif not uid.startswith(IMPORT_PREFIX):
            first = group[0]
            starts = [d - timedelta(milliseconds=l["lap_time_ms"]) for l in group
                      if (d := _parse(l["recorded_at"])) is not None]
            if starts:
                g = guess_setup(storage, first["game"], first["car"], first["track"], min(starts))
                setup = {**g, "source": "auto"} if g else None
        for lap in group:
            lap["setup"] = setup
    return laps
