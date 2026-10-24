"""调教库：定时扫描游戏的调教目录，文件内容每变一次就在 SQLite 里存一个版本。

原则：只读游戏目录，从不修改、移动或删除里面的文件。

ACC 目录结构：<根>/<车型>/<赛道>/<名字>.json，根目录默认是
“文档/Assetto Corsa Competizione/Setups”（文档被 OneDrive 接管时会多一层 OneDrive），可以用 --acc-setups 指定。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..storage import Storage
from .acc import diff_setups, load_setup
from .acc_tables import CARS

log = logging.getLogger(__name__)


def default_acc_setups_dir() -> Path | None:
    home = Path.home()
    for docs in (home / "Documents", home / "OneDrive" / "Documents", home / "OneDrive" / "文档"):
        p = docs / "Assetto Corsa Competizione" / "Setups"
        if p.is_dir():
            return p
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_row(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["missing"] = bool(d.get("missing"))
    return d


class SetupLibrary:
    def __init__(self, storage: Storage, roots: dict[str, Path], interval_s: float = 15.0):
        self.storage = storage
        self.roots = {g: Path(p) for g, p in roots.items() if p}
        self.interval_s = interval_s
        self._seen: dict[Path, tuple[int, int]] = {}      # 文件 → (mtime_ns, size)，没变就不重新读
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- 后台扫描
    def start(self) -> None:
        if not self.roots:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="setup-scan", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:
                log.exception("扫描调教目录出错")
            self._stop.wait(self.interval_s)

    # ------------------------------------------------------------------ 扫描
    def scan(self) -> dict[str, int]:
        counts = {"new_files": 0, "new_versions": 0, "missing": 0}
        with self._lock:
            for game, root in self.roots.items():
                if game != "acc":
                    continue
                self._scan_acc(root, counts)
        if counts["new_versions"]:
            log.info("调教库：新文件 %d，新版本 %d", counts["new_files"], counts["new_versions"])
        return counts

    def _scan_acc(self, root: Path, counts: dict[str, int]) -> None:
        if not root.is_dir():
            return
        present: set[str] = set()
        for path in sorted(root.glob("*/*/*.json")):
            rel = path.relative_to(root).as_posix()
            present.add(rel)
            try:
                st = path.stat()
            except OSError:
                continue
            sig = (st.st_mtime_ns, st.st_size)
            if self._seen.get(path) == sig:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
                load_setup(json.loads(text))           # 游戏正在写、内容不完整时这里会失败，下一轮再试
            except (OSError, ValueError):
                continue
            car, track = path.parent.parent.name, path.parent.name
            mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")
            self._record("acc", rel, car, track, path.stem, text, mtime, counts)
            self._seen[path] = sig
        self._mark_missing("acc", present, counts)

    def _record(self, game: str, rel: str, car: str, track: str, name: str, text: str, mtime: str,
                counts: dict[str, int]) -> None:
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        now = _now()
        with self.storage.connection() as con:
            row = con.execute("SELECT id FROM setup_files WHERE game = ? AND rel_path = ?", (game, rel)).fetchone()
            if row is None:
                file_id = con.execute(
                    "INSERT INTO setup_files (game, rel_path, car, track, name, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (game, rel, car, track, name, now, now)).lastrowid
                counts["new_files"] += 1
            else:
                file_id = row["id"]
                con.execute("UPDATE setup_files SET last_seen = ?, missing = 0 WHERE id = ?", (now, file_id))
            last = con.execute("SELECT sha256 FROM setup_versions WHERE file_id = ? ORDER BY id DESC LIMIT 1",
                               (file_id,)).fetchone()
            if last is None or last["sha256"] != sha:
                con.execute("INSERT INTO setup_versions (file_id, sha256, content, file_mtime, captured_at) "
                            "VALUES (?, ?, ?, ?, ?)", (file_id, sha, text, mtime, now))
                counts["new_versions"] += 1

    def _mark_missing(self, game: str, present: set[str], counts: dict[str, int]) -> None:
        with self.storage.connection() as con:
            rows = con.execute("SELECT id, rel_path FROM setup_files WHERE game = ? AND missing = 0", (game,)).fetchall()
            gone = [r["id"] for r in rows if r["rel_path"] not in present]
            con.executemany("UPDATE setup_files SET missing = 1 WHERE id = ?", [(i,) for i in gone])
        counts["missing"] += len(gone)

    # ------------------------------------------------------------------ 查询
    def list_files(self, game: str | None = None, car: str | None = None, track: str | None = None) -> list[dict]:
        where, args = [], []
        for col, val in (("f.game", game), ("f.car", car), ("f.track", track)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        sql = ("SELECT f.*, COUNT(v.id) AS versions, MAX(v.captured_at) AS last_changed, MAX(v.id) AS latest_version_id "
               "FROM setup_files f LEFT JOIN setup_versions v ON v.file_id = f.id "
               + (f"WHERE {' AND '.join(where)} " if where else "")
               + "GROUP BY f.id ORDER BY f.car, f.track, f.name")
        with self.storage.connection() as con:
            rows = con.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = _file_row(r)
            d["car_name"] = _car_name(d["car"])
            out.append(d)
        return out

    def get_file(self, file_id: int) -> dict | None:
        with self.storage.connection() as con:
            row = con.execute("SELECT * FROM setup_files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                return None
            versions = con.execute("SELECT id, sha256, file_mtime, captured_at FROM setup_versions "
                                   "WHERE file_id = ? ORDER BY id DESC", (file_id,)).fetchall()
        d = _file_row(row)
        d["car_name"] = _car_name(d["car"])
        d["versions"] = [dict(v) for v in versions]
        return d

    def get_version(self, version_id: int) -> dict | None:
        with self.storage.connection() as con:
            row = con.execute("SELECT v.*, f.game, f.car, f.track, f.name, f.rel_path FROM setup_versions v "
                              "JOIN setup_files f ON f.id = v.file_id WHERE v.id = ?", (version_id,)).fetchone()
        return dict(row) if row else None

    def version_items(self, version_id: int) -> dict | None:
        v = self.get_version(version_id)
        if v is None:
            return None
        setup = load_setup(json.loads(v["content"]))
        meta = {k: v[k] for k in ("id", "file_id", "game", "car", "track", "name", "captured_at", "file_mtime")}
        return {**meta, "car_name": setup.car_name, "has_conversion": setup.has_conversion,
                "items": [{**asdict(it), "display": it.display} for it in setup.items()]}

    def diff(self, a_id: int, b_id: int, include_derived: bool = False) -> dict | None:
        a, b = self.get_version(a_id), self.get_version(b_id)
        if a is None or b is None:
            return None
        sa, sb = load_setup(json.loads(a["content"])), load_setup(json.loads(b["content"]))

        def side(it):
            return None if it is None else {"raw": it.raw, "value": it.value, "display": it.display}

        return {
            "a": {"id": a_id, "name": a["name"], "car": a["car"], "track": a["track"]},
            "b": {"id": b_id, "name": b["name"], "car": b["car"], "track": b["track"]},
            "same_car": sa.car == sb.car,
            "changes": [{"key": c.key, "group": c.group, "label": c.label, "unit": c.unit, "derived": c.derived,
                         "clicks": c.clicks, "a": side(c.a), "b": side(c.b)}
                        for c in diff_setups(sa, sb, include_derived)],
        }

    def update_file(self, file_id: int, note: str | None = None, tags: list[str] | None = None) -> dict | None:
        with self.storage.connection() as con:
            if note is not None:
                con.execute("UPDATE setup_files SET note = ? WHERE id = ?", (note, file_id))
            if tags is not None:
                clean = sorted({t.strip() for t in tags if t and t.strip()})
                con.execute("UPDATE setup_files SET tags = ? WHERE id = ?", (json.dumps(clean, ensure_ascii=False), file_id))
        return self.get_file(file_id)


def _car_name(car: str) -> str:
    info = CARS.get(car)
    return info["name"] if info else car
