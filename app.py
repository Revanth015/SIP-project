"""Universal Warehouse Digital Twin — Streamlit application."""
from pathlib import Path
import tempfile

import pandas as pd
import streamlit as st
from shapely.geometry import Point

from core.pipeline import run_pipeline, prepare_cad
from core.visualization_engine import plot_cad_geometry, plot_layout, create_daily_gif
from core.export_engine import build_kpi_table, export_excel
from core.process_engine import (
    ProcessModelConfig,
    simulate_process_model,
    process_summary,
    process_kpi_table,
    storage_paradox_table,
    scenario_sensitivity,
)

st.set_page_config(
    page_title="Universal Warehouse Digital Twin",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.main-title {font-size:2.2rem;font-weight:750;margin-bottom:.15rem}
.subtitle {color:#6b7280;margin-bottom:1rem}
.section-note {color:#5b6470;font-size:.92rem}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🏭 Universal Warehouse Digital Twin</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">CAD-assisted warehouse capacity, layout, daily simulation and MTO process decision support</div>',
    unsafe_allow_html=True,
)


def save_uploaded(upload, suffix):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(upload.getbuffer())
    tmp.close()
    return tmp.name


def get_manual_door(cad):
    warehouse = cad["warehouse"]
    minx, miny, maxx, maxy = warehouse.bounds
    st.info(
        "The operating door is a user-controlled input. Detected CAD doors are reference only; "
        "the Digital Twin will not automatically choose one."
    )
    mode = st.radio(
        "Door input method",
        ["Enter coordinates", "Use a detected CAD door as starting point"],
        horizontal=True,
        key="door_mode",
    )
    if mode == "Use a detected CAD door as starting point" and cad.get("doors"):
        doors = cad["doors"]
        options = [
            f"CAD Door {i}: X={d['x']:.3f}, Y={d['y']:.3f} · {d['layer']}"
            for i, d in enumerate(doors, 1)
        ]
        ref = st.selectbox("Reference door", options, key="reference_door")
        d = doors[options.index(ref)]
        default_x, default_y = float(d["x"]), float(d["y"])
    else:
        default_x, default_y = float(warehouse.centroid.x), float(warehouse.centroid.y)

    c1, c2 = st.columns(2)
    with c1:
        x = st.number_input(
            "Operating Door X (m)", min_value=float(minx), max_value=float(maxx),
            value=float(st.session_state.get("door_x", default_x)), step=0.1, key="door_x"
        )
    with c2:
        y = st.number_input(
            "Operating Door Y (m)", min_value=float(miny), max_value=float(maxy),
            value=float(st.session_state.get("door_y", default_y)), step=0.1, key="door_y"
        )
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
    st.caption(
        f"Detected warehouse layer: **{cad['warehouse_layer']}** · "
        f"Area: **{cad['warehouse'].area:,.2f} m²**"
    )
    st.pyplot(plot_cad_geometry(cad), use_container_width=True)

    st.subheader("2 · Fix Operating Door")
    door = get_manual_door(cad)
    st.session_state.selected_door = door
    st.pyplot(
        plot_cad_geometry({
            **cad,
            "doors": [{"x": door.x, "y": door.y, "layer": "USER_SELECTED"}],
        }),
        use_container_width=True,
    )
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
                st.session_state.cad_path,
                demand_path,
                manual_door_xy=(door.x, door.y),
                pallet_width_m=pallet_width,
                pallet_depth_m=pallet_depth,
                wall_clearance_m=wall_clearance,
                main_aisle_m=main_aisle,
                cross_aisle_m=cross_aisle,
                turning_diameter_m=turning_diameter,
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
            "turning_diameter_m": turning_diameter,
            "target_occupancy_pct": occupancy,
            "forklift_speed_mps": forklift_speed,
            "handling_time_sec_per_pallet": handling_time,
            "operating_door_x_m": door.x,
            "operating_door_y_m": door.y,
        }
        st.session_state.result = result
        st.success("Digital Twin calculation completed.")
    except Exception as e:
        st.error(f"The calculation could not be completed: {e}")
        st.stop()


if "result" in st.session_state:
    r = st.session_state.result
    tabs = st.tabs(["Model 1 · Warehouse Layout", "Model 2 · MTO Process", "Downloads"])

    # ------------------------------------------------------------------
    # MODEL 1
    # ------------------------------------------------------------------
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
        st.pyplot(
            plot_layout(
                r["warehouse"], r["storage_region"], r["slots"], r["door"],
                r["winner"]["layout"]["main_aisle"],
                r["winner"]["layout"]["turning_circle"],
                r["obstacles"], title="Optimized Warehouse Layout",
            ),
            use_container_width=True,
        )

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

    # ------------------------------------------------------------------
    # MODEL 2
    # ------------------------------------------------------------------
    with tabs[1]:
        st.subheader("Model 2 · MTO Process Change Digital Twin")
        st.markdown(
            "**Current:** Picking → loose/mixed packing → temporary WIP → retrieval/rehandling → dispatch  "
            "\n\n**Proposed:** Picking → single-SKU stick packing → temporary staging → dispatch/consolidation"
        )
        st.caption(
            "Daily pallet demand is used as the process-volume proxy when detailed order-level MTO data are not supplied. "
            "All scenario assumptions below are editable."
        )

        defaults = ProcessModelConfig()
        with st.expander("⚙ Model 2 assumptions — change these to run what-if scenarios", expanded=True):
            st.markdown("**A. Demand & box logic**")
            a1, a2, a3, a4 = st.columns(4)
            with a1:
                mto_share = st.number_input("MTO share (%)", 0.0, 100.0, float(defaults.mto_share_pct), 1.0)
            with a2:
                box_qty = st.number_input("Standard box quantity", 1.0, 10000.0, float(defaults.standard_box_qty), 1.0)
            with a3:
                belts_order = st.number_input("Avg belts / MTO order", 0.1, 10000.0, float(defaults.average_belts_per_mto_order), 0.5)
            with a4:
                partial_rate = st.slider("Partial-box rate (%)", 0, 100, int(defaults.partial_box_rate_pct))

            st.markdown("**B. Current vs proposed process**")
            b1, b2, b3, b4 = st.columns(4)
            with b1:
                current_touches = st.number_input("Current touches / box", 0.1, 100.0, float(defaults.current_touches_per_box), 0.5)
                cur_pack = st.number_input("Current pack time (sec)", 0.0, 3600.0, float(defaults.current_pack_time_sec), 5.0)
            with b2:
                proposed_touches = st.number_input("Proposed touches / box", 0.1, 100.0, float(defaults.proposed_touches_per_box), 0.5)
                prop_pack = st.number_input("Proposed pack time (sec)", 0.0, 3600.0, float(defaults.proposed_pack_time_sec), 5.0)
            with b3:
                cur_count = st.number_input("Current count time (sec)", 0.0, 3600.0, float(defaults.current_count_time_sec), 5.0)
                prop_count = st.number_input("Proposed count time (sec)", 0.0, 3600.0, float(defaults.proposed_count_time_sec), 5.0)
            with b4:
                error_prob = st.number_input("Error probability / touch (%)", 0.0, 100.0, float(defaults.error_probability_per_touch_pct), 0.1)
                box_area = st.number_input("Box footprint (m²)", 0.001, 100.0, float(defaults.box_footprint_m2), 0.01)

            st.markdown("**C. WIP & temporary storage behaviour**")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                cur_store = st.number_input("Current WIP store (sec)", 0.0, 3600.0, float(defaults.current_wip_store_time_sec), 5.0)
                cur_ret = st.number_input("Current WIP retrieve (sec)", 0.0, 3600.0, float(defaults.current_wip_retrieve_time_sec), 5.0)
            with c2:
                prop_store = st.number_input("Proposed WIP store (sec)", 0.0, 3600.0, float(defaults.proposed_wip_store_time_sec), 5.0)
                prop_ret = st.number_input("Proposed WIP retrieve (sec)", 0.0, 3600.0, float(defaults.proposed_wip_retrieve_time_sec), 5.0)
            with c3:
                cur_dwell = st.number_input("Current WIP dwell (days)", 0.0, 365.0, float(defaults.current_wip_dwell_days), 0.5)
                prop_dwell = st.number_input("Proposed WIP dwell (days)", 0.0, 365.0, float(defaults.proposed_wip_dwell_days), 0.5)
            with c4:
                wip_reduction = st.slider("Target WIP reduction (%)", 0, 100, int(defaults.target_wip_reduction_pct))
                max_wip = st.number_input("Maximum temporary WIP boxes", 1.0, 100000.0, float(defaults.max_temporary_wip_boxes), 10.0)

            st.markdown("**D. Proxy & automation assumptions**")
            d1, d2, d3, d4 = st.columns(4)
            with d1:
                fatigue_touch = st.number_input("Fatigue points / touch", 0.0, 100.0, float(defaults.fatigue_points_per_touch), 0.1)
                fatigue_wip = st.number_input("Fatigue points / WIP cycle", 0.0, 100.0, float(defaults.fatigue_points_per_wip_cycle), 0.1)
            with d2:
                automation = st.slider("Automation coverage (%)", 0, 100, int(defaults.automation_coverage_pct))
            with d3:
                auto_time = st.slider("Automation time reduction (%)", 0, 100, int(defaults.automation_time_reduction_pct))
            with d4:
                auto_touch = st.slider("Automation touch reduction (%)", 0, 100, int(defaults.automation_touch_reduction_pct))

            st.markdown("**E. Storage paradox assumptions**")
            e1, e2, e3 = st.columns(3)
            with e1:
                shared_slots = st.number_input("Current shared storage slots", 1.0, 100000.0, float(defaults.current_shared_storage_slots), 1.0)
            with e2:
                current_wip_slots = st.number_input("Current temporary WIP slots", 0.0, 100000.0, float(defaults.current_temporary_wip_slots), 1.0)
            with e3:
                density_penalty = st.number_input("Density / staging penalty slots", 0.0, 100000.0, float(defaults.density_staging_penalty_slots), 1.0)
            st.caption(
                "Storage logic: Proposed shared storage = Current shared storage − WIP reduction effect + density/staging penalty. "
                "Negative net change means storage is released; positive means storage is consumed."
            )

        cfg = ProcessModelConfig(
            mto_share_pct=mto_share,
            standard_box_qty=box_qty,
            average_belts_per_mto_order=belts_order,
            partial_box_rate_pct=partial_rate,
            current_touches_per_box=current_touches,
            proposed_touches_per_box=proposed_touches,
            error_probability_per_touch_pct=error_prob,
            current_pack_time_sec=cur_pack,
            proposed_pack_time_sec=prop_pack,
            current_count_time_sec=cur_count,
            proposed_count_time_sec=prop_count,
            current_wip_store_time_sec=cur_store,
            proposed_wip_store_time_sec=prop_store,
            current_wip_retrieve_time_sec=cur_ret,
            proposed_wip_retrieve_time_sec=prop_ret,
            current_wip_dwell_days=cur_dwell,
            proposed_wip_dwell_days=prop_dwell,
            target_wip_reduction_pct=wip_reduction,
            max_temporary_wip_boxes=max_wip,
            fatigue_points_per_touch=fatigue_touch,
            fatigue_points_per_wip_cycle=fatigue_wip,
            automation_coverage_pct=automation,
            automation_time_reduction_pct=auto_time,
            automation_touch_reduction_pct=auto_touch,
            box_footprint_m2=box_area,
            current_shared_storage_slots=shared_slots,
            current_temporary_wip_slots=current_wip_slots,
            density_staging_penalty_slots=density_penalty,
        )

        try:
            p2 = simulate_process_model(r["demand"], cfg)
            ps = process_summary(p2)
            st.session_state.process_result = p2
            st.session_state.process_config = cfg

            # Executive KPI cards
            st.markdown("### Model 2 Executive Dashboard")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Processing Time", f"{ps['time_reduction_pct']:.1f}% ↓", border=True)
            k2.metric("Manual Touches", f"{ps['touch_reduction_pct']:.1f}% ↓", border=True)
            k3.metric("Fatigue Proxy", f"{ps['fatigue_reduction_pct']:.1f}% ↓", border=True)
            k4.metric("Expected Error Proxy", f"{ps['error_proxy_reduction_pct']:.1f}% ↓", border=True)
            k5, k6, k7, k8 = st.columns(4)
            k5.metric("Temporary WIP", f"{ps['wip_reduction_pct']:.1f}% ↓", border=True)
            k6.metric("Current Shared Storage", f"{ps['current_shared_storage_slots']:,.0f} slots", border=True)
            k7.metric("Proposed Shared Storage", f"{ps['proposed_shared_storage_slots']:,.0f} slots", border=True)
            k8.metric("Net Storage Change", f"{ps['net_shared_storage_change_slots']:+,.0f} slots", border=True)

            st.markdown("### 1 · Process KPI Comparison")
            st.dataframe(process_kpi_table(ps), use_container_width=True, hide_index=True)

            st.markdown("### 2 · Temporary WIP Simulation")
            chart_df = p2.set_index("DATE")[["CURRENT_WIP_BOXES", "PROPOSED_WIP_BOXES"]]
            st.line_chart(chart_df)
            st.caption(
                f"Average current WIP: {ps['average_current_wip_boxes']:.2f} boxes · "
                f"Average proposed WIP: {ps['average_proposed_wip_boxes']:.2f} boxes · "
                f"Peak proposed WIP: {ps['peak_proposed_wip_boxes']:.2f} boxes"
            )

            st.markdown("### 3 · Storage Paradox")
            left, right = st.columns([1, 1.4])
            with left:
                st.metric("WIP reduction effect", f"-{ps['wip_reduction_effect_slots']:.1f} slots", delta_color="inverse")
                st.metric("Density / staging penalty", f"+{ps['density_staging_penalty_slots']:.1f} slots")
                st.metric("NET STORAGE CHANGE", f"{ps['net_shared_storage_change_slots']:+.1f} slots", delta_color="inverse")
                st.caption(
                    "The proposed process can improve handling while the single-SKU storage rule can create a density penalty. "
                    "The model makes both effects visible rather than hiding the trade-off."
                )
            with right:
                st.dataframe(storage_paradox_table(ps), use_container_width=True, hide_index=True)

            st.markdown("### 4 · Automation What-If")
            st.caption("Change automation coverage above. The model recalculates time, touches, fatigue, error proxy, WIP and net storage.")
            sens_parameter = st.selectbox(
                "Sensitivity variable",
                ["mto_share_pct", "target_wip_reduction_pct", "automation_coverage_pct", "standard_box_qty", "partial_box_rate_pct"],
                format_func=lambda x: {
                    "mto_share_pct": "MTO share (%)",
                    "target_wip_reduction_pct": "Target WIP reduction (%)",
                    "automation_coverage_pct": "Automation coverage (%)",
                    "standard_box_qty": "Standard box quantity",
                    "partial_box_rate_pct": "Partial-box rate (%)",
                }[x],
            )
            if sens_parameter in {"mto_share_pct", "target_wip_reduction_pct", "automation_coverage_pct", "partial_box_rate_pct"}:
                sens_values = st.slider(
                    "Sensitivity range",
                    0, 100,
                    (20, 80),
                    5,
                    key=f"sens_{sens_parameter}",
                )
                values = list(range(sens_values[0], sens_values[1] + 1, 5))
            else:
                sens_values = st.slider("Box quantity range", 1, 100, (5, 20), 1, key="sens_box_qty")
                values = list(range(sens_values[0], sens_values[1] + 1))
            sensitivity_df = scenario_sensitivity(r["demand"], cfg, sens_parameter, values)
            st.dataframe(sensitivity_df, use_container_width=True, hide_index=True)
            st.line_chart(
                sensitivity_df.set_index("SCENARIO_VALUE")[["TIME_REDUCTION_%", "WIP_REDUCTION_%", "NET_STORAGE_CHANGE_%"]]
            )

            st.markdown("### 5 · Daily Model 2 Results")
            st.dataframe(p2, use_container_width=True, hide_index=True)
            st.caption(
                "Interpretation: fatigue is a relative proxy; expected errors are modelled risk estimates; "
                "WIP reduction depends on the stated process assumptions; storage figures are scenario outputs."
            )
        except Exception as e:
            st.error(f"Model 2 could not be calculated: {e}")

    # ------------------------------------------------------------------
    # DOWNLOADS
    # ------------------------------------------------------------------
    with tabs[2]:
        st.subheader("Downloads")
        out = Path(tempfile.mkdtemp())
        excel_path = out / "Universal_Warehouse_Digital_Twin.xlsx"
        kpi = build_kpi_table(r["capacity"], r["demand_metrics"], r["simulation_summary"], r["winner"])
        export_excel(
            excel_path, kpi, r["capacity_table"], r["daily_df"], r["occupancy_df"],
            r["slot_df"], r["search_df"], r["config"], r["demand_metrics"],
        )
        st.download_button(
            "⬇ Download Model 1 Excel",
            data=excel_path.read_bytes(),
            file_name=excel_path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        if "process_result" in st.session_state and not st.session_state.process_result.empty:
            p2 = st.session_state.process_result
            p2_path = out / "Model_2_MTO_Process_Analysis.xlsx"
            with pd.ExcelWriter(p2_path, engine="openpyxl") as writer:
                p2.to_excel(writer, sheet_name="Daily_Model_2", index=False)
                process_summary(p2) and process_kpi_table(process_summary(p2)).to_excel(writer, sheet_name="Process_KPIs", index=False)
                storage_paradox_table(process_summary(p2)).to_excel(writer, sheet_name="Storage_Paradox", index=False)
            st.download_button(
                "⬇ Download Model 2 Excel",
                data=p2_path.read_bytes(),
                file_name=p2_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        if st.button("🎞 Generate Daily Simulation GIF"):
            gif_path = out / "Daily_Warehouse_Simulation.gif"
            create_daily_gif(
                r["daily_df"], r["warehouse"], r["storage_region"], r["slots"], r["door"],
                r["winner"]["layout"]["main_aisle"], r["winner"]["layout"]["turning_circle"],
                r["obstacles"], gif_path,
            )
            st.session_state.gif_bytes = gif_path.read_bytes()
        if "gif_bytes" in st.session_state:
            st.download_button(
                "⬇ Download Daily Simulation GIF",
                data=st.session_state.gif_bytes,
                file_name="Daily_Warehouse_Simulation.gif",
                mime="image/gif",
            )

st.divider()
st.caption("Universal Warehouse Digital Twin · Planning capacity is a planning target, not a physical capacity.")
