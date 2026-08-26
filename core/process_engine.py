"""Model 2 — MTO packing/process automation model.

Purpose:
    Model the proposed process in which MTO belts that do not yet make a full
    standard box are placed together in one temporary box. The box remains in
    temporary staging until additional compatible belts/orders complete it or
    the contents are consumed/dispatched.

This is a decision-support model. When detailed order-level MTO data are not
available, the daily pallet demand can be used as a demand-volume proxy and
all process assumptions remain editable in the UI.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ProcessModelConfig:
    """Editable Model 2 assumptions."""
    mto_share_pct: float = 30.0
    standard_box_qty: float = 10.0
    average_belts_per_mto_order: float = 6.0
    partial_box_rate_pct: float = 50.0
    current_touches_per_box: float = 4.0
    proposed_touches_per_box: float = 2.0
    error_probability_per_touch_pct: float = 1.5
    current_pack_time_sec: float = 40.0
    proposed_pack_time_sec: float = 25.0
    current_count_time_sec: float = 30.0
    proposed_count_time_sec: float = 20.0
    current_wip_store_time_sec: float = 60.0
    proposed_wip_store_time_sec: float = 35.0
    current_wip_retrieve_time_sec: float = 90.0
    proposed_wip_retrieve_time_sec: float = 45.0
    average_wip_dwell_days: float = 3.0
    proposed_wip_dwell_days: float = 2.0
    box_footprint_m2: float = 0.25
    boxes_per_pallet: float = 32.0
    boxes_per_temporary_pallet: float = 32.0
    automation_coverage_pct: float = 0.0
    max_temporary_wip_boxes: float = 500.0

    def validate(self):
        if not 0 <= self.mto_share_pct <= 100:
            raise ValueError("MTO share must be between 0 and 100%.")
        if self.standard_box_qty <= 0:
            raise ValueError("Standard box quantity must be greater than zero.")
        if self.average_belts_per_mto_order <= 0:
            raise ValueError("Average belts per MTO order must be greater than zero.")
        for name in ("partial_box_rate_pct", "automation_coverage_pct", "error_probability_per_touch_pct"):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100%.")
        for name in (
            "current_touches_per_box", "proposed_touches_per_box",
            "current_pack_time_sec", "proposed_pack_time_sec",
            "current_count_time_sec", "proposed_count_time_sec",
            "current_wip_store_time_sec", "proposed_wip_store_time_sec",
            "current_wip_retrieve_time_sec", "proposed_wip_retrieve_time_sec",
            "average_wip_dwell_days", "proposed_wip_dwell_days",
            "box_footprint_m2", "boxes_per_pallet", "boxes_per_temporary_pallet",
            "max_temporary_wip_boxes",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative.")


def normalize_process_demand(demand_df: pd.DataFrame):
    """Normalize daily demand for Model 2.

    Accepts DATE + TOTAL_PALLETS/PALLETS/DEMAND. The model converts pallet
    demand into a configurable process-volume proxy. If order-level MTO data
    are supplied later, this function can be replaced by exact order logic.
    """
    df = demand_df.copy()
    date_col = next((c for c in df.columns if str(c).upper() in {"DATE", "DATETIME", "DAY"}), None)
    qty_col = next((c for c in df.columns if str(c).upper() in {
        "TOTAL_PALLETS", "PALLETS", "DEMAND", "DAILY_DEMAND"
    }), None)
    if date_col is None or qty_col is None:
        raise ValueError("Model 2 demand needs DATE and TOTAL_PALLETS/PALLETS/DEMAND.")
    df = df.rename(columns={date_col: "DATE", qty_col: "TOTAL_PALLETS"})
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df["TOTAL_PALLETS"] = pd.to_numeric(df["TOTAL_PALLETS"], errors="coerce")
    df = df.dropna(subset=["DATE", "TOTAL_PALLETS"])
    return df.groupby("DATE", as_index=False)["TOTAL_PALLETS"].sum().sort_values("DATE")


def _boxes_from_belts(belts, box_qty):
    """Number of standard boxes required if every partial quantity is boxed."""
    return int(math.ceil(max(float(belts), 0.0) / box_qty))


def estimate_process_day(pallets, cfg: ProcessModelConfig):
    """Estimate current/proposed process quantities for one demand day."""
    mto_belts = max(float(pallets), 0.0) * cfg.mto_share_pct / 100.0 * cfg.average_belts_per_mto_order
    full_boxes = math.floor(mto_belts / cfg.standard_box_qty)
    remainder = mto_belts % cfg.standard_box_qty
    partial_orders = 1 if remainder > 1e-9 else 0

    # Current process: partial quantities can create additional manual handling
    # and separate temporary pieces. Proposed process: one SKU/partial quantity
    # is placed in one temporary box until completion/dispatch.
    current_process_boxes = full_boxes + partial_orders
    proposed_initial_wip_boxes = partial_orders * cfg.partial_box_rate_pct / 100.0

    # Process automation only affects the configurable covered share; the rest
    # retains the proposed manual assumptions.
    coverage = cfg.automation_coverage_pct / 100.0
    proposed_touches = (
        cfg.proposed_touches_per_box * coverage
        + cfg.current_touches_per_box * (1 - coverage)
    )
    proposed_pack_time = cfg.proposed_pack_time_sec * coverage + cfg.current_pack_time_sec * (1 - coverage)
    proposed_count_time = cfg.proposed_count_time_sec * coverage + cfg.current_count_time_sec * (1 - coverage)
    proposed_store_time = cfg.proposed_wip_store_time_sec * coverage + cfg.current_wip_store_time_sec * (1 - coverage)
    proposed_retrieve_time = cfg.proposed_wip_retrieve_time_sec * coverage + cfg.current_wip_retrieve_time_sec * (1 - coverage)

    current_time = current_process_boxes * (
        cfg.current_pack_time_sec + cfg.current_count_time_sec
        + cfg.current_wip_store_time_sec + cfg.current_wip_retrieve_time_sec
    )
    proposed_time = (full_boxes + proposed_initial_wip_boxes) * (
        proposed_pack_time + proposed_count_time
        + proposed_store_time + proposed_retrieve_time
    )

    current_touches = current_process_boxes * cfg.current_touches_per_box
    proposed_touches = (full_boxes + proposed_initial_wip_boxes) * proposed_touches

    p = cfg.error_probability_per_touch_pct / 100.0
    current_error_risk_pct = 100 * (1 - (1 - p) ** max(current_touches, 0))
    proposed_error_risk_pct = 100 * (1 - (1 - p) ** max(proposed_touches, 0))

    return {
        "MTO_BELTS_PROXY": mto_belts,
        "FULL_BOXES": full_boxes,
        "PARTIAL_BOXES": partial_orders,
        "CURRENT_PROCESS_BOXES": current_process_boxes,
        "PROPOSED_INITIAL_WIP_BOXES": proposed_initial_wip_boxes,
        "CURRENT_TIME_SEC": current_time,
        "PROPOSED_TIME_SEC": proposed_time,
        "CURRENT_TOUCHES": current_touches,
        "PROPOSED_TOUCHES": proposed_touches,
        "CURRENT_ERROR_RISK_%": current_error_risk_pct,
        "PROPOSED_ERROR_RISK_%": proposed_error_risk_pct,
    }


def simulate_process_model(demand_df: pd.DataFrame, config: ProcessModelConfig | None = None):
    """Run Model 2 over the daily demand history."""
    cfg = config or ProcessModelConfig()
    cfg.validate()
    demand = normalize_process_demand(demand_df)

    rows = []
    for _, row in demand.iterrows():
        metrics = estimate_process_day(row["TOTAL_PALLETS"], cfg)
        rows.append({"DATE": row["DATE"], "TOTAL_PALLETS": row["TOTAL_PALLETS"], **metrics})

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Rolling WIP is the amount of proposed temporary boxes still waiting to
    # be completed/consumed. Dwell time is configurable and rounded up.
    dwell_days = max(int(math.ceil(cfg.proposed_wip_dwell_days)), 1)
    df["PROPOSED_WIP_BOXES"] = (
        df["PROPOSED_INITIAL_WIP_BOXES"]
        .rolling(window=dwell_days, min_periods=1)
        .sum()
        .clip(upper=cfg.max_temporary_wip_boxes)
    )

    df["CURRENT_BOX_STORAGE_M2"] = df["CURRENT_PROCESS_BOXES"] * cfg.box_footprint_m2
    df["PROPOSED_WIP_STORAGE_M2"] = df["PROPOSED_WIP_BOXES"] * cfg.box_footprint_m2
    df["NET_STORAGE_CHANGE_M2"] = df["PROPOSED_WIP_STORAGE_M2"] - df["CURRENT_BOX_STORAGE_M2"]
    df["NET_STORAGE_CHANGE_%"] = np.where(
        df["CURRENT_BOX_STORAGE_M2"] > 0,
        df["NET_STORAGE_CHANGE_M2"] / df["CURRENT_BOX_STORAGE_M2"] * 100,
        np.nan,
    )
    df["TIME_SAVED_SEC"] = df["CURRENT_TIME_SEC"] - df["PROPOSED_TIME_SEC"]
    df["TIME_REDUCTION_%"] = np.where(
        df["CURRENT_TIME_SEC"] > 0,
        df["TIME_SAVED_SEC"] / df["CURRENT_TIME_SEC"] * 100,
        np.nan,
    )
    df["TOUCH_REDUCTION_%"] = np.where(
        df["CURRENT_TOUCHES"] > 0,
        (df["CURRENT_TOUCHES"] - df["PROPOSED_TOUCHES"]) / df["CURRENT_TOUCHES"] * 100,
        np.nan,
    )
    return df


def process_summary(df: pd.DataFrame):
    """Return the key Model 2 decision metrics."""
    if df.empty:
        return {}
    return {
        "average_current_time_min": float(df["CURRENT_TIME_SEC"].mean() / 60),
        "average_proposed_time_min": float(df["PROPOSED_TIME_SEC"].mean() / 60),
        "time_reduction_pct": float(df["TIME_REDUCTION_%"].mean()),
        "average_current_touches": float(df["CURRENT_TOUCHES"].mean()),
        "average_proposed_touches": float(df["PROPOSED_TOUCHES"].mean()),
        "touch_reduction_pct": float(df["TOUCH_REDUCTION_%"].mean()),
        "average_current_error_risk_pct": float(df["CURRENT_ERROR_RISK_%"].mean()),
        "average_proposed_error_risk_pct": float(df["PROPOSED_ERROR_RISK_%"].mean()),
        "peak_proposed_wip_boxes": float(df["PROPOSED_WIP_BOXES"].max()),
        "average_proposed_wip_boxes": float(df["PROPOSED_WIP_BOXES"].mean()),
        "average_current_storage_m2": float(df["CURRENT_BOX_STORAGE_M2"].mean()),
        "average_proposed_wip_storage_m2": float(df["PROPOSED_WIP_STORAGE_M2"].mean()),
        "average_net_storage_change_m2": float(df["NET_STORAGE_CHANGE_M2"].mean()),
        "average_net_storage_change_pct": float(df["NET_STORAGE_CHANGE_%"].replace([np.inf, -np.inf], np.nan).dropna().mean()),
        "peak_net_storage_change_m2": float(df["NET_STORAGE_CHANGE_M2"].max()),
    }


def storage_paradox_table(summary):
    """Make the storage paradox explicit: process efficiency can increase WIP space."""
    return pd.DataFrame([
        {"METRIC": "Average current box storage", "VALUE": summary["average_current_storage_m2"], "UNIT": "m²"},
        {"METRIC": "Average proposed temporary WIP storage", "VALUE": summary["average_proposed_wip_storage_m2"], "UNIT": "m²"},
        {"METRIC": "Average net storage change", "VALUE": summary["average_net_storage_change_m2"], "UNIT": "m²"},
        {"METRIC": "Average net storage change", "VALUE": summary["average_net_storage_change_pct"], "UNIT": "%"},
        {"METRIC": "Peak additional temporary storage", "VALUE": summary["peak_net_storage_change_m2"], "UNIT": "m²"},
        {"METRIC": "Average process time reduction", "VALUE": summary["time_reduction_pct"], "UNIT": "%"},
        {"METRIC": "Average touch reduction", "VALUE": summary["touch_reduction_pct"], "UNIT": "%"},
        {"METRIC": "Average error-risk change", "VALUE": summary["average_proposed_error_risk_pct"] - summary["average_current_error_risk_pct"], "UNIT": "percentage points"},
    ])
