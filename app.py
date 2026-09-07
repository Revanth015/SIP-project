"""JK Fenner Warehouse Digital Twin — final integrated workflow."""
from pathlib import Path
import io, zipfile, tempfile
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from shapely.geometry import Point, box as sbox
from core.pipeline import prepare_cad, run_pipeline
from core.simulation_engine import SimulationConfig, simulate_daily, simulation_summary
from core.scenario_engine import prepare_movement, build_scenario_table, rank_skus, allocate_slots
from core.visualization_engine import plot_layout, plot_slot_allocation

st.set_page_config(page_title="JK Fenner Warehouse Digital Twin", page_icon="🏭", layout="wide")
st.title("🏭 JK Fenner Warehouse Digital Twin")
st.caption("CAD → door selection → Model 1 physical slots → manual study-area selection → selected-area simulation → Model 2 scenarios")

def tmp(f):
    p=tempfile.NamedTemporaryFile(delete=False,suffix=Path(f.name).suffix); p.write(f.getbuffer()); p.close(); return p.name

def reset():
    for k in list(st.session_state):
        if k.startswith(("cad_","m1_","area_","scenario_")): st.session_state.pop(k)
    st.rerun()

def cad_plot(cad,door=None,turn=None):
    fig=go.Figure()
    for e in cad.get("entities",[]):
        pts=e.get("points") or []
        if len(pts)>1: fig.add_trace(go.Scatter(x=[p[0] for p in pts],y=[p[1] for p in pts],mode="lines",line=dict(width=1),showlegend=False,hoverinfo="skip"))
    x,y=cad["warehouse"].exterior.xy; fig.add_trace(go.Scatter(x=list(x),y=list(y),mode="lines",line=dict(width=3),name="Warehouse"))
    for i,d in enumerate(cad.get("doors",[]),1): fig.add_trace(go.Scatter(x=[d["x"]],y=[d["y"]],mode="markers+text",text=[f"D{i}"],textposition="top center",marker=dict(size=14,symbol="star"),name=f"CAD Door {i}"))
    if door: fig.add_trace(go.Scatter(x=[door.x],y=[door.y],mode="markers+text",text=["OPERATING DOOR"],textposition="bottom center",marker=dict(size=15,symbol="x"),name="Operating door"))
    if turn:
        c,r=turn[0],turn[1]/2; fig.add_shape(type="circle",x0=c.x-r,x1=c.x+r,y0=c.y-r,y1=c.y+r,line=dict(dash="dot"))
    fig.update_layout(height=600,title="CAD / selected operating door",xaxis_title="X (m)",yaxis_title="Y (m)",margin=dict(l=20,r=20,t=50,b=20)); fig.update_yaxes(scaleanchor="x",scaleratio=1); return fig

def m1_plot(r,title="Model 1 — generated physical slots",selectable=False):
    fig=go.Figure(); x,y=r["warehouse"].exterior.xy; fig.add_trace(go.Scatter(x=list(x),y=list(y),mode="lines",line=dict(width=3),name="Warehouse"))
    for o in r.get("obstacles",[]):
        x,y=o.exterior.xy; fig.add_trace(go.Scatter(x=list(x),y=list(y),mode="lines",fill="toself",opacity=.2,showlegend=False))
    s=r["slot_df"]; fig.add_trace(go.Scatter(x=s.X_M,y=s.Y_M,mode="markers",marker=dict(size=6,symbol="square-open"),name="Model 1 slots",customdata=s.SLOT_ID,hovertemplate="%{customdata}<br>X=%{x:.2f}<br>Y=%{y:.2f}<extra></extra>"))
    d=r["door"]; fig.add_trace(go.Scatter(x=[d.x],y=[d.y],mode="markers+text",text=["DOOR"],textposition="top center",marker=dict(size=15,symbol="star"),name="Operating door"))
    tc=r.get("turning_center")
    if r.get("turning_enabled") and tc:
        rr=r.get("turning_diameter_m",7)/2; fig.add_shape(type="circle",x0=tc.x-rr,x1=tc.x+rr,y0=tc.y-rr,y1=tc.y+rr,line=dict(dash="dot"))
    fig.update_layout(height=650,title=title,xaxis_title="X (m)",yaxis_title="Y (m)",dragmode="select" if selectable else "zoom",margin=dict(l=20,r=20,t=50,b=20)); fig.update_yaxes(scaleanchor="x",scaleratio=1); return fig

def event_range(event):
    if event is None:return None
    try: boxes=event.selection.box
    except Exception:
        try: boxes=event["selection"]["box"]
        except Exception:return None
    if not boxes:return None
    b=boxes[-1]
    try:r=b.range
    except Exception:r=b.get("range",{})
    try:xr,yr=r["x"],r["y"]; return (min(xr),max(xr),min(yr),max(yr))
    except Exception:return None

def slots_in(df,region):
    return df[df.apply(lambda q:region.covers(Point(float(q.X_M),float(q.Y_M))),axis=1)].copy().sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)

def selected_sim(r,slots):
    n=len(slots); plan=int(n*r["config"]["target_occupancy_pct"]/100); c=r["config"]
    cfg=SimulationConfig(forklift_speed_mps=c["forklift_speed_mps"],handling_time_sec_per_pallet=c["handling_time_sec_per_pallet"],target_occupancy_pct=c["target_occupancy_pct"])
    daily,occ=simulate_daily(r["demand"],slots,n,plan,cfg); s=simulation_summary(daily); s.update(SELECTED_FEASIBLE_SLOTS=n,SELECTED_PLANNING_CAPACITY=plan,AVERAGE_STORAGE_USAGE_PCT=float(daily["STORAGE_USAGE_%"].mean()) if len(daily) else 0); return daily,s

def fig_png(fig):
    b=io.BytesIO(); fig.savefig(b,format="png",dpi=180,bbox_inches="tight"); return b.getvalue()

def make_zip(r,region,slots,sc,daily):
    b=io.BytesIO()
    with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
        dfs={"01_Model1_Full_Slot_Master.csv":r["slot_df"],"02_Selected_Area_Slot_Master.csv":slots,"03_Selected_Area_Daily_Simulation.csv":daily,"04_SKU_Movement_Master.csv":sc["sku"],"05_Model2_Scenario_Comparison.csv":sc["scenario_table"],"06_Selected_Strategy_Allocation.csv":sc["allocation"]}
        for n,d in dfs.items():z.writestr(n,d.to_csv(index=False))
        p={**r["config"],"area_xmin_m":region.bounds[0],"area_ymin_m":region.bounds[1],"area_xmax_m":region.bounds[2],"area_ymax_m":region.bounds[3],"selected_slots":len(slots),"selected_strategy":sc["strategy"],"scenario_belts_per_slot":sc["belts_per_slot"]}; z.writestr("07_Selected_Area_Parameters.csv",pd.DataFrame([p]).to_csv(index=False))
        f=plot_layout(r["warehouse"],r["storage_region"],r["slots"],r["door"],main_aisle=r["winner"]["layout"].get("main_aisle"),turning_circle=r["winner"]["layout"].get("turning_circle"),obstacles=r["obstacles"],title="Model 1 — Full Warehouse Layout"); z.writestr("PPT_GRAPHS/01_Model1_Full_Layout.png",fig_png(f))
        a=plot_layout(r["warehouse"],region,[Point(q.X_M,q.Y_M).buffer(.5) for _,q in slots.iterrows()],r["door"],obstacles=r["obstacles"],title="Selected Study Area"); z.writestr("PPT_GRAPHS/02_Selected_Area.png",fig_png(a))
        q=plot_slot_allocation(r["warehouse"],region,[Point(x.X_M,x.Y_M).buffer(.5) for _,x in slots.iterrows()],sc["allocation"],r["door"],title=f"Model 2 — {sc['strategy']} — Selected Area"); z.writestr("PPT_GRAPHS/03_Model2_Slot_Allocation.png",fig_png(q))
        z.writestr("README.txt","Model 1 is the single source of truth for physical slots. The study area is a user-selected filter on those generated slots. Model 2 does not create new physical slots.\n")
    return b.getvalue()

with st.sidebar:
    st.header("Inputs")
    cad_file=st.file_uploader("1. Warehouse CAD",type=["dxf"]); demand_file=st.file_uploader("2. Daily demand",type=["xlsx","csv"]); mto_file=st.file_uploader("3. MTO Consolidated Master",type=["xlsx","csv"]); mta_file=st.file_uploader("4. MTA Consolidated Master",type=["xlsx","csv"]); box_file=st.file_uploader("5. Size–Box Master",type=["xlsx","csv"])
    st.divider(); st.subheader("Model 1")
    pw=st.number_input("Pallet width (m)",.1,5.,1.2,.05); pd_=st.number_input("Pallet depth (m)",.1,5.,1.,.05); occ=st.slider("Planning occupancy (%)",50,85,65); wall=st.number_input("Wall clearance (m)",0.,5.,.25,.05); aisle=st.number_input("Main aisle (m)",.5,10.,4.,.25); cross=st.number_input("Cross aisle (m)",0.,10.,2.,.25); speed=st.number_input("Forklift speed (m/s)",.1,10.,2.5,.1); handle=st.number_input("Handling time (sec/pallet)",0.,600.,0.,5.)
    st.divider(); st.subheader("Model 2")
    strategy=st.selectbox("Strategy",["Frequency priority","Volume priority","Frequency × volume"]); density=st.number_input("Scenario density parameter (belts/slot)",1.,100000.,300.,50.,help="Planning scenario parameter only; not observed inventory capacity.")
    if st.button("Reset all",use_container_width=True):reset()

if cad_file:
    key=f"{cad_file.name}:{cad_file.size}"
    if st.session_state.get("cad_key")!=key:
        try:st.session_state.cad_path=tmp(cad_file); st.session_state.cad_data=prepare_cad(st.session_state.cad_path); st.session_state.cad_key=key
        except Exception as e:st.error(f"CAD read failed: {e}");st.stop()
if "cad_data" not in st.session_state:st.info("Upload the CAD to begin.");st.stop()
cad=st.session_state.cad_data; mnx,mny,mxx,mxy=cad["warehouse"].bounds; doors=cad.get("doors",[])
st.write(f"**Warehouse area:** {cad['warehouse'].area:,.1f} m²  |  **CAD doors:** {len(doors)}  |  **Obstacles:** {len(cad.get('obstacles',[]))}")

st.header("1 — Select operating door")
if doors:
    dm=st.radio("Door source",["CAD-detected door","Manual door position"],horizontal=True)
    if dm=="CAD-detected door":
        labs=[f"CAD Door {i}: ({d['x']:.2f}, {d['y']:.2f})" for i,d in enumerate(doors,1)]; ch=st.selectbox("Operating door",labs); dd=doors[labs.index(ch)]; dx,dy=float(dd["x"]),float(dd["y"]); door_source=ch
    else:
        dx=st.number_input("Door X (m)",float(mnx),float(mxx),float(cad["warehouse"].centroid.x),.1); dy=st.number_input("Door Y (m)",float(mny),float(mxy),float(cad["warehouse"].centroid.y),.1); door_source="Manual X/Y"
else:
    st.warning("No CAD door candidate detected; enter validated coordinates."); dx=st.number_input("Door X (m)",float(mnx),float(mxx),float(cad["warehouse"].centroid.x),.1); dy=st.number_input("Door Y (m)",float(mny),float(mxy),float(cad["warehouse"].centroid.y),.1); door_source="Manual X/Y"
door=Point(dx,dy); st.write(f"Selected operating door: **({dx:.3f}, {dy:.3f}) m** — {door_source}")

st.header("2 — Configure turning zone")
ton=st.checkbox("Enable turning zone",True); td=st.number_input("Turning diameter (m)",1.,20.,7.,.25,disabled=not ton); tm=st.radio("Turning-zone placement",["At operating door","Manual position"],horizontal=True,disabled=not ton)
if ton and tm=="Manual position":
    tx=st.number_input("Turning X (m)",float(mnx),float(mxx),float(dx),.1); ty=st.number_input("Turning Y (m)",float(mny),float(mxy),float(dy),.1); turn=Point(tx,ty)
elif ton:turn=door
else:turn=None
st.plotly_chart(cad_plot(cad,door,(turn,td) if ton else None),use_container_width=True)

st.header("3 — Run Model 1")
if st.button("▶ Generate physical slots",type="primary",use_container_width=True):
    if not demand_file:st.error("Upload Daily demand first.")
    else:
        try:
            with st.spinner("Generating feasible physical slots…"):
                r=run_pipeline(st.session_state.cad_path,tmp(demand_file),manual_door_xy=(dx,dy),pallet_width_m=pw,pallet_depth_m=pd_,wall_clearance_m=wall,main_aisle_m=aisle,cross_aisle_m=cross,turning_diameter_m=td,turning_enabled=ton,turning_center_xy=(turn.x,turn.y) if turn else None,target_occupancy_pct=occ,forklift_speed_mps=speed,handling_time_sec_per_pallet=handle)
            r["config"]={"pallet_width_m":pw,"pallet_depth_m":pd_,"wall_clearance_m":wall,"main_aisle_m":aisle,"cross_aisle_m":cross,"turning_enabled":ton,"turning_diameter_m":td if ton else 0,"turning_center_x_m":turn.x if turn else None,"turning_center_y_m":turn.y if turn else None,"target_occupancy_pct":occ,"forklift_speed_mps":speed,"handling_time_sec_per_pallet":handle,"operating_door_x_m":dx,"operating_door_y_m":dy,"door_source":door_source}
            st.session_state.m1_result=r; st.session_state.pop("area_confirmed",None); st.session_state.pop("scenario_result",None); st.success(f"Model 1 completed: {len(r['slot_df']):,} feasible slots.")
        except Exception as e:st.error(f"Model 1 failed: {e}")

if "m1_result" in st.session_state:
    r=st.session_state.m1_result
    st.header("4 — Select the exact study area")
    st.plotly_chart(m1_fig(r),use_container_width=True)
    st.write("Use the **Box Select** tool in the Plotly toolbar on the chart below, then click-drag around the exact area you want to study. Only existing Model 1 slots inside that rectangle will continue.")
    ev=st.plotly_chart(m1_fig(r,"SELECT STUDY AREA — Box Select",True),use_container_width=True,key="area_selector",on_select="rerun",selection_mode="box")
    rr=event_range(ev)
    if rr:
        st.session_state.area_range=rr; st.session_state.area_region=r["warehouse"].intersection(sbox(rr[0],rr[2],rr[1],rr[3])); st.session_state.area_confirmed=False
    if st.session_state.get("area_range"):
        rr=st.session_state.area_range; region=st.session_state.area_region; slots=slots_in(r["slot_df"],region)
        st.success(f"Marked area: X {rr[0]:.2f}–{rr[1]:.2f} m · Y {rr[2]:.2f}–{rr[3]:.2f} m · **{len(slots):,} Model 1 slots** inside.")
        if len(slots):
            if st.button("✅ Confirm study area",type="primary"):st.session_state.area_confirmed=True;st.session_state.area_region=region;st.session_state.area_slots=slots;st.session_state.pop("scenario_result",None)
        else:st.warning("No Model 1 slots are inside this rectangle. Select a larger area.")
    if st.session_state.get("area_confirmed"):
        region=st.session_state.area_region; slots=st.session_state.area_slots
        st.header("5 — Selected-area Model 1 simulation")
        daily,summ=selected_sim(r,slots); st.session_state.selected_daily=daily
        a,b,c=st.columns(3); a.metric("Selected Model 1 slots",len(slots)); b.metric("Selected planning capacity",summ["SELECTED_PLANNING_CAPACITY"]); c.metric("Average storage usage",f"{summ['AVERAGE_STORAGE_USAGE_PCT']:.2f}%")
        st.dataframe(pd.DataFrame([summ]),use_container_width=True); st.line_chart(daily.set_index("DATE")["STORAGE_USAGE_%"] if len(daily) else pd.Series(dtype=float))
        st.header("6 — Model 2: movement-based scenarios")
        if not all([mto_file,mta_file,box_file]):st.warning("Upload all three Model 2 files to continue.")
        elif st.button("▶ Run Model 2 scenarios",type="primary",use_container_width=True):
            try:
                with st.spinner("Building movement master and allocating only selected Model 1 slots…"):
                    sku,mto,mta,box=prepare_movement(tmp(mto_file),tmp(mta_file),tmp(box_file)); table,allocs=build_scenario_table(sku,slots,float(density)); ranked=rank_skus(sku,strategy); allocation,summary=allocate_slots(ranked,slots,float(density))
                st.session_state.scenario_result={"sku":sku,"mto":mto,"mta":mta,"box":box,"scenario_table":table,"allocations":allocs,"allocation":allocation,"summary":summary,"strategy":strategy,"belts_per_slot":float(density)}; st.success("Model 2 completed using only confirmed Model 1 slots.")
            except Exception as e:st.error(f"Model 2 failed: {e}")
        if "scenario_result" in st.session_state:
            sc=st.session_state.scenario_result; st.dataframe(sc["scenario_table"],use_container_width=True); a,b=st.columns(2); a.metric("Movement coverage",f"{sc['summary']['movement_coverage_pct']:.2f}%"); b.metric("Slot utilization",f"{sc['summary']['slot_utilization_pct']:.2f}%")
            af=plot_slot_allocation(r["warehouse"],region,[Point(q.X_M,q.Y_M).buffer(.5) for _,q in slots.iterrows()],sc["allocation"],r["door"],title=f"Model 2 — {sc['strategy']} — Selected Area"); st.pyplot(af,use_container_width=True); st.dataframe(sc["allocation"].head(1000),use_container_width=True)
            st.header("7 — Download complete outputs")
            try:st.download_button("⬇ Download ZIP — tables + PPT graphs",make_zip(r,region,slots,sc,daily),"JK_Fenner_Selected_Area_Digital_Twin_Output.zip","application/zip",type="primary",use_container_width=True)
            except Exception as e:st.error(f"Export failed: {e}")
