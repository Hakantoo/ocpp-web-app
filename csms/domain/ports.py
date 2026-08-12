"""Interfaces the domain layer depends on.

The domain must be able to tell a charge point to do something without
importing anything from the OCPP layer -- otherwise the state machine could
not be tested without a WebSocket. ocpp_/registry.py provides the concrete
implementation.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class CommandError(RuntimeError):
    """A CALL failed, timed out, or the charger is not connected."""


@runtime_checkable
class ChargePointCommands(Protocol):
    """Outbound OCPP CALLs, as the domain layer sees them."""

    def is_connected(self, identity: str) -> bool: ...

    async def remote_start_transaction(
        self,
        identity: str,
        *,
        id_tag: str,
        connector_id: int | None = None,
        charging_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def remote_stop_transaction(
        self, identity: str, *, transaction_id: int
    ) -> dict[str, Any]: ...

    async def set_charging_profile(
        self, identity: str, *, connector_id: int, cs_charging_profiles: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def clear_charging_profile(
        self,
        identity: str,
        *,
        profile_id: int | None = None,
        connector_id: int | None = None,
        charging_profile_purpose: str | None = None,
        stack_level: int | None = None,
    ) -> dict[str, Any]: ...

    async def trigger_message(
        self, identity: str, *, requested_message: str, connector_id: int | None = None
    ) -> dict[str, Any]: ...

    async def reset(self, identity: str, *, type: str = "Soft") -> dict[str, Any]: ...

    async def unlock_connector(
        self, identity: str, *, connector_id: int
    ) -> dict[str, Any]: ...

    async def change_configuration(
        self, identity: str, *, key: str, value: str
    ) -> dict[str, Any]: ...

    async def get_configuration(
        self, identity: str, *, keys: list[str] | None = None
    ) -> dict[str, Any]: ...
