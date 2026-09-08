"""SIP continuation — MTO storage optimization using the existing Process-4 output."""
import io

import pandas as pd
import plotly.express as px
import streamlit as st
from shapely.geometry import Point

from core.milp_optimizer import BOXES_PER_PALLET, clean_mto, company_master, sku_cooccurrence, sku_master, solve_day

st.set_page_config(page_title="MTO MILP Optimization — JK Fenner", page_icon="🎯", layout="wide")
st.title("🎯 MTO Storage Optimization — MILP")
st.caption("SIP continuation after Process 4 — existing physical output is reused; MTO is analysed separately here.")

m1 = st.session_state.get("m1_result")
area_region = st.session_state.get("area_region")
area_range = st.session_state.get("area_range")

# Critical lineage rule: never rerun Processes 1–4 on this page.
if not isinstance(m1, dict) or "slot_df" not in m1 or "demand_detail" not in m1:
    st.error("Process 1–4 data is not available. Complete the existing SIP Process 1–4 workflow first.")
    st.stop()
if area_region is None or area_range is None:
    st.error("The Process-4 selected area is not available. Select the study area in the existing SIP workflow first.")
    st.stop()

process4 = m1["slot_df"].copy()
required = ["SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"]
if not set(required).issubset(process4.columns):
    st.error(f"The existing Process-4 location/distance table is incomplete. Required columns: {required}")
    st.stop()
mask = process4.apply(lambda r: area_region.covers(Point(float(r.X_M), float(r.Y_M))), axis=1)
process4 = process4.loc[mask, required].copy().sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)
if process4.empty:
    st.error("The existing Process-4 selected area contains no generated pallet positions.")
    st.stop()

mto = clean_mto(m1["demand_detail"])

st.subheader("1. Existing Process-4 output used as the physical MILP input")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Process-4 pallet positions", f"{len(process4):,}")
c2.metric("MTO transaction lines", f"{len(mto):,}")
c3.metric("Historical MTO boxes", f"{mto.BOXES.sum():,.0f}")
c4.metric("Boxes / pallet", BOXES_PER_PALLET)
st.success("🟢 Process-4 data connected. The MILP uses the existing pallet-location IDs, coordinates and distance-from-door table. No physical slot generation is repeated here.")
st.caption(f"Selected area: X {area_range[0]:.2f}–{area_range[1]:.2f} m · Y {area_range[2]:.2f}–{area_range[3]:.2f} m")
with st.expander("Exact Process-4 location + distance table", expanded=True):
    st.dataframe(process4, use_container_width=True, height=360)

st.subheader("2. Separate MTO calculation")
cm = company_master(mto)
sm = sku_master(mto)
co = sku_cooccurrence(mto, 150)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Companies", len(cm))
c2.metric("Priority companies", int(cm.PRIORITY.sum()))
c3.metric("Priority box share", f"{cm.loc[cm.PRIORITY, 'MTO_BOXES'].sum() / cm.MTO_BOXES.sum() * 100:.2f}%")
c4.metric("SKUs", len(sm))
c5.metric("MTO dates", mto.DATE.nunique())

st.markdown("**Priority rule:** rank companies by historical MTO box volume and select the smallest company set reaching at least 80% cumulative box volume. All other companies remain eligible for storage; priority changes the optimization preference, not eligibility.")
left, right = st.columns(2)
with left:
    st.dataframe(cm[["COMPANY_ID", "COMPANY_NAME", "MTO_BOXES", "MTO_BELTS", "MTO_LINES", "MTO_ORDERS", "VOLUME_SHARE_PCT", "CUMULATIVE_SHARE_PCT", "PRIORITY"]].round(3), use_container_width=True, height=420)
with right:
    top = cm.head(15).sort_values("MTO_BOXES")
    st.plotly_chart(px.bar(top, x="MTO_BOXES", y="COMPANY_NAME", orientation="h", title="Top companies by historical MTO boxes"), use_container_width=True)

st.subheader("3. MILP formulation")
st.latex(r"q_{o,l}=\text{integer boxes from order }o\text{ stored at Process-4 pallet position }l")
st.latex(r"h_o=\text{boxes from order }o\text{ held outside the selected area}")
st.latex(r"\sum_l q_{o,l}+h_o=B_o\quad\forall o")
st.latex(r"\sum_o q_{o,l}\le32\quad\forall l")
st.latex(r"q_{o,l}\le32y_{o,l},\quad y_{o,l}\in\{0,1\}")
st.markdown("The solve is lexicographic: **(1)** maximise priority-company boxes stored, **(2)** maximise total boxes stored, **(3)** minimise distance-weighted retrieval effort and order-to-location fragmentation. Thus the 93 physical pallet positions are a fixed constraint inherited from Process 4, while the MTO data determines the allocation.")

with st.sidebar:
    st.header("MILP controls")
    date_options = sorted(mto.DATE.dt.normalize().dropna().unique())
    selected_date = st.selectbox("MTO planning date", date_options, index=len(date_options) - 1, format_func=lambda x: pd.Timestamp(x).strftime("%d-%b-%Y"))
    time_limit = st.slider("MILP time limit (seconds)", 10, 120, 30, 10)
    run = st.button("▶ Run MILP", type="primary", use_container_width=True)
    run_all = st.button("Calculate all MTO dates", use_container_width=True)

if run:
    with st.spinner("Solving the three-stage MILP using the existing Process-4 pallet positions…"):
        try:
            st.session_state["mto_milp_result"] = solve_day(mto, process4, selected_date, time_limit=time_limit)
            st.session_state["mto_milp_date"] = pd.Timestamp(selected_date)
        except Exception as exc:
            st.error(f"MILP failed: {exc}")

if run_all:
    rows = []
    dates = sorted(mto.DATE.dt.normalize().unique())
    progress = st.progress(0)
    try:
        for i, dt in enumerate(dates):
            rr = solve_day(mto, process4, dt, time_limit=min(time_limit, 30))
            rows.append(rr["summary"])
            progress.progress((i + 1) / len(dates))
        st.session_state["mto_milp_all_days"] = pd.DataFrame(rows)
    except Exception as exc:
        st.error(f"All-date MILP calculation failed: {exc}")
    finally:
        progress.empty()

result = st.session_state.get("mto_milp_result")
if result is None:
    st.info("Select an MTO planning date and click **Run MILP**. Processes 1–4 will not be rerun.")
else:
    summary = result["summary"]
    st.subheader(f"4. MILP result — {pd.Timestamp(summary['Date']).strftime('%d-%b-%Y')}")
    cards = st.columns(6)
    cards[0].metric("Process-4 pallets", summary["Process4_pallet_positions"])
    cards[1].metric("MTO boxes", f"{summary['MTO_boxes']:,}")
    cards[2].metric("Pallet equivalent", summary["Required_pallet_equivalent"])
    cards[3].metric("Boxes stored", f"{summary['Boxes_stored']:,}")
    cards[4].metric("Boxes held", f"{summary['Boxes_held']:,}")
    cards[5].metric("Storage utilisation", f"{summary['Storage_utilisation_pct']:.1f}%")

    if summary["Boxes_held"]:
        st.warning(f"Capacity is binding: {summary['Boxes_held']:,} boxes cannot be accommodated inside the {summary['Process4_pallet_positions']} Process-4 pallet positions for this MTO date. These quantities become production/packing-priority candidates.")
    else:
        st.success("All MTO boxes for the selected date can be accommodated within the Process-4 pallet capacity under the MILP solution.")

    st.subheader("5. Optimized allocation across the existing Process-4 locations")
    loc = result["location_summary"]
    show = process4.copy()
    if not loc.empty:
        show = show.merge(loc.drop(columns=["DISTANCE_FROM_DOOR_M"], errors="ignore"), on="SLOT_ID", how="left")
    for c in ["BOXES_STORED", "ORDERS", "COMPANIES", "UTILISATION_PCT"]:
        if c not in show:
            show[c] = 0
    show = show.fillna({"BOXES_STORED": 0, "ORDERS": 0, "COMPANIES": 0, "UTILISATION_PCT": 0})
    st.dataframe(show.sort_values("DISTANCE_FROM_DOOR_M").round(2), use_container_width=True, height=430)
    st.plotly_chart(px.bar(show.sort_values("DISTANCE_FROM_DOOR_M"), x="DISTANCE_FROM_DOOR_M", y="BOXES_STORED", title="MILP loading against the existing Process-4 distance table", labels={"DISTANCE_FROM_DOOR_M": "Process-4 distance from door (m)", "BOXES_STORED": "Boxes stored"}), use_container_width=True)

    st.subheader("6. Order allocation and capacity decisions")
    st.dataframe(result["allocation"].sort_values(["DISTANCE_FROM_DOOR_M", "INVOICE_ID"]).round(2), use_container_width=True, height=420)
    if not result["held"].empty:
        st.subheader("Orders / quantities held outside selected capacity")
        st.dataframe(result["held"].sort_values("BOXES_HELD", ascending=False), use_container_width=True)

    st.subheader("7. SKU movement and candidate co-storage relationships")
    st.dataframe(result["sku_master"].head(50).round(3), use_container_width=True, height=400)
    st.markdown("Candidate SKU relationships are based on same-order co-occurrence in MTO data. They are planning relationships only; physical compatibility must be validated by JK Fenner operations.")
    st.dataframe(result["candidate_groups"], use_container_width=True, height=360)

all_days = st.session_state.get("mto_milp_all_days")
if all_days is not None:
    st.subheader("8. Management view across all MTO dates")
    st.dataframe(all_days, use_container_width=True, height=420)
    st.plotly_chart(px.line(all_days, x="Date", y=["MTO_boxes", "Boxes_stored"], markers=True, title="MTO box flow vs MILP-stored boxes"), use_container_width=True)
    st.plotly_chart(px.bar(all_days, x="Date", y="Boxes_held", title="Capacity-constrained boxes requiring production / packing attention"), use_container_width=True)

    peak = all_days.loc[all_days["MTO_boxes"].idxmax()]
    st.info(f"Peak MTO date in the supplied history: {peak['Date']} with {int(peak['MTO_boxes']):,} boxes ({int(peak['Required_pallet_equivalent'])} pallet equivalents) against {int(peak['Process4_pallet_positions'])} Process-4 pallet positions.")

if result is not None:
    st.subheader("9. Download MILP outputs")
    output = {
        "Process4_Locations": process4,
        "Company_Priority": cm,
        "SKU_Movement": sm,
        "SKU_Candidate_Groups": co,
        "MILP_Summary": pd.DataFrame([result["summary"]]),
        "MILP_Location_Allocation": show,
        "MILP_Order_Allocation": result["allocation"],
        "MILP_Held": result["held"],
    }
    if all_days is not None:
        output["MILP_All_Dates"] = all_days
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in output.items():
            df.to_excel(writer, sheet_name=name[:31], index=False)
    st.download_button("📥 Download SIP MTO MILP output workbook", data=buf.getvalue(), file_name="JK_Fenner_SIP_MTO_MILP_Output.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
