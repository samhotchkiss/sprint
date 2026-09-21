import json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from tests.test_sprintd import Base, sprintd
from sprint_coordinator.message_quality import DIMENSIONS

class PublicationTest(Base):
    def setUp(self):
        super().setUp()
        Path(self.app.data_dir, 'message-quality.json').write_text(json.dumps({'enabled': True,'daily_max_calls':2}))

    def test_agent_rejected_before_write_user_never_checked(self):
        response=SimpleNamespace(answers={k:{'noul':0.1} for k in DIMENSIONS},input_tokens=1,output_tokens=0)
        with patch('sprint_coordinator.publication.load_api_key',return_value='test'), patch('sprint_coordinator.publication.JevClient.evaluate',return_value=response) as judge:
            before=len(self.app.sidebar_thread())
            with self.assertRaises(sprintd.ApiError) as error:
                self.app.sidebar_post('A long irrelevant report.', 'session')
            self.assertEqual(error.exception.code,'message_needs_revision')
            self.assertEqual(len(self.app.sidebar_thread()),before)
            self.app.sidebar_post('My message stays intact.', 'user')
            self.assertEqual(judge.call_count,1)

    def test_accepted_and_bounded_calls(self):
        response=SimpleNamespace(answers={k:{'noul':0.99} for k in DIMENSIONS},input_tokens=1,output_tokens=0)
        with patch('sprint_coordinator.publication.load_api_key',return_value='test'), patch('sprint_coordinator.publication.JevClient.evaluate',return_value=response) as judge:
            self.app.sidebar_post('**Ready.**\n\n- Review #1.', 'session')
            self.app.sidebar_post('A second update.', 'session')
            self.app.sidebar_post('A third update.', 'session')
            self.assertEqual(judge.call_count,2)

    def test_worker_card_and_question_checked(self):
        card=self.app.create_card('A task',[],False)
        self.app.transition(card['num'],'triaging','session')
        response=SimpleNamespace(answers={k:{'noul':0.1} for k in DIMENSIONS},input_tokens=1,output_tokens=0)
        with patch('sprint_coordinator.publication.load_api_key',return_value='test'), patch('sprint_coordinator.publication.JevClient.evaluate',return_value=response):
            with self.assertRaises(sprintd.ApiError):
                self.app.card_event(card['num'],'chat',{'text':'noise'},actor='worker')
            self.app.ask(card['num'],'Unclear?',[],actor='worker')
            self.app.card_event(card['num'],'error',{'text':'Critical failure'},actor='worker')
            self.assertEqual(self.app.card_row(card['num'])['state'],'needs_you')

    def test_uncertain_quality_preserves_message(self):
        response=SimpleNamespace(answers={k:{'noul':0.5} for k in DIMENSIONS},input_tokens=1,output_tokens=0)
        with patch('sprint_coordinator.publication.load_api_key',return_value='test'), patch('sprint_coordinator.publication.JevClient.evaluate',return_value=response):
            self.app.sidebar_post('A useful update.', 'session')
            self.assertEqual(self.app.sidebar_thread()[-1]['payload']['text'],'A useful update.')
