"""FastAPI：圈对比 REST 接口 + 实时 WebSocket + 两个页面。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .analysis import compare_laps
from .collector import Collector
from .corners import corner_table, theoretical_best
from .lapfile import export_filename, export_lap, import_lap
from .live import LiveHub
from .paths import RESOURCE_ROOT
from .setups.library import SetupLibrary
from .setups.linking import annotate_laps, names_match, set_session_setup, setup_candidates, version_info
from .storage import Storage

log = logging.getLogger(__name__)

WEB_DIR = RESOURCE_ROOT / "web"
VENDOR_DIR = WEB_DIR / "vendor"

# 前端图表库。web/vendor/ 里有本地文件就用本地的（离线可用），否则跳转到 CDN。
UPLOT_VERSION = "1.6.32"
CDN = {
    "uPlot.iife.min.js": f"https://cdn.jsdelivr.net/npm/uplot@{UPLOT_VERSION}/dist/uPlot.iife.min.js",
    "uPlot.min.css": f"https://cdn.jsdelivr.net/npm/uplot@{UPLOT_VERSION}/dist/uPlot.min.css",
}

NO_CACHE = {"Cache-Control": "no-cache"}


def create_app(storage: Storage, hub: LiveHub, collector: Collector | None = None,
               ws_hz: float = 20.0, setups: SetupLibrary | None = None) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if collector:
            collector.start()
        if setups:
            setups.start()
        yield
        if setups:
            await asyncio.to_thread(setups.stop)
        if collector:
            await asyncio.to_thread(collector.stop)

    app = FastAPI(title="Sim Telemetry", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    def page_compare():
        return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)

    @app.get("/live", include_in_schema=False)
    def page_live():
        return FileResponse(WEB_DIR / "live.html", headers=NO_CACHE)

    @app.get("/setups", include_in_schema=False)
    def page_setups():
        return FileResponse(WEB_DIR / "setups.html", headers=NO_CACHE)

    @app.get("/vendor/{name}", include_in_schema=False)
    def vendor(name: str):
        if name not in CDN:
            raise HTTPException(404)
        local = VENDOR_DIR / name
        if local.is_file():
            return FileResponse(local)
        return RedirectResponse(CDN[name], status_code=307)

    # -------------------------------------------------------------------- api
    @app.get("/api/status")
    def api_status():
        return {
            "collector": collector.status() if collector else None,
            "live": hub.snapshot(),
        }

    @app.get("/api/combos")
    def api_combos():
        return storage.list_combos()

    @app.get("/api/laps")
    def api_laps(game: str, track: str, car: str):
        laps = annotate_laps(storage, storage.list_laps(game, track, car))
        best = storage.best_lap(game, track, car)
        return {"laps": laps, "best_lap_id": best["id"] if best else None}

    @app.get("/api/laps/{lap_id}")
    def api_lap(lap_id: int):
        lap = storage.get_lap(lap_id)
        if not lap:
            raise HTTPException(404, "lap not found")
        return lap

    @app.get("/api/laps/{lap_id}/samples")
    def api_lap_samples(lap_id: int):
        if not storage.get_lap(lap_id):
            raise HTTPException(404, "lap not found")
        return storage.get_samples(lap_id)

    @app.delete("/api/laps/{lap_id}")
    def api_delete_lap(lap_id: int):
        if not storage.delete_lap(lap_id):
            raise HTTPException(404, "lap not found")
        return {"deleted": lap_id}

    @app.get("/api/compare")
    def api_compare(ref: int, cmp: int, points: int = Query(1500, ge=50, le=10000)):
        ref_lap, cmp_lap = storage.get_lap(ref), storage.get_lap(cmp)
        if not ref_lap or not cmp_lap:
            raise HTTPException(404, "lap not found")
        res = compare_laps(ref_lap, storage.get_samples(ref), cmp_lap, storage.get_samples(cmp),
                           points=points)
        res["corners"] = corner_table(res)
        for side in ("ref", "cmp"):
            res[side].pop("t", None)          # 只用来分段，不发给前端
        return res

    @app.get("/api/laps/{lap_id}/export")
    def api_export_lap(lap_id: int, note: str = ""):
        lap = storage.get_lap(lap_id)
        if not lap:
            raise HTTPException(404, "lap not found")
        return Response(export_lap(storage, lap_id, note), media_type="application/gzip", headers={
            "Content-Disposition": f'attachment; filename="{quote(export_filename(lap))}"'})

    @app.post("/api/laps/import")
    async def api_import_lap(request: Request):
        """请求体就是 .lap.gz 文件本身（不用 multipart，免得多一个依赖）。"""
        size = int(request.headers.get("content-length") or 0)
        if size > 30 * 1024 * 1024:
            raise HTTPException(413, "文件太大")
        data = await request.body()
        try:
            return await asyncio.to_thread(import_lap, storage, data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/setup_choices")
    def api_setup_choices(game: str, car: str, track: str):
        """给圈选调教时的候选：车型对得上的所有调教版本，赛道也对得上的排前面。"""
        out = setup_candidates(storage, game, car)
        for c in out:
            c["track_match"] = names_match(c["track"], track)
        out.sort(key=lambda c: not c["track_match"])            # 稳定排序：组内仍按时间新→旧
        return out[:300]

    @app.put("/api/sessions/{session_uid}/setup")
    def api_session_setup(session_uid: str, version_id: int | None = Body(None), auto: bool = Body(False)):
        """给一个会话（同一次上赛道的所有圈）指定调教；auto=true 回到自动猜，version_id=null 表示“没有/不记得”。"""
        if storage.find_session_lap(session_uid) is None:
            raise HTTPException(404, "session not found")
        if version_id is not None and version_info(storage, version_id) is None:
            raise HTTPException(404, "setup version not found")
        set_session_setup(storage, session_uid, version_id, auto=auto)
        return {"session_uid": session_uid, "version_id": version_id, "auto": auto}

    @app.get("/api/theoretical_best")
    def api_theoretical_best(game: str, track: str, car: str):
        """理论最快圈：每段取各干净圈里最快的一段。少于 2 个干净圈时 best/theoretical 为 null。"""
        clean = [l for l in storage.list_laps(game, track, car) if l["is_clean"] and not l["is_imported"]]
        res = theoretical_best([(l, storage.get_samples(l["id"])) for l in clean]) if len(clean) >= 2 else None
        if not res:
            return {"best_ms": None, "theoretical_ms": None, "gain_ms": None, "laps": len(clean), "segments": []}
        return res

    # ----------------------------------------------------------------- setups
    def lib() -> SetupLibrary:
        if setups is None:
            raise HTTPException(404, "调教库没有启用")
        return setups

    @app.get("/api/setups")
    def api_setups(game: str | None = None, car: str | None = None, track: str | None = None):
        if setups is None:
            return {"enabled": False, "roots": {}, "files": []}
        return {"enabled": True, "roots": {g: str(p) for g, p in setups.roots.items()},
                "files": setups.list_files(game, car, track)}

    @app.post("/api/setups/scan")
    async def api_setups_scan():
        return await asyncio.to_thread(lib().scan)

    @app.get("/api/setups/files/{file_id}")
    def api_setup_file(file_id: int):
        f = lib().get_file(file_id)
        if not f:
            raise HTTPException(404, "setup not found")
        return f

    @app.patch("/api/setups/files/{file_id}")
    def api_setup_file_update(file_id: int, note: str | None = Body(None), tags: list[str] | None = Body(None)):
        f = lib().update_file(file_id, note=note, tags=tags)
        if not f:
            raise HTTPException(404, "setup not found")
        return f

    @app.get("/api/setups/versions/{version_id}")
    def api_setup_version(version_id: int):
        v = lib().version_items(version_id)
        if not v:
            raise HTTPException(404, "version not found")
        return v

    @app.get("/api/setups/versions/{version_id}/raw")
    def api_setup_version_raw(version_id: int):
        """原始 JSON，用来自己备份 / 手动放回游戏目录。"""
        v = lib().get_version(version_id)
        if not v:
            raise HTTPException(404, "version not found")
        return Response(v["content"], media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="{quote(v["name"])}.json"'})

    @app.get("/api/setups/diff")
    def api_setup_diff(a: int, b: int, derived: bool = False):
        d = lib().diff(a, b, include_derived=derived)
        if not d:
            raise HTTPException(404, "version not found")
        return d

    # --------------------------------------------------------------- realtime
    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        await ws.accept()
        try:
            hz = float(ws.query_params.get("hz", ws_hz))
        except ValueError:
            hz = ws_hz
        interval = 1.0 / max(1.0, min(hz, 50.0))
        closed = asyncio.Event()

        async def watch_close():
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                closed.set()

        watcher = asyncio.create_task(watch_close())
        last_seq, last_sent = -1, 0.0
        loop = asyncio.get_running_loop()
        try:
            while not closed.is_set():
                snap = hub.snapshot()
                now = loop.time()
                if snap.get("seq") != last_seq or now - last_sent > 2.0:   # 无变化时 2 s 心跳一次
                    await ws.send_json(snap)
                    last_seq, last_sent = snap.get("seq"), now
                await asyncio.sleep(interval)
        except Exception as e:  # 客户端断开时 send 会抛各种异常，安静结束即可
            log.debug("WebSocket 结束：%r", e)
        finally:
            watcher.cancel()

    return app
