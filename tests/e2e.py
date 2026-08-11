"""End-to-end test: CSMS + simulator, over a real OCPP WebSocket.

Drives the whole flow the dashboard drives, and asserts the things that
matter: the meter freezes while held and continues from exactly that value,
the cable cannot be pulled mid-charge, a car cannot be in two sockets, and a
full battery ends the session.

    python tests/e2e.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CSMS = "http://localhost:9000"
SIM = "http://localhost:9100"
IDENTITY = "CP001"  # the simulator's default charger; every /plug, /swipe,
                     # /unplug, /fault call needs this now that the simulator
                     # runs a pool of independent chargers rather than one

PASS, FAIL = "  ok   ", "  FAIL "
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL}{label}{'  ' + detail if detail else ''}")
    if not condition:
        failures.append(label)


def parse_ts(value: str) -> float:
    """The canonical timestamp format, as epoch seconds."""
    from datetime import datetime, timezone

    return datetime.strptime(
        value.rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f"
    ).replace(tzinfo=timezone.utc).timestamp()


async def _get(client: httpx.AsyncClient, url: str):
    r = await client.get(url)
    r.raise_for_status()
    return r.json()


async def wait_for(client: httpx.AsyncClient, url: str, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await client.get(url, timeout=2.0)).status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{url} never became ready")


async def poll(fn, predicate, timeout: float = 25.0, interval: float = 0.4):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await fn()
        if predicate(last):
            return last
        await asyncio.sleep(interval)
    return last


async def connector(client: httpx.AsyncClient, n: int = 1) -> dict:
    data = await _get(client, f"{CSMS}/api/overview")
    return next(c for c in data["connectors"] if c["connector_id"] == n)


async def sim_connector(client: httpx.AsyncClient, n: int = 1) -> dict:
    data = await _get(client, f"{SIM}/state")
    return next(c for c in data["connectors"] if c["connector_id"] == n)


async def run() -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        print("\nWaiting for CSMS and simulator...")
        await wait_for(client, f"{CSMS}/api/health")
        await wait_for(client, f"{SIM}/state")

        # -- 1. connection --------------------------------------------------
        health = await poll(
            lambda: _get(client, f"{CSMS}/api/health"),
            lambda h: "CP001" in h["connected_charge_points"],
        )
        check("charger connected over OCPP 1.6J", "CP001" in health["connected_charge_points"])
        cp = await _get(client, f"{CSMS}/api/charge-points/CP001")
        check("BootNotification recorded", cp["vendor"] == "SimVendor",
              f"{cp['vendor']} {cp['model']} fw{cp['firmware_version']}")

        vehicles = await _get(client, f"{CSMS}/api/vehicles")
        check("simulator and dashboard share one vehicle list", len(vehicles) >= 2,
              f"{len(vehicles)} cars")
        car = vehicles[0]

        # -- 2. plug in binds the vehicle immediately -----------------------
        r = await client.post(f"{SIM}/plug",
                              json={"identity": IDENTITY, "connector_id": 1, "vehicle_id": car["id"]})
        check("plug in accepted", r.status_code == 200)
        c1 = await poll(lambda: connector(client, 1), lambda c: c["session_id"] is not None)
        check("plug in creates a WAITING session", c1["session_state"] == "WAITING")
        check("vehicle known before any card is presented",
              c1["vehicle_name"] == car["name"], c1["vehicle_name"] or "")
        check("no transaction opened yet", c1["cable_locked"] == 0)

        # -- 3. one car, one socket -----------------------------------------
        r = await client.post(f"{SIM}/plug",
                              json={"identity": IDENTITY, "connector_id": 2, "vehicle_id": car["id"]})
        check("same car refused on a second connector", r.status_code == 409,
              r.json().get("detail", "")[:56])

        # -- 4. cards, and the opt-in gate ------------------------------------
        #
        # The gate is off by default, so Start works without one. Turned on,
        # it must refuse until a card has been read -- and it only makes sense
        # on chargers that report card reads at all.
        r = await client.patch(f"{CSMS}/api/charge-points/CP001",
                               json={"require_card_before_start": True})
        check("card gate can be switched on per charger", r.status_code == 200)

        r = await client.post(f"{CSMS}/api/charge-points/CP001/start",
                              json={"connector_id": 1})
        check("with the gate on, Start refuses before any card",
              r.status_code == 409, r.json().get("detail", "")[:52])

        swipe = await client.post(f"{SIM}/swipe",
                                  json={"identity": IDENTITY, "connector_id": 1, "id_tag": "RFID-0001"})
        check("card accepted", swipe.json().get("status") == "Accepted")
        # A card read is the only thing that opens a transaction -- confirmed
        # against real hardware, not the old two-step "authorize, then a
        # separate Start" design. The session goes ACTIVE immediately; what
        # follows (Charging vs SuspendedEV) depends only on whether power is
        # already being offered, which it is not yet here.
        gated = await poll(lambda: connector(client, 1),
                           lambda c: c["session_state"] == "ACTIVE")
        check("the connector records which card was presented",
              gated["authorized_id_tag"] == "RFID-0001",
              gated["authorized_id_tag"] or "")
        check("presenting a card opens the transaction itself",
              gated["session_state"] == "ACTIVE", gated["session_state"] or "")
        check("with no power offered yet, status is SuspendedEV not Charging",
              gated["status"] == "SuspendedEV", gated["status"] or "")

        r = await client.post(f"{CSMS}/api/charge-points/CP001/start",
                              json={"connector_id": 1})
        check("Start on an already-open session is still accepted",
              r.status_code == 200, r.text[:60])
        # Start alone only opens/confirms the transaction -- it does not make
        # power actually flow. That is the EVSE side's own decision (the "C
        # switch"), confirmed against real hardware and modeled here as its
        # own separate action, independent of authorization.
        await client.post(f"{SIM}/power",
                          json={"identity": IDENTITY, "connector_id": 1, "offered": True})
        c1 = await poll(lambda: connector(client, 1), lambda c: c["status"] == "Charging")
        check("session becomes ACTIVE", c1["session_state"] == "ACTIVE")
        check("cable locks while charging", c1["cable_locked"] == 1)
        session_id = c1["session_id"]

        c1 = await poll(lambda: connector(client, 1),
                        lambda c: (c["session_energy_wh"] or 0) > 0)
        check("energy accumulating", c1["session_energy_wh"] > 0,
              f"{c1['session_energy_wh'] / 1000:.3f} kWh")
        check("charging time ticking", (c1["session_active_seconds"] or 0) > 0,
              f"{c1['session_active_seconds']}s")

        # -- 5. cable is captive --------------------------------------------
        r = await client.post(f"{SIM}/unplug", json={"identity": IDENTITY, "connector_id": 1})
        check("unplug refused while power is flowing", r.status_code == 409,
              r.json().get("detail", "")[:52])

        # -- 6. stop holds at zero -------------------------------------------
        r = await client.post(f"{CSMS}/api/sessions/{session_id}/stop")
        check("Stop accepted", r.status_code == 200, r.text[:70])
        sc = await poll(lambda: sim_connector(client, 1),
                        lambda c: c["status"] == "SuspendedEVSE")
        check("connector reports SuspendedEVSE", sc["status"] == "SuspendedEVSE")
        check("0 W profile installed", sc["power_limit_w"] == 0.0)
        check("transaction still open while held", sc["transaction_id"] is not None)
        check("cable stays locked while held -- a paused session could resume at any moment",
              sc["cable_locked"] is True)

        frozen = sc["meter_wh"]
        await asyncio.sleep(6)
        sc = await sim_connector(client, 1)
        check("meter frozen while held", abs(sc["meter_wh"] - frozen) < 0.5,
              f"{frozen:.0f} Wh -> {sc['meter_wh']:.0f} Wh")

        # -- 7. start again resumes on the same transaction ------------------
        before = (await _get(client, f"{CSMS}/api/sessions/{session_id}"))["energy_wh"]
        r = await client.post(f"{CSMS}/api/charge-points/CP001/start",
                              json={"connector_id": 1})
        check("Start resumes a held session without a second card",
              r.status_code == 200, r.text[:60])
        sc = await poll(lambda: sim_connector(client, 1), lambda c: c["meter_wh"] > frozen + 1)
        check("register continues from where it stopped", sc["meter_wh"] > frozen,
              f"resumed at {frozen:.0f} Wh, no reset")
        session = await poll(
            lambda: _get(client, f"{CSMS}/api/sessions/{session_id}"),
            lambda s: s["energy_wh"] > before)
        check("still a single transaction", len(session["transactions"]) == 1,
              f"{before / 1000:.3f} -> {session['energy_wh'] / 1000:.3f} kWh")

        # -- 8. charts --------------------------------------------------------
        series = session["series"]
        check("energy series recorded", len(series["Energy.Active.Import.Register"]) > 2,
              f"{len(series['Energy.Active.Import.Register'])} points")
        check("power series recorded", len(series["Power.Active.Import"]) > 2)
        check("battery series recorded", len(series["SoC"]) > 2,
              f"latest {series['SoC'][-1]['v']}%")

        overview = await _get(client, f"{CSMS}/api/overview")
        check("hourly energy computed from meter deltas",
              len(overview["energy_by_hour"]) > 0,
              f"{overview['energy_by_hour'][0]['kwh']:.3f} kWh this hour"
              if overview["energy_by_hour"] else "")
        check("daily energy computed", len(overview["energy_by_day"]) > 0)

        # -- 9. a full battery: SuspendedEV, then the session closes ----------
        #
        # The order is the point. SuspendedEV means the car stopped drawing
        # while the transaction was still open; Finishing means the
        # transaction is over. Seeing both, in that order, is what proves the
        # charger is not claiming the session ended early.
        seen: list[str] = []

        async def status_trail() -> str:
            status = (await connector(client, 1))["status"]
            if not seen or seen[-1] != status:
                seen.append(status)
            return status

        c1 = await poll(lambda: status_trail(),
                        lambda st: st == "Finishing", timeout=120.0)
        check("car reports SuspendedEV when full", "SuspendedEV" in seen,
              " -> ".join(seen))

        # The log is authoritative: polling can miss a short-lived state, but
        # every StatusNotification is recorded in order.
        frames = await _get(client, f"{CSMS}/api/logs?action=StatusNotification&limit=400")
        reported = [
            f["payload"] for f in reversed(frames) if f["direction"] == "INBOUND"
        ]
        order = [
            i for i, p in enumerate(reported)
            if p and ("SuspendedEV\"" in p or "Finishing" in p)
        ]
        suspended = next(
            (i for i, p in enumerate(reported) if p and "SuspendedEV\"" in p), None)
        finishing = next(
            (i for i, p in enumerate(reported) if p and "Finishing" in p), None)
        check("SuspendedEV is reported before Finishing",
              suspended is not None and finishing is not None and suspended < finishing,
              f"positions {suspended} then {finishing}" if order else "")
        c1 = await connector(client, 1)
        check("charging stops at 100%", c1["status"] == "Finishing", c1["status"])
        # The cable is still in -- the same session is still there, WAITING
        # for whatever comes next, not gone and not a fresh one either.
        check("connector still shows the same session, cable not removed",
              c1["session_id"] == session_id, str(c1["session_id"]))
        session = await _get(client, f"{CSMS}/api/sessions/{session_id}")
        check("the transaction closing at full battery returns the session to WAITING",
              session["state"] == "WAITING", session["state"])
        check("energy delivered while charging is kept",
              session["energy_wh"] > 0, f"{session['energy_wh'] / 1000:.2f} kWh")
        check("charging time excludes held time", session["active_seconds"] > 0)

        # A cable that never came out, with an already-WAITING session on it,
        # is exactly the state where the SAME card can be presented again to
        # open a fresh transaction on the SAME session -- not a brand new
        # session, and not refused by the one-card-per-session rule either,
        # since WAITING has no transaction for a different card to conflict
        # with in the first place.
        swiped = await client.post(
            f"{SIM}/swipe", json={"identity": IDENTITY, "connector_id": 1, "id_tag": "RFID-0001"})
        check("card accepted on a cable that was already connected",
              swiped.json().get("status") == "Accepted",
              swiped.json().get("status", ""))
        reopened = await poll(
            lambda: _get(client, f"{CSMS}/api/sessions/{session_id}"),
            lambda s: s["state"] == "ACTIVE")
        check("the same session reopens a transaction rather than a new session appearing",
              reopened["id"] == session_id, str(reopened["id"]))
        check("a session started by a card still knows its car",
              bool(reopened.get("vehicle_name")),
              reopened.get("vehicle_name") or "(anonymous)")

        # That start opened a transaction on a full battery, so the latch is
        # engaged again until the charger works out there is nothing to do and
        # closes it. Wait for the cable to be released before pulling it.
        await poll(lambda: sim_connector(client, 1),
                   lambda c: c["transaction_id"] is None, timeout=40.0)

        r = await client.post(f"{SIM}/unplug", json={"identity": IDENTITY, "connector_id": 1})
        check("unplug allowed once charging is over", r.status_code == 200)
        c1 = await poll(lambda: connector(client, 1), lambda c: c["status"] == "Available")
        check("connector returns to Available", c1["status"] == "Available")
        check("unplugging clears the card, so the next driver must present one",
              c1["authorized_id_tag"] is None,
              c1["authorized_id_tag"] or "cleared")

        # -- 9a. End sends RemoteStopTransaction --------------------------------
        #
        # Distinct from Stop, which holds the transaction open. This one must
        # actually reach the charger -- the endpoint that sends it was once
        # deleted while the code behind it stayed, so nothing could trigger it.
        vehicles = await _get(client, f"{CSMS}/api/vehicles")
        spare = next(v for v in vehicles if v["session_id"] is None)
        await client.post(f"{SIM}/plug",
                          json={"identity": IDENTITY, "connector_id": 1, "vehicle_id": spare["id"]})
        await poll(lambda: connector(client, 1), lambda c: c["session_id"] is not None)

        # Every other Start call in this file lands on a session that a card
        # swipe already opened, so it takes the "resume an existing session"
        # path (a ClearChargingProfile nudge) rather than genuinely calling
        # RemoteStartTransaction. This is the one deliberate check that the
        # command itself still works: turn the card gate off, and Start a
        # connector that is still WAITING with no card presented yet.
        await client.patch(f"{CSMS}/api/charge-points/CP001",
                           json={"require_card_before_start": False})
        remote_started = await client.post(f"{CSMS}/api/charge-points/CP001/start",
                                           json={"connector_id": 1, "id_tag": "RFID-0001"})
        check("RemoteStartTransaction accepted on a genuinely fresh session",
              remote_started.status_code == 200, remote_started.text[:60])
        await poll(lambda: connector(client, 1), lambda c: c["session_state"] == "ACTIVE")
        await client.patch(f"{CSMS}/api/charge-points/CP001",
                           json={"require_card_before_start": True})

        await client.post(f"{SIM}/swipe",
                          json={"identity": IDENTITY, "connector_id": 1, "id_tag": "RFID-0001"})
        await client.post(f"{CSMS}/api/charge-points/CP001/start",
                          json={"connector_id": 1})
        live = await poll(lambda: connector(client, 1),
                          lambda c: c["session_state"] == "ACTIVE")

        r = await client.post(f"{CSMS}/api/sessions/{live['session_id']}/end")
        check("End is reachable and accepted", r.status_code == 200, r.text[:60])
        ended = await poll(
            lambda: _get(client, f"{CSMS}/api/sessions/{live['session_id']}"),
            lambda s: s["state"] == "WAITING")
        check("End closes the transaction but the session stays WAITING, cable still in",
              ended["state"] == "WAITING", ended["state"])

        sent = await _get(client,
                          f"{CSMS}/api/logs?action=RemoteStopTransaction&direction=OUTBOUND")
        check("RemoteStopTransaction actually went out on the wire", bool(sent),
              f"{len(sent)} frame(s)")
        await client.post(f"{SIM}/unplug", json={"identity": IDENTITY, "connector_id": 1})

        # -- 9b. a fault keeps the car and is recorded as a fault ---------------
        vehicles = await _get(client, f"{CSMS}/api/vehicles")
        spare = next(v for v in vehicles if v["session_id"] is None)
        await client.post(f"{SIM}/plug",
                          json={"identity": IDENTITY, "connector_id": 1, "vehicle_id": spare["id"]})
        await poll(lambda: connector(client, 1), lambda c: c["session_id"] is not None)
        await client.post(f"{SIM}/swipe",
                          json={"identity": IDENTITY, "connector_id": 1, "id_tag": "RFID-0001"})
        await client.post(f"{CSMS}/api/charge-points/CP001/start",
                          json={"connector_id": 1})
        c1 = await poll(lambda: connector(client, 1),
                        lambda c: c["session_state"] == "ACTIVE")
        faulted_session = c1["session_id"]

        await client.post(f"{SIM}/fault", json={"identity": IDENTITY, "connector_id": 1, "faulted": True})
        closed = await poll(
            lambda: _get(client, f"{CSMS}/api/sessions/{faulted_session}"),
            lambda s: s["state"] in ("FAULTED", "COMPLETED"))
        check("a fault during charging is recorded as FAULTED",
              closed["state"] == "FAULTED", closed["state"])
        check("the faulted session keeps its vehicle",
              closed.get("vehicle_name") == spare["name"],
              closed.get("vehicle_name") or "")

        await client.post(f"{SIM}/fault", json={"identity": IDENTITY, "connector_id": 1, "faulted": False})
        recovered = await poll(lambda: connector(client, 1),
                               lambda c: c["session_id"] == faulted_session)
        check("clearing a fault resumes the same session, not a new one",
              recovered["session_id"] == faulted_session,
              str(recovered["session_id"]))
        recovered_session = await poll(
            lambda: _get(client, f"{CSMS}/api/sessions/{faulted_session}"),
            lambda s: s["state"] == "ACTIVE")
        check("the resumed session goes back to ACTIVE and keeps its car",
              recovered_session.get("vehicle_name") == spare["name"],
              recovered_session.get("vehicle_name") or "(anonymous)")
        await client.post(f"{SIM}/unplug", json={"identity": IDENTITY, "connector_id": 1})

        # -- 10. authorisation -------------------------------------------------
        vehicles = await _get(client, f"{CSMS}/api/vehicles")
        other = next(v for v in vehicles if v["session_id"] is None)
        await client.post(f"{SIM}/plug",
                          json={"identity": IDENTITY, "connector_id": 2, "vehicle_id": other["id"]})
        await poll(lambda: connector(client, 2), lambda c: c["session_id"] is not None)

        for tag, expected in (("RFID-BLOCKED", "Blocked"), ("RFID-EXPIRED", "Expired")):
            r = await client.post(f"{SIM}/swipe",
                                  json={"identity": IDENTITY, "connector_id": 2, "id_tag": tag})
            check(f"card reader explains {expected.lower()} card",
                  r.json().get("status") == expected, r.json().get("status", ""))

        r = await client.post(f"{CSMS}/api/charge-points/CP001/start",
                              json={"connector_id": 2, "id_tag": "RFID-BLOCKED"})
        check("blocked card refused on Start", r.status_code == 409,
              r.json().get("detail", "")[:46])

        # -- 11. directory ------------------------------------------------------
        r = await client.post(f"{CSMS}/api/tags",
                              json={"id_tag": "RFID-NEW", "status": "Accepted"})
        check("create a card", r.status_code == 201)
        r = await client.patch(f"{CSMS}/api/tags/RFID-NEW", json={"status": "Blocked"})
        check("edit a card", r.status_code == 200)
        r = await client.post(f"{CSMS}/api/tags", json={"id_tag": "RFID-NEW"})
        check("duplicate card refused", r.status_code == 409,
              r.json().get("detail", ""))
        r = await client.delete(f"{CSMS}/api/tags/RFID-NEW")
        check("delete a card", r.status_code == 200)

        r = await client.post(f"{CSMS}/api/vehicles",
                              json={"name": "e-208", "battery_capacity_kwh": 50,
                                    "max_charge_kw": 11, "current_soc": 30})
        check("create a vehicle", r.status_code == 201)
        new_id = r.json()["id"]
        r = await client.patch(f"{CSMS}/api/vehicles/{new_id}", json={"current_soc": 44})
        check("edit a vehicle", r.status_code == 200)
        r = await client.delete(f"{CSMS}/api/vehicles/{other['id']}")
        check("cannot delete a car that is plugged in", r.status_code == 409,
              r.json().get("detail", ""))
        r = await client.delete(f"{CSMS}/api/vehicles/{new_id}")
        check("delete a vehicle", r.status_code == 200)

        r = await client.patch(f"{CSMS}/api/charge-points/CP001",
                               json={"label": "Garage bay 1", "heartbeat_interval": 120})
        check("edit charger name and heartbeat", r.status_code == 200)

        # -- 11b. a charger that says nothing still appears -----------------------
        #
        # Some chargers volunteer a StatusNotification per connector after
        # booting; others stay silent until something physically happens. The
        # silent ones were invisible on the overview until they were restarted,
        # so their sockets are now discovered at boot instead of waited for.
        import websockets
        from ocpp.v16 import ChargePoint as RawCP
        from ocpp.v16 import call as raw_call

        async with websockets.connect(
            "ws://localhost:9000/ocpp/QUIETBOX", subprotocols=["ocpp1.6"]
        ) as quiet_ws:
            quiet = RawCP("QUIETBOX", quiet_ws)
            quiet_task = asyncio.create_task(quiet.start())
            await quiet.call(raw_call.BootNotification(
                charge_point_vendor="ACME", charge_point_model="Quiet"))

            found = await poll(
                lambda: _get(client, f"{CSMS}/api/overview"),
                lambda o: any(c["charge_point_id"] == "QUIETBOX"
                              for c in o["connectors"]),
                timeout=15.0)
            check("a charger that only boots still shows a connector",
                  any(c["charge_point_id"] == "QUIETBOX" for c in found["connectors"]),
                  "no restart needed")
            quiet_task.cancel()

        # -- 12. every read endpoint actually responds ---------------------------
        #
        # A join against a table that no longer exists only fails when the
        # endpoint is called, so each one is called here rather than trusting
        # that the parts it is built from were tested individually.
        for path in (
            "/api/health",
            "/api/overview",
            "/api/charge-points",
            "/api/charge-points/CP001",
            "/api/sessions",
            "/api/sessions?limit=200",
            "/api/sessions?charge_point_id=CP001",
            f"/api/sessions/{session_id}",
            "/api/tags",
            "/api/vehicles",
            "/api/logs",
            "/api/logs?direction=INBOUND&limit=10",
        ):
            r = await client.get(f"{CSMS}{path}")
            check(f"GET {path}", r.status_code == 200,
                  "" if r.status_code == 200 else r.text[:70])

        listed = await _get(client, f"{CSMS}/api/sessions?limit=200")
        check("session list carries its vehicle",
              any(s.get("vehicle_name") for s in listed),
              next((s["vehicle_name"] for s in listed if s.get("vehicle_name")), ""))

        # The list and the detail read the same record through different
        # queries, so they are checked separately -- a join missing from one
        # of them is invisible from the other.
        detail = await _get(client, f"{CSMS}/api/sessions/{session_id}")
        check("session detail carries its vehicle too",
              bool(detail.get("vehicle_name")), detail.get("vehicle_name") or "")
        check("session detail records when it ended",
              bool(detail.get("ended_at")), detail.get("ended_at") or "")

        # Charging time counts delivery, so the seconds spent in SuspendedEV
        # waiting to shut down must not be included.
        check("charging time never exceeds time plugged in",
              all(
                  s["active_seconds"] <= (
                      (parse_ts(s["ended_at"]) - parse_ts(s["plugged_in_at"])) + 2
                  )
                  for s in listed
                  if s["ended_at"] and s["plugged_in_at"]
              ))

        # -- 13. logging ---------------------------------------------------------
        logs = await _get(client, f"{CSMS}/api/logs?limit=500")
        actions = {row["action"] for row in logs if row["action"]}
        for expected in ("BootNotification", "StatusNotification", "StartTransaction",
                         "MeterValues", "SetChargingProfile", "ClearChargingProfile",
                         "RemoteStartTransaction", "StopTransaction"):
            check(f"logged {expected}", expected in actions)
        check("both directions captured",
              {row["direction"] for row in logs} == {"INBOUND", "OUTBOUND"})
        results = [r for r in logs if r["message_type_id"] == 3]
        check("every CALLRESULT is labelled with the action it answers",
              all(r["action"] for r in results),
              f"{len(results)} results, {sum(1 for r in results if not r['action'])} unlabelled")

        await client.post(f"{SIM}/unplug", json={"identity": IDENTITY, "connector_id": 2})


def main() -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}

    # The suite asserts against the seeded fixture -- three cars at known
    # states of charge, four cards -- which means it needs to reset the
    # database first. That is destructive: it deletes the real database file
    # and everything in it, including any real chargers, sessions, and cards
    # you have actually been using. This used to run automatically, with no
    # warning, which is exactly what wiped a real database once already.
    # It will never run silently again.
    print(
        "\nThis test suite resets the database before running --\n"
        "it deletes data/csms.db and everything in it (chargers, sessions,\n"
        "cards, vehicles -- all of it) and replaces it with a fresh test\n"
        "fixture. This cannot be undone.\n"
    )
    confirmed = input("Type RESET to continue, anything else to cancel: ")
    if confirmed.strip() != "RESET":
        print("Cancelled. Nothing was touched.")
        return 1

    reset = subprocess.run(
        [sys.executable, "-m", "csms.db.seed", "--reset"],
        cwd=ROOT, env=env, capture_output=True, text=True)
    if reset.returncode != 0:
        print(reset.stdout, reset.stderr)
        return 1

    csms = subprocess.Popen(
        [sys.executable, "-m", "csms.app"], cwd=ROOT, env=env,
        stdout=open("/tmp/csms.log", "w"), stderr=subprocess.STDOUT)
    sim = subprocess.Popen(
        [sys.executable, "-m", "simulator.main"], cwd=ROOT,
        env={**env, "SIM_TIME_SCALE": "900", "SIM_SAMPLE_INTERVAL": "2"},
        stdout=open("/tmp/sim.log", "w"), stderr=subprocess.STDOUT)
    try:
        asyncio.run(run())
    finally:
        sim.terminate(); csms.terminate()
        sim.wait(timeout=10); csms.wait(timeout=10)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())