"""An agent's tool steps in the Motion view, typing, and paging back."""

import json
import uuid

from django.core.cache import cache
from django.test import TestCase

from conversations.models import ConversationParticipant, Message, Motion, ThinkingEntity, ToolResult, ToolUse
from conversations.views_motions import PAGE


class StepsTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.tool = ConversationParticipant.objects.create(name='tool-result', participant_type='tool')
        cls.motion = Motion.objects.create(slug='m26')
        cls.session = uuid.uuid4()

    def add(self, sender, content, model=Message, **fields):
        return model.objects.create(id=uuid.uuid4(), sender=sender, motion=self.motion, content=content,
                                    timestamp=1, session_id=self.session, **fields)

    def get(self, **params):
        return self.client.get('/api/motions/m26/turns/', params).json()

    def test_steps_come_separately_so_a_tool_call_is_never_an_answer(self):
        self.add(self.justin, '@magent check it')
        self.add(self.magent, {'command': 'ls', 'description': 'List files'}, model=ToolUse,
                 tool_name='Bash', tool_id='t1')
        self.add(self.tool, 'a\nb', model=ToolResult, tool_use_id='t1')
        self.add(self.magent, [{'type': 'text', 'text': 'two files'}])
        body = self.get()
        self.assertEqual([t['sender'] for t in body['turns']], ['justin', 'magent'])
        self.assertEqual([(s['tool'], s['verb'], s['summary']) for s in body['steps']], [('Bash', 'ran', 'List files')])

    def test_a_choice_not_to_speak_is_a_dot_not_a_turn(self):
        self.add(self.justin, '@magent anything?')
        self.add(self.magent, [{'type': 'text', 'text': '<silent/>'}])
        self.add(self.magent, [{'type': 'text', 'text': '<silent>they are sorting out the gig</silent>'}])
        body = self.get()
        self.assertEqual([t['sender'] for t in body['turns']], ['justin'])  # the poller reads turns as answers
        self.assertEqual([q['reason'] for q in body['quiet']], ['', 'they are sorting out the gig'])

    def test_a_step_opens_to_its_input_and_result(self):
        step = self.add(self.magent, {'command': 'false'}, model=ToolUse, tool_name='Bash', tool_id='t9')
        self.add(self.tool, 'exit 1', model=ToolResult, tool_use_id='t9', is_error=True)
        detail = self.client.get(f'/api/motions/m26/steps/{step.id}/').json()
        self.assertEqual(detail['input'], {'command': 'false'})
        self.assertEqual(detail['result'], {'text': 'exit 1', 'is_error': True})

    def test_a_step_is_only_found_in_its_own_motion(self):
        other = Motion.objects.create(slug='elsewhere')
        step = ToolUse.objects.create(id=uuid.uuid4(), sender=self.magent, motion=other, content={},
                                      timestamp=1, tool_name='Bash', tool_id='t2')
        self.assertEqual(self.client.get(f'/api/motions/m26/steps/{step.id}/').status_code, 404)
        self.assertEqual(self.client.get('/api/motions/m26/steps/not-a-uuid/').status_code, 404)

    def test_a_first_load_is_the_newest_page_and_earlier_pages_follow(self):
        for i in range(PAGE + 5):
            self.add(self.justin, f'line {i}')
        first = self.get()
        self.assertEqual(len(first['turns']), PAGE)
        self.assertEqual(first['turns'][-1]['text'], f'line {PAGE + 4}')
        self.assertTrue(first['has_earlier'])
        back = self.get(before=first['turns'][0]['id'])
        self.assertEqual([t['text'] for t in back['turns']], [f'line {i}' for i in range(5)])
        self.assertFalse(back['has_earlier'])

    def test_a_poll_after_an_id_is_everything_since(self):
        first = self.add(self.justin, 'one')
        self.add(self.justin, 'two')
        self.assertEqual([t['text'] for t in self.get(after=first.id)['turns']], ['two'])


class TypingTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        Motion.objects.create(slug='m26')

    def setUp(self):
        cache.clear()

    def test_typing_needs_a_device_and_shows_in_the_turns(self):
        from unittest import mock
        device = mock.Mock(entity_id='skyler')
        self.assertEqual(self.client.post('/api/motions/m26/typing/', '{}', content_type='application/json').status_code, 401)
        with mock.patch('conversations.services.motion_auth.device_for', return_value=device):
            self.client.post('/api/motions/m26/typing/', json.dumps({'typing': True}), content_type='application/json')
            self.assertEqual(self.client.get('/api/motions/m26/turns/').json()['typing'], ['skyler'])
            self.client.post('/api/motions/m26/typing/', json.dumps({'typing': False}), content_type='application/json')
        self.assertEqual(self.client.get('/api/motions/m26/turns/').json()['typing'], [])
