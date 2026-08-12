"""Enumerations mirroring the CHECK constraints in schema.sql.

These exist so application code never hard-codes a string literal that the
database would reject at insert time. Every member's value is byte-identical
to a value listed in the corresponding CHECK constraint.

Values drawn from the OCPP 1.6 specification keep the exact casing used on the
wire, so no translation is needed when serialising. Values that are ours alone
are UPPER_SNAKE, which makes the distinction obvious at a glance.
"""

from __future__ import annotations

import enum


class StrEnum(str, enum.Enum):
    """str-valued enum: ``ConnectorStatus.CHARGING == "Charging"`` is True,
    and instances bind directly as SQL parameters."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ---------------------------------------------------------------------------
# OCPP 1.6 protocol enums (wire values -- do not rename)
# ---------------------------------------------------------------------------


class RegistrationStatus(StrEnum):
    ACCEPTED = "Accepted"
    PENDING = "Pending"
    REJECTED = "Rejected"


class ConnectorStatus(StrEnum):
    AVAILABLE = "Available"
    PREPARING = "Preparing"
    CHARGING = "Charging"
    SUSPENDED_EVSE = "SuspendedEVSE"
    SUSPENDED_EV = "SuspendedEV"
    FINISHING = "Finishing"
    RESERVED = "Reserved"
    UNAVAILABLE = "Unavailable"
    FAULTED = "Faulted"


class ChargePointErrorCode(StrEnum):
    CONNECTOR_LOCK_FAILURE = "ConnectorLockFailure"
    EV_COMMUNICATION_ERROR = "EVCommunicationError"
    GROUND_FAILURE = "GroundFailure"
    HIGH_TEMPERATURE = "HighTemperature"
    INTERNAL_ERROR = "InternalError"
    LOCAL_LIST_CONFLICT = "LocalListConflict"
    NO_ERROR = "NoError"
    OTHER_ERROR = "OtherError"
    OVER_CURRENT_FAILURE = "OverCurrentFailure"
    OVER_VOLTAGE = "OverVoltage"
    POWER_METER_FAILURE = "PowerMeterFailure"
    POWER_SWITCH_FAILURE = "PowerSwitchFailure"
    READER_FAILURE = "ReaderFailure"
    RESET_FAILURE = "ResetFailure"
    UNDER_VOLTAGE = "UnderVoltage"
    WEAK_SIGNAL = "WeakSignal"


class AuthorizationStatus(StrEnum):
    ACCEPTED = "Accepted"
    BLOCKED = "Blocked"
    EXPIRED = "Expired"
    INVALID = "Invalid"
    CONCURRENT_TX = "ConcurrentTx"


class StopReason(StrEnum):
    EMERGENCY_STOP = "EmergencyStop"
    EV_DISCONNECTED = "EVDisconnected"
    HARD_RESET = "HardReset"
    LOCAL = "Local"
    OTHER = "Other"
    POWER_LOSS = "PowerLoss"
    REBOOT = "Reboot"
    REMOTE = "Remote"
    SOFT_RESET = "SoftReset"
    UNLOCK_COMMAND = "UnlockCommand"
    DE_AUTHORIZED = "DeAuthorized"


class Measurand(StrEnum):
    ENERGY_ACTIVE_IMPORT_REGISTER = "Energy.Active.Import.Register"
    POWER_ACTIVE_IMPORT = "Power.Active.Import"
    CURRENT_IMPORT = "Current.Import"
    CURRENT_OFFERED = "Current.Offered"
    VOLTAGE = "Voltage"
    SOC = "SoC"
    TEMPERATURE = "Temperature"


class ReadingContext(StrEnum):
    INTERRUPTION_BEGIN = "Interruption.Begin"
    INTERRUPTION_END = "Interruption.End"
    OTHER = "Other"
    SAMPLE_CLOCK = "Sample.Clock"
    SAMPLE_PERIODIC = "Sample.Periodic"
    TRANSACTION_BEGIN = "Transaction.Begin"
    TRANSACTION_END = "Transaction.End"
    TRIGGER = "Trigger"


class ChargingProfilePurpose(StrEnum):
    CHARGE_POINT_MAX_PROFILE = "ChargePointMaxProfile"
    TX_DEFAULT_PROFILE = "TxDefaultProfile"
    TX_PROFILE = "TxProfile"


class MessageTypeId(enum.IntEnum):
    """OCPP-J RPC framing message type numbers."""

    CALL = 2
    CALLRESULT = 3
    CALLERROR = 4


class ErrorCode(StrEnum):
    """CALLERROR codes, from Table 7 of the OCPP-J 1.6 specification."""

    NOT_IMPLEMENTED = "NotImplemented"
    NOT_SUPPORTED = "NotSupported"
    INTERNAL_ERROR = "InternalError"
    PROTOCOL_ERROR = "ProtocolError"
    SECURITY_ERROR = "SecurityError"
    FORMATION_VIOLATION = "FormationViolation"
    PROPERTY_CONSTRAINT_VIOLATION = "PropertyConstraintViolation"
    OCCURENCE_CONSTRAINT_VIOLATION = "OccurenceConstraintViolation"
    TYPE_CONSTRAINT_VIOLATION = "TypeConstraintViolation"
    GENERIC_ERROR = "GenericError"


# ---------------------------------------------------------------------------
# CSMS domain enums (ours)
# ---------------------------------------------------------------------------


class SessionState(StrEnum):
    """See the charging_sessions comment in schema.sql."""

    WAITING = "WAITING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAULTED = "FAULTED"


OPEN_SESSION_STATES = (
    SessionState.WAITING,
    SessionState.ACTIVE,
    SessionState.PAUSED,
    SessionState.FAULTED,
)


class TransactionState(StrEnum):
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"


class MessageDirection(StrEnum):
    """Direction relative to the CSMS."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"