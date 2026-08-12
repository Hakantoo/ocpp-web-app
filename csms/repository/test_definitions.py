"""Saved, reusable stress-test definitions.

Deliberately separate from an actual run: a run is transient, real,
in-progress-or-finished execution (tracked in memory on the CSMS process --
see domain/stress_test.py), while a definition is the reusable template you
come back to and re-run whenever you want. That is the thing worth
persisting across a restart and sharing across devices.
"""

from __future__ import annotations

import json
from typing import Any

Conn = Any


async def create(conn: Conn, *, name: str, steps: list[dict[str, Any]]) -> int:
    async with conn.execute(
        "INSERT INTO test_definitions (name, steps_json) VALUES (?, ?)",
        (name, json.dumps(steps)),
    ) as cur:
        return int(cur.lastrowid or 0)


async def list_all(conn: Conn) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT id, name, steps_json, created_at FROM test_definitions "
        "ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_deserialize(dict(row)) for row in rows]


async def get(conn: Conn, definition_id: int) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT id, name, steps_json, created_at FROM test_definitions WHERE id = ?",
        (definition_id,),
    ) as cur:
        row = await cur.fetchone()
    return _deserialize(dict(row)) if row else None


async def rename(conn: Conn, definition_id: int, name: str) -> None:
    await conn.execute(
        "UPDATE test_definitions SET name = ? WHERE id = ?", (name, definition_id)
    )


async def update_steps(
    conn: Conn, definition_id: int, steps: list[dict[str, Any]]
) -> None:
    await conn.execute(
        "UPDATE test_definitions SET steps_json = ? WHERE id = ?",
        (json.dumps(steps), definition_id),
    )


async def delete(conn: Conn, definition_id: int) -> None:
    await conn.execute("DELETE FROM test_definitions WHERE id = ?", (definition_id,))


def _deserialize(row: dict[str, Any]) -> dict[str, Any]:
    row["steps"] = json.loads(row.pop("steps_json"))
    return row