"""SQLite 存储：一圈一条记录（laps）+ 采样点表（samples）。

线程模型：每次操作单独开连接（sqlite3 连接很便宜），采集线程写、Web 线程读互不干扰；
数据库开 WAL，读写可以并发。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import SAMPLE_CHANNELS, LapRecord

SCHEMA_VERSION = 1

# “干净圈”：参与最快圈比较
CLEAN_SQL = "is_valid = 1 AND is_complete = 1 AND is_out_lap = 0 AND is_in_lap = 0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS laps (
    id              INTEGER PRIMARY KEY,
    game            TEXT    NOT NULL,
    track           TEXT    NOT NULL,
    car             TEXT    NOT NULL,
    session_type    TEXT    NOT NULL DEFAULT '',
    session_uid     TEXT    NOT NULL DEFAULT '',
    lap_number      INTEGER NOT NULL,
    lap_time_ms     INTEGER NOT NULL,
    time_source     TEXT    NOT NULL DEFAULT 'game',
    is_valid        INTEGER NOT NULL,
    is_out_lap      INTEGER NOT NULL,
    is_in_lap       INTEGER NOT NULL,
    is_complete     INTEGER NOT NULL,
    track_length_m  REAL,
    sample_count    INTEGER NOT NULL,
    recorded_at     TEXT    NOT NULL           -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_laps_combo ON laps (game, track, car, lap_time_ms);

CREATE TABLE IF NOT EXISTS samples (
    lap_id    INTEGER NOT NULL REFERENCES laps (id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    t_ms      INTEGER NOT NULL,   -- 本圈已用时间
    pos       REAL    NOT NULL,   -- 归一化位置（已处理起终点回绕，可能略小于 0 或略大于 1）
    speed     REAL    NOT NULL,   -- km/h
    throttle  REAL    NOT NULL,   -- 0..1
    brake     REAL    NOT NULL,   -- 0..1
    steer     REAL    NOT NULL,   -- -1..1
    gear      INTEGER NOT NULL,   -- -1 R / 0 N / 1..
    rpm       INTEGER NOT NULL,
    in_pit    INTEGER NOT NULL,
    valid     INTEGER NOT NULL,
    PRIMARY KEY (lap_id, idx)
) WITHOUT ROWID;
"""

_LAP_COLUMNS = (
    "id, game, track, car, session_type, session_uid, lap_number, lap_time_ms, time_source, "
    "is_valid, is_out_lap, is_in_lap, is_complete, track_length_m, sample_count, recorded_at"
)


def _lap_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("is_valid", "is_out_lap", "is_in_lap", "is_complete"):
        d[k] = bool(d[k])
    d["is_clean"] = d["is_valid"] and d["is_complete"] and not d["is_out_lap"] and not d["is_in_lap"]
    return d


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(_SCHEMA)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA synchronous = NORMAL")
        try:
            with con:              # 事务：成功提交，异常回滚
                yield con
        finally:
            con.close()

    # ------------------------------------------------------------------ write
    def save_lap(self, rec: LapRecord) -> int:
        s = rec.session
        n = rec.sample_count
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO laps (game, track, car, session_type, session_uid, lap_number, "
                "lap_time_ms, time_source, is_valid, is_out_lap, is_in_lap, is_complete, "
                "track_length_m, sample_count, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s.game, s.track, s.car, s.session_type, rec.session_uid, rec.lap_number,
                 rec.lap_time_ms, rec.time_source, int(rec.is_valid), int(rec.is_out_lap),
                 int(rec.is_in_lap), int(rec.is_complete), s.track_length_m, n,
                 rec.recorded_at.isoformat(timespec="seconds")),
            )
            lap_id = int(cur.lastrowid)
            cols = [rec.samples[c] for c in SAMPLE_CHANNELS]
            con.executemany(
                f"INSERT INTO samples (lap_id, idx, {', '.join(SAMPLE_CHANNELS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(SAMPLE_CHANNELS))})",
                ((lap_id, i, *row) for i, row in enumerate(zip(*cols))),
            )
        return lap_id

    def delete_lap(self, lap_id: int) -> bool:
        with self._conn() as con:
            return con.execute("DELETE FROM laps WHERE id = ?", (lap_id,)).rowcount > 0

    # ------------------------------------------------------------------- read
    def list_combos(self) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                f"""SELECT game, track, car,
                          COUNT(*) AS laps,
                          SUM(CASE WHEN {CLEAN_SQL} THEN 1 ELSE 0 END) AS clean_laps,
                          MIN(CASE WHEN {CLEAN_SQL} THEN lap_time_ms END) AS best_ms,
                          MAX(recorded_at) AS last_at
                   FROM laps GROUP BY game, track, car ORDER BY last_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def list_laps(self, game: str, track: str, car: str) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                f"SELECT {_LAP_COLUMNS} FROM laps WHERE game = ? AND track = ? AND car = ? "
                "ORDER BY recorded_at DESC, id DESC",
                (game, track, car),
            ).fetchall()
        return [_lap_row_to_dict(r) for r in rows]

    def get_lap(self, lap_id: int) -> dict | None:
        with self._conn() as con:
            row = con.execute(f"SELECT {_LAP_COLUMNS} FROM laps WHERE id = ?", (lap_id,)).fetchone()
        return _lap_row_to_dict(row) if row else None

    def get_samples(self, lap_id: int) -> dict[str, list]:
        with self._conn() as con:
            rows = con.execute(
                f"SELECT {', '.join(SAMPLE_CHANNELS)} FROM samples WHERE lap_id = ? ORDER BY idx",
                (lap_id,),
            ).fetchall()
        cols = list(zip(*rows)) if rows else [()] * len(SAMPLE_CHANNELS)
        return {c: list(v) for c, v in zip(SAMPLE_CHANNELS, cols)}

    def best_lap(self, game: str, track: str, car: str) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                f"SELECT {_LAP_COLUMNS} FROM laps WHERE game = ? AND track = ? AND car = ? "
                f"AND {CLEAN_SQL} ORDER BY lap_time_ms, id LIMIT 1",
                (game, track, car),
            ).fetchone()
        return _lap_row_to_dict(row) if row else None
