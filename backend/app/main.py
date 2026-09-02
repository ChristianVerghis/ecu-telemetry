"""FastAPI app factory + lifespan (ingest servers, maintenance, static dashboard)."""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .anomaly import AnomalyEngine
from .api import router
from .bus import EventBus
from .db import Database, maintenance_loop
from .diagnostics import Diagnostician
from .ingest import Ingest

STATIC = Path(__file__).resolve().parent.parent / "static"


def create_app(db_path: str | None = None, udp_port: int | None = None, tcp_port: int | None = None,
               host: str | None = None) -> FastAPI:
    udp = int(udp_port if udp_port is not None else os.environ.get("ECU_UDP_PORT", 8781))
    tcp = int(tcp_port if tcp_port is not None else os.environ.get("ECU_TCP_PORT", 8782))
    bind = host or os.environ.get("ECU_BIND", "0.0.0.0")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(db_path)
        bus = EventBus()
        engine = AnomalyEngine(db, bus)
        ingest = Ingest(db, bus, engine, udp, tcp, bind)
        app.state.db, app.state.bus, app.state.engine, app.state.ingest = db, bus, engine, ingest
        app.state.diag = Diagnostician(db, ingest)
        app.state.started_at = time.time()
        await ingest.start()
        maint = asyncio.create_task(maintenance_loop(db))
        print(f"ecu-telemetry: UDP :{ingest.udp_port}  TCP :{ingest.tcp_port}  db={db.path}")
        try:
            yield
        finally:
            maint.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maint
            await ingest.stop()
            db.close()

    app = FastAPI(title="ECU Telemetry", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)

    if STATIC.is_dir():
        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(STATIC / "index.html")

        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


app = create_app()
