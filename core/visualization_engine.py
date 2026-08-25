"""Visualization engine for the Universal Warehouse Digital Twin.

Produces CAD/layout views, slot-occupancy views and a day-by-day GIF. The
functions return matplotlib figures so the Streamlit UI can display them
without coupling visualization logic to the application interface.
"""

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle
from matplotlib.animation import FuncAnimation, PillowWriter


def _draw_polygon(ax, polygon, **kwargs):
    if polygon is None or polygon.is_empty:
        return
    x, y = polygon.exterior.xy
    ax.plot(x, y, **kwargs)


def plot_cad_geometry(cad_data, figsize=(11, 8)):
    """Display extracted CAD entities and detected warehouse/doors."""
    fig, ax = plt.subplots(figsize=figsize)

    for entity in cad_data.get("entities", []):
        points = entity.get("points")
        if not points:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        layer = str(entity.get("layer", "0")).lower()
        linewidth = 2.8 if "warehouse" in layer else 3.5 if any(k in layer for k in ("door", "gate", "entry", "exit")) else 0.8
        ax.plot(xs, ys, linewidth=linewidth, alpha=0.75)

    warehouse = cad_data.get("warehouse")
    if warehouse is not None:
        _draw_polygon(ax, warehouse, linewidth=3, label="Warehouse boundary")

    for i, door in enumerate(cad_data.get("doors", []), start=1):
        ax.scatter(door["x"], door["y"], s=110, marker="*", zorder=10,
                   label=f"Door {i}")
        ax.annotate(
            f"D{i}\n({door['x']:.2f}, {door['y']:.2f})",
            (door["x"], door["y"]),
            xytext=(7, 7), textcoords="offset points", fontsize=8,
        )

    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")
    ax.set_title("CAD Geometry Review")
    ax.grid(alpha=0.15)
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    return fig


def plot_layout(
    warehouse,
    storage_region,
    slots,
    door,
    main_aisle=None,
    turning_circle=None,
    obstacles=None,
    occupied_count=0,
    title="Warehouse Digital Twin Layout",
    figsize=(11, 8),
):
    """Draw a warehouse layout and highlight the first occupied slots."""
    fig, ax = plt.subplots(figsize=figsize)

    _draw_polygon(ax, warehouse, linewidth=2.5, label="Warehouse")
    _draw_polygon(ax, storage_region, linestyle="--", linewidth=1.5, label="Storage region")

    if main_aisle is not None:
        x, y = main_aisle.exterior.xy
        ax.fill(x, y, alpha=0.18, label="Main aisle")

    if turning_circle is not None:
        x, y = turning_circle.exterior.xy
        ax.plot(x, y, linestyle=":", linewidth=1.8, label="Turning area")

    for i, obstacle in enumerate(obstacles or []):
        x, y = obstacle.exterior.xy
        ax.fill(x, y, alpha=0.30, label="Obstacle" if i == 0 else None)

    for i, slot in enumerate(slots):
        x1, y1, x2, y2 = slot.bounds
        occupied = i < occupied_count
        ax.add_patch(Rectangle(
            (x1, y1), x2 - x1, y2 - y1,
            facecolor="tab:green" if occupied else "none",
            edgecolor="gray", linewidth=0.35,
            alpha=0.72 if occupied else 1.0,
        ))

    if door is not None:
        ax.scatter([door.x], [door.y], s=130, marker="*", zorder=20, label="Selected door")

    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")
    ax.set_title(title, fontsize=13, weight="bold")
    ax.grid(alpha=0.12)
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    return fig


def create_daily_gif(
    simulation_df,
    warehouse,
    storage_region,
    slots,
    door,
    main_aisle=None,
    turning_circle=None,
    obstacles=None,
    output_path="warehouse_simulation.gif",
    fps=4,
):
    """Create a day-by-day animated warehouse occupancy GIF."""
    simulation_df = simulation_df.reset_index(drop=True)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(11, 8))

    def update(frame):
        ax.clear()
        row = simulation_df.iloc[frame]
        occupied_count = int(row["OCCUPIED_SLOTS"])

        _draw_polygon(ax, warehouse, linewidth=2.5)
        _draw_polygon(ax, storage_region, linestyle="--", linewidth=1.2)

        if main_aisle is not None:
            x, y = main_aisle.exterior.xy
            ax.fill(x, y, alpha=0.18)

        if turning_circle is not None:
            x, y = turning_circle.exterior.xy
            ax.plot(x, y, linestyle=":", linewidth=1.5)

        for obstacle in obstacles or []:
            x, y = obstacle.exterior.xy
            ax.fill(x, y, alpha=0.30)

        for i, slot in enumerate(slots):
            x1, y1, x2, y2 = slot.bounds
            ax.add_patch(Rectangle(
                (x1, y1), x2 - x1, y2 - y1,
                facecolor="tab:green" if i < occupied_count else "none",
                edgecolor="gray", linewidth=0.35,
                alpha=0.72 if i < occupied_count else 1.0,
            ))

        ax.scatter([door.x], [door.y], s=130, marker="*", zorder=20)
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel("X (m)")
        ax.set_ylabel("Y (m)")
        ax.set_title(
            f"Warehouse Digital Twin | {row['DATE']} | "
            f"Demand: {int(row['PALLETS'])} | "
            f"Occupied: {occupied_count} | "
            f"Overflow: {int(row['OVERFLOW'])}",
            fontsize=12,
            weight="bold",
        )
        ax.grid(alpha=0.12)
        fig.tight_layout()

    animation = FuncAnimation(fig, update, frames=len(simulation_df), interval=1000 // max(fps, 1), repeat=True)
    animation.save(output_path, writer=PillowWriter(fps=fps))
    plt.close(fig)
    return output_path
