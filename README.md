# OCPP 1.6J Central System

A CSMS (Central System) that speaks OCPP 1.6J over WebSocket, a hardware
simulator that speaks it back, and a SQLite database underneath. The dashboard
frontend is the next piece to build; the REST and WebSocket APIs it will
consume are already in place and tested.

---

## Quick start

Three terminals.

```bash
# 1. backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m csms.db.seed --reset     # create the schema and load the fixture
python -m csms.app                 # CSMS on :9000

# 2. simulated hardware
python -m simulator.main           # charger on :9100

# 3. dashboard
cd frontend && npm install && npm run dev   # http://localhost:5173
```

The dev server proxies `/api` and `/ws` to the CSMS and `/sim` to the
simulator, so the browser sees a single origin and the WebSocket upgrade works
without any CORS handling.

Then drive the simulated hardware:

```bash
curl -X POST localhost:9100/plug  -H 'Content-Type: application/json' \
     -d '{"identity":"CP001","connector_id":1,"vehicle_id":1}'

curl -X POST localhost:9000/api/charge-points/CP001/start -H 'Content-Type: application/json' \
     -d '{"connector_id":1,"id_tag":"RFID-0001"}'

curl -X POST localhost:9000/api/sessions/1/stop      # pause: holds at 0 W, transaction stays open
curl -X POST localhost:9000/api/charge-points/CP001/start -H 'Content-Type: application/json' \
     -d '{"connector_id":1}'                          # resume: same endpoint as start
curl -X POST localhost:9000/api/sessions/1/end        # end: closes the transaction for good
```

Clear charging history without losing your cards, cars and chargers:

```bash
python -m csms.db.wipe             # sessions, transactions, meter readings
python -m csms.db.wipe --logs      # those, plus the OCPP frame log
python -m csms.db.wipe --all       # all of it, and reset every car to 20%
python -m csms.db.wipe --logs-only # just the frame log
```

Deleting sessions by hand in the `sqlite3` CLI does not work properly: SQLite
disables foreign keys by default there, so the `ON DELETE CASCADE` rules never
fire and you are left with orphaned transactions and meter readings. This tool
connects with them enabled.

To start completely fresh instead, `python -m csms.db.seed --reset` deletes the
database file and rebuilds the fixture.

Verify the whole thing end to end:

```bash
python tests/e2e.py
```

It boots both services, runs 40 checks over a real OCPP WebSocket, and tears
them down.

---

## How Start / Stop actually works

OCPP 1.6 has no native pause. A transaction is atomic: once `StopTransaction`
records a `meterStop`, it is over. So "stop and later carry on from where it
left off" is built on a different primitive.

**Pause installs a `TxProfile` with a limit of 0 W.** The charger reports
`SuspendedEVSE`, energy delivery halts, and **the transaction stays open**.
Its cumulative energy register keeps its value rather than resetting.
`ClearChargingProfile` lifts the limit and charging continues from exactly
that reading.

The payoff: resuming needs no offset arithmetic anywhere in the codebase. The
register simply carries on.

```
   [plug in]                                        [unplug / End]
       |                                                  ^
       v                                                  |
   +---------+   start   +--------+   pause   +-----------+
   | WAITING |---------->| ACTIVE |<--------->|  PAUSED   |
   +---------+           +--------+  resume   +-----------+
   Preparing             Charging             SuspendedEVSE
   no transaction        txn open             txn open, 0 W
```

| Action | OCPP message | Result |
|---|---|---|
| plug in | (charger sends `StatusNotification: Preparing`) | session created, `WAITING`, car bound |
| **Start** | `RemoteStartTransaction` → `StartTransaction` | `ACTIVE`, cable latches |
| **Stop** | `SetChargingProfile` limit 0 | `PAUSED`, transaction open, cable releases |
| **Start** again | `ClearChargingProfile` | `ACTIVE`, meter continues |
| unplug | `StopTransaction` | `COMPLETED` |
| battery full | `StopTransaction` | `COMPLETED`, connector waits at `Finishing` |

Two controls, not three. Unplugging is what ends a session, and the connector
latch makes that safe: the cable is held captive while power flows and
released when you press Stop, so an unplug always means charging was
deliberately halted first. That mirrors real hardware, where you physically
cannot pull the plug mid-charge.

While paused the charger keeps sending `MeterValues` with a flat energy
register, so the kWh graph shows a visible plateau. That plateau is the proof
the pause took effect.

**Fallback.** If a charge point rejects `SetChargingProfile`, set
`supports_charging_profiles = 0` on its row and pause degrades to ending the
transaction. `charging_sessions` is already one-to-many over `transactions`,
so the session's energy total survives across both. No migration needed.

**Vehicle identity.** OCPP 1.6 has no concept of a vehicle, so the simulator
reports which car is connected over a small HTTP call to the CSMS when a cable
goes in. That binds the car to the session immediately, before any card is
presented. It is a lab convenience: real hardware cannot do this, and with a
physical charger a session simply has no vehicle attached. The vehicle list
itself lives only in the CSMS — the simulator reads it — so a car added in the
dashboard is pluggable at once and cannot appear to be in two sockets.

---

## Connector states, and which one means what

OCPP 1.6 defines nine connector states. Two of them are easy to confuse, and
the difference decides how a stop is reported.

| State | Meaning |
|---|---|
| `Available` | Nothing plugged in |
| `Preparing` | Cable connected, no transaction yet |
| `Charging` | Delivering energy |
| `SuspendedEV` | Connected, **the charger is offering energy but the car is not taking it** |
| `SuspendedEVSE` | Connected, **the charger is not offering energy to the car** |
| `Finishing` | Transaction over, cable still connected |
| `Reserved` | Held for a booking |
| `Unavailable` | Out of service, or we have lost contact |
| `Faulted` | The charger is reporting a fault |

The distinction is simply **who stopped**: `SuspendedEV` is the vehicle's
decision, `SuspendedEVSE` is the charger's. Both keep the transaction open and
both freeze the meter register, so from the billing record's point of view
they look the same — but they mean opposite things about whose choice it was.

**Pressing Stop reports `SuspendedEVSE`.** Stop installs a charging profile
limited to 0 W, so the charger is no longer offering energy. The specification
names that case directly: `SuspendedEVSE` covers a connector held by "a smart
charging restriction", which is exactly what a 0 W `TxProfile` is. Reporting
`SuspendedEV` there would claim the car had declined the energy, when in fact
it was never offered any.

**A full battery reports `SuspendedEV`.** The charger is still willing to
supply; the vehicle has stopped accepting. After a short dwell — real chargers
wait, because the car may resume — the transaction is closed and the connector
moves to `Finishing` until the cable is removed.

Charging time counts only time spent actually delivering, so it stops in both
suspended states and resumes if the car starts drawing again.

---

## Architecture

One process serves both the OCPP endpoint and the dashboard API. Modularity is
enforced at the package boundary rather than the process boundary, and the
seams are drawn so that splitting them later is a configuration change.

```
+-----------------------------------------------+
|  csms  (single process, FastAPI + ASGI)       |
|                                               |
|  /ocpp/{id}  --> transport -> rpc -> handlers |
|  /api/*      --> routes                       |
|  /ws/dashboard --> live event stream          |
|                        |                      |
|            +-----------v----------+           |
|            |   domain services    |           |
|            | sessions, auth,      |           |
|            | metering, scheduler  |           |
|            +-----------+----------+           |
|                 repository -> SQLite (WAL)    |
|            in-process asyncio EventBus        |
+-----------------------------------------------+
        ^                              ^
   ws:// | OCPP 1.6J              HTTP |
+-------+--------+            +--------+-------+
|   simulator    |            |   frontend     |
| (own process)  |            | React + Vite   |
+----------------+            +----------------+
```

Layering is strictly downward:

```
transport / routes  ->  domain services  ->  repository  ->  Database
```

Two rules keep it honest:

- **Nothing in `domain/` imports from `ocpp_/`.** The domain reaches chargers
  through the `ChargePointCommands` protocol in `domain/ports.py`, which the
  registry implements. The state machine is testable with no WebSocket.
- **The repository never opens a transaction.** Every function takes an open
  connection, so callers decide the boundary and multi-table changes stay
  atomic.

Swapping the in-process bus for Redis is one new class implementing
`EventBus`. Swapping SQLite for PostgreSQL is a connection string and a
migration — only portable SQL is used, apart from the pragma hook.

### Layout

```
csms/
├── app.py                  ASGI assembly, lifespan, wiring (the only place)
├── config.py               every tunable; override with CSMS_* env vars
├── bus.py                  EventBus protocol + in-process implementation
├── db/
│   ├── schema.sql          single source of truth for the schema
│   ├── database.py         connection pool, transactions, timestamp helpers
│   ├── enums.py            mirrors the CHECK constraints in schema.sql
│   ├── seed.py             create + load the fixture
│   └── wipe.py             clear history without losing cards/cars/chargers
├── ocpp_/
│   ├── transport.py        /ocpp/{id}, subprotocol negotiation, 404 handling
│   ├── connection.py       handlers + frame logging (both directions)
│   └── registry.py         live connections + outbound CALLs
├── domain/
│   ├── ports.py            ChargePointCommands protocol
│   ├── sessions.py         the state machine
│   ├── authorization.py    idTag rules
│   ├── stress_test.py      server-orchestrated load test runner
│   └── events.py           bus topic names
├── repository/             plain SQL, one module per aggregate
└── api/
    ├── routes.py           REST for the dashboard
    ├── diagnostics.py      receives uploaded diagnostics files
    └── ws.py               /ws/dashboard live feed

simulator/
├── charge_point.py         a real OCPP client, not a mock
├── vehicle.py               battery + charge curve
└── main.py                  runner + HTTP control API

tests/e2e.py                73 checks over a live WebSocket
```

---

## Database

SQLite in WAL mode. `csms/db/schema.sql` is plain DDL you can read, diff, and
run with the `sqlite3` CLI. Fourteen tables and two views.

```
id_tags        vehicles
    │              │
    └──────┬───────┘
           │  (a session records which card authorised it
           │   and which car was connected -- the only place
           │   both facts are true together)
           v
charge_points ──< connectors ──< charging_sessions ──< transactions ──< meter_values
      │                                   │
      ├──< configuration_keys             ├──< charging_profiles
      ├──< message_log                    └──< faults
      └──< connection_events

test_definitions   (standalone -- a saved stress-test's name and step list,
                     with no foreign key into anything above; a test is not
                     about a specific charger, it is a reusable script that
                     creates its own throwaway ones when run)
```

Conventions: timestamps are ISO-8601 UTC text (lexicographic order equals
chronological order, so `BETWEEN` and `ORDER BY` work directly); booleans are
`INTEGER` 0/1; enums are `TEXT` with a `CHECK` constraint using the exact OCPP
wire casing; energy is `INTEGER` Wh exactly as OCPP reports it.

Constraints doing real work:

- **`ux_sessions_one_open_per_connector`** — a partial unique index over
  `state IN ('WAITING','ACTIVE','PAUSED','FAULTED')`. At most one open session
  per connector, enforced by the storage engine rather than by application
  locking. `FAULTED` is included because a fault is not terminal -- the
  transaction very likely stays open on the charger's side.
- **`CHECK ((state = 'STOPPED') = (stopped_at IS NOT NULL))`** — a transaction
  cannot be marked stopped without a stop time, or carry one without being
  stopped.
- **`meter_stop_wh >= meter_start_wh`** and `meter_last_wh >= meter_start_wh` —
  the register cannot run backwards.

`ChargingSession` is what the dashboard shows. `Transaction` is the OCPP-level
record underneath it.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/overview` | everything the landing page needs, one round trip |
| GET | `/api/charge-points` | list, with connectors and live status |
| POST | `/api/charge-points` | provision a charger before it has ever connected |
| GET | `/api/charge-points/{id}` | detail + configuration keys |
| PATCH | `/api/charge-points/{id}` | the fields the dashboard owns: name, heartbeat, registration, etc. |
| DELETE | `/api/charge-points/{id}` | remove; history is relabeled, not destroyed; refused while live or open |
| GET | `/api/charge-points/{id}/uptime` | current streak + reliability percentages |
| GET | `/api/charge-points/{id}/uptime/timeline` | connect/disconnect segments for a window |
| POST | `/api/charge-points/{id}/start` | begin charging on a connector, or resume a held one |
| POST | `/api/charge-points/{id}/configuration` | `ChangeConfiguration` |
| POST | `/api/charge-points/{id}/get-configuration` | `GetConfiguration` |
| POST | `/api/charge-points/{id}/reset` | soft or hard reset |
| POST | `/api/charge-points/{id}/trigger` | `TriggerMessage` |
| POST | `/api/charge-points/{id}/clear-cache` | `ClearCache` |
| POST | `/api/charge-points/{id}/unlock` | `UnlockConnector` |
| POST | `/api/charge-points/{id}/availability` | `ChangeAvailability` |
| POST | `/api/charge-points/{id}/diagnostics` | `GetDiagnostics` |
| POST | `/api/charge-points/{id}/get-local-list-version` | `GetLocalListVersion` |
| POST | `/api/charge-points/{id}/send-local-list` | `SendLocalList` |
| POST | `/api/charge-points/{id}/reserve-now` | `ReserveNow` |
| POST | `/api/charge-points/{id}/cancel-reservation` | `CancelReservation` |
| POST | `/api/charge-points/{id}/composite-schedule` | `GetCompositeSchedule` |
| POST | `/api/charge-points/{id}/update-firmware` | `UpdateFirmware` |
| POST | `/api/charge-points/{id}/data-transfer` | `DataTransfer` |
| POST | `/api/charge-points/{id}/vehicle` | side channel: which car is on a connector (simulator only) |
| GET | `/api/sessions` | recent sessions |
| GET | `/api/sessions/{id}` | detail + chart series + OCPP message timeline |
| POST | `/api/sessions/{id}/stop` | hold at 0 W; transaction stays open |
| POST | `/api/sessions/{id}/end` | close the transaction for good |
| GET/POST/PATCH/DELETE | `/api/tags` | RFID cards |
| GET/POST/PATCH/DELETE | `/api/vehicles` | vehicles |
| GET | `/api/logs` | every OCPP frame, filterable |
| GET | `/api/faults` | fault occurrences, filterable by charger or session |
| GET | `/api/health` | liveness + schema version + currently-connected chargers |
| POST | `/api/stress-tests` | start a server-orchestrated load test (see below) |
| GET | `/api/stress-tests` | every run this process has started |
| GET | `/api/stress-tests/{id}` | one run's live progress |
| POST | `/api/stress-tests/{id}/cancel` | stop a run after its current step |
| POST | `/api/test-definitions` | save a reusable, named test |
| GET | `/api/test-definitions` | every saved test |
| PATCH | `/api/test-definitions/{id}` | rename and/or edit a saved test's steps |
| DELETE | `/api/test-definitions/{id}` | remove a saved test |
| POST | `/api/test-definitions/{id}/run` | run a saved test, same as building it fresh |
| WS | `/ws/dashboard?topics=session.*` | live event feed |
| WS | `/ocpp/{chargePointId}` | the charger endpoint |

Interactive docs at `http://localhost:9000/docs`.

Domain failures map to real status codes: `409` for an illegal transition
(`SessionError`), `502` when a charger rejects or times out a CALL
(`CommandError`). Neither is a 500.

---

## Charging profiles

Every vehicle follows a real, sourced DC fast-charging curve rather than one
generic taper. `simulator/vehicle.py` defines five, each built from actual
published charging test data, chosen to be genuinely different shapes rather
than the same curve scaled by a number:

| Profile | Shape | Source |
|---|---|---|
| `generic` | flat to 80%, straight taper to 15% by 100% | the original default |
| `renault` | peaks around 10% SOC, steady taper from ~25% | Renault Megane E-Tech test data (planevcharge.com, evcourse.com) |
| `tesla` | very high peak, sustained through a narrow low-SOC window, sharp drop above 80% | Tesla Motors Club V3 Supercharging threads, ChargeCalcs |
| `hyundai_kia` | flat plateau from ~5% to ~50%, then steps down with a real cliff at 80% | ChargeMath / ChargeCalcs E-GMP 800V platform data |
| `vw_id` | peaks to ~30%, then a gentle, wide-range fade through 30-70% | InsideEVs ID.4 82 kWh DC fast charging analysis |
| `nissan_leaf` | lower peak reached gradually, earlier and more gradual taper — reflects the Leaf's lack of active thermal management | InsideEVs Nissan Leaf DC charging curve test, My Nissan Leaf Forum |

Set per vehicle from the Directory page or the Simulator's own vehicle
picker. `csms/db/schema.sql`'s `vehicles.charge_profile` column constrains
it to exactly these five values plus `generic`.

## Stress testing

`/tests` builds a short, ordered sequence of steps and runs it against real,
genuinely simulated hardware — actual WebSocket connections speaking real
OCPP, not a synthetic shortcut. Every physical action is its own step, not a
bundled cascade, so a sequence like "create, wait 10s, plug in, wait 10s,
present card, offer power" is built one real action at a time, in whatever
order actually makes sense to test:

| Step | What it does |
|---|---|
| Create chargers | spins up N chargers under a run-specific identity prefix (`{run_id}-0000`, `{run_id}-0001`, …) |
| Plug in | plugs a fresh test vehicle into every connector this run has created |
| Present card | swipes a fresh test card at every connector |
| Offer power / Withdraw power | the EVSE "C switch" — the same toggle the Simulator page's own button uses |
| Unplug | removes the cable |
| Remote start / Remote stop | goes through the real CSMS API on `:9000`, the exact same `RemoteStartTransaction` / End path the dashboard's own Start/End buttons trigger — not the simulator's control API |
| Inject fault / Clear fault | applies to every connector on every charger the run has created |
| Wait | pauses for a fixed number of seconds |
| Delete | see below |

Every step targets "every charger this run has created so far." **A step
that would be genuinely refused is refused, honestly, not silently
skipped or faked** — presenting a card before plugging in, or remote-starting
before a card was presented, gets the same real rejection a person doing it
by hand from the Simulator or dashboard would get. The step's result records
how many connectors succeeded versus how many were rejected, and the run
keeps going rather than stopping at the first rejection, so a badly-sequenced
test is visible in the results rather than silently wrong.

Execution is server-side: the browser sends the whole step sequence once and
polls `GET /api/stress-tests/{id}` for progress, rather than looping through
however many actions itself. Steps run strictly one after another; there is
no concurrent-step mode.

**Delete is explicit, not automatic.** A test run's chargers stay running
after the run finishes unless a **Delete** step was included. When it runs,
it is a genuine, complete removal — every charger, card, and vehicle the run
created, cascaded away with no tombstone — deliberately not the
history-preserving delete a real charger gets, since a stress test's data
was never meant to be historical. A run never touches anything it did not
create itself.

**Saved, reusable tests.** A sequence can be saved as a named definition
(`POST /api/test-definitions`), which persists on the backend across
restarts, separate from run history. A saved test can be run again with one
click, or opened and edited in place — its steps reuse the exact same
step-builder rows as building a new test, just pre-filled. Run history below
collapses once a run finishes (stays open while a run is still going), and
shows exactly when it ran and, per step, real counts — how many connectors
were plugged in, how many cards were accepted, how many were rejected.

## Blocking specific commands

Each simulated charger has its own **Blocking** menu (on the Simulator page)
with one switch per inbound OCPP command it can genuinely refuse:
`RemoteStartTransaction`, `RemoteStopTransaction`, `SetChargingProfile`,
`ClearChargingProfile`, `ChangeConfiguration`, `Reset`, `UnlockConnector`,
`TriggerMessage`. Switching one on makes the charger reply with a real,
honest OCPP rejection status every time the CSMS sends that command — the
same genuine "no" the handler already sends for its own real reasons
elsewhere (a fault, an unknown profile), just triggered deliberately rather
than by a real condition. This is for testing how the CSMS behaves when a
charger simply will not honour a given command, which real hardware in the
field can do (an unsupported feature, a firmware bug, a deliberate policy).
Plain reads with no real "no" in the spec (`GetConfiguration`) are
deliberately not offered here.



Implemented from the OCPP-J 1.6 specification:

- Connection URL is the endpoint plus `/` plus the percent-encoded charge
  point identity.
- `Sec-WebSocket-Protocol` must contain `ocpp1.6`. If no offered subprotocol is
  acceptable, the handshake **completes without the header** and the socket is
  then closed immediately — as the spec requires, rather than refusing outright.
- Unknown charge point identity gets HTTP 404 when
  `reject_unknown_charge_points` is on; otherwise the unit is auto-provisioned
  so it appears in the dashboard.
- One CALL outstanding at a time, with a configurable response timeout.
- Message IDs capped at 36 characters.
- Reconnecting does not require a fresh `BootNotification`.
- A dropped WebSocket leaves open sessions open — the charger keeps charging
  autonomously and reports on reconnect. Connector states go `Unavailable`,
  because we genuinely no longer know them.
- On startup, before accepting any connection, any charger the database still
  calls online from a previous run has that closed out immediately — a killed
  process never gets to run its own disconnect hook, so without this the
  uptime history would show one unbroken "connected" streak spanning however
  long the process was actually down.
- Heartbeat responses carry `currentTime`; WebSocket Ping/Pong cannot, which is
  exactly why the message still matters.
- `Faulted` is not terminal. A charger frequently keeps the same transaction
  running straight through a fault window — confirmed against real hardware,
  not assumed — so the session's own state becomes `FAULTED`, the clock stops
  as a side effect of that state change, and every occurrence is separately
  recorded in a `faults` table independent of the session lifecycle. Recovery
  resumes the same session once the charger reports a real status again,
  rather than the session being closed and a new one opened.

**Not implemented yet:** TLS, HTTP Basic authentication, and the
`AuthorizationKey` onboarding flow. Deferred deliberately — plain `ws://` for
local development. The schema has no auth columns, so adding them is additive.

---

## Dashboard

Vite + React + TypeScript + Tailwind + TanStack Query + Recharts. The UI
primitives are hand-written rather than pulled from a component library, so the
panel vocabulary stays consistent and there is nothing to override.

### Design

The subject is a control room, not a SaaS product, so the visual language comes
from switchboard and SCADA mimic panels rather than from dashboard templates.

- **Palette.** Deep instrument slate (`#12161F`) with a *semantic* signal set
  rather than a single accent: live teal, hold amber, idle steel, fault red,
  waiting blue. On a real panel colour means state, so it is never used as
  decoration — `connectorSignal()` in `src/lib/status.ts` is the one place the
  mapping lives.
- **Type.** Space Grotesk for interface text, JetBrains Mono for every number.
  Instrumentation needs tabular figures that line up column to column.
- **Signature element.** The connector meter tape: a live trace of the energy
  register that visibly flatlines under a hatched HOLD band while paused. The
  plateau is the product's core idea made visible.

Responsive from mobile up, keyboard focus rings throughout, and
`prefers-reduced-motion` respected.

### Pages

| Route | What it is for |
|---|---|
| `/` | every charger grouped into a collapsible card, an online/offline chart you can click to filter, search, favourites pinned to the top, and named containers you can group chargers into (search matches a container's name or any charger inside it); Expand all / Collapse all act on everything at once; opening a card reveals its connectors with their meter tapes and Start / Stop / End controls |
| `/chargers` | hardware inventory — the same collapsible / searchable / favourited / container grouping as `/`, one card per charger with its connectors, firmware, and uptime |
| `/chargers/:id` | connectors, configuration keys, scheduled commands, reset |
| `/sessions` | session history |
| `/sessions/:id` | energy / power / SoC charts, transactions, the OCPP frames |
| `/directory` | RFID cards and vehicles, both editable, including which real-world DC charging curve a car follows |
| `/logs` | every frame, filterable and expandable to the full payload |
| `/simulator` | plug, unplug, present a card, inject a fault, block specific inbound commands per charger; the same collapsible / searchable / favourited / container grouping as `/` |
| `/tests` | build, save, and run server-orchestrated load tests against real, genuinely simulated chargers |

Containers are a local-only, per-browser way of grouping chargers under a
custom name — a charger belongs to at most one container at a time. Same for
favourites. Neither is shared across devices or persisted on the backend;
saved test definitions (`/tests`) are the one thing in this list that is
backend-persisted, since those are meant to survive a restart and be shared.

### Data flow

The WebSocket feed is the source of *"something changed"*; React Query stays
the source of truth for what the data is. `useLiveFeed` maps each event topic
to the specific queries it could have invalidated. A dropped socket therefore
degrades to the polling intervals rather than to a stale screen that looks
authoritative.

The simulator page is deliberately separate from the operator pages: everything
on it is a physical act on the charger (a cable connected, a card presented, a
fault occurring), not a command sent to it. Blurring those two is how a
dashboard ends up lying about what it can actually do.

```bash
cd frontend
npm run dev      # dev server with proxies
npm run build    # type-check and produce dist/
npm run lint     # type-check only
```