"""Model 2B — movement-aware multi-SKU shared-location layout optimization.

This page extends the existing Model 2 experiment without replacing Model 1.
It tries to reuse Model 1 / Process 4 objects from Streamlit session state. If the
running app does not expose them, the page accepts a Process-4 slot CSV and MTO file.
"""
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.layout_optimizer import STRATEGIES, prepare_sku_demand, evaluate_strategy, compare_layouts

st.set_page_config(page_title="Layout Optimization — JK Fenner", page_icon="📐", layout="wide")
st.title("📐 Model 2B — Multi-SKU Layout Optimization")
st.caption("Same Process-4 physical opportunity → shared SKU locations → movement-aware allocation → historical MTO replay")


def _find_session_df(required):
    for value in st.session_state.values():
        if isinstance(value, pd.DataFrame) and set(required).issubset(value.columns):
            return value.copy()
        if isinstance(value, dict):
            for v in value.values():
                if isinstance(v, pd.DataFrame) and set(required).issubset(v.columns):
                    return v.copy()
    return None


def _find_session_upload(exts):
    for value in st.session_state.values():
        name = getattr(value, "name", "")
        if name and Path(name).suffix.lower() in exts:
            return value
    return None


def _read_upload(upload):
    if Path(upload.name).suffix.lower() == ".csv":
        return pd.read_csv(upload)
    return pd.read_excel(upload)


def _normalise_mto(df):
    lookup = {str(c).strip().upper(): c for c in df.columns}
    def col(names):
        for n in names:
            if n.upper() in lookup:
                return lookup[n.upper()]
        return None
    dc, sc, qc = col(["DATE", "DATETIME", "DAY"]), col(["ITEM_SIZE", "BELT_SIZE", "SKU", "LINE_ITEM_SIZE_ID", "SIZE"]), col(["NO_OF_BELTS", "BELTS", "QUANTITY", "QTY"])
    if not all([dc, sc, qc]):
        raise ValueError(f"MTO file must contain date, SKU and belt quantity columns. Found: {list(df.columns)}")
    out = df.rename(columns={dc: "DATE", sc: "ITEM_SIZE", qc: "BELTS"}).copy()
    out["DATE"] = pd.to_datetime(out["DATE"], errors="coerce", dayfirst=True)
    out["ITEM_SIZE"] = out["ITEM_SIZE"].astype("string").str.strip().str.upper()
    out["BELTS"] = pd.to_numeric(out["BELTS"], errors="coerce")
    return out.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])[lambda x: x["BELTS"] > 0].copy()


def allocation_plot(slots, allocation, title):
    fig = go.Figure()
    if slots.empty:
        return fig
    fig.add_trace(go.Scatter(x=slots.X_M, y=slots.Y_M, mode="markers", marker=dict(size=7, symbol="square-open"), name="Available locations"))
    a = allocation[allocation["SLOT_ID"] != "UNALLOCATED"].copy()
    if not a.empty:
        a["LABEL"] = a["ITEM_SIZE"].astype(str) + " | " + a["SLOT_ID"].astype(str)
        fig.add_trace(go.Scatter(x=a.X_M, y=a.Y_M, mode="markers", marker=dict(size=11, symbol="square"), text=a.LABEL, customdata=np.c_[a.ITEM_SIZE, a.SLOT_ID, a.RANK, a.ALLOCATED_STORAGE_EQ], hovertemplate="SKU %{customdata[0]}<br>Location %{customdata[1]}<br>Rank %{customdata[2]}<br>Allocated eq %{customdata[3]:.2f}<extra></extra>", name="SKU allocations"))
    fig.update_layout(height=650, title=title, xaxis_title="X (m)", yaxis_title="Y (m)", margin=dict(l=20,r=20,t=50,b=20))
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


# Try to reuse objects from the main app.
slots = _find_session_df({"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"})
mto = _find_session_df({"DATE", "ITEM_SIZE", "BELTS"})

st.info("The optimizer keeps the Process-4 physical locations fixed. It changes only how the SKU universe is grouped and assigned to those locations.")

with st.sidebar:
    st.header("Optimizer controls")
    capacity_units = st.number_input("Capacity per physical location (pallet-equivalent)", min_value=0.10, max_value=10.0, value=1.0, step=0.10)
    safety_buffer = st.slider("Storage planning buffer (%)", 0, 50, 15, 5)
    freq_weight = st.slider("Combined strategy — frequency weight (%)", 0, 100, 50, 5)
    st.caption("Capacity and buffer are modelling controls. Validate them physically before implementation.")

if slots is None or mto is None:
    st.warning("Process-4 slots or MTO detail were not exposed by the current page session. Upload them below to run the optimizer independently.")
    c1, c2 = st.columns(2)
    with c1:
        slot_file = st.file_uploader("Process-4 Slot Master", type=["csv", "xlsx"], key="optimizer_slots")
    with c2:
        mto_file = st.file_uploader("MTO detail / consolidated master", type=["csv", "xlsx"], key="optimizer_mto")
    if slot_file is not None:
        slots = _read_upload(slot_file)
    if mto_file is not None:
        mto = _normalise_mto(_read_upload(mto_file))

if slots is None or mto is None:
    st.stop()

# Accept common Process-4 naming variants.
slot_alias = {str(c).strip().upper(): c for c in slots.columns}
for target, options in {
    "SLOT_ID": ["SLOT_ID", "SLOT", "LOCATION_ID"],
    "X_M": ["X_M", "X", "CENTER_X"],
    "Y_M": ["Y_M", "Y", "CENTER_Y"],
    "DISTANCE_FROM_DOOR_M": ["DISTANCE_FROM_DOOR_M", "DISTANCE_M", "DISTANCE"],
}.items():
    if target not in slots.columns:
        for option in options:
            if option in slot_alias:
                slots = slots.rename(columns={slot_alias[option]: target})
                break

missing = {"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"} - set(slots.columns)
if missing:
    st.error(f"Process-4 slot data is missing: {sorted(missing)}")
    st.stop()

try:
    sku = prepare_sku_demand(mto, capacity_units=capacity_units, safety_buffer=safety_buffer / 100)
except Exception as exc:
    st.error(f"Could not build SKU movement master: {exc}")
    st.stop()

# Use the sidebar weighting for the combined strategy, while retaining the existing
# 50:50 default unless the user intentionally changes it.
sku["COMBINED_SCORE"] = (freq_weight / 100) * sku["FREQUENCY_SCORE"] + (1 - freq_weight / 100) * sku["VOLUME_SCORE"]

st.subheader("1. V-belt SKU movement universe")
cards = st.columns(5)
cards[0].metric("SKUs in MTO", f"{len(sku):,}")
cards[1].metric("MTO lines", f"{len(mto):,}")
cards[2].metric("MTO belts", f"{mto['BELTS'].sum():,.0f}")
cards[3].metric("Physical locations", f"{len(slots):,}")
cards[4].metric("Storage buffer", f"{safety_buffer}%")

with st.expander("SKU movement and storage master", expanded=False):
    st.dataframe(sku, use_container_width=True, height=420)

st.subheader("2. Compare four allocation strategies")
results = {}
progress = st.progress(0)
for i, strategy in enumerate(STRATEGIES):
    ranked, allocation, location, summary = evaluate_strategy(mto, slots, sku, strategy, capacity_units=capacity_units)
    results[strategy] = {"ranked": ranked, "allocation": allocation, "location": location, "summary": summary}
    progress.progress((i + 1) / len(STRATEGIES))
progress.empty()

comparison = compare_layouts(results)
st.dataframe(comparison, use_container_width=True, hide_index=True)

best = comparison.iloc[0]
st.success(f"Current model-defined winner: **{best['Strategy']}** (balanced layout score {best['Balanced_Layout_Score']:.2f}). This is a comparative simulation result, not an observed warehouse KPI.")

fig = px.bar(comparison, x="Strategy", y="Balanced_Layout_Score", title="Balanced layout score by strategy")
st.plotly_chart(fig, use_container_width=True)

c1, c2 = st.columns(2)
with c1:
    fig = px.bar(comparison, x="Strategy", y="Line coverage %", title="Historical MTO line coverage")
    st.plotly_chart(fig, use_container_width=True)
with c2:
    fig = px.bar(comparison, x="Strategy", y="Belt coverage %", title="Historical belt-volume coverage")
    st.plotly_chart(fig, use_container_width=True)

fig = px.bar(comparison, x="Strategy", y="Total one-way travel m", title="Comparative one-way movement distance")
st.plotly_chart(fig, use_container_width=True)

st.subheader("3. Inspect each proposed layout")
strategy = st.selectbox("Layout scenario", STRATEGIES, index=STRATEGIES.index("Movement + Space"))
r = results[strategy]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Used locations", int(r["location"]["USED_STORAGE_EQ"].gt(0).sum()))
m2.metric("Shared locations", int(r["location"]["ASSIGNED_SKUS"].gt(1).sum()))
m3.metric("Line coverage", f"{r['summary']['Line coverage %']:.2f}%")
m4.metric("Belt coverage", f"{r['summary']['Belt coverage %']:.2f}%")

st.plotly_chart(allocation_plot(slots, r["allocation"], f"{strategy} — multi-SKU physical allocation"), use_container_width=True)

left, right = st.columns(2)
with left:
    st.markdown("**Location utilization**")
    st.dataframe(r["location"].sort_values("CAPACITY_UTILIZATION_PCT", ascending=False), use_container_width=True, height=360)
with right:
    st.markdown("**SKU-to-location allocation**")
    st.dataframe(r["allocation"].sort_values(["RANK", "DISTANCE_FROM_DOOR_M"]), use_container_width=True, height=360)

st.subheader("4. Historical daily MTO replay")
flow, daily = __import__("core.layout_optimizer", fromlist=["replay_flow"]).replay_flow(mto, r["allocation"])
fig = px.line(daily, x="DATE", y="TOTAL_ONE_WAY_TRAVEL_M", title=f"Daily comparative travel — {strategy}")
st.plotly_chart(fig, use_container_width=True)
st.dataframe(daily, use_container_width=True, height=360)

st.subheader("5. Exportable model outputs")
for name, df in {
    "SKU_Movement_Storage_Master.csv": sku,
    "Strategy_Comparison.csv": comparison,
    "Selected_Strategy_Ranking.csv": r["ranked"],
    "Selected_Strategy_Allocation.csv": r["allocation"],
    "Selected_Strategy_Location_Utilization.csv": r["location"],
    "Selected_Strategy_Daily_Replay.csv": daily,
}.items():
    st.download_button(f"Download {name}", df.to_csv(index=False).encode("utf-8"), file_name=name, mime="text/csv", key=f"dl_{name}")

st.caption("Model boundary: storage requirement is a planning proxy derived from historical peak daily boxes; multiple SKUs may share a location; actual physical compatibility, replenishment, route constraints and congestion require warehouse validation.")
