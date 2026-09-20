"""Case-specific provider overrides, checked at the last dispatch boundary."""
import json
from sprint_coordinator.util import canonical_json, sha256_text
from sprint_coordinator.judgments import usage_from_tokens


class DispatchPolicy:
    def __init__(self, coordinator):
        self.c = coordinator
        self.default_provider = coordinator.config.raw.get('default_provider', 'grok')
        self.settings_ok = True
        coordinator.store.conn.execute('''CREATE TABLE IF NOT EXISTS provider_decisions (
            id INTEGER PRIMARY KEY, assignment_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            event TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL)''')

    def recover(self):
        rows = self.c.store.conn.execute("SELECT d.* FROM provider_decisions d WHERE d.id=(SELECT MAX(x.id) FROM provider_decisions x WHERE x.assignment_id=d.assignment_id AND x.fingerprint=d.fingerprint) AND d.event='requested'").fetchall()
        for row in rows:
            self.c.store.conn.execute('INSERT INTO provider_decisions(assignment_id,fingerprint,event,detail,created_at) VALUES(?,?,?,?,?)',
                (row['assignment_id'],row['fingerprint'],'interrupted','{}',self.c.clock.now()))

    def refresh(self):
        fn = getattr(self.c.board, 'settings', None)
        if not callable(fn):
            return
        try:
            settings = fn().get('settings', {})
            provider = settings.get('worker', {}).get('default_executor')
            if provider not in ('grok', 'claude', 'codex'):
                raise ValueError('unsupported board default executor')
            self.default_provider = provider
            self.settings_ok = True
        except Exception:
            self.settings_ok = False

    def snapshot(self, row):
        spec = self.c.config.worker(row['worker'])
        job = row.get('job') or {}
        previous = job.get('previous_attempt') or {}
        reason = job.get('override_reason', '').strip()
        if not reason and previous.get('rejection'):
            reason = 'The previous attempt failed: ' + str(previous['rejection'])
        defaults = [s for s in self.c.config.workers.values()
                    if s.get('provider') == self.default_provider and s['role'] == row['role']
                    and s.get('cost') == 'low']
        model = self.c.config.raw.get('default_models', {}).get(self.default_provider)
        if not model and defaults:
            model = defaults[0].get('model')
        requested = spec.get('provider') or self.default_provider
        requested_model = spec.get('model') or model or 'test-adapter'
        return {'assignment_id': row['id'], 'task': job.get('question') or '',
                'revision': job.get('thread_revision'), 'command': spec['command'],
                'override_request_id': job.get('override_request_id'),
                'code_base_ref': self.c.config.raw.get('code_base_ref'),
                'default_provider': self.default_provider, 'requested_provider': requested,
                'default_model': model or requested_model, 'requested_model': requested_model,
                'reason': reason, 'evidence': {'previous_attempt': previous,
                    'supplied_evidence': job.get('override_evidence') or {}}}

    def record(self, row, snapshot, event, detail):
        self.c.store.conn.execute('INSERT INTO provider_decisions(assignment_id,fingerprint,event,detail,created_at) VALUES(?,?,?,?,?)',
            (row['id'], sha256_text(canonical_json(snapshot)), event,
             canonical_json(detail), self.c.clock.now()))

    def decision(self, row, snapshot):
        fp = sha256_text(canonical_json(snapshot))
        r = self.c.store.conn.execute('SELECT event,detail FROM provider_decisions WHERE assignment_id=? AND fingerprint=? ORDER BY id DESC LIMIT 1', (row['id'],fp)).fetchone()
        return (r['event'], json.loads(r['detail'])) if r else (None, None)

    def ready(self, row):
        if not self.settings_ok:
            return False
        s = self.snapshot(row)
        if (s['default_provider'],s['default_model']) == (s['requested_provider'],s['requested_model']):
            return True
        event, _ = self.decision(row,s)
        if event == 'approved':
            return True
        if event in ('requested','denied','unavailable','consumed'):
            return False
        if not s['reason']:
            self.record(row,s,'denied',{'reason':'override needs a case-specific reason'})
            self.c.router.hold(row['obligation_id'],'provider_override_reason_required')
            return False
        if not self.c.router.reserve(1,0):
            self.c.router.hold(row['obligation_id'],'budget_exhausted')
            return False
        fn = getattr(self.c.jev,'evaluate_provider_override',None)
        if not callable(fn):
            self.record(row,s,'unavailable',{'reason':'provider override verifier unavailable'})
            self.c.router.hold(row['obligation_id'],'provider_override_verifier_unavailable')
            return False
        self.record(row,s,'requested',s)
        if not self.c.pool.submit('provider_override', row['id'], self.evaluate, fn, s):
            self.record(row,s,'unavailable',{'reason':'verifier queue unavailable'})
        return False

    @staticmethod
    def evaluate(fn,s):
        result=fn(**{k:s[k] for k in ('task','default_provider','requested_provider',
                  'default_model','requested_model','reason','evidence')})
        return {'snapshot':s,'outcome':result}

    def finish(self, aid, payload, error):
        row=self.c.store.assignment(aid)
        if row is None:return
        s=(payload or {}).get('snapshot') or self.snapshot(row)
        if error:
            self.record(row,s,'unavailable',{'reason':type(error).__name__})
            self.c.router.hold(row['obligation_id'],'provider_override_verifier_unavailable')
            return
        outcome=payload['outcome']
        action=outcome.get('action') if isinstance(outcome,dict) else getattr(outcome,'action',None)
        reason=outcome.get('reason') if isinstance(outcome,dict) else getattr(outcome,'reason','')
        scores=outcome.get('judgments',{}) if isinstance(outcome,dict) else getattr(outcome,'judgments',{})
        tokens=outcome.get('usage_tokens',0) if isinstance(outcome,dict) else getattr(outcome,'usage_tokens',0)
        if tokens:
            from sprint_coordinator.routing import budget_day
            self.c.store.add_usage(budget_day(self.c.clock.now()),0,usage_from_tokens(tokens)['spend_usd'])
        self.record(row,s,'approved' if action=='accept' else 'denied', {'action':action,'reason':reason,'judgments':scores})
        if action!='accept':self.c.router.hold(row['obligation_id'],'provider_override_denied')

    def consume(self,row):
        s=self.snapshot(row)
        if (s['default_provider'],s['default_model']) != (s['requested_provider'],s['requested_model']):
            event,_=self.decision(row,s)
            if event!='approved':raise RuntimeError('override is not approved')
            self.record(row,s,'consumed',{'worker':row['worker']})
