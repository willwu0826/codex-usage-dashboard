"""使用纯合成日志验证计量边界，不访问真实 Codex 数据。"""
import atexit
import json
from pathlib import Path
import sys
import unittest
import tempfile

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / '02_实现'))
ARTIFACTS = TESTS / '.artifacts'
ARTIFACTS.mkdir(exist_ok=True)
TEMP = tempfile.TemporaryDirectory(prefix='计量-', dir=ARTIFACTS)
atexit.register(TEMP.cleanup)
ROOT = Path(TEMP.name)
from usage_core import Ledger, parse_rollout, stamp, project_id

def counters(i, o=0, cached=0, reasoning=0):
    return dict(input_tokens=i, output_tokens=o, cached_input_tokens=cached,
                reasoning_output_tokens=reasoning, total_tokens=i+o)

def meta(tid='parent', when='2026-10-03T00:00:00Z', **extra):
    return dict(timestamp=when, type='session_meta', payload=dict(id=tid, timestamp=when, cwd=r'D:\fixture', **extra))

def event(usage, when='2026-10-03T01:00:00Z', last=None, quota=None):
    return dict(timestamp=when, type='event_msg', payload=dict(type='token_count', info=dict(total_token_usage=usage, last_token_usage=last), rate_limits=quota))

def write(name, records):
    path = ROOT / 'fixtures' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in records), encoding='utf-8')
    return path

class Accounting(unittest.TestCase):
    def test_cumulative_duplicate_and_subset(self):
        data = [meta(), event(counters(100,10,80,4)), event(counters(100,10,80,4)),
                event(counters(200,30,160,9), '2026-10-03T02:00:00Z')]
        parsed = parse_rollout(write('cumulative.jsonl', data))
        self.assertEqual(sum(e['total'] for e in parsed['events']), 230)
        self.assertEqual(sum(e['cached'] for e in parsed['events']),160)
        self.assertEqual(parsed['duplicates'],1)

    def test_restored_balance_not_charged(self):
        p = parse_rollout(write('restored.jsonl', [meta(), event(counters(1000),last=counters(100))]))
        self.assertEqual(p['events'][0]['total'],100)
        self.assertEqual(p['unplaced_tokens'],900)

    def test_out_of_order_preserves_high_water(self):
        p = parse_rollout(write('old-counter.jsonl',[meta(),event(counters(100)),event(counters(200)),event(counters(100)),event(counters(250))]))
        self.assertEqual(sum(e['total'] for e in p['events']),250)
        self.assertEqual(p['ambiguous_counters'],1)

    def test_verified_new_epoch(self):
        p=parse_rollout(write('reset.jsonl',[meta(),event(counters(100)),event(counters(40),last=counters(40)),event(counters(70),last=counters(30))]))
        self.assertEqual(sum(e['total'] for e in p['events']),170)

    def test_first_meta_owns_inherited_header(self):
        p=parse_rollout(write('child.jsonl',[meta('child',forked_from_id='parent'),meta('parent','2026-10-02T00:00:00Z'),event(counters(20))]))
        self.assertEqual(p['events'][0]['thread'],'child')

    def test_migration_time_and_quota_removed(self):
        when='2026-10-03T00:00:00Z'
        q=dict(limit_id='codex',primary=dict(window_minutes=10080,used_percent=20,resets_at=1791590400))
        p=parse_rollout(write('migration.jsonl',[meta(),event(counters(100),when,quota=q),event(counters(200),when,quota=q)]))
        self.assertTrue(all(e['time_unknown'] for e in p['events']))
        self.assertEqual(p['quotas'],[])

    def test_last_only_reported_as_unsupported(self):
        p=parse_rollout(write('unsupported.jsonl',[meta(),event(None,last=counters(30))]))
        self.assertEqual(p['unsupported_usage'],1)

    def test_partial_line_ignored_until_complete(self):
        path=write('partial.jsonl',[meta(),event(counters(10))])
        with path.open('ab') as f:f.write(b'{"type":"event_msg","payload":{"type":"token_count"')
        p=parse_rollout(path)
        self.assertEqual(p['partial_lines'],1)
        self.assertEqual(sum(e['total'] for e in p['events']),10)

    def test_week_primary_and_expiry(self):
        ledger=Ledger(ROOT/'isolated-ledger',codex_home=ROOT/'empty-codex',workspace=ROOT/'workspace',registry=ROOT/'no-registry.json')
        now=stamp('2026-10-03T10:00:00Z')
        ledger.snapshot={'threads':{},'parents':{},'events':[],'quotas':[dict(limit_id='codex',sampled=now-10,resets_at=now+100,used_percent=25,window_minutes=10080)],'scan':{}}
        report=ledger.report('week',now)
        self.assertEqual(report['quota']['remaining_percent'],75)
        self.assertEqual(stamp(report['period']['start']),now+100-604800)
        expired=ledger.report('week',now+200)
        self.assertTrue(expired['quota']['expired'])
        self.assertIsNone(expired['quota']['remaining_percent'])
        self.assertIsNone(expired['period']['start'])

    def test_ambiguous_registry_and_child(self):
        ledger=Ledger(ROOT/'isolated-ledger',codex_home=ROOT/'empty-codex',workspace=ROOT/'workspace',registry=ROOT/'no-registry.json')
        ids={'a':{'id':'a','path':r'D:\one','name':'one'},'b':{'id':'b','path':r'D:\two','name':'two'}}
        value=ledger.attribution('child',{}, {'child':'parent'},ids,{'parent':{'a','b'}})
        self.assertEqual(value[0],'unassigned')
        value=ledger.attribution('child',{}, {'child':'parent'},ids,{'parent':{'a'}})
        self.assertEqual(value[0],'a')

    def test_copy_dedupe_refresh_idempotent(self):
        base=ROOT/'copy-ledger'
        logs=base/'codex'/'sessions';logs.mkdir(parents=True,exist_ok=True)
        parent=[meta(),{'timestamp':'2026-10-03T01:00:00Z','type':'turn_context','payload':{'turn_id':'stable-turn','model':'test'}},event(counters(100),last=counters(100)),event(counters(200),'2026-10-03T02:00:00Z',last=counters(100))]
        child=[meta('child',forked_from_id='parent',source={'subagent':{'thread_spawn':{'parent_thread_id':'parent'}}})]+parent[1:]+[event(counters(250),'2026-10-03T03:00:00Z',last=counters(50))]
        for name,records in [('parent.jsonl',parent),('child.jsonl',child)]:
            (logs/name).write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
        ledger=Ledger(base,codex_home=base/'codex',workspace=base/'workspace',registry=base/'none.json')
        ledger.refresh();a=ledger.report('all')
        ledger.refresh();b=ledger.report('all')
        self.assertEqual(a['totals']['total'],250)
        self.assertEqual(a['totals'],b['totals'])
        self.assertEqual(a['scan']['inherited_copies'],2)

if __name__=='__main__':
    unittest.main(verbosity=2)
