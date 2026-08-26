"""Model 2 — MTO Process Change Digital Twin.

The model compares the current MTO packing process with a proposed
single-SKU stick-pack / temporary-staging process. It is deliberately
scenario-based: inputs are editable and the outputs are decision-support
metrics, not direct observations.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ProcessModelConfig:
    mto_share_pct: float = 30.0
    standard_box_qty: float = 10.0
    average_belts_per_mto_order: float = 6.0
    partial_box_rate_pct: float = 50.0
    current_touches_per_box: float = 4.0
    proposed_touches_per_box: float = 2.0
    current_pack_time_sec: float = 40.0
    proposed_pack_time_sec: float = 25.0
    current_count_time_sec: float = 30.0
    proposed_count_time_sec: float = 20.0
    current_wip_store_time_sec: float = 60.0
    proposed_wip_store_time_sec: float = 35.0
    current_wip_retrieve_time_sec: float = 90.0
    proposed_wip_retrieve_time_sec: float = 45.0
    current_wip_dwell_days: float = 3.0
    proposed_wip_dwell_days: float = 2.0
    target_wip_reduction_pct: float = 65.0
    max_temporary_wip_boxes: float = 500.0
    error_probability_per_touch_pct: float = 1.5
    fatigue_points_per_touch: float = 1.0
    fatigue_points_per_wip_cycle: float = 2.5
    automation_coverage_pct: float = 0.0
    automation_time_reduction_pct: float = 20.0
    automation_touch_reduction_pct: float = 20.0
    automation_wip_bonus_pct: float = 5.0
    box_footprint_m2: float = 0.25
    boxes_per_pallet: float = 32.0
    # Baseline storage scenario is calibrated to the SIP presentation:
    # 1,913 current shared slots, ~82 slots released at 65% WIP reduction,
    # +2-slot density/staging penalty => ~1,833 proposed / -80 net slots.
    current_shared_storage_slots: float = 1913.0
    current_temporary_wip_slots: float = 126.15
    density_staging_penalty_slots: float = 2.0

    def validate(self):
        pct_fields = (
            "mto_share_pct", "partial_box_rate_pct", "target_wip_reduction_pct",
            "error_probability_per_touch_pct", "automation_coverage_pct",
            "automation_time_reduction_pct", "automation_touch_reduction_pct",
            "automation_wip_bonus_pct",
        )
        for name in pct_fields:
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100%.")
        positive_fields = (
            "standard_box_qty", "average_belts_per_mto_order", "current_touches_per_box",
            "proposed_touches_per_box", "current_pack_time_sec", "proposed_pack_time_sec",
            "current_count_time_sec", "proposed_count_time_sec", "current_wip_store_time_sec",
            "proposed_wip_store_time_sec", "current_wip_retrieve_time_sec",
            "proposed_wip_retrieve_time_sec", "box_footprint_m2", "boxes_per_pallet",
            "current_shared_storage_slots", "current_temporary_wip_slots", "max_temporary_wip_boxes",
        )
        for name in positive_fields:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero.")
        nonnegative_fields = (
            "current_wip_dwell_days", "proposed_wip_dwell_days",
            "fatigue_points_per_touch", "fatigue_points_per_wip_cycle", "density_staging_penalty_slots",
        )
        for name in nonnegative_fields:
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative.")


def normalize_process_demand(demand_df: pd.DataFrame):
    """Normalize daily demand; daily pallets are a process-volume proxy."""
    df = demand_df.copy()
    date_col = next((c for c in df.columns if str(c).upper() in {"DATE", "DATETIME", "DAY"}), None)
    qty_col = next((c for c in df.columns if str(c).upper() in {"TOTAL_PALLETS", "PALLETS", "DEMAND", "DAILY_DEMAND"}), None)
    if date_col is None or qty_col is None:
        raise ValueError("Model 2 demand needs DATE and TOTAL_PALLETS/PALLETS/DEMAND.")
    df = df.rename(columns={date_col: "DATE", qty_col: "TOTAL_PALLETS"})
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df["TOTAL_PALLETS"] = pd.to_numeric(df["TOTAL_PALLETS"], errors="coerce")
    df = df.dropna(subset=["DATE", "TOTAL_PALLETS"])
    return df.groupby("DATE", as_index=False)["TOTAL_PALLETS"].sum().sort_values("DATE")


def _automation_blend(base_value, automation_value, coverage_pct):
    coverage = coverage_pct / 100.0
    return base_value * (1.0 - coverage) + automation_value * coverage


def estimate_process_day(pallets, cfg: ProcessModelConfig):
    """Calculate current/proposed process metrics for one demand day."""
    mto_belts = max(float(pallets), 0.0) * cfg.mto_share_pct / 100.0 * cfg.average_belts_per_mto_order
    full_boxes = int(math.floor(mto_belts / cfg.standard_box_qty))
    remainder = mto_belts % cfg.standard_box_qty
    partial_boxes = 1 if remainder > 1e-9 else 0
    current_boxes = full_boxes + partial_boxes
    proposed_initial_wip_boxes = partial_boxes * cfg.partial_box_rate_pct / 100.0
    proposed_boxes = full_boxes + proposed_initial_wip_boxes

    proposed_pack_time = _automation_blend(cfg.proposed_pack_time_sec, cfg.proposed_pack_time_sec * (1 - cfg.automation_time_reduction_pct / 100.0), cfg.automation_coverage_pct)
    proposed_count_time = _automation_blend(cfg.proposed_count_time_sec, cfg.proposed_count_time_sec * (1 - cfg.automation_time_reduction_pct / 100.0), cfg.automation_coverage_pct)
    proposed_store_time = _automation_blend(cfg.proposed_wip_store_time_sec, cfg.proposed_wip_store_time_sec * (1 - cfg.automation_time_reduction_pct / 100.0), cfg.automation_coverage_pct)
    proposed_retrieve_time = _automation_blend(cfg.proposed_wip_retrieve_time_sec, cfg.proposed_wip_retrieve_time_sec * (1 - cfg.automation_time_reduction_pct / 100.0), cfg.automation_coverage_pct)
    proposed_touches_per_box = _automation_blend(cfg.proposed_touches_per_box, cfg.proposed_touches_per_box * (1 - cfg.automation_touch_reduction_pct / 100.0), cfg.automation_coverage_pct)

    current_time = current_boxes * (cfg.current_pack_time_sec + cfg.current_count_time_sec + cfg.current_wip_store_time_sec + cfg.current_wip_retrieve_time_sec)
    proposed_time = proposed_boxes * (proposed_pack_time + proposed_count_time + proposed_store_time + proposed_retrieve_time)
    current_touches = current_boxes * cfg.current_touches_per_box
    proposed_touches = proposed_boxes * proposed_touches_per_box

    p = cfg.error_probability_per_touch_pct / 100.0
    current_error_risk = 100.0 * (1.0 - (1.0 - p) ** max(current_touches, 0.0))
    proposed_error_risk = 100.0 * (1.0 - (1.0 - p) ** max(proposed_touches, 0.0))

    current_fatigue = current_touches * cfg.fatigue_points_per_touch + partial_boxes * cfg.fatigue_points_per_wip_cycle
    proposed_fatigue = proposed_touches * cfg.fatigue_points_per_touch + proposed_initial_wip_boxes * cfg.fatigue_points_per_wip_cycle

    return {
        "MTO_BELTS_PROXY": mto_belts,
        "FULL_BOXES": full_boxes,
        "PARTIAL_BOXES": partial_boxes,
        "CURRENT_PROCESS_BOXES": current_boxes,
        "PROPOSED_PROCESS_BOXES": proposed_boxes,
        "PROPOSED_INITIAL_WIP_BOXES": proposed_initial_wip_boxes,
        "CURRENT_TIME_SEC": current_time,
        "PROPOSED_TIME_SEC": proposed_time,
        "CURRENT_TOUCHES": current_touches,
        "PROPOSED_TOUCHES": proposed_touches,
        "CURRENT_FATIGUE_PROXY": current_fatigue,
        "PROPOSED_FATIGUE_PROXY": proposed_fatigue,
        "CURRENT_ERROR_RISK_%": current_error_risk,
        "PROPOSED_ERROR_RISK_%": proposed_error_risk,
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

    current_dwell = max(int(math.ceil(cfg.current_wip_dwell_days)), 1)
    proposed_dwell = max(int(math.ceil(cfg.proposed_wip_dwell_days)), 1)
    current_wip = df["PARTIAL_BOXES"].rolling(current_dwell, min_periods=1).sum()
    effective_wip_reduction = min(100.0, cfg.target_wip_reduction_pct + cfg.automation_wip_bonus_pct * (cfg.automation_coverage_pct / 100.0))
    proposed_wip = (current_wip.rolling(proposed_dwell, min_periods=1).mean() * (1.0 - effective_wip_reduction / 100.0)).clip(upper=cfg.max_temporary_wip_boxes)

    df["CURRENT_WIP_BOXES"] = current_wip
    df["PROPOSED_WIP_BOXES"] = proposed_wip
    df["WIP_REDUCTION_%"] = np.where(current_wip > 0, (current_wip - proposed_wip) / current_wip * 100.0, np.nan)
    df["CURRENT_WIP_STORAGE_M2"] = current_wip * cfg.box_footprint_m2
    df["PROPOSED_WIP_STORAGE_M2"] = proposed_wip * cfg.box_footprint_m2
    df["WIP_STORAGE_CHANGE_M2"] = df["PROPOSED_WIP_STORAGE_M2"] - df["CURRENT_WIP_STORAGE_M2"]
    df["TIME_SAVED_SEC"] = df["CURRENT_TIME_SEC"] - df["PROPOSED_TIME_SEC"]
    df["TIME_REDUCTION_%"] = np.where(df["CURRENT_TIME_SEC"] > 0, df["TIME_SAVED_SEC"] / df["CURRENT_TIME_SEC"] * 100.0, np.nan)
    df["TOUCH_REDUCTION_%"] = np.where(df["CURRENT_TOUCHES"] > 0, (df["CURRENT_TOUCHES"] - df["PROPOSED_TOUCHES"]) / df["CURRENT_TOUCHES"] * 100.0, np.nan)
    df["FATIGUE_REDUCTION_%"] = np.where(df["CURRENT_FATIGUE_PROXY"] > 0, (df["CURRENT_FATIGUE_PROXY"] - df["PROPOSED_FATIGUE_PROXY"]) / df["CURRENT_FATIGUE_PROXY"] * 100.0, np.nan)
    df["ERROR_PROXY_REDUCTION_%"] = np.where(df["CURRENT_ERROR_RISK_%"] > 0, (df["CURRENT_ERROR_RISK_%"] - df["PROPOSED_ERROR_RISK_%"]) / df["CURRENT_ERROR_RISK_%"] * 100.0, np.nan)

    wip_reduction_slots = cfg.current_temporary_wip_slots * effective_wip_reduction / 100.0
    proposed_shared_storage = cfg.current_shared_storage_slots - wip_reduction_slots + cfg.density_staging_penalty_slots
    net_storage_slots = proposed_shared_storage - cfg.current_shared_storage_slots
    df["CURRENT_SHARED_STORAGE_SLOTS"] = cfg.current_shared_storage_slots
    df["WIP_REDUCTION_EFFECT_SLOTS"] = wip_reduction_slots
    df["DENSITY_STAGING_PENALTY_SLOTS"] = cfg.density_staging_penalty_slots
    df["PROPOSED_SHARED_STORAGE_SLOTS"] = proposed_shared_storage
    df["NET_SHARED_STORAGE_CHANGE_SLOTS"] = net_storage_slots
    df["NET_SHARED_STORAGE_CHANGE_%"] = net_storage_slots / cfg.current_shared_storage_slots * 100.0
    return df


def process_summary(df: pd.DataFrame):
    """Return the key Model 2 decision metrics."""
    if df.empty:
        return {}
    def mean_col(name):
        return float(df[name].replace([np.inf, -np.inf], np.nan).dropna().mean())
    return {
        "average_current_time_min": mean_col("CURRENT_TIME_SEC") / 60.0,
        "average_proposed_time_min": mean_col("PROPOSED_TIME_SEC") / 60.0,
        "time_reduction_pct": mean_col("TIME_REDUCTION_%"),
        "average_current_touches": mean_col("CURRENT_TOUCHES"),
        "average_proposed_touches": mean_col("PROPOSED_TOUCHES"),
        "touch_reduction_pct": mean_col("TOUCH_REDUCTION_%"),
        "average_current_fatigue": mean_col("CURRENT_FATIGUE_PROXY"),
        "average_proposed_fatigue": mean_col("PROPOSED_FATIGUE_PROXY"),
        "fatigue_reduction_pct": mean_col("FATIGUE_REDUCTION_%"),
        "average_current_error_risk_pct": mean_col("CURRENT_ERROR_RISK_%"),
        "average_proposed_error_risk_pct": mean_col("PROPOSED_ERROR_RISK_%"),
        "error_proxy_reduction_pct": mean_col("ERROR_PROXY_REDUCTION_%"),
        "average_current_wip_boxes": mean_col("CURRENT_WIP_BOXES"),
        "average_proposed_wip_boxes": mean_col("PROPOSED_WIP_BOXES"),
        "peak_current_wip_boxes": float(df["CURRENT_WIP_BOXES"].max()),
        "peak_proposed_wip_boxes": float(df["PROPOSED_WIP_BOXES"].max()),
        "wip_reduction_pct": mean_col("WIP_REDUCTION_%"),
        "average_current_wip_storage_m2": mean_col("CURRENT_WIP_STORAGE_M2"),
        "average_proposed_wip_storage_m2": mean_col("PROPOSED_WIP_STORAGE_M2"),
        "average_wip_storage_change_m2": mean_col("WIP_STORAGE_CHANGE_M2"),
        "current_shared_storage_slots": float(df["CURRENT_SHARED_STORAGE_SLOTS"].iloc[0]),
        "wip_reduction_effect_slots": float(df["WIP_REDUCTION_EFFECT_SLOTS"].iloc[0]),
        "density_staging_penalty_slots": float(df["DENSITY_STAGING_PENALTY_SLOTS"].iloc[0]),
        "proposed_shared_storage_slots": float(df["PROPOSED_SHARED_STORAGE_SLOTS"].iloc[0]),
        "net_shared_storage_change_slots": float(df["NET_SHARED_STORAGE_CHANGE_SLOTS"].iloc[0]),
        "net_shared_storage_change_pct": float(df["NET_SHARED_STORAGE_CHANGE_%"].iloc[0]),
    }


def storage_paradox_table(summary):
    return pd.DataFrame([
        {"STORAGE EFFECT": "Current shared storage", "VALUE": summary["current_shared_storage_slots"], "UNIT": "slots", "INTERPRETATION": "Baseline shared storage"},
        {"STORAGE EFFECT": "WIP reduction effect", "VALUE": -summary["wip_reduction_effect_slots"], "UNIT": "slots", "INTERPRETATION": "Storage released by lower temporary WIP"},
        {"STORAGE EFFECT": "Density / staging penalty", "VALUE": summary["density_staging_penalty_slots"], "UNIT": "slots", "INTERPRETATION": "Space consumed by single-SKU staging / lower pooling efficiency"},
        {"STORAGE EFFECT": "Proposed shared storage", "VALUE": summary["proposed_shared_storage_slots"], "UNIT": "slots", "INTERPRETATION": "Baseline − WIP effect + penalty"},
        {"STORAGE EFFECT": "NET STORAGE CHANGE", "VALUE": summary["net_shared_storage_change_slots"], "UNIT": "slots", "INTERPRETATION": "Negative = net storage released; positive = net storage consumed"},
        {"STORAGE EFFECT": "NET STORAGE CHANGE", "VALUE": summary["net_shared_storage_change_pct"], "UNIT": "%", "INTERPRETATION": "Relative change from current shared storage"},
    ])


def process_kpi_table(summary):
    return pd.DataFrame([
        {"METRIC": "Average Processing Time", "UNIT": "minutes/order", "CURRENT": summary["average_current_time_min"], "PROPOSED": summary["average_proposed_time_min"], "IMPROVEMENT_%": summary["time_reduction_pct"]},
        {"METRIC": "Fatigue Proxy", "UNIT": "relative score", "CURRENT": summary["average_current_fatigue"], "PROPOSED": summary["average_proposed_fatigue"], "IMPROVEMENT_%": summary["fatigue_reduction_pct"]},
        {"METRIC": "Manual Touch Points", "UNIT": "touches/order", "CURRENT": summary["average_current_touches"], "PROPOSED": summary["average_proposed_touches"], "IMPROVEMENT_%": summary["touch_reduction_pct"]},
        {"METRIC": "Expected Error Proxy", "UNIT": "% risk proxy", "CURRENT": summary["average_current_error_risk_pct"], "PROPOSED": summary["average_proposed_error_risk_pct"], "IMPROVEMENT_%": summary["error_proxy_reduction_pct"]},
        {"METRIC": "Modelled Temporary WIP", "UNIT": "boxes", "CURRENT": summary["average_current_wip_boxes"], "PROPOSED": summary["average_proposed_wip_boxes"], "IMPROVEMENT_%": summary["wip_reduction_pct"]},
    ])


def scenario_sensitivity(demand_df: pd.DataFrame, base_config: ProcessModelConfig, parameter: str, values):
    """Run one-variable sensitivity analysis for management what-if testing."""
    rows = []
    for value in values:
        kwargs = dict(base_config.__dict__)
        kwargs[parameter] = float(value)
        cfg = ProcessModelConfig(**kwargs)
        result = simulate_process_model(demand_df, cfg)
        summary = process_summary(result)
        rows.append({
            "SCENARIO_VALUE": value,
            "TIME_REDUCTION_%": summary["time_reduction_pct"],
            "TOUCH_REDUCTION_%": summary["touch_reduction_pct"],
            "FATIGUE_REDUCTION_%": summary["fatigue_reduction_pct"],
            "ERROR_PROXY_REDUCTION_%": summary["error_proxy_reduction_pct"],
            "WIP_REDUCTION_%": summary["wip_reduction_pct"],
            "NET_STORAGE_CHANGE_SLOTS": summary["net_shared_storage_change_slots"],
            "NET_STORAGE_CHANGE_%": summary["net_shared_storage_change_pct"],
        })
    return pd.DataFrame(rows)
