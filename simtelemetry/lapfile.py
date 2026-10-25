"""单圈文件（.lap.gz）：把一圈导出给别人，或者导入别人的圈当参考圈对比。

    python -m simtelemetry.lapfile export 圈ID [-o 文件] [--note "张三 @ 车队"] [--db …]
    python -m simtelemetry.lapfile import 文件 [更多文件 …] [--db …]

文件是 gzip 压缩的 JSON：格式名、版本、导出程序版本、这一圈的信息（游戏/赛道/车型/圈时/标记…）和全部采样点。
不会自动写入任何名字；note 是导出时自己填的一句备注。

导入的圈 session_uid 以 “import-” 开头（按文件内容去重，重复导入不会多出一圈），可以选作参考圈，
但不算“个人最快圈”（实时 delta 的参考、理论最快圈都只看自己跑的圈）。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .models import SAMPLE_CHANNELS, LapRecord, SessionInfo
from .recorder import format_lap_time
from .storage import DEFAULT_DB, IMPORT_PREFIX, Storage

FORMAT = "simtelemetry-lap"
VERSION = 1
MAX_BYTES = 20 * 1024 * 1024          # 解压后上限：一圈 50 Hz 几分钟也就几百 KB，防止被塞进超大文件

_LAP_FIELDS = ("game", "track", "car", "session_type", "lap_number", "lap_time_ms", "time_source", "is_valid",
               "is_out_lap", "is_in_lap", "is_complete", "track_length_m", "recorded_at")


def export_lap(storage: Storage, lap_id: int, note: str = "") -> bytes:
    lap = storage.get_lap(lap_id)
    if lap is None:
        raise KeyError(lap_id)
    doc = {
        "format": FORMAT, "version": VERSION, "exported_by": f"simtelemetry {__version__}",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note.strip()[:200],
        "lap": {k: lap[k] for k in _LAP_FIELDS},
        "samples": storage.get_samples(lap_id),
    }
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, mtime=0)


def export_filename(lap: dict) -> str:
    t = format_lap_time(lap["lap_time_ms"]).replace(":", "m").replace(".", "s")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{lap['track']}_{lap['car']}")
    return f"{lap['game']}_{safe}_{t}.lap.gz"


def parse_lap_file(data: bytes) -> tuple[LapRecord, str]:
    """→ (可以直接 save_lap 的 LapRecord, 备注)。格式不对时抛 ValueError。"""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            raw = f.read(MAX_BYTES + 1)
    except (OSError, EOFError) as e:
        raise ValueError(f"不是 .lap.gz 文件（{e}）") from e
    if len(raw) > MAX_BYTES:
        raise ValueError("文件太大")
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise ValueError("文件内容不是 JSON") from e
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("不是 simtelemetry 导出的圈")
    if doc.get("version") != VERSION:
        raise ValueError(f"圈文件版本 {doc.get('version')} 不支持（本程序支持 {VERSION}），请升级程序")
    lap, samples = doc.get("lap") or {}, doc.get("samples") or {}
    missing = [k for k in _LAP_FIELDS if k not in lap] + [c for c in SAMPLE_CHANNELS if c not in samples]
    if missing:
        raise ValueError(f"圈文件缺少字段：{', '.join(missing)}")
    n = len(samples["t_ms"])
    if n < 2 or any(len(samples[c]) != n for c in SAMPLE_CHANNELS):
        raise ValueError("采样点数据不完整")
    length = lap["track_length_m"]
    if length is not None:
        try:
            length = float(length)
        except (TypeError, ValueError):
            raise ValueError("圈文件里的赛道长度不是数字") from None
        if not (math.isfinite(length) and length > 0):
            raise ValueError("圈文件里的赛道长度不对")
    try:
        recorded = datetime.fromisoformat(lap["recorded_at"])
        session = SessionInfo(game=str(lap["game"]), track=str(lap["track"]), car=str(lap["car"]),
                              track_length_m=length, session_type=str(lap["session_type"]))
        rec = LapRecord(
            session=session, session_uid=IMPORT_PREFIX + hashlib.sha256(raw).hexdigest()[:12],
            lap_number=int(lap["lap_number"]), lap_time_ms=int(lap["lap_time_ms"]), time_source=str(lap["time_source"]),
            is_valid=bool(lap["is_valid"]), is_out_lap=bool(lap["is_out_lap"]), is_in_lap=bool(lap["is_in_lap"]),
            is_complete=bool(lap["is_complete"]), recorded_at=recorded,
            samples={c: list(samples[c]) for c in SAMPLE_CHANNELS},
            track_length_m=length,
        )
    except (TypeError, ValueError, KeyError) as e:
        raise ValueError(f"圈文件内容有误：{e}") from e
    if rec.lap_time_ms <= 0:
        raise ValueError("圈时不对")
    return rec, str(doc.get("note") or "")[:200]


def import_lap(storage: Storage, data: bytes) -> dict:
    """导入一圈，返回 {id, duplicate, note, lap}。同一个文件导两次只保存一次。"""
    rec, note = parse_lap_file(data)
    existing = storage.find_session_lap(rec.session_uid)
    if existing is not None:
        return {"id": existing, "duplicate": True, "note": note, "lap": storage.get_lap(existing)}
    lap_id = storage.save_lap(rec)
    return {"id": lap_id, "duplicate": False, "note": note, "lap": storage.get_lap(lap_id)}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m simtelemetry.lapfile", description="导出 / 导入单圈文件（.lap.gz）")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="导出一圈")
    e.add_argument("lap_id", type=int)
    e.add_argument("-o", "--output")
    e.add_argument("--note", default="", help="附在文件里的一句备注，比如车手名、车队、调教")
    i = sub.add_parser("import", help="导入别人的圈")
    i.add_argument("files", nargs="+")
    args = p.parse_args(argv)

    storage = Storage(args.db)
    if args.cmd == "export":
        lap = storage.get_lap(args.lap_id)
        if lap is None:
            p.error(f"没有 id 为 {args.lap_id} 的圈")
        out = Path(args.output or export_filename(lap))
        out.write_bytes(export_lap(storage, args.lap_id, args.note))
        print(f"已导出 {format_lap_time(lap['lap_time_ms'])} → {out}")
        return
    for f in args.files:
        try:
            r = import_lap(storage, Path(f).read_bytes())
        except (OSError, ValueError) as err:
            print(f"{f}：跳过（{err}）")
            continue
        lap = r["lap"]
        what = "已经导入过" if r["duplicate"] else "已导入"
        note = f"（{r['note']}）" if r["note"] else ""
        print(f"{f}：{what} {lap['game']} / {lap['track']} / {lap['car']} {format_lap_time(lap['lap_time_ms'])}{note}")


if __name__ == "__main__":
    main()
