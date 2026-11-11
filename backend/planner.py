import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString,mapping
from .spatial import Spatial
class Cancelled(Exception):pass
class Planner:
    def __init__(self,s):self.s=s;self.transform=Transformer.from_crs(4526,4326,always_xy=True)
    def matrix(self,p):return self.s.dist/(p['speed']*60)+p['prep']
    def metrics(self,selected,p):
        times=self.matrix(p);n=len(self.s.dn)
        best=times[selected].min(axis=0) if selected else np.full(n,np.inf)
        covered=best<=p['response'];finite=np.isfinite(best);w=self.s.weights
        slots=[]
        for slot in self.s.slots:
            mask=self.s.demands.slot.eq(slot).to_numpy();ok=finite&mask
            slots.append(dict(slot=slot,coverage=float(covered[mask].mean()*100),risk_coverage=float(w[covered&mask].sum()/w[mask].sum()*100),mean_response=float(best[ok].mean()) if ok.any() else None,p90_response=float(np.percentile(best[ok],90)) if ok.any() else None,unreachable=int((mask&~finite).sum())))
        return dict(bases=len(selected),drones=len(selected)*p['drones_per_base'],cost=round(len(selected)*(p['base_cost']+p['drone_cost']*p['drones_per_base']),6),coverage=float(covered.mean()*100),risk_coverage=float(w[covered].sum()/w.sum()*100),mean_response=float(best[finite].mean()) if finite.any() else None,p90_response=float(np.percentile(best[finite],90)) if finite.any() else None,unreachable=int((~finite).sum()),uncovered=int((~covered).sum()),slots=slots)
    def choose(self,p,strategy,check=lambda:None):
        ids=self.s.ids;locked=[ids.index(x) for x in p['locked']];disabled=set(p['disabled']);unit=p['base_cost']+p['drone_cost']*p['drones_per_base'];limit=int((p['budget']*(.65 if strategy=='economy' else 1)+1e-9)//unit)
        if len(locked)>limit:raise ValueError('锁定点所需预算超过该策略可用预算，请提高预算或解除锁定')
        pool=[i for i,x in enumerate(ids) if x not in disabled and self.s.bn[i]>=0];selected=locked[:]
        t=self.matrix(p);cover=t<=p['response'];w=self.s.weights;w=w/w.sum();goal=p['target']/100*(.95 if strategy=='economy' else 1.)
        best=t[selected].min(axis=0) if selected else np.full(t.shape[1],np.inf)
        for _ in range(limit-len(selected)):
            check();available=[i for i in pool if i not in selected]
            if not available:break
            current=best<=p['response'];gain=(cover[available]&~current).dot(w)
            improvement=np.sum(np.maximum(0,np.minimum(best,p['response']*3)-np.minimum(t[available],p['response']*3))*w,axis=1)/(p['response']*3)
            score=gain+(.20 if strategy=='coverage' else .08)*improvement
            pick=int(np.argmax(score));i=available[pick]
            if score[pick]<1e-10:break
            selected.append(i);best=np.minimum(best,t[i])
            if strategy!='coverage' and w[best<=p['response']].sum()>=goal:break
        # Deterministic genetic refinement, inherited selection idea from rescue GA notebook.
        rng=np.random.default_rng(42);fixed=set(locked);size=len(selected)
        def fitness(a):
            z=t[list(a)].min(axis=0) if a else np.full(t.shape[1],np.inf)
            return float(w[z<=p['response']].sum()-.08*np.sum(np.minimum(z,p['response']*3)*w)/(p['response']*3))
        pop=[set(selected)];bestscore=fitness(pop[0])
        if size>len(fixed) and len(pool)>size:
            for gen in range(12):
                check();children=[]
                for _ in range(12):
                    a=set(pop[int(rng.integers(len(pop)))]);rem=list(a-fixed);add=[i for i in pool if i not in a]
                    if rem and add:a.remove(rem[int(rng.integers(len(rem)))]);a.add(add[int(rng.integers(len(add)))])
                    children.append(a)
                pop=sorted(pop+children,key=fitness,reverse=True)[:6]
            if fitness(pop[0])>bestscore:selected=sorted(pop[0])
        return sorted(selected)
    def feature(self,coords,kind,slot,base_id):
        lon,lat=self.transform.transform(*np.array(coords).T);return dict(type='Feature',geometry={'type':'LineString','coordinates':np.column_stack([lon,lat]).tolist()},properties=dict(kind=kind,slot=slot,base_id=base_id,length_m=float(LineString(coords).length)))
    def routes(self,selected,p,check):
        features=[];times=self.matrix(p)
        if not selected:return {'type':'FeatureCollection','features':[]}
        assignments=np.argmin(times[selected],axis=0)
        for j in range(len(self.s.dn)):
            check();b=selected[assignments[j]]
            coords=self.s.path(b,j)
            if coords:features.append(self.feature(coords,'rescue',self.s.demands.slot.iloc[j],self.s.ids[b]))
        # Risk-driven greedy inspection tours, full graph paths, closed at the base.
        import pandas as pd
        risk=pd.read_csv(self.s.meta_path if hasattr(self.s,'meta_path') else __import__('pathlib').Path(__file__).resolve().parents[1]/'data/risk.csv')
        for slotno,slot in enumerate(self.s.slots):
            values=self.s.grid.grid_id.map(risk[risk.time_slot==sorted(risk.time_slot.unique())[slotno]].set_index('grid_id').final_risk).fillna(0).to_numpy()
            assign=np.argmin(self.s.gdist[selected],axis=0)
            for k,b in enumerate(selected):
                check();options=np.flatnonzero((assign==k)&np.isfinite(self.s.gdist[b])&(values>0));options=options[np.argsort(values[options]/(self.s.gdist[b,options]+150))[::-1]][:12]
                node=int(self.s.bn[b]);path=[node];length=2*self.s.bo[b];visited=0
                for j in options:
                    target=int(self.s.gn[j]);leg=self.s.astar(node,target)
                    if not leg:continue
                    step=float(np.linalg.norm(np.diff(self.s.xy[leg],axis=0),axis=1).sum());home=float(self.s.gdist[b,j])
                    if length+step+home>40*60*p['speed']:continue
                    path+=leg[1:];length+=step;node=target;visited+=1
                    if visited>=5:break
                if visited:
                    home=self.s.astar(node,int(self.s.bn[b]));path+=home[1:];coords=[self.s.base_xy[b].tolist()]+self.s.xy[path].tolist()+[self.s.base_xy[b].tolist()]
                    f=self.feature(coords,'patrol',slot,self.s.ids[b]);f['properties']['visited_grids']=visited;features.append(f)
        return dict(type='FeatureCollection',features=features)
    def run(self,p,strategy,progress,check):
        progress(10,'正在优化选址');selected=self.choose(p,strategy,check);m=self.metrics(selected,p);progress(45,'正在生成避障救援与巡检路线');routes=self.routes(selected,p,check);progress(85,'正在计算预算联动曲线')
        curve=[]
        for ratio in [.35,.5,.65,.8,1.]:
            check();q={**p,'budget':round(p['budget']*ratio,2)}
            try:a=self.choose(q,'coverage',check);z=self.metrics(a,q);curve.append(dict(budget=q['budget'],coverage=z['risk_coverage'],response=z['mean_response'],cost=z['cost']))
            except ValueError:continue
        warnings=[]
        if m['risk_coverage']+1e-6<p['target']:warnings.append('当前预算与空间约束下未达到目标风险覆盖率；该结果为启发式方案，不是不可行性的数学证明。')
        if m['unreachable']:warnings.append(f"存在 {m['unreachable']} 个需求无法连接避障空间图，已计入未覆盖需求。")
        warnings.append('响应为静态飞行可达性估算，未模拟并发排队、天气及实时空域；巡检是40分钟航程约束下的风险优先路线。')
        return dict(selected=[self.s.ids[i] for i in selected],metrics=m,routes=routes,curve=curve,warnings=warnings,phases=[dict(phase='一期优先',sites=[self.s.ids[i] for i in selected[:max(1,len(selected)//3)]]),dict(phase='二期完善',sites=[self.s.ids[i] for i in selected[max(1,len(selected)//3):]])])
    def marginal(self,result,p,site_id):
        if site_id not in self.s.ids:raise ValueError('点位不存在')
        selected=[self.s.ids.index(x) for x in result['selected']];i=self.s.ids.index(site_id);is_selected=i in selected;other=[x for x in selected if x!=i] if is_selected else selected+[i]
        before=self.metrics(selected,p);after=self.metrics(other,p)
        return dict(site_id=site_id,action='移除' if is_selected else '加入',method='固定其他点位的局部评估',delta_coverage=after['risk_coverage']-before['risk_coverage'],delta_response=(after['mean_response']-before['mean_response']) if after['mean_response'] is not None and before['mean_response'] is not None else None,delta_cost=after['cost']-before['cost'],after=after,exceeds_budget=after['cost']>p['budget'])
