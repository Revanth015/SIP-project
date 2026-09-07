"""Universal Warehouse Digital Twin — manager-facing Streamlit application."""
from pathlib import Path
import tempfile

import pandas as pd
import streamlit as st
from shapely.geometry import Point

from core.pipeline import run_pipeline, prepare_cad
from core.visualization_engine import plot_cad_geometry, plot_layout, create_daily_gif
from core.export_engine import build_kpi_table, export_excel
from core.mto_actual_engine import (
    ActualMTOConfig,
    prepare_actual_mto,
    simulate_actual_mto,
    build_actual_kpi_table,
    build_storage_paradox_actual,
)

st.set_page_config(page_title="Warehouse Digital Twin", page_icon="🏭", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
.block-container {padding-top: 1.5rem; padding-bottom: 2rem;}
.hero {padding: .4rem 0 1rem;}
.hero h1 {font-size: 2.35rem; margin: 0; letter-spacing: -.03em;}
.hero p {margin: .25rem 0 0; color: #6b7280; font-size: 1rem;}
.eyebrow {font-size: .75rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: #6b7280;}
.status-card {border: 1px solid #e5e7eb; border-radius: 12px; padding: 1rem; background: #fafafa;}
.small-note {font-size: .82rem; color: #6b7280;}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="hero"><div class="eyebrow">Decision-support prototype</div><h1>🏭 Warehouse Digital Twin</h1><p>Physical warehouse capacity + transaction-level MTO process analysis in one workspace.</p></div>', unsafe_allow_html=True)


def save_uploaded(upload, suffix=None):
    suffix = suffix or Path(upload.name).suffix.lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(upload.getbuffer())
    tmp.close()
    return tmp.name


def clear_results():
    for key in ("result", "model2_actual", "gif_bytes", "selected_door"):
        st.session_state.pop(key, None)


def get_manual_door(cad):
    warehouse = cad["warehouse"]
    minx, miny, maxx, maxy = warehouse.bounds
    doors = cad.get("doors", [])
    if doors:
        labels = [f"CAD Door {i}: ({d['x']:.2f}, {d['y']:.2f})" for i, d in enumerate(doors, 1)]
        selected = st.selectbox("Reference door", labels, key="door_reference")
        ref = doors[labels.index(selected)]
        default_x, default_y = float(ref["x"]), float(ref["y"])
    else:
        default_x, default_y = float(warehouse.centroid.x), float(warehouse.centroid.y)
        st.caption("No CAD door was detected. Enter the validated operating-door coordinates manually.")
    c1, c2 = st.columns(2)
    with c1:
        x = st.number_input("Operating door X (m)", min_value=float(minx), max_value=float(maxx), value=float(st.session_state.get("door_x", default_x)), step=0.1, key="door_x")
    with c2:
        y = st.number_input("Operating door Y (m)", min_value=float(miny), max_value=float(maxy), value=float(st.session_state.get("door_y", default_y)), step=0.1, key="door_y")
    return Point(x, y)


with st.sidebar:
    st.header("Project inputs")
    st.caption("Upload the files once. Model 1 and Model 2 share the same warehouse context.")
    cad_file = st.file_uploader("1 · Warehouse CAD", type=["dxf"], key="cad_upload")
    demand_file = st.file_uploader("2 · Daily demand", type=["xlsx", "csv"], key="demand_upload")
    mto_file = st.file_uploader("3 · MTO transactions", type=["xlsx"], key="mto_upload_global")
    box_file = st.file_uploader("4 · Size / Box Master", type=["xlsx"], key="box_upload_global")

    st.divider()
    st.subheader("Core physical settings")
    pallet_width = st.number_input("Pallet width (m)", min_value=0.1, value=1.20, step=0.05)
    pallet_depth = st.number_input("Pallet depth (m)", min_value=0.1, value=1.00, step=0.05)
    occupancy = st.slider("Planning occupancy (%)", 50, 85, 65)

    with st.expander("Advanced engineering constraints"):
        wall_clearance = st.number_input("Wall clearance (m)", min_value=0.0, value=0.25, step=0.05)
        main_aisle = st.number_input("Main aisle (m)", min_value=0.5, value=4.0, step=0.25)
        cross_aisle = st.number_input("Cross aisle (m)", min_value=0.0, value=2.0, step=0.25)
        turning_diameter = st.number_input("Turning diameter (m)", min_value=0.0, value=7.0, step=0.25)
        forklift_speed = st.number_input("Forklift speed (m/s)", min_value=0.1, value=2.5, step=0.1)
        handling_time = st.number_input("Handling time (sec/pallet)", min_value=0.0, value=0.0, step=5.0)

    run_physical = st.button("▶ Run Physical Twin", type="primary", use_container_width=True)
    if st.button("↺ Reset analysis", use_container_width=True):
        clear_results()
        st.rerun()


if cad_file:
    key = f"{cad_file.name}:{cad_file.size}"
    if st.session_state.get("cad_preview_key") != key:
        try:
            st.session_state.cad_path = save_uploaded(cad_file, ".dxf")
            st.session_state.cad_data = prepare_cad(st.session_state.cad_path)
            st.session_state.cad_preview_key = key
            for k in ["door_x", "door_y", "door_reference"]:
                st.session_state.pop(k, None)
            clear_results()
        except Exception as exc:
            st.error(f"CAD could not be read: {exc}")
            st.stop()

if "cad_data" in st.session_state:
    cad = st.session_state.cad_data
    with st.expander("CAD review & operating door", expanded=True):
        a, b, c = st.columns(3)
        a.metric("Warehouse area", f"{cad['warehouse'].area:,.1f} m²")
        b.metric("CAD doors detected", f"{len(cad.get('doors', []))}")
        c.metric("Obstacles detected", f"{len(cad.get('obstacles', []))}")
        st.pyplot(plot_cad_geometry(cad), use_container_width=True)
        door = get_manual_door(cad)
        st.session_state.selected_door = door
        st.caption(f"Operating door: **X = {door.x:.3f} m · Y = {door.y:.3f} m**")


if run_physical:
    if "cad_path" not in st.session_state or not demand_file:
        st.error("Upload the Warehouse CAD and Daily demand files first.")
        st.stop()
    if "selected_door" not in st.session_state:
        st.error("Set the operating door before running the Physical Twin.")
        st.stop()
    try:
        demand_path = save_uploaded(demand_file)
        door = st.session_state.selected_door
        with st.spinner("Running CAD → layout → capacity → daily simulation…"):
            result = run_pipeline(
                st.session_state.cad_path, demand_path,
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
        st.session_state.pop("model2_actual", None)
        st.success("Physical Digital Twin completed.")
    except Exception as exc:
        st.error(f"Physical Digital Twin could not be completed: {exc}")
        st.stop()


if "result" not in st.session_state:
    st.markdown("### Start here")
    cols = st.columns(4)
    statuses = [
        ("Physical model", "Ready" if cad_file and demand_file else "Needs CAD + demand"),
        ("Process model", "Ready" if mto_file and box_file else "Needs MTO + box master"),
        ("Shared context", "CAD + operating door + physical constraints"),
        ("Decision output", "Available after the models are run"),
    ]
    for col, (title, text) in zip(cols, statuses):
        with col:
            st.markdown(f'<div class="status-card"><b>{title}</b><br><span class="small-note">{text}</span></div>', unsafe_allow_html=True)
    st.info("Use the sidebar to upload the project files, review the CAD, set the operating door, and run the Physical Twin. Model 2 can then be run from the Process Twin tab.")
    st.stop()


r = st.session_state.result
cap = r["capacity"]
sim = r["simulation_summary"]
dm = r["demand_metrics"]
door = r["door"]

overview_tab, physical_tab, process_tab, decision_tab, downloads_tab = st.tabs([
    "Overview", "Physical Twin", "Process Twin", "Decision Workspace", "Downloads"
])

with overview_tab:
    st.markdown("### Executive overview")
    k = st.columns(6)
    k[0].metric("Realistic capacity", f"{cap['realistic_capacity']:,} slots")
    k[1].metric("Planning capacity", f"{cap['planning_capacity']:,.0f} slots")
    k[2].metric("Avg daily demand", f"{dm['average_daily_demand']:.1f}")
    k[3].metric("Peak demand", f"{dm['peak_demand']:.0f}")
    k[4].metric("Overflow days", f"{sim['overflow_days']}")
    k[5].metric("Avg fatigue proxy", f"{sim['average_fatigue']:.2f}")

    left, right = st.columns([1.55, 1])
    with left:
        st.markdown("#### Optimized warehouse layout")
        st.pyplot(plot_layout(r["warehouse"], r["storage_region"], r["slots"], r["door"], r["winner"]["layout"]["main_aisle"], r["winner"]["layout"]["turning_circle"], r["obstacles"], title="Optimized Warehouse Layout"), use_container_width=True)
    with right:
        st.markdown("#### What the model says")
        st.info(
            f"The optimized scenario generates **{len(r['slots']):,} feasible slots**. "
            f"The 95th-percentile demand is **{dm['p95_demand']:.1f}**, while peak demand reaches **{dm['peak_demand']:.0f}**. "
            "Peak-day buffering is therefore a separate management question rather than a reason to over-size the base layout."
        )
        a, b = st.columns(2)
        a.metric("Average slot distance", f"{r['winner']['avg_distance_m']:.2f} m")
        b.metric("Warehouse area", f"{r['warehouse'].area:,.0f} m²")
        st.caption(f"Operating door: X={door.x:.2f} m, Y={door.y:.2f} m")

with physical_tab:
    st.markdown("### Model 1 · Physical Warehouse Twin")
    a, b, c, d = st.columns(4)
    a.metric("Theoretical capacity", f"{cap['theoretical_capacity']:,}")
    b.metric("Realistic capacity", f"{cap['realistic_capacity']:,}")
    c.metric("Planning capacity", f"{cap['planning_capacity']:,.0f}")
    d.metric("Planning occupancy", f"{cap['planning_occupancy_pct']:.0f}%")
    st.pyplot(plot_layout(r["warehouse"], r["storage_region"], r["slots"], r["door"], r["winner"]["layout"]["main_aisle"], r["winner"]["layout"]["turning_circle"], r["obstacles"], title="Optimized Warehouse Layout"), use_container_width=True)
    a, b, c, d = st.columns(4)
    a.metric("Average distance", f"{r['winner']['avg_distance_m']:.2f} m")
    b.metric("Average handling time", f"{sim['average_time_min']:.2f} min")
    c.metric("Average fatigue proxy", f"{sim['average_fatigue']:.2f}")
    d.metric("Overflow days", f"{sim['overflow_days']}")
    with st.expander("Daily simulation evidence"):
        st.dataframe(r["daily_df"], use_container_width=True, hide_index=True)
    with st.expander("Technical capacity, slot and layout-search evidence"):
        st.dataframe(r["capacity_table"], use_container_width=True, hide_index=True)
        st.dataframe(r["slot_df"], use_container_width=True, hide_index=True)
        st.dataframe(r["search_df"], use_container_width=True, hide_index=True)

with process_tab:
    st.markdown("### Model 2 · Transaction-level MTO Process Twin")
    st.caption("Model 2 inherits the physical warehouse context from Model 1 and uses actual MTO transactions for handling, WIP and storage analysis.")
    if not (mto_file and box_file):
        st.warning("Upload the MTO transaction file and Size / Box Master in the sidebar to activate the Process Twin.")
    else:
        st.markdown("**Current:** pick → count/pack → temporary WIP → retrieve/rehandle → dispatch")
        st.markdown("**Proposed:** pick → single-SKU stick pack → temporary staging → same-SKU completion → dispatch")
        cfg0 = ActualMTOConfig()
        with st.expander("Model 2 assumptions & data rules"):
            a, b, c, d = st.columns(4)
            with a:
                default_box = st.number_input("Default belts / box", 1.0, 10000.0, float(cfg0.default_box_qty), 1.0)
                current_touches = st.number_input("Current touches / box", 0.1, 100.0, float(cfg0.current_touches_per_box), 0.5)
                proposed_touches = st.number_input("Proposed touches / box", 0.1, 100.0, float(cfg0.proposed_touches_per_box), 0.5)
            with b:
                conflict_box = st.number_input("Conflict fallback / box", 1.0, 10000.0, float(cfg0.conflict_box_qty), 1.0)
                current_pack = st.number_input("Current pack (sec)", 0.0, 3600.0, float(cfg0.current_pack_time_sec), 5.0)
                proposed_pack = st.number_input("Proposed pack (sec)", 0.0, 3600.0, float(cfg0.proposed_pack_time_sec), 5.0)
            with c:
                current_count = st.number_input("Current count (sec)", 0.0, 3600.0, float(cfg0.current_count_time_sec), 5.0)
                proposed_count = st.number_input("Proposed count (sec)", 0.0, 3600.0, float(cfg0.proposed_count_time_sec), 5.0)
                box_area = st.number_input("Box footprint (m²)", 0.001, 100.0, float(cfg0.box_footprint_m2), 0.01)
            with d:
                current_store = st.number_input("Current store (sec)", 0.0, 3600.0, float(cfg0.current_store_time_sec), 5.0)
                current_retrieve = st.number_input("Current retrieve (sec)", 0.0, 3600.0, float(cfg0.current_retrieve_time_sec), 5.0)
                wip_multiplier = st.number_input("WIP location multiplier", 0.1, 10.0, float(cfg0.wip_location_multiplier), 0.1)
            e, f, g = st.columns(3)
            with e:
                proposed_store = st.number_input("Proposed store (sec)", 0.0, 3600.0, float(cfg0.proposed_store_time_sec), 5.0)
                proposed_retrieve = st.number_input("Proposed retrieve (sec)", 0.0, 3600.0, float(cfg0.proposed_retrieve_time_sec), 5.0)
            with f:
                err_prob = st.number_input("Error probability / touch (%)", 0.0, 100.0, float(cfg0.error_probability_per_touch_pct), 0.1)
                fat_touch = st.number_input("Fatigue points / touch", 0.0, 100.0, float(cfg0.fatigue_points_per_touch), 0.1)
            with g:
                fat_wip = st.number_input("Fatigue points / WIP cycle", 0.0, 100.0, float(cfg0.fatigue_points_per_wip_cycle), 0.1)
                staging_area = st.number_input("Approved staging area (m²)", 0.0, 100000.0, 0.0, 1.0, help="Leave at 0 when no measured/approved staging limit is available.")
            st.caption("Fatigue and error outputs are proxies. Box standards come from the uploaded master; missing, zero and conflicting standards follow the configured fallback rules.")

        if st.button("▶ Run Process Twin", type="primary"):
            try:
                cfg = ActualMTOConfig(
                    default_box_qty=default_box, conflict_box_qty=conflict_box,
                    current_touches_per_box=current_touches, proposed_touches_per_box=proposed_touches,
                    current_pack_time_sec=current_pack, proposed_pack_time_sec=proposed_pack,
                    current_count_time_sec=current_count, proposed_count_time_sec=proposed_count,
                    current_store_time_sec=current_store, proposed_store_time_sec=proposed_store,
                    current_retrieve_time_sec=current_retrieve, proposed_retrieve_time_sec=proposed_retrieve,
                    error_probability_per_touch_pct=err_prob, fatigue_points_per_touch=fat_touch,
                    fatigue_points_per_wip_cycle=fat_wip, box_footprint_m2=box_area,
                    wip_location_multiplier=wip_multiplier,
                )
                with st.spinner("Processing actual MTO transactions and simulating SKU-level WIP…"):
                    detail, master = prepare_actual_mto(save_uploaded(mto_file, ".xlsx"), save_uploaded(box_file, ".xlsx"), cfg)
                    daily2, wip_detail, summary = simulate_actual_mto(detail, cfg)
                st.session_state.model2_actual = {"detail": detail, "master": master, "daily": daily2, "wip_detail": wip_detail, "summary": summary, "config": cfg, "staging_area_m2": staging_area}
                st.success(f"Process Twin completed: {len(detail):,} transaction rows processed.")
            except Exception as exc:
                st.error(f"Process Twin could not be completed: {exc}")

        if "model2_actual" in st.session_state:
            m2 = st.session_state.model2_actual
            sm = m2["summary"]
            st.markdown("#### Process impact")
            k = st.columns(6)
            k[0].metric("Processing time", f"{sm['time_reduction_pct']:.1f}% ↓")
            k[1].metric("Manual touches", f"{sm['touch_reduction_pct']:.1f}% ↓")
            k[2].metric("Fatigue proxy", f"{sm['fatigue_reduction_pct']:.1f}% ↓")
            k[3].metric("Error exposure", f"{sm['error_risk_reduction_pct']:.1f}% ↓")
            k[4].metric("Peak temporary WIP", f"{sm['peak_closing_wip_boxes']:.0f} boxes")
            k[5].metric("Peak WIP space", f"{sm['peak_closing_wip_storage_m2']:.1f} m²")
            st.dataframe(build_actual_kpi_table(sm), use_container_width=True, hide_index=True)
            left, right = st.columns([1.5, 1])
            with left:
                if not m2["daily"].empty:
                    chart = m2["daily"].set_index("DATE")[["OPENING_WIP_BOXES", "NEW_PARTIAL_WIP_BOXES", "BOXES_COMPLETED_FROM_WIP", "CLOSING_WIP_BOXES"]]
                    st.line_chart(chart, use_container_width=True)
            with right:
                peak = float(sm["peak_closing_wip_storage_m2"])
                if m2["staging_area_m2"] > 0:
                    st.metric("Staging headroom", f"{m2['staging_area_m2'] - peak:+.1f} m²")
                else:
                    st.metric("Staging limit", "Not specified")
                st.metric("Avg closing WIP", f"{sm['average_closing_wip_boxes']:.1f} boxes")
            with st.expander("Storage paradox & WIP evidence"):
                st.dataframe(build_storage_paradox_actual(sm, m2["config"].box_footprint_m2), use_container_width=True, hide_index=True)
                st.dataframe(m2["daily"], use_container_width=True, hide_index=True)
                if m2["wip_detail"].empty:
                    st.success("No SKU-level closing WIP remains at the end of the supplied history.")
                else:
                    st.dataframe(m2["wip_detail"], use_container_width=True, hide_index=True)
            with st.expander("Data-quality evidence"):
                q = st.columns(5)
                q[0].metric("Transactions", f"{sm['transactions']:,}")
                q[1].metric("Total belts", f"{sm['total_belts']:,.0f}")
                q[2].metric("Master-standard rows", f"{sm['master_standard_rows']:,}")
                q[3].metric("Defaulted rows", f"{sm['default_missing_rows'] + sm['default_zero_rows']:,}")
                q[4].metric("Conflict rows", f"{sm['default_conflict_rows']:,}")
                st.dataframe(m2["detail"].head(500), use_container_width=True, hide_index=True)
                st.caption("First 500 transaction rows shown. Complete transaction evidence is available in the export.")

with decision_tab:
    st.markdown("### Managerial decision workspace")
    st.caption("Physical feasibility, process benefit and temporary-storage constraints are shown separately. This is decision support, not an autonomous approval.")
    if "model2_actual" not in st.session_state:
        st.info("Run the Process Twin to complete the integrated decision view.")
    else:
        m2 = st.session_state.model2_actual
        sm = m2["summary"]
        peak_area = float(sm["peak_closing_wip_storage_m2"])
        stage = float(m2["staging_area_m2"])
        left, right = st.columns(2)
        with left:
            st.markdown("#### Model 1 · Physical reality")
            a, b = st.columns(2)
            a.metric("Realistic capacity", f"{cap['realistic_capacity']:,}")
            b.metric("Planning capacity", f"{cap['planning_capacity']:,.0f}")
            a.metric("Peak demand", f"{dm['peak_demand']:.0f}")
            b.metric("Overflow days", f"{sim['overflow_days']}")
        with right:
            st.markdown("#### Model 2 · Process reality")
            a, b = st.columns(2)
            a.metric("Time improvement", f"{sm['time_reduction_pct']:.1f}%")
            b.metric("Touch improvement", f"{sm['touch_reduction_pct']:.1f}%")
            a.metric("Peak temporary WIP", f"{sm['peak_closing_wip_boxes']:.0f} boxes")
            b.metric("Peak WIP space", f"{peak_area:.1f} m²")
        if stage <= 0:
            decision = "🟡 OPERATIONAL VALIDATION REQUIRED"
            reason = "Process benefits are modelled, but no approved staging-area limit has been supplied. Validate WIP space and operating feasibility before implementation."
        elif peak_area <= stage:
            decision = "🟢 PILOT / VALIDATE"
            reason = "The model shows process benefit and the modelled peak temporary-WIP requirement fits the supplied staging constraint. Validate time, error and handling assumptions in a controlled pilot."
        else:
            decision = "🟠 REDESIGN / CONSTRAIN"
            reason = "The model shows process benefit, but peak temporary WIP exceeds the supplied staging constraint. Redesign the WIP policy, staging arrangement or operating parameters before implementation."
        st.markdown(f"## {decision}")
        st.info(reason)
        checklist = pd.DataFrame([
            ["Physical layout", "Realistic capacity and access constraints", "Modelled", "Validate on floor"],
            ["Process time", "Current vs proposed handling time", "Modelled proxy", "Time-and-motion study"],
            ["Error exposure", "Expected error exposure from touch probability", "Modelled proxy", "Collect actual error logs"],
            ["Fatigue", "Relative workload proxy", "Modelled proxy", "Validate ergonomically"],
            ["Temporary WIP", f"Peak requirement = {peak_area:.1f} m²", "Modelled from transactions", "Confirm staging capacity"],
        ], columns=["Dimension", "Finding", "Evidence type", "Next validation"])
        st.dataframe(checklist, use_container_width=True, hide_index=True)

with downloads_tab:
    st.markdown("### Evidence & downloads")
    out = Path(tempfile.mkdtemp())
    excel_path = out / "Universal_Warehouse_Digital_Twin.xlsx"
    kpi = build_kpi_table(cap, dm, sim, r["winner"])
    export_excel(excel_path, kpi, r["capacity_table"], r["daily_df"], r["occupancy_df"], r["slot_df"], r["search_df"], r["config"], dm)
    st.download_button("⬇ Download Physical Twin workbook", data=excel_path.read_bytes(), file_name=excel_path.name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    if "model2_actual" in st.session_state:
        m2 = st.session_state.model2_actual
        p2_path = out / "Model_2_Actual_MTO_Analysis.xlsx"
        with pd.ExcelWriter(p2_path, engine="openpyxl") as writer:
            build_actual_kpi_table(m2["summary"]).to_excel(writer, sheet_name="Process_KPIs", index=False)
            m2["daily"].to_excel(writer, sheet_name="Daily_WIP", index=False)
            m2["wip_detail"].to_excel(writer, sheet_name="Open_WIP_by_SKU", index=False)
            m2["detail"].to_excel(writer, sheet_name="Transaction_Box_Detail", index=False)
            m2["master"].to_excel(writer, sheet_name="Box_Master_Cleaned", index=False)
            build_storage_paradox_actual(m2["summary"], m2["config"].box_footprint_m2).to_excel(writer, sheet_name="Storage_Paradox", index=False)
            pd.DataFrame([r["config"]]).to_excel(writer, sheet_name="Model1_Physical_Context", index=False)
        st.download_button("⬇ Download Process Twin + physical context", data=p2_path.read_bytes(), file_name=p2_path.name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    with st.expander("Generate daily simulation GIF"):
        if st.button("🎞 Generate GIF"):
            gif_path = out / "Daily_Warehouse_Simulation.gif"
            create_daily_gif(r["daily_df"], r["warehouse"], r["storage_region"], r["slots"], r["door"], r["winner"]["layout"]["main_aisle"], r["winner"]["layout"]["turning_circle"], r["obstacles"], gif_path)
            st.session_state.gif_bytes = gif_path.read_bytes()
        if "gif_bytes" in st.session_state:
            st.download_button("⬇ Download Daily Simulation GIF", data=st.session_state.gif_bytes, file_name="Daily_Warehouse_Simulation.gif", mime="image/gif", use_container_width=True)

st.divider()
st.caption("Universal Warehouse Digital Twin · Model 1 = physical warehouse · Model 2 = MTO process/WIP · Decision Workspace = integrated managerial view")
