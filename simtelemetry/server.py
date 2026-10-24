"""FastAPI：圈对比 REST 接口 + 实时 WebSocket + 两个页面。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .analysis import compare_laps
from .collector import Collector
from .live import LiveHub
from .setups.library import SetupLibrary
from .storage import Storage

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
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
        laps = storage.list_laps(game, track, car)
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
        return compare_laps(ref_lap, storage.get_samples(ref), cmp_lap, storage.get_samples(cmp),
                            points=points)

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
