"""The EV side of the simulation: a battery and a real charge curve.

Vehicles are not defined here. They are read from the CSMS at startup, so
there is one list of cars rather than two that drift apart -- which is what
made the same car look like two different cars on two connectors.
"""

from __future__ import annotations

from dataclasses import dataclass

# Each profile is a real, sourced DC fast-charging curve shape, not the
# single generic taper every vehicle used to share. Points are
# (state_of_charge_percent, fraction_of_peak_power), read as a piecewise
# linear curve between them -- chosen to be shape-distinct from each other,
# matching what actual published charging tests show, not five different
# multipliers on the same shape.
#
#   generic     -- the original: flat to 80%, straight taper to 15% by 100%.
#   renault     -- Renault Megane E-Tech. Peaks around 10% SOC, holds
#                  strong to ~25-30%, then a steady taper -- roughly
#                  80-90 kW by 50% SOC, falling further after 80%.
#                  Source: planevcharge.com, evcourse.com Megane E-Tech
#                  charging guides.
#   tesla       -- Tesla Model 3 on a V3 Supercharger. Very high peak,
#                  sustained through a fairly narrow low-SOC window
#                  (roughly 5-25%), then a real but moderate decline,
#                  with the sharpest drop above 80%.
#                  Source: Tesla Motors Club V3 Supercharging curve
#                  threads, ChargeCalcs Tesla charging time calculator.
#   hyundai_kia -- Hyundai/Kia E-GMP 800V platform (Ioniq 5 / EV6).
#                  The most distinct shape here: holds close to its flat
#                  peak from ~5% all the way to ~50% SOC, then steps down
#                  rather than gradually tapering, with a real cliff at 80%.
#                  Source: ChargeMath EV charge curve simulator, ChargeCalcs
#                  Ioniq 5 & EV6 calculator -- both cite real DCFC test data.
#   vw_id       -- Volkswagen ID.4. Peaks a touch lower than Tesla/Hyundai,
#                  holds to about 30%, then a gentle, wide-range fade
#                  through the 30-70% window, dropping sharply after 80%.
#                  Source: InsideEVs ID.4 82 kWh DC fast charging analysis,
#                  Recharged VW ID.4 charging speed guide.
#   nissan_leaf -- Nissan Leaf 62 kWh, CHAdeMO, passively cooled. The
#                  slowest and least consistent of the five: a much lower
#                  peak that is reached gradually rather than instantly,
#                  starting to taper earlier (around 50-60% SOC) and more
#                  gradually than the others, reflecting the Leaf's known
#                  lack of active thermal management.
#                  Source: InsideEVs Nissan Leaf DC fast charging curve
#                  test, My Nissan Leaf Forum CHAdeMO tapering threads.
CHARGE_CURVES: dict[str, tuple[tuple[float, float], ...]] = {
    "generic": (
        (0.0, 1.0), (80.0, 1.0), (100.0, 0.15),
    ),
    "renault": (
        (0.0, 0.85), (10.0, 1.0), (25.0, 1.0), (50.0, 0.65),
        (80.0, 0.35), (100.0, 0.08),
    ),
    "tesla": (
        (0.0, 0.9), (5.0, 1.0), (25.0, 1.0), (50.0, 0.6),
        (80.0, 0.35), (100.0, 0.06),
    ),
    "hyundai_kia": (
        (0.0, 0.8), (5.0, 1.0), (50.0, 1.0), (55.0, 0.65),
        (80.0, 0.5), (81.0, 0.2), (100.0, 0.07),
    ),
    "vw_id": (
        (0.0, 0.85), (10.0, 1.0), (30.0, 1.0), (70.0, 0.5),
        (80.0, 0.45), (100.0, 0.1),
    ),
    "nissan_leaf": (
        (0.0, 0.5), (15.0, 1.0), (55.0, 1.0), (85.0, 0.3),
        (100.0, 0.12),
    ),
}


def _curve_fraction(profile: str, soc: float) -> float:
    """Piecewise-linear interpolation over a curve's (soc, fraction) points."""
    points = CHARGE_CURVES.get(profile, CHARGE_CURVES["generic"])
    if soc <= points[0][0]:
        return points[0][1]
    for (s0, f0), (s1, f1) in zip(points, points[1:]):
        if s0 <= soc <= s1:
            if s1 == s0:
                return f1
            t = (soc - s0) / (s1 - s0)
            return f0 + (f1 - f0) * t
    return points[-1][1]


@dataclass
class Vehicle:
    """A car with a battery that remembers its state of charge."""

    id: int
    name: str
    battery_capacity_kwh: float = 64.0
    max_charge_kw: float = 11.0
    current_soc: float = 24.0
    charge_profile: str = "generic"

    @classmethod
    def from_api(cls, row: dict) -> "Vehicle":
        return cls(
            id=int(row["id"]),
            name=str(row["name"]),
            battery_capacity_kwh=float(row["battery_capacity_kwh"]),
            max_charge_kw=float(row["max_charge_kw"]),
            current_soc=float(row["current_soc"]),
            charge_profile=str(row.get("charge_profile") or "generic"),
        )

    def power_at_current_soc(self, evse_limit_kw: float) -> float:
        """Charge power in kW, given what the charger is willing to supply.

        The shape comes from this vehicle's own charge_profile -- a real,
        sourced curve (see CHARGE_CURVES above), not one generic taper
        every car used to share regardless of make.
        """
        if self.current_soc >= 100.0:
            return 0.0
        ceiling = min(self.max_charge_kw, evse_limit_kw)
        fraction = _curve_fraction(self.charge_profile, self.current_soc)
        return max(0.0, ceiling * fraction)

    def absorb(self, energy_kwh: float) -> None:
        gained = (energy_kwh / self.battery_capacity_kwh) * 100.0
        self.current_soc = min(100.0, self.current_soc + gained)

    @property
    def is_full(self) -> bool:
        return self.current_soc >= 100.0