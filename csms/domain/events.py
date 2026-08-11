"""Event topic names.

Kept as constants in one place so a typo in a topic string is a NameError at
import time rather than a subscriber that silently never fires.
"""

from __future__ import annotations

# Charge point connectivity
CP_CONNECTED = "chargepoint.connected"
CP_DISCONNECTED = "chargepoint.disconnected"
CP_BOOTED = "chargepoint.booted"
CP_HEARTBEAT = "chargepoint.heartbeat"

# Connectors
CONNECTOR_STATUS = "connector.status"

# A card the database has never seen was presented at a reader. Carries the
# number so the dashboard can offer to add it, which is the only moment the
# operator knows a physical card exists.
UNKNOWN_CARD = "card.unknown"

# Sessions
SESSION_CREATED = "session.created"      # cable plugged in, WAITING
SESSION_STARTED = "session.started"      # transaction opened, ACTIVE
SESSION_PAUSED = "session.paused"        # 0 W profile applied, PAUSED
SESSION_RESUMED = "session.resumed"      # profile cleared, ACTIVE
SESSION_ENDED = "session.ended"          # transaction closed, COMPLETED
SESSION_UPDATED = "session.updated"      # energy / SoC tick
SESSION_FAULTED = "session.faulted"      # charger reported a fault, clock paused

# Metering
METER_VALUES = "meter.values"

# Protocol log
MESSAGE_LOGGED = "message.logged"

# Scheduling
SCHEDULE_CREATED = "schedule.created"
SCHEDULE_FIRED = "schedule.fired"
SCHEDULE_FAILED = "schedule.failed"