import json
from pathlib import Path
from unittest.mock import patch

from test_sprintd import Base


class AssignmentHTTPTests(Base):
    def setUp(self):
        super().setUp()
        self.app.save_settings({'worker': {
            'default_executor': 'grok',
            'executors': {'grok': {'kind': 'tmux', 'command': 'grok', 'model': 'grok-4.6'},
                          'codex': {'kind': 'tmux', 'command': 'codex'}},
        }}, actor='session')
        (Path(self.app.data_dir) / 'assignment-policy.json').write_text(json.dumps({
            'enabled': True, 'daily_max_calls': 10,
        }))
        self.num = self.new_card('Implement a bounded parser change')['num']

    def assign(self, **extra):
        return self.post('/api/cards/%s/assign' % self.num, {
            'agent_name': 'sprint-card-%s' % self.num,
            'work_kind': 'ops', 'actor': 'session', **extra,
        })

    def test_default_model_needs_no_verifier_or_credentials(self):
        with patch('sprint_coordinator.assignment_policy.load_api_key', side_effect=AssertionError('no paid call')):
            status, result = self.assign(executor='grok', model='grok-4.6')
        self.assertEqual(status, 200, result)
        self.assertEqual(result['card']['executor'], 'grok')

    def test_alternate_without_reason_cannot_mutate_assignment(self):
        status, result = self.assign(executor='codex', model='gpt-5.6-sol')
        self.assertEqual(status, 422, result)
        self.assertEqual(result['error'], 'assignment_override_rejected')
        self.assertIsNone(self.app.card_row(self.num)['agent_name'])

    def test_omitted_model_resolves_to_configured_default(self):
        status, result = self.assign(executor='grok')
        self.assertEqual(status, 200, result)

    def test_same_provider_different_model_requires_approval(self):
        status, result = self.assign(executor='grok', model='some-other-model')
        self.assertEqual(status, 422, result)
        self.assertIsNone(self.app.card_row(self.num)['agent_name'])

    def test_card_title_change_during_verification_invalidates_approval(self):
        def evaluate(*args, **kwargs):
            with self.app.lock:
                self.app.conn.execute('UPDATE cards SET title=? WHERE num=?',
                                      ('A materially different task', self.num))
            return {'action': 'accept', 'judgments': {}, 'usage_tokens': 0}
        with patch('sprint_coordinator.assignment_policy.load_api_key', return_value='test-key'), \
             patch('sprint_coordinator.assignment_policy.evaluate_provider_override', side_effect=evaluate):
            status, result = self.assign(executor='codex', model='gpt-5.6-sol',
                                         model_reason='Independent concurrency review')
        self.assertEqual(status, 409, result)
        self.assertIsNone(self.app.card_row(self.num)['agent_name'])

    def test_approved_override_is_checked_again_for_each_assignment(self):
        with patch('sprint_coordinator.assignment_policy.load_api_key', return_value='test-key'), \
             patch('sprint_coordinator.assignment_policy.evaluate_provider_override',
                   return_value={'action': 'accept', 'judgments': {}, 'usage_tokens': 0}) as verify:
            for _ in range(2):
                status, result = self.assign(executor='codex', model='gpt-5.6-sol',
                                             model_reason='Concurrency change needs an independent code review')
                self.assertEqual(status, 200, result)
        self.assertEqual(verify.call_count, 2)

    def test_omitting_executor_cannot_hide_retained_alternate(self):
        with self.app.lock:
            self.app.conn.execute('UPDATE cards SET executor=?,model=? WHERE num=?',
                                  ('codex', 'gpt-5.6-sol', self.num))
        status, result = self.assign()
        self.assertEqual(status, 422, result)
        self.assertIsNone(self.app.card_row(self.num)['agent_name'])

    def test_changed_default_during_verification_rejects_stale_approval(self):
        def evaluate(*args, **kwargs):
            self.app.save_settings({'worker': {'default_executor': 'codex'}}, actor='session')
            return {'action': 'accept', 'judgments': {}, 'usage_tokens': 0}
        with patch('sprint_coordinator.assignment_policy.load_api_key', return_value='test-key'), \
             patch('sprint_coordinator.assignment_policy.evaluate_provider_override', side_effect=evaluate):
            status, result = self.assign(executor='codex', model='gpt-5.6-sol',
                                         model_reason='Independent concurrency review')
        self.assertEqual(status, 409, result)
        self.assertIsNone(self.app.card_row(self.num)['agent_name'])

    def test_outage_blocks_override_without_mutating_card(self):
        with patch('sprint_coordinator.assignment_policy.load_api_key', return_value='test-key'), \
             patch('sprint_coordinator.assignment_policy.evaluate_provider_override', side_effect=TimeoutError):
            status, result = self.assign(executor='codex', model='gpt-5.6-sol',
                                         model_reason='Independent concurrency review')
        self.assertEqual(status, 422, result)
        self.assertIsNone(self.app.card_row(self.num)['agent_name'])
