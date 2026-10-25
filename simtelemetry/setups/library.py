"""调教库：定时扫描游戏的调教目录，文件内容每变一次就在 SQLite 里存一个版本。

原则：只读游戏目录，从不修改、移动或删除里面的文件。

- ACC：<根>/<车型>/<赛道>/<名字>.json，根目录默认是“文档/Assetto Corsa Competizione/Setups”
  （文档被 OneDrive 接管时会多一层 OneDrive），可以用 --acc-setups 指定。
- LMU：<根>/<赛道>/<名字>.svm（车型写在文件里），根目录是“<游戏目录>/UserData/player/Settings”，
  默认在 Steam 的各个库目录里找，可以用 --lmu-setups 指定。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..storage import Storage
from . import parse_setup
from .acc_tables import CARS
from .base import diff_setups
from .lmu import _decode as decode_svm

log = logging.getLogger(__name__)


def default_acc_setups_dir() -> Path | None:
    home = Path.home()
    for docs in (home / "Documents", home / "OneDrive" / "Documents", home / "OneDrive" / "文档"):
        p = docs / "Assetto Corsa Competizione" / "Setups"
        if p.is_dir():
            return p
    return None


def _steam_libraries() -> list[Path]:
    roots = [Path("C:/Program Files (x86)/Steam"), Path("C:/Program Files/Steam")]
    libs: list[Path] = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            libs.append(root)
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            libs += [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', text)]
    return libs


def default_lmu_setups_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    for lib in _steam_libraries():
        p = lib / "steamapps" / "common" / "Le Mans Ultimate" / "UserData" / "player" / "Settings"
        if p.is_dir():
            return p
    return None


def car_display_name(game: str, car: str) -> str:
    if game == "acc":
        info = CARS.get(car)
        return info["name"] if info else car
    return car.replace("_", " ")


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
                if root.is_dir():
                    self._scan(game, root, counts)
        if counts["new_versions"]:
            log.info("调教库：新文件 %d，新版本 %d", counts["new_files"], counts["new_versions"])
        return counts

    @staticmethod
    def _candidates(game: str, root: Path) -> Iterator[tuple[Path, str | None, str]]:
        """(文件, 路径里的车型（没有时为 None）, 赛道)"""
        if game == "acc":
            for p in sorted(root.glob("*/*/*.json")):
                yield p, p.parent.parent.name, p.parent.name
        elif game == "lmu":
            for p in sorted(root.glob("*/*.svm")):
                yield p, None, p.parent.name

    def _scan(self, game: str, root: Path, counts: dict[str, int]) -> None:
        present: set[str] = set()
        for path, car, track in self._candidates(game, root):
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
                raw = path.read_bytes()
                text = decode_svm(raw) if game == "lmu" else raw.decode("utf-8-sig")
                setup = parse_setup(game, text)          # 游戏正在写、内容不完整时这里会失败，下一轮再试
            except (OSError, ValueError):
                continue
            mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")
            self._record(game, rel, car or setup.car, track, path.stem, text, raw, mtime, counts)
            self._seen[path] = sig
        self._mark_missing(game, present, counts)

    def _record(self, game: str, rel: str, car: str, track: str, name: str, text: str, raw: bytes, mtime: str,
                counts: dict[str, int]) -> None:
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        now = _now()
        with self.storage.connection() as con:
            row = con.execute("SELECT id, car FROM setup_files WHERE game = ? AND rel_path = ?", (game, rel)).fetchone()
            if row is not None and row["car"] != car:
                # 同一路径被换成了另一辆车的调教（LMU 的车型写在文件里）：旧记录连同历史版本改名归档，
                # 新内容另起一条，免得旧版本被挂到新车型名下
                con.execute("UPDATE setup_files SET rel_path = rel_path || ?, missing = 1 WHERE id = ?",
                            (f"#replaced-{row['id']}", row["id"]))
                row = None
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
                con.execute("INSERT INTO setup_versions (file_id, sha256, content, raw, file_mtime, captured_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (file_id, sha, text, raw, mtime, now))
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
            d["car_name"] = car_display_name(d["game"], d["car"])
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
        d["car_name"] = car_display_name(d["game"], d["car"])
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
        setup = parse_setup(v["game"], v["content"])
        meta = {k: v[k] for k in ("id", "file_id", "game", "car", "track", "name", "captured_at", "file_mtime")}
        return {**meta, "car_name": setup.car_name, "has_conversion": setup.has_conversion,
                "items": [{**asdict(it), "display": it.display} for it in setup.items()]}

    def diff(self, a_id: int, b_id: int, include_derived: bool = False) -> dict | None:
        a, b = self.get_version(a_id), self.get_version(b_id)
        if a is None or b is None:
            return None
        sa, sb = parse_setup(a["game"], a["content"]), parse_setup(b["game"], b["content"])

        def side(it):
            return None if it is None else {"raw": it.raw, "value": it.value, "display": it.display}

        return {
            "a": {"id": a_id, "name": a["name"], "game": a["game"], "car": a["car"], "track": a["track"]},
            "b": {"id": b_id, "name": b["name"], "game": b["game"], "car": b["car"], "track": b["track"]},
            "same_car": a["game"] == b["game"] and sa.car == sb.car,
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

