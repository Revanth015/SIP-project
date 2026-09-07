import pandas as pd

from core.layout_optimizer import (
    STRATEGIES,
    prepare_sku_demand,
    evaluate_strategy,
    allocate_shared_locations,
    replay_flow,
)
from core.decision_engine import normalize_location_master, replay_current_layout, improvement


def sample_mto():
    return pd.DataFrame(
        {
            "DATE": pd.to_datetime(["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"]),
            "ITEM_SIZE": ["A", "B", "A", "C"],
            "BELTS": [20, 10, 20, 5],
            "BELTS_PER_BOX": [10, 10, 10, 10],
        }
    )


def sample_slots():
    return pd.DataFrame(
        {
            "SLOT_ID": ["S1", "S2", "S3"],
            "X_M": [1.0, 2.0, 3.0],
            "Y_M": [0.0, 0.0, 0.0],
            "DISTANCE_FROM_DOOR_M": [1.0, 2.0, 3.0],
        }
    )


def test_prepare_sku_demand_creates_storage_requirements():
    sku = prepare_sku_demand(sample_mto(), capacity_units=1.0, safety_buffer=0.0)
    assert set(["MTO_LINES", "MTO_BELTS", "PEAK_PALLET_EQ", "REQUIRED_LOCATIONS"]).issubset(sku.columns)
    assert set(sku["ITEM_SIZE"]) == {"A", "B", "C"}
    assert int(sku.loc[sku["ITEM_SIZE"] == "A", "MTO_LINES"].iloc[0]) == 2


def test_shared_locations_are_allowed_under_capacity():
    sku = prepare_sku_demand(sample_mto(), capacity_units=1.0, safety_buffer=0.0)
    ranked = sku.copy()
    ranked["RANK"] = range(1, len(ranked) + 1)
    ranked["STRATEGY"] = "Movement + Space"
    ranked["STRATEGY_SCORE"] = ranked["MOVEMENT_SPACE_SCORE"]
    allocation, locations = allocate_shared_locations(ranked, sample_slots(), capacity_units=1.0)
    assert not allocation.empty
    assert locations["ASSIGNED_SKUS"].max() >= 1
    assert (locations["USED_STORAGE_EQ"] <= 1.0 + 1e-9).all()


def test_all_strategies_use_the_same_physical_location_count():
    mto = sample_mto()
    slots = sample_slots()
    sku = prepare_sku_demand(mto, capacity_units=1.0, safety_buffer=0.0)
    counts = []
    for strategy in STRATEGIES:
        _, _, _, summary = evaluate_strategy(mto, slots, sku, strategy, capacity_units=1.0)
        counts.append(summary["Available physical locations"])
    assert counts == [3, 3, 3, 3]


def test_replay_flow_returns_daily_results():
    mto = sample_mto()
    slots = sample_slots()
    sku = prepare_sku_demand(mto, capacity_units=1.0, safety_buffer=0.0)
    _, allocation, _, _ = evaluate_strategy(mto, slots, sku, "Movement + Space", capacity_units=1.0)
    flow, daily = replay_flow(mto, allocation)
    assert len(flow) == len(mto)
    assert len(daily) == 2
    assert {"LINE_COVERAGE_PCT", "BELT_COVERAGE_PCT"}.issubset(daily.columns)


def test_current_baseline_normalization_and_replay():
    mapping = normalize_location_master(
        pd.DataFrame(
            {
                "SKU": ["A", "B"],
                "LOCATION": ["C1", "C2"],
                "X": [1.0, 2.0],
                "Y": [0.0, 0.0],
                "DISTANCE": [1.0, 2.0],
            }
        )
    )
    _, summary = replay_current_layout(sample_mto(), mapping)
    assert summary["Current locations"] == 2
    assert summary["Mapped SKUs"] == 2
    assert summary["Unmapped SKUs"] == 1


def test_improvement_direction_is_transparent():
    current = {"Line coverage %": 50.0, "Belt coverage %": 50.0, "Total one-way travel m": 100.0,
               "Avg one-way m / assigned line": 10.0, "Belt-weighted travel m": 1000.0}
    optimized = {"Line coverage %": 60.0, "Belt coverage %": 70.0, "Total one-way travel m": 80.0,
                 "Avg one-way m / assigned line": 8.0, "Belt-weighted travel m": 800.0}
    result = improvement(current, optimized)
    assert result["Line coverage change pp"] == 10.0
    assert result["Belt coverage change pp"] == 20.0
    assert result["Travel change %"] == -20.0
