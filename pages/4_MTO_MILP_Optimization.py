"""Excel-only MTO MILP output stage using the existing Process-4 result."""
import streamlit as st

from core.milp_excel_export import build_milp_workbook
from core.milp_optimizer import BOXES_PER_PALLET, clean_mto
from shapely.geometry import Point

st.set_page_config(page_title="MTO MILP Excel Output — JK Fenner", page_icon="📊", layout="wide")
st.title("MTO Storage Optimization — Excel Output")
st.caption("Final methodology: reuse the existing Process-4 physical output, run the MTO MILP, and deliver the analysis only as an Excel workbook.")

m1 = st.session_state.get("m1_result")
area_region = st.session_state.get("area_region")
area_range = st.session_state.get("area_range")

if not isinstance(m1, dict) or "slot_df" not in m1 or "demand_detail" not in m1:
    st.error("Process 1–4 data is not available. Complete the existing Process 1–4 workflow first.")
    st.stop()
if area_region is None or area_range is None:
    st.error("The Process-4 selected area is not available. Select the study area in Process 4 first.")
    st.stop()

process4 = m1["slot_df"].copy()
required = ["SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"]
if not set(required).issubset(process4.columns):
    st.error(f"The existing Process-4 location table is incomplete. Required columns: {required}")
    st.stop()
mask = process4.apply(lambda r: area_region.covers(Point(float(r.X_M), float(r.Y_M))), axis=1)
process4 = process4.loc[mask, required].copy().sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)
mto = clean_mto(m1["demand_detail"])

c1, c2, c3, c4 = st.columns(4)
c1.metric("Process-4 pallet positions", f"{len(process4):,}")
c2.metric("MTO transaction lines", f"{len(mto):,}")
c3.metric("Historical MTO boxes", f"{mto.BOXES.sum():,.0f}")
c4.metric("Boxes / pallet", BOXES_PER_PALLET)

st.info("The page does not rerun Processes 1–4 and does not create a management dashboard. It takes the exact existing Process-4 locations/distance table and produces the complete Excel analysis pack.")

with st.expander("Process-4 input verification", expanded=True):
    st.write(f"Selected area: X {area_range[0]:.2f}–{area_range[1]:.2f} m · Y {area_range[2]:.2f}–{area_range[3]:.2f} m")
    st.write(f"Exact Process-4 pallet positions passed to MILP: **{len(process4)}**")
    st.dataframe(process4, use_container_width=True, height=320)

st.markdown("### Final MILP logic")
st.markdown("1. Maximise priority-company boxes stored (priority = smallest company set reaching ≥80% historical MTO box volume).  ")
st.markdown("2. Fix that optimum and maximise total boxes stored.  ")
st.markdown("3. Fix both optima and minimise box-distance from the door using the existing Process-4 distance values.  ")
st.markdown(f"4. Capacity is fixed at **{len(process4)} pallet positions × {BOXES_PER_PALLET} boxes per position**. No arbitrary SKU-per-location limit is imposed.")

if st.button("Generate complete Excel output pack", type="primary", use_container_width=True):
    with st.spinner("Running the exact daily MTO MILP and building the Excel output pack…"):
        try:
            data = build_milp_workbook(mto, process4)
            st.session_state["milp_excel_bytes"] = data
            st.success("Excel output pack generated and verified during creation.")
        except Exception as exc:
            st.error(f"Excel generation failed: {exc}")

if "milp_excel_bytes" in st.session_state:
    st.download_button(
        "Download DATA-DRIVEN WAREHOUSE OPTIMIZATION MILP outputs",
        data=st.session_state["milp_excel_bytes"],
        file_name="JK_Fenner_DATA_DRIVEN_WAREHOUSE_OPTIMIZATION_MILP_Outputs.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
