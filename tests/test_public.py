"""Run against the public data only. No private project inputs are read."""
from pathlib import Path
import os,sys,time,unittest,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['PLANNER_DB']=str(ROOT/'tests/public-check.sqlite3')
os.environ['PLANNER_OUTPUT']=str(ROOT/'outputs')
from fastapi.testclient import TestClient
from backend.app import app,Params
class PublicTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.client=TestClient(app);cls.client.__enter__()
  for _ in range(120):
   if cls.client.get('/api/health').json()['ready']:break
   time.sleep(.25)
  assert cls.client.get('/api/health').json()['ready']
 @classmethod
 def tearDownClass(cls):cls.client.__exit__(None,None,None)
 def test_public_provenance(self):
  meta=self.client.get('/api/meta').json();self.assertTrue(meta['version'].startswith('public-'));self.assertEqual(meta['grids'],1600);self.assertEqual(meta['demands'],420)
  self.assertTrue(all(r['source']=='generated-public' for r in meta['files']))
  for name in ('boundary','candidates','grid','buildings','restricted','risk_0','demand_0003'):self.assertEqual(self.client.get('/api/layers/'+name).status_code,200)
  for path in ('/data/source/','/data/platform.sqlite3','/.env','/research/','/backend/app.py'):self.assertEqual(self.client.get(path).status_code,404)
 def test_complete_planning(self):
  task=self.client.post('/api/scenarios',json=Params(strategies=['balanced']).model_dump());self.assertEqual(task.status_code,200)
  for _ in range(300):
   result=self.client.get('/api/tasks/'+task.json()['id']).json()
   if result['status'] not in ('queued','running'):break
   time.sleep(.1)
  self.assertEqual(result['status'],'completed',result);plan=self.client.get('/api/plans/'+result['plans'][0]).json();self.assertGreater(plan['metrics']['bases'],0);self.assertLessEqual(plan['metrics']['cost'],45);self.assertEqual(len(plan['metrics']['slots']),8)
  self.assertEqual(self.client.get('/api/plans/'+plan['id']+'/export/html').status_code,200)
  self.assertEqual(self.client.get('/api/plans/'+plan['id']+'/marginal/'+plan['selected'][0]).status_code,200)
 def test_frontend(self):self.assertEqual(self.client.get('/').status_code,200)
if __name__=='__main__':unittest.main(verbosity=2)
