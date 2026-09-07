"""Movement-aware multi-SKU warehouse layout optimizer for the V-belt Digital Twin.

The optimizer is deliberately scenario-based. It uses the physical locations produced by
Model 1 / Process 4 and does not create a second physical warehouse model.
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


def prepare_sku_demand(mto_detail: pd.DataFrame, capacity_units: float = 1.0,
                       safety_buffer: float = 0.15) -> pd.DataFrame:
    """Create a movement and storage-requirement master from historical MTO flow.

    Storage requirement is a planning proxy: maximum observed daily boxes converted to
    pallet-equivalent demand. It is intentionally parameterized because actual slot/bin
    capacity should be validated physically before implementation.
    """
    d = mto_detail.copy()
    required = {"DATE", "ITEM_SIZE", "BELTS"}
    missing = required - set(d.columns)
    if missing:
        raise ValueError(f"MTO detail missing columns: {sorted(missing)}")
    d["DATE"] = pd.to_datetime(d["DATE"], errors="coerce")
    d["ITEM_SIZE"] = d["ITEM_SIZE"].astype("string").str.strip().str.upper()
    d["BELTS"] = pd.to_numeric(d["BELTS"], errors="coerce")
    d = d.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])
    d = d[d["BELTS"] > 0].copy()

    if "BELTS_PER_BOX" not in d.columns:
        d["BELTS_PER_BOX"] = 28.0
    d["BELTS_PER_BOX"] = pd.to_numeric(d["BELTS_PER_BOX"], errors="coerce").fillna(28.0)
    d["BELTS_PER_BOX"] = d["BELTS_PER_BOX"].clip(lower=1)
    d["BOXES"] = np.ceil(d["BELTS"] / d["BELTS_PER_BOX"])

    daily_sku = d.groupby(["DATE", "ITEM_SIZE"], as_index=False).agg(
        DAILY_BELTS=("BELTS", "sum"), DAILY_BOXES=("BOXES", "sum")
    )
    sku = d.groupby("ITEM_SIZE", as_index=False).agg(
        MTO_BELTS=("BELTS", "sum"),
        MTO_LINES=("ITEM_SIZE", "size"),
        ACTIVE_DAYS=("DATE", "nunique"),
        ACTIVE_MONTHS=("DATE", lambda x: x.dt.to_period("M").nunique()),
        AVG_BELTS_PER_LINE=("BELTS", "mean"),
        MAX_BELTS_PER_LINE=("BELTS", "max"),
    )
    peak = daily_sku.groupby("ITEM_SIZE", as_index=False).agg(
        PEAK_DAILY_BELTS=("DAILY_BELTS", "max"),
        PEAK_DAILY_BOXES=("DAILY_BOXES", "max"),
    )
    sku = sku.merge(peak, on="ITEM_SIZE", how="left")
    sku["PEAK_PALLET_EQ"] = sku["PEAK_DAILY_BOXES"] / 32.0
    sku["PLANNING_STORAGE_EQ"] = (sku["PEAK_PALLET_EQ"] * (1.0 + safety_buffer)).clip(lower=0.01)
    sku["REQUIRED_LOCATIONS"] = np.ceil(sku["PLANNING_STORAGE_EQ"] / max(capacity_units, 0.01)).astype(int)
    sku["MOVEMENT_SHARE_PCT"] = sku["MTO_BELTS"] / max(float(sku["MTO_BELTS"].sum()), 1.0) * 100
    sku["FREQUENCY_SCORE"] = _norm(sku["MTO_LINES"])
    sku["VOLUME_SCORE"] = _norm(sku["MTO_BELTS"])
    sku["CONSISTENCY_SCORE"] = _norm(sku["ACTIVE_DAYS"])
    sku["COMBINED_SCORE"] = 0.5 * sku["FREQUENCY_SCORE"] + 0.5 * sku["VOLUME_SCORE"]
    sku["SPACE_EFFICIENCY_SCORE"] = 1.0 / sku["REQUIRED_LOCATIONS"].clip(lower=1)
    sku["MOVEMENT_SPACE_SCORE"] = (
        0.35 * sku["FREQUENCY_SCORE"]
        + 0.35 * sku["VOLUME_SCORE"]
        + 0.20 * sku["CONSISTENCY_SCORE"]
        + 0.10 * sku["SPACE_EFFICIENCY_SCORE"]
    )
    return sku.sort_values("MOVEMENT_SPACE_SCORE", ascending=False).reset_index(drop=True)


def rank_skus(sku: pd.DataFrame, strategy: str) -> pd.DataFrame:
    if strategy == "Frequency":
        keys = ["MTO_LINES", "ACTIVE_DAYS", "MTO_BELTS", "ITEM_SIZE"]
        asc = [False, False, False, True]
        score = "FREQUENCY_SCORE"
    elif strategy == "Volume":
        keys = ["MTO_BELTS", "MTO_LINES", "ACTIVE_DAYS", "ITEM_SIZE"]
        asc = [False, False, False, True]
        score = "VOLUME_SCORE"
    elif strategy == "Frequency + Volume":
        keys = ["COMBINED_SCORE", "MTO_LINES", "MTO_BELTS", "ITEM_SIZE"]
        asc = [False, False, False, True]
        score = "COMBINED_SCORE"
    elif strategy == "Movement + Space":
        keys = ["MOVEMENT_SPACE_SCORE", "MTO_LINES", "MTO_BELTS", "ITEM_SIZE"]
        asc = [False, False, False, True]
        score = "MOVEMENT_SPACE_SCORE"
    else:
        raise ValueError(f"Unknown strategy: {strategy}")
    out = sku.sort_values(keys, ascending=asc).reset_index(drop=True).copy()
    out["RANK"] = np.arange(1, len(out) + 1)
    out["STRATEGY"] = strategy
    out["STRATEGY_SCORE"] = out[score]
    return out


def allocate_shared_locations(ranked: pd.DataFrame, slots: pd.DataFrame,
                              capacity_units: float = 1.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pack multiple SKUs into the same physical location where capacity permits.

    Locations are ordered by distance from the operating door. High-priority SKUs are
    placed into the nearest locations first. A first-fit decreasing-style pass is used
    on storage requirement, while preserving movement priority.
    """
    if slots.empty or ranked.empty:
        return pd.DataFrame(), pd.DataFrame()
    s = slots.sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True).copy()
    cap = max(float(capacity_units), 0.01)
    remaining = np.full(len(s), cap, dtype=float)
    assignments = []

    for _, sku in ranked.iterrows():
        need = max(float(sku.get("PLANNING_STORAGE_EQ", 0.01)), 0.01)
        left = need
        locations_used = 0
        # Prefer the nearest locations with available capacity.
        for i in np.argsort(s["DISTANCE_FROM_DOOR_M"].to_numpy()):
            if left <= 1e-9:
                break
            usable = min(left, remaining[i])
            if usable <= 1e-9:
                continue
            assignments.append({
                "ITEM_SIZE": sku["ITEM_SIZE"],
                "SLOT_ID": s.loc[i, "SLOT_ID"],
                "X_M": s.loc[i, "X_M"],
                "Y_M": s.loc[i, "Y_M"],
                "DISTANCE_FROM_DOOR_M": s.loc[i, "DISTANCE_FROM_DOOR_M"],
                "ALLOCATED_STORAGE_EQ": usable,
                "SKU_REQUIRED_STORAGE_EQ": need,
                "RANK": sku["RANK"],
                "STRATEGY": sku["STRATEGY"],
                "STRATEGY_SCORE": sku["STRATEGY_SCORE"],
                "MTO_LINES": sku["MTO_LINES"],
                "MTO_BELTS": sku["MTO_BELTS"],
                "ACTIVE_DAYS": sku["ACTIVE_DAYS"],
                "MOVEMENT_SHARE_PCT": sku["MOVEMENT_SHARE_PCT"],
                "MOVEMENT_SEGMENT": (
                    "High Frequency / High Volume" if sku["FREQUENCY_SCORE"] >= 0.75 and sku["VOLUME_SCORE"] >= 0.75
                    else "High Frequency / Standard Volume" if sku["FREQUENCY_SCORE"] >= 0.75
                    else "Standard Frequency / High Volume" if sku["VOLUME_SCORE"] >= 0.75
                    else "Standard Frequency / Standard Volume"
                ),
            })
            remaining[i] -= usable
            left -= usable
            locations_used += 1
        if left > 1e-9:
            # The SKU is not fully accommodated; retain the unallocated requirement.
            assignments.append({
                "ITEM_SIZE": sku["ITEM_SIZE"], "SLOT_ID": "UNALLOCATED",
                "X_M": np.nan, "Y_M": np.nan, "DISTANCE_FROM_DOOR_M": np.nan,
                "ALLOCATED_STORAGE_EQ": 0.0, "SKU_REQUIRED_STORAGE_EQ": left,
                "RANK": sku["RANK"], "STRATEGY": sku["STRATEGY"],
                "STRATEGY_SCORE": sku["STRATEGY_SCORE"], "MTO_LINES": sku["MTO_LINES"],
                "MTO_BELTS": sku["MTO_BELTS"], "ACTIVE_DAYS": sku["ACTIVE_DAYS"],
                "MOVEMENT_SHARE_PCT": sku["MOVEMENT_SHARE_PCT"], "MOVEMENT_SEGMENT": "Unallocated",
            })

    alloc = pd.DataFrame(assignments)
    loc = s.copy()
    used = alloc[alloc["SLOT_ID"] != "UNALLOCATED"].groupby("SLOT_ID").agg(
        USED_STORAGE_EQ=("ALLOCATED_STORAGE_EQ", "sum"),
        ASSIGNED_SKUS=("ITEM_SIZE", "nunique"),
    ).reset_index()
    loc = loc.merge(used, on="SLOT_ID", how="left")
    loc["USED_STORAGE_EQ"] = loc["USED_STORAGE_EQ"].fillna(0.0)
    loc["ASSIGNED_SKUS"] = loc["ASSIGNED_SKUS"].fillna(0).astype(int)
    loc["CAPACITY_UTILIZATION_PCT"] = loc["USED_STORAGE_EQ"] / cap * 100
    loc["REMAINING_CAPACITY_EQ"] = cap - loc["USED_STORAGE_EQ"]
    return alloc, loc


def replay_flow(mto_detail: pd.DataFrame, allocation: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replay historical MTO flow through the shared-location allocation."""
    flow = mto_detail[["DATE", "ITEM_SIZE", "BELTS"]].copy()
    flow["DATE"] = pd.to_datetime(flow["DATE"], errors="coerce")
    flow["ITEM_SIZE"] = flow["ITEM_SIZE"].astype("string").str.strip().str.upper()
    flow["BELTS"] = pd.to_numeric(flow["BELTS"], errors="coerce")
    flow = flow.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])
    flow = flow[flow["BELTS"] > 0].copy()
    # One SKU can have multiple locations. Use the nearest assigned location for the
    # comparative movement metric; storage allocation itself may span multiple locations.
    a = allocation[allocation["SLOT_ID"] != "UNALLOCATED"].copy()
    nearest = a.sort_values(["ITEM_SIZE", "DISTANCE_FROM_DOOR_M"]).drop_duplicates("ITEM_SIZE")
    nearest = nearest[["ITEM_SIZE", "SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"]]
    flow = flow.merge(nearest, on="ITEM_SIZE", how="left")
    flow["ASSIGNED"] = flow["SLOT_ID"].notna()
    flow["TRAVEL_M"] = flow["DISTANCE_FROM_DOOR_M"].fillna(0.0)
    flow["BELT_WEIGHTED_TRAVEL_M"] = flow["BELTS"] * flow["TRAVEL_M"]
    daily = flow.groupby("DATE", as_index=False).agg(
        FLOW_LINES=("ITEM_SIZE", "size"), FLOW_BELTS=("BELTS", "sum"),
        ASSIGNED_LINES=("ASSIGNED", "sum"), ASSIGNED_BELTS=("BELTS", lambda x: float(x[flow.loc[x.index, "ASSIGNED"]].sum())),
        TOTAL_ONE_WAY_TRAVEL_M=("TRAVEL_M", "sum"), BELT_WEIGHTED_TRAVEL_M=("BELT_WEIGHTED_TRAVEL_M", "sum"),
    )
    daily["LINE_COVERAGE_PCT"] = daily["ASSIGNED_LINES"] / daily["FLOW_LINES"].replace(0, np.nan) * 100
    daily["BELT_COVERAGE_PCT"] = daily["ASSIGNED_BELTS"] / daily["FLOW_BELTS"].replace(0, np.nan) * 100
    return flow, daily


def evaluate_strategy(mto_detail: pd.DataFrame, slots: pd.DataFrame,
                       sku: pd.DataFrame, strategy: str,
                       capacity_units: float = 1.0) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    ranked = rank_skus(sku, strategy)
    allocation, location = allocate_shared_locations(ranked, slots, capacity_units)
    flow, daily = replay_flow(mto_detail, allocation)
    assigned = flow[flow["ASSIGNED"]]
    total_lines = len(flow)
    total_belts = float(flow["BELTS"].sum())
    used_slots = int(location["USED_STORAGE_EQ"].gt(0).sum())
    shared_locations = int(location["ASSIGNED_SKUS"].gt(1).sum())
    total_travel = float(assigned["TRAVEL_M"].sum()) if not assigned.empty else 0.0
    weighted_travel = float(assigned["BELT_WEIGHTED_TRAVEL_M"].sum()) if not assigned.empty else 0.0
    summary = {
        "Strategy": strategy,
        "Available physical locations": int(len(slots)),
        "SKUs in movement master": int(len(ranked)),
        "SKUs fully/partly allocated": int(allocation["ITEM_SIZE"].nunique()) if not allocation.empty else 0,
        "Used locations": used_slots,
        "Shared locations": shared_locations,
        "Historical flow lines": total_lines,
        "Assigned flow lines": int(assigned.shape[0]),
        "Line coverage %": assigned.shape[0] / max(total_lines, 1) * 100,
        "Historical MTO belts": total_belts,
        "Assigned MTO belts": float(assigned["BELTS"].sum()) if not assigned.empty else 0.0,
        "Belt coverage %": (float(assigned["BELTS"].sum()) / max(total_belts, 1)) * 100 if not assigned.empty else 0.0,
        "Total one-way travel m": total_travel,
        "Avg one-way m / assigned line": total_travel / max(len(assigned), 1),
        "Belt-weighted travel m": weighted_travel,
        "Average location utilization %": float(location["CAPACITY_UTILIZATION_PCT"].mean()) if not location.empty else 0.0,
        "Peak location utilization %": float(location["CAPACITY_UTILIZATION_PCT"].max()) if not location.empty else 0.0,
        "Unallocated storage rows": int((allocation["SLOT_ID"] == "UNALLOCATED").sum()) if not allocation.empty else 0,
    }
    return ranked, allocation, location, summary


def compare_layouts(results: dict[str, dict]) -> pd.DataFrame:
    rows = [v["summary"] for v in results.values()]
    x = pd.DataFrame(rows)
    if x.empty:
        return x
    x["Travel_Efficiency"] = 1 - (
        x["Total one-way travel m"] - x["Total one-way travel m"].min()
    ) / max(x["Total one-way travel m"].max() - x["Total one-way travel m"].min(), 1e-9)
    x["Storage_Efficiency"] = (x["Average location utilization %"] / 100).clip(0, 1)
    x["Balanced_Layout_Score"] = (
        x["Line coverage %"] / 100 + x["Belt coverage %"] / 100
        + x["Travel_Efficiency"] + x["Storage_Efficiency"]
    ) / 4 * 100
    x["Overall_Rank"] = x["Balanced_Layout_Score"].rank(method="min", ascending=False).astype(int)
    return x.sort_values("Overall_Rank").reset_index(drop=True)
