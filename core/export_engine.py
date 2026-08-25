"""Export and reporting engine for the Universal Warehouse Digital Twin."""

from pathlib import Path
import json

import pandas as pd


def build_kpi_table(capacity_summary, demand_metrics, simulation_summary, layout_metrics=None):
    """Create one dashboard-friendly KPI table from the independent engines."""
    rows = [
        ("Theoretical Capacity", capacity_summary.get("theoretical_capacity"), "pallet slots"),
        ("Realistic Operational Capacity", capacity_summary.get("realistic_capacity"), "pallet slots"),
        ("Planning Occupancy", capacity_summary.get("planning_occupancy_pct"), "%"),
        ("Planning Capacity", capacity_summary.get("planning_capacity"), "pallet slots"),
        ("Average Daily Demand", demand_metrics.get("average_daily_demand"), "pallets/day"),
        ("Peak Daily Demand", demand_metrics.get("peak_demand"), "pallets/day"),
        ("95th Percentile Demand", demand_metrics.get("p95_demand"), "pallets/day"),
        ("Average Storage Usage", simulation_summary.get("average_storage_usage_pct", None), "%"),
        ("Average Distance", simulation_summary.get("average_distance_m"), "m/day"),
        ("Average Time", simulation_summary.get("average_time_min"), "min/day"),
        ("Average Fatigue Proxy", simulation_summary.get("average_fatigue"), "score"),
        ("Overflow Days", simulation_summary.get("overflow_days"), "days"),
    ]
    if layout_metrics:
        rows.extend([
            ("Average Slot Distance", layout_metrics.get("avg_distance_m"), "m"),
            ("Generated Slot Count", layout_metrics.get("capacity"), "slots"),
            ("Main Aisle", layout_metrics.get("main_aisle_m"), "m"),
            ("Cross Aisle", layout_metrics.get("cross_aisle_m"), "m"),
            ("Wall Clearance", layout_metrics.get("wall_clearance_m"), "m"),
            ("Pallet Orientation", layout_metrics.get("orientation"), "degrees"),
        ])
    return pd.DataFrame(rows, columns=["METRIC", "VALUE", "UNIT"])


def export_excel(
    output_path,
    kpi_table,
    capacity_table=None,
    daily_simulation=None,
    occupancy=None,
    slot_master=None,
    layout_search=None,
    assumptions=None,
    demand_metrics=None,
):
    """Export all decision-support tables to one workbook."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        kpi_table.to_excel(writer, sheet_name="KPI_Dashboard", index=False)
        if capacity_table is not None:
            capacity_table.to_excel(writer, sheet_name="Capacity", index=False)
        if daily_simulation is not None:
            daily_simulation.to_excel(writer, sheet_name="Daily_Simulation", index=False)
        if occupancy is not None:
            occupancy.to_excel(writer, sheet_name="Slot_Occupancy", index=False)
        if slot_master is not None:
            slot_master.to_excel(writer, sheet_name="Slot_Master", index=False)
        if layout_search is not None:
            layout_search.to_excel(writer, sheet_name="Layout_Search", index=False)
        if demand_metrics is not None:
            pd.DataFrame([demand_metrics]).to_excel(writer, sheet_name="Demand_Metrics", index=False)
        if assumptions is not None:
            pd.DataFrame(
                [(str(k), str(v)) for k, v in assumptions.items()],
                columns=["ASSUMPTION", "VALUE"],
            ).to_excel(writer, sheet_name="Assumptions", index=False)

    return output_path


def export_summary_json(output_path, summary):
    """Export a machine-readable summary for integrations or future APIs."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    return output_path
