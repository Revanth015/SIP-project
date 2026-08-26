"""Universal Warehouse Digital Twin — Streamlit application."""
from pathlib import Path
import tempfile
import pandas as pd
import streamlit as st
from shapely.geometry import Point

from core.pipeline import run_pipeline, prepare_cad
from core.visualization_engine import plot_cad_geometry, plot_layout, create_daily_gif
from core.export_engine import build_kpi_table, export_excel
from core.process_engine import ProcessModelConfig, simulate_process_model, process_summary, storage_paradox_table

st.set_page_config(page_title="Universal Warehouse Digital Twin", page_icon="🏭", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>.main-title{font-size:2.2rem;font-weight:750}.subtitle{color:#6b7280;margin-bottom:1rem}</style>""", unsafe_allow_html=True)
st.markdown('<div class="main-title">🏭 Universal Warehouse Digital Twin</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">CAD-assisted warehouse capacity, layout, simulation and MTO process decision support</div>', unsafe_allow_html=True)


def save_uploaded(upload, suffix):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix); tmp.write(upload.getbuffer()); tmp.close(); return tmp.name


def get_manual_door(cad):
    warehouse = cad["warehouse"]; minx, miny, maxx, maxy = warehouse.bounds
    st.info("The operating door is a user-controlled input. Detected CAD doors are reference only; the Digital Twin will not automatically choose one.")
    mode = st.radio("Door input method", ["Enter coordinates", "Use a detected CAD door as starting point"], horizontal=True, key="door_mode")
    if mode == "Use a detected CAD door as starting point" and cad.get("doors"):
        doors = cad["doors"]; options = [f"CAD Door {i}: X={d['x']:.3f}, Y={d['y']:.3f} · {d['layer']}" for i,d in enumerate(doors,1)]
        ref = st.selectbox("Reference door", options, key="reference_door"); d=doors[options.index(ref)]; default_x,default_y=float(d["x"]),float(d["y"])
    else: default_x,default_y=float(warehouse.centroid.x),float(warehouse.centroid.y)
    c1,c2=st.columns(2)
    with c1: x=st.number_input("Operating Door X (m)",min_value=float(minx),max_value=float(maxx),value=float(st.session_state.get("door_x",default_x)),step=0.1,key="door_x")
    with c2: y=st.number_input("Operating Door Y (m)",min_value=float(miny),max_value=float(maxy),value=float(st.session_state.get("door_y",default_y)),step=0.1,key="door_y")
    return Point(x,y)

with st.sidebar:
    st.header("Project Inputs")
    cad_file=st.file_uploader("Warehouse CAD (.dxf)",type=["dxf"]); demand_file=st.file_uploader("Daily demand (.xlsx / .csv)",type=["xlsx","csv"])
    st.divider(); st.subheader("Pallet")
    pallet_width=st.number_input("Width (m)",min_value=0.1,value=1.20,step=0.05); pallet_depth=st.number_input("Depth (m)",min_value=0.1,value=1.00,step=0.05)
    st.subheader("Operational Constraints")
    wall_clearance=st.number_input("Wall clearance (m)",min_value=0.0,value=0.25,step=0.05); main_aisle=st.number_input("Main aisle (m)",min_value=0.5,value=4.0,step=0.25); cross_aisle=st.number_input("Cross aisle (m)",min_value=0.0,value=2.0,step=0.25); turning_diameter=st.number_input("Turning diameter (m)",min_value=0.0,value=7.0,step=0.25)
    st.subheader("Planning"); occupancy=st.slider("Target occupancy (%)",60,70,65); forklift_speed=st.number_input("Forklift speed (m/s)",min_value=0.1,value=2.5,step=0.1); handling_time=st.number_input("Handling time (sec/pallet)",min_value=0.0,value=0.0,step=5.0)
    run=st.button("▶ Run Digital Twin",type="primary",use_container_width=True)

if cad_file:
    key=f"{cad_file.name}:{cad_file.size}"
    if st.session_state.get("cad_preview_key")!=key:
        try:
            st.session_state.cad_path=save_uploaded(cad_file,".dxf"); st.session_state.cad_data=prepare_cad(st.session_state.cad_path); st.session_state.cad_preview_key=key
            for k in ["door_x","door_y","reference_door"]: st.session_state.pop(k,None)
        except Exception as e: st.error(f"CAD could not be read: {e}"); st.stop()

if "cad_data" in st.session_state:
    cad=st.session_state.cad_data; st.subheader("1 · CAD Review"); st.caption(f"Detected warehouse layer: **{cad['warehouse_layer']}** · Area: **{cad['warehouse'].area:,.2f} m²**"); st.pyplot(plot_cad_geometry(cad),use_container_width=True)
    st.subheader("2 · Fix Operating Door"); door=get_manual_door(cad); st.session_state.selected_door=door
    st.pyplot(plot_cad_geometry({**cad,"doors":[{"x":door.x,"y":door.y,"layer":"USER_SELECTED"}]}),use_container_width=True)
    st.caption(f"**Operating door fixed at:** X = {door.x:.3f} m, Y = {door.y:.3f} m")

if run:
    if "cad_path" not in st.session_state or not demand_file: st.error("Upload both a warehouse DXF and a daily demand file before running the Digital Twin."); st.stop()
    if "selected_door" not in st.session_state: st.error("Set the operating door coordinates before running the Digital Twin."); st.stop()
    try:
        demand_path=save_uploaded(demand_file,Path(demand_file.name).suffix.lower()); door=st.session_state.selected_door
        with st.spinner("Running CAD → layout → capacity → simulation pipeline..."):
            result=run_pipeline(st.session_state.cad_path,demand_path,manual_door_xy=(door.x,door.y),pallet_width_m=pallet_width,pallet_depth_m=pallet_depth,wall_clearance_m=wall_clearance,main_aisle_m=main_aisle,cross_aisle_m=cross_aisle,turning_diameter_m=turning_diameter,target_occupancy_pct=occupancy,forklift_speed_mps=forklift_speed,handling_time_sec_per_pallet=handling_time)
        result["config"]={"pallet_width_m":pallet_width,"pallet_depth_m":pallet_depth,"wall_clearance_m":wall_clearance,"main_aisle_m":main_aisle,"cross_aisle_m":cross_aisle,"turning_diameter_m":turning_diameter,"target_occupancy_pct":occupancy,"forklift_speed_mps":forklift_speed,"handling_time_sec_per_pallet":handling_time,"operating_door_x_m":door.x,"operating_door_y_m":door.y}
        st.session_state.result=result; st.success("Digital Twin calculation completed.")
    except Exception as e: st.error(f"The calculation could not be completed: {e}"); st.stop()

if "result" in st.session_state:
    r=st.session_state.result
    tabs=st.tabs(["Model 1 · Warehouse Layout","Model 2 · MTO Process","Downloads"])
    with tabs[0]:
        st.subheader("Decision Dashboard"); cap=r["capacity"]; c1,c2,c3,c4=st.columns(4); c1.metric("Theoretical Capacity",f"{cap['theoretical_capacity']:,}"); c2.metric("Realistic Capacity",f"{cap['realistic_capacity']:,}"); c3.metric("Planning Capacity",f"{cap['planning_capacity']:,.1f}"); c4.metric("Planning Occupancy",f"{cap['planning_occupancy_pct']:.0f}%")
        st.dataframe(r["capacity_table"],use_container_width=True,hide_index=True)
        st.subheader("Optimized Layout"); st.pyplot(plot_layout(r["warehouse"],r["storage_region"],r["slots"],r["door"],r["winner"]["layout"]["main_aisle"],r["winner"]["layout"]["turning_circle"],r["obstacles"],title="Optimized Warehouse Layout"),use_container_width=True)
        a,b,c,d=st.columns(4); a.metric("Average Slot Distance",f"{r['winner']['avg_distance_m']:.2f} m"); b.metric("Slots Generated",f"{len(r['slots']):,}"); c.metric("Detected Obstacles",f"{len(r['obstacles'])}"); d.metric("Peak Demand",f"{r['demand_metrics']['peak_demand']:.0f}")
        st.subheader("Daily Simulation"); st.dataframe(r["daily_df"],use_container_width=True,hide_index=True); s=r["simulation_summary"]; a,b,c,d=st.columns(4); a.metric("Avg Time",f"{s['average_time_min']:.2f} min"); b.metric("Avg Distance",f"{s['average_distance_m']:.2f} m"); c.metric("Avg Fatigue Proxy",f"{s['average_fatigue']:.2f}"); d.metric("Overflow Days",f"{s['overflow_days']}")
        st.subheader("Slot Distance Reference"); st.dataframe(r["slot_df"],use_container_width=True,hide_index=True)
        st.subheader("Layout Search"); st.dataframe(r["search_df"],use_container_width=True,hide_index=True)
    with tabs[1]:
        st.subheader("Model 2 · MTO Packing & Temporary WIP")
        st.caption("Partial MTO quantities are packed into one temporary box and remain in staging until completion, use or dispatch. Change assumptions to test the storage/process trade-off.")
        defaults=ProcessModelConfig()
        with st.expander("⚙ Change Model 2 assumptions",expanded=True):
            x1,x2,x3=st.columns(3)
            with x1:
                mto_share=st.number_input("MTO share (%)",0.0,100.0,float(defaults.mto_share_pct),1.0); box_qty=st.number_input("Standard box quantity",1.0,10000.0,float(defaults.standard_box_qty),1.0); belts_order=st.number_input("Avg belts / MTO order",0.1,10000.0,float(defaults.average_belts_per_mto_order),0.5); partial_rate=st.slider("Partial-box rate (%)",0,100,int(defaults.partial_box_rate_pct))
            with x2:
                current_touches=st.number_input("Current touches / box",0.0,100.0,float(defaults.current_touches_per_box),0.5); proposed_touches=st.number_input("Proposed touches / box",0.0,100.0,float(defaults.proposed_touches_per_box),0.5); error_prob=st.number_input("Error probability / touch (%)",0.0,100.0,float(defaults.error_probability_per_touch_pct),0.1); box_area=st.number_input("Box footprint (m²)",0.001,100.0,float(defaults.box_footprint_m2),0.01)
            with x3:
                cur_pack=st.number_input("Current pack time (sec)",0.0,3600.0,float(defaults.current_pack_time_sec),5.0); prop_pack=st.number_input("Proposed pack time (sec)",0.0,3600.0,float(defaults.proposed_pack_time_sec),5.0); cur_count=st.number_input("Current count time (sec)",0.0,3600.0,float(defaults.current_count_time_sec),5.0); prop_count=st.number_input("Proposed count time (sec)",0.0,3600.0,float(defaults.proposed_count_time_sec),5.0)
            y1,y2,y3=st.columns(3)
            with y1: cur_store=st.number_input("Current WIP store time (sec)",0.0,3600.0,float(defaults.current_wip_store_time_sec),5.0); prop_store=st.number_input("Proposed WIP store time (sec)",0.0,3600.0,float(defaults.proposed_wip_store_time_sec),5.0)
            with y2: cur_ret=st.number_input("Current WIP retrieve time (sec)",0.0,3600.0,float(defaults.current_wip_retrieve_time_sec),5.0); prop_ret=st.number_input("Proposed WIP retrieve time (sec)",0.0,3600.0,float(defaults.proposed_wip_retrieve_time_sec),5.0)
            with y3: cur_dwell=st.number_input("Current WIP dwell (days)",0.0,365.0,float(defaults.average_wip_dwell_days),0.5); prop_dwell=st.number_input("Proposed WIP dwell (days)",0.0,365.0,float(defaults.proposed_wip_dwell_days),0.5)
            automation=st.slider("Automation coverage (%)",0,100,int(defaults.automation_coverage_pct)); max_wip=st.number_input("Maximum temporary WIP boxes",1.0,100000.0,float(defaults.max_temporary_wip_boxes),10.0)
        cfg=ProcessModelConfig(mto_share,box_qty,belts_order,partial_rate,current_touches,proposed_touches,error_prob,cur_pack,prop_pack,cur_count,prop_count,cur_store,prop_store,cur_ret,prop_ret,cur_dwell,prop_dwell,box_area,defaults.boxes_per_pallet,defaults.boxes_per_temporary_pallet,automation,max_wip)
        try:
            p2=simulate_process_model(r["demand"],cfg); ps=process_summary(p2); paradox=storage_paradox_table(ps)
            st.subheader("Process impact"); a,b,c,d=st.columns(4); a.metric("Time reduction",f"{ps['time_reduction_pct']:.1f}%"); b.metric("Touch reduction",f"{ps['touch_reduction_pct']:.1f}%"); c.metric("Avg net storage",f"{ps['average_net_storage_change_m2']:+.2f} m²"); d.metric("Peak extra WIP",f"{ps['peak_net_storage_change_m2']:+.2f} m²")
            st.subheader("Storage Paradox — Process improves, temporary storage can increase")
            st.dataframe(paradox,use_container_width=True,hide_index=True)
            st.subheader("Daily Model 2 results"); st.dataframe(p2,use_container_width=True,hide_index=True)
        except Exception as e: st.error(f"Model 2 could not be calculated: {e}")
    with tabs[2]:
        st.subheader("Downloads")
        out=Path(tempfile.mkdtemp()); excel_path=out/"Universal_Warehouse_Digital_Twin.xlsx"; kpi=build_kpi_table(r["capacity"],r["demand_metrics"],r["simulation_summary"],r["winner"])
        export_excel(excel_path,kpi,r["capacity_table"],r["daily_df"],r["occupancy_df"],r["slot_df"],r["search_df"],r["config"],r["demand_metrics"])
        st.download_button("⬇ Download Model 1 Excel",data=excel_path.read_bytes(),file_name=excel_path.name,mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if "p2" in locals() and not p2.empty:
            p2_path=out/"Model_2_MTO_Process_Analysis.xlsx"; p2.to_excel(p2_path,index=False); st.download_button("⬇ Download Model 2 Excel",data=p2_path.read_bytes(),file_name=p2_path.name,mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if st.button("🎞 Generate Daily Simulation GIF"):
            gif_path=out/"Daily_Warehouse_Simulation.gif"; create_daily_gif(r["daily_df"],r["warehouse"],r["storage_region"],r["slots"],r["door"],r["winner"]["layout"]["main_aisle"],r["winner"]["layout"]["turning_circle"],r["obstacles"],gif_path); st.session_state.gif_bytes=gif_path.read_bytes()
        if "gif_bytes" in st.session_state: st.download_button("⬇ Download Daily Simulation GIF",data=st.session_state.gif_bytes,file_name="Daily_Warehouse_Simulation.gif",mime="image/gif")

st.divider(); st.caption("Universal Warehouse Digital Twin · Planning capacity is a planning target, not a physical capacity.")
