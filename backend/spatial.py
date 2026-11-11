from pathlib import Path
import json,heapq,threading
import numpy as np
import geopandas as gpd
import shapely
from shapely.geometry import Point,LineString
from scipy.sparse import csr_matrix,save_npz,load_npz
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
ROOT=Path(__file__).resolve().parents[1]
class Spatial:
    def __init__(self):
        self.lock=threading.RLock();self.meta=json.loads((ROOT/'data/manifest.json').read_text(encoding='utf-8'))
        self.bases=gpd.read_file(ROOT/'data/candidates.gpkg');self.demands=gpd.read_file(ROOT/'data/demands.gpkg');self.grid=gpd.read_file(ROOT/'data/grid.gpkg')
        self.boundary=gpd.read_file(ROOT/'data/boundary.gpkg').geometry.union_all()
        obs=gpd.read_file(ROOT/'data/highrise.gpkg').geometry.to_list()+gpd.read_file(ROOT/'data/restricted.gpkg').geometry.to_list()
        self.obstacles=shapely.union_all(obs).buffer(20)
        self.base_xy=np.array([(g.x,g.y) for g in self.bases.geometry]);self.demand_xy=np.array([(g.x,g.y) for g in self.demands.geometry]);self.grid_xy=np.array([(g.centroid.x,g.centroid.y) for g in self.grid.geometry])
        self.weights=self.demands.risk.to_numpy(float);self.weights=np.maximum(self.weights,1e-10)
        self.slots=['0003','0306','0609','0912','1215','1518','1821','2124'];self.ids=self.bases.id.astype(str).tolist()
        shapely.prepare(self.boundary);shapely.prepare(self.obstacles)
        self.build_graph();self.load_paths()
    def build_graph(self):
        self.res=150.;x0,y0,x1,y1=self.boundary.bounds;self.origin=np.array([x0-self.res,y0-self.res]);self.w=int((x1-x0)/self.res)+3;self.h=int((y1-y0)/self.res)+3
        yy,xx=np.mgrid[:self.h,:self.w];self.xy=np.column_stack([xx.ravel(),yy.ravel()])*self.res+self.origin
        pts=shapely.points(self.xy);valid=shapely.covers(self.boundary,pts)&~shapely.intersects(self.obstacles,pts)
        self.valid=valid;self.tree=cKDTree(self.xy[valid]);self.valid_ids=np.flatnonzero(valid)
        graph_cache=ROOT/f"data/cache/graph_{self.meta['version']}_v2.npz"
        if graph_cache.exists():
            self.graph=load_npz(graph_cache);return
        rows=[];cols=[];vals=[]
        for dx,dy in [(1,0),(0,1),(1,1),(-1,1)]:
            rr=yy+dy;cc=xx+dx;inside=(rr>=0)&(rr<self.h)&(cc>=0)&(cc<self.w);a=np.flatnonzero(inside.ravel());b=(rr.ravel()[a]*self.w+cc.ravel()[a]);ok=valid[a]&valid[b];a=a[ok];b=b[ok]
            lines=shapely.linestrings(np.stack([self.xy[a],self.xy[b]],axis=1));ok=~shapely.intersects(self.obstacles,lines)&shapely.covers(self.boundary,lines);a=a[ok];b=b[ok]
            rows.extend([a,b]);cols.extend([b,a]);vals.extend([np.full(len(a),self.res*np.hypot(dx,dy))]*2)
        self.graph=csr_matrix((np.concatenate(vals),(np.concatenate(rows),np.concatenate(cols))),shape=(len(self.xy),len(self.xy)))
        save_npz(graph_cache,self.graph)
    def snap(self,points):
        dist,ix=self.tree.query(points,k=12);nodes=np.full(len(points),-1,dtype=int);offset=np.zeros(len(points))
        for i,p in enumerate(points):
            if not self.boundary.covers(Point(p)) or self.obstacles.intersects(Point(p)):continue
            for d,j in zip(dist[i],ix[i]):
                n=self.valid_ids[j]
                if d<=450 and not self.obstacles.intersects(LineString([p,self.xy[n]])) and self.boundary.covers(LineString([p,self.xy[n]])):
                    nodes[i]=n;offset[i]=d;break
        return nodes,offset
    def load_paths(self):
        self.bn,self.bo=self.snap(self.base_xy);self.dn,self.do=self.snap(self.demand_xy);self.gn,self.go=self.snap(self.grid_xy)
        cache=ROOT/f"data/cache/paths_{self.meta['version']}_v2.npz"
        if cache.exists():
            x=np.load(cache);self.dist=x['dist'];self.pred=x['pred'];self.gdist=x['gdist'];return
        self.dist=np.full((len(self.ids),len(self.dn)),np.inf);self.gdist=np.full((len(self.ids),len(self.gn)),np.inf);self.pred=np.full((len(self.ids),len(self.xy)),-9999,dtype=np.int32)
        for i,node in enumerate(self.bn):
            if node<0:continue
            dd,pr=dijkstra(self.graph,directed=False,indices=int(node),return_predecessors=True)
            self.pred[i]=pr;ok=self.dn>=0;self.dist[i,ok]=dd[self.dn[ok]]+self.bo[i]+self.do[ok];ok=self.gn>=0;self.gdist[i,ok]=dd[self.gn[ok]]+self.bo[i]+self.go[ok]
        np.savez_compressed(cache,dist=self.dist,pred=self.pred,gdist=self.gdist)
    def astar(self,start,end):
        # A* on the same collision-checked raster; used for route legs.
        if start<0 or end<0:return []
        queue=[(0.,int(start))];cost={int(start):0.};prev={};done=set()
        while queue:
            _,u=heapq.heappop(queue)
            if u in done:continue
            if u==end:
                path=[u]
                while u in prev:u=prev[u];path.append(u)
                return path[::-1]
            done.add(u)
            for k in range(self.graph.indptr[u],self.graph.indptr[u+1]):
                v=int(self.graph.indices[k]);g=cost[u]+self.graph.data[k]
                if g<cost.get(v,float('inf')):cost[v]=g;prev[v]=u;heapq.heappush(queue,(g+np.linalg.norm(self.xy[v]-self.xy[end]),v))
        return []
    def path(self,b,j):
        u=int(self.dn[j]);start=int(self.bn[b]);nodes=[]
        if not np.isfinite(self.dist[b,j]):return []
        while u!=start:
            nodes.append(u);u=int(self.pred[b,u])
            if u<0:return []
        nodes.append(start)
        return [self.base_xy[b].tolist()]+self.xy[nodes[::-1]].tolist()+[self.demand_xy[j].tolist()]
    def add_base(self,lon,lat):
        from pyproj import Transformer
        x,y=Transformer.from_crs(4326,4526,always_xy=True).transform(lon,lat);pt=Point(x,y)
        if not self.boundary.covers(pt):raise ValueError('新增点必须位于研究区域内')
        if self.obstacles.intersects(pt):raise ValueError('新增点落入高层建筑或特殊用地避障区（含20米缓冲）')
        if np.min(np.linalg.norm(self.base_xy-[x,y],axis=1))<50:raise ValueError('新增点与已有候选点距离不足50米')
        nodes,off=self.snap(np.array([[x,y]]));node=nodes[0]
        if node<0:raise ValueError('新增点无法连接可通行空间图')
        dd,pr=dijkstra(self.graph,directed=False,indices=int(node),return_predecessors=True)
        new_id='U'+str(len(self.ids)+1);self.ids.append(new_id);self.base_xy=np.vstack([self.base_xy,[x,y]]);self.bn=np.append(self.bn,node);self.bo=np.append(self.bo,off[0]);self.pred=np.vstack([self.pred,pr])
        for attr,n,offset in [('dist',self.dn,self.do),('gdist',self.gn,self.go)]:
            row=np.full(len(n),np.inf);ok=n>=0;row[ok]=dd[n[ok]]+off[0]+offset[ok];setattr(self,attr,np.vstack([getattr(self,attr),row]))
        return {'id':new_id,'lon':lon,'lat':lat,'type_name':'自定义候选点','existing':False}
