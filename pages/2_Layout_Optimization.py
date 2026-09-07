"""Model 2B — Process-4-connected multi-SKU layout optimization.

This page intentionally reads the exact Process-4 selection from the main app's
Streamlit session state. It never substitutes the full Model-1 slot master.
"""
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from shapely.geometry import Point

from core.layout_optimizer import STRATEGIES, prepare_sku_demand, evaluate_strategy, compare_layouts, replay_flow

st.set_page_config(page_title="Layout Optimization — JK Fenner", page_icon="📐", layout="wide")
st.title("📐 Model 2B — Multi-SKU Layout Optimization")
st.caption("Process 4 selected physical opportunity → shared SKU locations → movement-aware allocation → historical MTO replay")


def get_process4_inputs():
    """Return ONLY the slots and MTO detail generated/selected by Process 4."""
    m1 = st.session_state.get("m1_result")
    region = st.session_state.get("area_region")
    area_range = st.session_state.get("area_range")

    if not isinstance(m1, dict) or "slot_df" not in m1 or "demand_detail" not in m1:
        return None, None, area_range
    if region is None or area_range is None:
        return None, None, area_range

    slot_df = m1["slot_df"].copy()
    required_slots = {"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"}
    if not required_slots.issubset(slot_df.columns):
        return None, None, area_range

    # This is the same geographic test used by Process 4 in app.py.
    mask = slot_df.apply(
        lambda q: region.covers(Point(float(q.X_M), float(q.Y_M))), axis=1
    )
    process4_slots = (
        slot_df[mask]
        .copy()
        .sort_values("DISTANCE_FROM_DOOR_M")
        .reset_index(drop=True)
    )

    mto = m1["demand_detail"].copy()
    required_mto = {"DATE", "ITEM_SIZE", "BELTS"}
    if not required_mto.issubset(mto.columns):
        return None, None, area_range

    return process4_slots, mto, area_range


slots, mto, area_range = get_process4_inputs()

# -----------------------------------------------------------------------------
# Explicit lineage check. The page refuses to calculate without Process 4.
# -----------------------------------------------------------------------------
st.subheader("🔗 Data lineage check")

if slots is None or mto is None:
    st.error(
        "🔴 PROCESS 4 DATA NOT CONNECTED — Complete Model 1 and Process 4 in the main app first. "
        "This page will not calculate using the full Model-1 slot master or an independently invented slot count."
    )
    st.stop()

model1_slots = st.session_state["m1_result"]["slot_df"]
x1, x2, x3, x4 = st.columns(4)
x1.metric("Process-4 locations", f"{len(slots):,}")
x2.metric("Model-1 feasible slots", f"{len(model1_slots):,}")
x3.metric("MTO transaction lines", f"{len(mto):,}")
x4.metric("MTO belts", f"{mto['BELTS'].sum():,.0f}")

st.success(
    "🟢 LIVE PROCESS-4 DATA CONNECTED — Layout Optimization is using the physical locations inside the exact study-area selection from Process 4."
)
st.caption(
    f"Process-4 selection: X {area_range[0]:.2f}–{area_range[1]:.2f} m · "
    f"Y {area_range[2]:.2f}–{area_range[3]:.2f} m · "
    f"{len(slots):,} physical locations passed to Model 2B."
)
st.info(
    "The physical-location count is dynamic. If Process 4 produces 93 locations, Model 2B receives 93; "
    "if you change the Process-4 rectangle, Model 2B automatically uses the new count."
)

# Model controls affect storage allocation, not the Process-4 physical opportunity.
with st.sidebar:
    st.header("Model 2B controls")
    capacity_units = st.number_input(
        "Planning capacity per physical location (pallet-equivalent)",
        min_value=0.10,
        max_value=10.0,
        value=1.0,
        step=0.10,
    )
    safety_buffer = st.slider("Storage planning buffer (%)", 0, 50, 15, 5)
    st.caption(
        "Capacity and buffer are modelling parameters. They do NOT change the Process-4 physical opportunity. "
        "Validate actual location capacity before implementation."
    )

try:
    sku = prepare_sku_demand(
        mto,
        capacity_units=capacity_units,
        safety_buffer=safety_buffer / 100,
    )
except Exception as exc:
    st.error(f"Could not build the SKU movement master: {exc}")
    st.stop()

st.subheader("1. Historical V-belt movement universe")
cards = st.columns(5)
cards[0].metric("SKUs in MTO", f"{len(sku):,}")
cards[1].metric("MTO lines", f"{len(mto):,}")
cards[2].metric("MTO belts", f"{mto['BELTS'].sum():,.0f}")
cards[3].metric("Process-4 locations", f"{len(slots):,}")
cards[4].metric("Planning buffer", f"{safety_buffer}%")

with st.expander("SKU movement + storage requirement master", expanded=False):
    st.dataframe(sku, use_container_width=True, height=430)

st.subheader("2. Compare four allocation strategies")
st.write(
    "The same Process-4 physical opportunity is held constant. The strategies differ only in how the historical moving-SKU universe is prioritised and packed into those physical locations."
)

results = {}
progress = st.progress(0)
for i, strategy in enumerate(STRATEGIES):
    ranked, allocation, location, summary = evaluate_strategy(
        mto, slots, sku, strategy, capacity_units=capacity_units
    )
    results[strategy] = {
        "ranked": ranked,
        "allocation": allocation,
        "location": location,
        "summary": summary,
    }
    progress.progress((i + 1) / len(STRATEGIES))
progress.empty()

comparison = compare_layouts(results)

# Transparent management decision score. This is a model-defined decision aid,
# not an observed warehouse KPI.
def minmax(s, higher=True):
    s = pd.to_numeric(s, errors="coerce").fillna(0.0)
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo:
        return pd.Series(1.0, index=s.index)
    z = (s - lo) / (hi - lo)
    return z if higher else 1.0 - z

comparison["Frequency objective"] = minmax(comparison["Line coverage %"], True)
comparison["Volume objective"] = minmax(comparison["Belt coverage %"], True)
comparison["Travel objective"] = minmax(comparison["Total one-way travel m"], False)
comparison["Space objective"] = minmax(comparison["Average location utilization %"], True)
comparison["Decision Score"] = (
    0.25 * comparison["Frequency objective"]
    + 0.30 * comparison["Volume objective"]
    + 0.30 * comparison["Travel objective"]
    + 0.15 * comparison["Space objective"]
) * 100
comparison["Decision Rank"] = comparison["Decision Score"].rank(method="min", ascending=False).astype(int)
comparison = comparison.sort_values(["Decision Rank", "Strategy"]).reset_index(drop=True)

st.dataframe(comparison.round(3), use_container_width=True, hide_index=True)
winner = comparison.iloc[0]
st.success(
    f"Recommended under the default management objective: **{winner['Strategy']}** "
    f"(Decision Score {winner['Decision Score']:.1f}/100)."
)
st.caption(
    "Decision Score = 25% frequency/line coverage + 30% belt-volume coverage + 30% travel efficiency + 15% space utilisation. "
    "The score is model-defined and is not an observed warehouse KPI."
)

c1, c2 = st.columns(2)
with c1:
    fig = px.bar(comparison, x="Strategy", y="Line coverage %", title="Historical MTO line coverage")
    st.plotly_chart(fig, use_container_width=True)
with c2:
    fig = px.bar(comparison, x="Strategy", y="Belt coverage %", title="Historical MTO belt-volume coverage")
    st.plotly_chart(fig, use_container_width=True)

c1, c2 = st.columns(2)
with c1:
    fig = px.bar(comparison, x="Strategy", y="Total one-way travel m", title="Comparative one-way movement distance")
    st.plotly_chart(fig, use_container_width=True)
with c2:
    fig = px.bar(comparison, x="Strategy", y="Average location utilization %", title="Average physical-location utilisation")
    st.plotly_chart(fig, use_container_width=True)

st.subheader("3. Inspect the Process-4 layout + selected strategy")
strategy = st.selectbox("Layout scenario", STRATEGIES, index=STRATEGIES.index("Movement + Space"))
r = results[strategy]
allocation = r["allocation"]

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=slots["X_M"], y=slots["Y_M"], mode="markers",
        marker=dict(size=7, symbol="square-open"),
        name="Process-4 physical locations",
    )
)
assigned = allocation[allocation["SLOT_ID"] != "UNALLOCATED"].copy()
if not assigned.empty:
    assigned["LABEL"] = assigned["ITEM_SIZE"].astype(str) + " | " + assigned["SLOT_ID"].astype(str)
    fig.add_trace(
        go.Scatter(
            x=assigned["X_M"], y=assigned["Y_M"], mode="markers",
            marker=dict(size=10, symbol="square"), text=assigned["LABEL"],
            customdata=np.c_[assigned["ITEM_SIZE"], assigned["SLOT_ID"], assigned["ALLOCATED_STORAGE_EQ"]],
            hovertemplate="SKU %{customdata[0]}<br>Location %{customdata[1]}<br>Allocated storage eq %{customdata[2]:.2f}<extra></extra>",
            name="SKU allocation",
        )
    )
fig.update_layout(
    height=650,
    title=f"{strategy} — using exactly the Process-4 selected locations",
    xaxis_title="X (m)", yaxis_title="Y (m)", margin=dict(l=20, r=20, t=50, b=20),
)
fig.update_yaxes(scaleanchor="x", scaleratio=1)
st.plotly_chart(fig, use_container_width=True)

m1c, m2c, m3c, m4c, m5c = st.columns(5)
m1c.metric("Process-4 locations", len(slots))
m2c.metric("Used locations", int(r["location"]["USED_STORAGE_EQ"].gt(0).sum()))
m3c.metric("Shared locations", int(r["location"]["ASSIGNED_SKUS"].gt(1).sum()))
m4c.metric("Allocated SKUs", int(allocation["ITEM_SIZE"].nunique()) if not allocation.empty else 0)
m5c.metric("Unallocated storage rows", int(r["summary"]["Unallocated storage rows"]))

st.subheader("4. Historical MTO replay")
replayed, daily = replay_flow(mto, allocation)
st.dataframe(daily.sort_values("DATE").round(3), use_container_width=True, height=360)

st.subheader("5. Allocation table")
show_cols = [
    "ITEM_SIZE", "SLOT_ID", "DISTANCE_FROM_DOOR_M", "ALLOCATED_STORAGE_EQ",
    "SKU_REQUIRED_STORAGE_EQ", "RANK", "MTO_LINES", "MTO_BELTS", "ACTIVE_DAYS",
    "MOVEMENT_SEGMENT",
]
show_cols = [c for c in show_cols if c in allocation.columns]
st.dataframe(
    allocation[show_cols].sort_values(["RANK", "SLOT_ID"], na_position="last").round(3),
    use_container_width=True,
    height=480,
)

st.caption(
    "Process 4 determines which physical locations are feasible inside the selected study area. "
    "Model 2B then allocates the historical moving SKU universe within that fixed physical opportunity. "
    "Multiple SKUs may share a location subject to the modelled capacity."
)
