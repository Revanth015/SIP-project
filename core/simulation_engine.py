"""Daily warehouse digital-twin simulation engine.

Consumes a confirmed slot master and daily pallet demand. It intentionally
keeps operational time and fatigue as configurable proxies rather than
claiming they are direct observations of worker performance.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SimulationConfig:
    forklift_speed_mps: float = 2.5
    handling_time_sec_per_pallet: float = 0.0
    target_occupancy_pct: float = 65.0
    fatigue_travel_weight: float = 0.6
    fatigue_congestion_weight: float = 0.3
    fatigue_overflow_weight: float = 0.1

    def validate(self):
        if self.forklift_speed_mps <= 0:
            raise ValueError("Forklift speed must be greater than zero.")
        if self.target_occupancy_pct <= 0:
            raise ValueError("Target occupancy must be greater than zero.")
        weights = (
            self.fatigue_travel_weight,
            self.fatigue_congestion_weight,
            self.fatigue_overflow_weight,
        )
        if any(w < 0 for w in weights):
            raise ValueError("Fatigue weights cannot be negative.")
        if not math.isclose(sum(weights), 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("Fatigue weights must sum to 1.0.")


def normalize_demand(demand_df: pd.DataFrame):
    """Normalize common demand formats to DATE + TOTAL_PALLETS."""
    df = demand_df.copy()
    date_col = next((c for c in df.columns if str(c).upper() in {"DATE", "DATETIME", "DAY"}), None)
    pallet_col = next((c for c in df.columns if str(c).upper() in {
        "TOTAL_PALLETS", "PALLETS", "DEMAND", "DAILY_DEMAND"
    }), None)

    if date_col is None or pallet_col is None:
        raise ValueError(
            "Demand file must contain a date column and one of: "
            "TOTAL_PALLETS, PALLETS, DEMAND, DAILY_DEMAND."
        )

    df = df.rename(columns={date_col: "DATE", pallet_col: "TOTAL_PALLETS"})
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df["TOTAL_PALLETS"] = pd.to_numeric(df["TOTAL_PALLETS"], errors="coerce")
    df = df.dropna(subset=["DATE", "TOTAL_PALLETS"])
    df["TOTAL_PALLETS"] = df["TOTAL_PALLETS"].clip(lower=0)

    # If multiple rows exist per day, aggregate them before simulation.
    return (
        df.groupby("DATE", as_index=False)["TOTAL_PALLETS"]
        .sum()
        .sort_values("DATE")
        .reset_index(drop=True)
    )


def _ordered_slots(slot_master: pd.DataFrame):
    required = {"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"}
    missing = required - set(slot_master.columns)
    if missing:
        raise ValueError(f"Slot master missing columns: {sorted(missing)}")
    return slot_master.sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)


def simulate_daily(
    demand_df: pd.DataFrame,
    slot_master_df: pd.DataFrame,
    realistic_capacity: int,
    planning_capacity: float,
    config: SimulationConfig | None = None,
):
    """Simulate daily occupied slots using nearest-door-first assignment.

    The assignment is deterministic, which makes the visual digital twin
    reproducible. A day represents the warehouse state after that day's demand
    has been placed into the available slots.
    """
    config = config or SimulationConfig()
    config.validate()

    demand = normalize_demand(demand_df)
    slots = _ordered_slots(slot_master_df)
    slot_count = len(slots)
    if slot_count == 0:
        raise ValueError("Cannot simulate without pallet slots.")
    if realistic_capacity != slot_count:
        raise ValueError(
            f"Realistic capacity ({realistic_capacity}) does not match slot master ({slot_count})."
        )

    max_daily = max(float(demand["TOTAL_PALLETS"].max()), 1.0)
    max_distance_work = max_daily * max(float(slots["DISTANCE_FROM_DOOR_M"].sum()), 1.0)
    overflow_scale = max(float(demand["TOTAL_PALLETS"].max()) - realistic_capacity, 1.0)

    rows = []
    occupancy_rows = []

    for _, row in demand.iterrows():
        date = row["DATE"]
        pallets = int(round(row["TOTAL_PALLETS"]))
        occupied_n = min(pallets, slot_count)
        occupied_slots = slots.iloc[:occupied_n]
        overflow = max(pallets - slot_count, 0)

        total_distance = float(occupied_slots["DISTANCE_FROM_DOOR_M"].sum())
        travel_time_min = total_distance / config.forklift_speed_mps / 60.0
        handling_time_min = occupied_n * config.handling_time_sec_per_pallet / 60.0
        total_time_min = travel_time_min + handling_time_min

        travel_component = min(total_distance / max_distance_work, 1.0)
        congestion_component = min(pallets / max(realistic_capacity, 1), 1.0)
        overflow_component = min(overflow / overflow_scale, 1.0)
        fatigue = 100 * (
            config.fatigue_travel_weight * travel_component
            + config.fatigue_congestion_weight * congestion_component
            + config.fatigue_overflow_weight * overflow_component
        )

        physical_util = pallets / max(realistic_capacity, 1) * 100
        planning_util = pallets / max(planning_capacity, 1e-9) * 100

        rows.append({
            "DATE": date,
            "PALLETS": pallets,
            "OCCUPIED_SLOTS": occupied_n,
            "OVERFLOW": overflow,
            "STORAGE_USAGE_%": min(physical_util, 100.0),
            "PLANNING_UTILIZATION_%": planning_util,
            "TOTAL_DISTANCE_M": total_distance,
            "TIME_MIN": total_time_min,
            "FATIGUE_SCORE": min(fatigue, 100.0),
        })

        for rank, slot in enumerate(occupied_slots.itertuples(index=False), start=1):
            occupancy_rows.append({
                "DATE": date,
                "SLOT_ID": slot.SLOT_ID,
                "OCCUPIED": 1,
                "PALLET_RANK": rank,
                "X_M": slot.X_M,
                "Y_M": slot.Y_M,
                "DISTANCE_FROM_DOOR_M": slot.DISTANCE_FROM_DOOR_M,
            })

    daily_df = pd.DataFrame(rows)
    occupancy_df = pd.DataFrame(occupancy_rows)

    if not occupancy_df.empty:
        occupancy_df = occupancy_df.sort_values(["DATE", "PALLET_RANK"]).reset_index(drop=True)

    return daily_df, occupancy_df


def simulation_summary(daily_df: pd.DataFrame):
    """Return dashboard-level daily simulation statistics."""
    if daily_df.empty:
        return {
            "operating_days": 0,
            "average_pallets": 0.0,
            "peak_pallets": 0,
            "average_time_min": 0.0,
            "average_distance_m": 0.0,
            "average_fatigue": 0.0,
            "overflow_days": 0,
        }

    return {
        "operating_days": int(len(daily_df)),
        "average_pallets": float(daily_df["PALLETS"].mean()),
        "peak_pallets": int(daily_df["PALLETS"].max()),
        "average_time_min": float(daily_df["TIME_MIN"].mean()),
        "average_distance_m": float(daily_df["TOTAL_DISTANCE_M"].mean()),
        "average_fatigue": float(daily_df["FATIGUE_SCORE"].mean()),
        "overflow_days": int((daily_df["OVERFLOW"] > 0).sum()),
    }


def compare_simulations(current_df: pd.DataFrame, proposed_df: pd.DataFrame):
    """Compare two simulations on common KPI definitions."""
    metrics = [
        ("Average Fatigue", "FATIGUE_SCORE", "mean"),
        ("Average Time (min)", "TIME_MIN", "mean"),
        ("Average Distance (m)", "TOTAL_DISTANCE_M", "mean"),
        ("Average Storage Usage (%)", "STORAGE_USAGE_%", "mean"),
        ("Overflow Days", "OVERFLOW", lambda s: int((s > 0).sum())),
    ]
    rows = []
    for label, col, fn in metrics:
        c = float(fn(current_df[col]))
        p = float(fn(proposed_df[col]))
        improvement = ((c - p) / c * 100) if c else np.nan
        rows.append({
            "METRIC": label,
            "CURRENT": c,
            "PROPOSED": p,
            "IMPROVEMENT_%": improvement,
        })
    return pd.DataFrame(rows)
