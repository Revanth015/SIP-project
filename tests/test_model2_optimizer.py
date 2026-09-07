import pandas as pd

from core.layout_optimizer import (
    STRATEGIES,
    prepare_sku_demand,
    evaluate_strategy,
    allocate_shared_locations,
    replay_flow,
)


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


def test_prepare_sku_demand_creates_movement_master():
    sku = prepare_sku_demand(sample_mto())
    assert {"MTO_LINES", "MTO_BELTS", "FREQUENCY_SCORE", "VOLUME_SCORE", "MOVEMENT_SPACE_SCORE"}.issubset(sku.columns)
    assert set(sku["ITEM_SIZE"]) == {"A", "B", "C"}
    assert int(sku.loc[sku["ITEM_SIZE"] == "A", "MTO_LINES"].iloc[0]) == 2


def test_shared_locations_are_allowed():
    sku = prepare_sku_demand(sample_mto())
    ranked = sku.copy()
    ranked["RANK"] = range(1, len(ranked) + 1)
    ranked["STRATEGY"] = "Movement + Space"
    ranked["STRATEGY_SCORE"] = ranked["MOVEMENT_SPACE_SCORE"]
    allocation, locations = allocate_shared_locations(ranked, sample_slots(), max_skus_per_location=2)
    assert not allocation.empty
    assert locations["ASSIGNED_SKUS"].max() <= 2
    assert locations["ASSIGNED_SKUS"].max() == 2


def test_all_strategies_use_the_same_physical_location_count():
    mto = sample_mto()
    slots = sample_slots()
    sku = prepare_sku_demand(mto)
    counts = []
    for strategy in STRATEGIES:
        _, _, _, summary = evaluate_strategy(mto, slots, sku, strategy, max_skus_per_location=2)
        counts.append(summary["Available physical locations"])
    assert counts == [3, 3, 3, 3]


def test_replay_flow_returns_daily_results():
    mto = sample_mto()
    slots = sample_slots()
    sku = prepare_sku_demand(mto)
    _, allocation, _, _ = evaluate_strategy(mto, slots, sku, "Movement + Space", max_skus_per_location=2)
    flow, daily = replay_flow(mto, allocation)
    assert len(flow) == len(mto)
    assert len(daily) == 2
    assert {"LINE_COVERAGE_PCT", "BELT_COVERAGE_PCT"}.issubset(daily.columns)
