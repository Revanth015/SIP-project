"""Model 2 — Actual MTO transaction-level Digital Twin page."""

from pathlib import Path
import tempfile

import pandas as pd
import streamlit as st

from core.mto_actual_engine import (
    ActualMTOConfig,
    prepare_actual_mto,
    simulate_actual_mto,
    build_actual_kpi_table,
    build_storage_paradox_actual,
)

st.set_page_config(page_title="Model 2 · Actual MTO", page_icon="📦", layout="wide")

st.title("📦 Model 2 — Actual MTO Process Digital Twin")
st.caption(
    "Transaction-level MTO model using actual belt quantities and the Size/Box Master. "
    "No pallet-to-MTO conversion is used in this model."
)

with st.sidebar:
    st.header("Actual MTO Data")
    mto_file = st.file_uploader(
        "MTO Packing List (.xlsx)", type=["xlsx"], key="actual_mto_file",
        help="Use the workbook containing the Consolidated_Packing_Lists sheet."
    )
    box_file = st.file_uploader(
        "Size / Box Master (.xlsx)", type=["xlsx"], key="actual_box_file",
        help="Use the workbook containing Belt Size and Units Per Box."
    )

    st.divider()
    st.subheader("Box-standard rules")
    default_box = st.number_input(
        "Default box quantity", min_value=1.0, max_value=10000.0, value=28.0, step=1.0,
        help="Used when the SKU is absent from the master or its master value is zero."
    )
    conflict_box = st.number_input(
        "Conflicting-standard fallback", min_value=1.0, max_value=10000.0, value=28.0, step=1.0,
        help="Used when one SKU has multiple positive box standards in the master. Such rows are flagged."
    )
    round_non_integer = st.checkbox(
        "Round non-integer belt quantities to whole belts", value=True,
        help="Belts are discrete units. The model reports how many source rows required this treatment."
    )

    st.divider()
    st.subheader("Current vs proposed process")
    current_touches = st.number_input("Current touches / box", 0.1, 100.0, 4.0, 0.5)
    proposed_touches = st.number_input("Proposed touches / box", 0.1, 100.0, 2.0, 0.5)
    current_pack = st.number_input("Current pack time (sec/box)", 0.0, 3600.0, 40.0, 5.0)
    proposed_pack = st.number_input("Proposed pack time (sec/box)", 0.0, 3600.0, 25.0, 5.0)
    current_count = st.number_input("Current count time (sec/box)", 0.0, 3600.0, 30.0, 5.0)
    proposed_count = st.number_input("Proposed count time (sec/box)", 0.0, 3600.0, 20.0, 5.0)
    current_store = st.number_input("Current store time (sec/event)", 0.0, 3600.0, 60.0, 5.0)
    proposed_store = st.number_input("Proposed store time (sec/event)", 0.0, 3600.0, 35.0, 5.0)
    current_retrieve = st.number_input("Current retrieve time (sec/event)", 0.0, 3600.0, 90.0, 5.0)
    proposed_retrieve = st.number_input("Proposed retrieve time (sec/event)", 0.0, 3600.0, 45.0, 5.0)

    st.divider()
    st.subheader("Proxy assumptions")
    error_probability = st.number_input("Error probability / touch (%)", 0.0, 100.0, 1.5, 0.1)
    fatigue_touch = st.number_input("Fatigue points / touch", 0.0, 100.0, 1.0, 0.1)
    fatigue_wip = st.number_input("Fatigue points / WIP cycle", 0.0, 100.0, 2.5, 0.1)

    st.divider()
    st.subheader("Automation what-if")
    automation = st.slider("Automation coverage (%)", 0, 100, 0)
    auto_time = st.slider("Automation time reduction (%)", 0, 100, 20)
    auto_touch = st.slider("Automation touch reduction (%)", 0, 100, 20)

    st.divider()
    st.subheader("Storage")
    box_footprint = st.number_input("Box footprint (m²)", 0.001, 100.0, 0.25, 0.01)
    wip_multiplier = st.number_input("WIP location multiplier", 0.1, 10.0, 1.0, 0.1)
    current_wip_baseline = st.number_input(
        "Measured current temporary-WIP boxes", 0.0, 100000.0, 0.0, 1.0,
        help="Enter a measured current temporary-WIP baseline if available. Zero means the current process is treated as having no separate temporary-WIP stock."
    )

    run = st.button("▶ Run Actual MTO Model", type="primary", use_container_width=True)


def save_upload(upload):
    suffix = Path(upload.name).suffix.lower()
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.write(upload.getbuffer())
    f.close()
    return f.name


if not mto_file or not box_file:
    st.info("Upload the actual MTO Packing List and Size / Box Master to run Model 2.")
    st.markdown(
        """
### Data logic used

1. **Positive `Units Per Box` in the master → use it.**
2. **`Units Per Box = 0` → do not use zero; use the default 28.**
3. **No SKU match → use the default 28.**
4. **Multiple positive standards for the same SKU → flag as conflict and use the configurable conflict fallback (28 by default).**
5. Proposed temporary WIP is tracked at **ITEM_SIZE level**, so later quantities of the same SKU can complete an open partial box.
        """
    )
    st.stop()

if run:
    try:
        cfg = ActualMTOConfig(
            default_box_qty=default_box,
            conflict_box_qty=conflict_box,
            round_non_integer_belts=round_non_integer,
            current_touches_per_box=current_touches,
            proposed_touches_per_box=proposed_touches,
            current_pack_time_sec=current_pack,
            proposed_pack_time_sec=proposed_pack,
            current_count_time_sec=current_count,
            proposed_count_time_sec=proposed_count,
            current_store_time_sec=current_store,
            proposed_store_time_sec=proposed_store,
            current_retrieve_time_sec=current_retrieve,
            proposed_retrieve_time_sec=proposed_retrieve,
            error_probability_per_touch_pct=error_probability,
            fatigue_points_per_touch=fatigue_touch,
            fatigue_points_per_wip_cycle=fatigue_wip,
            automation_coverage_pct=automation,
            automation_time_reduction_pct=auto_time,
            automation_touch_reduction_pct=auto_touch,
            box_footprint_m2=box_footprint,
            wip_location_multiplier=wip_multiplier,
        )
        with st.spinner("Reading actual MTO transactions and simulating SKU-level WIP..."):
            mto_detail, box_master = prepare_actual_mto(save_upload(mto_file), save_upload(box_file), cfg)
            daily, wip_detail, summary = simulate_actual_mto(mto_detail, cfg)
        st.session_state.actual_mto_detail = mto_detail
        st.session_state.actual_box_master = box_master
        st.session_state.actual_mto_daily = daily
        st.session_state.actual_mto_wip = wip_detail
        st.session_state.actual_mto_summary = summary
        st.session_state.actual_mto_cfg = cfg
        st.success("Actual-data Model 2 completed.")
    except Exception as exc:
        st.error(f"Model 2 could not be completed: {exc}")
        st.exception(exc)
        st.stop()

if "actual_mto_summary" not in st.session_state:
    st.stop()

summary = st.session_state.actual_mto_summary
daily = st.session_state.actual_mto_daily
wip_detail = st.session_state.actual_mto_wip
mto_detail = st.session_state.actual_mto_detail
box_master = st.session_state.actual_box_master
cfg = st.session_state.actual_mto_cfg

st.subheader("1 · Executive Decision Dashboard")
a, b, c, d, e, f = st.columns(6)
a.metric("Processing Time", f"{summary['time_reduction_pct']:.1f}% ↓")
b.metric("Manual Touches", f"{summary['touch_reduction_pct']:.1f}% ↓")
c.metric("Fatigue Proxy", f"{summary['fatigue_reduction_pct']:.1f}% ↓")
d.metric("Error-Risk Proxy", f"{summary['error_risk_reduction_pct']:.1f}% ↓")
e.metric("Peak Temporary WIP", f"{summary['peak_closing_wip_boxes']:.0f} boxes")
f.metric("Peak WIP Space", f"{summary['peak_closing_wip_storage_m2']:.1f} m²")

st.subheader("2 · Current vs Proposed Process")
st.markdown("**Current:** Actual transaction quantity → box requirement → manual handling")
st.markdown("**Proposed:** Actual transaction quantity → full boxes + single-SKU partial box → temporary WIP → later same-SKU completion → dispatch")
kpi = build_actual_kpi_table(summary)
st.dataframe(kpi, use_container_width=True, hide_index=True)

st.subheader("3 · Storage Paradox")
left, right = st.columns(2)
with left:
    st.metric("Average Proposed Temporary WIP", f"{summary['average_closing_wip_storage_m2']:.2f} m²")
    st.metric("Peak Proposed Temporary WIP", f"{summary['peak_closing_wip_storage_m2']:.2f} m²")
with right:
    current_m2 = current_wip_baseline * cfg.box_footprint_m2 * cfg.wip_location_multiplier
    avg_net = summary['average_closing_wip_storage_m2'] - current_m2
    peak_net = summary['peak_closing_wip_storage_m2'] - current_m2
    st.metric("Average Net Storage Change", f"{avg_net:+.2f} m²")
    st.metric("Peak Net Storage Change", f"{peak_net:+.2f} m²")
st.caption(
    "Positive net storage change means the proposed temporary-SKU staging requires more space than the measured current temporary-WIP baseline. "
    "This is intentionally shown as a separate temporary-storage requirement; it is not confused with physical warehouse capacity."
)

storage = build_storage_paradox_actual(summary, cfg.box_footprint_m2).copy()
st.dataframe(storage, use_container_width=True, hide_index=True)

st.subheader("4 · Daily SKU-Level WIP Simulation")
chart_df = daily.set_index("DATE")[["OPENING_WIP_BOXES", "NEW_PARTIAL_WIP_BOXES", "BOXES_COMPLETED_FROM_WIP", "CLOSING_WIP_BOXES"]]
st.line_chart(chart_df, use_container_width=True)
st.dataframe(daily, use_container_width=True, hide_index=True)

st.subheader("5 · Final Open WIP by SKU")
if wip_detail.empty:
    st.success("No SKU-level temporary WIP remains at the end of the selected history.")
else:
    st.dataframe(wip_detail, use_container_width=True, hide_index=True)

st.subheader("6 · Box Master / Data Quality")
q1, q2, q3, q4, q5 = st.columns(5)
q1.metric("MTO Transactions", f"{summary['transactions']:,}")
q2.metric("Total Belts", f"{summary['total_belts']:,.0f}")
q3.metric("Master-standard rows", f"{summary['master_standard_rows']:,}")
q4.metric("Defaulted rows", f"{summary['default_missing_rows'] + summary['default_zero_rows']:,}")
q5.metric("Conflict rows", f"{summary['default_conflict_rows']:,}")

st.warning(
    f"{summary['default_missing_rows']:,} transactions had no SKU match, "
    f"{summary['default_zero_rows']:,} had a zero master value, and "
    f"{summary['default_conflict_rows']:,} were affected by conflicting positive standards. "
    f"The configured fallback is {cfg.default_box_qty:g} belts/box. "
    f"{summary['non_integer_rows']:,} source rows contained non-integer belt quantities."
)
st.dataframe(
    mto_detail[["ITEM_SIZE", "NO_OF_BELTS_RAW", "NO_OF_BELTS_USED", "BOX_QTY_USED", "BOX_SOURCE", "FULL_BOXES", "PARTIAL_REMAINDER_BELTS", "PARTIAL_BOX_FLAG"]].head(1000),
    use_container_width=True,
    hide_index=True,
)

st.subheader("7 · Download Model 2 Evidence")
with tempfile.TemporaryDirectory() as tmpdir:
    path = Path(tmpdir) / "Model_2_Actual_MTO_Analysis.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        kpi.to_excel(writer, sheet_name="Process_KPIs", index=False)
        daily.to_excel(writer, sheet_name="Daily_WIP", index=False)
        wip_detail.to_excel(writer, sheet_name="Open_WIP_by_SKU", index=False)
        storage.to_excel(writer, sheet_name="Storage_Paradox", index=False)
        mto_detail.to_excel(writer, sheet_name="Transaction_Box_Detail", index=False)
        box_master.to_excel(writer, sheet_name="Box_Master_Cleaned", index=False)
    st.download_button(
        "⬇ Download Actual Model 2 Excel",
        data=path.read_bytes(),
        file_name="Model_2_Actual_MTO_Analysis.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.caption(
    "Method note: box standards come from the supplied Size Master. Missing and zero standards use the editable default. "
    "Conflicting positive standards are flagged rather than silently choosing one. Fatigue and error outputs are proxies."
)
