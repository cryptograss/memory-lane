"""The record side of a woken turn: where it finds its session, who said what, what the view shows."""

import json
import uuid

from django.test import TestCase

from conversations.models import Era, Message, Motion, ThinkingEntity
from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2

WAKE = '<motion-wake motion="m26">\n[skyler, 2026-09-29T18:00Z] @magent are you there?\n</motion-wake>'


def line(session, role, content, **overrides):
    record = {'type': role, 'uuid': str(uuid.uuid4()), 'parentUuid': None, 'sessionId': str(session),
              'timestamp': '2026-09-29T18:11:00.000Z', 'message': {'role': role, 'content': content}}
    record.update(overrides)
    return json.dumps(record)


class MotionWakeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.era = Era.objects.create(name='Test Era')
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        cls.motion = Motion.objects.create(slug='m26')
        cls.session = uuid.uuid4()
        cls.motion.claim(cls.session)

    def imp(self, raw):
        return import_line_from_claude_code_v2(raw, self.era, 'test.jsonl', 'justin')[0]

    def test_wake_prompt_is_the_pollers_not_the_container_owners(self):
        msg = self.imp(line(self.session, 'user', WAKE, entrypoint='sdk-cli'))
        self.assertEqual(msg.sender_id, 'motion-poller')
        msg = self.imp(line(self.session, 'user', [{'type': 'text', 'text': WAKE}], entrypoint='sdk-cli'))
        self.assertEqual(msg.sender_id, 'motion-poller')

    def test_a_person_typing_the_wrapper_is_still_that_person(self):
        msg = self.imp(line(self.session, 'user', WAKE, entrypoint='cli'))
        self.assertEqual(msg.sender_id, 'justin')

    def test_ordinary_prompt_is_still_the_container_owners(self):
        self.assertEqual(self.imp(line(self.session, 'user', 'justin: hello')).sender_id, 'justin')

    def test_view_and_mentions_show_neither_the_wake_nor_a_chosen_silence(self):
        self.imp(line(self.session, 'user', WAKE, entrypoint='sdk-cli'))
        self.imp(line(self.session, 'assistant', [{'type': 'text', 'text': '<silent/>'}]))
        self.imp(line(self.session, 'assistant', [{'type': 'text', 'text': 'Here, Sky.'}]))

        turns = self.client.get('/api/motions/m26/turns/').json()['turns']
        self.assertEqual([t['text'] for t in turns], ['Here, Sky.'])
        self.assertEqual(self.client.get('/api/mentions/magent/').json()['mentions'], [])

    def test_sessions_newest_first_filtered_by_sender(self):
        older, newer = uuid.uuid4(), uuid.uuid4()
        for session, sender, when in ((older, self.magent, 1), (newer, self.magent, 2), (uuid.uuid4(), self.skyler, 3)):
            m = Message.objects.create(id=uuid.uuid4(), sender=sender, content='x', motion=self.motion,
                                       session_id=session)
            Message.objects.filter(id=m.id).update(created_at=m.created_at.replace(minute=when))

        sessions = self.client.get('/api/motions/m26/sessions/?sender=magent').json()['sessions']
        self.assertEqual([s['session_id'] for s in sessions], [str(newer), str(older)])
        self.assertEqual(len(self.client.get('/api/motions/m26/sessions/').json()['sessions']), 3)

    def test_sessions_of_an_unknown_motion_is_404(self):
        self.assertEqual(self.client.get('/api/motions/nope/sessions/').status_code, 404)
