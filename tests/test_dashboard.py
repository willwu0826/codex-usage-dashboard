"""使用纯合成数据验证看板口径、归属、偏好及本地接口。"""
import atexit
import json
from pathlib import Path
import sys
import threading
import unittest
import tempfile
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent/'02_实现'))
ARTIFACTS = TESTS / '.artifacts'
ARTIFACTS.mkdir(exist_ok=True)
TEMP = tempfile.TemporaryDirectory(prefix='看板-', dir=ARTIFACTS)
atexit.register(TEMP.cleanup)
BASE = Path(TEMP.name)
from usage_core import Ledger, stamp
from server import make_handler

NOW=stamp('2026-10-03T10:00:00Z')
CAT={'one':{'id':'one','name':'one','path':r'D:\one'}}
def fixture(name):
    root=BASE/'validation_fixture'/name
    ledger=Ledger(root,codex_home=root/'empty-codex',workspace=root/'workspace',registry=root/'none.json')
    ledger.catalog=lambda:(CAT,{'parent':{'one'}})
    ledger.snapshot={'threads':{'parent':{'title':'Parent'},'child':{'title':'Child'},'other':{'title':'Other'}},
        'parents':{'child':'parent'},'events':[
            {'thread':'parent','when':NOW-864000,'model':'model-a','input':80,'cached':60,'output':20,'reasoning':10,'total':100,'time_unknown':True},
            {'thread':'child','when':NOW-50,'model':'model-b','input':40,'cached':30,'output':10,'reasoning':5,'total':50,'time_unknown':False},
            {'thread':'other','when':NOW-30,'model':'unknown','input':50,'cached':0,'output':0,'reasoning':0,'total':50,'time_unknown':False}],
        'quotas':[dict(limit_id='codex',sampled=NOW-10,resets_at=NOW+100,used_percent=25,window_minutes=10080)],'scan':{}}
    return ledger

class V2(unittest.TestCase):
    def test_lifetime_stable_and_unknown_dates_excluded_from_week(self):
        ledger=fixture('history')
        week,all_=ledger.report('week',NOW),ledger.report('all',NOW)
        self.assertEqual(week['lifetime'],all_['lifetime'])
        self.assertEqual(week['lifetime']['one']['total'],150)
        self.assertEqual(week['lifetime']['one']['undated_tokens'],100)
        self.assertEqual(week['totals']['total'],100)
        self.assertEqual(all_['totals']['total'],200)
        project=next(p for p in all_['projects'] if p['id']=='one')
        self.assertEqual(sum(v['total'] for v in project['daily']),50)
        self.assertEqual(sum(v['total'] for v in project['models']),150)
        self.assertEqual(project['undated_tokens'],100)

    def test_synthetic_parent_and_share_never_quota(self):
        week=fixture('parent').report('week',NOW)
        parent=next(t for t in week['threads'] if t['id']=='parent')
        self.assertEqual((parent['total'],parent['including_agents_total']),(0,50))
        self.assertEqual(week['totals']['agent_tokens'],50)
        self.assertEqual(sum(p['token_share'] for p in week['projects']),1)
        self.assertTrue(all(p['quota_estimate_pp'] is None for p in week['projects']))
        json.dumps(week)

    def test_mixed_models_not_reported_as_first_only(self):
        ledger=fixture('models')
        ledger.snapshot['events'].append({**ledger.snapshot['events'][0],'when':NOW-20,'model':'model-c','time_unknown':False})
        report=ledger.report('all',NOW)
        parent=next(t for t in report['threads'] if t['id']=='parent')
        self.assertEqual(parent['model'],'model-a、model-c')
        self.assertEqual(parent['including_agents_total'],250)

    def test_expired_or_mismatched_window_not_relabelled(self):
        ledger=fixture('window')
        expired=ledger.report('week',NOW+200)
        self.assertIsNone(expired['period']['start'])
        self.assertEqual(expired['projects'],[])
        self.assertEqual(expired['lifetime']['one']['total'],150)
        ledger.snapshot['quotas'][0]['sampled']=NOW-700000
        self.assertIsNone(ledger.report('week',NOW)['quota'])
        self.assertGreater(ledger.report('7d',NOW)['totals']['total'],0)

    def test_preferences_persist_without_mapping_or_counts(self):
        ledger=fixture('preferences')
        before=ledger.report('all',NOW)['totals']
        ledger.set_preference('one',True)
        rebuilt=fixture('preferences')
        self.assertEqual(rebuilt.preferences['focused_projects'],['one'])
        self.assertEqual(before,rebuilt.report('all',NOW)['totals'])
        self.assertEqual(rebuilt.manual,{})
        with self.assertRaises(ValueError):rebuilt.set_preference('unassigned',True)
        with self.assertRaises(ValueError):rebuilt.set_preference('one','true')
        rebuilt.set_preference('one',False)
        self.assertEqual(rebuilt.preferences['focused_projects'],[])

    def test_http_csv_and_preference_validation(self):
        ledger=fixture('http')
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(ledger))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(url+'/api/dashboard') as response:
                body=json.load(response)
            self.assertEqual(body['week']['generated_at'],body['all']['generated_at'])
            self.assertEqual(body['week']['lifetime'],body['all']['lifetime'])
            with urlopen(url+'/api/export.csv?period=all') as response:
                csv=response.read().decode('utf-8-sig')
            self.assertNotIn('分摊',csv)
            self.assertIn('累计本地token',csv)
            for payload,headers,code in [({'project_id':'one','focused':True},{},403),
                ([],{'X-Usage-Dashboard':'1'},400),
                ({'project_id':'missing','focused':True},{'X-Usage-Dashboard':'1'},400)]:
                req=Request(url+'/api/preferences',json.dumps(payload).encode(),headers={'Content-Type':'application/json',**headers})
                with self.assertRaises(HTTPError) as raised:urlopen(req)
                self.assertEqual(raised.exception.code,code)
                raised.exception.close()
            req=Request(url+'/api/preferences',b'{"project_id":"one","focused":true}',headers={'Content-Type':'application/json','X-Usage-Dashboard':'1'})
            with urlopen(req) as response:self.assertEqual(json.load(response),{'ok':True})
            ledger.set_preference('one',False)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
