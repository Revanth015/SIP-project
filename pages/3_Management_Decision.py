"""Model 2C — management decision engine for the warehouse Digital Twin.

The page is intentionally connected to the main app's Process-4 state. The
current physical baseline is stated directly from the validated project baseline
(66 operational Poly-V pallet positions); no fake SKU-location mapping is created.
"""
from io import BytesIO

import pandas as pd
import plotly.express as px
import streamlit as st

from core.layout_optimizer import STRATEGIES, prepare_sku_demand, evaluate_strategy, compare_layouts
from core.export_utils import workbook_bytes

st.set_page_config(page_title="Management Decision — JK Fenner", page_icon="🎯", layout="wide")
st.title("🎯 Model 2C — Management Decision Engine")
st.caption("Current state → Model-1 physical layout → Process-4 opportunity → SKU allocation optimization → management decision")

CURRENT_OPERATIONAL_CAPACITY = 66
CURRENT_STATE_SOURCE = "Validated project baseline — existing Poly-V operational capacity"


def get_process4_inputs():
    """Read the exact Process-4 selection from the main app; never fall back to full Model-1 slots."""
    m1 = st.session_state.get("m1_result")
    region = st.session_state.get("area_region")
    area_range = st.session_state.get("area_range")
    if not isinstance(m1, dict) or "slot_df" not in m1 or "demand_detail" not in m1:
        return None, None, area_range
    if region is None or area_range is None:
        return None, None, area_range
    slots = m1["slot_df"].copy()
    required = {"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"}
    if not required.issubset(slots.columns):
        return None, None, area_range
    from shapely.geometry import Point
    mask = slots.apply(lambda q: region.covers(Point(float(q.X_M), float(q.Y_M))), axis=1)
    slots = slots[mask].sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)
    mto = m1["demand_detail"].copy()
    if not {"DATE", "ITEM_SIZE", "BELTS"}.issubset(mto.columns):
        return None, None, area_range
    return slots, mto, area_range


def mm(series, higher=True):
    s = pd.to_numeric(series, errors="coerce").fillna(0.0)
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo:
        z = pd.Series(1.0, index=s.index)
    else:
        z = (s - lo) / (hi - lo)
    return z if higher else 1 - z


slots, mto, area_range = get_process4_inputs()

st.subheader("🔗 Data lineage")
if slots is None or mto is None:
    st.error("🔴 Process 4 is not complete. Return to the main app, run Model 1, select the exact study area in Process 4, and then open Management Decision.")
    st.stop()

m1 = st.session_state["m1_result"]
model1_slots = m1["slot_df"]

# Explicit current physical state — not a fabricated SKU-location mapping.
st.subheader("1. Current physical state")
c = st.columns(5)
c[0].metric("Current operational capacity", f"{CURRENT_OPERATIONAL_CAPACITY:,} pallet slots")
c[1].metric("Model-1 feasible slots", f"{len(model1_slots):,}")
c[2].metric("Process-4 selected locations", f"{len(slots):,}")
peak = float(m1.get("demand_metrics", {}).get("peak_demand", 0))
operating_days = int(m1.get("demand_metrics", {}).get("operating_days", 0))
overflow_days = int(m1.get("demand_metrics", {}).get("physical_overflow_days", 0))
c[3].metric("Historical peak demand", f"{peak:.0f} pallets")
c[4].metric("Current-state overflow days", f"{overflow_days:,}")
st.caption(f"Current physical baseline: {CURRENT_STATE_SOURCE}. The current 66-slot figure is a physical operational baseline; it is not treated as an SKU-location map.")

if area_range:
    st.info(f"🟢 **LIVE PROCESS-4 DATA CONNECTED** — X {area_range[0]:.2f}–{area_range[1]:.2f} m · Y {area_range[2]:.2f}–{area_range[3]:.2f} m · **{len(slots):,} physical locations**")

# Show the actual Model-1 optimization decision when available.
st.subheader("2. Physical layout optimization used before SKU slotting")
winner = m1.get("winner", {})
layout_rows = []
for key, label in [
    ("orientation", "Pallet orientation (deg)"),
    ("wall_clearance_m", "Wall clearance (m)"),
    ("main_aisle_m", "Main aisle (m)"),
    ("cross_aisle_m", "Cross aisle (m)"),
    ("capacity", "Generated feasible slots"),
    ("avg_distance_m", "Average slot distance (m)"),
    ("score", "Layout score"),
]:
    if key in winner:
        layout_rows.append({"Metric": label, "Value": winner[key]})
if layout_rows:
    st.dataframe(pd.DataFrame(layout_rows), use_container_width=True, hide_index=True)
st.caption("Model 1 generates feasible pallet positions after wall clearance, aisles, obstacles and turning-zone constraints, evaluates pallet orientations, and selects the best capacity/distance trade-off. Process 4 then restricts Model 2 to the selected physical subset.")

with st.sidebar:
    st.header("Decision controls")
    capacity = st.number_input("Capacity per physical location (pallet-equivalent)", 0.10, 10.0, 1.0, 0.10)
    buffer = st.slider("Storage planning buffer (%)", 0, 50, 15, 5)
    st.divider()
    st.subheader("Management objective")
    wf = st.slider("Frequency / service (%)", 0, 100, 25, 5)
    wv = st.slider("Volume / belt coverage (%)", 0, 100, 25, 5)
    wt = st.slider("Travel efficiency (%)", 0, 100, 35, 5)
    ws = st.slider("Space utilization (%)", 0, 100, 15, 5)
    total_w = wf + wv + wt + ws
    if total_w == 0:
        st.error("At least one decision weight must be positive.")
        st.stop()

sku = prepare_sku_demand(mto, capacity_units=capacity, safety_buffer=buffer / 100)
sku["COMBINED_SCORE"] = 0.5 * sku["FREQUENCY_SCORE"] + 0.5 * sku["VOLUME_SCORE"]

st.subheader("3. SKU movement universe")
c = st.columns(4)
c[0].metric("SKUs with historical movement", f"{len(sku):,}")
c[1].metric("MTO transaction lines", f"{len(mto):,}")
c[2].metric("MTO belts", f"{mto['BELTS'].sum():,.0f}")
c[3].metric("Process-4 locations", f"{len(slots):,}")

st.subheader("4. Optimize SKU allocation within the fixed Process-4 physical opportunity")
results = {}
for strategy in STRATEGIES:
    ranked, allocation, location, summary = evaluate_strategy(mto, slots, sku, strategy, capacity_units=capacity)
    results[strategy] = {"ranked": ranked, "allocation": allocation, "location": location, "summary": summary}
comparison = compare_layouts(results)

w = {"frequency": wf / total_w, "volume": wv / total_w, "travel": wt / total_w, "space": ws / total_w}
decision = comparison.copy()
decision["Frequency objective"] = mm(decision["Line coverage %"])
decision["Volume objective"] = mm(decision["Belt coverage %"])
decision["Travel objective"] = mm(decision["Total one-way travel m"], False)
decision["Space objective"] = mm(decision["Average location utilization %"])
decision["Decision Score"] = (w["frequency"]*decision["Frequency objective"] + w["volume"]*decision["Volume objective"] + w["travel"]*decision["Travel objective"] + w["space"]*decision["Space objective"]) * 100
decision["Decision Rank"] = decision["Decision Score"].rank(method="min", ascending=False).astype(int)
decision = decision.sort_values(["Decision Rank", "Strategy"]).reset_index(drop=True)
best = decision.iloc[0]
recommended = results[best["Strategy"]]

st.success(f"Recommended allocation strategy: **{best['Strategy']}** — decision score **{best['Decision Score']:.2f}/100**.")
st.dataframe(decision[["Strategy", "Line coverage %", "Belt coverage %", "Total one-way travel m", "Average location utilization %", "Shared locations", "Unallocated storage rows", "Decision Score", "Decision Rank"]], use_container_width=True, hide_index=True)

st.subheader("5. Recommended optimized layout")
rc = st.columns(6)
rc[0].metric("Strategy", best["Strategy"])
rc[1].metric("Line coverage", f"{best['Line coverage %']:.2f}%")
rc[2].metric("Belt coverage", f"{best['Belt coverage %']:.2f}%")
rc[3].metric("Total travel", f"{best['Total one-way travel m']:,.0f} m")
rc[4].metric("Shared locations", int(best["Shared locations"]))
rc[5].metric("Used locations", int(recommended["location"]["USED_STORAGE_EQ"].gt(0).sum()))

st.markdown("**Recommended SKU-location allocation — multiple SKUs may share one physical location subject to model capacity.**")
st.dataframe(recommended["allocation"].sort_values(["RANK", "DISTANCE_FROM_DOOR_M"]), use_container_width=True, height=450)

st.subheader("6. Location utilization")
st.dataframe(recommended["location"].sort_values("DISTANCE_FROM_DOOR_M"), use_container_width=True, height=350)

st.subheader("7. Management action list")
a = recommended["allocation"].copy()
a = a[a["SLOT_ID"] != "UNALLOCATED"].copy()
a["ACTION"] = a["MOVEMENT_SEGMENT"].map({
    "High Frequency / High Volume": "Prioritize near-door accessibility",
    "High Frequency / Standard Volume": "Prioritize near-door accessibility",
    "Standard Frequency / High Volume": "Prioritize adequate storage capacity",
    "Standard Frequency / Standard Volume": "Use shared-location capacity where compatible",
}).fillna("Validate physical handling requirement")
a["RATIONALE"] = a["MOVEMENT_SEGMENT"] + "; movement rank " + a["RANK"].astype(str)
actions = a[["ITEM_SIZE", "SLOT_ID", "ALLOCATED_STORAGE_EQ", "DISTANCE_FROM_DOOR_M", "MOVEMENT_SEGMENT", "ACTION", "RATIONALE"]]
st.dataframe(actions, use_container_width=True, height=400)

st.subheader("8. Sensitivity analysis")
sensitivity = []
scenarios = [("Balanced",25,25,25,25),("Movement priority",15,20,50,15),("Service priority",50,20,20,10),("Volume priority",20,50,20,10),("Space priority",15,20,15,50)]
for name, af, av, at, ass in scenarios:
    scores = comparison.copy()
    scores["score"] = (af/100)*mm(scores["Line coverage %"])+(av/100)*mm(scores["Belt coverage %"])+(at/100)*mm(scores["Total one-way travel m"],False)+(ass/100)*mm(scores["Average location utilization %"])
    scores["rank"] = scores["score"].rank(method="min", ascending=False).astype(int)
    winner_s = scores.sort_values("rank").iloc[0]
    sensitivity.append({"Scenario":name,"Winner":winner_s["Strategy"],"Score":winner_s["score"]*100})
sensitivity_df = pd.DataFrame(sensitivity)
st.dataframe(sensitivity_df, use_container_width=True, hide_index=True)

st.subheader("9. Current state vs optimized state")
# Physical baseline is directly known. SKU movement baseline is deliberately not invented.
current_demand = m1.get("demand_metrics", {})
current_rows = [
    {"Metric":"Operational pallet capacity", "Current state":CURRENT_OPERATIONAL_CAPACITY, "Optimized / proposed":len(model1_slots), "Unit":"pallet slots", "Basis":"Validated current operational baseline vs Model-1 feasible layout"},
    {"Metric":"Process-4 physical opportunity", "Current state":CURRENT_OPERATIONAL_CAPACITY, "Optimized / proposed":len(slots), "Unit":"pallet locations", "Basis":"Current baseline vs selected study-area opportunity"},
    {"Metric":"Peak historical demand", "Current state":current_demand.get("peak_demand", peak), "Optimized / proposed":current_demand.get("peak_demand", peak), "Unit":"pallets/day", "Basis":"Same historical demand used for both scenarios"},
    {"Metric":"Physical overflow days", "Current state":current_demand.get("physical_overflow_days", overflow_days), "Optimized / proposed":int((m1.get("derived_daily_demand", pd.DataFrame()).get("TOTAL_PALLETS", pd.Series(dtype=float)) > len(model1_slots)).sum()), "Unit":"days", "Basis":"Historical demand replay against capacity"},
]
current_vs_layout = pd.DataFrame(current_rows)
st.dataframe(current_vs_layout, use_container_width=True, hide_index=True)
st.warning("A true current-vs-optimized SKU travel comparison requires the actual current SKU-location master. The application does not invent one. The physical current state (66 operational pallet positions) is shown directly from the project baseline.")

fig = px.bar(decision, x="Strategy", y="Decision Score", title="Management decision score")
st.plotly_chart(fig, use_container_width=True)

st.subheader("10. One-file complete output")
exports = {
    "00_Current_State": current_vs_layout,
    "01_Process4_Slots": slots,
    "02_Model1_Winner": pd.DataFrame(layout_rows),
    "03_SKU_Movement_Master": sku,
    "04_Strategy_Comparison": comparison,
    "05_Decision_Score": decision,
    "06_Recommended_Allocation": recommended["allocation"],
    "07_Location_Utilization": recommended["location"],
    "08_Management_Actions": actions,
    "09_Sensitivity": sensitivity_df,
    "10_Recommended_Daily_Flow": recommended.get("daily", pd.DataFrame()),
}
for strategy_name, data in results.items():
    exports[f"{strategy_name[:20]}_Ranked"] = data["ranked"]
    exports[f"{strategy_name[:20]}_Allocation"] = data["allocation"]
    exports[f"{strategy_name[:20]}_Location"] = data["location"]

xlsx = workbook_bytes(exports)
st.download_button("📥 Download ALL outputs as one Excel workbook", data=xlsx, file_name="JK_Fenner_Digital_Twin_Complete_Output.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary", use_container_width=True)
st.caption("The workbook contains the physical baseline, Process-4 slots, Model-1 layout decision, SKU movement master, every strategy, recommended allocation, location utilization, action list and sensitivity analysis.")

st.caption("Model boundary: the SKU allocation is a scenario-based heuristic optimization over the fixed Process-4 physical opportunity. Storage capacity, SKU compatibility, replenishment, congestion and actual current SKU locations require physical validation before implementation.")
