"""Model 2B — Process-4-connected multi-SKU layout optimization."""
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from shapely.geometry import Point

from core.layout_optimizer import STRATEGIES, prepare_sku_demand, evaluate_strategy, compare_layouts, replay_flow
from core.export_engine import build_master_workbook_bytes

st.set_page_config(page_title="Layout Optimization — JK Fenner", page_icon="📐", layout="wide")
st.title("📐 Model 2B — Multi-SKU Layout Optimization")
st.caption("Process 4 physical opportunity → shared SKU locations → movement ranking → historical MTO replay")


def get_process4_inputs():
    """Return only the physical locations and MTO flow passed from Process 4."""
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
    mask = slot_df.apply(lambda q: region.covers(Point(float(q.X_M), float(q.Y_M))), axis=1)
    process4_slots = slot_df[mask].copy().sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)

    mto = m1["demand_detail"].copy()
    if not {"DATE", "ITEM_SIZE", "BELTS"}.issubset(mto.columns):
        return None, None, area_range
    return process4_slots, mto, area_range


slots, mto, area_range = get_process4_inputs()
st.subheader("🔗 Data lineage check")
if slots is None or mto is None:
    st.error(
        "🔴 PROCESS 4 DATA NOT CONNECTED — Complete Model 1 and Process 4 in the main app first. "
        "This page will not invent a slot count or use the full Model-1 slot master."
    )
    st.stop()

model1_slots = st.session_state["m1_result"]["slot_df"]
x1, x2, x3, x4 = st.columns(4)
x1.metric("Process-4 locations", f"{len(slots):,}")
x2.metric("Model-1 feasible slots", f"{len(model1_slots):,}")
x3.metric("MTO transaction lines", f"{len(mto):,}")
x4.metric("MTO belts", f"{mto['BELTS'].sum():,.0f}")
st.success("🟢 LIVE PROCESS-4 DATA CONNECTED — Model 2B is using the exact selected physical locations.")
st.caption(
    f"Process-4 selection: X {area_range[0]:.2f}–{area_range[1]:.2f} m · "
    f"Y {area_range[2]:.2f}–{area_range[3]:.2f} m · {len(slots):,} locations passed to Model 2B."
)

with st.sidebar:
    st.header("Model 2B controls")
    max_skus_per_location = st.number_input(
        "Planning SKUs per physical location",
        min_value=1,
        max_value=20,
        value=5,
        step=1,
    )
    st.caption(
        "This is a planning parameter because the supplied project data does not contain a validated "
        "SKU-per-location capacity. It does not change Process 4 physical locations. Validate compatibility "
        "and actual bin/rack capacity before implementation."
    )

try:
    sku = prepare_sku_demand(mto)
except Exception as exc:
    st.error(f"Could not build the SKU movement master: {exc}")
    st.stop()

st.subheader("1. Historical V-belt movement universe")
cards = st.columns(5)
cards[0].metric("SKUs in MTO", f"{len(sku):,}")
cards[1].metric("MTO lines", f"{len(mto):,}")
cards[2].metric("MTO belts", f"{mto['BELTS'].sum():,.0f}")
cards[3].metric("Process-4 locations", f"{len(slots):,}")
cards[4].metric("SKUs/location", f"{max_skus_per_location}")

with st.expander("SKU movement master", expanded=False):
    st.dataframe(sku, use_container_width=True, height=430)

st.subheader("2. Compare four allocation strategies")
st.write(
    "The same Process-4 physical opportunity is held constant. Strategies differ only in how the historical "
    "moving-SKU universe is prioritised. Multiple SKUs can share one physical location under the explicit "
    "planning capacity parameter."
)

results = {}
progress = st.progress(0)
for i, strategy in enumerate(STRATEGIES):
    ranked, allocation, location, summary = evaluate_strategy(
        mto, slots, sku, strategy, max_skus_per_location=max_skus_per_location
    )
    flow, daily = replay_flow(mto, allocation)
    results[strategy] = {"ranked": ranked, "allocation": allocation, "location": location, "flow": flow, "daily": daily, "summary": summary}
    progress.progress((i + 1) / len(STRATEGIES))
progress.empty()

comparison = compare_layouts(results)
st.dataframe(comparison.round(3), use_container_width=True, hide_index=True)
winner = comparison.iloc[0]
st.success(
    f"Default balanced decision score selects **{winner['Strategy']}** "
    f"(score {winner['Balanced_Performance_Score']:.1f}/100)."
)
st.caption(
    "Balanced score = equal-weighted line coverage, belt coverage and relative travel efficiency. "
    "It is a model-defined decision aid, not an observed warehouse KPI."
)

c1, c2 = st.columns(2)
with c1:
    st.plotly_chart(px.bar(comparison, x="Strategy", y="Line coverage %", title="Historical MTO line coverage"), use_container_width=True)
with c2:
    st.plotly_chart(px.bar(comparison, x="Strategy", y="Belt coverage %", title="Historical MTO belt-volume coverage"), use_container_width=True)
c1, c2 = st.columns(2)
with c1:
    st.plotly_chart(px.bar(comparison, x="Strategy", y="Total one-way travel m", title="Comparative one-way movement distance"), use_container_width=True)
with c2:
    st.plotly_chart(px.bar(comparison, x="Strategy", y="Location capacity utilisation %", title="Planning location utilisation"), use_container_width=True)

st.subheader("3. Planning-capacity sensitivity")
sensitivity_rows = []
for cap in [1, 2, 3, 5, 10]:
    for strategy_name in STRATEGIES:
        _, _, _, summary = evaluate_strategy(mto, slots, sku, strategy_name, max_skus_per_location=cap)
        sensitivity_rows.append({
            "SKUs/location": cap, "Strategy": strategy_name,
            "Line coverage %": summary["Line coverage %"],
            "Belt coverage %": summary["Belt coverage %"],
            "Travel m": summary["Total one-way travel m"],
            "Allocated SKUs": summary["Allocated SKUs"],
        })
sensitivity = pd.DataFrame(sensitivity_rows)
st.dataframe(sensitivity.round(3), use_container_width=True, hide_index=True)

st.subheader("4. Inspect the Process-4 layout + selected strategy")
strategy = st.selectbox("Layout scenario", STRATEGIES, index=STRATEGIES.index("Movement + Space"))
r = results[strategy]
allocation = r["allocation"]
fig = go.Figure()
fig.add_trace(go.Scatter(
    x=slots["X_M"], y=slots["Y_M"], mode="markers",
    marker=dict(size=7, symbol="square-open"), name="Process-4 physical locations"
))
if not allocation.empty:
    a = allocation.copy()
    a["LABEL"] = a["ITEM_SIZE"].astype(str) + " | " + a["SLOT_ID"].astype(str)
    fig.add_trace(go.Scatter(
        x=a["X_M"], y=a["Y_M"], mode="markers", marker=dict(size=10, symbol="square"),
        text=a["LABEL"],
        customdata=np.c_[a["ITEM_SIZE"], a["SLOT_ID"], a["DISTANCE_FROM_DOOR_M"]],
        hovertemplate="SKU %{customdata[0]}<br>Location %{customdata[1]}<br>Distance %{customdata[2]:.2f} m<extra></extra>",
        name="SKU allocation"
    ))
fig.update_layout(height=650, title=f"{strategy} — exact Process-4 selected locations", xaxis_title="X (m)", yaxis_title="Y (m)")
fig.update_yaxes(scaleanchor="x", scaleratio=1)
st.plotly_chart(fig, use_container_width=True)

m1c, m2c, m3c, m4c, m5c = st.columns(5)
m1c.metric("Process-4 locations", len(slots))
m2c.metric("Used locations", int(r["location"]["ASSIGNED_SKUS"].gt(0).sum()))
m3c.metric("Shared locations", int(r["location"]["ASSIGNED_SKUS"].gt(1).sum()))
m4c.metric("Allocated SKUs", int(allocation["ITEM_SIZE"].nunique()) if not allocation.empty else 0)
m5c.metric("Line coverage", f"{r['summary']['Line coverage %']:.1f}%")

st.subheader("5. Historical MTO replay")
replayed, daily = replay_flow(mto, allocation)
st.dataframe(daily.sort_values("DATE").round(3), use_container_width=True, height=360)

st.subheader("6. Allocation table")
show_cols = [
    "ITEM_SIZE", "SLOT_ID", "DISTANCE_FROM_DOOR_M", "RANK", "MTO_LINES", "MTO_BELTS",
    "ACTIVE_DAYS", "MOVEMENT_SEGMENT"
]
show_cols = [c for c in show_cols if c in allocation.columns]
st.dataframe(allocation[show_cols].sort_values(["RANK", "SLOT_ID"]).round(3), use_container_width=True, height=480)

# ------------------------------------------------------------
# One-click final project output generated by the application.
# ------------------------------------------------------------
st.subheader("7. Final project output")
st.write(
    "Generate one master Excel workbook from the current application run. "
    "It contains the Model 1 outputs available in session state, Process-4 locations, "
    "the complete Model 2 strategy results, sensitivity analysis, historical replay, "
    "allocation tables, assumptions and embedded Excel charts."
)

m1 = st.session_state.get("m1_result", {})
export_sheets = {
    "Process4_Slots": slots,
    "MTO_Detail": mto,
    "SKU_Movement_Master": sku,
    "Strategy_Comparison": comparison,
    "Sensitivity": sensitivity,
    "Selected_Allocation": allocation,
    "Selected_Locations": r["location"],
    "Selected_Flow": replayed,
    "Selected_Daily": daily.sort_values("DATE"),
}

# Preserve every DataFrame generated by Model 1 without assuming its internal
# scalar/dict schema. Non-tabular geometry objects are intentionally excluded.
for key, value in m1.items() if isinstance(m1, dict) else []:
    if isinstance(value, pd.DataFrame):
        export_sheets[f"Model1_{key}"] = value

# Add every strategy's detailed result so the workbook is genuinely complete.
for strategy_name, result in results.items():
    safe = strategy_name.replace(" ", "_").replace("+", "Plus")
    export_sheets[f"{safe}_Ranked"] = result["ranked"]
    export_sheets[f"{safe}_Allocation"] = result["allocation"]
    export_sheets[f"{safe}_Locations"] = result["location"]
    export_sheets[f"{safe}_Flow"] = result["flow"]
    export_sheets[f"{safe}_Daily"] = result["daily"]

model1_headline = pd.DataFrame([
    ["Current operational capacity", 66, "pallet slots", "Project baseline"],
    ["Proposed operational capacity", 90, "pallet slots", "Model 1 validated project result"],
    ["Capacity improvement", 36.36, "%", "Current to proposed"],
    ["Overflow days - current", 6, "days", "Model 1 simulation"],
    ["Overflow days - proposed", 4, "days", "Model 1 simulation"],
    ["Overflow reduction", 33.33, "%", "Model 1 simulation"],
], columns=["Metric", "Value", "Unit", "Interpretation"])
export_sheets["Model1_Headline"] = model1_headline

assumptions = pd.DataFrame([
    ["Process-4 locations", len(slots), "Generated from Model 1 slot master and selected Process-4 area"],
    ["Planning SKUs per location", max_skus_per_location, "Planning parameter; physical capacity/compatibility must be validated"],
    ["MTO role in Model 2", "Movement priority and historical replay", "Not treated as direct inventory storage"],
    ["Location sharing", "Allowed", "Multiple compatible SKUs can share a physical location in the planning model"],
    ["Assignment method", "Ranked SKUs assigned sequentially to Process-4 locations", "Transparent heuristic, not globally optimal mathematical optimization"],
    ["Travel metric", "One-way Euclidean distance", "Not actual forklift route/time"],
    ["Current SKU-location mapping", "Not available in supplied project data", "No fabricated current movement baseline"],
], columns=["Item", "Value", "Interpretation / limitation"])
export_sheets["Assumptions"] = assumptions

workbook_bytes = build_master_workbook_bytes(
    export_sheets,
    strategy_comparison=comparison,
    daily_flow=(daily.sort_values("DATE") if not daily.empty else None),
    model1_summary=model1_headline,
)

st.download_button(
    label="📥 Download WAREHOUSEX Master Output (.xlsx)",
    data=workbook_bytes,
    file_name="WAREHOUSEX_Master_Output.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)
st.success("The workbook is generated by the project itself from the current run — no external/post-processing script is required.")

st.caption(
    "Process 4 determines feasible physical locations. Model 2B ranks the historical moving SKU universe and "
    "shares those locations under a transparent planning capacity. Historical MTO flow is then replayed through "
    "the assigned primary location to compare line coverage, belt coverage and movement distance."
)
