"""Dashboard WebSocket: a live feed of everything happening on the bus."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from ..config import settings

log = logging.getLogger(__name__)

router = APIRouter()


@router.websocket(settings.dashboard_ws_path)
async def dashboard_feed(websocket: WebSocket) -> None:
    """Push events to the browser.

    Subscribers get their own bounded queue; if a browser tab stalls, its
    oldest events are dropped rather than back-pressuring the OCPP loop.
    """
    await websocket.accept()
    bus = websocket.app.state.bus

    # Optional ?topics=session.*,connector.status filter.
    raw = websocket.query_params.get("topics")
    topics = tuple(t.strip() for t in raw.split(",")) if raw else ("*",)

    async def drain_client() -> None:
        # We do not expect messages from the browser, but a read must be
        # pending for a disconnect to be noticed promptly.
        try:
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass

    reader = asyncio.create_task(drain_client())
    try:
        async with bus.subscribe(*topics) as subscription:
            await websocket.send_json({"topic": "connected", "topics": list(topics)})
            async for event in subscription:
                if reader.done():
                    break
                await websocket.send_json(event.as_dict())
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("Dashboard feed failed")
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
