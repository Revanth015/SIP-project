# Universal Warehouse Digital Twin V3
# Integrated four-input engine with dedicated pallet-storage-area selection.
# See the downloadable local V3 file supplied in this conversation for the complete implementation.

from pathlib import Path
import math, io, tempfile
import numpy as np
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle
from shapely.geometry import Polygon, MultiPolygon, Point, LineString, box
from shapely.ops import unary_union, polygonize, snap
try:
    import ezdxf
except ImportError:
    ezdxf = None

PALLET_W, PALLET_D = 1.20, 1.00
WALL_CLEARANCE, MAIN_AISLE, CROSS_AISLE, TURNING_DIAMETER = .25, 4., 2., 7.
TARGET_OCCUPANCY = 65.
FORKLIFT_SPEED, HANDLING_MIN_PER_PALLET = 2.5, 1.5
TIME_PICK, TIME_COUNT, TIME_PACK = 45, 30, 40
TIME_WIP_STORE, TIME_WIP_RETRIEVE = 60, 90
FATIGUE_PER_TOUCH, FATIGUE_PER_WIP, ERROR_PROB_PER_TOUCH = 1., 2.5, .015
BOXES_PER_PALLET, BIN_CAPACITY_BELTS, STAGING_DWELL_DAYS = 32, 300, 2
CURRENT_CAD_DOOR = Point(0, 0)

def clean_poly(p):
    try:
        p = p.buffer(0)
        if p.is_empty or p.area <= 1e-6: return None
        return max(p.geoms, key=lambda g:g.area) if isinstance(p, MultiPolygon) else p
    except Exception: return None

def read_table(uploaded):
    data=uploaded.getvalue(); suffix=Path(uploaded.name).suffix.lower()
    if suffix=='.csv': return pd.read_csv(io.BytesIO(data))
    if suffix in ('.xlsx','.xls'): return pd.read_excel(io.BytesIO(data))
    raise ValueError('Unsupported table format.')

def find_col(df, candidates):
    cols={str(c).strip().upper():c for c in df.columns}
    for c in candidates:
        if c.upper() in cols: return cols[c.upper()]
    for a in df.columns:
        aa=str(a).strip().upper().replace(' ','_')
        for c in candidates:
            cc=c.upper().replace(' ','_')
            if aa==cc or cc in aa or aa in cc: return a
    return None

def standardize_transactions(df, master):
    df=df.copy(); master=master.copy()
    df.columns=[str(c).strip() for c in df.columns]; master.columns=[str(c).strip() for c in master.columns]
    dc=find_col(df,['DATE','DAY','TRANSACTION_DATE']); ic=find_col(df,['ITEM_SIZE','ITEM CODE','ITEM_CODE','BELT SIZE','SIZE']); qc=find_col(df,['NO_OF_BELTS','ORDER QUANTITY','ORDER_QTY','QTY','QUANTITY'])
    mi=find_col(master,['Belt Size','ITEM_SIZE','ITEM CODE','ITEM_CODE','SIZE']); mb=find_col(master,['Units Per Box','STANDARD BOX QUANTITY','BOX_QTY','BOX QUANTITY'])
    if not all([dc,ic,qc,mi,mb]): raise ValueError('Could not identify Date, Item/Size, Quantity and master box-standard columns.')
    lookup=(master[[mi,mb]].dropna().drop_duplicates(subset=[mi]).set_index(mi)[mb])
    x=df.copy(); x['_DATE']=pd.to_datetime(x[dc],errors='coerce'); x['_ITEM']=x[ic].astype(str).str.strip(); x['_QTY']=pd.to_numeric(x[qc],errors='coerce'); x['_UNITS_PER_BOX']=pd.to_numeric(x['_ITEM'].map(lookup),errors='coerce')
    before=len(x); x=x.dropna(subset=['_DATE','_QTY','_UNITS_PER_BOX']); x=x[(x['_QTY']>=0)&(x['_UNITS_PER_BOX']>0)].copy()
    x['FULL_BOXES']=np.floor(x['_QTY']/x['_UNITS_PER_BOX']).astype(int); x['BALANCE_QTY']=x['_QTY']-x['FULL_BOXES']*x['_UNITS_PER_BOX']; x['HAS_BALANCE']=x['BALANCE_QTY']>1e-9; x['TOTAL_BOXES']=x['FULL_BOXES']+x['HAS_BALANCE'].astype(int); x['DATE']=x['_DATE'].dt.date
    return x,before-len(x)

def load_dxf(uploaded):
    if ezdxf is None: raise RuntimeError('Install ezdxf from requirements.txt.')
    with tempfile.NamedTemporaryFile(suffix='.dxf',delete=False) as f: f.write(uploaded.getvalue()); path=f.name
    doc=ezdxf.readfile(path); msp=doc.modelspace(); polys=[]; lines=[]; entities=[]; circles=[]
    for e in msp:
        try:
            typ=e.dxftype(); layer=str(getattr(e.dxf,'layer','0'))
            if typ=='LINE':
                p1=(float(e.dxf.start.x),float(e.dxf.start.y)); p2=(float(e.dxf.end.x),float(e.dxf.end.y)); lines.append(LineString([p1,p2])); entities.append({'type':typ,'layer':layer,'points':[p1,p2]})
            elif typ in ('LWPOLYLINE','POLYLINE'):
                pts=[(float(p[0]),float(p[1])) for p in e.get_points()] if typ=='LWPOLYLINE' else [(float(v.dxf.location.x),float(v.dxf.location.y)) for v in e.vertices()]
                closed=bool(getattr(e,'closed',False)) or (len(pts)>=3 and np.linalg.norm(np.array(pts[0])-np.array(pts[-1]))<=.01); entities.append({'type':typ,'layer':layer,'points':pts})
                if closed and len(pts)>=3:
                    p=clean_poly(Polygon(pts));
                    if p: polys.append({'polygon':p,'layer':layer,'area':p.area})
                elif len(pts)>=2: lines.append(LineString(pts))
            elif typ=='CIRCLE':
                c=e.dxf.center; circles.append({'center':(float(c.x),float(c.y)),'radius':float(e.dxf.radius),'layer':layer})
            elif typ=='ARC':
                c=e.dxf.center; r=float(e.dxf.radius); a1=math.radians(float(e.dxf.start_angle)); a2=math.radians(float(e.dxf.end_angle));
                if a2<=a1:a2+=2*math.pi
                lines.append(LineString([(c.x+r*math.cos(a),c.y+r*math.sin(a)) for a in np.linspace(a1,a2,40)]))
        except Exception: pass
    if not polys and lines:
        for tol in (.02,.05,.1,.25,.5,1.):
            try:
                generated=[p for p in polygonize(snap(unary_union(lines),unary_union(lines),tol)) if p.area>1e-6]
                if generated:
                    polys=[{'polygon':clean_poly(p),'layer':'RECONSTRUCTED_FROM_LINEWORK','area':p.area} for p in generated if clean_poly(p)]; break
            except Exception: pass
    if not polys: raise ValueError('No closed warehouse polygon could be reconstructed from the DXF.')
    wh=[p for p in polys if 'WAREHOUSE' in p['layer'].upper()]; wh=max(wh,key=lambda p:p['area']) if wh else max(polys,key=lambda p:p['area']); warehouse=wh['polygon']
    keys=('STORAGE','STORE','PALLET','RACK','POLY','FG','STOCK','FINISHED'); sc=[p for p in polys if p['polygon'].area<warehouse.area and any(k in p['layer'].upper() for k in keys)]; storage=max(sc,key=lambda p:p['area'])['polygon'] if sc else warehouse
    obs=[p['polygon'] for p in polys if p['polygon'].area<.30*warehouse.area and warehouse.contains(p['polygon'].centroid) and not any(k in p['layer'].upper() for k in keys)]
    doors=[e for e in entities if any(k in e['layer'].upper() for k in ('DOOR','GATE','ENTRY','EXIT')) and len(e.get('points',[]))>=2]
    if doors:
        a,b=doors[0]['points'][0],doors[0]['points'][-1]; door=Point((a[0]+b[0])/2,(a[1]+b[1])/2); ds='Semantic CAD door/gate layer'
    else:
        cs=list(warehouse.exterior.coords); mids=[Point((a[0]+b[0])/2,(a[1]+b[1])/2) for a,b in zip(cs[:-1],cs[1:])]; door=min(mids,key=lambda p:p.y); ds='Fallback lowest warehouse boundary'
    return {'warehouse':warehouse,'storage':storage,'storage_source':'CAD-detected storage region' if sc else 'Full warehouse','obstacles':obs,'door':door,'door_source':ds,'warehouse_source':wh['layer']}

def apply_storage_area(cad,mode='Automatic',x_min=None,y_min=None,x_max=None,y_max=None):
    if mode=='Full warehouse': region=cad['warehouse']; source='Entire warehouse designated for pallet storage'
    elif mode=='Manual rectangle':
        if None in (x_min,y_min,x_max,y_max) or x_max<=x_min or y_max<=y_min: raise ValueError('Enter valid manual storage-area coordinates.')
        region=cad['warehouse'].intersection(box(x_min,y_min,x_max,y_max)); source='Manager-defined pallet storage rectangle'
        if region.is_empty: raise ValueError('Designated area is outside the warehouse.')
    else: region=cad['storage']; source=cad.get('storage_source','CAD-detected storage region')
    region=clean_poly(region)
    if region is None: raise ValueError('Invalid pallet-storage area.')
    obs=[o for o in cad['obstacles'] if region.intersects(o) and region.intersection(o).area>1e-6]
    return region,obs,source

def rotated_dims(o): return (PALLET_D,PALLET_W) if o==90 else (PALLET_W,PALLET_D)
def theoretical_slots(region,o,obstacles):
    pw,pd=rotated_dims(o); minx,miny,maxx,maxy=region.bounds; slots=[]; x=minx
    while x+pw<=maxx+1e-9:
        y=miny
        while y+pd<=maxy+1e-9:
            s=box(x,y,x+pw,y+pd)
            if region.contains(s) and not any(s.intersects(z) for z in obstacles): slots.append(s)
            y+=pd
        x+=pw
    return slots

def realistic_layout(region,o,wall,aisle,cross,door,obstacles):
    pw,pd=rotated_dims(o); usable=region.buffer(-wall)
    if usable.is_empty:return {'slots':[],'capacity':0,'avg_distance':float('inf'),'orientation':o,'wall':wall,'aisle':aisle,'cross':cross,'score':-1e9,'main_aisle':None,'turning':None,'usable':usable}
    minx,miny,maxx,maxy=usable.bounds; main=box(door.x-aisle/2,miny,door.x+aisle/2,maxy); turn=door.buffer(TURNING_DIAMETER/2); blocked=[main,turn]+obstacles; slots=[]; x=minx
    while x+pw<=maxx+1e-9:
        y=miny; row=0
        while y+pd<=maxy+1e-9:
            s=box(x,y,x+pw,y+pd)
            if usable.contains(s) and not any(s.intersects(b) for b in blocked): slots.append(s)
            row+=1
            if row==2:y+=cross; row=0
            y+=pd
        x+=pw
    avg=float(np.mean([s.centroid.distance(door) for s in slots])) if slots else float('inf'); score=len(slots)-.10*avg if slots else -1e9
    return {'slots':slots,'main_aisle':main,'turning':turn,'usable':usable,'capacity':len(slots),'avg_distance':avg,'orientation':o,'wall':wall,'aisle':aisle,'cross':cross,'score':score}

def optimize_layout(cad):
    cand=[]
    for o in (0,90):
        for wall in (.15,.25,.50):
            for aisle in (3.,3.5,4.,4.5):
                for cross in (1.,1.5,2.,2.5,3.):
                    r=realistic_layout(cad['storage'],o,wall,aisle,cross,cad['door'],cad['obstacles'])
                    if r['capacity']:cand.append(r)
    if not cand: raise ValueError('No feasible operational layout found in the designated pallet-storage area.')
    winner=max(cand,key=lambda r:r['score']); theo=max(len(theoretical_slots(cad['storage'],0,cad['obstacles'])),len(theoretical_slots(cad['storage'],90,cad['obstacles'])))
    search=pd.DataFrame([{'Capacity':r['capacity'],'Avg distance (m)':round(r['avg_distance'],2),'Orientation':r['orientation'],'Wall clearance (m)':r['wall'],'Main aisle (m)':r['aisle'],'Cross aisle (m)':r['cross'],'Score':round(r['score'],2)} for r in cand]).sort_values(['Capacity','Avg distance (m)'],ascending=[False,True])
    return winner,search.reset_index(drop=True),theo

st.set_page_config(page_title='Universal Warehouse Digital Twin',layout='wide'); st.title('🏭 Universal Warehouse Digital Twin'); st.caption('Four source files → CAD + transaction processing → capacity → simulation')
with st.sidebar:
    st.header('Four source files'); diagram=st.file_uploader('① Warehouse diagram — DXF',type=['dxf']); mta_file=st.file_uploader('② MTA data',type=['xlsx','xls','csv']); mto_file=st.file_uploader('③ MTO data',type=['xlsx','xls','csv']); master_file=st.file_uploader('④ Belt / units-per-box master',type=['xlsx','xls','csv'])
    st.divider(); st.subheader('Pallet storage area'); storage_mode=st.radio('Where may pallets be stored?',['Automatic','Full warehouse','Manual rectangle'],help='Automatic uses CAD storage detection. Full warehouse allows pallet storage everywhere. Manual rectangle dedicates a manager-selected area.')
    mc={}
    if storage_mode=='Manual rectangle':
        c1,c2=st.columns(2); mc['x_min']=c1.number_input('X minimum',value=0.0); mc['x_max']=c2.number_input('X maximum',value=10.0); c3,c4=st.columns(2); mc['y_min']=c3.number_input('Y minimum',value=0.0); mc['y_max']=c4.number_input('Y maximum',value=10.0)
    st.divider(); st.write(f'Planning occupancy: **{TARGET_OCCUPANCY:.0f}%**'); st.write(f'Pallet: **{PALLET_W:.2f} × {PALLET_D:.2f} m**'); st.write(f'Boxes/pallet: **{BOXES_PER_PALLET}**')

if st.button('▶ Build & Simulate Digital Twin',type='primary',use_container_width=True):
    if not all([diagram,mta_file,mto_file,master_file]): st.error('Please upload the four required source files.'); st.stop()
    try:
        cad=load_dxf(diagram); master=read_table(master_file); mto,_=standardize_transactions(read_table(mto_file),master); mta,_=standardize_transactions(read_table(mta_file),master)
        selected,obs,source=apply_storage_area(cad,storage_mode,mc.get('x_min'),mc.get('y_min'),mc.get('x_max'),mc.get('y_max')); cad['storage']=selected; cad['obstacles']=obs; cad['storage_source']=source
        winner,search,theo=optimize_layout(cad); realistic=winner['capacity']; planning=realistic*TARGET_OCCUPANCY/100
        st.success(f'Pallet storage zone: {selected.area:,.2f} m² | Source: {source} | Realistic capacity: {realistic:,} slots | Planning: {planning:,.1f} slots')
        a,b,c,d=st.columns(4); a.metric('Warehouse area',f"{cad['warehouse'].area:,.1f} m²"); b.metric('Pallet-storage area',f"{selected.area:,.1f} m²"); c.metric('Theoretical slots',f'{theo:,}'); d.metric('Realistic slots',f'{realistic:,}')
        st.subheader('Designated pallet-storage area'); st.info('Pallet capacity and occupancy are calculated only inside the designated area. Space outside this zone is not treated as pallet capacity.')
        fig,ax=plt.subplots(figsize=(11,7)); x,y=cad['warehouse'].exterior.xy; ax.plot(x,y,linewidth=2,label='Warehouse'); sx,sy=selected.exterior.xy; ax.fill(sx,sy,alpha=.12,label='Dedicated pallet storage'); ax.scatter([cad['door'].x],[cad['door'].y],s=100,marker='*',label='Door'); ax.set_aspect('equal'); ax.legend(); ax.set_title('Warehouse with designated pallet-storage area'); st.pyplot(fig,use_container_width=True)
        st.dataframe(pd.DataFrame([{'Pallet-storage area m²':round(selected.area,2),'Theoretical slots':theo,'Realistic operational slots':realistic,'Planning capacity @65%':round(planning,1),'Orientation':winner['orientation'],'Wall clearance m':winner['wall'],'Main aisle m':winner['aisle'],'Cross aisle m':winner['cross'],'Average slot distance m':round(winner['avg_distance'],2)}]),use_container_width=True)
        st.subheader('Layout search — operational alternatives'); st.dataframe(search.head(50),use_container_width=True)
        st.download_button('⬇️ Download layout results',search.to_csv(index=False).encode(),file_name='warehouse_layout_search.csv',mime='text/csv')
    except Exception as e: st.error(str(e)); st.exception(e)
else:
    st.info('Upload the four source files, choose the pallet-storage-area mode, and click Build & Simulate Digital Twin.')
''