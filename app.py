"""Universal Warehouse Digital Twin — Streamlit application."""
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

st.set_page_config(page_title="Universal Warehouse Digital Twin", page_icon="🏭", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
.main-title{font-size:2.2rem;font-weight:750;margin-bottom:.15rem}
.subtitle{color:#6b7280;margin-bottom:1rem}
</style>
""", unsafe_allow_html=True)
st.markdown('<div class="main-title">🏭 Universal Warehouse Digital Twin</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">CAD-assisted warehouse capacity, layout, daily simulation and MTO process decision support</div>', unsafe_allow_html=True)


def save_uploaded(upload, suffix):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(upload.getbuffer())
    tmp.close()
    return tmp.name


def get_manual_door(cad):
    warehouse = cad["warehouse"]
    minx, miny, maxx, maxy = warehouse.bounds
    st.info("The operating door is a user-controlled input. Detected CAD doors are reference only; the Digital Twin will not automatically choose one.")
    mode = st.radio("Door input method", ["Enter coordinates", "Use a detected CAD door as starting point"], horizontal=True, key="door_mode")
    if mode == "Use a detected CAD door as starting point" and cad.get("doors"):
        doors = cad["doors"]
        options = [f"CAD Door {i}: X={d['x']:.3f}, Y={d['y']:.3f} · {d['layer']}" for i, d in enumerate(doors, 1)]
        ref = st.selectbox("Reference door", options, key="reference_door")
        d = doors[options.index(ref)]
        default_x, default_y = float(d["x"]), float(d["y"])
    else:
        default_x, default_y = float(warehouse.centroid.x), float(warehouse.centroid.y)
    c1, c2 = st.columns(2)
    with c1:
        x = st.number_input("Operating Door X (m)", min_value=float(minx), max_value=float(maxx), value=float(st.session_state.get("door_x", default_x)), step=0.1, key="door_x")
    with c2:
        y = st.number_input("Operating Door Y (m)", min_value=float(miny), max_value=float(maxy), value=float(st.session_state.get("door_y", default_y)), step=0.1, key="door_y")
    return Point(x, y)


with st.sidebar:
    st.header("Project Inputs")
    cad_file = st.file_uploader("Warehouse CAD (.dxf)", type=["dxf"])
    demand_file = st.file_uploader("Daily demand (.xlsx / .csv)", type=["xlsx", "csv"])
    st.divider()
    st.subheader("Pallet")
    pallet_width = st.number_input("Width (m)", min_value=0.1, value=1.20, step=0.05)
    pallet_depth = st.number_input("Depth (m)", min_value=0.1, value=1.00, step=0.05)
    st.subheader("Operational Constraints")
    wall_clearance = st.number_input("Wall clearance (m)", min_value=0.0, value=0.25, step=0.05)
    main_aisle = st.number_input("Main aisle (m)", min_value=0.5, value=4.0, step=0.25)
    cross_aisle = st.number_input("Cross aisle (m)", min_value=0.0, value=2.0, step=0.25)
    turning_diameter = st.number_input("Turning diameter (m)", min_value=0.0, value=7.0, step=0.25)
    st.subheader("Planning")
    occupancy = st.slider("Target occupancy (%)", 60, 70, 65)
    forklift_speed = st.number_input("Forklift speed (m/s)", min_value=0.1, value=2.5, step=0.1)
    handling_time = st.number_input("Handling time (sec/pallet)", min_value=0.0, value=0.0, step=5.0)
    run = st.button("▶ Run Digital Twin", type="primary", use_container_width=True)


if cad_file:
    key = f"{cad_file.name}:{cad_file.size}"
    if st.session_state.get("cad_preview_key") != key:
        try:
            st.session_state.cad_path = save_uploaded(cad_file, ".dxf")
            st.session_state.cad_data = prepare_cad(st.session_state.cad_path)
            st.session_state.cad_preview_key = key
            for k in ["door_x", "door_y", "reference_door"]:
                st.session_state.pop(k, None)
        except Exception as e:
            st.error(f"CAD could not be read: {e}")
            st.stop()

if "cad_data" in st.session_state:
    cad = st.session_state.cad_data
    st.subheader("1 · CAD Review")
    st.caption(f"Detected warehouse layer: **{cad['warehouse_layer']}** · Area: **{cad['warehouse'].area:,.2f} m²**")
    st.pyplot(plot_cad_geometry(cad), use_container_width=True)
    st.subheader("2 · Fix Operating Door")
    door = get_manual_door(cad)
    st.session_state.selected_door = door
    st.pyplot(plot_cad_geometry({**cad, "doors": [{"x": door.x, "y": door.y, "layer": "USER_SELECTED"}]}), use_container_width=True)
    st.caption(f"**Operating door fixed at:** X = {door.x:.3f} m, Y = {door.y:.3f} m")


if run:
    if "cad_path" not in st.session_state or not demand_file:
        st.error("Upload both a warehouse DXF and a daily demand file before running the Digital Twin.")
        st.stop()
    if "selected_door" not in st.session_state:
        st.error("Set the operating door coordinates before running the Digital Twin.")
        st.stop()
    try:
        demand_path = save_uploaded(demand_file, Path(demand_file.name).suffix.lower())
        door = st.session_state.selected_door
        with st.spinner("Running CAD → layout → capacity → simulation pipeline..."):
            result = run_pipeline(
                st.session_state.cad_path, demand_path, manual_door_xy=(door.x, door.y),
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
        st.success("Digital Twin calculation completed.")
    except Exception as e:
        st.error(f"The calculation could not be completed: {e}")
        st.stop()


if "result" in st.session_state:
    r = st.session_state.result
    tabs = st.tabs(["Model 1 · Warehouse Layout", "Model 2 · Actual MTO Process", "Downloads"])

    with tabs[0]:
        st.subheader("Model 1 · Warehouse Layout Digital Twin")
        cap = r["capacity"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Theoretical Capacity", f"{cap['theoretical_capacity']:,}")
        c2.metric("Realistic Capacity", f"{cap['realistic_capacity']:,}")
        c3.metric("Planning Capacity", f"{cap['planning_capacity']:,.1f}")
        c4.metric("Planning Occupancy", f"{cap['planning_occupancy_pct']:.0f}%")
        st.dataframe(r["capacity_table"], use_container_width=True, hide_index=True)
        st.subheader("Optimized Layout")
        st.pyplot(plot_layout(r["warehouse"], r["storage_region"], r["slots"], r["door"], r["winner"]["layout"]["main_aisle"], r["winner"]["layout"]["turning_circle"], r["obstacles"], title="Optimized Warehouse Layout"), use_container_width=True)
        a, b, c, d = st.columns(4)
        a.metric("Average Slot Distance", f"{r['winner']['avg_distance_m']:.2f} m")
        b.metric("Slots Generated", f"{len(r['slots']):,}")
        c.metric("Detected Obstacles", f"{len(r['obstacles'])}")
        d.metric("Peak Demand", f"{r['demand_metrics']['peak_demand']:.0f}")
        st.subheader("Daily Simulation")
        st.dataframe(r["daily_df"], use_container_width=True, hide_index=True)
        s = r["simulation_summary"]
        a, b, c, d = st.columns(4)
        a.metric("Avg Time", f"{s['average_time_min']:.2f} min")
        b.metric("Avg Distance", f"{s['average_distance_m']:.2f} m")
        c.metric("Avg Fatigue Proxy", f"{s['average_fatigue']:.2f}")
        d.metric("Overflow Days", f"{s['overflow_days']}")
        st.subheader("Slot Distance Reference")
        st.dataframe(r["slot_df"], use_container_width=True, hide_index=True)
        st.subheader("Layout Search")
        st.dataframe(r["search_df"], use_container_width=True, hide_index=True)

    with tabs[1]:
        st.subheader("Model 2 · Actual SKU-Level MTO Process Digital Twin")
        st.markdown("**Current:** Pick belts → count/pack → temporary WIP → retrieve/rehandle → dispatch")
        st.markdown("**Proposed:** Pick MTO belts → single-SKU stick pack → temporary staging → later same-SKU completion → dispatch")
        st.info("Model 2 now uses the actual MTO transaction data. It does not convert pallets into synthetic MTO orders. For the box master: positive values are used; zero values and missing values use the default 28 belts/box; conflicting positive values are flagged and use the default 28 for the scenario.")

        mto_upload = st.file_uploader("Upload actual MTO packing-list master (.xlsx)", type=["xlsx"], key="model2_mto")
        box_upload = st.file_uploader("Upload Size / Box Master (.xlsx)", type=["xlsx"], key="model2_box")

        cfg0 = ActualMTOConfig()
        with st.expander("⚙ Model 2 editable assumptions", expanded=True):
            a1, a2, a3, a4 = st.columns(4)
            with a1:
                default_box = st.number_input("Default belts / box", 1.0, 10000.0, float(cfg0.default_box_qty), 1.0, help="Used when the master has no usable positive value, including zero or no match.")
            with a2:
                conflict_box = st.number_input("Conflict fallback / box", 1.0, 10000.0, float(cfg0.conflict_box_qty), 1.0)
            with a3:
                box_area = st.number_input("Box footprint (m²)", 0.001, 100.0, float(cfg0.box_footprint_m2), 0.01)
            with a4:
                wip_multiplier = st.number_input("WIP location multiplier", 0.1, 10.0, float(cfg0.wip_location_multiplier), 0.1)

            st.markdown("**Current vs proposed process parameters**")
            b1, b2, b3, b4 = st.columns(4)
            with b1:
                cur_touch = st.number_input("Current touches / box", 0.1, 100.0, float(cfg0.current_touches_per_box), 0.5)
                cur_pack = st.number_input("Current pack (sec)", 0.0, 3600.0, float(cfg0.current_pack_time_sec), 5.0)
            with b2:
                prop_touch = st.number_input("Proposed touches / box", 0.1, 100.0, float(cfg0.proposed_touches_per_box), 0.5)
                prop_pack = st.number_input("Proposed pack (sec)", 0.0, 3600.0, float(cfg0.proposed_pack_time_sec), 5.0)
            with b3:
                cur_count = st.number_input("Current count (sec)", 0.0, 3600.0, float(cfg0.current_count_time_sec), 5.0)
                prop_count = st.number_input("Proposed count (sec)", 0.0, 3600.0, float(cfg0.proposed_count_time_sec), 5.0)
            with b4:
                err_prob = st.number_input("Error probability / touch (%)", 0.0, 100.0, float(cfg0.error_probability_per_touch_pct), 0.1)
                fat_touch = st.number_input("Fatigue points / touch", 0.0, 100.0, float(cfg0.fatigue_points_per_touch), 0.1)

            st.markdown("**WIP handling and automation**")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                cur_store = st.number_input("Current WIP store (sec)", 0.0, 3600.0, float(cfg0.current_store_time_sec), 5.0)
                cur_ret = st.number_input("Current WIP retrieve (sec)", 0.0, 3600.0, float(cfg0.current_retrieve_time_sec), 5.0)
            with c2:
                prop_store = st.number_input("Proposed WIP store (sec)", 0.0, 3600.0, float(cfg0.proposed_store_time_sec), 5.0)
                prop_ret = st.number_input("Proposed WIP retrieve (sec)", 0.0, 3600.0, float(cfg0.proposed_retrieve_time_sec), 5.0)
            with c3:
                fat_wip = st.number_input("Fatigue points / WIP cycle", 0.0, 100.0, float(cfg0.fatigue_points_per_wip_cycle), 0.1)
                automation = st.slider("Automation coverage (%)", 0, 100, int(cfg0.automation_coverage_pct))
            with c4:
                auto_time = st.slider("Automation time reduction (%)", 0, 100, int(cfg0.automation_time_reduction_pct))
                auto_touch = st.slider("Automation touch reduction (%)", 0, 100, int(cfg0.automation_touch_reduction_pct))

            st.markdown("**Storage baseline (editable only when you have a measured current WIP baseline)**")
            s1, s2 = st.columns(2)
            with s1:
                current_shared = st.number_input("Current shared storage slots", 0.0, 100000.0, 1913.0, 1.0)
            with s2:
                current_wip = st.number_input("Measured current temporary-WIP slots", 0.0, 100000.0, 0.0, 1.0, help="Leave 0 if no measured current WIP baseline is available. The actual model will not invent one.")

        if mto_upload and box_upload:
            try:
                mto_path = save_uploaded(mto_upload, ".xlsx")
                box_path = save_uploaded(box_upload, ".xlsx")
                cfg = ActualMTOConfig(
                    default_box_qty=default_box, conflict_box_qty=conflict_box,
                    current_touches_per_box=cur_touch, proposed_touches_per_box=prop_touch,
                    current_pack_time_sec=cur_pack, proposed_pack_time_sec=prop_pack,
                    current_count_time_sec=cur_count, proposed_count_time_sec=prop_count,
                    current_store_time_sec=cur_store, proposed_store_time_sec=prop_store,
                    current_retrieve_time_sec=cur_ret, proposed_retrieve_time_sec=prop_ret,
                    error_probability_per_touch_pct=err_prob,
                    fatigue_points_per_touch=fat_touch, fatigue_points_per_wip_cycle=fat_wip,
                    automation_coverage_pct=automation, automation_time_reduction_pct=auto_time,
                    automation_touch_reduction_pct=auto_touch, box_footprint_m2=box_area,
                    wip_location_multiplier=wip_multiplier,
                )
                detail, master = prepare_actual_mto(mto_path, box_path, cfg)
                daily2, wip_detail, summary = simulate_actual_mto(detail, cfg)
                st.session_state.model2_actual = {"detail": detail, "master": master, "daily": daily2, "wip_detail": wip_detail, "summary": summary, "config": cfg}

                st.success(f"Actual MTO model completed: {len(detail):,} transaction rows processed.")
                st.markdown("### Model 2 Executive Dashboard")
                k = st.columns(6)
                k[0].metric("Processing Time", f"{summary['time_reduction_pct']:.1f}% ↓")
                k[1].metric("Manual Touches", f"{summary['touch_reduction_pct']:.1f}% ↓")
                k[2].metric("Fatigue Proxy", f"{summary['fatigue_reduction_pct']:.1f}% ↓")
                k[3].metric("Error-Risk Proxy", f"{summary['error_risk_reduction_pct']:.1f}% ↓")
                k[4].metric("Peak Temporary WIP", f"{summary['peak_closing_wip_boxes']:.0f} boxes")
                k[5].metric("Final Open WIP", f"{summary['final_open_wip_boxes']:.0f} boxes")

                st.markdown("### 1 · Data / Box Conversion Validation")
                q = st.columns(5)
                q[0].metric("MTO Transactions", f"{summary['transactions']:,}")
                q[1].metric("Master Standard Rows", f"{summary['master_standard_rows']:,}")
                q[2].metric("Default — Missing", f"{summary['default_missing_rows']:,}")
                q[3].metric("Default — Zero", f"{summary['default_zero_rows']:,}")
                q[4].metric("Conflicting Standards", f"{summary['default_conflict_rows']:,}")
                st.dataframe(detail.head(500), use_container_width=True, hide_index=True)
                st.caption("The table is limited to the first 500 transaction rows on screen; the full transaction-level table is included in the Excel export.")

                st.markdown("### 2 · Process KPI Comparison")
                st.dataframe(build_actual_kpi_table(summary), use_container_width=True, hide_index=True)

                st.markdown("### 3 · Actual SKU-Level WIP Simulation")
                if not daily2.empty:
                    chart = daily2.set_index("DATE")[["OPENING_WIP_BOXES", "CLOSING_WIP_BOXES"]]
                    st.line_chart(chart)
                    st.caption(f"Average closing WIP: {summary['average_closing_wip_boxes']:.2f} boxes · Peak closing WIP: {summary['peak_closing_wip_boxes']:.0f} boxes · Peak WIP storage: {summary['peak_closing_wip_storage_m2']:.2f} m²")
                    st.dataframe(daily2, use_container_width=True, hide_index=True)

                st.markdown("### 4 · Storage Paradox")
                st.dataframe(build_storage_paradox_actual(summary, box_area), use_container_width=True, hide_index=True)
                st.info("Positive storage change means additional temporary staging is required. A measured current-WIP baseline can be entered above to convert this into a true current-vs-proposed net storage comparison.")

                st.markdown("### 5 · Open WIP by SKU")
                if wip_detail.empty:
                    st.success("No SKU-level closing WIP remains at the end of the supplied period.")
                else:
                    st.dataframe(wip_detail, use_container_width=True, hide_index=True)

                st.markdown("### 6 · Data Quality / Assumption Summary")
                st.write({
                    "Default box quantity used": default_box,
                    "Non-integer quantity rows": summary["non_integer_rows"],
                    "Default due to missing master": summary["default_missing_rows"],
                    "Default due to zero/invalid master": summary["default_zero_rows"],
                    "Default due to conflicting positive standards": summary["default_conflict_rows"],
                    "Interpretation": "Fatigue and error are proxies; WIP and storage are modelled from actual SKU-level transactions and the stated box-standard rules.",
                })
            except Exception as e:
                st.error(f"Actual MTO Model 2 could not be calculated: {e}")
        else:
            st.warning("Upload both the actual MTO packing-list master and the Size / Box Master to run Model 2.")

    with tabs[2]:
        st.subheader("Downloads")
        out = Path(tempfile.mkdtemp())
        excel_path = out / "Universal_Warehouse_Digital_Twin.xlsx"
        kpi = build_kpi_table(r["capacity"], r["demand_metrics"], r["simulation_summary"], r["winner"])
        export_excel(excel_path, kpi, r["capacity_table"], r["daily_df"], r["occupancy_df"], r["slot_df"], r["search_df"], r["config"], r["demand_metrics"])
        st.download_button("⬇ Download Model 1 Excel", data=excel_path.read_bytes(), file_name=excel_path.name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

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
            st.download_button("⬇ Download Model 2 Actual MTO Excel", data=p2_path.read_bytes(), file_name=p2_path.name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        if st.button("🎞 Generate Daily Simulation GIF"):
            gif_path = out / "Daily_Warehouse_Simulation.gif"
            create_daily_gif(r["daily_df"], r["warehouse"], r["storage_region"], r["slots"], r["door"], r["winner"]["layout"]["main_aisle"], r["winner"]["layout"]["turning_circle"], r["obstacles"], gif_path)
            st.session_state.gif_bytes = gif_path.read_bytes()
        if "gif_bytes" in st.session_state:
            st.download_button("⬇ Download Daily Simulation GIF", data=st.session_state.gif_bytes, file_name="Daily_Warehouse_Simulation.gif", mime="image/gif")

st.divider()
st.caption("Universal Warehouse Digital Twin · Planning capacity is a planning target, not a physical capacity.")
