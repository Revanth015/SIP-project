import pandas as pd
from shapely.geometry import Polygon, Point

from core.layout_engine import LayoutConfig, theoretical_capacity, optimize_realistic_layout, slot_master
from core.capacity_engine import capacity_summary
from core.simulation_engine import SimulationConfig, simulate_daily


def test_capacity_separation():
    summary = capacity_summary(100, 80, 100, 60, 65)
    assert summary["theoretical_capacity"] == 100
    assert summary["realistic_capacity"] == 60
    assert summary["planning_capacity"] == 39


def test_layout_and_slot_master():
    region = Polygon([(0, 0), (12, 0), (12, 10), (0, 10)])
    cfg = LayoutConfig(
        pallet_width_m=1,
        pallet_depth_m=1,
        wall_clearance_m=0.25,
        main_aisle_m=2,
        cross_aisle_m=1,
        turning_diameter_m=2,
    )
    theoretical = theoretical_capacity(region, cfg)
    winner, search = optimize_realistic_layout(
        region, Point(1, 0.5), config=cfg,
        wall_clearance_options=(0.25,),
        main_aisle_options=(2,),
        cross_aisle_options=(1,),
    )
    slots = winner["layout"]["slots"]
    assert theoretical["capacity"] >= len(slots) > 0
    df = slot_master(slots, Point(1, 0.5))
    assert len(df) == len(slots)
    assert df["DISTANCE_FROM_DOOR_M"].is_monotonic_increasing


def test_daily_simulation():
    slot_df = pd.DataFrame([
        {"SLOT_ID": "S0001", "X_M": 1, "Y_M": 1, "DISTANCE_FROM_DOOR_M": 1},
        {"SLOT_ID": "S0002", "X_M": 2, "Y_M": 1, "DISTANCE_FROM_DOOR_M": 2},
        {"SLOT_ID": "S0003", "X_M": 3, "Y_M": 1, "DISTANCE_FROM_DOOR_M": 3},
    ])
    demand = pd.DataFrame({
        "DATE": ["2026-01-01", "2026-01-02"],
        "TOTAL_PALLETS": [2, 4],
    })
    daily, occupancy = simulate_daily(
        demand, slot_df, realistic_capacity=3, planning_capacity=2.0,
        config=SimulationConfig(forklift_speed_mps=2.5),
    )
    assert daily.iloc[0]["OCCUPIED_SLOTS"] == 2
    assert daily.iloc[1]["OVERFLOW"] == 1
    assert len(occupancy) == 5
