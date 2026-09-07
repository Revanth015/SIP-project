"""Management Decision Engine — Model 2C."""
from pathlib import Path
import pandas as pd
import plotly.express as px
import streamlit as st

from core.layout_optimizer import prepare_sku_demand, evaluate_strategy, compare_layouts, STRATEGIES
from core.decision_engine import normalize_location_master, replay_current_layout, improvement

st.set_page_config(page_title="Management Decision — JK Fenner", page_icon="🎯", layout="wide")
st.title("🎯 Model 2C — Management Decision Engine")
st.caption("Current-state baseline → alternative layouts → recommended allocation → management action list")


def read_upload(upload):
    if Path(upload.name).suffix.lower() == ".csv":
        return pd.read_csv(upload)
    return pd.read_excel(upload)


def normalize_mto(df):
    lookup = {str(c).strip().upper(): c for c in df.columns}
    def pick(options):
        for n in options:
            if n.upper() in lookup:
                return lookup[n.upper()]
        return None
    dc = pick(["DATE", "DATETIME", "DAY"])
    sc = pick(["ITEM_SIZE", "BELT_SIZE", "SKU", "LINE_ITEM_SIZE_ID", "SIZE"])
    qc = pick(["NO_OF_BELTS", "BELTS", "QUANTITY", "QTY"])
    if not all([dc, sc, qc]):
        raise ValueError(f"MTO file must contain date, SKU and belt quantity columns. Found: {list(df.columns)}")
    out = df.rename(columns={dc: "DATE", sc: "ITEM_SIZE", qc: "BELTS"}).copy()
    out["DATE"] = pd.to_datetime(out["DATE"], errors="coerce", dayfirst=True)
    out["ITEM_SIZE"] = out["ITEM_SIZE"].astype("string").str.strip().str.upper()
    out["BELTS"] = pd.to_numeric(out["BELTS"], errors="coerce")
    return out.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])[lambda x: x["BELTS"] > 0].copy()


def session_df(required):
    for value in st.session_state.values():
        if isinstance(value, pd.DataFrame) and set(required).issubset(value.columns):
            return value.copy()
        if isinstance(value, dict):
            for v in value.values():
                if isinstance(v, pd.DataFrame) and set(required).issubset(v.columns):
                    return v.copy()
    return None

slots = session_df({"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"})
mto = session_df({"DATE", "ITEM_SIZE", "BELTS"})

st.subheader("1. Inputs")
with st.sidebar:
    st.header("Decision controls")
    capacity = st.number_input("Capacity per physical location (pallet-equivalent)", 0.10, 10.0, 1.0, 0.10)
    buffer = st.slider("Storage planning buffer (%)", 0, 50, 15, 5)
    wf = st.slider("Frequency / service (%)", 0, 100, 25, 5)
    wv = st.slider("Volume / belt coverage (%)", 0, 100, 25, 5)
    wt = st.slider("Travel efficiency (%)", 0, 100, 35, 5)
    ws = st.slider("Space utilization (%)", 0, 100, 15, 5)
    total_w = wf + wv + wt + ws
    if total_w == 0:
        st.error("At least one decision weight must be positive.")
        st.stop()

c1, c2, c3 = st.columns(3)
with c1:
    if slots is None:
        f = st.file_uploader("Process-4 physical locations", ["csv", "xlsx"], key="md_slots")
        if f is not None:
            slots = read_upload(f)
with c2:
    if mto is None:
        f = st.file_uploader("Historical MTO detail", ["csv", "xlsx"], key="md_mto")
        if f is not None:
            mto = normalize_mto(read_upload(f))
with c3:
    baseline_file = st.file_uploader("Current SKU-location mapping (optional)", ["csv", "xlsx"], key="md_baseline")

if slots is None or mto is None:
    st.info("Provide the Process-4 location master and historical MTO detail to run the decision engine.")
    st.stop()

slot_lookup = {str(c).strip().upper(): c for c in slots.columns}
for target, options in {
    "SLOT_ID": ["SLOT_ID", "SLOT", "LOCATION_ID"],
    "X_M": ["X_M", "X", "CENTER_X"],
    "Y_M": ["Y_M", "Y", "CENTER_Y"],
    "DISTANCE_FROM_DOOR_M": ["DISTANCE_FROM_DOOR_M", "DISTANCE_M", "DISTANCE"],
}.items():
    if target not in slots.columns:
        for option in options:
            if option in slot_lookup:
                slots = slots.rename(columns={slot_lookup[option]: target})
                break

required = {"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"}
if required - set(slots.columns):
    st.error(f"Location master missing: {sorted(required - set(slots.columns))}")
    st.stop()

sku = prepare_sku_demand(mto, capacity_units=capacity, safety_buffer=buffer / 100)
# Keep the combined benchmark independent from management weights.
sku["COMBINED_SCORE"] = 0.5 * sku["FREQUENCY_SCORE"] + 0.5 * sku["VOLUME_SCORE"]

st.metric("MTO SKUs in movement master", f"{len(sku):,}")
st.metric("Process-4 physical locations", f"{len(slots):,}")

st.subheader("2. Alternative layout scenarios")
results = {}
for strategy in STRATEGIES:
    ranked, allocation, location, summary = evaluate_strategy(mto, slots, sku, strategy, capacity_units=capacity)
    results[strategy] = {"ranked": ranked, "allocation": allocation, "location": location, "summary": summary}
comparison = compare_layouts(results)

# Transparent management score.
def mm(series, higher=True):
    s = pd.to_numeric(series, errors="coerce").fillna(0.0)
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo:
        z = pd.Series(1.0, index=s.index)
    else:
        z = (s - lo) / (hi - lo)
    return z if higher else 1 - z

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
st.success(f"Recommended strategy under the selected management objective: **{best['Strategy']}** — decision score **{best['Decision Score']:.2f}/100**.")

st.dataframe(decision[["Strategy", "Line coverage %", "Belt coverage %", "Total one-way travel m", "Average location utilization %", "Shared locations", "Unallocated storage rows", "Decision Score", "Decision Rank"]], use_container_width=True, hide_index=True)

st.subheader("3. Current → recommended comparison")
current_summary = None
current_flow = None
if baseline_file is not None:
    try:
        current_mapping = normalize_location_master(read_upload(baseline_file))
        current_flow, current_summary = replay_current_layout(mto, current_mapping)
        st.success("Current-state baseline loaded successfully.")
    except Exception as exc:
        st.error(f"Could not read current-state mapping: {exc}")
else:
    st.warning("Current-state baseline is not supplied. No current-vs-optimized improvement percentage will be fabricated.")

if current_summary is not None:
    optimized_summary = results[best["Strategy"]]["summary"]
    delta = {
        "Current": current_summary,
        "Recommended": optimized_summary,
    }
    rows = []
    for metric in ["Line coverage %", "Belt coverage %", "Total one-way travel m", "Avg one-way m / assigned line", "Belt-weighted travel m"]:
        rows.append({"Metric": metric, "Current": current_summary[metric], "Recommended": optimized_summary[metric]})
    comparison_current = pd.DataFrame(rows)
    st.dataframe(comparison_current, use_container_width=True, hide_index=True)
    imp = improvement(current_summary, optimized_summary)
    ic = st.columns(5)
    ic[0].metric("Line coverage Δ", f"{imp['Line coverage change pp']:+.2f} pp")
    ic[1].metric("Belt coverage Δ", f"{imp['Belt coverage change pp']:+.2f} pp")
    ic[2].metric("Travel change", f"{imp['Travel change %']:+.2f}%")
    ic[3].metric("Avg travel/line", f"{imp['Avg travel / line change %']:+.2f}%")
    ic[4].metric("Weighted travel", f"{imp['Belt-weighted travel change %']:+.2f}%")
else:
    st.info("Upload the actual current SKU-location mapping to unlock the Current → Recommended improvement section.")

st.subheader("4. Recommended layout")
r = results[best["Strategy"]]
rc = st.columns(5)
rc[0].metric("Recommended strategy", best["Strategy"])
rc[1].metric("Line coverage", f"{best['Line coverage %']:.2f}%")
rc[2].metric("Belt coverage", f"{best['Belt coverage %']:.2f}%")
rc[3].metric("Shared locations", int(best["Shared locations"]))
rc[4].metric("Used locations", int(r["location"]["USED_STORAGE_EQ"].gt(0).sum()))

st.markdown("**Recommended SKU-location allocation**")
st.dataframe(r["allocation"].sort_values(["RANK", "DISTANCE_FROM_DOOR_M"]), use_container_width=True, height=420)

st.subheader("5. Management action list")
a = r["allocation"].copy()
a = a[a["SLOT_ID"] != "UNALLOCATED"].copy()
a["ACTION"] = a["MOVEMENT_SEGMENT"].map({
    "High Frequency / High Volume": "Prioritize near-door accessibility",
    "High Frequency / Standard Volume": "Prioritize near-door accessibility",
    "Standard Frequency / High Volume": "Prioritize adequate storage capacity",
    "Standard Frequency / Standard Volume": "Use shared-location capacity where compatible",
}).fillna("Validate physical handling requirement")
a["RATIONALE"] = a["MOVEMENT_SEGMENT"] + "; rank " + a["RANK"].astype(str)
actions = a[["ITEM_SIZE", "SLOT_ID", "ALLOCATED_STORAGE_EQ", "DISTANCE_FROM_DOOR_M", "MOVEMENT_SEGMENT", "ACTION", "RATIONALE"]]
st.dataframe(actions, use_container_width=True, height=420)

st.subheader("6. Sensitivity check")
sensitivity = []
scenarios = [
    ("Balanced",25,25,25,25),
    ("Movement priority",15,20,50,15),
    ("Service priority",50,20,20,10),
    ("Volume priority",20,50,20,10),
    ("Space priority",15,20,15,50),
]
for name, af, av, at, ass in scenarios:
    scores = comparison.copy()
    scores["score"] = (af/100)*mm(scores["Line coverage %"])+(av/100)*mm(scores["Belt coverage %"])+(at/100)*mm(scores["Total one-way travel m"],False)+(ass/100)*mm(scores["Average location utilization %"])
    winner = scores.sort_values("score", ascending=False).iloc[0]
    sensitivity.append({"Scenario":name,"Winner":winner["Strategy"],"Score":winner["score"]*100})
st.dataframe(pd.DataFrame(sensitivity), use_container_width=True, hide_index=True)

st.subheader("7. Export")
exports = {
    "Recommended_Layout.csv": r["allocation"],
    "Management_Decision.csv": decision,
    "Management_Action_List.csv": actions,
    "Strategy_Comparison.csv": comparison,
    "Sensitivity_Analysis.csv": pd.DataFrame(sensitivity),
}
if current_summary is not None:
    exports["Current_vs_Recommended.csv"] = comparison_current
for name, df in exports.items():
    st.download_button(f"Download {name}", df.to_csv(index=False).encode("utf-8"), name, "text/csv", key="md_"+name)

st.caption("Model boundary: the recommended allocation is a scenario-based decision aid. Storage capacity, SKU compatibility, replenishment rules, route constraints, congestion and current-state locations require physical validation before implementation.")
