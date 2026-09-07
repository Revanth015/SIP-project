"""Management decision and current-state baseline utilities for Model 2B."""
from __future__ import annotations

import numpy as np
import pandas as pd


def normalize_location_master(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a current SKU-location mapping to the optimizer schema."""
    x = df.copy()
    lookup = {str(c).strip().upper(): c for c in x.columns}

    def pick(options):
        for name in options:
            if name.upper() in lookup:
                return lookup[name.upper()]
        return None

    sku_col = pick(["ITEM_SIZE", "SKU", "BELT_SIZE", "LINE_ITEM_SIZE_ID", "SIZE"])
    slot_col = pick(["SLOT_ID", "SLOT", "LOCATION_ID", "LOCATION", "BIN"])
    x_col = pick(["X_M", "X", "CENTER_X"])
    y_col = pick(["Y_M", "Y", "CENTER_Y"])
    d_col = pick(["DISTANCE_FROM_DOOR_M", "DISTANCE_M", "DISTANCE"])
    if not all([sku_col, slot_col, x_col, y_col, d_col]):
        raise ValueError(
            "Current mapping must contain SKU, location, X, Y and distance columns. "
            f"Found: {list(df.columns)}"
        )
    x = x.rename(columns={sku_col: "ITEM_SIZE", slot_col: "SLOT_ID", x_col: "X_M", y_col: "Y_M", d_col: "DISTANCE_FROM_DOOR_M"})
    x["ITEM_SIZE"] = x["ITEM_SIZE"].astype("string").str.strip().str.upper()
    x["SLOT_ID"] = x["SLOT_ID"].astype("string").str.strip()
    for c in ["X_M", "Y_M", "DISTANCE_FROM_DOOR_M"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.dropna(subset=["ITEM_SIZE", "SLOT_ID", "DISTANCE_FROM_DOOR_M"]).copy()
    return x


def replay_current_layout(mto_detail: pd.DataFrame, current_mapping: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Replay historical MTO against the actual current SKU-location mapping."""
    flow = mto_detail[["DATE", "ITEM_SIZE", "BELTS"]].copy()
    flow["DATE"] = pd.to_datetime(flow["DATE"], errors="coerce")
    flow["ITEM_SIZE"] = flow["ITEM_SIZE"].astype("string").str.strip().str.upper()
    flow["BELTS"] = pd.to_numeric(flow["BELTS"], errors="coerce")
    flow = flow.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])
    flow = flow[flow["BELTS"] > 0].copy()

    # Multiple current locations per SKU are allowed; the nearest listed location
    # is used for the comparative movement proxy, matching the optimizer replay rule.
    nearest = (
        current_mapping.sort_values(["ITEM_SIZE", "DISTANCE_FROM_DOOR_M"])
        .drop_duplicates("ITEM_SIZE")
    )
    nearest = nearest[["ITEM_SIZE", "SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"]]
    flow = flow.merge(nearest, on="ITEM_SIZE", how="left")
    flow["ASSIGNED"] = flow["SLOT_ID"].notna()
    flow["TRAVEL_M"] = flow["DISTANCE_FROM_DOOR_M"].fillna(0.0)
    flow["BELT_WEIGHTED_TRAVEL_M"] = flow["BELTS"] * flow["TRAVEL_M"]

    assigned = flow[flow["ASSIGNED"]]
    summary = {
        "Historical flow lines": int(len(flow)),
        "Assigned flow lines": int(len(assigned)),
        "Line coverage %": len(assigned) / max(len(flow), 1) * 100,
        "Historical MTO belts": float(flow["BELTS"].sum()),
        "Assigned MTO belts": float(assigned["BELTS"].sum()),
        "Belt coverage %": float(assigned["BELTS"].sum()) / max(float(flow["BELTS"].sum()), 1.0) * 100,
        "Total one-way travel m": float(assigned["TRAVEL_M"].sum()),
        "Avg one-way m / assigned line": float(assigned["TRAVEL_M"].mean()) if not assigned.empty else 0.0,
        "Belt-weighted travel m": float(assigned["BELT_WEIGHTED_TRAVEL_M"].sum()),
        "Mapped SKUs": int(assigned["ITEM_SIZE"].nunique()),
        "Unmapped SKUs": int(flow.loc[~flow["ASSIGNED"], "ITEM_SIZE"].nunique()),
        "Current locations": int(current_mapping["SLOT_ID"].nunique()),
    }
    return flow, summary


def improvement(current: dict, optimized: dict) -> dict:
    """Return directional current-to-optimized changes."""
    def pct(old, new):
        return (new - old) / abs(old) * 100 if abs(old) > 1e-12 else np.nan

    return {
        "Line coverage change pp": optimized["Line coverage %"] - current["Line coverage %"],
        "Belt coverage change pp": optimized["Belt coverage %"] - current["Belt coverage %"],
        "Travel change %": pct(current["Total one-way travel m"], optimized["Total one-way travel m"]),
        "Avg travel / line change %": pct(current["Avg one-way m / assigned line"], optimized["Avg one-way m / assigned line"]),
        "Belt-weighted travel change %": pct(current["Belt-weighted travel m"], optimized["Belt-weighted travel m"]),
    }
