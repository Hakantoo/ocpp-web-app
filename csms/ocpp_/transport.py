"""The OCPP-J WebSocket endpoint.

Implements the handshake rules from section 3 of the OCPP-J 1.6 specification:

* The connection URL is the endpoint URL plus '/' plus a string uniquely
  identifying the charge point, percent-encoded. Starlette has already decoded
  the path parameter by the time we see it.
* The exact OCPP version must be given in Sec-WebSocket-Protocol. We accept
  'ocpp1.6'.
* If the charge point identifier is not recognised, respond HTTP 404 and abort
  the WebSocket connection.
* If we do not agree to any subprotocol the client offered, complete the
  handshake with a response that carries no Sec-WebSocket-Protocol header and
  then immediately close the connection.

That last rule is the fiddly one: the handshake must *succeed* and then close,
rather than being refused outright.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket
from starlette.responses import PlainTextResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..config import settings
from ..db.database import Database
from ..repository import charge_points as cp_repo
from .connection import ChargePointConnection

log = logging.getLogger(__name__)

router = APIRouter()


class ConnectionClosed(Exception):
    """Raised by the adapter so the ocpp library's read loop terminates."""


class StarletteWebSocketAdapter:
    """Presents a Starlette WebSocket the way the ocpp library expects.

    The library only ever needs ``recv()``, ``send()`` and ``subprotocol``.
    """

    def __init__(self, websocket: WebSocket, subprotocol: str) -> None:
        self._ws = websocket
        self.subprotocol = subprotocol

    async def recv(self) -> str:
        try:
            return await self._ws.receive_text()
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise ConnectionClosed(str(exc)) from exc

    async def send(self, message: str) -> None:
        if self._ws.client_state is not WebSocketState.CONNECTED:
            raise ConnectionClosed("socket is no longer connected")
        try:
            await self._ws.send_text(message)
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise ConnectionClosed(str(exc)) from exc

    async def close(self, code: int = 1000) -> None:
        if self._ws.client_state is WebSocketState.CONNECTED:
            await self._ws.close(code)


@router.websocket(settings.ocpp_path_prefix + "/{identity}")
async def ocpp_endpoint(websocket: WebSocket, identity: str) -> None:
    app = websocket.app
    db: Database = app.state.db
    registry = app.state.registry
    sessions = app.state.sessions

    offered = websocket.scope.get("subprotocols") or []

    # -- Is this a charge point we know about? -----------------------------
    async with db.transaction() as conn:
        known = await cp_repo.exists(conn, identity)
        if not known:
            if settings.reject_unknown_charge_points:
                log.warning("Rejecting unknown charge point %r with 404", identity)
                await websocket.send_denial_response(
                    PlainTextResponse("Unknown charge point", status_code=404)
                )
                return
            # Otherwise auto-provision, so a new unit appears in the dashboard
            # instead of silently failing to connect.
            log.info("Auto-provisioning previously unseen charge point %r", identity)
            await cp_repo.register_unknown(conn, identity)

    # -- Subprotocol negotiation -------------------------------------------
    if settings.subprotocol not in offered:
        log.warning(
            "%s offered subprotocols %s; none acceptable. Completing handshake "
            "without a Sec-WebSocket-Protocol header, then closing.",
            identity,
            offered or "<none>",
        )
        await websocket.accept()  # no subprotocol header
        await websocket.close(code=1002)
        return

    await websocket.accept(subprotocol=settings.subprotocol)
    log.info("%s connected using %s", identity, settings.subprotocol)

    adapter = StarletteWebSocketAdapter(websocket, settings.subprotocol)
    charge_point = ChargePointConnection(
        identity,
        adapter,
        db=db,
        bus=app.state.bus,
        sessions=sessions,
        registry=registry,
    )

    displaced = registry.add(charge_point)
    if displaced is not None:
        # A reconnect that arrived before we noticed the old socket had died.
        log.info("%s reconnected; discarding the previous connection", identity)

    await sessions.on_charge_point_connected(identity)

    try:
        await charge_point.start()
    except (ConnectionClosed, WebSocketDisconnect) as exc:
        # Surface the close code: 1000 is a clean close by the charger, 1001
        # "going away", 1006 an abrupt drop with no close frame (network death
        # or a watchdog kill). Knowing which tells us whether the charger chose
        # to leave or something severed the socket underneath it.
        code = getattr(exc, "code", None)
        log.info("%s disconnected (close code %s)", identity, code)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Unhandled error on the %s message loop", identity)
    finally:
        registry.remove(identity, charge_point)
        await sessions.on_charge_point_disconnected(identity)
        try:
            await adapter.close()
        except Exception:  # pragma: no cover - socket already gone
            pass