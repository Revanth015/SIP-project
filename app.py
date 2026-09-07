"""JK Fenner Warehouse Operations Scenario Simulation — Streamlit app.

Model 1 creates the physical warehouse slot master.
Model 2 consumes that slot master and the three supplied operational files.
"""
from pathlib import Path
import tempfile

import pandas as pd
import streamlit as st
from shapely.geometry import Point, box as shapely_box

from core.pipeline import run_pipeline, prepare_cad
from core.visualization_engine import plot_cad_geometry, plot_layout, plot_slot_allocation
from core.scenario_engine import (
    DEFAULT_BELTS_PER_BOX,
    prepare_movement,
    build_scenario_table,
    rank_skus,
    allocate_slots,
)

st.set_page_config(page_title="JK Fenner Warehouse Operations Scenario Simulation", page_icon="🏭", layout="wide")

st.markdown("""
<style>
.block-container{padding-top:1.2rem;padding-bottom:2rem}
.hero h1{margin:0;font-size:2.1rem}
.hero p{margin:.2rem 0 1rem;color:#6b7280}
.note{font-size:.82rem;color:#6b7280}
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="hero"><h1>🏭 JK Fenner Warehouse Operations Scenario Simulation</h1>'
    '<p>Model 1 establishes feasible physical slots. Model 2 uses only the three supplied operational files to test SKU allocation inside those slots.</p></div>',
    unsafe_allow_html=True,
)


def save_upload(upload):
    suffix = Path(upload.name).suffix.lower()
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.write(upload.getbuffer())
    f.close()
    return f.name


def reset_results():
    for key in (
        "result", "scenario", "cad_data", "cad_key", "selected_door",
        "door_x", "door_y", "door_reference", "turning_x", "turning_y",
    ):
        st.session_state.pop(key, None)


def operating_door(cad):
    wh = cad["warehouse"]
    minx, miny, maxx, maxy = wh.bounds
    doors = cad.get("doors", [])
    st.markdown("#### Operating door")
    if doors:
        labels = [f"CAD Door {i}: ({d['x']:.2f}, {d['y']:.2f})" for i, d in enumerate(doors, 1)]
        choice = st.selectbox("Reference door", labels, key="door_reference")
        ref = doors[labels.index(choice)]
        dx, dy = float(ref["x"]), float(ref["y"])
    else:
        dx, dy = float(wh.centroid.x), float(wh.centroid.y)
        st.warning("No CAD door detected. Enter the validated operating-door coordinates.")
    c1, c2 = st.columns(2)
    with c1:
        x = st.number_input("Door X (m)", float(minx), float(maxx), float(st.session_state.get("door_x", dx)), 0.1, key="door_x")
    with c2:
        y = st.number_input("Door Y (m)", float(miny), float(maxy), float(st.session_state.get("door_y", dy)), 0.1, key="door_y")
    st.caption(f"Selected operating door: X = {x:.3f} m · Y = {y:.3f} m")
    return Point(x, y)


def turning_zone_controls(cad, door):
    """Expose turning-zone enable/disable, diameter and manual placement."""
    wh = cad["warehouse"]
    minx, miny, maxx, maxy = wh.bounds
    st.markdown("#### Turning zone")
    enabled = st.checkbox("Use turning zone", value=True, help="Disable this only when the operating method does not require a dedicated turning zone.")
    diameter = st.number_input("Turning diameter (m)", 0.0, 20.0, 7.0, 0.25, disabled=not enabled)
    placement = st.radio(
        "Turning-zone placement",
        ["At operating door", "Manual position"],
        horizontal=True,
        disabled=not enabled,
    )
    if not enabled:
        st.caption("Turning zone is disabled; no turning area will block pallet slots.")
        return False, 0.0, None
    if placement == "At operating door":
        st.caption(f"Turning zone center follows operating door: ({door.x:.2f}, {door.y:.2f})")
        return True, diameter, Point(door.x, door.y)
    c1, c2 = st.columns(2)
    with c1:
        tx = st.number_input("Turning-zone X (m)", float(minx), float(maxx), float(st.session_state.get("turning_x", door.x)), 0.1, key="turning_x")
    with c2:
        ty = st.number_input("Turning-zone Y (m)", float(miny), float(maxy), float(st.session_state.get("turning_y", door.y)), 0.1, key="turning_y")
    st.caption(f"Manual turning-zone center: X = {tx:.3f} m · Y = {ty:.3f} m")
    return True, diameter, Point(tx, ty)


def analysis_region(warehouse, mode, x0=0.0, y0=0.0, x1=0.0, y1=0.0):
    minx, miny, maxx, maxy = warehouse.bounds
    if mode == "Whole warehouse":
        return warehouse
    if mode == "Left half":
        return warehouse.intersection(shapely_box(minx, miny, (minx + maxx) / 2, maxy))
    if mode == "Right half":
        return warehouse.intersection(shapely_box((minx + maxx) / 2, miny, maxx, maxy))
    if mode == "Lower half":
        return warehouse.intersection(shapely_box(minx, miny, maxx, (miny + maxy) / 2))
    if mode == "Upper half":
        return warehouse.intersection(shapely_box(minx, (miny + maxy) / 2, maxx, maxy))
    return warehouse.intersection(shapely_box(x0, y0, x1, y1))


with st.sidebar:
    st.header("Project inputs")
    st.subheader("Model 1 · Physical inputs")
    cad_file = st.file_uploader("1 · Warehouse CAD", ["dxf"], key="cad")
    demand_file = st.file_uploader("2 · Daily demand", ["xlsx", "csv"], key="demand")

    st.divider()
    st.subheader("Model 2 · Required data inputs")
    st.caption("Model 2 accepts exactly these three supplied files.")
    mto_file = st.file_uploader("1 · MTO Consolidated Master", ["xlsx", "csv"], key="mto")
    mta_file = st.file_uploader("2 · MTA Consolidated Master", ["xlsx", "csv"], key="mta")
    box_file = st.file_uploader("3 · Size–Box Master Table", ["xlsx", "csv"], key="box")

    st.divider()
    st.subheader("Model 1 · Physical settings")
    pallet_width = st.number_input("Pallet width (m)", 0.1, 5.0, 1.20, 0.05)
    pallet_depth = st.number_input("Pallet depth (m)", 0.1, 5.0, 1.00, 0.05)
    occupancy = st.slider("Planning occupancy (%)", 50, 85, 65)
    with st.expander("Advanced physical constraints"):
        wall_clearance = st.number_input("Wall clearance (m)", 0.0, 5.0, 0.25, 0.05)
        main_aisle = st.number_input("Main aisle (m)", 0.5, 10.0, 4.0, 0.25)
        cross_aisle = st.number_input("Cross aisle (m)", 0.0, 10.0, 2.0, 0.25)
        forklift_speed = st.number_input("Forklift speed (m/s)", 0.1, 10.0, 2.5, 0.1)
        handling_time = st.number_input("Handling time (sec/pallet)", 0.0, 600.0, 0.0, 5.0)

    st.subheader("Model 2 · Scenario settings")
    area_mode = st.selectbox("Analysis area", ["Whole warehouse", "Left half", "Right half", "Lower half", "Upper half", "Custom rectangle"])
    if area_mode == "Custom rectangle" and "cad_data" in st.session_state:
        wh = st.session_state.cad_data["warehouse"]
        mnx, mny, mxx, mxy = wh.bounds
        x0 = st.number_input("Area X-min", float(mnx), float(mxx), float(mnx), 0.1)
        x1 = st.number_input("Area X-max", float(mnx), float(mxx), float(mxx), 0.1)
        y0 = st.number_input("Area Y-min", float(mny), float(mxy), float(mny), 0.1)
        y1 = st.number_input("Area Y-max", float(mny), float(mxy), float(mxy), 0.1)
    else:
        x0 = y0 = x1 = y1 = 0.0
    strategy = st.selectbox("Allocation strategy", ["Frequency priority", "Volume priority", "Frequency × volume"])
    belts_per_slot = st.number_input("Scenario conversion: belts represented by one pallet slot", min_value=1.0, max_value=100000.0, value=1000.0, step=50.0, help="Planning parameter; not observed inventory capacity.")

    run_physical = st.button("▶ Run Model 1", type="primary", use_container_width=True)
    run_scenario = st.button("▶ Run Model 2 Scenario", use_container_width=True)
    if st.button("↺ Reset", use_container_width=True):
        reset_results()
        st.rerun()


if cad_file:
    key = f"{cad_file.name}:{cad_file.size}"
    if st.session_state.get("cad_key") != key:
        try:
            st.session_state.cad_path = save_upload(cad_file)
            st.session_state.cad_data = prepare_cad(st.session_state.cad_path)
            st.session_state.cad_key = key
            for k in ("door_x", "door_y", "door_reference", "selected_door", "turning_x", "turning_y"):
                st.session_state.pop(k, None)
        except Exception as exc:
            st.error(f"CAD could not be read: {exc}")
            st.stop()

if "cad_data" in st.session_state:
    cad = st.session_state.cad_data
    with st.expander("CAD review · door · turning zone", expanded=True):
        a, b, c = st.columns(3)
        a.metric("Warehouse area", f"{cad['warehouse'].area:,.1f} m²")
        b.metric("CAD doors", len(cad.get("doors", [])))
        c.metric("Obstacles", len(cad.get("obstacles", [])))
        st.pyplot(plot_cad_geometry(cad), use_container_width=True)
        selected_door = operating_door(cad)
        turning_enabled, turning_diameter, turning_center = turning_zone_controls(cad, selected_door)
        st.session_state.selected_door = selected_door
        st.session_state.turning_enabled = turning_enabled
        st.session_state.turning_diameter = turning_diameter
        st.session_state.turning_center = turning_center

if run_physical:
    if "cad_path" not in st.session_state or not demand_file:
        st.error("Upload CAD + Daily demand first.")
        st.stop()
    if "selected_door" not in st.session_state:
        st.error("Set the operating/custom door below the CAD drawing.")
        st.stop()
    try:
        door = st.session_state.selected_door
        turning_center = st.session_state.get("turning_center") or door
        turning_enabled = st.session_state.get("turning_enabled", True)
        turning_diameter = st.session_state.get("turning_diameter", 7.0)
        with st.spinner("Running Model 1 physical scenario…"):
            result = run_pipeline(
                st.session_state.cad_path,
                save_upload(demand_file),
                manual_door_xy=(door.x, door.y),
                pallet_width_m=pallet_width,
                pallet_depth_m=pallet_depth,
                wall_clearance_m=wall_clearance,
                main_aisle_m=main_aisle,
                cross_aisle_m=cross_aisle,
                turning_diameter_m=turning_diameter,
                turning_enabled=turning_enabled,
                turning_center_xy=(turning_center.x, turning_center.y) if turning_center is not None else None,
                target_occupancy_pct=occupancy,
                forklift_speed_mps=forklift_speed,
                handling_time_sec_per_pallet=handling_time,
            )
        result["config"] = {
            "pallet_width_m": pallet_width,
            "pallet_depth_m": pallet_depth,
            "wall_clearance_m": wall_clearance,
            "main_aisle_m": main_aisle,
            "cross_aisle_m": cross_aisle,
            "turning_enabled": turning_enabled,
            "turning_diameter_m": turning_diameter if turning_enabled else 0.0,
            "turning_center_x_m": turning_center.x if turning_center is not None else None,
            "turning_center_y_m": turning_center.y if turning_center is not None else None,
            "target_occupancy_pct": occupancy,
            "forklift_speed_mps": forklift_speed,
            "handling_time_sec_per_pallet": handling_time,
            "operating_door_x_m": door.x,
            "operating_door_y_m": door.y,
        }
        st.session_state.result = result
        st.success("Model 1 completed. Its generated slots are now available to Model 2.")
    except Exception as exc:
        st.error(f"Model 1 failed: {exc}")
        st.stop()


if run_scenario:
    if "result" not in st.session_state:
        st.error("Run Model 1 first so Model 2 can use its generated slots.")
        st.stop()
    missing = []
    if not mto_file: missing.append("MTO Consolidated Master")
    if not mta_file: missing.append("MTA Consolidated Master")
    if not box_file: missing.append("Size–Box Master Table")
    if missing:
        st.error("Upload the three Model 2 files: " + ", ".join(missing) + ".")
        st.stop()
    try:
        r = st.session_state.result
        region = analysis_region(r["warehouse"], area_mode, x0, y0, x1, y1)
        if region.is_empty or region.area <= 0:
            st.error("Selected analysis area is empty. Adjust the area selection.")
            st.stop()
        slot_df = r["slot_df"].copy()
        slot_df = slot_df[slot_df.apply(lambda row: region.covers(Point(row["X_M"], row["Y_M"])), axis=1)].copy()
        slot_df = slot_df.sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)
        if slot_df.empty:
            st.error("No Model 1-generated slots fall inside the selected area.")
            st.stop()
        with st.spinner("Preparing Model 2 files and allocating Model 1 slots…"):
            sku, mto_clean, mta_clean, box_clean = prepare_movement(save_upload(mto_file), save_upload(mta_file), save_upload(box_file))
            scenario_table, allocations = build_scenario_table(sku, slot_df, belts_per_slot)
            selected_rank = rank_skus(sku, strategy)
            selected_allocation, selected_summary = allocate_slots(selected_rank, slot_df, belts_per_slot)
        st.session_state.scenario = {
            "sku": sku, "mto": mto_clean, "mta": mta_clean, "box": box_clean,
            "region": region, "slot_df": slot_df, "strategy": strategy,
            "belts_per_slot": belts_per_slot, "scenario_table": scenario_table,
            "allocations": allocations, "allocation": selected_allocation, "summary": selected_summary,
        }
        st.success(f"Model 2 completed using {len(slot_df):,} Model 1-generated slots in the selected area.")
    except Exception as exc:
        st.error(f"Model 2 scenario failed: {exc}")
        st.stop()


if "result" not in st.session_state and "scenario" not in st.session_state:
    st.info("Upload Model 1 inputs and run Model 1 first. Model 2 then uses the three supplied data files.")
    st.stop()

result = st.session_state.get("result")
scenario = st.session_state.get("scenario")
tabs = st.tabs(["Overview", "Model 1 · Physical", "Model 2 · Slot Scenario", "Decision", "Downloads"])

with tabs[0]:
    st.markdown("### Executive overview")
    if result:
        cap, sim, dm = result["capacity"], result["simulation_summary"], result["demand_metrics"]
        k = st.columns(5)
        k[0].metric("Realistic capacity", f"{cap['realistic_capacity']:,}")
        k[1].metric("Planning capacity", f"{cap['planning_capacity']:,.0f}")
        k[2].metric("Average demand", f"{dm['average_daily_demand']:.1f}")
        k[3].metric("Peak demand", f"{dm['peak_demand']:.0f}")
        k[4].metric("Overflow days", f"{sim['overflow_days']}")
    if scenario:
        s = scenario["summary"]
        k = st.columns(5)
        k[0].metric("Selected area slots", f"{s['total_slots']:,}")
        k[1].metric("Allocated slots", f"{s['allocated_slots']:,}")
        k[2].metric("Slot utilization", f"{s['slot_utilization_pct']:.1f}%")
        k[3].metric("Movement coverage", f"{s['movement_coverage_pct']:.1f}%")
        k[4].metric("Overflow slot equivalents", f"{s['overflow_slot_equivalents']:,}")

with tabs[1]:
    if not result:
        st.info("Run Model 1.")
    else:
        r = result
        cap = r["capacity"]
        st.markdown("### Model 1 · Physical Warehouse Scenario")
        k = st.columns(5)
        k[0].metric("Theoretical capacity", f"{cap['theoretical_capacity']:,}")
        k[1].metric("Realistic capacity", f"{cap['realistic_capacity']:,}")
        k[2].metric("Planning capacity", f"{cap['planning_capacity']:,.0f}")
        k[3].metric("Average door distance", f"{r['winner']['avg_distance_m']:.2f} m")
        k[4].metric("Turning zone", "OFF" if not r.get("turning_enabled", True) else f"{r.get('turning_diameter_m', 0):.1f} m")
        st.caption(
            f"Operating door: ({r['door'].x:.2f}, {r['door'].y:.2f}) · "
            + ("Turning zone disabled" if not r.get("turning_enabled", True) else f"Turning center: ({r['turning_center'].x:.2f}, {r['turning_center'].y:.2f})")
        )
        st.pyplot(
            plot_layout(
                r["warehouse"], r["storage_region"], r["slots"], r["door"],
                r["winner"]["layout"]["main_aisle"],
                r["winner"]["layout"]["turning_circle"],
                r["obstacles"],
                title="Model 1 · Generated Feasible Pallet Slots",
            ), use_container_width=True,
        )
        st.dataframe(r["slot_df"], use_container_width=True, hide_index=True)

with tabs[2]:
    if not scenario:
        st.info("Run Model 2 Scenario after Model 1.")
    else:
        s = scenario["summary"]
        st.markdown("### Model 2 · SKU Slot-Allocation Scenario")
        st.caption("Model 2 uses only the three supplied files and the exact physical slots generated by Model 1.")
        a, b, c, d = st.columns(4)
        a.metric("Selected area slots", f"{s['total_slots']:,}")
        b.metric("Allocated slots", f"{s['allocated_slots']:,}")
        c.metric("Movement coverage", f"{s['movement_coverage_pct']:.1f}%")
        d.metric("Overflow slot equivalents", f"{s['overflow_slot_equivalents']:,}")
        sku = scenario["sku"]
        default_count = int((sku["BELTS_PER_BOX_SOURCE"] == f"Default = {DEFAULT_BELTS_PER_BOX}").sum())
        master_count = int((sku["BELTS_PER_BOX_SOURCE"] == "Master").sum())
        st.info(f"Packing standard coverage: {master_count:,} SKUs from the Size–Box Master; {default_count:,} SKUs use the documented default of {DEFAULT_BELTS_PER_BOX} belts/box.")
        st.pyplot(plot_slot_allocation(result["warehouse"], scenario["region"], result["slots"], scenario["allocation"], result["door"], title=f"Model 2 · {scenario['strategy']} · Selected Area"), use_container_width=True)
        st.markdown("#### Strategy comparison")
        st.dataframe(scenario["scenario_table"], use_container_width=True, hide_index=True)
        st.markdown("#### Selected allocation sequence")
        st.dataframe(scenario["allocation"], use_container_width=True, hide_index=True)
        st.markdown("#### SKU movement and packing profile")
        st.dataframe(sku, use_container_width=True, hide_index=True)

with tabs[3]:
    st.markdown("### Decision logic")
    st.markdown("**Model 1:** establishes the physically feasible pallet-slot master from CAD, the selected operating door, aisle constraints and the configurable turning-zone rule.  \n**Model 2:** selects an area, filters those exact Model 1 slots, ranks SKUs from MTO/MTA movement and uses the Size–Box Master where available.")
    st.warning("Belts-per-slot is a planning scenario parameter, not observed inventory capacity. Validate it before operational implementation.")
    if scenario:
        best = scenario["scenario_table"].sort_values("movement_coverage_pct", ascending=False).iloc[0]
        st.success(f"Within the selected Model 1 area, the highest movement coverage in the tested scenarios is {best['STRATEGY']} at {best['movement_coverage_pct']:.1f}%.")

with tabs[4]:
    st.markdown("### Downloads")
    if result:
        st.download_button("Download Model 1 slot master", result["slot_df"].to_csv(index=False).encode("utf-8"), "model1_slot_master.csv", "text/csv")
        st.download_button("Download Model 1 daily simulation", result["daily_simulation"].to_csv(index=False).encode("utf-8"), "model1_daily_simulation.csv", "text/csv")
    if scenario:
        st.download_button("Download Model 2 SKU movement master", scenario["sku"].to_csv(index=False).encode("utf-8"), "model2_sku_movement_master.csv", "text/csv")
        st.download_button("Download Model 2 scenario comparison", scenario["scenario_table"].to_csv(index=False).encode("utf-8"), "model2_scenario_comparison.csv", "text/csv")
        st.download_button("Download Model 2 selected allocation", scenario["allocation"].to_csv(index=False).encode("utf-8"), "model2_selected_allocation.csv", "text/csv")
