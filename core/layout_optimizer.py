"""Movement-aware shared-location optimizer for the V-belt warehouse Digital Twin.

Model 2B keeps Process-4 physical locations fixed and uses historical MTO flow to
prioritise moving SKUs. Multiple SKUs may share a physical location. Because the
available project data does not contain a validated SKU-per-location capacity,
that capacity is an explicit planning parameter rather than an invented fact.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

STRATEGIES = ["Frequency", "Volume", "Frequency + Volume", "Movement + Space"]


def _norm(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").fillna(0.0)
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo:
        return pd.Series(1.0, index=s.index)
    return (s - lo) / (hi - lo)


def prepare_sku_demand(mto_detail: pd.DataFrame) -> pd.DataFrame:
    """Build the historical moving-SKU master from transaction-level MTO flow."""
    required = {"DATE", "ITEM_SIZE", "BELTS"}
    missing = required - set(mto_detail.columns)
    if missing:
        raise ValueError(f"MTO detail missing columns: {sorted(missing)}")

    d = mto_detail.copy()
    d["DATE"] = pd.to_datetime(d["DATE"], errors="coerce")
    d["ITEM_SIZE"] = d["ITEM_SIZE"].astype("string").str.strip().str.upper()
    d["BELTS"] = pd.to_numeric(d["BELTS"], errors="coerce")
    d = d.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])
    d = d[d["BELTS"] > 0].copy()

    if "BELTS_PER_BOX" not in d.columns:
        d["BELTS_PER_BOX"] = 28.0
    d["BELTS_PER_BOX"] = pd.to_numeric(d["BELTS_PER_BOX"], errors="coerce").fillna(28.0).clip(lower=1)
    d["BOXES"] = np.ceil(d["BELTS"] / d["BELTS_PER_BOX"])

    daily = d.groupby(["DATE", "ITEM_SIZE"], as_index=False).agg(
        DAILY_BELTS=("BELTS", "sum"), DAILY_BOXES=("BOXES", "sum")
    )
    sku = d.groupby("ITEM_SIZE", as_index=False).agg(
        MTO_BELTS=("BELTS", "sum"), MTO_LINES=("ITEM_SIZE", "size"),
        ACTIVE_DAYS=("DATE", "nunique"),
        ACTIVE_MONTHS=("DATE", lambda x: x.dt.to_period("M").nunique()),
        AVG_BELTS_PER_LINE=("BELTS", "mean"), MAX_BELTS_PER_LINE=("BELTS", "max"),
    )
    daily_stats = daily.groupby("ITEM_SIZE", as_index=False).agg(
        AVG_DAILY_BELTS=("DAILY_BELTS", "mean"), PEAK_DAILY_BELTS=("DAILY_BELTS", "max"),
        AVG_DAILY_BOXES=("DAILY_BOXES", "mean"), PEAK_DAILY_BOXES=("DAILY_BOXES", "max"),
    )
    sku = sku.merge(daily_stats, on="ITEM_SIZE", how="left")
    sku["MOVEMENT_SHARE_PCT"] = sku["MTO_BELTS"] / max(float(sku["MTO_BELTS"].sum()), 1.0) * 100
    sku["FREQUENCY_SCORE"] = _norm(sku["MTO_LINES"])
    sku["VOLUME_SCORE"] = _norm(sku["MTO_BELTS"])
    sku["CONSISTENCY_SCORE"] = _norm(sku["ACTIVE_DAYS"])
    sku["COMBINED_SCORE"] = 0.5 * sku["FREQUENCY_SCORE"] + 0.5 * sku["VOLUME_SCORE"]
    # Space is evaluated at the location level through sharing/utilisation. We do not
    # invent a pallet-equivalent inventory requirement from MTO flow.
    sku["MOVEMENT_SPACE_SCORE"] = (
        0.40 * sku["FREQUENCY_SCORE"] + 0.40 * sku["VOLUME_SCORE"] + 0.20 * sku["CONSISTENCY_SCORE"]
    )
    return sku.sort_values("MOVEMENT_SPACE_SCORE", ascending=False).reset_index(drop=True)


def rank_skus(sku: pd.DataFrame, strategy: str) -> pd.DataFrame:
    if strategy == "Frequency":
        keys, score = ["MTO_LINES", "ACTIVE_DAYS", "MTO_BELTS", "ITEM_SIZE"], "FREQUENCY_SCORE"
    elif strategy == "Volume":
        keys, score = ["MTO_BELTS", "MTO_LINES", "ACTIVE_DAYS", "ITEM_SIZE"], "VOLUME_SCORE"
    elif strategy == "Frequency + Volume":
        keys, score = ["COMBINED_SCORE", "MTO_LINES", "MTO_BELTS", "ITEM_SIZE"], "COMBINED_SCORE"
    elif strategy == "Movement + Space":
        keys, score = ["MOVEMENT_SPACE_SCORE", "MTO_LINES", "MTO_BELTS", "ITEM_SIZE"], "MOVEMENT_SPACE_SCORE"
    else:
        raise ValueError(f"Unknown strategy: {strategy}")
    out = sku.sort_values(keys, ascending=[False, False, False, True]).reset_index(drop=True).copy()
    out["RANK"] = np.arange(1, len(out) + 1)
    out["STRATEGY"] = strategy
    out["STRATEGY_SCORE"] = out[score]
    return out


def allocate_shared_locations(ranked: pd.DataFrame, slots: pd.DataFrame,
                              max_skus_per_location: int = 5) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Assign ranked SKUs to nearest Process-4 locations with controlled sharing.

    This is a transparent slotting heuristic, not a claim of global mathematical optimality.
    """
    if slots.empty or ranked.empty:
        return pd.DataFrame(), pd.DataFrame()
    if max_skus_per_location < 1:
        raise ValueError("max_skus_per_location must be >= 1")

    s = slots.sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True).copy()
    rows = []
    max_assignments = len(s) * int(max_skus_per_location)
    for rank_index, (_, sku) in enumerate(ranked.iterrows()):
        if rank_index >= max_assignments:
            break
        loc = s.iloc[rank_index // int(max_skus_per_location)]
        rows.append({
            "ITEM_SIZE": sku["ITEM_SIZE"], "SLOT_ID": loc["SLOT_ID"],
            "X_M": loc["X_M"], "Y_M": loc["Y_M"],
            "DISTANCE_FROM_DOOR_M": loc["DISTANCE_FROM_DOOR_M"],
            "RANK": sku["RANK"], "STRATEGY": sku["STRATEGY"],
            "STRATEGY_SCORE": sku["STRATEGY_SCORE"], "MTO_LINES": sku["MTO_LINES"],
            "MTO_BELTS": sku["MTO_BELTS"], "ACTIVE_DAYS": sku["ACTIVE_DAYS"],
            "MOVEMENT_SHARE_PCT": sku["MOVEMENT_SHARE_PCT"],
            "MOVEMENT_SEGMENT": (
                "High Frequency / High Volume" if sku["FREQUENCY_SCORE"] >= 0.75 and sku["VOLUME_SCORE"] >= 0.75
                else "High Frequency / Standard Volume" if sku["FREQUENCY_SCORE"] >= 0.75
                else "Standard Frequency / High Volume" if sku["VOLUME_SCORE"] >= 0.75
                else "Standard Frequency / Standard Volume"
            ),
        })

    alloc = pd.DataFrame(rows)
    loc = s.copy()
    used = alloc.groupby("SLOT_ID").agg(ASSIGNED_SKUS=("ITEM_SIZE", "nunique")).reset_index()
    loc = loc.merge(used, on="SLOT_ID", how="left")
    loc["ASSIGNED_SKUS"] = loc["ASSIGNED_SKUS"].fillna(0).astype(int)
    loc["LOCATION_SHARE_CAPACITY"] = int(max_skus_per_location)
    loc["CAPACITY_UTILIZATION_PCT"] = loc["ASSIGNED_SKUS"] / int(max_skus_per_location) * 100
    loc["REMAINING_SKU_CAPACITY"] = int(max_skus_per_location) - loc["ASSIGNED_SKUS"]
    return alloc, loc


def replay_flow(mto_detail: pd.DataFrame, allocation: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replay every historical MTO transaction against the assigned primary location."""
    flow = mto_detail[["DATE", "ITEM_SIZE", "BELTS"]].copy()
    flow["DATE"] = pd.to_datetime(flow["DATE"], errors="coerce")
    flow["ITEM_SIZE"] = flow["ITEM_SIZE"].astype("string").str.strip().str.upper()
    flow["BELTS"] = pd.to_numeric(flow["BELTS"], errors="coerce")
    flow = flow.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])
    flow = flow[flow["BELTS"] > 0].copy()
    nearest = allocation.sort_values(["ITEM_SIZE", "DISTANCE_FROM_DOOR_M"]).drop_duplicates("ITEM_SIZE")
    flow = flow.merge(nearest[["ITEM_SIZE", "SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"]], on="ITEM_SIZE", how="left")
    flow["ASSIGNED"] = flow["SLOT_ID"].notna()
    flow["TRAVEL_M"] = flow["DISTANCE_FROM_DOOR_M"].fillna(0.0)
    flow["BELT_WEIGHTED_TRAVEL_M"] = flow["BELTS"] * flow["TRAVEL_M"]
    daily = flow.groupby("DATE", as_index=False).agg(
        FLOW_LINES=("ITEM_SIZE", "size"), FLOW_BELTS=("BELTS", "sum"),
        ASSIGNED_LINES=("ASSIGNED", "sum"),
        ASSIGNED_BELTS=("BELTS", lambda x: float(x[flow.loc[x.index, "ASSIGNED"]].sum())),
        TOTAL_ONE_WAY_TRAVEL_M=("TRAVEL_M", "sum"),
        BELT_WEIGHTED_TRAVEL_M=("BELT_WEIGHTED_TRAVEL_M", "sum"),
    )
    daily["LINE_COVERAGE_PCT"] = daily["ASSIGNED_LINES"] / daily["FLOW_LINES"].replace(0, np.nan) * 100
    daily["BELT_COVERAGE_PCT"] = daily["ASSIGNED_BELTS"] / daily["FLOW_BELTS"].replace(0, np.nan) * 100
    return flow, daily


def evaluate_strategy(mto_detail: pd.DataFrame, slots: pd.DataFrame, sku: pd.DataFrame,
                      strategy: str, max_skus_per_location: int = 5) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    ranked = rank_skus(sku, strategy)
    allocation, location = allocate_shared_locations(ranked, slots, max_skus_per_location)
    flow, daily = replay_flow(mto_detail, allocation)
    assigned = flow[flow["ASSIGNED"]]
    total_lines, total_belts = len(flow), float(flow["BELTS"].sum())
    used_locations = int(location["ASSIGNED_SKUS"].gt(0).sum())
    shared_locations = int(location["ASSIGNED_SKUS"].gt(1).sum())
    total_travel = float(assigned["TRAVEL_M"].sum()) if not assigned.empty else 0.0
    weighted_travel = float(assigned["BELT_WEIGHTED_TRAVEL_M"].sum()) if not assigned.empty else 0.0
    allocated_skus = int(allocation["ITEM_SIZE"].nunique()) if not allocation.empty else 0
    summary = {
        "Strategy": strategy, "Available physical locations": int(len(slots)),
        "SKUs in movement master": int(len(ranked)), "Allocated SKUs": allocated_skus,
        "Unallocated SKUs": int(len(ranked) - allocated_skus), "Used locations": used_locations,
        "Shared locations": shared_locations,
        "Average SKUs / used location": float(allocated_skus / used_locations) if used_locations else 0.0,
        "Location capacity utilisation %": float(location["ASSIGNED_SKUS"].sum() / (len(location) * max_skus_per_location) * 100) if len(location) else 0.0,
        "Historical flow lines": total_lines, "Assigned flow lines": int(len(assigned)),
        "Line coverage %": len(assigned) / max(total_lines, 1) * 100,
        "Historical MTO belts": total_belts,
        "Assigned MTO belts": float(assigned["BELTS"].sum()) if not assigned.empty else 0.0,
        "Belt coverage %": float(assigned["BELTS"].sum()) / max(total_belts, 1) * 100 if not assigned.empty else 0.0,
        "Total one-way travel m": total_travel,
        "Avg one-way m / assigned line": total_travel / max(len(assigned), 1),
        "Belt-weighted travel m": weighted_travel,
    }
    return ranked, allocation, location, summary


def compare_layouts(results: dict[str, dict]) -> pd.DataFrame:
    x = pd.DataFrame([v["summary"] for v in results.values()])
    if x.empty:
        return x
    lo, hi = x["Total one-way travel m"].min(), x["Total one-way travel m"].max()
    x["Travel_Efficiency"] = 1.0 if hi <= lo else 1 - (x["Total one-way travel m"] - lo) / (hi - lo)
    x["Balanced_Performance_Score"] = (x["Line coverage %"] / 100 + x["Belt coverage %"] / 100 + x["Travel_Efficiency"]) / 3 * 100
    x["Decision_Rank"] = x["Balanced_Performance_Score"].rank(method="min", ascending=False).astype(int)
    return x.sort_values(["Decision_Rank", "Strategy"]).reset_index(drop=True)
