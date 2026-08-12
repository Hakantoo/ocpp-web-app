"""Live connection registry and the outbound command surface.

Implements domain.ports.ChargePointCommands, so the domain layer can tell a
charger to do something without importing anything from the OCPP layer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ocpp.v16 import call

from ..domain.ports import CommandError
from .connection import ChargePointConnection

log = logging.getLogger(__name__)


class ChargePointRegistry:
    """Maps charge point identity -> live connection."""

    def __init__(self) -> None:
        self._connections: dict[str, ChargePointConnection] = {}

    # -- membership --------------------------------------------------------

    def add(self, connection: ChargePointConnection) -> ChargePointConnection | None:
        """Register a connection, returning any previous one it displaced.

        A charger that reconnects before we noticed the old socket died would
        otherwise leave a ghost entry that swallows every command.
        """
        previous = self._connections.get(connection.id)
        self._connections[connection.id] = connection
        return previous

    def remove(self, identity: str, connection: ChargePointConnection | None = None) -> None:
        # Only remove if it is still the same object: a reconnect may already
        # have replaced it, and we must not evict the new socket.
        current = self._connections.get(identity)
        if current is not None and (connection is None or current is connection):
            del self._connections[identity]

    def get(self, identity: str) -> ChargePointConnection | None:
        return self._connections.get(identity)

    def is_connected(self, identity: str) -> bool:
        return identity in self._connections

    @property
    def connected_ids(self) -> list[str]:
        return sorted(self._connections)

    # -- outbound CALLs ----------------------------------------------------

    async def _call(self, identity: str, payload: Any) -> dict[str, Any]:
        cp = self._connections.get(identity)
        if cp is None:
            raise CommandError(f"Charge point {identity} is not connected")
        action = type(payload).__name__
        try:
            response = await cp.call(payload)
        except asyncio.TimeoutError as exc:
            raise CommandError(
                f"{action} to {identity} timed out with no CALLRESULT"
            ) from exc
        except Exception as exc:
            raise CommandError(f"{action} to {identity} failed: {exc}") from exc

        if response is None:
            raise CommandError(f"{action} to {identity} returned no result")
        return _as_dict(response)

    async def remote_start_transaction(
        self,
        identity: str,
        *,
        id_tag: str,
        connector_id: int | None = None,
        charging_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            identity,
            call.RemoteStartTransaction(
                id_tag=id_tag,
                connector_id=connector_id,
                charging_profile=charging_profile,
            ),
        )

    async def remote_stop_transaction(
        self, identity: str, *, transaction_id: int
    ) -> dict[str, Any]:
        return await self._call(
            identity, call.RemoteStopTransaction(transaction_id=transaction_id)
        )

    async def set_charging_profile(
        self, identity: str, *, connector_id: int, cs_charging_profiles: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._call(
            identity,
            call.SetChargingProfile(
                connector_id=connector_id, cs_charging_profiles=cs_charging_profiles
            ),
        )

    async def clear_charging_profile(
        self,
        identity: str,
        *,
        profile_id: int | None = None,
        connector_id: int | None = None,
        charging_profile_purpose: str | None = None,
        stack_level: int | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            identity,
            call.ClearChargingProfile(
                id=profile_id,
                connector_id=connector_id,
                charging_profile_purpose=charging_profile_purpose,
                stack_level=stack_level,
            ),
        )

    async def trigger_message(
        self, identity: str, *, requested_message: str, connector_id: int | None = None
    ) -> dict[str, Any]:
        return await self._call(
            identity,
            call.TriggerMessage(
                requested_message=requested_message, connector_id=connector_id
            ),
        )

    async def reset(self, identity: str, *, type: str = "Soft") -> dict[str, Any]:
        return await self._call(identity, call.Reset(type=type))

    async def unlock_connector(
        self, identity: str, *, connector_id: int
    ) -> dict[str, Any]:
        return await self._call(
            identity, call.UnlockConnector(connector_id=connector_id)
        )

    async def change_availability(
        self, identity: str, *, connector_id: int, type: str
    ) -> dict[str, Any]:
        return await self._call(
            identity, call.ChangeAvailability(connector_id=connector_id, type=type)
        )

    async def change_configuration(
        self, identity: str, *, key: str, value: str
    ) -> dict[str, Any]:
        return await self._call(
            identity, call.ChangeConfiguration(key=key, value=value)
        )

    async def get_configuration(
        self, identity: str, *, keys: list[str] | None = None
    ) -> dict[str, Any]:
        return await self._call(identity, call.GetConfiguration(key=keys))

    async def get_diagnostics(
        self,
        identity: str,
        *,
        location: str,
        retries: int | None = None,
        retry_interval: int | None = None,
        start_time: str | None = None,
        stop_time: str | None = None,
    ) -> dict[str, Any]:
        """Ask the charger to upload its logs to a location you control."""
        payload: dict[str, Any] = {"location": location}
        if retries is not None:
            payload["retries"] = retries
        if retry_interval is not None:
            payload["retry_interval"] = retry_interval
        if start_time:
            payload["start_time"] = start_time
        if stop_time:
            payload["stop_time"] = stop_time
        return await self._call(identity, call.GetDiagnostics(**payload))

    async def get_local_list_version(self, identity: str) -> dict[str, Any]:
        """Ask which revision of the local authorization list the charger holds."""
        return await self._call(identity, call.GetLocalListVersion())

    async def send_local_list(
        self,
        identity: str,
        *,
        list_version: int,
        update_type: str = "Full",
        local_authorization_list: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Push (or clear, with an empty list) the offline authorization list.

        update_type must be "Full" or "Differential" -- Full replaces the
        charger's list outright, Differential adds/updates/removes only the
        entries given.
        """
        return await self._call(
            identity,
            call.SendLocalList(
                list_version=list_version,
                update_type=update_type,
                local_authorization_list=local_authorization_list or [],
            ),
        )

    async def reserve_now(
        self,
        identity: str,
        *,
        connector_id: int,
        expiry_date: str,
        id_tag: str,
        reservation_id: int,
        parent_id_tag: str | None = None,
    ) -> dict[str, Any]:
        """Hold a connector for one id tag until expiry_date (ISO 8601)."""
        payload: dict[str, Any] = {
            "connector_id": connector_id,
            "expiry_date": expiry_date,
            "id_tag": id_tag,
            "reservation_id": reservation_id,
        }
        if parent_id_tag:
            payload["parent_id_tag"] = parent_id_tag
        return await self._call(identity, call.ReserveNow(**payload))

    async def cancel_reservation(
        self, identity: str, *, reservation_id: int
    ) -> dict[str, Any]:
        return await self._call(
            identity, call.CancelReservation(reservation_id=reservation_id)
        )

    async def get_composite_schedule(
        self,
        identity: str,
        *,
        connector_id: int,
        duration: int,
        charging_rate_unit: str | None = None,
    ) -> dict[str, Any]:
        """Ask what limit is actually in effect on a connector right now,
        combining every stacked charging profile -- the honest answer to
        "what will this connector actually deliver", as opposed to what any
        one profile we sent asked for."""
        payload: dict[str, Any] = {"connector_id": connector_id, "duration": duration}
        if charging_rate_unit:
            payload["charging_rate_unit"] = charging_rate_unit
        return await self._call(identity, call.GetCompositeSchedule(**payload))

    async def update_firmware(
        self,
        identity: str,
        *,
        location: str,
        retrieve_date: str,
        retries: int | None = None,
        retry_interval: int | None = None,
    ) -> dict[str, Any]:
        """Tell the charger to fetch and install firmware from location at
        retrieve_date (ISO 8601). No reply confirms success -- watch for
        FirmwareStatusNotification for that."""
        payload: dict[str, Any] = {
            "location": location,
            "retrieve_date": retrieve_date,
        }
        if retries is not None:
            payload["retries"] = retries
        if retry_interval is not None:
            payload["retry_interval"] = retry_interval
        return await self._call(identity, call.UpdateFirmware(**payload))

    async def data_transfer(
        self,
        identity: str,
        *,
        vendor_id: str,
        message_id: str | None = None,
        data: str | None = None,
    ) -> dict[str, Any]:
        """The escape hatch for anything outside the standard message set.
        Only meaningful to a charger that recognises vendor_id -- most will
        reply UnknownVendorId to anything else, which is a valid, expected
        answer, not a failure."""
        payload: dict[str, Any] = {"vendor_id": vendor_id}
        if message_id:
            payload["message_id"] = message_id
        if data:
            payload["data"] = data
        return await self._call(identity, call.DataTransfer(**payload))

    async def clear_cache(self, identity: str) -> dict[str, Any]:
        return await self._call(identity, call.ClearCache())


def _as_dict(response: Any) -> dict[str, Any]:
    """Normalise an ocpp call_result dataclass into a plain dict."""
    if isinstance(response, dict):
        return response
    import dataclasses

    if dataclasses.is_dataclass(response):
        out = dataclasses.asdict(response)
    else:
        out = dict(getattr(response, "__dict__", {}))
    # Enum members serialise poorly downstream; unwrap to their values.
    return {k: (v.value if hasattr(v, "value") else v) for k, v in out.items()}