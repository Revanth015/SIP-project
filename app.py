"""JK Fenner Warehouse Digital Twin — integrated Model 1 + Model 2 workflow."""
from pathlib import Path
import io
import tempfile
import zipfile

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from shapely.geometry import Point, box as sbox

from core.pipeline import prepare_cad, run_pipeline
from core.scenario_engine import prepare_movement
from core.simulation_engine import SimulationConfig, simulate_daily, simulation_summary

st.set_page_config(page_title="JK Fenner Warehouse Digital Twin", page_icon="🏭", layout="wide")
st.title("🏭 JK Fenner Warehouse Digital Twin")
st.caption("CAD → operating door → Model 1 physical slots → exact study area → Model 2 movement-based slotting")

FALLBACK_BELTS_PER_BOX = 28  # project fallback carried from the documented Model 1 methodology


def tmp(uploaded):
    p = tempfile.NamedTemporaryFile(delete=False, suffix=Path(uploaded.name).suffix)
    p.write(uploaded.getbuffer())
    p.close()
    return p.name


def load_excel(path, preferred_sheet=None):
    if Path(path).suffix.lower() == ".csv":
        return pd.read_csv(path)
    xl = pd.ExcelFile(path)
    if preferred_sheet and preferred_sheet in xl.sheet_names:
        return pd.read_excel(path, sheet_name=preferred_sheet)
    return pd.read_excel(path, sheet_name=0)


def find_col(df, names, required=True):
    lookup = {str(c).strip().upper(): c for c in df.columns}
    for n in names:
        if n.upper() in lookup:
            return lookup[n.upper()]
    if required:
        raise ValueError(f"Missing one of {names}. Available columns: {list(df.columns)}")
    return None


def derive_daily_demand(mto_path, box_path):
    """Build Model 1 daily pallet demand directly from MTO + Size–Box data."""
    mto = load_excel(mto_path, "Consolidated_Packing_Lists").copy()
    date_col = find_col(mto, ["DATE", "DATETIME", "DAY"])
    sku_col = find_col(mto, ["ITEM_SIZE", "BELT_SIZE", "SKU", "LINE_ITEM_SIZE_ID", "SIZE"])
    qty_col = find_col(mto, ["NO_OF_BELTS", "BELTS", "QUANTITY", "QTY"])

    box = load_excel(box_path).copy()
    bsku = find_col(box, ["BELT SIZE", "ITEM_SIZE", "BELT_SIZE", "SKU", "ITEM CODE", "SIZE"])
    bqty = find_col(box, ["UNITS PER BOX", "UNITS_PER_BOX", "BELTS_PER_BOX", "STANDARD BOX QUANTITY", "BOX_QTY"])

    mto = mto.rename(columns={date_col: "DATE", sku_col: "ITEM_SIZE", qty_col: "BELTS"})
    box = box.rename(columns={bsku: "ITEM_SIZE", bqty: "BELTS_PER_BOX"})
    mto["DATE"] = pd.to_datetime(mto["DATE"], errors="coerce", dayfirst=True)
    mto["ITEM_SIZE"] = mto["ITEM_SIZE"].astype("string").str.strip().str.upper()
    mto["BELTS"] = pd.to_numeric(mto["BELTS"], errors="coerce")
    box["ITEM_SIZE"] = box["ITEM_SIZE"].astype("string").str.strip().str.upper()
    box["BELTS_PER_BOX"] = pd.to_numeric(box["BELTS_PER_BOX"], errors="coerce")
    box = box.dropna(subset=["ITEM_SIZE", "BELTS_PER_BOX"])
    box = box[box["BELTS_PER_BOX"] > 0].drop_duplicates("ITEM_SIZE")

    d = mto.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"]).copy()
    d = d[d["BELTS"] > 0]
    d = d.merge(box[["ITEM_SIZE", "BELTS_PER_BOX"]], on="ITEM_SIZE", how="left")
    d["BELTS_PER_BOX_SOURCE"] = np.where(d["BELTS_PER_BOX"].notna(), "Size–Box Master", f"Fallback = {FALLBACK_BELTS_PER_BOX}")
    d["BELTS_PER_BOX"] = d["BELTS_PER_BOX"].fillna(FALLBACK_BELTS_PER_BOX)
    d["BOXES"] = np.ceil(d["BELTS"] / d["BELTS_PER_BOX"]).astype(int)

    daily = d.groupby("DATE", as_index=False).agg(
        TOTAL_BELTS=("BELTS", "sum"),
        TOTAL_BOXES=("BOXES", "sum"),
        TRANSACTION_LINES=("ITEM_SIZE", "size"),
    )
    daily["TOTAL_PALLETS"] = np.ceil(daily["TOTAL_BOXES"] / 32.0).astype(int)
    daily = daily.sort_values("DATE").reset_index(drop=True)
    return daily, d


def cad_plot(cad, door=None, turn=None):
    fig = go.Figure()
    for e in cad.get("entities", []):
        pts = e.get("points") or []
        if len(pts) > 1:
            fig.add_trace(go.Scatter(x=[p[0] for p in pts], y=[p[1] for p in pts], mode="lines", showlegend=False, hoverinfo="skip"))
    x, y = cad["warehouse"].exterior.xy
    fig.add_trace(go.Scatter(x=list(x), y=list(y), mode="lines", line=dict(width=3), name="Warehouse"))
    for i, d in enumerate(cad.get("doors", []), 1):
        fig.add_trace(go.Scatter(x=[d["x"]], y=[d["y"]], mode="markers+text", text=[f"D{i}"], textposition="top center", marker=dict(size=14, symbol="star"), name=f"CAD Door {i}"))
    if door:
        fig.add_trace(go.Scatter(x=[door.x], y=[door.y], mode="markers+text", text=["OPERATING DOOR"], textposition="bottom center", marker=dict(size=15, symbol="x"), name="Operating door"))
    if turn:
        c, r = turn[0], turn[1] / 2
        fig.add_shape(type="circle", x0=c.x-r, x1=c.x+r, y0=c.y-r, y1=c.y+r, line=dict(dash="dot"))
    fig.update_layout(height=600, title="CAD / operating door / turning zone", xaxis_title="X (m)", yaxis_title="Y (m)", margin=dict(l=20,r=20,t=50,b=20))
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def m1_plot(r, title="Model 1 — generated physical slots", selectable=False):
    """Render the Model 1 result. This fixes the previous m1_fig NameError."""
    fig = go.Figure()
    x, y = r["warehouse"].exterior.xy
    fig.add_trace(go.Scatter(x=list(x), y=list(y), mode="lines", line=dict(width=3), name="Warehouse"))
    for o in r.get("obstacles", []):
        x, y = o.exterior.xy
        fig.add_trace(go.Scatter(x=list(x), y=list(y), mode="lines", fill="toself", opacity=.2, showlegend=False))
    s = r["slot_df"]
    fig.add_trace(go.Scatter(x=s.X_M, y=s.Y_M, mode="markers", marker=dict(size=6, symbol="square-open"), name="Model 1 slots", customdata=s.SLOT_ID, hovertemplate="%{customdata}<br>X=%{x:.2f}<br>Y=%{y:.2f}<extra></extra>"))
    d = r["door"]
    fig.add_trace(go.Scatter(x=[d.x], y=[d.y], mode="markers+text", text=["DOOR"], textposition="top center", marker=dict(size=15, symbol="star"), name="Operating door"))
    tc = r.get("turning_center")
    if r.get("turning_enabled") and tc:
        rr = r.get("turning_diameter_m", 7) / 2
        fig.add_shape(type="circle", x0=tc.x-rr, x1=tc.x+rr, y0=tc.y-rr, y1=tc.y+rr, line=dict(dash="dot"))
    fig.update_layout(height=650, title=title, xaxis_title="X (m)", yaxis_title="Y (m)", dragmode="select" if selectable else "zoom", margin=dict(l=20,r=20,t=50,b=20))
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def event_range(event):
    if event is None:
        return None
    try:
        boxes = event.selection.box
    except Exception:
        try:
            boxes = event["selection"]["box"]
        except Exception:
            return None
    if not boxes:
        return None
    b = boxes[-1]
    try:
        r = b.range
    except Exception:
        r = b.get("range", {})
    try:
        xr, yr = r["x"], r["y"]
        return min(xr), max(xr), min(yr), max(yr)
    except Exception:
        return None


def slots_in(df, region):
    mask = df.apply(lambda q: region.covers(Point(float(q.X_M), float(q.Y_M))), axis=1)
    return df[mask].copy().sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)


def selected_sim(r, slots):
    n = len(slots)
    planning = max(int(n * r["config"]["target_occupancy_pct"] / 100), 1)
    c = r["config"]
    cfg = SimulationConfig(forklift_speed_mps=c["forklift_speed_mps"], handling_time_sec_per_pallet=c["handling_time_sec_per_pallet"], target_occupancy_pct=c["target_occupancy_pct"])
    daily, occupancy = simulate_daily(r["demand"], slots, n, planning, cfg)
    summary = simulation_summary(daily)
    summary.update(SELECTED_FEASIBLE_SLOTS=n, SELECTED_PLANNING_CAPACITY=planning)
    return daily, occupancy, summary


def reset():
    st.session_state.clear()
    st.rerun()


with st.sidebar:
    st.header("Inputs")
    cad_file = st.file_uploader("1. Warehouse CAD", type=["dxf"])
    mto_file = st.file_uploader("2. MTO Consolidated Master", type=["xlsx", "csv"])
    mta_file = st.file_uploader("3. MTA Consolidated Master", type=["xlsx", "csv"])
    box_file = st.file_uploader("4. Size–Box Master", type=["xlsx", "csv"])
    st.caption("Daily demand is calculated automatically from the MTO Master + Size–Box Master. No separate daily-demand file is required.")

    st.divider()
    st.subheader("Model 1 physical controls")
    pw = st.number_input("Pallet width (m)", .1, 5., 1.2, .05)
    pd_ = st.number_input("Pallet depth (m)", .1, 5., 1.0, .05)
    occ = st.slider("Planning occupancy (%)", 50, 85, 65)
    wall = st.number_input("Wall clearance (m)", 0., 5., .25, .05)
    aisle = st.number_input("Main aisle (m)", .5, 10., 4., .25)
    cross = st.number_input("Cross aisle (m)", 0., 10., 2., .25)
    speed = st.number_input("Forklift speed (m/s)", .1, 10., 2.5, .1)
    handle = st.number_input("Handling time (sec/pallet)", 0., 600., 0., 5.)

    st.divider()
    st.subheader("Model 2")
    strategy = st.selectbox("Strategy", ["Frequency priority", "Volume priority", "Frequency × volume"])
    density = st.number_input("Scenario density (belts/slot)", 1., 100000., 300., 50., help="Scenario parameter only; not an observed inventory capacity.")
    if st.button("Reset all", use_container_width=True):
        reset()

if not cad_file:
    st.info("Upload the warehouse CAD to begin.")
    st.stop()
if not (mto_file and mta_file and box_file):
    st.info("Upload the MTO Consolidated Master, MTA Consolidated Master and Size–Box Master.")
    st.stop()

cad_key = f"{cad_file.name}:{cad_file.size}"
if st.session_state.get("cad_key") != cad_key:
    try:
        st.session_state.cad_path = tmp(cad_file)
        st.session_state.cad_data = prepare_cad(st.session_state.cad_path)
        st.session_state.cad_key = cad_key
    except Exception as e:
        st.error(f"CAD read failed: {e}")
        st.stop()

# Prepare master-data paths once per upload.
for key, uploaded in [("mto", mto_file), ("mta", mta_file), ("box", box_file)]:
    sig = f"{uploaded.name}:{uploaded.size}"
    if st.session_state.get(f"{key}_key") != sig:
        st.session_state[f"{key}_path"] = tmp(uploaded)
        st.session_state[f"{key}_key"] = sig

cad = st.session_state.cad_data
mnx, mny, mxx, mxy = cad["warehouse"].bounds

auto_doors = cad.get("doors", [])
st.write(f"**Warehouse area:** {cad['warehouse'].area:,.1f} m²  |  **CAD doors:** {len(auto_doors)}  |  **Obstacles:** {len(cad.get('obstacles', []))}")

st.header("1 — Select operating door")
if auto_doors:
    dm = st.radio("Door source", ["CAD-detected door", "Manual door position"], horizontal=True)
    if dm == "CAD-detected door":
        labels = [f"CAD Door {i}: ({d['x']:.2f}, {d['y']:.2f})" for i, d in enumerate(auto_doors, 1)]
        chosen = st.selectbox("Operating door", labels)
        dd = auto_doors[labels.index(chosen)]
        dx, dy = float(dd["x"]), float(dd["y"])
        door_source = chosen
    else:
        dx = st.number_input("Door X (m)", float(mnx), float(mxx), float(cad["warehouse"].centroid.x), .1)
        dy = st.number_input("Door Y (m)", float(mny), float(mxy), float(cad["warehouse"].centroid.y), .1)
        door_source = "Manual X/Y"
else:
    st.warning("No CAD door candidate detected; enter validated coordinates.")
    dx = st.number_input("Door X (m)", float(mnx), float(mxx), float(cad["warehouse"].centroid.x), .1)
    dy = st.number_input("Door Y (m)", float(mny), float(mxy), float(cad["warehouse"].centroid.y), .1)
    door_source = "Manual X/Y"

door = Point(dx, dy)
st.write(f"Selected operating door: **({dx:.3f}, {dy:.3f}) m** — {door_source}")

st.header("2 — Configure turning zone")
ton = st.checkbox("Enable turning zone", True)
td = st.number_input("Turning diameter (m)", 1., 20., 7., .25, disabled=not ton)
tm = st.radio("Turning-zone placement", ["At operating door", "Manual position"], horizontal=True, disabled=not ton)
if ton and tm == "Manual position":
    tx = st.number_input("Turning X (m)", float(mnx), float(mxx), float(dx), .1)
    ty = st.number_input("Turning Y (m)", float(mny), float(mxy), float(dy), .1)
    turn = Point(tx, ty)
elif ton:
    turn = door
else:
    turn = None
st.plotly_chart(cad_plot(cad, door, (turn, td) if ton else None), use_container_width=True)

st.header("3 — Run Model 1")
if st.button("▶ Generate physical slots", type="primary", use_container_width=True):
    try:
        with st.spinner("Deriving daily demand from MTO + Size–Box Master and generating feasible physical slots…"):
            daily_demand, demand_detail = derive_daily_demand(st.session_state["mto_path"], st.session_state["box_path"])
            demand_path = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
            daily_demand.to_csv(demand_path.name, index=False)
            demand_path.close()
            r = run_pipeline(
                st.session_state.cad_path,
                demand_path.name,
                manual_door_xy=(dx, dy),
                pallet_width_m=pw,
                pallet_depth_m=pd_,
                wall_clearance_m=wall,
                main_aisle_m=aisle,
                cross_aisle_m=cross,
                turning_diameter_m=td,
                turning_enabled=ton,
                turning_center_xy=(turn.x, turn.y) if turn else None,
                target_occupancy_pct=occ,
                forklift_speed_mps=speed,
                handling_time_sec_per_pallet=handle,
            )
            r["derived_daily_demand"] = daily_demand
            r["demand_detail"] = demand_detail
            r["config"] = {
                "pallet_width_m": pw, "pallet_depth_m": pd_, "wall_clearance_m": wall,
                "main_aisle_m": aisle, "cross_aisle_m": cross, "turning_enabled": ton,
                "turning_diameter_m": td if ton else 0, "turning_center_x_m": turn.x if turn else None,
                "turning_center_y_m": turn.y if turn else None, "target_occupancy_pct": occ,
                "forklift_speed_mps": speed, "handling_time_sec_per_pallet": handle,
                "operating_door_x_m": dx, "operating_door_y_m": dy, "door_source": door_source,
                "daily_demand_source": "Derived from MTO Consolidated Master + Size–Box Master",
            }
            st.session_state.m1_result = r
            st.session_state.pop("area_range", None)
            st.session_state.pop("area_region", None)
            st.session_state.pop("scenario_result", None)
        st.success(f"Model 1 completed: {len(r['slot_df']):,} feasible slots.")
    except Exception as e:
        st.error(f"Model 1 failed: {e}")

if "m1_result" in st.session_state:
    r = st.session_state.m1_result

    st.header("4 — Select the exact study area")
    # IMPORTANT: call the function that actually exists: m1_plot, not m1_fig.
    st.plotly_chart(m1_plot(r), use_container_width=True)
    st.write("Use the **Box Select** tool in the Plotly toolbar on the chart below. Drag around the exact warehouse area you want to improve. Model 2 will use only the existing Model 1 slots inside that area.")
    event = st.plotly_chart(
        m1_plot(r, "SELECT STUDY AREA — Box Select", True),
        use_container_width=True,
        key="area_selector",
        on_select="rerun",
        selection_mode="box",
    )
    rr = event_range(event)
    if rr:
        st.session_state.area_range = rr
        st.session_state.area_region = r["warehouse"].intersection(sbox(rr[0], rr[2], rr[1], rr[3]))

    if st.session_state.get("area_range"):
        rr = st.session_state.area_range
        region = st.session_state.area_region
        slots = slots_in(r["slot_df"], region)
        st.success(f"Selected area: X {rr[0]:.2f}–{rr[1]:.2f} m · Y {rr[2]:.2f}–{rr[3]:.2f} m · **{len(slots):,} existing Model 1 slots**")

        if len(slots) == 0:
            st.warning("No Model 1 slots fall inside the selected area. Select an area containing generated slots.")
            st.stop()

        st.header("5 — Selected-area simulation")
        daily, occupancy, sim_summary = selected_sim(r, slots)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Selected slots", len(slots))
        c2.metric("Planning capacity", int(len(slots) * occ / 100))
        c3.metric("Peak daily pallets", int(daily["PALLETS"].max()) if len(daily) else 0)
        c4.metric("Overflow days", int((daily["OVERFLOW"] > 0).sum()) if len(daily) else 0)
        st.dataframe(daily, use_container_width=True)

        st.header("6 — Model 2: movement-based slotting")
        with st.spinner("Analysing MTO + MTA movement and allocating the selected Model 1 slots…"):
            sku, mto_clean, mta_clean, box_clean = prepare_movement(
                st.session_state["mto_path"], st.session_state["mta_path"], st.session_state["box_path"]
            )
            # Keep Model 2 strictly dependent on Model 1's selected physical slots.
            ranked = sku.copy()
            if strategy == "Frequency priority":
                ranked["STRATEGY_SCORE"] = ranked["FREQUENCY_SCORE"]
            elif strategy == "Volume priority":
                ranked["STRATEGY_SCORE"] = ranked["MTO_BELTS"]
            else:
                f = ranked["FREQUENCY_SCORE"] / max(float(ranked["FREQUENCY_SCORE"].max()), 1)
                v = ranked["MTO_BELTS"] / max(float(ranked["MTO_BELTS"].max()), 1)
                ranked["STRATEGY_SCORE"] = np.sqrt(f * v)
            ranked = ranked.sort_values(["STRATEGY_SCORE", "MTO_BELTS"], ascending=False).reset_index(drop=True)

            allocations = []
            slot_list = slots.reset_index(drop=True)
            pos = 0
            for rank, row in ranked.iterrows():
                required = max(int(np.ceil(float(row["MTO_BELTS"]) / density)), 1)
                for j in range(required):
                    out = {"SKU_RANK": rank + 1, "ITEM_SIZE": row["ITEM_SIZE"], "STRATEGY_SCORE": row["STRATEGY_SCORE"], "SLOTS_REQUIRED_FOR_SKU": required, "SLOT_WITHIN_SKU": j + 1}
                    if pos < len(slot_list):
                        s = slot_list.iloc[pos]
                        out.update({"SLOT_ID": s["SLOT_ID"], "X_M": s["X_M"], "Y_M": s["Y_M"], "DISTANCE_FROM_DOOR_M": s["DISTANCE_FROM_DOOR_M"], "STATUS": "ALLOCATED"})
                        pos += 1
                    else:
                        out.update({"SLOT_ID": None, "X_M": np.nan, "Y_M": np.nan, "DISTANCE_FROM_DOOR_M": np.nan, "STATUS": "OVERFLOW"})
                    allocations.append(out)
            allocation = pd.DataFrame(allocations)
            allocated = allocation[allocation.STATUS == "ALLOCATED"]
            comparison = pd.DataFrame([{
                "STRATEGY": strategy,
                "SELECTED_MODEL1_SLOTS": len(slots),
                "ALLOCATED_SLOTS": len(allocated),
                "OVERFLOW_SLOT_EQUIVALENTS": int((allocation.STATUS == "OVERFLOW").sum()),
                "SLOT_UTILIZATION_PCT": len(allocated) / len(slots) * 100,
                "BELTS_PER_SLOT_SCENARIO": density,
            }])

        st.dataframe(comparison, use_container_width=True)
        st.subheader("Selected Model 1 slots → Model 2 allocation")
        st.dataframe(allocation.head(500), use_container_width=True)

        st.header("7 — Download outputs")
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("01_Derived_Daily_Demand.csv", r["derived_daily_demand"].to_csv(index=False))
            z.writestr("02_Daily_Demand_Detail.csv", r["demand_detail"].to_csv(index=False))
            z.writestr("03_Model1_Full_Slot_Master.csv", r["slot_df"].to_csv(index=False))
            z.writestr("04_Selected_Area_Slot_Master.csv", slots.to_csv(index=False))
            z.writestr("05_Selected_Area_Daily_Simulation.csv", daily.to_csv(index=False))
            z.writestr("06_SKU_Movement_Master.csv", sku.to_csv(index=False))
            z.writestr("07_Model2_Slot_Allocation.csv", allocation.to_csv(index=False))
            z.writestr("08_Model2_Comparison.csv", comparison.to_csv(index=False))
            z.writestr("09_Parameters.csv", pd.DataFrame([{
                **r["config"], "selected_area_xmin_m": rr[0], "selected_area_xmax_m": rr[1],
                "selected_area_ymin_m": rr[2], "selected_area_ymax_m": rr[3],
                "selected_slots": len(slots), "model2_strategy": strategy, "scenario_belts_per_slot": density,
            }]).to_csv(index=False))
            z.writestr("README.txt", "Daily demand is derived from the MTO Consolidated Master and Size–Box Master. Model 2 uses only the physical slots generated by Model 1 inside the user-selected study area. Scenario density is a planning parameter, not an observed capacity.\n")
        st.download_button("⬇ Download complete analysis ZIP", zbuf.getvalue(), file_name="JK_Fenner_Digital_Twin_Outputs.zip", mime="application/zip", use_container_width=True)
