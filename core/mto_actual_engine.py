"""Transaction-level MTO Process Twin engine."""
from dataclasses import dataclass, asdict
import math
import numpy as np
import pandas as pd


@dataclass
class ActualMTOConfig:
    # Box/data rules
    default_box_qty: float = 28.0
    conflict_box_qty: float = 28.0
    round_non_integer_belts: bool = True
    # Human-time assumptions
    pick_time_sec: float = 45.0
    count_time_sec: float = 30.0
    pack_time_sec: float = 40.0
    wip_store_time_sec: float = 60.0
    wip_retrieve_time_sec: float = 90.0
    # Touch/fatigue/error assumptions
    ordinary_touches_per_order: float = 3.0
    balance_extra_touches: float = 4.0
    fatigue_points_per_touch: float = 1.0
    fatigue_points_per_wip_cycle: float = 2.5
    error_probability_per_touch_pct: float = 1.5
    # Storage/shared constants
    wip_avg_dwell_days: float = 3.0
    material_cost_per_box_inr: float = 25.0
    boxes_per_pallet: float = 32.0
    pallet_footprint_sqft: float = 16.200625
    bin_capacity_belts: float = 300.0
    bins_per_pallet: float = 1.0
    mto_balance_bins_can_share: bool = True
    stick_pack_one_sku_per_box: bool = True
    staging_dwell_days: float = 2.0

    def validate(self):
        for name in (
            "default_box_qty", "conflict_box_qty", "pick_time_sec",
            "count_time_sec", "pack_time_sec", "wip_store_time_sec",
            "wip_retrieve_time_sec", "ordinary_touches_per_order",
            "balance_extra_touches", "fatigue_points_per_touch",
            "fatigue_points_per_wip_cycle", "wip_avg_dwell_days",
            "material_cost_per_box_inr", "boxes_per_pallet",
            "pallet_footprint_sqft", "bin_capacity_belts",
            "bins_per_pallet", "staging_dwell_days",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero.")
        if not 0 <= self.error_probability_per_touch_pct <= 100:
            raise ValueError("error_probability_per_touch_pct must be between 0 and 100.")


def _norm(value):
    return "".join(ch for ch in str(value).strip().upper() if ch.isalnum())


def _find_column(df, candidates):
    cols = {_norm(c): c for c in df.columns}
    for candidate in candidates:
        if _norm(candidate) in cols:
            return cols[_norm(candidate)]
    return None


def _read_best_sheet(source, required_groups):
    if isinstance(source, pd.DataFrame):
        return source.copy(), "DataFrame"
    if str(source).lower().endswith(".csv"):
        return pd.read_csv(source), "CSV"
    xl = pd.ExcelFile(source)
    for sheet in xl.sheet_names:
        for header in (0, 1, 2, 3):
            try:
                df = pd.read_excel(source, sheet_name=sheet, header=header)
            except Exception:
                continue
            if all(_find_column(df, group) is not None for group in required_groups):
                return df, sheet
    raise ValueError("No sheet matched the required columns.")


def load_mto_transactions(source):
    df, sheet = _read_best_sheet(source, [
        ["DATE", "DATETIME", "DAY"],
        ["ITEM_SIZE", "BELT SIZE", "SIZE"],
        ["NO_OF_BELTS", "BELTS", "QUANTITY"],
    ])
    date_col = _find_column(df, ["DATE", "DATETIME", "DAY"])
    size_col = _find_column(df, ["ITEM_SIZE", "BELT SIZE", "SIZE"])
    qty_col = _find_column(df, ["NO_OF_BELTS", "BELTS", "QUANTITY"])
    invoice_col = _find_column(df, ["INVOICE_ID", "BILL NUMBER", "INVOICE"])
    item_col = _find_column(df, ["ITEM_INDEX", "LINE_ITEM", "LINE_ITEM_SIZE_ID"])
    raw_box_col = _find_column(df, ["BOX_QTY_STD", "BOX QTY", "UNITS PER BOX", "STANDARD BOX QUANTITY"])
    out = pd.DataFrame({
        "DATE": pd.to_datetime(df[date_col], dayfirst=True, errors="coerce"),
        "ITEM_SIZE": df[size_col].astype(str).str.strip(),
        "NO_OF_BELTS_RAW": pd.to_numeric(df[qty_col], errors="coerce"),
        "INVOICE_ID": df[invoice_col].astype(str).str.strip() if invoice_col else "",
        "ITEM_INDEX": df[item_col].astype(str).str.strip() if item_col else "",
    })
    if raw_box_col:
        out["BOX_QTY_FROM_RAW"] = pd.to_numeric(df[raw_box_col], errors="coerce")
    out = out.replace({"ITEM_SIZE": {"nan": np.nan, "": np.nan}})
    out = out.dropna(subset=["DATE", "ITEM_SIZE", "NO_OF_BELTS_RAW"])
    out = out[out["NO_OF_BELTS_RAW"] > 0].copy()
    out["NON_INTEGER_QTY_FLAG"] = (out["NO_OF_BELTS_RAW"] % 1).abs() > 1e-9
    out = out.sort_values(["DATE", "ITEM_SIZE", "INVOICE_ID", "ITEM_INDEX"]).reset_index(drop=True)
    out.attrs["source_sheet"] = sheet
    return out


def build_box_master(source=None, default_box_qty=28.0, conflict_box_qty=28.0):
    if source is None:
        return pd.DataFrame(columns=["ITEM_SIZE", "BOX_QTY_USED", "BOX_SOURCE"])
    df, sheet = _read_best_sheet(source, [
        ["BELT SIZE", "ITEM_SIZE", "SIZE"],
        ["UNITS PER BOX", "STANDARD BOX QUANTITY", "BOX QTY", "BOX_QTY_STD"],
    ])
    size_col = _find_column(df, ["BELT SIZE", "ITEM_SIZE", "SIZE"])
    qty_col = _find_column(df, ["UNITS PER BOX", "STANDARD BOX QUANTITY", "BOX QTY", "BOX_QTY_STD"])
    b = pd.DataFrame({
        "ITEM_SIZE": df[size_col].astype(str).str.strip(),
        "UNITS_PER_BOX_RAW": pd.to_numeric(df[qty_col], errors="coerce"),
    }).replace({"ITEM_SIZE": {"nan": np.nan, "": np.nan}}).dropna(subset=["ITEM_SIZE"])
    rows = []
    for size, g in b.groupby("ITEM_SIZE"):
        values = sorted(set(float(v) for v in g["UNITS_PER_BOX_RAW"].dropna() if v > 0))
        zero = bool((g["UNITS_PER_BOX_RAW"] == 0).any())
        if len(values) == 1:
            qty, source_name = values[0], "MASTER"
        elif len(values) > 1:
            qty, source_name = float(conflict_box_qty), "DEFAULT_CONFLICT"
        else:
            qty = float(default_box_qty)
            source_name = "DEFAULT_ZERO" if zero else "DEFAULT_INVALID"
        rows.append({"ITEM_SIZE": size, "BOX_QTY_USED": qty, "BOX_SOURCE": source_name})
    result = pd.DataFrame(rows)
    result.attrs["source_sheet"] = sheet
    return result


def attach_box_standards(mto_df, box_master_df, default_box_qty=28.0):
    out = mto_df.copy()
    if "BOX_QTY_FROM_RAW" in out.columns:
        raw = pd.to_numeric(out["BOX_QTY_FROM_RAW"], errors="coerce")
        out["BOX_QTY_USED"] = raw.where(raw > 0)
        out["BOX_SOURCE"] = np.where(out["BOX_QTY_USED"].notna(), "RAW_COLUMN", None)
    else:
        out["BOX_QTY_USED"], out["BOX_SOURCE"] = np.nan, None
    if box_master_df is not None and not box_master_df.empty:
        master = box_master_df[["ITEM_SIZE", "BOX_QTY_USED", "BOX_SOURCE"]].rename(
            columns={"BOX_QTY_USED": "MASTER_BOX_QTY", "BOX_SOURCE": "MASTER_BOX_SOURCE"}
        )
        out = out.merge(master, on="ITEM_SIZE", how="left")
        use = out["BOX_QTY_USED"].isna() & out["MASTER_BOX_QTY"].notna()
        out.loc[use, "BOX_QTY_USED"] = out.loc[use, "MASTER_BOX_QTY"]
        out.loc[use, "BOX_SOURCE"] = out.loc[use, "MASTER_BOX_SOURCE"]
        out = out.drop(columns=["MASTER_BOX_QTY", "MASTER_BOX_SOURCE"])
    missing = out["BOX_QTY_USED"].isna()
    out.loc[missing, "BOX_QTY_USED"] = float(default_box_qty)
    out.loc[missing, "BOX_SOURCE"] = "DEFAULT_MISSING"
    out["BOX_QTY_USED"] = pd.to_numeric(out["BOX_QTY_USED"], errors="coerce")
    out["FULL_BOXES"] = np.floor(out["NO_OF_BELTS_USED"] / out["BOX_QTY_USED"]).astype(int)
    out["BALANCE_QTY"] = out["NO_OF_BELTS_USED"] - out["FULL_BOXES"] * out["BOX_QTY_USED"]
    out["HAS_BALANCE"] = out["BALANCE_QTY"] > 1e-9
    out["TOTAL_BOXES"] = out["FULL_BOXES"] + out["HAS_BALANCE"].astype(int)
    return out


def prepare_actual_mto(mto_source, box_source=None, config=None):
    cfg = config or ActualMTOConfig()
    cfg.validate()
    mto = load_mto_transactions(mto_source)
    mto["NO_OF_BELTS_USED"] = mto["NO_OF_BELTS_RAW"].round() if cfg.round_non_integer_belts else mto["NO_OF_BELTS_RAW"]
    master = build_box_master(box_source, cfg.default_box_qty, cfg.conflict_box_qty)
    return attach_box_standards(mto, master, cfg.default_box_qty), master


def simulate_actual_mto(detail, config=None):
    """Compare current shared-bin handling with proposed direct single-SKU packing."""
    cfg = config or ActualMTOConfig()
    cfg.validate()
    d = detail.copy()
    base = cfg.pick_time_sec + cfg.count_time_sec + cfg.pack_time_sec
    bal = d["HAS_BALANCE"].astype(int)
    d["TIME_CURRENT_SEC"] = d["TOTAL_BOXES"] * base + bal * (cfg.wip_store_time_sec + cfg.wip_retrieve_time_sec)
    d["TIME_PROPOSED_SEC"] = d["TOTAL_BOXES"] * base
    d["TOUCHES_CURRENT"] = cfg.ordinary_touches_per_order + cfg.balance_extra_touches * bal
    d["TOUCHES_PROPOSED"] = cfg.ordinary_touches_per_order
    d["FATIGUE_CURRENT"] = d["TOUCHES_CURRENT"] * cfg.fatigue_points_per_touch + bal * cfg.fatigue_points_per_wip_cycle
    d["FATIGUE_PROPOSED"] = d["TOUCHES_PROPOSED"] * cfg.fatigue_points_per_touch
    d["WIP_CURRENT_BELTS"] = d["BALANCE_QTY"]
    d["WIP_PROPOSED_BELTS"] = 0.0 if cfg.stick_pack_one_sku_per_box else d["BALANCE_QTY"]
    p = cfg.error_probability_per_touch_pct / 100.0
    d["EXPECTED_ERRORS_CURRENT"] = d["TOUCHES_CURRENT"] * p
    d["EXPECTED_ERRORS_PROPOSED"] = d["TOUCHES_PROPOSED"] * p
    daily = d.groupby(d["DATE"].dt.normalize(), sort=True).agg(
        ORDERS=("ITEM_SIZE", "size"), TOTAL_BELTS=("NO_OF_BELTS_USED", "sum"),
        TOTAL_BOXES=("TOTAL_BOXES", "sum"), ORDERS_WITH_BALANCE=("HAS_BALANCE", "sum"),
        TIME_CURRENT_SEC=("TIME_CURRENT_SEC", "sum"), TIME_PROPOSED_SEC=("TIME_PROPOSED_SEC", "sum"),
        TOUCHES_CURRENT=("TOUCHES_CURRENT", "sum"), TOUCHES_PROPOSED=("TOUCHES_PROPOSED", "sum"),
        FATIGUE_CURRENT=("FATIGUE_CURRENT", "sum"), FATIGUE_PROPOSED=("FATIGUE_PROPOSED", "sum"),
        WIP_CURRENT_BELTS=("WIP_CURRENT_BELTS", "sum"),
        EXPECTED_ERRORS_CURRENT=("EXPECTED_ERRORS_CURRENT", "sum"),
        EXPECTED_ERRORS_PROPOSED=("EXPECTED_ERRORS_PROPOSED", "sum"),
    ).reset_index(names="DATE")
    daily["TIME_SAVED_SEC"] = daily["TIME_CURRENT_SEC"] - daily["TIME_PROPOSED_SEC"]
    daily["TIME_SAVED_PCT"] = np.where(daily["TIME_CURRENT_SEC"] > 0, daily["TIME_SAVED_SEC"] / daily["TIME_CURRENT_SEC"] * 100, 0)
    daily["ERRORS_AVOIDED"] = daily["EXPECTED_ERRORS_CURRENT"] - daily["EXPECTED_ERRORS_PROPOSED"]
    start, end = d["DATE"].min(), d["DATE"].max()
    days = max((end - start).days, 1)
    s = {
        "orders": len(d), "belts": float(d["NO_OF_BELTS_USED"].sum()), "boxes": int(d["TOTAL_BOXES"].sum()),
        "balance_orders": int(d["HAS_BALANCE"].sum()), "balance_pct": float(d["HAS_BALANCE"].mean() * 100),
        "wip_belts": float(d["WIP_CURRENT_BELTS"].sum()), "time_current_sec": float(d["TIME_CURRENT_SEC"].sum()),
        "time_proposed_sec": float(d["TIME_PROPOSED_SEC"].sum()), "touches_current": float(d["TOUCHES_CURRENT"].sum()),
        "touches_proposed": float(d["TOUCHES_PROPOSED"].sum()), "fatigue_current": float(d["FATIGUE_CURRENT"].sum()),
        "fatigue_proposed": float(d["FATIGUE_PROPOSED"].sum()), "errors_current": float(d["EXPECTED_ERRORS_CURRENT"].sum()),
        "errors_proposed": float(d["EXPECTED_ERRORS_PROPOSED"].sum()), "calendar_days": days,
    }
    s["time_saved_pct"] = 100 * (s["time_current_sec"] - s["time_proposed_sec"]) / s["time_current_sec"] if s["time_current_sec"] else 0
    s["touch_reduction_pct"] = 100 * (s["touches_current"] - s["touches_proposed"]) / s["touches_current"] if s["touches_current"] else 0
    s["fatigue_reduction_pct"] = 100 * (s["fatigue_current"] - s["fatigue_proposed"]) / s["fatigue_current"] if s["fatigue_current"] else 0
    s["error_reduction_pct"] = 100 * (s["errors_current"] - s["errors_proposed"]) / s["errors_current"] if s["errors_current"] else 0
    s["avg_wip_on_hand_belts"] = s["wip_belts"] / s["calendar_days"] * cfg.wip_avg_dwell_days
    for source_name, key in [("MASTER", "master_standard_rows"), ("RAW_COLUMN", "raw_standard_rows"), ("DEFAULT_ZERO", "default_zero_rows"), ("DEFAULT_MISSING", "default_missing_rows"), ("DEFAULT_CONFLICT", "default_conflict_rows")]:
        s[key] = int((d["BOX_SOURCE"] == source_name).sum())
    return {"detail": d, "daily": daily, "summary": s, "config": asdict(cfg)}


def build_actual_kpi_table(result, config=None):
    s, cfg = result["summary"], (config or ActualMTOConfig())
    b = s["boxes"]
    rows = [
        ("Average Processing Time per Order (sec)", s["time_current_sec"]/s["orders"], s["time_proposed_sec"]/s["orders"]),
        ("Total Handling Time (hrs)", s["time_current_sec"]/3600, s["time_proposed_sec"]/3600),
        ("Total Manual Touch Points", s["touches_current"], s["touches_proposed"]),
        ("Operator Fatigue Score (total)", s["fatigue_current"], s["fatigue_proposed"]),
        ("Orders Generating Temporary WIP", s["balance_orders"], 0),
        ("Total WIP Quantity Generated (belts, cumulative)", s["wip_belts"], 0),
        ("Avg WIP On Hand (Little's Law)", s["avg_wip_on_hand_belts"], 0),
        ("Total Boxes Required (MTO)", b, b),
        ("Packaging Material Cost (INR)", b*cfg.material_cost_per_box_inr, b*cfg.material_cost_per_box_inr),
        ("Expected Picking/Counting Errors (count)", s["errors_current"], s["errors_proposed"]),
    ]
    out = pd.DataFrame(rows, columns=["KPI", "Current Process", "Proposed Process"])
    out["Improvement"] = out["Current Process"] - out["Proposed Process"]
    out["Improvement %"] = np.where(out["Current Process"] != 0, out["Improvement"]/out["Current Process"]*100, 0)
    return out


def build_storage_paradox_actual(result, mta_belts=0.0, config=None):
    cfg, s = (config or ActualMTOConfig()), result["summary"]
    full = int(result["detail"]["FULL_BOXES"].sum())
    balance_boxes = int(s["balance_orders"]) if cfg.stick_pack_one_sku_per_box else 0
    balance_bins = math.ceil(s["wip_belts"]/cfg.bin_capacity_belts) if cfg.mto_balance_bins_can_share and s["wip_belts"] else balance_boxes
    current_balance_slots = math.ceil(balance_bins/cfg.bins_per_pallet) if balance_bins else 0
    current_mto = current_balance_slots + math.ceil(full/cfg.boxes_per_pallet)
    density = math.ceil((full+balance_boxes)/cfg.boxes_per_pallet)
    staging_boxes = balance_boxes/max(s["calendar_days"], 1)*cfg.staging_dwell_days
    staging = math.ceil(staging_boxes/cfg.boxes_per_pallet) if staging_boxes else 0
    proposed_mto = density + staging
    mta_slots = math.ceil(float(mta_belts)/cfg.bin_capacity_belts) if mta_belts > 0 else 0
    return pd.DataFrame([
        ["MTA belts (period)", float(mta_belts)], ["MTA pallet slots", mta_slots],
        ["MTO full boxes", full], ["MTO balance boxes", balance_boxes],
        ["Current MTO pallet slots", current_mto], ["Proposed MTO density slots", density],
        ["Proposed staging slots", staging], ["Proposed MTO pallet slots", proposed_mto],
        ["Combined current pallet slots", current_mto+mta_slots],
        ["Combined proposed pallet slots", proposed_mto+mta_slots],
        ["Net pallet-slot change", proposed_mto-current_mto],
        ["Combined current floor space (sq ft)", (current_mto+mta_slots)*cfg.pallet_footprint_sqft],
        ["Combined proposed floor space (sq ft)", (proposed_mto+mta_slots)*cfg.pallet_footprint_sqft],
    ], columns=["Metric", "Value"])
