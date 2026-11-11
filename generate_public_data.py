"""Generate a fictional city from a fixed random seed. Reads no research input."""
from pathlib import Path
import hashlib,json
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import box,Point,LineString
from pyproj import Transformer
ROOT=Path(__file__).resolve().parent
D=ROOT/'data';(D/'layers').mkdir(parents=True,exist_ok=True);(D/'cache').mkdir(exist_ok=True)
rng=np.random.default_rng(20261111)
x0,y0=Transformer.from_crs(4326,4526,always_xy=True).transform(114.25,30.6)
width,height=9000,9000

def write(name,geometries,properties):
 frame=gpd.GeoDataFrame(properties,geometry=geometries,crs=4526)
 frame.to_file(D/(name+'.gpkg'),driver='GPKG')
 (D/'layers'/(name+'.json')).write_text(frame.to_crs(4326).to_json(drop_id=True),encoding='utf-8')
 return frame
boundary=write('boundary',[box(x0,y0,x0+width,y0+height)],{'name':['虚拟城区']})
size=40;cell=width/size
polygons=[box(x0+ix*cell,y0+iy*cell,x0+(ix+1)*cell,y0+(iy+1)*cell) for iy in range(size) for ix in range(size)]
grid=write('grid',polygons,{'grid_id':list(range(size*size))})
xy=np.column_stack([rng.uniform(250,width-250,229),rng.uniform(250,height-250,229)])+[x0,y0]
ids=[f'S{i+1:03}' for i in range(229)]
bases=write('candidates',[Point(x,y) for x,y in xy],{'id':ids,'site_id':list(range(229)),'type_name':[['示意公共设施','示意物流节点','示意服务站'][i%3] for i in range(229)],'source':['generated-public']*229,'score':rng.uniform(.5,1,229),'existing':[i%12==0 for i in range(229)]})
# Deliberately fabricated obstacles, buildings, demand locations and risk values.
buildings=[];heights=[]
for _ in range(240):
 x,y=rng.uniform(150,width-300),rng.uniform(150,height-300);w,h=rng.uniform(35,100,2)
 buildings.append(box(x0+x,y0+y,x0+x+w,y0+y+h));heights.append(int(rng.integers(3,28)))
write('buildings',buildings,{'height':heights})
write('highrise',buildings[:24],{'height':heights[:24]})
restricted=[box(x0+1800,y0+3200,x0+2200,y0+3600),box(x0+6100,y0+1600,x0+6450,y0+1950)]
write('restricted',restricted,{'YDYHFLMC':['示意避障区A','示意避障区B']})
slots=['0003','0306','0609','0912','1215','1518','1821','2124'];demands=[]
for i,slot in enumerate(slots):
 n=53 if i<4 else 52
 pts=np.column_stack([rng.uniform(120,width-120,n),rng.uniform(120,height-120,n)])+[x0,y0]
 demands.append(write('demand_'+slot,[Point(x,y) for x,y in pts],{'event_id':[f'G-{slot}-{j:03}' for j in range(n)],'grid_id':rng.integers(0,size*size,n),'risk':rng.uniform(.1,1,n),'period':[i]*n,'slot':[slot]*n}))
gpd.GeoDataFrame(pd.concat(demands,ignore_index=True),crs=4526).to_file(D/'demands.gpkg',driver='GPKG')
centroids=np.array([[g.centroid.x-x0,g.centroid.y-y0] for g in polygons]);risk=[]
for i in range(8):
 center=np.array([2400+500*np.sin(i),4200+1200*np.cos(i)])
 values=.02+rng.random(len(grid))*.08+np.exp(-np.sum((centroids-center)**2,axis=1)/(2*1500**2))*.9
 (D/'layers'/f'risk_{i}.json').write_text(json.dumps({'values':values.tolist(),'source_slot':i,'max':float(values.max()),'p95':float(np.quantile(values,.95))}),encoding='utf-8')
 risk.extend({'grid_id':j,'time_slot':i,'final_risk':float(v)} for j,v in enumerate(values))
pd.DataFrame(risk).to_csv(D/'risk.csv',index=False)
write('historical_bases',[Point(x,y) for x,y in xy[:12]],{'base_id':ids[:12],'global_keep':[1]*12})
write('historical_patrol',[LineString([xy[i],xy[i]+[300,100],xy[i]+[100,350],xy[i]]) for i in range(12)],{'base_id':ids[:12]})
write('historical_rescue',[LineString([xy[i],xy[i]+[500,250]]) for i in range(12)],{'base_id':ids[:12]})
manifest={'version':'public-'+hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12],'crs':'EPSG:4526','web_crs':'EPSG:4326','candidates':229,'grids':1600,'demands':420,'slots':slots,'baseline':{'bases':12,'drones':36,'facility_budget':18.48,'objective':None},'files':[],'notes':['公开作品集使用固定种子独立生成的虚拟城区、建筑、需求与风险值。','不包含研究原始坐标、真实设施布局、火灾记录、原始风险表或Notebook。','示意需求420个，覆盖与响应仅用于验证算法和交互，不代表真实城市消防结论。']}
for path in sorted(D.glob('*.gpkg')):
 manifest['files'].append({'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'source':'generated-public'})
(D/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'generated':manifest['version'],'candidates':229,'demands':420,'grids':1600}))
