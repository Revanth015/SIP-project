"""Universal Warehouse Digital Twin — Streamlit application."""

from pathlib import Path
import tempfile

import streamlit as st
from shapely.geometry import Point

from core.pipeline import run_pipeline, prepare_cad
from core.visualization_engine import plot_cad_geometry, plot_layout, create_daily_gif
from core.export_engine import build_kpi_table, export_excel

st.set_page_config(
    page_title="Universal Warehouse Digital Twin",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.main-title {font-size: 2.2rem; font-weight: 750; margin-bottom: .15rem;}
.subtitle {color: #6b7280; margin-bottom: 1rem;}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🏭 Universal Warehouse Digital Twin</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">CAD-assisted capacity planning, layout optimization and daily warehouse simulation</div>', unsafe_allow_html=True)


def save_uploaded(upload, suffix):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(upload.getbuffer())
    tmp.close()
    return tmp.name


def get_manual_door(cad):
    """Render a manual door selector; CAD door candidates are reference only."""
    warehouse = cad["warehouse"]
    minx, miny, maxx, maxy = warehouse.bounds

    st.info(
        "The operating door is a user-controlled input. Detected CAD doors are shown "
        "only as reference; the Digital Twin will not automatically choose one."
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
        reference = st.selectbox("Reference door", options, key="reference_door")
        idx = options.index(reference)
        default_x = float(doors[idx]["x"])
        default_y = float(doors[idx]["y"])
    else:
        default_x = float(warehouse.centroid.x)
        default_y = float(warehouse.centroid.y)

    c1, c2 = st.columns(2)
    with c1:
        door_x = st.number_input(
            "Operating Door X (m)",
            min_value=float(minx), max_value=float(maxx),
            value=float(st.session_state.get("door_x", default_x)),
            step=0.1, key="door_x",
            help="Enter the actual X coordinate of the warehouse operating door.",
        )
    with c2:
        door_y = st.number_input(
            "Operating Door Y (m)",
            min_value=float(miny), max_value=float(maxy),
            value=float(st.session_state.get("door_y", default_y)),
            step=0.1, key="door_y",
            help="Enter the actual Y coordinate of the warehouse operating door.",
        )

    return Point(door_x, door_y)


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
    cad_key = f"{cad_file.name}:{cad_file.size}"
    if st.session_state.get("cad_preview_key") != cad_key:
        try:
            cad_path = save_uploaded(cad_file, ".dxf")
            st.session_state.cad_path = cad_path
            st.session_state.cad_data = prepare_cad(cad_path)
            st.session_state.cad_preview_key = cad_key
            st.session_state.pop("door_x", None)
            st.session_state.pop("door_y", None)
            st.session_state.pop("reference_door", None)
        except Exception as exc:
            st.error(f"CAD could not be read: {exc}")
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
    selected_door = get_manual_door(cad)
    st.session_state.selected_door = selected_door

    # A separate coordinate marker makes the user-selected point visually explicit.
    st.pyplot(
        plot_cad_geometry({
            **cad,
            "doors": [{
                "x": selected_door.x,
                "y": selected_door.y,
                "layer": "USER_SELECTED",
            }],
        }),
        use_container_width=True,
    )
    st.caption(
        f"**Operating door fixed at:** X = {selected_door.x:.3f} m, "
        f"Y = {selected_door.y:.3f} m"
    )


if run:
    if "cad_path" not in st.session_state or not demand_file:
        st.error("Upload both a warehouse DXF and a daily demand file before running the Digital Twin.")
        st.stop()
    if "selected_door" not in st.session_state:
        st.error("Set the operating door coordinates before running the Digital Twin.")
        st.stop()

    try:
        demand_path = save_uploaded(demand_file, Path(demand_file.name).suffix.lower())
        selected_door = st.session_state.selected_door

        with st.spinner("Running CAD → layout → capacity → simulation pipeline..."):
            result = run_pipeline(
                st.session_state.cad_path,
                demand_path,
                manual_door_xy=(selected_door.x, selected_door.y),
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
            "operating_door_x_m": selected_door.x,
            "operating_door_y_m": selected_door.y,
        }
        st.session_state.result = result
        st.success("Digital Twin calculation completed.")
    except Exception as exc:
        st.error(f"The calculation could not be completed: {exc}")
        st.stop()


if "result" in st.session_state:
    r = st.session_state.result
    st.divider()
    st.subheader("3 · Decision Dashboard")
    cap = r["capacity"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Theoretical Capacity", f"{cap['theoretical_capacity']:,}")
    c2.metric("Realistic Capacity", f"{cap['realistic_capacity']:,}")
    c3.metric("Planning Capacity", f"{cap['planning_capacity']:,.1f}")
    c4.metric("Planning Occupancy", f"{cap['planning_occupancy_pct']:.0f}%")
    st.dataframe(r["capacity_table"], use_container_width=True, hide_index=True)

    st.subheader("4 · Optimized Layout")
    st.pyplot(plot_layout(
        r["warehouse"], r["storage_region"], r["slots"], r["door"],
        r["winner"]["layout"]["main_aisle"], r["winner"]["layout"]["turning_circle"],
        r["obstacles"], title="Optimized Warehouse Layout",
    ), use_container_width=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Average Slot Distance", f"{r['winner']['avg_distance_m']:.2f} m")
    m2.metric("Slots Generated", f"{len(r['slots']):,}")
    m3.metric("Detected Obstacles", f"{len(r['obstacles'])}")
    m4.metric("Peak Demand", f"{r['demand_metrics']['peak_demand']:.0f}")

    st.subheader("5 · Daily Simulation")
    st.dataframe(r["daily_df"], use_container_width=True, hide_index=True)
    s = r["simulation_summary"]
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Avg Time", f"{s['average_time_min']:.2f} min")
    a2.metric("Avg Distance", f"{s['average_distance_m']:.2f} m")
    a3.metric("Avg Fatigue Proxy", f"{s['average_fatigue']:.2f}")
    a4.metric("Overflow Days", f"{s['overflow_days']}")

    st.subheader("6 · Slot Distance Reference")
    st.dataframe(r["slot_df"], use_container_width=True, hide_index=True)

    st.subheader("7 · Layout Search")
    st.dataframe(r["search_df"], use_container_width=True, hide_index=True)

    st.subheader("8 · Downloads")
    out = Path(tempfile.mkdtemp())
    excel_path = out / "Universal_Warehouse_Digital_Twin.xlsx"
    kpi = build_kpi_table(r["capacity"], r["demand_metrics"], r["simulation_summary"], r["winner"])
    export_excel(
        excel_path, kpi, r["capacity_table"], r["daily_df"], r["occupancy_df"],
        r["slot_df"], r["search_df"], r["config"], r["demand_metrics"],
    )
    st.download_button(
        "⬇ Download Excel Report", data=excel_path.read_bytes(), file_name=excel_path.name,
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
            "⬇ Download Daily Simulation GIF", data=st.session_state.gif_bytes,
            file_name="Daily_Warehouse_Simulation.gif", mime="image/gif",
        )

st.divider()
st.caption("Universal Warehouse Digital Twin · Planning capacity is a planning target, not a physical capacity.")
