"""Warehouse layout and slot-generation engine.

This module is deliberately independent of the UI and demand simulation. It
converts confirmed CAD geometry and operating constraints into theoretical and
realistic pallet slots plus a slot-distance master.
"""

from dataclasses import dataclass
from typing import Iterable

import numpy as np
from shapely.geometry import Point, Polygon, box


@dataclass(frozen=True)
class LayoutConfig:
    pallet_width_m: float = 1.20
    pallet_depth_m: float = 1.00
    wall_clearance_m: float = 0.25
    main_aisle_m: float = 4.00
    cross_aisle_m: float = 2.00
    turning_diameter_m: float = 7.00
    orientations: tuple[int, ...] = (0, 90)


def oriented_pallet_size(width: float, depth: float, orientation: int):
    if orientation % 180 == 90:
        return depth, width
    return width, depth


def _blocked(pallet, blocked_regions: Iterable[Polygon]):
    return any(pallet.intersects(region) for region in blocked_regions)


def generate_theoretical_slots(region, pallet_width, pallet_depth, obstacles=None, orientation=0):
    """Pack pallets on a simple rectangular grid without aisle deductions.

    This is intentionally a geometric upper bound, not an operating recommendation.
    """
    obstacles = list(obstacles or [])
    pw, pd = oriented_pallet_size(pallet_width, pallet_depth, orientation)
    minx, miny, maxx, maxy = region.bounds
    slots = []

    x = minx
    while x + pw <= maxx + 1e-9:
        y = miny
        while y + pd <= maxy + 1e-9:
            pallet = box(x, y, x + pw, y + pd)
            if region.contains(pallet) and not _blocked(pallet, obstacles):
                slots.append(pallet)
            y += pd
        x += pw

    return slots


def theoretical_capacity(region, config: LayoutConfig, obstacles=None):
    """Return the best geometric capacity across supported orientations."""
    candidates = []
    for orientation in config.orientations:
        slots = generate_theoretical_slots(
            region,
            config.pallet_width_m,
            config.pallet_depth_m,
            obstacles,
            orientation,
        )
        candidates.append({
            "orientation": orientation,
            "capacity": len(slots),
            "slots": slots,
        })
    return max(candidates, key=lambda x: x["capacity"])


def _build_layout(
    region,
    door: Point,
    obstacles,
    config: LayoutConfig,
    orientation: int,
    wall_clearance_m: float,
    main_aisle_m: float,
    cross_aisle_m: float,
):
    """Generate a feasible slot arrangement around an aisle connected to the door."""
    usable = region.buffer(-wall_clearance_m)
    if usable.is_empty:
        return {"slots": [], "usable_region": usable, "main_aisle": None, "turning_circle": None}

    pw, pd = oriented_pallet_size(
        config.pallet_width_m,
        config.pallet_depth_m,
        orientation,
    )
    minx, miny, maxx, maxy = usable.bounds

    # The main aisle is connected to the selected operating door and extends
    # through the warehouse. This is a conservative reusable abstraction;
    # company-specific aisle networks can later be added as explicit CAD zones.
    main_aisle = box(
        door.x - main_aisle_m / 2,
        miny,
        door.x + main_aisle_m / 2,
        maxy,
    )
    turning_circle = door.buffer(config.turning_diameter_m / 2)
    blocked = [main_aisle, turning_circle, *list(obstacles or [])]

    slots = []
    x = minx
    while x + pw <= maxx + 1e-9:
        y = miny
        rows_since_cross_aisle = 0
        while y + pd <= maxy + 1e-9:
            pallet = box(x, y, x + pw, y + pd)
            feasible = usable.contains(pallet) and not _blocked(pallet, blocked)
            if feasible:
                slots.append(pallet)
            rows_since_cross_aisle += 1
            y += pd
            if rows_since_cross_aisle >= 2:
                y += cross_aisle_m
                rows_since_cross_aisle = 0
        x += pw

    return {
        "slots": slots,
        "usable_region": usable,
        "main_aisle": main_aisle,
        "turning_circle": turning_circle,
    }


def optimize_realistic_layout(
    region,
    door: Point,
    obstacles=None,
    config: LayoutConfig | None = None,
    orientation_options=None,
    wall_clearance_options=None,
    main_aisle_options=None,
    cross_aisle_options=None,
):
    """Search feasible layout alternatives and return the best capacity/distance trade-off.

    Capacity is the primary objective; average door-to-slot distance is a
    secondary penalty. The returned search table is useful for transparency
    in the final dashboard.
    """
    config = config or LayoutConfig()
    orientations = orientation_options or config.orientations
    walls = wall_clearance_options or (config.wall_clearance_m,)
    mains = main_aisle_options or (config.main_aisle_m,)
    crosses = cross_aisle_options or (config.cross_aisle_m,)

    candidates = []
    for orientation in orientations:
        for wall in walls:
            for main_aisle in mains:
                for cross_aisle in crosses:
                    layout = _build_layout(
                        region,
                        door,
                        obstacles,
                        config,
                        orientation,
                        wall,
                        main_aisle,
                        cross_aisle,
                    )
                    slots = layout["slots"]
                    if not slots:
                        continue
                    distances = [slot.centroid.distance(door) for slot in slots]
                    avg_distance = float(np.mean(distances))
                    # Capacity dominates; distance breaks near-equal capacity ties.
                    score = len(slots) - 0.10 * avg_distance
                    candidates.append({
                        "score": score,
                        "capacity": len(slots),
                        "orientation": orientation,
                        "wall_clearance_m": wall,
                        "main_aisle_m": main_aisle,
                        "cross_aisle_m": cross_aisle,
                        "avg_distance_m": avg_distance,
                        "layout": layout,
                    })

    if not candidates:
        raise ValueError(
            "No feasible layout was generated. Check warehouse geometry, "
            "door position, pallet dimensions and aisle constraints."
        )

    search_rows = [
        {k: v for k, v in c.items() if k != "layout"}
        for c in candidates
    ]
    search_df = __import__("pandas").DataFrame(search_rows).sort_values(
        ["capacity", "avg_distance_m"],
        ascending=[False, True],
    ).reset_index(drop=True)

    winner = max(candidates, key=lambda c: c["score"])
    return winner, search_df


def slot_master(slots, door: Point):
    """Create the slot-level reference table used by the simulation/dashboard."""
    rows = []
    for i, slot in enumerate(slots, start=1):
        centroid = slot.centroid
        rows.append({
            "SLOT_ID": f"S{i:04d}",
            "X_M": centroid.x,
            "Y_M": centroid.y,
            "DISTANCE_FROM_DOOR_M": centroid.distance(door),
        })

    df = __import__("pandas").DataFrame(rows)
    if not df.empty:
        df = df.sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)
        df["DISTANCE_RANK"] = np.arange(1, len(df) + 1)
    return df
