"""JK Fenner Poly-V Warehouse Digital Twin — practical Streamlit app."""
from pathlib import Path
import tempfile
from dataclasses import asdict

import pandas as pd
import streamlit as st
from shapely.geometry import Point

from core.pipeline import run_pipeline, prepare_cad
from core.visualization_engine import plot_cad_geometry, plot_layout
from core.mto_actual_engine import (
    ActualMTOConfig,
    prepare_actual_mto,
    simulate_actual_mto,
    build_actual_kpi_table,
    build_storage_paradox_actual,
)

st.set_page_config(page_title="JK Fenner Warehouse Digital Twin", page_icon="🏭", layout="wide")

st.markdown("""
<style>
.block-container{padding-top:1.2rem;padding-bottom:2rem}
.hero h1{margin:0;font-size:2.25rem}
.hero p{margin:.2rem 0 1rem;color:#6b7280}
.note{font-size:.82rem;color:#6b7280}
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="hero"><h1>🏭 JK Fenner Warehouse Digital Twin</h1>'
    '<p>Physical warehouse layout + transaction-level MTO process simulation.</p></div>',
    unsafe_allow_html=True,
)


def save_upload(upload):
    suffix = Path(upload.name).suffix.lower()
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.write(upload.getbuffer())
    f.close()
    return f.name


def reset_results():
    for key in ("result", "model2", "cad_data", "cad_key", "selected_door", "door_x", "door_y", "door_reference"):
        st.session_state.pop(key, None)


def operating_door(cad):
    """The custom door controls deliberately appear below the CAD drawing."""
    wh = cad["warehouse"]
    minx, miny, maxx, maxy = wh.bounds
    doors = cad.get("doors", [])
    st.markdown("#### Operating / custom door")
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
        x = st.number_input("Custom door X (m)", float(minx), float(maxx), float(st.session_state.get("door_x", dx)), 0.1, key="door_x")
    with c2:
        y = st.number_input("Custom door Y (m)", float(miny), float(maxy), float(st.session_state.get("door_y", dy)), 0.1, key="door_y")
    st.caption(f"**Model input:** X = {x:.3f} m · Y = {y:.3f} m")
    return Point(x, y)


with st.sidebar:
    st.header("Project inputs")
    st.caption("Use the same physical context for both models.")
    cad_file = st.file_uploader("1 · Warehouse CAD", ["dxf"], key="cad")
    demand_file = st.file_uploader("2 · Daily demand", ["xlsx", "csv"], key="demand")
    mto_file = st.file_uploader("3 · Raw MTO transactions", ["xlsx", "csv"], key="mto")
    mta_file = st.file_uploader("4 · Raw MTA transactions (optional)", ["xlsx", "csv"], key="mta")
    box_file = st.file_uploader(
        "5 · Size / Box Master (optional)", ["xlsx", "csv"], key="box",
        help="Optional if the raw MTO file already contains Box_Qty_Std / Units Per Box."
    )

    st.divider()
    st.subheader("Model 1 · Physical settings")
    pallet_width = st.number_input("Pallet width (m)", 0.1, 5.0, 1.20, 0.05)
    pallet_depth = st.number_input("Pallet depth (m)", 0.1, 5.0, 1.00, 0.05)
    occupancy = st.slider("Planning occupancy (%)", 50, 85, 65)
    with st.expander("Advanced physical constraints"):
        wall_clearance = st.number_input("Wall clearance (m)", 0.0, 5.0, 0.25, 0.05)
        main_aisle = st.number_input("Main aisle (m)", 0.5, 10.0, 4.0, 0.25)
        cross_aisle = st.number_input("Cross aisle (m)", 0.0, 10.0, 2.0, 0.25)
        turning_diameter = st.number_input("Turning diameter (m)", 0.0, 20.0, 7.0, 0.25)
        forklift_speed = st.number_input("Forklift speed (m/s)", 0.1, 10.0, 2.5, 0.1)
        handling_time = st.number_input("Handling time (sec/pallet)", 0.0, 600.0, 0.0, 5.0)

    st.subheader("Model 2 · All adjustable assumptions")
    with st.expander("Open Model 2 assumption form", expanded=True):
        st.markdown("**Box / data rules**")
        default_box = st.number_input("Default belts / box", 1.0, 10000.0, 28.0, 1.0)
        conflict_box = st.number_input("Conflict fallback belts / box", 1.0, 10000.0, 28.0, 1.0)
        round_qty = st.checkbox("Round non-integer belt quantities", True)

        st.markdown("**Human activity time (sec)**")
        pick = st.number_input("Pick time / box", 0.0, 3600.0, 45.0, 5.0)
        count = st.number_input("Count time / box", 0.0, 3600.0, 30.0, 5.0)
        pack = st.number_input("Pack time / box", 0.0, 3600.0, 40.0, 5.0)
        store = st.number_input("Current WIP store time", 0.0, 3600.0, 60.0, 5.0)
        retrieve = st.number_input("Current WIP retrieve time", 0.0, 3600.0, 90.0, 5.0)

        st.markdown("**Touches / fatigue / error**")
        ordinary_touches = st.number_input("Ordinary touches / order", 0.1, 50.0, 3.0, 0.5)
        balance_extra = st.number_input("Extra touches when balance exists", 0.0, 50.0, 4.0, 0.5)
        fatigue_touch = st.number_input("Fatigue points / touch", 0.0, 50.0, 1.0, 0.5)
        fatigue_wip = st.number_input("Fatigue points / WIP cycle", 0.0, 50.0, 2.5, 0.5)
        error_pct = st.number_input("Error probability / touch (%)", 0.0, 100.0, 1.5, 0.1)

        st.markdown("**Storage / shared constants**")
        wip_dwell = st.number_input("Average WIP dwell (days)", 0.1, 365.0, 3.0, 0.5)
        material_cost = st.number_input("Packaging material cost / box (INR)", 0.0, 10000.0, 25.0, 1.0)
        boxes_pallet = st.number_input("Boxes / pallet", 1.0, 1000.0, 32.0, 1.0)
        footprint = st.number_input("Pallet footprint (sq ft)", 0.01, 1000.0, 16.200625, 0.000001, format="%.6f")
        bin_capacity = st.number_input("Bin capacity (belts / bin)", 1.0, 100000.0, 300.0, 10.0)
        bins_pallet = st.number_input("Bins / pallet", 0.1, 100.0, 1.0, 0.5)
        share_bins = st.checkbox("MTO balance can share bins", True)
        stick_pack = st.checkbox("One SKU per proposed stick-pack box", True)
        staging_dwell = st.number_input("Dispatch staging dwell (days)", 0.1, 365.0, 2.0, 0.5)

    run_physical = st.button("▶ Run Physical Twin", type="primary", use_container_width=True)
    run_process = st.button("▶ Run Process Twin", use_container_width=True)
    if st.button("↺ Reset", use_container_width=True):
        reset_results()
        st.rerun()


# CAD review and the requested custom-door controls below the drawing.
if cad_file:
    key = f"{cad_file.name}:{cad_file.size}"
    if st.session_state.get("cad_key") != key:
        try:
            st.session_state.cad_path = save_upload(cad_file)
            st.session_state.cad_data = prepare_cad(st.session_state.cad_path)
            st.session_state.cad_key = key
            for k in ("door_x", "door_y", "door_reference", "selected_door"):
                st.session_state.pop(k, None)
        except Exception as exc:
            st.error(f"CAD could not be read: {exc}")
            st.stop()

if "cad_data" in st.session_state:
    cad = st.session_state.cad_data
    with st.expander("CAD review & operating door", expanded=True):
        a, b, c = st.columns(3)
        a.metric("Warehouse area", f"{cad['warehouse'].area:,.1f} m²")
        b.metric("CAD doors", len(cad.get("doors", [])))
        c.metric("Obstacles", len(cad.get("obstacles", [])))
        st.pyplot(plot_cad_geometry(cad), use_container_width=True)
        st.session_state.selected_door = operating_door(cad)


if run_physical:
    if "cad_path" not in st.session_state or not demand_file:
        st.error("Upload CAD + Daily demand first.")
        st.stop()
    if "selected_door" not in st.session_state:
        st.error("Set the operating/custom door below the CAD drawing.")
        st.stop()
    try:
        door = st.session_state.selected_door
        with st.spinner("Running Physical Twin…"):
            result = run_pipeline(
                st.session_state.cad_path, save_upload(demand_file),
                manual_door_xy=(door.x, door.y),
                pallet_width_m=pallet_width, pallet_depth_m=pallet_depth,
                wall_clearance_m=wall_clearance, main_aisle_m=main_aisle,
                cross_aisle_m=cross_aisle, turning_diameter_m=turning_diameter,
                target_occupancy_pct=occupancy, forklift_speed_mps=forklift_speed,
                handling_time_sec_per_pallet=handling_time,
            )
        result["config"] = {
            "pallet_width_m": pallet_width, "pallet_depth_m": pallet_depth,
            "wall_clearance_m": wall_clearance, "main_aisle_m": main_aisle,
            "cross_aisle_m": cross_aisle, "turning_diameter_m": turning_diameter,
            "target_occupancy_pct": occupancy, "forklift_speed_mps": forklift_speed,
            "handling_time_sec_per_pallet": handling_time,
            "operating_door_x_m": door.x, "operating_door_y_m": door.y,
        }
        st.session_state.result = result
        st.success("Physical Twin completed.")
    except Exception as exc:
        st.error(f"Physical Twin failed: {exc}")
        st.stop()


if run_process:
    if not mto_file:
        st.error("Upload the raw MTO transaction file first.")
        st.stop()
    cfg = ActualMTOConfig(
        default_box_qty=default_box, conflict_box_qty=conflict_box,
        round_non_integer_belts=round_qty, pick_time_sec=pick,
        count_time_sec=count, pack_time_sec=pack,
        wip_store_time_sec=store, wip_retrieve_time_sec=retrieve,
        ordinary_touches_per_order=ordinary_touches, balance_extra_touches=balance_extra,
        fatigue_points_per_touch=fatigue_touch, fatigue_points_per_wip_cycle=fatigue_wip,
        error_probability_per_touch_pct=error_pct, wip_avg_dwell_days=wip_dwell,
        material_cost_per_box_inr=material_cost, boxes_per_pallet=boxes_pallet,
        pallet_footprint_sqft=footprint, bin_capacity_belts=bin_capacity,
        bins_per_pallet=bins_pallet, mto_balance_bins_can_share=share_bins,
        stick_pack_one_sku_per_box=stick_pack, staging_dwell_days=staging_dwell,
    )
    try:
        with st.spinner("Reading raw MTO → resolving box standards → calculating boxes → simulating…"):
            detail, master = prepare_actual_mto(
                save_upload(mto_file), save_upload(box_file) if box_file else None, cfg
            )
            model2 = simulate_actual_mto(detail, cfg)

        mta_belts = 0.0
        if mta_file:
            mta_path = save_upload(mta_file)
            candidates = [pd.read_csv(mta_path)] if mta_path.lower().endswith(".csv") else [
                pd.read_excel(mta_path, sheet_name=s) for s in pd.ExcelFile(mta_path).sheet_names
            ]
            for xdf in candidates:
                cols = {str(c).strip().upper(): c for c in xdf.columns}
                qcol = next((cols[c] for c in ("NO_OF_BELTS", "BELTS", "QUANTITY") if c in cols), None)
                if qcol:
                    mta_belts = pd.to_numeric(xdf[qcol], errors="coerce").fillna(0).clip(lower=0).sum()
                    break

        storage_df = build_storage_paradox_actual(model2, mta_belts, cfg)
        model2["master"] = master
        model2["storage_df"] = storage_df
        model2["storage"] = dict(zip(storage_df["Metric"], storage_df["Value"]))
        model2["config"] = asdict(cfg)
        model2["physical_context"] = st.session_state.get("result", {}).get("config", {})
        st.session_state.model2 = model2
        st.success(f"Process Twin completed: {len(detail):,} raw transaction lines processed.")
    except Exception as exc:
        st.error(f"Process Twin failed: {exc}")
        st.stop()


if "result" not in st.session_state and "model2" not in st.session_state:
    st.info("Upload the inputs and run either model from the sidebar.")
    st.stop()

result = st.session_state.get("result")
model2 = st.session_state.get("model2")
tabs = st.tabs(["Overview", "Physical Twin", "Process Twin", "Decision Workspace", "Downloads"])

with tabs[0]:
    st.markdown("### Executive overview")
    if result:
        cap, sim, dm = result["capacity"], result["simulation_summary"], result["demand_metrics"]
        k = st.columns(5)
        k[0].metric("Planning capacity", f"{cap['planning_capacity']:,.0f}")
        k[1].metric("Realistic capacity", f"{cap['realistic_capacity']:,}")
        k[2].metric("Avg demand", f"{dm['average_daily_demand']:.1f}")
        k[3].metric("Peak demand", f"{dm['peak_demand']:.0f}")
        k[4].metric("Overflow days", f"{sim['overflow_days']}")
    if model2:
        s = model2["summary"]
        st.markdown("#### Model 2 · Raw-data output")
        k = st.columns(6)
        k[0].metric("MTO lines", f"{s['orders']:,}")
        k[1].metric("Belts", f"{s['belts']:,.0f}")
        k[2].metric("Boxes calculated", f"{s['boxes']:,}")
        k[3].metric("Balance lines", f"{s['balance_orders']:,}")
        k[4].metric("Time saved", f"{s['time_saved_pct']:.1f}%")
        k[5].metric("Fatigue reduction", f"{s['fatigue_reduction_pct']:.1f}%")
        st.success(
            f"Boxes are calculated automatically from belt quantity + box standard. "
            f"Current result: **{s['boxes']:,} boxes**; expected error exposure reduction: **{s['error_reduction_pct']:.1f}%**."
        )

with tabs[1]:
    if not result:
        st.info("Run the Physical Twin.")
    else:
        r, cap, sim = result, result["capacity"], result["simulation_summary"]
        st.markdown("### Model 1 · Physical Warehouse Twin")
        k = st.columns(4)
        k[0].metric("Theoretical capacity", f"{cap['theoretical_capacity']:,}")
        k[1].metric("Realistic capacity", f"{cap['realistic_capacity']:,}")
        k[2].metric("Planning capacity", f"{cap['planning_capacity']:,.0f}")
        k[3].metric("Planning occupancy", f"{cap['planning_occupancy_pct']:.0f}%")
        st.pyplot(plot_layout(
            r["warehouse"], r["storage_region"], r["slots"], r["door"],
            r["winner"]["layout"]["main_aisle"], r["winner"]["layout"]["turning_circle"],
            r["obstacles"], title="Optimized Warehouse Layout"
        ), use_container_width=True)
        a, b, c, d = st.columns(4)
        a.metric("Average distance", f"{r['winner']['avg_distance_m']:.2f} m")
        b.metric("Average handling time", f"{sim['average_time_min']:.2f} min")
        c.metric("Average fatigue proxy", f"{sim['average_fatigue']:.2f}")
        d.metric("Overflow days", f"{sim['overflow_days']}")
        st.caption(f"Operating door used: X={r['door'].x:.2f} m, Y={r['door'].y:.2f} m")

with tabs[2]:
    if not model2:
        st.info("Run the Process Twin.")
    else:
        s, storage = model2["summary"], model2["storage"]
        st.markdown("### Model 2 · Transaction-level MTO Process Twin")
        st.caption("Current: pick → count/pack → shared temporary WIP → retrieve/rehandle → dispatch")
        st.caption("Proposed: pick → single-SKU stick pack → dispatch staging → dispatch")
        k = st.columns(6)
        k[0].metric("MTO lines", f"{s['orders']:,}")
        k[1].metric("Belts", f"{s['belts']:,.0f}")
        k[2].metric("Boxes calculated", f"{s['boxes']:,}")
        k[3].metric("Time reduction", f"{s['time_saved_pct']:.1f}%")
        k[4].metric("Fatigue reduction", f"{s['fatigue_reduction_pct']:.1f}%")
        k[5].metric("Error exposure reduction", f"{s['error_reduction_pct']:.1f}%")

        st.markdown("#### Automatic box calculation")
        a, b, c, d = st.columns(4)
        a.metric("Balance lines", f"{s['balance_orders']:,}")
        a.caption(f"{s['balance_pct']:.1f}% of lines")
        b.metric("Cumulative WIP", f"{s['wip_belts']:,.0f} belts")
        c.metric("Total boxes", f"{s['boxes']:,}")
        d.metric("Material cost", f"₹{s['boxes'] * model2['config']['material_cost_per_box_inr']:,.0f}")
        st.caption("No box count is entered manually: the engine calculates Full Boxes + Balance Box for every raw transaction.")

        st.markdown("#### Storage trade-off")
        a, b, c = st.columns(3)
        a.metric("Current combined slots", f"{storage['Combined current pallet slots']:,.0f}")
        b.metric("Proposed combined slots", f"{storage['Combined proposed pallet slots']:,.0f}")
        c.metric("Net slot change", f"{storage['Net pallet-slot change']:+,.0f}")
        with st.expander("Transaction-level audit"):
            st.dataframe(model2["detail"], use_container_width=True, hide_index=True)
        with st.expander("Daily process results"):
            st.dataframe(model2["daily"], use_container_width=True, hide_index=True)
        with st.expander("Resolved box-standard mapping"):
            st.dataframe(model2["master"], use_container_width=True, hide_index=True)
        with st.expander("Storage paradox calculation"):
            st.dataframe(model2["storage_df"], use_container_width=True, hide_index=True)
        with st.expander("Assumptions used in this run"):
            st.dataframe(pd.DataFrame({"Assumption": list(model2["config"].keys()), "Value": list(model2["config"].values())}), use_container_width=True, hide_index=True)

with tabs[3]:
    st.markdown("### Decision Workspace")
    if result:
        cap, dm, sim = result["capacity"], result["demand_metrics"], result["simulation_summary"]
        st.markdown("**Physical feasibility**")
        st.write(f"Planning capacity = **{cap['planning_capacity']:,.0f} slots**; peak demand = **{dm['peak_demand']:.0f} pallets**; historical overflow days = **{sim['overflow_days']}**.")
    if model2:
        s = model2["summary"]
        st.markdown("**Process benefit**")
        st.write(f"**{s['balance_pct']:.1f}%** of MTO lines contain a balance. The model estimates **{s['time_saved_pct']:.1f}%** lower processing time, **{s['fatigue_reduction_pct']:.1f}%** lower fatigue proxy and **{s['error_reduction_pct']:.1f}%** lower expected error exposure.")
        st.markdown("**Storage trade-off**")
        st.write(f"Combined modelled storage changes from **{model2['storage']['Combined current pallet slots']:,.0f}** to **{model2['storage']['Combined proposed pallet slots']:,.0f}** slots.")
        st.warning("These time, fatigue and error figures are modelled proxies until time-study and actual error-log data are collected.")

with tabs[4]:
    st.markdown("### Downloads")
    if result:
        st.download_button("Download Model 1 daily results", result["daily_df"].to_csv(index=False).encode(), "model1_daily_simulation.csv", "text/csv", use_container_width=True)
    if model2:
        st.download_button("Download Model 2 transaction audit", model2["detail"].to_csv(index=False).encode(), "model2_transaction_box_audit.csv", "text/csv", use_container_width=True)
        st.download_button("Download Model 2 KPI table", build_actual_kpi_table(model2).to_csv(index=False).encode(), "model2_kpis.csv", "text/csv", use_container_width=True)
        st.download_button("Download Model 2 storage analysis", model2["storage_df"].to_csv(index=False).encode(), "model2_storage_analysis.csv", "text/csv", use_container_width=True)
        st.download_button("Download Model 2 assumptions", pd.DataFrame({"Assumption": list(model2["config"].keys()), "Value": list(model2["config"].values())}).to_csv(index=False).encode(), "model2_assumptions.csv", "text/csv", use_container_width=True)

st.divider()
st.caption("JK Fenner Poly-V Warehouse Digital Twin · Model 1 = layout · Model 2 = MTO process/WIP · Decision Workspace = integrated view")
