"""Application assembly.

One process serves both the OCPP WebSocket endpoint and the dashboard API.
The pieces are wired together here and nowhere else, so every module can be
imported and tested without starting a server.

Layering, strictly downward:

    transport / routes   ->  domain services  ->  repository  ->  Database

The domain layer talks to chargers through the ChargePointCommands protocol,
which the registry implements. Nothing in domain/ imports from ocpp_/.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router as api_router
from .api.diagnostics import router as diagnostics_router
from .api.ws import router as dashboard_router
from .bus import InProcessEventBus
from .config import settings
from .db.database import Database
from .domain.sessions import SessionService
from .ocpp_.registry import ChargePointRegistry
from .ocpp_.transport import router as ocpp_router
from .repository import charge_points as cp_repo
from .repository import uptime as uptime_repo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# The ocpp library logs every frame at INFO; we already persist them all.
logging.getLogger("ocpp").setLevel(logging.WARNING)

log = logging.getLogger("csms")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(
        settings.database_path,
        pool_size=settings.db_pool_size,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    await db.connect()
    await db.initialise()

    # A charger the database still calls "online" from before this process
    # started cannot actually be connected to it -- the registry that would
    # know about a live socket is brand new and empty. The previous process
    # never got to run its own disconnect hook when it stopped (a plain kill
    # or crash skips it entirely), so that half of the connected/disconnected
    # pair was simply never written. Left alone, the uptime history treats
    # that gap as one unbroken "connected" streak spanning the entire time
    # nothing was even running. Closing it out here, at the moment we know
    # for certain nothing is connected yet, is the only place this can be
    # done reliably.
    async with db.transaction() as conn:
        for cp in await cp_repo.list_all(conn):
            if cp["is_online"]:
                await cp_repo.set_online(conn, cp["identity"], False)
                await uptime_repo.record(conn, cp["identity"], "disconnected")
                log.info(
                    "%s was marked online from a previous run; closing that "
                    "out before accepting new connections",
                    cp["identity"],
                )

    bus = InProcessEventBus()
    registry = ChargePointRegistry()
    sessions = SessionService(db, bus, registry)

    app.state.db = db
    app.state.bus = bus
    app.state.registry = registry
    app.state.sessions = sessions

    log.info(
        "CSMS ready: OCPP on ws://%s:%s%s/{chargePointId}, API on http://%s:%s/api",
        settings.host, settings.port, settings.ocpp_path_prefix,
        settings.host, settings.port,
    )
    try:
        yield
    finally:
        await db.close()
        log.info("CSMS stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="OCPP 1.6J Central System",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ocpp_router)
    app.include_router(dashboard_router)
    app.include_router(api_router)
    app.include_router(diagnostics_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "csms.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        # Keep the WebSocket warm with a frequent ping. The charger answers
        # these, and the pong is what holds the socket open between OCPP
        # Heartbeats, which are minutes apart -- without it an idle connection
        # is torn down within seconds by the charger's watchdog or the network.
        # A short interval stays well inside that window; a generous timeout
        # tolerates a charger that is slow to pong without dropping it.
        ws_ping_interval=10.0,
        ws_ping_timeout=60.0,
    )