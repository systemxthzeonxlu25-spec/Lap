"""SQLite 存储：一圈一条记录（laps）+ 采样点表（samples）。

线程模型：每次操作单独开连接（sqlite3 连接很便宜），采集线程写、Web 线程读互不干扰；
数据库开 WAL，读写可以并发。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import SAMPLE_CHANNELS, LapRecord, is_clean_lap
from .paths import DATA_ROOT

DEFAULT_DB = DATA_ROOT / "data" / "telemetry.db"

SCHEMA_VERSION = 2          # 2：加了调教库（setup_files / setup_versions）；只新增表，旧库打开时自动补上

# “干净圈”：参与最快圈比较（与 models.is_clean_lap 保持一致）
CLEAN_SQL = "is_valid = 1 AND is_complete = 1 AND is_out_lap = 0 AND is_in_lap = 0"
# 别人导出、导入进来的圈（lapfile.py）：可以当参考圈对比，但不算“个人最快圈”
IMPORT_PREFIX = "import-"
OWN_SQL = f"session_uid NOT LIKE '{IMPORT_PREFIX}%'"

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

-- 调教库：游戏目录里的每个调教文件一行；内容每变一次存一个版本（只读游戏目录，从不写回）
CREATE TABLE IF NOT EXISTS setup_files (
    id          INTEGER PRIMARY KEY,
    game        TEXT    NOT NULL,
    rel_path    TEXT    NOT NULL,         -- 相对调教根目录，如 ferrari_296_gt3/monza/q1.json
    car         TEXT    NOT NULL,
    track       TEXT    NOT NULL,
    name        TEXT    NOT NULL,         -- 文件名（不含扩展名）
    note        TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '[]',   -- JSON 数组
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    missing     INTEGER NOT NULL DEFAULT 0,      -- 游戏目录里已经没有这个文件了（历史版本仍保留）
    UNIQUE (game, rel_path)
);

CREATE TABLE IF NOT EXISTS setup_versions (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES setup_files (id) ON DELETE CASCADE,
    sha256      TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    file_mtime  TEXT    NOT NULL,         -- 文件修改时间（ISO-8601 UTC）
    captured_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_setup_versions_file ON setup_versions (file_id, id);
"""

_LAP_COLUMNS = (
    "id, game, track, car, session_type, session_uid, lap_number, lap_time_ms, time_source, "
    "is_valid, is_out_lap, is_in_lap, is_complete, track_length_m, sample_count, recorded_at"
)


def _lap_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("is_valid", "is_out_lap", "is_in_lap", "is_complete"):
        d[k] = bool(d[k])
    d["is_clean"] = is_clean_lap(d["is_valid"], d["is_complete"], d["is_out_lap"], d["is_in_lap"])
    d["is_imported"] = d["session_uid"].startswith(IMPORT_PREFIX)
    return d


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:" or self.path.startswith("file:"):
            # 每次操作都新开连接，内存库每次都是空的，表建完就没了
            raise ValueError("Storage 需要一个文件路径（测试请用临时目录下的文件）")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"数据库 {self.path} 的结构版本是 {version}，比本程序支持的 "
                                   f"{SCHEMA_VERSION} 新，请升级程序")
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(_SCHEMA)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
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
        with self.connection() as con:
            cur = con.execute(
                "INSERT INTO laps (game, track, car, session_type, session_uid, lap_number, "
                "lap_time_ms, time_source, is_valid, is_out_lap, is_in_lap, is_complete, "
                "track_length_m, sample_count, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s.game, s.track, s.car, s.session_type, rec.session_uid, rec.lap_number,
                 rec.lap_time_ms, rec.time_source, int(rec.is_valid), int(rec.is_out_lap),
                 int(rec.is_in_lap), int(rec.is_complete), rec.track_length_m, n,
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

    def has_session(self, session_uid: str) -> bool:
        return self.find_session_lap(session_uid) is not None

    def find_session_lap(self, session_uid: str) -> int | None:
        """这个 session_uid 下任意一圈的 id（导入时用来去重）。"""
        with self.connection() as con:
            row = con.execute("SELECT id FROM laps WHERE session_uid = ? LIMIT 1", (session_uid,)).fetchone()
        return row["id"] if row else None

    def delete_lap(self, lap_id: int) -> bool:
        with self.connection() as con:
            return con.execute("DELETE FROM laps WHERE id = ?", (lap_id,)).rowcount > 0

    # ------------------------------------------------------------------- read
    def list_combos(self) -> list[dict]:
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT game, track, car,
                          COUNT(*) AS laps,
                          SUM(CASE WHEN {CLEAN_SQL} AND {OWN_SQL} THEN 1 ELSE 0 END) AS clean_laps,
                          MIN(CASE WHEN {CLEAN_SQL} AND {OWN_SQL} THEN lap_time_ms END) AS best_ms,
                          MAX(recorded_at) AS last_at
                   FROM laps GROUP BY game, track, car ORDER BY last_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def list_laps(self, game: str, track: str, car: str) -> list[dict]:
        with self.connection() as con:
            rows = con.execute(
                f"SELECT {_LAP_COLUMNS} FROM laps WHERE game = ? AND track = ? AND car = ? "
                "ORDER BY recorded_at DESC, id DESC",
                (game, track, car),
            ).fetchall()
        return [_lap_row_to_dict(r) for r in rows]

    def get_lap(self, lap_id: int) -> dict | None:
        with self.connection() as con:
            row = con.execute(f"SELECT {_LAP_COLUMNS} FROM laps WHERE id = ?", (lap_id,)).fetchone()
        return _lap_row_to_dict(row) if row else None

    def get_samples(self, lap_id: int) -> dict[str, list]:
        with self.connection() as con:
            rows = con.execute(
                f"SELECT {', '.join(SAMPLE_CHANNELS)} FROM samples WHERE lap_id = ? ORDER BY idx",
                (lap_id,),
            ).fetchall()
        cols = list(zip(*rows)) if rows else [()] * len(SAMPLE_CHANNELS)
        return {c: list(v) for c, v in zip(SAMPLE_CHANNELS, cols)}

    def best_lap(self, game: str, track: str, car: str) -> dict | None:
        with self.connection() as con:
            row = con.execute(
                f"SELECT {_LAP_COLUMNS} FROM laps WHERE game = ? AND track = ? AND car = ? "
                f"AND {CLEAN_SQL} AND {OWN_SQL} ORDER BY lap_time_ms, id LIMIT 1",
                (game, track, car),
            ).fetchone()
        return _lap_row_to_dict(row) if row else None
