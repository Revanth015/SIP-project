"""JK Fenner Warehouse Digital Twin — Model 1 physical twin + Model 2 frequency slotting."""
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

st.set_page_config(page_title="JK Fenner Warehouse Digital Twin", page_icon="🏭", layout="wide")
st.title("🏭 JK Fenner Warehouse Digital Twin")
st.caption("CAD → physical slots → exact study area → frequency-matrix slotting → daily flow simulation")

FALLBACK_BELTS_PER_BOX = 28


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
    for name in names:
        if name.upper() in lookup:
            return lookup[name.upper()]
    if required:
        raise ValueError(f"Missing one of {names}. Available columns: {list(df.columns)}")
    return None


def derive_daily_demand(mto_path, box_path):
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
    d = d[d["BELTS"] > 0].drop_duplicates().copy()
    d = d.merge(box[["ITEM_SIZE", "BELTS_PER_BOX"]], on="ITEM_SIZE", how="left")
    d["BELTS_PER_BOX_SOURCE"] = np.where(d["BELTS_PER_BOX"].notna(), "Size–Box Master", f"Fallback = {FALLBACK_BELTS_PER_BOX}")
    d["BELTS_PER_BOX"] = d["BELTS_PER_BOX"].fillna(FALLBACK_BELTS_PER_BOX)
    d["BOXES"] = np.ceil(d["BELTS"] / d["BELTS_PER_BOX"]).astype(int)
    daily = d.groupby("DATE", as_index=False).agg(TOTAL_BELTS=("BELTS", "sum"), TOTAL_BOXES=("BOXES", "sum"), TRANSACTION_LINES=("ITEM_SIZE", "size"))
    daily["TOTAL_PALLETS"] = np.ceil(daily["TOTAL_BOXES"] / 32.0).astype(int)
    return daily.sort_values("DATE").reset_index(drop=True), d


def cad_plot(cad, door=None, turn=None):
    fig = go.Figure()
    x, y = cad["warehouse"].exterior.xy
    fig.add_trace(go.Scatter(x=list(x), y=list(y), mode="lines", line=dict(width=3), name="Warehouse"))
    for o in cad.get("obstacles", []):
        x, y = o.exterior.xy
        fig.add_trace(go.Scatter(x=list(x), y=list(y), mode="lines", fill="toself", opacity=.2, showlegend=False))
    for i, d in enumerate(cad.get("doors", []), 1):
        fig.add_trace(go.Scatter(x=[d["x"]], y=[d["y"]], mode="markers+text", text=[f"D{i}"], textposition="top center", marker=dict(size=14, symbol="star"), name=f"CAD Door {i}"))
    if door:
        fig.add_trace(go.Scatter(x=[door.x], y=[door.y], mode="markers+text", text=["OPERATING DOOR"], textposition="bottom center", marker=dict(size=15, symbol="x"), name="Operating door"))
    if turn:
        c, rad = turn[0], turn[1] / 2
        fig.add_shape(type="circle", x0=c.x-rad, x1=c.x+rad, y0=c.y-rad, y1=c.y+rad, line=dict(dash="dot"))
    fig.update_layout(height=600, title="CAD / operating door / turning zone", xaxis_title="X (m)", yaxis_title="Y (m)", margin=dict(l=20,r=20,t=50,b=20))
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def m1_plot(r, title="Model 1 — generated physical slots", selectable=False, allocation=None):
    fig = go.Figure()
    x, y = r["warehouse"].exterior.xy
    fig.add_trace(go.Scatter(x=list(x), y=list(y), mode="lines", line=dict(width=3), name="Warehouse"))
    for o in r.get("obstacles", []):
        x, y = o.exterior.xy
        fig.add_trace(go.Scatter(x=list(x), y=list(y), mode="lines", fill="toself", opacity=.2, showlegend=False))
    s = r["slot_df"].copy()
    if allocation is not None and not allocation.empty:
        a = allocation.dropna(subset=["SLOT_ID"])[["SLOT_ID","FREQUENCY_CLASS","MOVEMENT_SEGMENT","ITEM_SIZE","FREQUENCY_RANK"]].drop_duplicates("SLOT_ID")
        s = s.merge(a, on="SLOT_ID", how="left")
        assigned = s[s["ITEM_SIZE"].notna()]
        unassigned = s[s["ITEM_SIZE"].isna()]
        fig.add_trace(go.Scatter(x=unassigned.X_M, y=unassigned.Y_M, mode="markers", marker=dict(size=7, symbol="square-open"), name="Unassigned Model 1 slots", hovertemplate="%{x:.2f}, %{y:.2f}<extra></extra>"))
        fig.add_trace(go.Scatter(x=assigned.X_M, y=assigned.Y_M, mode="markers", marker=dict(size=9, symbol="square"), name="Frequency-assigned slots", customdata=np.c_[assigned.SLOT_ID,assigned.ITEM_SIZE,assigned.FREQUENCY_RANK,assigned.MOVEMENT_SEGMENT], hovertemplate="Slot %{customdata[0]}<br>%{customdata[1]}<br>Frequency rank: %{customdata[2]}<br>%{customdata[3]}<extra></extra>"))
    else:
        fig.add_trace(go.Scatter(x=s.X_M, y=s.Y_M, mode="markers", marker=dict(size=6, symbol="square-open"), name="Model 1 slots", customdata=s.SLOT_ID, hovertemplate="%{customdata}<br>X=%{x:.2f}<br>Y=%{y:.2f}<extra></extra>"))
    d = r["door"]
    fig.add_trace(go.Scatter(x=[d.x], y=[d.y], mode="markers+text", text=["DOOR"], textposition="top center", marker=dict(size=15, symbol="star"), name="Operating door"))
    tc = r.get("turning_center")
    if r.get("turning_enabled") and tc:
        rr = r.get("turning_diameter_m", 7) / 2
        fig.add_shape(type="circle", x0=tc.x-rr, x1=tc.x+rr, y0=tc.y-rr, y1=tc.y+rr, line=dict(dash="dot"))
    fig.update_layout(height=700, title=title, xaxis_title="X (m)", yaxis_title="Y (m)", dragmode="select" if selectable else "zoom", margin=dict(l=20,r=20,t=50,b=20))
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def event_range(event):
    if event is None:
        return None
    try:
        data = event.to_dict()
    except Exception:
        try:
            data = dict(event)
        except Exception:
            return None
    sel = data.get("selection", {})
    if hasattr(sel, "to_dict"):
        sel = sel.to_dict()
    boxes = sel.get("box", []) if isinstance(sel, dict) else []
    if isinstance(boxes, dict):
        boxes = [boxes]
    for b in reversed(boxes):
        if hasattr(b, "to_dict"):
            b = b.to_dict()
        rng = b.get("range", b) if isinstance(b, dict) else {}
        if hasattr(rng, "to_dict"):
            rng = rng.to_dict()
        if isinstance(rng, dict) and rng.get("x") is not None and rng.get("y") is not None:
            xr, yr = rng["x"], rng["y"]
            return float(min(xr)), float(max(xr)), float(min(yr)), float(max(yr))
    return None


def slots_in(df, region):
    mask = df.apply(lambda q: region.covers(Point(float(q.X_M), float(q.Y_M))), axis=1)
    return df[mask].copy().sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)


def build_frequency_matrix(mto_detail):
    d = mto_detail[["DATE", "ITEM_SIZE", "BELTS"]].copy()
    d["ITEM_SIZE"] = d["ITEM_SIZE"].astype("string").str.strip().str.upper()
    d["BELTS"] = pd.to_numeric(d["BELTS"], errors="coerce")
    d = d.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])
    d = d[d["BELTS"] > 0]
    sku = d.groupby("ITEM_SIZE", as_index=False).agg(
        MTO_BELTS=("BELTS", "sum"),
        MTO_LINES=("ITEM_SIZE", "size"),
        ACTIVE_DAYS=("DATE", "nunique"),
        ACTIVE_MONTHS=("DATE", lambda x: x.dt.to_period("M").nunique()),
        AVG_BELTS_PER_LINE=("BELTS", "mean"),
    )
    freq_cut = float(sku["ACTIVE_DAYS"].median())
    vol_cut = float(sku["MTO_BELTS"].median())
    sku["FREQUENCY_CLASS"] = np.where(sku["ACTIVE_DAYS"] >= freq_cut, "High Frequency", "Low Frequency")
    sku["VOLUME_CLASS"] = np.where(sku["MTO_BELTS"] >= vol_cut, "High Volume", "Low Volume")
    sku["MOVEMENT_SEGMENT"] = sku["FREQUENCY_CLASS"] + " / " + sku["VOLUME_CLASS"]
    priority = {
        "High Frequency / High Volume": 1,
        "High Frequency / Low Volume": 2,
        "Low Frequency / High Volume": 3,
        "Low Frequency / Low Volume": 4,
    }
    sku["MATRIX_PRIORITY"] = sku["MOVEMENT_SEGMENT"].map(priority)
    sku = sku.sort_values(["MATRIX_PRIORITY", "ACTIVE_DAYS", "MTO_BELTS", "MTO_LINES"], ascending=[True, False, False, False]).reset_index(drop=True)
    sku["FREQUENCY_RANK"] = np.arange(1, len(sku) + 1)
    return sku, freq_cut, vol_cut


def assign_frequency_slots(sku, slots):
    """One physical Model 1 slot per prioritized SKU; no belts/slot assumption."""
    slots = slots.sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True).copy()
    n = min(len(slots), len(sku))
    ranked = sku.head(n).copy()
    out = slots.head(n).copy()
    out["ITEM_SIZE"] = ranked["ITEM_SIZE"].to_numpy()
    out["FREQUENCY_RANK"] = ranked["FREQUENCY_RANK"].to_numpy()
    out["ACTIVE_DAYS"] = ranked["ACTIVE_DAYS"].to_numpy()
    out["MTO_LINES"] = ranked["MTO_LINES"].to_numpy()
    out["MTO_BELTS"] = ranked["MTO_BELTS"].to_numpy()
    out["FREQUENCY_CLASS"] = ranked["FREQUENCY_CLASS"].to_numpy()
    out["VOLUME_CLASS"] = ranked["VOLUME_CLASS"].to_numpy()
    out["MOVEMENT_SEGMENT"] = ranked["MOVEMENT_SEGMENT"].to_numpy()
    out["MATRIX_PRIORITY"] = ranked["MATRIX_PRIORITY"].to_numpy()
    out["STATUS"] = "ASSIGNED"
    return out


def simulate_daily_flow(mto_detail, allocation, total_daily):
    """Replay historical MTO flow through the assigned SKU locations day by day."""
    flow = mto_detail[["DATE", "ITEM_SIZE", "BELTS"]].copy()
    flow["ITEM_SIZE"] = flow["ITEM_SIZE"].astype("string").str.strip().str.upper()
    flow["BELTS"] = pd.to_numeric(flow["BELTS"], errors="coerce")
    flow = flow.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])
    flow = flow[flow["BELTS"] > 0].copy()
    a = allocation[["ITEM_SIZE", "SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M", "FREQUENCY_RANK", "MOVEMENT_SEGMENT"]].copy()
    flow = flow.merge(a, on="ITEM_SIZE", how="left")
    flow["ASSIGNED"] = flow["SLOT_ID"].notna()
    flow["TRAVEL_DISTANCE_M"] = flow["DISTANCE_FROM_DOOR_M"].fillna(0)
    flow["TRAVEL_DISTANCE_ASSIGNED_M"] = np.where(flow["ASSIGNED"], flow["DISTANCE_FROM_DOOR_M"], 0)
    daily = flow.groupby("DATE", as_index=False).agg(
        FLOW_LINES=("ITEM_SIZE", "size"),
        FLOW_BELTS=("BELTS", "sum"),
        ASSIGNED_LINES=("ASSIGNED", "sum"),
        ASSIGNED_BELTS=("BELTS", lambda s: float(s[flow.loc[s.index, "ASSIGNED"]].sum())),
        EST_ONE_WAY_TRAVEL_M=("TRAVEL_DISTANCE_ASSIGNED_M", "sum"),
        ASSIGNED_SKUS=("ITEM_SIZE", lambda s: int(flow.loc[s.index & flow["ASSIGNED"], "ITEM_SIZE"].nunique()) if False else 0),
    )
    # Recalculate assigned SKU count and percentages cleanly.
    sku_count = flow[flow["ASSIGNED"]].groupby("DATE")["ITEM_SIZE"].nunique().rename("ASSIGNED_SKUS")
    daily = daily.drop(columns=["ASSIGNED_SKUS"]).merge(sku_count, on="DATE", how="left").fillna({"ASSIGNED_SKUS": 0})
    daily["ASSIGNED_LINES"] = daily["ASSIGNED_LINES"].astype(int)
    daily["UNASSIGNED_LINES"] = daily["FLOW_LINES"] - daily["ASSIGNED_LINES"]
    daily["UNASSIGNED_BELTS"] = daily["FLOW_BELTS"] - daily["ASSIGNED_BELTS"]
    daily["FLOW_COVERAGE_BY_LINES_%"] = daily["ASSIGNED_LINES"] / daily["FLOW_LINES"].replace(0, np.nan) * 100
    daily["FLOW_COVERAGE_BY_BELTS_%"] = daily["ASSIGNED_BELTS"] / daily["FLOW_BELTS"].replace(0, np.nan) * 100
    daily["AVG_TRAVEL_M_PER_ASSIGNED_LINE"] = daily["EST_ONE_WAY_TRAVEL_M"] / daily["ASSIGNED_LINES"].replace(0, np.nan)
    daily = daily.sort_values("DATE").reset_index(drop=True)
    summary = {
        "operating_days": int(len(daily)),
        "assigned_skus": int(allocation["ITEM_SIZE"].nunique()),
        "available_slots": int(len(allocation)),
        "flow_lines": int(flow.shape[0]),
        "assigned_lines": int(flow["ASSIGNED"].sum()),
        "line_coverage_pct": float(flow["ASSIGNED"].mean() * 100),
        "flow_belts": float(flow["BELTS"].sum()),
        "assigned_belts": float(flow.loc[flow["ASSIGNED"], "BELTS"].sum()),
        "belt_coverage_pct": float(flow.loc[flow["ASSIGNED"], "BELTS"].sum() / max(flow["BELTS"].sum(), 1) * 100),
        "total_one_way_travel_m": float(flow["TRAVEL_DISTANCE_ASSIGNED_M"].sum()),
        "avg_one_way_m_per_assigned_line": float(flow.loc[flow["ASSIGNED"], "DISTANCE_FROM_DOOR_M"].mean()),
    }
    return flow, daily, summary


def reset():
    st.session_state.clear()
    st.rerun()


with st.sidebar:
    st.header("Inputs")
    cad_file = st.file_uploader("1. Warehouse CAD", type=["dxf"])
    mto_file = st.file_uploader("2. MTO Consolidated Master", type=["xlsx", "csv"])
    mta_file = st.file_uploader("3. MTA Consolidated Master", type=["xlsx", "csv"])
    box_file = st.file_uploader("4. Size–Box Master", type=["xlsx", "csv"])
    st.caption("Daily demand is derived automatically from the MTO Master + Size–Box Master.")
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
            r = run_pipeline(st.session_state.cad_path, demand_path.name, manual_door_xy=(dx, dy), pallet_width_m=pw, pallet_depth_m=pd_, wall_clearance_m=wall, main_aisle_m=aisle, cross_aisle_m=cross, turning_diameter_m=td, turning_enabled=ton, turning_center_xy=(turn.x, turn.y) if turn else None, target_occupancy_pct=occ, forklift_speed_mps=speed, handling_time_sec_per_pallet=handle)
            r["derived_daily_demand"] = daily_demand
            r["demand_detail"] = demand_detail
            r["config"] = {"pallet_width_m":pw,"pallet_depth_m":pd_,"wall_clearance_m":wall,"main_aisle_m":aisle,"cross_aisle_m":cross,"turning_enabled":ton,"turning_diameter_m":td if ton else 0,"turning_center_x_m":turn.x if turn else None,"turning_center_y_m":turn.y if turn else None,"target_occupancy_pct":occ,"forklift_speed_mps":speed,"handling_time_sec_per_pallet":handle,"operating_door_x_m":dx,"operating_door_y_m":dy,"door_source":door_source,"daily_demand_source":"Derived from MTO Consolidated Master + Size–Box Master"}
            st.session_state.m1_result = r
            st.session_state.pop("area_range", None)
            st.session_state.pop("area_region", None)
            st.session_state.pop("frequency_result", None)
        st.success(f"Model 1 completed: {len(r['slot_df']):,} feasible slots.")
    except Exception as e:
        st.error(f"Model 1 failed: {e}")

if "m1_result" in st.session_state:
    r = st.session_state.m1_result
    st.header("4 — Select the exact study area")
    st.info("Choose Box Select (▭) in the chart toolbar and drag a rectangle over the exact area you want to improve. Model 2 will use only the Model 1 slots inside that rectangle.")
    event = st.plotly_chart(m1_plot(r, "SELECT STUDY AREA — Box Select", True), use_container_width=True, key="area_selector", on_select="rerun", selection_mode="box")
    rr = event_range(event)
    if rr is not None:
        st.session_state.area_range = rr
        st.session_state.area_region = r["warehouse"].intersection(sbox(rr[0], rr[2], rr[1], rr[3]))
    if st.session_state.get("area_range"):
        rr = st.session_state.area_range
        region = st.session_state.area_region
        slots = slots_in(r["slot_df"], region)
        st.success(f"Study area selected: X {rr[0]:.2f}–{rr[1]:.2f} m · Y {rr[2]:.2f}–{rr[3]:.2f} m · **{len(slots):,} existing Model 1 slots**")
        if len(slots) == 0:
            st.warning("No Model 1 slots are inside this rectangle. Select an area containing generated slot markers.")
        else:
            st.header("5 — Selected-area Model 1 flow simulation")
            daily = r["derived_daily_demand"].copy()
            peak = int(daily["TOTAL_PALLETS"].max()) if not daily.empty else 0
            avg = float(daily["TOTAL_PALLETS"].mean()) if not daily.empty else 0
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Selected Model 1 slots", len(slots))
            c2.metric("Average daily pallets", f"{avg:.1f}")
            c3.metric("Peak daily pallets", peak)
            c4.metric("Operating days", len(daily))
            st.dataframe(daily, use_container_width=True)

            st.header("6 — Model 2: Frequency Matrix Slotting + Daily Flow")
            st.markdown("**Rule:** one existing Model 1 slot is assigned to each prioritized SKU. Closest-to-door slots receive the highest movement priority. No belts/slot assumption is used.")
            if st.button("▶ Run Frequency Matrix + Daily Flow Simulation", type="primary", use_container_width=True):
                try:
                    with st.spinner("Building the frequency matrix, assigning the selected Model 1 slots and replaying daily MTO flow…"):
                        sku, freq_cut, vol_cut = build_frequency_matrix(r["demand_detail"])
                        allocation = assign_frequency_slots(sku, slots)
                        flow, daily_flow, summary = simulate_daily_flow(r["demand_detail"], allocation, daily)
                        st.session_state.frequency_result = {"sku":sku,"allocation":allocation,"flow":flow,"daily_flow":daily_flow,"summary":summary,"freq_cut":freq_cut,"vol_cut":vol_cut}
                except Exception as e:
                    st.error(f"Frequency Matrix simulation failed: {e}")

            if "frequency_result" in st.session_state:
                fr = st.session_state.frequency_result
                summary = fr["summary"]
                st.success(f"Frequency Matrix simulation completed: {summary['assigned_skus']:,} SKUs assigned to {summary['available_slots']:,} selected physical slots.")
                st.write(f"**Frequency threshold:** {fr['freq_cut']:.0f} active days (median)  |  **Volume threshold:** {fr['vol_cut']:.1f} MTO belts (median)")
                c1,c2,c3,c4 = st.columns(4)
                c1.metric("Assigned SKUs", summary["assigned_skus"])
                c2.metric("Flow-line coverage", f"{summary['line_coverage_pct']:.1f}%")
                c3.metric("Belt-flow coverage", f"{summary['belt_coverage_pct']:.1f}%")
                c4.metric("One-way travel", f"{summary['total_one_way_travel_m']:,.0f} m")

                st.subheader("Frequency matrix")
                matrix = fr["sku"].groupby(["FREQUENCY_CLASS","VOLUME_CLASS"], as_index=False).agg(SKUs=("ITEM_SIZE","count"), MTO_BELTS=("MTO_BELTS","sum"), MTO_LINES=("MTO_LINES","sum"))
                st.dataframe(matrix, use_container_width=True)

                st.subheader("Selected Model 1 slots with frequency assignment")
                st.dataframe(fr["allocation"], use_container_width=True)
                st.plotly_chart(m1_plot(r, "MODEL 2 — Frequency-matrix allocation inside selected Model 1 area", False, fr["allocation"]), use_container_width=True)

                st.subheader("Daily flow simulation")
                st.dataframe(fr["daily_flow"], use_container_width=True)
                fig = go.Figure()
                df = fr["daily_flow"]
                fig.add_trace(go.Scatter(x=df["DATE"], y=df["EST_ONE_WAY_TRAVEL_M"], mode="lines+markers", name="Estimated one-way travel (m)"))
                fig.update_layout(height=450, title="Daily MTO flow → estimated travel from assigned frequency-priority slots", xaxis_title="Date", yaxis_title="Travel distance (m)")
                st.plotly_chart(fig, use_container_width=True)

                st.subheader("Interpretation")
                st.info("The simulation replays the historical daily MTO flow through the assigned SKU locations. Travel is based on each SKU's Model 1 slot distance from the operating door. This is a flow/workload scenario, not an inventory-capacity claim.")

                st.header("7 — Download outputs")
                zbuf = io.BytesIO()
                with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("01_Derived_Daily_Demand.csv", r["derived_daily_demand"].to_csv(index=False))
                    z.writestr("02_Daily_Demand_Detail.csv", r["demand_detail"].to_csv(index=False))
                    z.writestr("03_Model1_Full_Slot_Master.csv", r["slot_df"].to_csv(index=False))
                    z.writestr("04_Selected_Area_Slot_Master.csv", slots.to_csv(index=False))
                    z.writestr("05_Frequency_Matrix_SKU_Master.csv", fr["sku"].to_csv(index=False))
                    z.writestr("06_Frequency_Matrix_Slot_Assignment.csv", fr["allocation"].to_csv(index=False))
                    z.writestr("07_Daily_Flow_Simulation.csv", fr["daily_flow"].to_csv(index=False))
                    z.writestr("08_MTO_Flow_With_Assigned_Slots.csv", fr["flow"].to_csv(index=False))
                    z.writestr("09_Model_Parameters.csv", pd.DataFrame([r["config"]]).to_csv(index=False))
                st.download_button("⬇ Download ZIP — Model 1 + Frequency Matrix + Daily Flow", zbuf.getvalue(), "JK_Fenner_Frequency_Matrix_Daily_Flow_Output.zip", "application/zip", type="primary", use_container_width=True)
