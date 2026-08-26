"""Transaction-level MTO process model.

This module uses the actual MTO packing-list data instead of converting daily
pallet demand into a synthetic MTO quantity.  It applies the user's box-master
rule:

* positive Units Per Box -> use the master value
* zero -> do not use zero; use the configurable default (28 by default)
* no match -> use the configurable default (28 by default)
* conflicting positive standards -> flag the SKU and use the configurable
  conflict fallback (28 by default) rather than silently selecting a value

The proposed process is modelled at ITEM_SIZE level.  Partial quantities are
held temporarily and can complete a box when later quantities of the same SKU
arrive.  This is a decision-support model; time, touch, fatigue and error
parameters remain editable proxies rather than measured observations.
"""

from dataclasses import dataclass
import math
from typing import Dict, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ActualMTOConfig:
    default_box_qty: float = 28.0
    conflict_box_qty: float = 28.0
    round_non_integer_belts: bool = True

    current_touches_per_box: float = 4.0
    proposed_touches_per_box: float = 2.0
    current_pack_time_sec: float = 40.0
    proposed_pack_time_sec: float = 25.0
    current_count_time_sec: float = 30.0
    proposed_count_time_sec: float = 20.0
    current_store_time_sec: float = 60.0
    proposed_store_time_sec: float = 35.0
    current_retrieve_time_sec: float = 90.0
    proposed_retrieve_time_sec: float = 45.0

    error_probability_per_touch_pct: float = 1.5
    fatigue_points_per_touch: float = 1.0
    fatigue_points_per_wip_cycle: float = 2.5

    automation_coverage_pct: float = 0.0
    automation_time_reduction_pct: float = 20.0
    automation_touch_reduction_pct: float = 20.0

    box_footprint_m2: float = 0.25
    wip_location_multiplier: float = 1.0

    def validate(self):
        for name in (
            "default_box_qty", "conflict_box_qty", "current_touches_per_box",
            "proposed_touches_per_box", "current_pack_time_sec",
            "proposed_pack_time_sec", "current_count_time_sec",
            "proposed_count_time_sec", "current_store_time_sec",
            "proposed_store_time_sec", "current_retrieve_time_sec",
            "proposed_retrieve_time_sec", "box_footprint_m2",
            "wip_location_multiplier",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero.")
        for name in (
            "error_probability_per_touch_pct", "automation_coverage_pct",
            "automation_time_reduction_pct", "automation_touch_reduction_pct",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100%.")
        for name in ("fatigue_points_per_touch", "fatigue_points_per_wip_cycle"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative.")


def _find_column(df, candidates):
    lookup = {str(c).strip().upper(): c for c in df.columns}
    for candidate in candidates:
        if candidate.upper() in lookup:
            return lookup[candidate.upper()]
    return None


def load_mto_transactions(source, sheet_name="Consolidated_Packing_Lists"):
    """Load and normalize the actual MTO packing-list data."""
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        df = pd.read_excel(source, sheet_name=sheet_name)

    date_col = _find_column(df, ["DATE", "DATETIME", "DAY"])
    size_col = _find_column(df, ["ITEM_SIZE", "BELT SIZE", "SIZE"])
    qty_col = _find_column(df, ["NO_OF_BELTS", "BELTS", "QUANTITY"])
    invoice_col = _find_column(df, ["INVOICE_ID", "BILL NUMBER", "INVOICE"])
    item_col = _find_column(df, ["ITEM_INDEX", "LINE_ITEM", "LINE_ITEM_SIZE_ID"])

    missing = [name for name, col in (("DATE", date_col), ("ITEM_SIZE", size_col), ("NO_OF_BELTS", qty_col)) if col is None]
    if missing:
        raise ValueError(f"MTO packing-list data missing required columns: {missing}")

    out = pd.DataFrame({
        "DATE": pd.to_datetime(df[date_col], dayfirst=True, errors="coerce"),
        "ITEM_SIZE": df[size_col].astype(str).str.strip(),
        "NO_OF_BELTS_RAW": pd.to_numeric(df[qty_col], errors="coerce"),
        "INVOICE_ID": df[invoice_col].astype(str).str.strip() if invoice_col else "",
        "ITEM_INDEX": df[item_col].astype(str).str.strip() if item_col else "",
    })
    out = out.replace({"ITEM_SIZE": {"nan": np.nan, "": np.nan}})
    out = out.dropna(subset=["DATE", "ITEM_SIZE", "NO_OF_BELTS_RAW"])
    out = out[out["NO_OF_BELTS_RAW"] > 0].copy()
    out["NON_INTEGER_QTY_FLAG"] = (out["NO_OF_BELTS_RAW"] % 1).abs() > 1e-9
    return out.sort_values(["DATE", "ITEM_SIZE", "INVOICE_ID", "ITEM_INDEX"]).reset_index(drop=True)


def build_box_master(source, sheet_name="Size Master Reference", default_box_qty=28.0, conflict_box_qty=28.0):
    """Create a clean box-standard lookup and a data-quality table."""
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        df = pd.read_excel(source, sheet_name=sheet_name)

    size_col = _find_column(df, ["Belt Size", "ITEM_SIZE", "SIZE"])
    qty_col = _find_column(df, ["Units Per Box", "STANDARD BOX QUANTITY", "BOX QTY"])
    if size_col is None or qty_col is None:
        raise ValueError("Size master must contain Belt Size and Units Per Box columns.")

    b = pd.DataFrame({
        "ITEM_SIZE": df[size_col].astype(str).str.strip(),
        "UNITS_PER_BOX_RAW": pd.to_numeric(df[qty_col], errors="coerce"),
    })
    b = b.replace({"ITEM_SIZE": {"nan": np.nan, "": np.nan}}).dropna(subset=["ITEM_SIZE"])

    rows = []
    for size, g in b.groupby("ITEM_SIZE"):
        values = sorted(set(float(v) for v in g["UNITS_PER_BOX_RAW"].dropna() if v > 0))
        has_zero = bool((g["UNITS_PER_BOX_RAW"] == 0).any())
        if len(values) == 1:
            qty = values[0]
            source_name = "MASTER"
        elif len(values) > 1:
            qty = float(conflict_box_qty)
            source_name = "DEFAULT_CONFLICT"
        else:
            qty = float(default_box_qty)
            source_name = "DEFAULT_ZERO" if has_zero else "DEFAULT_INVALID"
        rows.append({
            "ITEM_SIZE": size,
            "BOX_QTY_USED": qty,
            "BOX_SOURCE": source_name,
            "MASTER_POSITIVE_VALUES": ", ".join(str(v).rstrip("0").rstrip(".") for v in values),
            "MASTER_HAS_ZERO": has_zero,
            "MASTER_CONFLICT": len(values) > 1,
        })
    return pd.DataFrame(rows)


def attach_box_standards(mto_df, box_master_df, default_box_qty=28.0):
    """Apply master values and the 28-box default rule to every MTO line."""
    out = mto_df.copy()
    master = box_master_df.copy()
    out = out.merge(master, on="ITEM_SIZE", how="left")
    missing = out["BOX_QTY_USED"].isna()
    out.loc[missing, "BOX_QTY_USED"] = float(default_box_qty)
    out.loc[missing, "BOX_SOURCE"] = "DEFAULT_MISSING"
    out["BOX_QTY_USED"] = pd.to_numeric(out["BOX_QTY_USED"], errors="coerce")
    out["FULL_BOXES"] = np.floor(out["NO_OF_BELTS_USED"] / out["BOX_QTY_USED"]).astype(int)
    out["PARTIAL_REMAINDER_BELTS"] = out["NO_OF_BELTS_USED"] - out["FULL_BOXES"] * out["BOX_QTY_USED"]
    out["PARTIAL_BOX_FLAG"] = out["PARTIAL_REMAINDER_BELTS"] > 1e-9
    out["CURRENT_BOXES"] = out["FULL_BOXES"] + out["PARTIAL_BOX_FLAG"].astype(int)
    return out


def prepare_actual_mto(mto_source, box_source, config=None):
    cfg = config or ActualMTOConfig()
    cfg.validate()
    mto = load_mto_transactions(mto_source)
    if cfg.round_non_integer_belts:
        mto["NO_OF_BELTS_USED"] = mto["NO_OF_BELTS_RAW"].round()
    else:
        mto["NO_OF_BELTS_USED"] = mto["NO_OF_BELTS_RAW"]
    master = build_box_master(box_source, default_box_qty=cfg.default_box_qty, conflict_box_qty=cfg.conflict_box_qty)
    detail = attach_box_standards(mto, master, default_box_qty=cfg.default_box_qty)
    return detail, master


def _blend(base, reduction_pct, coverage_pct):
    return base * (1.0 - (coverage_pct / 100.0) * (reduction_pct / 100.0))


def _error_risk(touches, p):
    return 100.0 * (1.0 - (1.0 - p) ** max(float(touches), 0.0))


def simulate_actual_mto(mto_detail, config=None):
    """Simulate current/proposed process and actual SKU-level temporary WIP."""
    cfg = config or ActualMTOConfig()
    cfg.validate()
    d = mto_detail.copy().sort_values(["DATE", "ITEM_SIZE", "INVOICE_ID", "ITEM_INDEX"]).reset_index(drop=True)

    current_pack = cfg.current_pack_time_sec
    current_count = cfg.current_count_time_sec
    proposed_pack = _blend(cfg.proposed_pack_time_sec, cfg.automation_time_reduction_pct, cfg.automation_coverage_pct)
    proposed_count = _blend(cfg.proposed_count_time_sec, cfg.automation_time_reduction_pct, cfg.automation_coverage_pct)
    proposed_store = _blend(cfg.proposed_store_time_sec, cfg.automation_time_reduction_pct, cfg.automation_coverage_pct)
    proposed_retrieve = _blend(cfg.proposed_retrieve_time_sec, cfg.automation_time_reduction_pct, cfg.automation_coverage_pct)
    proposed_touches = _blend(cfg.proposed_touches_per_box, cfg.automation_touch_reduction_pct, cfg.automation_coverage_pct)

    # Current process: each transaction is handled as its own box requirement.
    d["CURRENT_PROCESS_TIME_SEC"] = d["CURRENT_BOXES"] * (current_pack + current_count)
    d["CURRENT_TOUCHES"] = d["CURRENT_BOXES"] * cfg.current_touches_per_box
    d["CURRENT_ERROR_RISK_%"] = d["CURRENT_TOUCHES"].apply(lambda x: _error_risk(x, cfg.error_probability_per_touch_pct / 100.0))
    d["CURRENT_FATIGUE_PROXY"] = d["CURRENT_TOUCHES"] * cfg.fatigue_points_per_touch

    # Proposed process: every transaction's full boxes can proceed; each partial
    # quantity is packed as one SKU-specific temporary box. Later quantities of
    # the same ITEM_SIZE can complete the box.
    d["PROPOSED_INITIAL_PARTIAL_BOX"] = d["PARTIAL_BOX_FLAG"].astype(int)
    d["PROPOSED_PROCESS_TIME_SEC"] = d["FULL_BOXES"] * (proposed_pack + proposed_count)
    d["PROPOSED_TOUCHES"] = d["FULL_BOXES"] * proposed_touches

    # SKU-level inventory balance. Closing remainder is the actual temporary WIP.
    balances: Dict[str, float] = {}
    daily_rows = []
    for date, day in d.groupby("DATE", sort=True):
        opening_wip_boxes = sum(1 for v in balances.values() if v > 1e-9)
        opening_wip_belts = sum(max(v, 0.0) for v in balances.values())
        new_partial_boxes = 0
        boxes_completed_from_wip = 0
        wip_store_events = 0
        wip_retrieve_events = 0
        closing_wip_belts = 0.0

        for sku, sg in day.groupby("ITEM_SIZE", sort=False):
            box_qty = float(sg["BOX_QTY_USED"].iloc[0])
            previous = balances.get(sku, 0.0)
            incoming = float(sg["NO_OF_BELTS_USED"].sum())
            available = previous + incoming
            completed = int(math.floor(available / box_qty))
            remainder = available - completed * box_qty
            if previous > 1e-9:
                wip_retrieve_events += 1
            if remainder > 1e-9:
                wip_store_events += 1
                new_partial_boxes += int((sg["PARTIAL_BOX_FLAG"]).any())
            if previous > 1e-9 and completed > 0:
                boxes_completed_from_wip += completed
            balances[sku] = remainder

        closing_wip_boxes = sum(1 for v in balances.values() if v > 1e-9)
        closing_wip_belts = sum(max(v, 0.0) for v in balances.values())
        proposed_completed_full_boxes = int(day["FULL_BOXES"].sum()) + boxes_completed_from_wip
        proposed_time = float(day["PROPOSED_PROCESS_TIME_SEC"].sum())
        proposed_touches_day = float(day["PROPOSED_TOUCHES"].sum())
        # Completion of a box from previous WIP requires retrieval/rehandling;
        # storage of a new partial box requires one store event.
        proposed_time += wip_store_events * proposed_store + wip_retrieve_events * proposed_retrieve
        proposed_touches_day += (wip_store_events + wip_retrieve_events) * proposed_touches
        proposed_error = _error_risk(proposed_touches_day, cfg.error_probability_per_touch_pct / 100.0)
        proposed_fatigue = proposed_touches_day * cfg.fatigue_points_per_touch + (wip_store_events + wip_retrieve_events) * cfg.fatigue_points_per_wip_cycle

        current_time = float(day["CURRENT_PROCESS_TIME_SEC"].sum())
        current_touches = float(day["CURRENT_TOUCHES"].sum())
        current_fatigue = float(day["CURRENT_FATIGUE_PROXY"].sum())
        current_error = _error_risk(current_touches, cfg.error_probability_per_touch_pct / 100.0)

        daily_rows.append({
            "DATE": date,
            "TRANSACTIONS": len(day),
            "BELTS": float(day["NO_OF_BELTS_USED"].sum()),
            "FULL_BOXES": int(day["FULL_BOXES"].sum()),
            "PARTIAL_BOXES": int(day["PARTIAL_BOX_FLAG"].sum()),
            "CURRENT_BOXES": int(day["CURRENT_BOXES"].sum()),
            "PROPOSED_FULL_BOXES": int(day["FULL_BOXES"].sum()),
            "OPENING_WIP_BOXES": opening_wip_boxes,
            "OPENING_WIP_BELTS": opening_wip_belts,
            "NEW_PARTIAL_WIP_BOXES": new_partial_boxes,
            "WIP_RETRIEVE_EVENTS": wip_retrieve_events,
            "WIP_STORE_EVENTS": wip_store_events,
            "BOXES_COMPLETED_FROM_WIP": boxes_completed_from_wip,
            "CLOSING_WIP_BOXES": closing_wip_boxes,
            "CLOSING_WIP_BELTS": closing_wip_belts,
            "CURRENT_TIME_SEC": current_time,
            "PROPOSED_TIME_SEC": proposed_time,
            "CURRENT_TOUCHES": current_touches,
            "PROPOSED_TOUCHES": proposed_touches_day,
            "CURRENT_FATIGUE_PROXY": current_fatigue,
            "PROPOSED_FATIGUE_PROXY": proposed_fatigue,
            "CURRENT_ERROR_RISK_%": current_error,
            "PROPOSED_ERROR_RISK_%": proposed_error,
            "CURRENT_WIP_STORAGE_M2": 0.0,
            "PROPOSED_WIP_STORAGE_M2": closing_wip_boxes * cfg.box_footprint_m2 * cfg.wip_location_multiplier,
        })

    daily = pd.DataFrame(daily_rows)
    if daily.empty:
        return daily, pd.DataFrame(), {}

    daily["TIME_REDUCTION_%"] = np.where(daily["CURRENT_TIME_SEC"] > 0, (daily["CURRENT_TIME_SEC"] - daily["PROPOSED_TIME_SEC"]) / daily["CURRENT_TIME_SEC"] * 100, np.nan)
    daily["TOUCH_REDUCTION_%"] = np.where(daily["CURRENT_TOUCHES"] > 0, (daily["CURRENT_TOUCHES"] - daily["PROPOSED_TOUCHES"]) / daily["CURRENT_TOUCHES"] * 100, np.nan)
    daily["FATIGUE_REDUCTION_%"] = np.where(daily["CURRENT_FATIGUE_PROXY"] > 0, (daily["CURRENT_FATIGUE_PROXY"] - daily["PROPOSED_FATIGUE_PROXY"]) / daily["CURRENT_FATIGUE_PROXY"] * 100, np.nan)
    daily["ERROR_RISK_REDUCTION_%"] = np.where(daily["CURRENT_ERROR_RISK_%"] > 0, (daily["CURRENT_ERROR_RISK_%"] - daily["PROPOSED_ERROR_RISK_%"]) / daily["CURRENT_ERROR_RISK_%"] * 100, np.nan)
    daily["WIP_STORAGE_CHANGE_M2"] = daily["PROPOSED_WIP_STORAGE_M2"] - daily["CURRENT_WIP_STORAGE_M2"]

    # Per-SKU closing balance at the end of the complete simulation horizon.
    final_rows = []
    for sku, balance in balances.items():
        if balance > 1e-9:
            last = d.loc[d["ITEM_SIZE"] == sku].iloc[-1]
            final_rows.append({
                "ITEM_SIZE": sku,
                "BOX_QTY_USED": float(last["BOX_QTY_USED"]),
                "CLOSING_WIP_BELTS": float(balance),
                "CLOSING_WIP_BOXES": 1,
                "WIP_FILL_%": float(balance / last["BOX_QTY_USED"] * 100.0),
            })
    wip_detail = pd.DataFrame(final_rows).sort_values("CLOSING_WIP_BELTS", ascending=False) if final_rows else pd.DataFrame(columns=["ITEM_SIZE", "BOX_QTY_USED", "CLOSING_WIP_BELTS", "CLOSING_WIP_BOXES", "WIP_FILL_%"])

    summary = {
        "transactions": int(len(d)),
        "total_belts": float(d["NO_OF_BELTS_USED"].sum()),
        "master_standard_rows": int((d["BOX_SOURCE"] == "MASTER").sum()),
        "default_missing_rows": int((d["BOX_SOURCE"] == "DEFAULT_MISSING").sum()),
        "default_zero_rows": int((d["BOX_SOURCE"] == "DEFAULT_ZERO").sum()),
        "default_conflict_rows": int((d["BOX_SOURCE"] == "DEFAULT_CONFLICT").sum()),
        "non_integer_rows": int(d["NON_INTEGER_QTY_FLAG"].sum()),
        "current_boxes": float(d["CURRENT_BOXES"].sum()),
        "full_boxes": float(d["FULL_BOXES"].sum()),
        "partial_boxes": float(d["PARTIAL_BOX_FLAG"].sum()),
        "average_current_time_min": float(daily["CURRENT_TIME_SEC"].mean() / 60.0),
        "average_proposed_time_min": float(daily["PROPOSED_TIME_SEC"].mean() / 60.0),
        "time_reduction_pct": float(daily["TIME_REDUCTION_%"].mean()),
        "average_current_touches": float(daily["CURRENT_TOUCHES"].mean()),
        "average_proposed_touches": float(daily["PROPOSED_TOUCHES"].mean()),
        "touch_reduction_pct": float(daily["TOUCH_REDUCTION_%"].mean()),
        "average_current_fatigue": float(daily["CURRENT_FATIGUE_PROXY"].mean()),
        "average_proposed_fatigue": float(daily["PROPOSED_FATIGUE_PROXY"].mean()),
        "fatigue_reduction_pct": float(daily["FATIGUE_REDUCTION_%"].mean()),
        "average_current_error_risk_pct": float(daily["CURRENT_ERROR_RISK_%"].mean()),
        "average_proposed_error_risk_pct": float(daily["PROPOSED_ERROR_RISK_%"].mean()),
        "error_risk_reduction_pct": float(daily["ERROR_RISK_REDUCTION_%"].mean()),
        "peak_closing_wip_boxes": float(daily["CLOSING_WIP_BOXES"].max()),
        "average_closing_wip_boxes": float(daily["CLOSING_WIP_BOXES"].mean()),
        "peak_closing_wip_belts": float(daily["CLOSING_WIP_BELTS"].max()),
        "average_closing_wip_storage_m2": float(daily["PROPOSED_WIP_STORAGE_M2"].mean()),
        "peak_closing_wip_storage_m2": float(daily["PROPOSED_WIP_STORAGE_M2"].max()),
        "final_open_wip_boxes": int(daily["CLOSING_WIP_BOXES"].iloc[-1]),
        "final_open_wip_belts": float(daily["CLOSING_WIP_BELTS"].iloc[-1]),
    }
    return daily, wip_detail, summary


def build_actual_kpi_table(summary):
    rows = [
        ("Average Processing Time", summary["average_current_time_min"], summary["average_proposed_time_min"], summary["time_reduction_pct"], "min/day"),
        ("Average Manual Touches", summary["average_current_touches"], summary["average_proposed_touches"], summary["touch_reduction_pct"], "touches/day"),
        ("Average Fatigue Proxy", summary["average_current_fatigue"], summary["average_proposed_fatigue"], summary["fatigue_reduction_pct"], "proxy points/day"),
        ("Average Error-Risk Proxy", summary["average_current_error_risk_pct"], summary["average_proposed_error_risk_pct"], summary["error_risk_reduction_pct"], "% risk"),
        ("Peak Temporary WIP", 0.0, summary["peak_closing_wip_boxes"], np.nan, "boxes"),
        ("Average Temporary WIP Storage", 0.0, summary["average_closing_wip_storage_m2"], np.nan, "m²"),
    ]
    return pd.DataFrame(rows, columns=["METRIC", "CURRENT", "PROPOSED", "IMPROVEMENT_%", "UNIT"])


def build_storage_paradox_actual(summary, box_footprint_m2):
    """Show the measured/modelled temporary storage requirement separately.

    No fabricated shared-storage baseline is inserted here. The net change is
    expressed as the proposed temporary WIP requirement minus the current
    temporary-WIP requirement, which is zero unless the user supplies a
    measured current WIP baseline.
    """
    proposed_peak_m2 = summary["peak_closing_wip_storage_m2"]
    proposed_avg_m2 = summary["average_closing_wip_storage_m2"]
    return pd.DataFrame([
        {"EFFECT": "Current temporary WIP baseline", "VALUE": 0.0, "UNIT": "m²", "NOTE": "Set to measured current temporary WIP if available."},
        {"EFFECT": "Proposed average temporary WIP", "VALUE": proposed_avg_m2, "UNIT": "m²", "NOTE": "SKU-level closing WIP × box footprint."},
        {"EFFECT": "Proposed peak temporary WIP", "VALUE": proposed_peak_m2, "UNIT": "m²", "NOTE": "Worst daily closing WIP requirement."},
        {"EFFECT": "Average net temporary-storage change", "VALUE": proposed_avg_m2, "UNIT": "m²", "NOTE": "Positive means additional temporary storage is required versus zero baseline."},
        {"EFFECT": "Peak net temporary-storage change", "VALUE": proposed_peak_m2, "UNIT": "m²", "NOTE": "Use this for capacity/staging planning."},
    ])
