"""JK Fenner Warehouse Digital Twin — Model 1 physical twin + comparative Model 2 slotting."""
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
st.caption("CAD → physical slots → study area → Model 1 dashboard → comparative SKU slotting")

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
        cols = ["SLOT_ID", "ITEM_SIZE", "STRATEGY", "RANK", "MOVEMENT_SEGMENT"]
        a = allocation[[c for c in cols if c in allocation.columns]].drop_duplicates("SLOT_ID")
        s = s.merge(a, on="SLOT_ID", how="left")
        assigned = s[s["ITEM_SIZE"].notna()]
        unassigned = s[s["ITEM_SIZE"].isna()]
        fig.add_trace(go.Scatter(x=unassigned.X_M, y=unassigned.Y_M, mode="markers", marker=dict(size=7, symbol="square-open"), name="Unassigned Model 1 slots"))
        fig.add_trace(go.Scatter(x=assigned.X_M, y=assigned.Y_M, mode="markers", marker=dict(size=9, symbol="square"), name="Assigned slots", customdata=np.c_[assigned.SLOT_ID, assigned.ITEM_SIZE, assigned.RANK, assigned.MOVEMENT_SEGMENT], hovertemplate="Slot %{customdata[0]}<br>SKU %{customdata[1]}<br>Rank %{customdata[2]}<br>%{customdata[3]}<extra></extra>"))
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


# --------------------------- Model 2 ---------------------------

def build_sku_master(mto_detail):
    """Build the evidence table used by all three Model 2 strategies."""
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
        MAX_BELTS_PER_LINE=("BELTS", "max"),
    )
    sku["MOVEMENT_SHARE_PCT"] = sku["MTO_BELTS"] / max(float(sku["MTO_BELTS"].sum()), 1.0) * 100
    # Frequency is transaction occurrence, not a simultaneous inventory quantity.
    sku["FREQUENCY_RANK"] = sku["MTO_LINES"].rank(method="min", ascending=False).astype(int)
    sku["VOLUME_RANK"] = sku["MTO_BELTS"].rank(method="min", ascending=False).astype(int)
    sku["FREQUENCY_SCORE"] = sku["MTO_LINES"].rank(method="average", pct=True)
    sku["VOLUME_SCORE"] = sku["MTO_BELTS"].rank(method="average", pct=True)
    sku["COMBINED_SCORE"] = 0.5 * sku["FREQUENCY_SCORE"] + 0.5 * sku["VOLUME_SCORE"]
    sku["FREQUENCY_CLASS"] = np.where(sku["MTO_LINES"] >= sku["MTO_LINES"].quantile(.75), "High Frequency", "Standard Frequency")
    sku["VOLUME_CLASS"] = np.where(sku["MTO_BELTS"] >= sku["MTO_BELTS"].quantile(.75), "High Volume", "Standard Volume")
    sku["MOVEMENT_SEGMENT"] = sku["FREQUENCY_CLASS"] + " / " + sku["VOLUME_CLASS"]
    return sku.sort_values(["COMBINED_SCORE", "MTO_LINES", "MTO_BELTS"], ascending=False).reset_index(drop=True)


def strategy_ranking(sku, strategy):
    if strategy == "Frequency":
        ranked = sku.sort_values(["MTO_LINES", "ACTIVE_DAYS", "MTO_BELTS", "ITEM_SIZE"], ascending=[False, False, False, True]).copy()
        score_col = "FREQUENCY_SCORE"
    elif strategy == "Volume":
        ranked = sku.sort_values(["MTO_BELTS", "MTO_LINES", "ACTIVE_DAYS", "ITEM_SIZE"], ascending=[False, False, False, True]).copy()
        score_col = "VOLUME_SCORE"
    else:
        ranked = sku.sort_values(["COMBINED_SCORE", "MTO_LINES", "MTO_BELTS", "ITEM_SIZE"], ascending=[False, False, False, True]).copy()
        score_col = "COMBINED_SCORE"
    ranked = ranked.reset_index(drop=True)
    ranked["RANK"] = np.arange(1, len(ranked) + 1)
    ranked["STRATEGY_SCORE"] = ranked[score_col]
    ranked["STRATEGY"] = strategy
    return ranked


def allocate_strategy(ranked, slots):
    slots = slots.sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True).copy()
    n = min(len(slots), len(ranked))
    a = slots.head(n).copy()
    s = ranked.head(n).copy()
    a["ITEM_SIZE"] = s["ITEM_SIZE"].to_numpy()
    a["RANK"] = s["RANK"].to_numpy()
    a["STRATEGY"] = s["STRATEGY"].to_numpy()
    a["STRATEGY_SCORE"] = s["STRATEGY_SCORE"].to_numpy()
    a["MTO_LINES"] = s["MTO_LINES"].to_numpy()
    a["MTO_BELTS"] = s["MTO_BELTS"].to_numpy()
    a["ACTIVE_DAYS"] = s["ACTIVE_DAYS"].to_numpy()
    a["MOVEMENT_SHARE_PCT"] = s["MOVEMENT_SHARE_PCT"].to_numpy()
    a["FREQUENCY_CLASS"] = s["FREQUENCY_CLASS"].to_numpy()
    a["VOLUME_CLASS"] = s["VOLUME_CLASS"].to_numpy()
    a["MOVEMENT_SEGMENT"] = s["MOVEMENT_SEGMENT"].to_numpy()
    return a


def simulate_strategy(mto_detail, allocation, strategy):
    flow = mto_detail[["DATE", "ITEM_SIZE", "BELTS"]].copy()
    flow["ITEM_SIZE"] = flow["ITEM_SIZE"].astype("string").str.strip().str.upper()
    flow["BELTS"] = pd.to_numeric(flow["BELTS"], errors="coerce")
    flow = flow.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])
    flow = flow[flow["BELTS"] > 0].copy()
    cols = ["ITEM_SIZE", "SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M", "RANK", "STRATEGY"]
    flow = flow.merge(allocation[cols], on="ITEM_SIZE", how="left")
    flow["ASSIGNED"] = flow["SLOT_ID"].notna()
    flow["ASSIGNED_TRAVEL_M"] = np.where(flow["ASSIGNED"], flow["DISTANCE_FROM_DOOR_M"], 0.0)
    flow["BELT_WEIGHTED_TRAVEL_M"] = flow["BELTS"] * flow["ASSIGNED_TRAVEL_M"]
    daily = flow.groupby("DATE", as_index=False).agg(
        FLOW_LINES=("ITEM_SIZE", "size"),
        FLOW_BELTS=("BELTS", "sum"),
        ASSIGNED_LINES=("ASSIGNED", "sum"),
        ASSIGNED_BELTS=("BELTS", lambda x: float(x[flow.loc[x.index, "ASSIGNED"]].sum())),
        EST_ONE_WAY_TRAVEL_M=("ASSIGNED_TRAVEL_M", "sum"),
        BELT_WEIGHTED_TRAVEL_M=("BELT_WEIGHTED_TRAVEL_M", "sum"),
        ASSIGNED_SKUS=("ITEM_SIZE", lambda x: x[flow.loc[x.index, "ASSIGNED"]].nunique()),
    )
    daily["UNASSIGNED_LINES"] = daily["FLOW_LINES"] - daily["ASSIGNED_LINES"]
    daily["UNASSIGNED_BELTS"] = daily["FLOW_BELTS"] - daily["ASSIGNED_BELTS"]
    daily["LINE_COVERAGE_PCT"] = daily["ASSIGNED_LINES"] / daily["FLOW_LINES"].replace(0, np.nan) * 100
    daily["BELT_COVERAGE_PCT"] = daily["ASSIGNED_BELTS"] / daily["FLOW_BELTS"].replace(0, np.nan) * 100
    daily["AVG_TRAVEL_M_PER_ASSIGNED_LINE"] = daily["EST_ONE_WAY_TRAVEL_M"] / daily["ASSIGNED_LINES"].replace(0, np.nan)
    daily["STRATEGY"] = strategy
    total_lines = len(flow)
    total_belts = float(flow["BELTS"].sum())
    assigned = flow[flow["ASSIGNED"]]
    total_travel = float(assigned["ASSIGNED_TRAVEL_M"].sum())
    weighted_travel = float(assigned["BELT_WEIGHTED_TRAVEL_M"].sum())
    assigned_lines = int(assigned.shape[0])
    assigned_belts = float(assigned["BELTS"].sum())
    summary = {
        "Strategy": strategy,
        "Assigned SKUs": int(allocation["ITEM_SIZE"].nunique()),
        "Available Model 1 slots": int(len(allocation)),
        "Historical flow lines": int(total_lines),
        "Assigned flow lines": assigned_lines,
        "Line coverage %": assigned_lines / max(total_lines, 1) * 100,
        "Historical MTO belts": total_belts,
        "Assigned MTO belts": assigned_belts,
        "Belt coverage %": assigned_belts / max(total_belts, 1) * 100,
        "Total one-way travel m": total_travel,
        "Avg one-way m / assigned line": total_travel / max(assigned_lines, 1),
        "Belt-weighted travel m": weighted_travel,
        "Avg slot distance m": float(assigned["DISTANCE_FROM_DOOR_M"].mean()) if assigned_lines else 0.0,
        "Max assigned slot distance m": float(assigned["DISTANCE_FROM_DOOR_M"].max()) if assigned_lines else 0.0,
    }
    return flow, daily, summary


def compare_strategies(summary_df):
    x = summary_df.copy()
    # Equal-weight decision score: movement coverage + volume coverage + travel efficiency.
    x["Travel_Efficiency"] = 1 - (x["Total one-way travel m"] - x["Total one-way travel m"].min()) / max(x["Total one-way travel m"].max() - x["Total one-way travel m"].min(), 1e-9)
    x["Balanced_Performance_Score"] = (x["Line coverage %"] / 100 + x["Belt coverage %"] / 100 + x["Travel_Efficiency"]) / 3 * 100
    x["Overall_Rank"] = x["Balanced_Performance_Score"].rank(method="min", ascending=False).astype(int)
    return x.sort_values("Overall_Rank").reset_index(drop=True)


def reset():
    st.session_state.clear()
    st.rerun()


with st.sidebar:
    st.header("Inputs")
    cad_file = st.file_uploader("1. Warehouse CAD", type=["dxf"])
    mto_file = st.file_uploader("2. MTO Consolidated Master", type=["xlsx", "csv"])
    mta_file = st.file_uploader("3. MTA Consolidated Master", type=["xlsx", "csv"])
    box_file = st.file_uploader("4. Size–Box Master", type=["xlsx", "csv"])
    st.caption("Daily demand and Model 2 flow are derived from the MTO Master + Size–Box Master.")
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
            st.session_state.pop("model2_result", None)
        st.success(f"Model 1 completed: {len(r['slot_df']):,} feasible slots.")
    except Exception as e:
        st.error(f"Model 1 failed: {e}")

if "m1_result" in st.session_state:
    r = st.session_state.m1_result
    st.header("4 — Select the exact study area")
    st.info("Choose Box Select (▭) in the chart toolbar and drag a rectangle over the exact area you want to improve. Model 2 uses the physical slots produced by Model 1 inside that selected area.")
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
            st.header("5 — Selected-area Model 1 flow dashboard")
            daily = r["derived_daily_demand"].copy()
            peak = int(daily["TOTAL_PALLETS"].max()) if not daily.empty else 0
            avg = float(daily["TOTAL_PALLETS"].mean()) if not daily.empty else 0
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Selected Model 1 slots", len(slots))
            c2.metric("Average daily pallets", f"{avg:.1f}")
            c3.metric("Peak daily pallets", peak)
            c4.metric("Operating days", len(daily))
            st.dataframe(daily, use_container_width=True)
            fig5 = go.Figure()
            fig5.add_trace(go.Scatter(x=daily["DATE"], y=daily["TOTAL_PALLETS"], mode="lines+markers", name="Daily pallets"))
            fig5.update_layout(height=400, title="Daily pallet flow used by Model 1", xaxis_title="Date", yaxis_title="Pallets")
            st.plotly_chart(fig5, use_container_width=True)

            st.header("6 — Model 2: Comparative SKU Slotting Analysis")
            st.markdown("Model 2 uses the **physical slot count produced by Process 4**. It does not create an independent storage capacity assumption. Three SKU strategies are tested on the same Process-4 output: **Frequency**, **Volume**, and **Frequency + Volume**.")
            st.caption("Frequency = MTO transaction-line occurrence. Volume = total MTO belts. Combined = equal-weight normalized frequency and volume scores. The same physical slots and historical MTO flow are used for all three scenarios.")
            if st.button("▶ Run all 3 Model 2 strategies", type="primary", use_container_width=True):
                try:
                    with st.spinner("Ranking SKUs, assigning the Process-4 physical slots and replaying the historical MTO flow for all three strategies…"):
                        sku_master = build_sku_master(r["demand_detail"])
                        results = {}
                        summaries = []
                        for strategy in ["Frequency", "Volume", "Frequency + Volume"]:
                            ranked = strategy_ranking(sku_master, strategy)
                            allocation = allocate_strategy(ranked, slots)
                            flow, daily_flow, summary = simulate_strategy(r["demand_detail"], allocation, strategy)
                            results[strategy] = {"ranked": ranked, "allocation": allocation, "flow": flow, "daily": daily_flow, "summary": summary}
                            summaries.append(summary)
                        comparison = compare_strategies(pd.DataFrame(summaries))
                        st.session_state.model2_result = {"sku_master": sku_master, "results": results, "comparison": comparison, "slots": slots}
                except Exception as e:
                    st.error(f"Model 2 failed: {e}")

            if "model2_result" in st.session_state:
                m2 = st.session_state.model2_result
                comparison = m2["comparison"]
                winner = comparison.iloc[0]
                st.success(f"Recommended strategy from the comparative model: **{winner['Strategy']}** — Balanced Performance Score {winner['Balanced_Performance_Score']:.1f}/100.")

                st.subheader("Model 2 decision dashboard")
                c1,c2,c3,c4 = st.columns(4)
                c1.metric("Best frequency SKU", m2["results"]["Frequency"]["ranked"].iloc[0]["ITEM_SIZE"])
                c2.metric("Best volume SKU", m2["results"]["Volume"]["ranked"].iloc[0]["ITEM_SIZE"])
                c3.metric("Best combined SKU", m2["results"]["Frequency + Volume"]["ranked"].iloc[0]["ITEM_SIZE"])
                c4.metric("Recommended strategy", winner["Strategy"])

                st.subheader("Strategy comparison")
                display_cols = ["Strategy", "Assigned SKUs", "Line coverage %", "Belt coverage %", "Total one-way travel m", "Avg one-way m / assigned line", "Belt-weighted travel m", "Balanced_Performance_Score", "Overall_Rank"]
                st.dataframe(comparison[display_cols].round(3), use_container_width=True)

                fig_cov = go.Figure()
                fig_cov.add_trace(go.Bar(x=comparison["Strategy"], y=comparison["Line coverage %"], name="Line coverage %"))
                fig_cov.add_trace(go.Bar(x=comparison["Strategy"], y=comparison["Belt coverage %"], name="Belt coverage %"))
                fig_cov.update_layout(height=430, barmode="group", title="Historical MTO flow coverage by strategy", yaxis_title="Coverage (%)")
                st.plotly_chart(fig_cov, use_container_width=True)

                fig_travel = go.Figure()
                fig_travel.add_trace(go.Bar(x=comparison["Strategy"], y=comparison["Total one-way travel m"], name="Total one-way travel"))
                fig_travel.update_layout(height=430, title="Estimated one-way travel for assigned historical flow", yaxis_title="Distance (m)")
                st.plotly_chart(fig_travel, use_container_width=True)

                fig_score = go.Figure()
                fig_score.add_trace(go.Bar(x=comparison["Strategy"], y=comparison["Balanced_Performance_Score"], name="Balanced Performance Score"))
                fig_score.update_layout(height=430, title="Overall comparative score", yaxis_title="Score (0–100)", yaxis_range=[0,100])
                st.plotly_chart(fig_score, use_container_width=True)

                st.subheader("Best SKU under each strategy")
                champions = []
                for strategy in ["Frequency", "Volume", "Frequency + Volume"]:
                    top = m2["results"][strategy]["ranked"].iloc[0]
                    champions.append({"Strategy":strategy,"Best SKU":top["ITEM_SIZE"],"Rank":1,"MTO Lines":top["MTO_LINES"],"MTO Belts":top["MTO_BELTS"],"Active Days":top["ACTIVE_DAYS"],"Movement Share %":top["MOVEMENT_SHARE_PCT"],"Strategy Score":top["STRATEGY_SCORE"]})
                st.dataframe(pd.DataFrame(champions).round(3), use_container_width=True)

                st.subheader("SKU ranking analysis")
                for strategy in ["Frequency", "Volume", "Frequency + Volume"]:
                    with st.expander(f"{strategy} — top 25 SKUs", expanded=(strategy == winner["Strategy"])):
                        st.dataframe(m2["results"][strategy]["ranked"].head(25), use_container_width=True)

                st.subheader("Physical allocation by strategy")
                selected_strategy = st.selectbox("View strategy", ["Frequency", "Volume", "Frequency + Volume"], index=["Frequency", "Volume", "Frequency + Volume"].index(winner["Strategy"]))
                selected = m2["results"][selected_strategy]
                st.dataframe(selected["allocation"], use_container_width=True)
                st.plotly_chart(m1_plot(r, f"MODEL 2 — {selected_strategy} allocation using Process-4 slots", False, selected["allocation"]), use_container_width=True)

                st.subheader("Daily simulation analysis")
                daily_frames = []
                for strategy in ["Frequency", "Volume", "Frequency + Volume"]:
                    daily_frames.append(m2["results"][strategy]["daily"])
                all_daily = pd.concat(daily_frames, ignore_index=True)
                st.dataframe(all_daily, use_container_width=True)
                fig_daily = go.Figure()
                for strategy in ["Frequency", "Volume", "Frequency + Volume"]:
                    df = m2["results"][strategy]["daily"]
                    fig_daily.add_trace(go.Scatter(x=df["DATE"], y=df["EST_ONE_WAY_TRAVEL_M"], mode="lines", name=strategy))
                fig_daily.update_layout(height=450, title="Daily estimated travel under the three slotting strategies", xaxis_title="Date", yaxis_title="One-way travel (m)")
                st.plotly_chart(fig_daily, use_container_width=True)

                st.subheader("Model 2 interpretation")
                st.info("The three scenarios use the same physical slots produced by Process 4 and replay the same historical MTO flow. The comparison therefore isolates the effect of SKU ranking/slot assignment. Travel is Euclidean distance from the operating door to the assigned Model 1 slot; it is a comparative workload indicator, not a measured route distance.")

                st.header("7 — Download complete Model 2 analysis")
                zbuf = io.BytesIO()
                with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("01_Derived_Daily_Demand.csv", r["derived_daily_demand"].to_csv(index=False))
                    z.writestr("02_Daily_Demand_Detail.csv", r["demand_detail"].to_csv(index=False))
                    z.writestr("03_Model1_Full_Slot_Master.csv", r["slot_df"].to_csv(index=False))
                    z.writestr("04_Process4_Selected_Area_Slot_Master.csv", slots.to_csv(index=False))
                    z.writestr("05_Model2_SKU_Master.csv", m2["sku_master"].to_csv(index=False))
                    z.writestr("06_Model2_Strategy_Comparison.csv", comparison.to_csv(index=False))
                    for i, strategy in enumerate(["Frequency", "Volume", "Frequency + Volume"], 7):
                        res = m2["results"][strategy]
                        safe = strategy.replace(" ", "_").replace("+", "plus")
                        z.writestr(f"{i:02d}_{safe}_SKU_Ranking.csv", res["ranked"].to_csv(index=False))
                        z.writestr(f"{i:02d}_{safe}_Slot_Allocation.csv", res["allocation"].to_csv(index=False))
                        z.writestr(f"{i:02d}_{safe}_Flow_Detail.csv", res["flow"].to_csv(index=False))
                        z.writestr(f"{i:02d}_{safe}_Daily_Simulation.csv", res["daily"].to_csv(index=False))
                        z.writestr(f"{i:02d}_{safe}_Summary.csv", pd.DataFrame([res["summary"]]).to_csv(index=False))
                    z.writestr("20_Best_SKU_By_Strategy.csv", pd.DataFrame(champions).to_csv(index=False))
                    z.writestr("21_Model1_Parameters.csv", pd.DataFrame([r["config"]]).to_csv(index=False))
                    z.writestr("22_Graph_Data_Strategy_Comparison.csv", comparison[["Strategy","Line coverage %","Belt coverage %","Total one-way travel m","Balanced_Performance_Score"]].to_csv(index=False))
                    z.writestr("23_Graph_Data_Daily_Travel.csv", all_daily.to_csv(index=False))
                    z.writestr("24_Graph_Data_Top_SKUs.csv", pd.concat([m2["results"][s]["ranked"].head(25).assign(STRATEGY=s) for s in ["Frequency","Volume","Frequency + Volume"]], ignore_index=True).to_csv(index=False))
                st.download_button("⬇ Download ZIP — Complete Model 1 + Model 2 Analysis", zbuf.getvalue(), "JK_Fenner_Comparative_Model2_Complete_Analysis.zip", "application/zip", type="primary", use_container_width=True)
