from pathlib import Path
import os,json,sqlite3,uuid,threading,traceback,io,csv,html
from datetime import datetime
from contextlib import asynccontextmanager,contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from fastapi import FastAPI,HTTPException
from fastapi.responses import FileResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,model_validator
from .spatial import Spatial,ROOT
from .planner import Planner,Cancelled
DB=Path(os.getenv('PLANNER_DB',str(ROOT/'data/platform.sqlite3')))
for directory in (DB.parent,ROOT/'logs',ROOT/'data/cache'):directory.mkdir(parents=True,exist_ok=True)
@contextmanager
def connect():
    db=sqlite3.connect(DB,timeout=30)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
def initdb():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,created TEXT,status TEXT,note TEXT,body TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,status TEXT,progress INTEGER,message TEXT,body TEXT)')
        db.execute("UPDATE tasks SET status='failed',message='服务重启，原任务已中断，可重新计算' WHERE status IN ('queued','running')")
initdb()
state={'ready':False,'message':'正在加载空间图和路径缓存'};executor=ThreadPoolExecutor(max_workers=1);cancelled=set();spatial=None;planner=None
custom_file=ROOT/'data/custom_sites.json'
def initialize():
    global spatial,planner
    try:
        spatial=Spatial()
        if custom_file.exists():
            for x in json.loads(custom_file.read_text(encoding='utf-8')):spatial.add_base(x['lon'],x['lat'])
        planner=Planner(spatial);state.update(ready=True,message='数据就绪')
    except Exception as e:state.update(message=str(e),error=traceback.format_exc());(ROOT/'logs/startup_error.log').write_text(traceback.format_exc(),encoding='utf-8')
@asynccontextmanager
async def lifespan(app):
    executor.submit(initialize);yield
    executor.shutdown(wait=False,cancel_futures=True)
app=FastAPI(title='云枢 · 低空消防设施智能规划',lifespan=lifespan)
class Params(BaseModel):
    name:str=Field(default='中心城区规划',max_length=80)
    budget:float=Field(default=45,gt=0,le=10000)
    target:float=Field(default=90,gt=0,le=100)
    response:float=Field(default=8,gt=.1,le=120)
    speed:float=Field(default=15,ge=1,le=50)
    prep:float=Field(default=1,ge=0,le=30)
    drones_per_base:int=Field(default=3,ge=1,le=30)
    base_cost:float=Field(default=1,gt=0,le=1000)
    drone_cost:float=Field(default=.18,ge=0,le=1000)
    locked:list[str]=Field(default_factory=list)
    disabled:list[str]=Field(default_factory=list)
    strategies:list[Literal['economy','balanced','coverage']]=Field(default_factory=lambda:['economy','balanced','coverage'])
    @model_validator(mode='after')
    def valid(self):
        if set(self.locked)&set(self.disabled):raise ValueError('同一候选点不能同时锁定和禁用')
        if not self.strategies:raise ValueError('至少选择一种策略')
        self.locked=sorted(set(self.locked));self.disabled=sorted(set(self.disabled));return self
class Site(BaseModel):
    lon:float=Field(ge=-180,le=180)
    lat:float=Field(ge=-90,le=90)
class Review(BaseModel):
    status:Literal['draft','archived']='draft'
    note:str=Field(default='',max_length=4000)
def ready():
    if not state['ready']:raise HTTPException(503,state['message'])
def getplan(id):
    with connect() as db:r=db.execute('SELECT body,status,note FROM plans WHERE id=?',(id,)).fetchone()
    if not r:raise HTTPException(404,'方案不存在')
    x=json.loads(r[0]);x.update(status=r[1],note=r[2]);return x
def task_update(id,status,progress,message,body=None):
    with connect() as db:
        if body is None:db.execute('UPDATE tasks SET status=?,progress=?,message=? WHERE id=?',(status,progress,message,id))
        else:db.execute('UPDATE tasks SET status=?,progress=?,message=?,body=? WHERE id=?',(status,progress,message,json.dumps(body,ensure_ascii=False),id))
@app.get('/api/health')
def health():return state
@app.get('/api/ready')
def deployment_ready():
    if not state['ready']:raise HTTPException(503,'正在准备展示数据')
    return {'ready':True}
@app.get('/api/meta')
def meta():
    ready();return {**spatial.meta,'notes':spatial.meta['notes']+['路径为150米二维栅格避障，障碍均为程序生成的示意多边形。','覆盖分母为公开版生成的420个示意需求点；响应均值和P90统计所有有限可达需求，未达响应阈值仍参与统计。','巡检路线是风险优先的示意性优化路线；不提供实际飞行指令。'],'custom_sites':json.loads(custom_file.read_text(encoding='utf-8')) if custom_file.exists() else []}
@app.get('/api/layers/{name}')
def layer(name:str):
    if name not in ['boundary','candidates','grid','buildings','highrise','restricted','historical_bases','historical_patrol','historical_rescue']+[f'demand_{s}' for s in ['0003','0306','0609','0912','1215','1518','1821','2124']]+[f'risk_{i}' for i in range(8)]:raise HTTPException(404)
    return FileResponse(ROOT/f'data/layers/{name}.json',media_type='application/json')
@app.post('/api/sites')
def add_site(p:Site):
    ready()
    with spatial.lock:
        try:r=spatial.add_base(p.lon,p.lat)
        except ValueError as e:raise HTTPException(422,str(e))
        sites=json.loads(custom_file.read_text(encoding='utf-8')) if custom_file.exists() else [];sites.append(r);custom_file.write_text(json.dumps(sites,ensure_ascii=False),encoding='utf-8')
    return r
@app.post('/api/scenarios')
def scenario(p:Params):
    ready()
    unknown=(set(p.locked)|set(p.disabled))-set(spatial.ids)
    if unknown:raise HTTPException(422,'存在无效点位编号')
    if any(spatial.bn[spatial.ids.index(x)]<0 for x in p.locked):raise HTTPException(422,'部分锁定点位位于避障区或无法连接路径图')
    min_budget=len(p.locked)*(p.base_cost+p.drone_cost*p.drones_per_base)
    if min_budget>p.budget*(.65 if 'economy' in p.strategies else 1)+1e-8:raise HTTPException(422,'锁定点预算超过所选策略可用预算；节约策略使用65%预算')
    id=uuid.uuid4().hex[:12];body={'params':p.model_dump(),'plans':[]}
    with connect() as db:db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)',(id,'queued',0,'任务已排队',json.dumps(body)))
    executor.submit(work,id,p.model_dump());return {'id':id}
def work(id,p):
    produced=[]
    def check():
        if id in cancelled:raise Cancelled()
    try:
        check()
        with spatial.lock:
            for idx,strategy in enumerate(p['strategies']):
                check()
                def progress(n,msg):check();task_update(id,'running',int((idx*100+n)/len(p['strategies'])),msg)
                r=planner.run(p,strategy,progress,check);check();pid=uuid.uuid4().hex[:12]
                sites=[dict(id=spatial.ids[i],x=float(spatial.base_xy[i,0]),y=float(spatial.base_xy[i,1])) for i in range(len(spatial.ids))]
                x=dict(id=pid,name=p['name']+' · '+{'economy':'节约预算','balanced':'均衡配置','coverage':'覆盖优先'}[strategy],created=datetime.now().astimezone().isoformat(),strategy=strategy,params=p,data_version=spatial.meta['version'],sites=sites,**r)
                with connect() as db:db.execute('INSERT INTO plans VALUES(?,?,?,?,?)',(pid,x['created'],'draft','',json.dumps(x,ensure_ascii=False)))
                produced.append(pid)
        task_update(id,'completed',100,'方案已生成',dict(params=p,plans=produced))
    except Cancelled:task_update(id,'cancelled',0,'任务已取消，已完成方案仍保留',dict(params=p,plans=produced))
    except Exception as e:
        task_update(id,'failed',0,str(e),dict(params=p,plans=produced));(ROOT/f'logs/{id}.log').write_text(traceback.format_exc(),encoding='utf-8')
    finally:cancelled.discard(id)
@app.get('/api/tasks/{id}')
def task(id:str):
    with connect() as db:r=db.execute('SELECT status,progress,message,body FROM tasks WHERE id=?',(id,)).fetchone()
    if not r:raise HTTPException(404)
    return dict(id=id,status=r[0],progress=r[1],message=r[2],**json.loads(r[3]))
@app.post('/api/tasks/{id}/cancel')
def cancel(id:str):
    t=task(id)
    if t['status'] not in ['queued','running']:return t
    cancelled.add(id);return {'message':'已请求取消'}
@app.get('/api/plans')
def plans():
    with connect() as db:rows=db.execute('SELECT body,status,note FROM plans ORDER BY created DESC').fetchall()
    return [dict(**{k:v for k,v in json.loads(r[0]).items() if k not in ['routes','sites']},status=r[1],note=r[2]) for r in rows]
@app.get('/api/plans/{id}')
def plan(id:str):return getplan(id)
@app.patch('/api/plans/{id}')
def review(id:str,r:Review):
    getplan(id)
    with connect() as db:db.execute('UPDATE plans SET status=?,note=? WHERE id=?',(r.status,r.note,id))
    return getplan(id)
@app.get('/api/plans/{id}/marginal/{site}')
def marginal(id:str,site:str):
    ready();x=getplan(id)
    try:
        with spatial.lock:return planner.marginal(x,x['params'],site)
    except ValueError as e:raise HTTPException(422,str(e))
@app.post('/api/plans/{id}/explain')
def explain(id:str):
    x=getplan(id);text=report_text(x);key=os.getenv('PLANNER_AI_KEY');url=os.getenv('PLANNER_AI_URL');model=os.getenv('PLANNER_AI_MODEL')
    if key and url and model:
        try:
            import httpx
            payload={'model':model,'messages':[{'role':'system','content':'你是规划报告助手，仅根据给定指标解释，不得新增指标或虚构法律规范。中文输出。'},{'role':'user','content':json.dumps({'metrics':x['metrics'],'params':{k:v for k,v in x['params'].items() if k not in ['locked','disabled']},'warnings':x['warnings']},ensure_ascii=False)}]}
            r=httpx.post(url,json=payload,headers={'Authorization':'Bearer '+key},timeout=30);r.raise_for_status();text=r.json()['choices'][0]['message']['content'];return {'mode':'大模型辅助（需人工核对）','text':text}
        except Exception:return {'mode':'离线指标报告（大模型不可用，已回退）','text':text}
    return {'mode':'离线指标报告','text':text}
def report_text(x,comparisons=None):
    m=x['metrics'];p=x['params'];fmt=lambda v:'不可达' if v is None else f'{v:.2f}'
    text=f"# {x['name']}\n\n生成方式：离线指标报告\n\n数据版本：{x['data_version']}；计算时间：{x['created']}\n\n## 规划说明\n\n采用虚拟城区八时段风险与程序生成的需求，使用150米栅格避障路径、风险加权选址与固定种子遗传改进。示意建筑和示意用地含20米缓冲为二维障碍。\n\n## 核心指标\n\n- 基地：{m['bases']} 个；无人机：{m['drones']} 架\n- 标准化设施预算：{m['cost']:.2f} / {p['budget']:.2f}；基地单价 {p['base_cost']}，无人机单价 {p['drone_cost']}\n- 普通覆盖率：{m['coverage']:.2f}%；风险加权覆盖率：{m['risk_coverage']:.2f}%\n- 可达需求平均响应：{fmt(m['mean_response'])} 分钟；P90：{fmt(m['p90_response'])} 分钟\n- 不可达需求：{m['unreachable']}；未覆盖需求：{m['uncovered']}\n\n## 参数与约束\n\n响应阈值 {p['response']} 分钟，准备时间 {p['prep']} 分钟，速度 {p['speed']} 米/秒，每站 {p['drones_per_base']} 架；覆盖目标 {p['target']}%。锁定点 {len(p['locked'])} 个，禁用点 {len(p['disabled'])} 个。预算只含设施配置成本，研究转移损失和拥挤惩罚不并入设施预算。\n\n## 分期建议\n\n"
    for phase in x['phases']:text+=phase['phase']+'：'+('、'.join(phase['sites']) or '无')+'。\n\n'
    text+='分期按当前方案顺序划分，需结合点位边际贡献和实地条件人工审核，不代表独立阶段的最优解。\n\n## 适用范围\n\n'+'\n'.join('- '+w for w in x['warnings'])
    text+='\n\n覆盖分母为程序生成的示意需求；均值与P90统计所有有限可达需求。历史对比层为独立生成的示意配置，不代表任何真实研究结论。\n\n## 人工审核备注\n\n'+(x.get('note') or '暂无')
    if comparisons:
        text+='\n\n## 方案比较\n\n|方案|设施预算|风险覆盖率|平均响应|P90响应|\n|---|---:|---:|---:|---:|\n'
        for y in [x]+comparisons:
            z=y['metrics'];text+=f"|{y['name'].replace('|','/')}|{z['cost']:.2f}|{z['risk_coverage']:.2f}%|{fmt(z['mean_response'])}|{fmt(z['p90_response'])}|\n"
        text+='\n比较基于相同数据版本；响应阈值和速度等参数不同的方案应结合参数解读。'
    return text
@app.get('/api/plans/{id}/export/{fmt}')
def export(id:str,fmt:Literal['html','md','csv','geojson'],compare:str=''):
    x=getplan(id);others=[getplan(i) for i in compare.split(',') if i and i!=id][:5]
    if any(y['data_version']!=x['data_version'] for y in others):raise HTTPException(422,'数据版本不同，无法直接比较')
    text=report_text(x,others);mime='text/plain'
    if fmt=='html':
        blocks=[];in_table=False
        for line in text.splitlines():
            safe=html.escape(line)
            if line.startswith('|'):
                if not in_table:blocks.append('<table>');in_table=True
                cells=line.strip('|').split('|')
                if all(set(c.strip())<=set('-: ') for c in cells):continue
                blocks.append('<tr>'+''.join('<td>'+html.escape(c)+'</td>' for c in cells)+'</tr>');continue
            if in_table:blocks.append('</table>');in_table=False
            if line.startswith('# '):blocks.append('<h1>'+html.escape(line[2:])+'</h1>')
            elif line.startswith('## '):blocks.append('<h2>'+html.escape(line[3:])+'</h2>')
            elif line.startswith('- '):blocks.append('<p class="bullet">• '+html.escape(line[2:])+'</p>')
            elif line:blocks.append('<p>'+safe+'</p>')
        if in_table:blocks.append('</table>')
        text='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>规划报告</title><style>body{max-width:900px;margin:48px auto;padding:0 28px;font:14px/1.9 Microsoft YaHei,sans-serif;color:#354d3b}h1{font-size:26px;border-bottom:3px solid #357653;padding-bottom:24px}h2{font-size:18px;margin-top:32px;color:#28734f}table{width:100%;border-collapse:collapse;font-size:12px}td{border-bottom:1px solid #e1e7df;padding:10px}tr:first-child{background:#f2f6ee}.bullet{margin:4px 0}button{padding:10px 18px;background:#286f4b;color:white;border:0;border-radius:4px;float:right}.brand{letter-spacing:2px;color:#6a8c65;font-size:12px}@media print{button{display:none}body{margin:15mm}h2{break-after:avoid}tr{break-inside:avoid}}</style><button onclick="print()">打印 / 保存为 PDF</button><div class="brand">云枢 AEROFIRE · 规划决策报告</div>'+''.join(blocks)+'</html>';mime='text/html' 
    elif fmt=='geojson':text=json.dumps(x['routes'],ensure_ascii=False);mime='application/geo+json'
    elif fmt=='csv':
        buf=io.StringIO();w=csv.writer(buf);w.writerow(['base_id','drones','facility_cost']);w.writerows([[i,x['params']['drones_per_base'],x['params']['base_cost']+x['params']['drone_cost']*x['params']['drones_per_base']] for i in x['selected']]);text='\ufeff'+buf.getvalue();mime='text/csv'
    filename=f'{id}.{fmt}';outdir=Path(os.getenv('PLANNER_OUTPUT',str(ROOT/'outputs')));outdir.mkdir(parents=True,exist_ok=True);(outdir/filename).write_text(text,encoding='utf-8')
    return Response(text,media_type=mime,headers={'Content-Disposition':f'attachment; filename="{filename}"'})
if (ROOT/'frontend/dist').exists():app.mount('/',StaticFiles(directory=ROOT/'frontend/dist',html=True),name='frontend')
