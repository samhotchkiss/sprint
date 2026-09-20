import tempfile
import unittest
from types import SimpleNamespace
from tests.test_sprint_coordinator import MemoryBoard, FakeJev, make_config, user_event, ok_reply
from sprint_coordinator.driver import Coordinator
from sprint_coordinator.workers import InProcessRunner
from sprint_coordinator.util import Clock

class Judge(FakeJev):
    def __init__(self,action='accept',error=False):
        super().__init__(); self.overrides=[];self.action=action;self.error=error
    def evaluate_provider_override(self,**kwargs):
        self.overrides.append(kwargs)
        if self.error:raise RuntimeError('service down')
        return SimpleNamespace(action=self.action,reason='evaluated supplied evidence',judgments={'reason_specific':.95},usage_tokens=100)

class ProviderOverrides(unittest.TestCase):
    def setup_case(self,action='accept',error=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.board=MemoryBoard();self.clock=Clock(1000);self.sent=[];self.judge=Judge(action,error)
        cfg=make_config(temp.name,extra_workers={
            'low':{'command':['low'],'role':'response','cost':'low','provider':'grok','model':'grok-test'},
            'high':{'command':['high'],'role':'response','cost':'high','provider':'claude','model':'claude-test'}})
        def run(job):self.sent.append(job);return ok_reply(job)
        c=Coordinator(cfg,'active',clock=self.clock,board=self.board,jev=self.judge,
                      runner=InProcessRunner({'low':run,'high':run}))
        c.open();self.addCleanup(c.close)
        self.board.add(user_event(1,'Diagnose the deadlock'))
        c.ingest();c.router.reconcile_obligations()
        self.oid=c.store.obligations()[0]['id'];self.c=c
        return c
    def assignment(self,worker='high',reason='Two Grok attempts failed the same deadlock reproduction; stronger concurrency reasoning is needed.'):
        c=self.c;aid=c.router._assign(self.oid,worker,'response','high' if worker=='high' else 'low')
        row=c.store.assignment(aid);job=row['job'];job['override_reason']=reason
        c.store.update_assignment(aid,job=job);return aid
    def settle(self):
        for _ in range(4):self.c._launch();self.c._settle()
    def test_default_never_calls_override_verifier(self):
        self.setup_case();self.assignment('low');self.settle()
        self.assertEqual(len(self.sent),1);self.assertEqual(self.judge.overrides,[])
    def test_denial_missing_reason_and_outage_never_send(self):
        for action,error,reason in [('deny',False,'I prefer Claude'),('accept',False,''),('accept',True,'Two failures')]:
            with self.subTest(action=action,error=error,reason=reason):
                self.setup_case(action,error);self.assignment(reason=reason);self.settle()
                self.assertEqual(self.sent,[])
    def test_approval_allows_exactly_one_launch_and_audits_model(self):
        c=self.setup_case();aid=self.assignment();self.settle();self.settle()
        self.assertEqual(len(self.sent),1);self.assertEqual(len(self.judge.overrides),1)
        self.assertEqual(self.sent[0]['provider'],'claude');self.assertEqual(self.sent[0]['selected_model'],'claude-test')
        self.assertEqual(c.store.conn.execute("SELECT COUNT(*) FROM provider_decisions WHERE event='consumed'").fetchone()[0],1)
    def test_changed_target_requires_another_approval(self):
        c=self.setup_case();aid=self.assignment();c._launch();c.pool.join(1);c._drain()
        self.assertEqual(self.sent,[])
        c.config.workers['high']['model']='different-model'
        c._launch();c.pool.join(1);c._drain()
        self.assertEqual(self.sent,[]);self.assertEqual(len(self.judge.overrides),2)
        c._launch();c._settle();self.assertEqual(len(self.sent),1)
    def test_new_context_prevents_launch_of_previously_approved_job(self):
        c=self.setup_case();aid=self.assignment();c._launch();c.pool.join(1);c._drain()
        self.board.add(user_event(2,'Do not pursue that approach.'));c.ingest()
        self.settle();self.assertEqual(self.sent,[])
        self.assertEqual(c.store.assignment(aid)['status'],'superseded')
    def test_requested_judgment_recovers_without_reusing_approval(self):
        c=self.setup_case();aid=self.assignment();row=c.store.assignment(aid);s=c.dispatch_policy.snapshot(row)
        c.dispatch_policy.record(row,s,'requested',s)
        c.dispatch_policy.recover();self.settle()
        self.assertEqual(len(self.judge.overrides),1);self.assertEqual(len(self.sent),1)

if __name__=='__main__':unittest.main()
