"""End-to-end orchestration for the Universal Warehouse Digital Twin."""

from pathlib import Path

import pandas as pd
from shapely.geometry import Point

from .cad_engine import read_dxf, detect_warehouse, detect_doors, detect_obstacles
from .layout_engine import LayoutConfig, theoretical_capacity, optimize_realistic_layout, slot_master
from .capacity_engine import CapacityConfig, capacity_summary, capacity_table, demand_metrics
from .simulation_engine import SimulationConfig, normalize_demand, simulate_daily, simulation_summary


def load_demand_file(path):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    raise ValueError("Demand file must be CSV or Excel.")


def prepare_cad(path):
    raw = read_dxf(path)
    warehouse, warehouse_layer = detect_warehouse(raw)
    raw["warehouse"] = warehouse
    raw["warehouse_layer"] = warehouse_layer
    raw["doors"] = detect_doors(raw)
    raw["obstacles"] = detect_obstacles(raw, warehouse)
    return raw


def run_pipeline(
    cad_path,
    demand_path,
    door_index=0,
    manual_door_xy=None,
    pallet_width_m=1.20,
    pallet_depth_m=1.00,
    wall_clearance_m=0.25,
    main_aisle_m=4.00,
    cross_aisle_m=2.00,
    turning_diameter_m=7.00,
    turning_enabled=True,
    turning_center_xy=None,
    target_occupancy_pct=65.0,
    forklift_speed_mps=2.5,
    handling_time_sec_per_pallet=0.0,
):
    """Run the complete deterministic warehouse model."""
    cad = prepare_cad(cad_path)
    doors = cad.get("doors", [])

    if manual_door_xy is not None:
        door = Point(float(manual_door_xy[0]), float(manual_door_xy[1]))
        selected_door = {"x": door.x, "y": door.y, "layer": "MANUAL"}
    else:
        if not doors:
            raise ValueError("No CAD door was detected. Provide manual door X/Y coordinates.")
        if not 0 <= door_index < len(doors):
            raise ValueError(f"Door index {door_index} is outside the detected door range.")
        selected_door = doors[door_index]
        door = Point(selected_door["x"], selected_door["y"])

    warehouse = cad["warehouse"]
    storage_region = warehouse
    obstacles = cad.get("obstacles", [])

    if turning_center_xy is not None:
        turning_center = Point(float(turning_center_xy[0]), float(turning_center_xy[1]))
    else:
        turning_center = door

    layout_cfg = LayoutConfig(
        pallet_width_m=pallet_width_m,
        pallet_depth_m=pallet_depth_m,
        wall_clearance_m=wall_clearance_m,
        main_aisle_m=main_aisle_m,
        cross_aisle_m=cross_aisle_m,
        turning_diameter_m=turning_diameter_m,
    )

    theoretical = theoretical_capacity(storage_region, layout_cfg, obstacles)
    winner, search_df = optimize_realistic_layout(
        storage_region,
        door,
        obstacles,
        layout_cfg,
        orientation_options=layout_cfg.orientations,
        wall_clearance_options=(wall_clearance_m,),
        main_aisle_options=(main_aisle_m,),
        cross_aisle_options=(cross_aisle_m,),
        turning_enabled=turning_enabled,
        turning_center=turning_center,
        turning_diameter_m=turning_diameter_m,
    )

    slots = winner["layout"]["slots"]
    realistic = len(slots)
    if realistic <= 0:
        raise ValueError("The selected constraints produced zero feasible pallet slots.")

    cap_cfg = CapacityConfig(target_occupancy_pct=target_occupancy_pct)
    cap_cfg.validate()
    cap = capacity_summary(
        warehouse.area,
        storage_region.area,
        theoretical["capacity"],
        realistic,
        target_occupancy_pct,
    )
    cap_table = capacity_table(cap)
    planning = cap["planning_capacity"]

    slots_df = slot_master(slots, door)
    demand_raw = load_demand_file(demand_path)
    demand = normalize_demand(demand_raw)

    sim_cfg = SimulationConfig(
        forklift_speed_mps=forklift_speed_mps,
        handling_time_sec_per_pallet=handling_time_sec_per_pallet,
        target_occupancy_pct=target_occupancy_pct,
    )
    daily_df, occupancy_df = simulate_daily(demand, slots_df, realistic, planning, sim_cfg)
    sim_summary = simulation_summary(daily_df)
    sim_summary["average_storage_usage_pct"] = float(daily_df["STORAGE_USAGE_%"].mean()) if not daily_df.empty else 0.0
    dmetrics = demand_metrics(demand, realistic, planning)

    return {
        "cad": cad,
        "warehouse": warehouse,
        "storage_region": storage_region,
        "obstacles": obstacles,
        "doors": doors,
        "selected_door": selected_door,
        "door": door,
        "turning_enabled": turning_enabled,
        "turning_center": turning_center,
        "turning_diameter_m": turning_diameter_m if turning_enabled else 0.0,
        "layout_config": layout_cfg,
        "theoretical": theoretical,
        "winner": winner,
        "search_df": search_df,
        "slots": slots,
        "slot_df": slots_df,
        "demand": demand,
        "daily_df": daily_df,
        "daily_simulation": daily_df,
        "occupancy_df": occupancy_df,
        "capacity": cap,
        "capacity_table": cap_table,
        "demand_metrics": dmetrics,
        "simulation_summary": sim_summary,
    }
