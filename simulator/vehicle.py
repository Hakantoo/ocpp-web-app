"""The EV side of the simulation: a battery and a charge curve.

Vehicles are not defined here. They are read from the CSMS at startup, so
there is one list of cars rather than two that drift apart -- which is what
made the same car look like two different cars on two connectors.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Vehicle:
    """A car with a battery that remembers its state of charge."""

    id: int
    name: str
    battery_capacity_kwh: float = 64.0
    max_charge_kw: float = 11.0
    current_soc: float = 24.0

    @classmethod
    def from_api(cls, row: dict) -> "Vehicle":
        return cls(
            id=int(row["id"]),
            name=str(row["name"]),
            battery_capacity_kwh=float(row["battery_capacity_kwh"]),
            max_charge_kw=float(row["max_charge_kw"]),
            current_soc=float(row["current_soc"]),
        )

    def power_at_current_soc(self, evse_limit_kw: float) -> float:
        """Charge power in kW, given what the charger is willing to supply.

        Constant power up to 80% state of charge, then a linear taper to 15%
        of maximum at 100%. Real curves are messier, but the shape is what
        makes the power graph look like an EV rather than a rectangle.
        """
        if self.current_soc >= 100.0:
            return 0.0
        ceiling = min(self.max_charge_kw, evse_limit_kw)
        if self.current_soc <= 80.0:
            return ceiling
        taper = 1.0 - ((self.current_soc - 80.0) / 20.0) * 0.85
        return max(0.0, ceiling * taper)

    def absorb(self, energy_kwh: float) -> None:
        gained = (energy_kwh / self.battery_capacity_kwh) * 100.0
        self.current_soc = min(100.0, self.current_soc + gained)

    @property
    def is_full(self) -> bool:
        return self.current_soc >= 100.0