"""Receives diagnostics files the charger uploads after GetDiagnostics.

OCPP carries only the filename and the progress notifications; the file itself
comes over a separate channel. GetDiagnostics hands the charger a location, and
the charger uploads there out of band. This is that destination.

It is mounted at the site root rather than under /api because the location we
give the charger has to be a plain URL it can PUT to -- the charger knows
nothing about our API layout, only the address it was handed.

Chargers differ on method: most PUT the file to location/filename, some POST to
the location itself. Both are accepted, and the filename is taken from the path
when present and from the upload otherwise, so neither convention is turned
away for a detail that does not matter.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from ..config import settings

router = APIRouter()

#: Uploaded files land here, beside the database.
DIAGNOSTICS_DIR = settings.data_dir / "diagnostics"
DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)

#: A charger names its own file, so the name is untrusted. Keep it to something
#: that cannot climb out of the folder or overwrite anything surprising.
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(raw: str) -> str:
    name = Path(raw).name  # strip any directory part
    name = _SAFE.sub("_", name)
    return name or "diagnostics.bin"


async def _store(request: Request, filename: str) -> Response:
    body = await request.body()
    if not body:
        # Some chargers send a header-only probe before the real upload.
        return Response(status_code=200)
    path = DIAGNOSTICS_DIR / _safe_name(filename)
    path.write_bytes(body)
    return JSONResponse(
        {"stored": path.name, "bytes": len(body)}, status_code=201
    )


@router.put("/diagnostics/{filename}")
async def put_named(request: Request, filename: str) -> Response:
    return await _store(request, filename)


@router.post("/diagnostics/{filename}")
async def post_named(request: Request, filename: str) -> Response:
    return await _store(request, filename)


@router.post("/diagnostics")
async def post_root(request: Request) -> Response:
    """A charger that POSTs to the bare location, filename in a header if at all."""
    name = (
        request.headers.get("x-file-name")
        or request.headers.get("content-disposition", "")
        .partition("filename=")[2]
        .strip('"')
        or "diagnostics.bin"
    )
    return await _store(request, name)


# -- browsing what was received, from the dashboard -------------------------


@router.get("/api/diagnostics")
async def list_files(charge_point_id: str | None = None) -> list[dict]:
    """Newest first, so the file you just pulled is at the top.

    Every diagnostics file is named "{identity}-diagnostics-...", by both
    real hardware and the simulator -- the one reliable signal available for
    filtering by charger, since the upload itself carries no other link back
    to which charge point sent it.
    """
    files = sorted(
        DIAGNOSTICS_DIR.glob("*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "name": p.name,
            "bytes": p.stat().st_size,
            "received_at": datetime.fromtimestamp(
                p.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for p in files
        if p.is_file()
        and (charge_point_id is None or p.name.startswith(f"{charge_point_id}-"))
    ]


@router.get("/api/diagnostics/{filename}")
async def download(filename: str) -> FileResponse:
    path = DIAGNOSTICS_DIR / _safe_name(filename)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")