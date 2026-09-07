"""Model 2B — movement-aware multi-SKU shared-location layout optimization.

This page extends the existing Model 2 experiment without replacing Model 1.
It tries to reuse Model 1 / Process 4 objects from Streamlit session state. If the
running app does not expose them, the page accepts a Process-4 slot CSV and MTO file.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.layout_optimizer import STRATEGIES, prepare_sku_demand, evaluate_strategy, compare_layouts, replay_flow

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

    dc = col(["DATE", "DATETIME", "DAY"])
    sc = col(["ITEM_SIZE", "BELT_SIZE", "SKU", "LINE_ITEM_SIZE_ID", "SIZE"])
    qc = col(["NO_OF_BELTS", "BELTS", "QUANTITY", "QTY"])
    if not all([dc, sc, qc]):
        raise ValueError(
            f"MTO file must contain date, SKU and belt quantity columns. Found: {list(df.columns)}"
        )
    out = df.rename(columns={dc: "DATE", sc: "ITEM_SIZE", qc: "BELTS"}).copy()
    out["DATE"] = pd.to_datetime(out["DATE"], errors="coerce", dayfirst=True)
    out["ITEM_SIZE"] = out["ITEM_SIZE"].astype("string").str.strip().str.upper()
    out["BELTS"] = pd.to_numeric(out["BELTS"], errors="coerce")
    return out.dropna(subset=["DATE", "ITEM_SIZE", "BELTS"])[lambda x: x["BELTS"] > 0].copy()


def allocation_plot(slots, allocation, title):
    fig = go.Figure()
    if slots.empty:
        return fig
    fig.add_trace(
        go.Scatter(
            x=slots.X_M,
            y=slots.Y_M,
            mode="markers",
            marker=dict(size=7, symbol="square-open"),
            name="Available locations",
        )
    )
    a = allocation[allocation["SLOT_ID"] != "UNALLOCATED"].copy()
    if not a.empty:
        a["LABEL"] = a["ITEM_SIZE"].astype(str) + " | " + a["SLOT_ID"].astype(str)
        fig.add_trace(
            go.Scatter(
                x=a.X_M,
                y=a.Y_M,
                mode="markers",
                marker=dict(size=11, symbol="square"),
                text=a.LABEL,
                customdata=np.c_[
                    a.ITEM_SIZE,
                    a.SLOT_ID,
                    a.RANK,
                    a.ALLOCATED_STORAGE_EQ,
                ],
                hovertemplate=(
                    "SKU %{customdata[0]}<br>Location %{customdata[1]}<br>"
                    "Rank %{customdata[2]}<br>Allocated eq %{customdata[3]:.2f}<extra></extra>"
                ),
                name="SKU allocations",
            )
        )
    fig.update_layout(
        height=650,
        title=title,
        xaxis_title="X (m)",
        yaxis_title="Y (m)",
        margin=dict(l=20, r=20, t=50, b=20),
    )
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def _minmax(values, higher_is_better=True):
    s = pd.to_numeric(values, errors="coerce").fillna(0.0)
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo:
        out = pd.Series(1.0, index=s.index)
    else:
        out = (s - lo) / (hi - lo)
    return out if higher_is_better else 1.0 - out


def objective_scores(comparison, weights):
    """Calculate a transparent management-objective score from scenario KPIs.

    This is a decision score, not an observed warehouse KPI. All four strategy
    scenarios use the same Process-4 physical opportunity, so the comparison
    isolates the allocation/layout strategy.
    """
    x = comparison.copy()
    x["Frequency objective"] = _minmax(x["Line coverage %"], True)
    x["Volume objective"] = _minmax(x["Belt coverage %"], True)
    x["Travel objective"] = _minmax(x["Total one-way travel m"], False)
    x["Space objective"] = _minmax(x["Average location utilization %"], True)
    x["Decision Score"] = (
        weights["frequency"] * x["Frequency objective"]
        + weights["volume"] * x["Volume objective"]
        + weights["travel"] * x["Travel objective"]
        + weights["space"] * x["Space objective"]
    ) * 100
    x["Decision Rank"] = x["Decision Score"].rank(method="min", ascending=False).astype(int)
    return x.sort_values(["Decision Rank", "Strategy"]).reset_index(drop=True)


# Try to reuse objects from the main app.
slots = _find_session_df({"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"})
mto = _find_session_df({"DATE", "ITEM_SIZE", "BELTS"})

st.info(
    "The optimizer keeps the Process-4 physical locations fixed. It changes only how the SKU universe is grouped and assigned to those locations."
)

with st.sidebar:
    st.header("Optimizer controls")
    capacity_units = st.number_input(
        "Capacity per physical location (pallet-equivalent)",
        min_value=0.10,
        max_value=10.0,
        value=1.0,
        step=0.10,
    )
    safety_buffer = st.slider("Storage planning buffer (%)", 0, 50, 15, 5)
    st.divider()
    st.subheader("Decision objective weights")
    freq_weight = st.slider("Frequency / line coverage (%)", 0, 100, 25, 5)
    volume_weight = st.slider("Volume / belt coverage (%)", 0, 100, 30, 5)
    travel_weight = st.slider("Travel efficiency (%)", 0, 100, 30, 5)
    space_weight = st.slider("Space utilization (%)", 0, 100, 15, 5)
    weight_total = freq_weight + volume_weight + travel_weight + space_weight
    if weight_total == 0:
        st.error("At least one objective weight must be greater than zero.")
        st.stop()
    if weight_total != 100:
        st.caption(f"Weights currently total {weight_total}%. They will be normalized to 100% for the decision score.")
    st.caption("Capacity, buffer and weights are modelling controls. Validate physical capacity, compatibility and replenishment rules before implementation.")

if slots is None or mto is None:
    st.warning(
        "Process-4 slots or MTO detail were not exposed by the current page session. Upload them below to run the optimizer independently."
    )
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
    sku = prepare_sku_demand(
        mto,
        capacity_units=capacity_units,
        safety_buffer=safety_buffer / 100,
    )
except Exception as exc:
    st.error(f"Could not build SKU movement master: {exc}")
    st.stop()

# Keep the existing combined strategy configurable, while preserving the original
# 50:50 weighting when the user selects 50% here.
combined_freq_weight = freq_weight / max(freq_weight + volume_weight, 1)
sku["COMBINED_SCORE"] = (
    combined_freq_weight * sku["FREQUENCY_SCORE"]
    + (1 - combined_freq_weight) * sku["VOLUME_SCORE"]
)

st.subheader("1. V-belt SKU movement universe")
cards = st.columns(5)
cards[0].metric("SKUs in MTO", f"{len(sku):,}")
cards[1].metric("MTO lines", f"{len(mto):,}")
cards[2].metric("MTO belts", f"{mto['BELTS'].sum():,.0f}")
cards[3].metric("Physical locations", f"{len(slots):,}")
cards[4].metric("Storage buffer", f"{safety_buffer}%")

with st.expander("SKU movement and storage master", expanded=False):
    st.dataframe(sku, use_container_width=True, height=420)

st.subheader("2. Benchmark the allocation strategies")
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
weights = {
    "frequency": freq_weight / weight_total,
    "volume": volume_weight / weight_total,
    "travel": travel_weight / weight_total,
    "space": space_weight / weight_total,
}
decision = objective_scores(comparison, weights)

# Keep the original model-defined balanced score visible as a benchmark, but use the
# transparent management objective score for the recommendation.
st.dataframe(decision, use_container_width=True, hide_index=True)

best = decision.iloc[0]
movement_space_rank = int(decision.loc[decision["Strategy"] == "Movement + Space", "Decision Rank"].iloc[0])
if best["Strategy"] == "Movement + Space":
    st.success(
        f"Recommended under the selected objective: **Movement + Space** "
        f"(decision score {best['Decision Score']:.2f}/100, rank 1)."
    )
else:
    st.info(
        f"Recommended under the selected objective: **{best['Strategy']}** "
        f"(decision score {best['Decision Score']:.2f}/100). "
        f"Movement + Space ranks {movement_space_rank}. This shows that the preferred layout depends on management priorities."
    )
st.caption(
    "Decision score is model-defined: normalized line coverage, belt coverage, travel efficiency and space utilization are combined using the selected weights. It is not an observed warehouse KPI."
)

fig = px.bar(
    decision.sort_values("Decision Score", ascending=False),
    x="Strategy",
    y="Decision Score",
    title="Management objective score by strategy",
)
st.plotly_chart(fig, use_container_width=True)

c1, c2 = st.columns(2)
with c1:
    fig = px.bar(comparison, x="Strategy", y="Line coverage %", title="Historical MTO line coverage")
    st.plotly_chart(fig, use_container_width=True)
with c2:
    fig = px.bar(comparison, x="Strategy", y="Belt coverage %", title="Historical belt-volume coverage")
    st.plotly_chart(fig, use_container_width=True)

c1, c2 = st.columns(2)
with c1:
    fig = px.bar(comparison, x="Strategy", y="Total one-way travel m", title="Comparative one-way movement distance")
    st.plotly_chart(fig, use_container_width=True)
with c2:
    fig = px.bar(comparison, x="Strategy", y="Average location utilization %", title="Average physical-location utilization")
    st.plotly_chart(fig, use_container_width=True)

st.subheader("3. Sensitivity analysis — does the recommendation change?")
scenarios = [
    ("Balanced", 25, 25, 25, 25),
    ("Movement priority", 15, 20, 50, 15),
    ("Service / frequency priority", 50, 20, 20, 10),
    ("Volume priority", 20, 50, 20, 10),
    ("Space priority", 15, 20, 15, 50),
]
sensitivity_rows = []
for label, wf, wv, wt, ws in scenarios:
    sc = objective_scores(
        comparison,
        {"frequency": wf / 100, "volume": wv / 100, "travel": wt / 100, "space": ws / 100},
    )
    winner = sc.iloc[0]
    sensitivity_rows.append(
        {
            "Scenario": label,
            "Frequency %": wf,
            "Volume %": wv,
            "Travel %": wt,
            "Space %": ws,
            "Winner": winner["Strategy"],
            "Winner score": winner["Decision Score"],
            "Movement + Space rank": int(sc.loc[sc["Strategy"] == "Movement + Space", "Decision Rank"].iloc[0]),
        }
    )
sensitivity = pd.DataFrame(sensitivity_rows)
st.dataframe(sensitivity, use_container_width=True, hide_index=True)

winner_counts = sensitivity["Winner"].value_counts().rename_axis("Strategy").reset_index(name="Wins")
fig = px.bar(winner_counts, x="Strategy", y="Wins", title="Number of sensitivity scenarios won")
st.plotly_chart(fig, use_container_width=True)

st.caption(
    "Sensitivity scenarios are analytical stress tests, not forecasts. They demonstrate whether the preferred layout is robust to different management priorities."
)

st.subheader("4. Inspect each proposed layout")
strategy = st.selectbox(
    "Layout scenario",
    STRATEGIES,
    index=STRATEGIES.index("Movement + Space"),
)
r = results[strategy]

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Used locations", int(r["location"]["USED_STORAGE_EQ"].gt(0).sum()))
m2.metric("Shared locations", int(r["location"]["ASSIGNED_SKUS"].gt(1).sum()))
m3.metric("Line coverage", f"{r['summary']['Line coverage %']:.2f}%")
m4.metric("Belt coverage", f"{r['summary']['Belt coverage %']:.2f}%")
m5.metric("Unallocated storage rows", int(r["summary"]["Unallocated storage rows"]))

st.plotly_chart(
    allocation_plot(slots, r["allocation"], f"{strategy} — multi-SKU physical allocation"),
    use_container_width=True,
)

left, right = st.columns(2)
with left:
    st.markdown("**Location utilization**")
    st.dataframe(
        r["location"].sort_values("CAPACITY_UTILIZATION_PCT", ascending=False),
        use_container_width=True,
        height=360,
    )
with right:
    st.markdown("**SKU-to-location allocation**")
    st.dataframe(
        r["allocation"].sort_values(["RANK", "DISTANCE_FROM_DOOR_M"]),
        use_container_width=True,
        height=360,
    )

st.subheader("5. Historical daily MTO replay")
flow, daily = replay_flow(mto, r["allocation"])
fig = px.line(
    daily,
    x="DATE",
    y="TOTAL_ONE_WAY_TRAVEL_M",
    title=f"Daily comparative travel — {strategy}",
)
st.plotly_chart(fig, use_container_width=True)
st.dataframe(daily, use_container_width=True, height=360)

st.subheader("6. Benchmark summary for viva / management review")
benchmark_cols = [
    "Strategy",
    "Line coverage %",
    "Belt coverage %",
    "Total one-way travel m",
    "Avg one-way m / assigned line",
    "Average location utilization %",
    "Peak location utilization %",
    "Shared locations",
    "Unallocated storage rows",
]
benchmark = decision[benchmark_cols + ["Decision Score", "Decision Rank"]].copy()
st.dataframe(benchmark, use_container_width=True, hide_index=True)

st.subheader("7. Exportable model outputs")
exports = {
    "SKU_Movement_Storage_Master.csv": sku,
    "Strategy_Comparison.csv": comparison,
    "Decision_Objective_Comparison.csv": decision,
    "Sensitivity_Analysis.csv": sensitivity,
    "Selected_Strategy_Ranking.csv": r["ranked"],
    "Selected_Strategy_Allocation.csv": r["allocation"],
    "Selected_Strategy_Location_Utilization.csv": r["location"],
    "Selected_Strategy_Daily_Replay.csv": daily,
}
for name, df in exports.items():
    st.download_button(
        f"Download {name}",
        df.to_csv(index=False).encode("utf-8"),
        file_name=name,
        mime="text/csv",
        key=f"dl_{name}",
    )

st.caption(
    "Model boundary: storage requirement is a planning proxy derived from historical peak daily boxes; multiple SKUs may share a location; actual physical compatibility, replenishment, route constraints, congestion and current-state SKU locations require warehouse validation."
)
