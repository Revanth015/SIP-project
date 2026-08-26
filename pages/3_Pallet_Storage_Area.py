"""Dedicated pallet-storage-area scenario page.

Uses the existing CAD/layout engines from the Universal Warehouse Digital Twin.
This page lets a manager constrain pallet storage to Automatic CAD storage,
the full warehouse, or a manually defined rectangle.
"""
from pathlib import Path
import tempfile
import streamlit as st
from shapely.geometry import box
from core.pipeline import prepare_cad
from core.layout_engine import LayoutConfig, theoretical_capacity, optimize_realistic_layout

st.set_page_config(page_title="Pallet Storage Area", page_icon="📦", layout="wide")
st.title("📦 Dedicated Pallet Storage Area")
st.caption("Operational scenario: dedicate only a defined part of the warehouse to pallet storage.")

cad_file = st.file_uploader("Warehouse CAD (.dxf)", type=["dxf"])

if cad_file:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as f:
        f.write(cad_file.getbuffer())
        cad_path = f.name
    try:
        cad = prepare_cad(cad_path)
    except Exception as e:
        st.error(f"CAD could not be read: {e}")
        st.stop()

    warehouse = cad["warehouse"]
    st.success(f"Warehouse detected: {warehouse.area:,.2f} m²")

    mode = st.radio(
        "Pallet storage allocation",
        ["Automatic CAD storage region", "Full warehouse", "Manual rectangle"],
        horizontal=True,
    )

    minx, miny, maxx, maxy = warehouse.bounds
    if mode == "Automatic CAD storage region":
        region = cad.get("storage_region", warehouse)
        # Existing prepare_cad may expose only warehouse; in that case the
        # entire warehouse is an explicit, safe fallback.
        source = "CAD-detected storage region" if region is not warehouse else "Full warehouse fallback"
    elif mode == "Full warehouse":
        region = warehouse
        source = "Entire warehouse"
    else:
        st.info("Define the pallet-only area using warehouse CAD coordinates.")
        c1,c2 = st.columns(2)
        x1=c1.number_input("X minimum", value=float(minx))
        x2=c2.number_input("X maximum", value=float(maxx))
        c3,c4 = st.columns(2)
        y1=c3.number_input("Y minimum", value=float(miny))
        y2=c4.number_input("Y maximum", value=float(maxy))
        if x2 <= x1 or y2 <= y1:
            st.error("Maximum coordinates must be greater than minimum coordinates.")
            st.stop()
        region = warehouse.intersection(box(x1,y1,x2,y2))
        source = "Manager-defined rectangle"

    if region.is_empty:
        st.error("The selected pallet-storage area does not overlap the warehouse.")
        st.stop()

    cfg = LayoutConfig(
        pallet_width_m=1.20,
        pallet_depth_m=1.00,
        wall_clearance_m=0.25,
        main_aisle_m=4.0,
        cross_aisle_m=2.0,
        turning_diameter_m=7.0,
    )
    door_data = cad.get("doors", [])
    if door_data:
        door = type("Door", (), {"x": door_data[0]["x"], "y": door_data[0]["y"]})()
        from shapely.geometry import Point
        door = Point(door.x, door.y)
    else:
        from shapely.geometry import Point
        door = Point(warehouse.centroid.x, warehouse.centroid.y)
        st.warning("No CAD door was detected on this page; using the warehouse centroid only as a reference. Use the main app's manual-door control for the validated operating door.")

    obstacles = [o for o in cad.get("obstacles", []) if region.intersects(o)]
    theo = theoretical_capacity(region, cfg, obstacles)
    try:
        winner, search = optimize_realistic_layout(
            region, door, obstacles, cfg,
            orientation_options=cfg.orientations,
            wall_clearance_options=(cfg.wall_clearance_m,),
            main_aisle_options=(cfg.main_aisle_m,),
            cross_aisle_options=(cfg.cross_aisle_m,),
        )
        realistic = len(winner["layout"]["slots"])
    except Exception as e:
        st.error(f"No feasible pallet layout exists inside the selected area: {e}")
        st.stop()

    planning = realistic * 0.65
    a,b,c,d=st.columns(4)
    a.metric("Designated area",f"{region.area:,.2f} m²")
    b.metric("Theoretical slots",f"{theo['capacity']:,}")
    c.metric("Realistic slots",f"{realistic:,}")
    d.metric("Planning slots @ 65%",f"{planning:,.1f}")

    st.info(f"Pallets are permitted only inside the selected zone: **{source}**. Space outside this zone is excluded from pallet capacity.")
    st.subheader("Managerial capacity comparison")
    st.dataframe(search, use_container_width=True, hide_index=True)

    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10,7))
    x,y=warehouse.exterior.xy; ax.plot(x,y,linewidth=2,label="Warehouse")
    if hasattr(region,"exterior"):
        x,y=region.exterior.xy; ax.fill(x,y,alpha=.18,label="Dedicated pallet-storage area")
    ax.scatter([door.x],[door.y],marker="*",s=120,label="Door")
    ax.set_aspect("equal"); ax.set_xlabel("X (m)"); ax.set_ylabel("Y (m)"); ax.legend(); ax.set_title("Warehouse — Dedicated Pallet Storage Zone")
    st.pyplot(fig,use_container_width=True)

    st.download_button(
        "⬇ Download storage-area scenario CSV",
        search.to_csv(index=False).encode(),
        file_name="Pallet_Storage_Area_Scenario.csv",
        mime="text/csv",
    )
else:
    st.info("Upload the warehouse DXF to define and evaluate a dedicated pallet-storage zone.")
