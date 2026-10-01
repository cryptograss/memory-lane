"""Which model, at what effort, an agent's turn ran on -- kept at import, shown in the Motion."""

import json
import uuid

from django.test import SimpleTestCase, TestCase

from conversations.models import Era, Message, Motion, ThinkingEntity
from conversations.services.motion_view import model_label
from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2


class ModelLabelTest(SimpleTestCase):

    def test_claude_names_read_as_family_and_version(self):
        self.assertEqual(model_label('claude-opus-5-5'), 'Opus 5.5')
        self.assertEqual(model_label('claude-fable-5-1'), 'Fable 5.1')
        self.assertEqual(model_label('claude-opus-5'), 'Opus 5')
        self.assertEqual(model_label('claude-haiku-4-5-20251001'), 'Haiku 4.5')
        self.assertEqual(model_label('claude-3-5-sonnet-20241022'), 'Sonnet 3.5')

    def test_other_names_pass_through_and_placeholders_vanish(self):
        self.assertEqual(model_label('gpt-5-codex'), 'gpt-5-codex')
        self.assertEqual(model_label('<synthetic>'), '')
        self.assertEqual(model_label(None), '')


class ImportAndShowTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.era = Era.objects.create(name='Test Era')
        ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.motion = Motion.objects.create(slug='m26')
        cls.session = uuid.uuid4()
        cls.motion.claim(cls.session)

    def assistant_line(self, text='hello', **extra):
        return json.dumps({
            'type': 'assistant', 'userType': 'external', 'uuid': str(uuid.uuid4()), 'parentUuid': None,
            'sessionId': str(self.session), 'timestamp': '2026-10-01T21:00:00.000Z',
            'effort': 'high', 'perTurnEffort': 'xhigh',
            'message': {'role': 'assistant', 'model': 'claude-opus-5-5', 'stop_reason': 'end_turn',
                        'content': [{'type': 'text', 'text': text}],
                        'usage': {'input_tokens': 12, 'output_tokens': 345,
                                  'cache_creation_input_tokens': 6, 'cache_read_input_tokens': 7890}},
            **extra})

    def test_model_effort_and_usage_are_kept(self):
        msg, _ = import_line_from_claude_code_v2(self.assistant_line(), self.era, 'a.jsonl', 'justin')
        msg = Message.objects.get(id=msg.id)
        self.assertEqual((msg.model_backend, msg.effort), ('claude-opus-5-5', 'xhigh'))  # this turn's effort
        self.assertEqual((msg.input_tokens, msg.output_tokens, msg.cache_read_input_tokens), (12, 345, 7890))

    def test_the_turn_says_how_it_ran(self):
        import_line_from_claude_code_v2(self.assistant_line('the answer'), self.era, 'a.jsonl', 'justin')
        turn = self.client.get('/api/motions/m26/turns/').json()['turns'][-1]
        self.assertEqual((turn['text'], turn['model'], turn['effort']), ('the answer', 'Opus 5.5', 'xhigh'))
