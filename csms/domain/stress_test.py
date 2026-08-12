"""Stress-test orchestration: create real load against the CSMS, on demand.

A test is a short sequence of steps, run in order, each waiting for the
previous one to finish -- deliberately simple, not a scheduler. Every
charger a "create" step produces uses this run's own identity prefix
(`{run_id}-NNNN`), which is what lets a later "delete" step find and remove
exactly what this run created, and nothing else. Chargers are genuine
simulated hardware: this module calls the simulator's own HTTP control API
(the same one the dashboard's Simulator page already uses) to create them,
so a stress test exercises the real OCPP path, real WebSocket connections,
real database writes -- not a synthetic shortcut.

Runs entirely in memory beyond the charger/card/vehicle rows a test itself
creates. There is no "test run" table: a run's own identity prefix is
sufficient to find everything it touched, and once cleaned up there is
nothing left to store.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from ..config import settings
from ..db.database import Database

log = logging.getLogger(__name__)

StepKind = Literal[
    "create", "plug_in", "unplug", "present_card", "charge", "stop_charge",
    "remote_start", "remote_stop", "fault", "clear_fault", "wait", "delete",
]


@dataclass
class Step:
    kind: StepKind
    # -- create --
    count: int = 1
    connectors: int = 2
    # -- wait --
    seconds: float = 1.0
    # -- delete --
    delete_target: Literal["all", "created_here"] = "created_here"


@dataclass
class StepResult:
    kind: StepKind
    ok: bool
    detail: str
    started_at: float
    finished_at: float | None = None


@dataclass
class TestRun:
    id: str
    name: str
    steps: list[Step]
    status: Literal["running", "done", "failed", "cancelled"] = "running"
    results: list[StepResult] = field(default_factory=list)
    created_identities: list[str] = field(default_factory=list)
    created_tags: list[str] = field(default_factory=list)
    created_vehicle_ids: list[int] = field(default_factory=list)
    cancel_requested: bool = False
    # Set by whichever "create" step ran -- how many connectors each of this
    # run's chargers actually has, so fault/clear_fault can reach every one
    # rather than assuming there is only ever one.
    connectors_per_charger: int = 1
    # How many chargers this run has created so far, across every create
    # step -- identities are numbered from this, not from each step's own
    # local loop counter, which is what let two create steps in the same
    # run collide on the exact same identity.
    charger_counter: int = 0


# Every run in this process's lifetime, by id. A run is deliberately never
# persisted -- restarting the CSMS loses the ability to track an in-flight
# run's progress, but never loses real data, since every row a run writes
# is a genuine charge_points/id_tags/vehicles row like any other.
_runs: dict[str, TestRun] = {}


def get_run(run_id: str) -> TestRun | None:
    return _runs.get(run_id)


def list_runs() -> list[TestRun]:
    return list(_runs.values())


class StressTestRunner:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def start(self, name: str, steps: list[Step]) -> TestRun:
        run_id = uuid.uuid4().hex[:8]
        run = TestRun(id=run_id, name=name, steps=steps)
        _runs[run_id] = run
        asyncio.create_task(self._execute(run))
        return run

    def cancel(self, run_id: str) -> bool:
        run = _runs.get(run_id)
        if run is None or run.status != "running":
            return False
        run.cancel_requested = True
        return True

    async def _execute(self, run: TestRun) -> None:
        try:
            async with httpx.AsyncClient(
                base_url=settings.simulator_base_url, timeout=30.0
            ) as sim:
                for step in run.steps:
                    if run.cancel_requested:
                        run.status = "cancelled"
                        return
                    result = StepResult(
                        kind=step.kind, ok=True, detail="", started_at=time.time()
                    )
                    run.results.append(result)
                    try:
                        detail = await self._run_step(run, step, sim)
                        result.detail = detail
                    except Exception as exc:  # noqa: BLE001 -- surfaced to the UI
                        result.ok = False
                        result.detail = str(exc)
                        log.warning("Stress test %s step %s failed: %s", run.id, step.kind, exc)
                        run.status = "failed"
                        return
                    finally:
                        result.finished_at = time.time()
            run.status = "done"
        except Exception as exc:  # noqa: BLE001
            log.exception("Stress test %s crashed", run.id)
            run.status = "failed"
            if run.results:
                run.results[-1].ok = False
                run.results[-1].detail = str(exc)

    async def _run_step(self, run: TestRun, step: Step, sim: httpx.AsyncClient) -> str:
        if step.kind == "wait":
            await asyncio.sleep(step.seconds)
            return f"waited {step.seconds:g}s"

        if step.kind == "create":
            return await self._create(run, step, sim)

        if step.kind == "plug_in":
            return await self._plug_in(run, sim)

        if step.kind == "unplug":
            return await self._for_each_connector(run, sim, "/unplug", {}, "unplugged")

        if step.kind == "present_card":
            return await self._present_card(run, sim)

        if step.kind == "charge":
            return await self._for_each_connector(
                run, sim, "/power", {"offered": True}, "started charging on"
            )

        if step.kind == "stop_charge":
            return await self._for_each_connector(
                run, sim, "/power", {"offered": False}, "stopped charging on"
            )

        if step.kind == "remote_start":
            return await self._remote(run, "start", "Started")

        if step.kind == "remote_stop":
            return await self._remote(run, "stop", "Ended")

        if step.kind in ("fault", "clear_fault"):
            return await self._fault(run, step, sim, faulted=step.kind == "fault")

        if step.kind == "delete":
            return await self._delete(run, step, sim)

        raise ValueError(f"Unknown step kind {step.kind!r}")

    async def _create(self, run: TestRun, step: Step, sim: httpx.AsyncClient) -> str:
        run.connectors_per_charger = step.connectors
        created = 0
        for _ in range(step.count):
            if run.cancel_requested:
                break
            identity = f"{run.id}-{run.charger_counter:04d}"
            r = await sim.post(
                "/chargers",
                json={"identity": identity, "connectors": step.connectors},
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"charger {identity} failed to provision: {r.text[:200]}"
                )
            run.created_identities.append(identity)
            run.charger_counter += 1
            created += 1
            # Give the WebSocket a moment to actually come up before the
            # next step in the sequence tries to act on it.
            await asyncio.sleep(0.05)
        return f"created {created}/{step.count} chargers ({step.connectors} connectors each)"

    async def _for_each_connector(
        self,
        run: TestRun,
        sim: httpx.AsyncClient,
        path: str,
        extra_json: dict[str, Any],
        verb: str,
    ) -> str:
        """A real call, per connector, on every charger this run has
        created. Honestly counts what actually succeeded versus was
        genuinely refused -- a rejected call here is real, useful
        information (a badly-sequenced test, e.g. presenting a card on a
        connector that was never plugged in), not something to hide."""
        ok = 0
        rejected = 0
        for identity in run.created_identities:
            if run.cancel_requested:
                break
            for connector_id in range(1, run.connectors_per_charger + 1):
                r = await sim.post(
                    path,
                    json={"identity": identity, "connector_id": connector_id, **extra_json},
                )
                if r.status_code >= 400:
                    rejected += 1
                else:
                    ok += 1
        summary = f"{verb} {ok} connectors"
        if rejected:
            summary += f", {rejected} rejected"
        return summary

    async def _plug_in(self, run: TestRun, sim: httpx.AsyncClient) -> str:
        plugged = 0
        rejected = 0
        for identity in run.created_identities:
            if run.cancel_requested:
                break
            for connector_id in range(1, run.connectors_per_charger + 1):
                vehicle_id = await self._create_test_vehicle(run, identity, connector_id)
                r = await sim.post(
                    "/plug",
                    json={
                        "identity": identity,
                        "connector_id": connector_id,
                        "vehicle_id": vehicle_id,
                    },
                )
                if r.status_code >= 400:
                    rejected += 1
                else:
                    plugged += 1
        summary = f"plugged in {plugged} connectors"
        if rejected:
            summary += f", {rejected} rejected"
        return summary

    async def _present_card(self, run: TestRun, sim: httpx.AsyncClient) -> str:
        carded = 0
        rejected = 0
        for identity in run.created_identities:
            if run.cancel_requested:
                break
            for connector_id in range(1, run.connectors_per_charger + 1):
                tag = await self._create_test_card(run, identity, connector_id)
                r = await sim.post(
                    "/swipe",
                    json={"identity": identity, "connector_id": connector_id, "id_tag": tag},
                )
                # swipe_card always answers 200 -- refusal is in the body
                # (Invalid, ConcurrentTx), not the HTTP status, since a
                # real card reader always answers, it just sometimes says no.
                status = r.json().get("status") if r.status_code < 400 else "error"
                if status == "Accepted":
                    carded += 1
                else:
                    rejected += 1
        summary = f"{carded} cards accepted"
        if rejected:
            summary += f", {rejected} refused"
        return summary

    async def _remote(self, run: TestRun, action: str, expect_prefix: str) -> str:
        """Remote start/stop go through the real CSMS API, not the
        simulator's control API -- this is the dashboard's own path, the
        same RemoteStartTransaction/RemoteStopTransaction a person clicking
        Start or End actually triggers, so a rejection here is the CSMS's
        own real business logic (e.g. no card presented yet) refusing it."""
        ok = 0
        rejected = 0
        async with httpx.AsyncClient(base_url="http://localhost:9000", timeout=30.0) as csms:
            for identity in run.created_identities:
                if run.cancel_requested:
                    break
                for connector_id in range(1, run.connectors_per_charger + 1):
                    if action == "start":
                        r = await csms.post(
                            f"/api/charge-points/{identity}/start",
                            json={"connector_id": connector_id},
                        )
                    else:
                        # End needs a session id, which the CSMS's own
                        # overview already knows for this connector.
                        overview = await csms.get("/api/overview")
                        session_id = next(
                            (
                                c["session_id"]
                                for c in overview.json().get("connectors", [])
                                if c["charge_point_id"] == identity
                                and c["connector_id"] == connector_id
                                and c["session_id"]
                            ),
                            None,
                        )
                        if session_id is None:
                            rejected += 1
                            continue
                        r = await csms.post(f"/api/sessions/{session_id}/end")
                    if r.status_code >= 400:
                        rejected += 1
                    else:
                        ok += 1
        summary = f"{expect_prefix} {ok} connectors"
        if rejected:
            summary += f", {rejected} rejected"
        return summary

    async def _create_test_vehicle(
        self, run: TestRun, identity: str, connector_id: int
    ) -> int:
        """A genuine, distinct car for this connector -- one connector
        cannot borrow another's, since a car can only be plugged in one
        place at a time (the schema itself enforces this). Named after the
        charger and connector it belongs to, so it is easy to trace which
        car goes with which charger when watching the run.
        """
        async with self.db.transaction() as conn:
            from ..repository import tags as tags_repo

            vehicle_id = await tags_repo.create_vehicle(
                conn,
                name=f"{identity}·{connector_id}",
                battery_capacity_kwh=60.0,
                max_charge_kw=50.0,
                current_soc=20.0,
            )
        run.created_vehicle_ids.append(vehicle_id)
        return vehicle_id

    async def _create_test_card(
        self, run: TestRun, identity: str, connector_id: int
    ) -> str:
        """A genuine, distinct card for this connector, named to match --
        capped to the schema's 20 character limit on id_tag, truncating the
        identity rather than the connector suffix so the card still reads
        as "which connector" at a glance even on a long charger name.
        """
        from ..repository import tags as tags_repo

        suffix = f"-{connector_id}"
        tag = f"{identity}{suffix}"[: 20 - len(suffix)] + suffix
        async with self.db.transaction() as conn:
            await tags_repo.create(conn, id_tag=tag, status="Accepted")
        run.created_tags.append(tag)
        return tag

    async def _fault(
        self, run: TestRun, step: Step, sim: httpx.AsyncClient, *, faulted: bool
    ) -> str:
        affected = 0
        for identity in run.created_identities:
            if run.cancel_requested:
                break
            for connector_id in range(1, run.connectors_per_charger + 1):
                await sim.post(
                    "/fault",
                    json={
                        "identity": identity,
                        "connector_id": connector_id,
                        "faulted": faulted,
                    },
                )
            affected += 1
        action = "faulted" if faulted else "cleared fault on"
        return f"{action} {affected} chargers"

    async def _delete(self, run: TestRun, step: Step, sim: httpx.AsyncClient) -> str:
        """A genuine, complete removal -- every row this run's chargers,
        cards, and vehicles touched, cascaded away with no tombstone. This
        is deliberately not the same delete a real charger gets: a stress
        test's data was never real to begin with, so there is nothing
        historical worth preserving."""
        removed_chargers = 0
        for identity in list(run.created_identities):
            if run.cancel_requested:
                break
            await sim.delete(f"/chargers/{identity}")
            async with self.db.transaction() as conn:
                await conn.execute(
                    "DELETE FROM charge_points WHERE identity = ?", (identity,)
                )
            removed_chargers += 1

        async with self.db.transaction() as conn:
            for vehicle_id in run.created_vehicle_ids:
                await conn.execute(
                    "DELETE FROM vehicles WHERE id = ?", (vehicle_id,)
                )
            for tag in run.created_tags:
                await conn.execute(
                    "DELETE FROM id_tags WHERE id_tag = ?", (tag,)
                )

        run.created_identities.clear()
        run.created_vehicle_ids.clear()
        run.created_tags.clear()
        return f"deleted {removed_chargers} chargers and every row they created"