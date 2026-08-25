"""Capacity metrics for the Universal Warehouse Digital Twin.

The module deliberately keeps physical/geometric capacity separate from
operational and planning capacity so the dashboard cannot accidentally
present a planning target as a physical limit.
"""

from dataclasses import dataclass
import math

import pandas as pd


@dataclass(frozen=True)
class CapacityConfig:
    """Capacity-planning assumptions."""
    target_occupancy_pct: float = 65.0
    min_target_occupancy_pct: float = 60.0
    max_target_occupancy_pct: float = 70.0

    def validate(self):
        if not 0 < self.target_occupancy_pct <= 100:
            raise ValueError("Target occupancy must be between 0 and 100%.")
        if self.min_target_occupancy_pct > self.max_target_occupancy_pct:
            raise ValueError("Minimum target occupancy cannot exceed maximum target occupancy.")


def planning_capacity(realistic_capacity: int | float, target_occupancy_pct: float = 65.0):
    """Calculate planning capacity; this is not a physical capacity."""
    if realistic_capacity < 0:
        raise ValueError("Realistic capacity cannot be negative.")
    if not 0 <= target_occupancy_pct <= 100:
        raise ValueError("Target occupancy must be between 0 and 100%.")
    return float(realistic_capacity) * target_occupancy_pct / 100.0


def capacity_summary(
    warehouse_area_m2: float,
    storage_area_m2: float,
    theoretical_capacity: int,
    realistic_capacity: int,
    target_occupancy_pct: float = 65.0,
):
    """Return a transparent summary of the three capacity levels."""
    if theoretical_capacity < 0 or realistic_capacity < 0:
        raise ValueError("Capacity values cannot be negative.")
    if realistic_capacity > theoretical_capacity:
        raise ValueError("Realistic capacity cannot exceed theoretical capacity.")

    target = planning_capacity(realistic_capacity, target_occupancy_pct)
    geometric_gap = theoretical_capacity - realistic_capacity
    realistic_pct_of_theoretical = (
        realistic_capacity / theoretical_capacity * 100
        if theoretical_capacity else 0.0
    )

    return {
        "warehouse_area_m2": float(warehouse_area_m2),
        "storage_area_m2": float(storage_area_m2),
        "theoretical_capacity": int(theoretical_capacity),
        "realistic_capacity": int(realistic_capacity),
        "planning_occupancy_pct": float(target_occupancy_pct),
        "planning_capacity": float(target),
        "theoretical_to_realistic_gap": int(geometric_gap),
        "realistic_as_pct_of_theoretical": float(realistic_pct_of_theoretical),
    }


def capacity_table(summary: dict):
    """Create a dashboard-ready capacity table."""
    return pd.DataFrame([
        {
            "METRIC": "Theoretical Capacity",
            "VALUE": summary["theoretical_capacity"],
            "UNIT": "pallet slots",
            "INTERPRETATION": "Geometric upper bound before operational deductions",
        },
        {
            "METRIC": "Realistic Operational Capacity",
            "VALUE": summary["realistic_capacity"],
            "UNIT": "pallet slots",
            "INTERPRETATION": "Feasible capacity after aisle, clearance and obstacle constraints",
        },
        {
            "METRIC": "Planning Occupancy",
            "VALUE": summary["planning_occupancy_pct"],
            "UNIT": "%",
            "INTERPRETATION": "Normal operating target; not physical capacity",
        },
        {
            "METRIC": "Planning Capacity",
            "VALUE": summary["planning_capacity"],
            "UNIT": "pallet slots",
            "INTERPRETATION": "Realistic capacity multiplied by planning occupancy",
        },
        {
            "METRIC": "Theoretical → Realistic Gap",
            "VALUE": summary["theoretical_to_realistic_gap"],
            "UNIT": "pallet slots",
            "INTERPRETATION": "Capacity sacrificed for operational feasibility",
        },
        {
            "METRIC": "Realistic / Theoretical",
            "VALUE": summary["realistic_as_pct_of_theoretical"],
            "UNIT": "%",
            "INTERPRETATION": "Share of geometric capacity retained operationally",
        },
    ])


def demand_metrics(demand_df: pd.DataFrame, realistic_capacity: int, planning_cap: float):
    """Calculate demand-vs-capacity metrics from a DATE/TOTAL_PALLETS table."""
    required = {"DATE", "TOTAL_PALLETS"}
    missing = required - set(demand_df.columns)
    if missing:
        raise ValueError(f"Demand data missing columns: {sorted(missing)}")

    demand = pd.to_numeric(demand_df["TOTAL_PALLETS"], errors="coerce").fillna(0)
    physical_overflow = (demand > realistic_capacity).sum()
    planning_overflow = (demand > planning_cap).sum()

    return {
        "operating_days": int(len(demand)),
        "average_daily_demand": float(demand.mean()) if len(demand) else 0.0,
        "peak_demand": float(demand.max()) if len(demand) else 0.0,
        "p95_demand": float(demand.quantile(0.95)) if len(demand) else 0.0,
        "physical_overflow_days": int(physical_overflow),
        "planning_threshold_days": int(planning_overflow),
        "average_physical_utilization_pct": float(
            (demand / max(realistic_capacity, 1) * 100).clip(upper=100).mean()
        ) if len(demand) else 0.0,
        "average_planning_utilization_pct": float(
            (demand / max(planning_cap, 1e-9) * 100).clip(upper=100).mean()
        ) if len(demand) else 0.0,
    }


def compare_capacity(current: dict, proposed: dict):
    """Compare two capacity summaries without confusing directionality."""
    rows = []
    metrics = [
        ("Theoretical Capacity", "theoretical_capacity"),
        ("Realistic Operational Capacity", "realistic_capacity"),
        ("Planning Capacity", "planning_capacity"),
    ]

    for label, key in metrics:
        c = float(current[key])
        p = float(proposed[key])
        improvement = ((p - c) / c * 100) if c else math.nan
        rows.append({
            "METRIC": label,
            "CURRENT": c,
            "PROPOSED": p,
            "CHANGE_%": improvement,
        })

    return pd.DataFrame(rows)
