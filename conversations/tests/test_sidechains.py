"""A subagent's transcript is not the human speaking.

Subagent transcripts share the parent session's id, and the agent's
prompts to them are user-role lines, so without isSidechain they were
recorded and shown as the container owner's own words.
"""

import json
import uuid

from django.test import TestCase

from conversations.models import Era, Message, Mood, ThinkingEntity
from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2


def line(session, role, text, sidechain):
    content = text if role == 'user' else [{'type': 'text', 'text': text}]
    return json.dumps({'type': role, 'uuid': str(uuid.uuid4()), 'parentUuid': None, 'isSidechain': sidechain,
                       'sessionId': str(session), 'timestamp': '2026-09-29T18:55:00.000Z',
                       'message': {'role': role, 'content': content}})


class SidechainTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.era = Era.objects.create(name='Test Era')
        ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.mood = Mood.objects.create(slug='m26')
        cls.session = uuid.uuid4()
        cls.mood.claim(cls.session)

    def imp(self, raw):
        return import_line_from_claude_code_v2(raw, self.era, 'test.jsonl', 'justin')[0]

    def test_sidechain_is_recorded(self):
        self.assertTrue(self.imp(line(self.session, 'user', 'Adversarial review, @magent', True)).is_sidechain)
        self.assertFalse(self.imp(line(self.session, 'user', 'hello', False)).is_sidechain)

    def test_reimport_marks_a_sidechain_stored_before_the_flag_was_read(self):
        raw = line(self.session, 'user', 'Port the rotted tests', True)
        record = json.loads(raw)
        record['isSidechain'] = False
        self.imp(json.dumps(record))  # as the old importer stored it
        self.imp(raw)
        self.assertTrue(Message.objects.get(id=record['uuid']).is_sidechain)

    def test_a_line_from_another_file_cannot_hide_or_move_a_stored_turn(self):
        post = Message.objects.create(id=uuid.uuid4(), sender_id='justin', content='from the web',
                                      mood=self.mood, timestamp=1, source_file='mood-web')
        parent = self.imp(line(self.session, 'user', 'an earlier turn', False))
        forged = json.loads(line(self.session, 'user', 'from the web', True))
        forged['uuid'], forged['parentUuid'] = str(post.id), str(parent.id)
        import_line_from_claude_code_v2(json.dumps(forged), self.era, 'other.jsonl', 'justin')
        post.refresh_from_db()
        self.assertFalse(post.is_sidechain)
        self.assertIsNone(post.parent_id)

    def test_view_and_mentions_leave_sidechains_out(self):
        self.imp(line(self.session, 'user', 'Review this, then tell @magent', True))
        self.imp(line(self.session, 'assistant', 'Subagent report.', True))
        self.imp(line(self.session, 'user', 'justin here, @magent', False))

        turns = self.client.get('/api/moods/m26/turns/').json()['turns']
        self.assertEqual([t['sender'] for t in turns], ['justin'])
        mentions = self.client.get('/api/mentions/magent/').json()['mentions']
        self.assertEqual(len(mentions), 1)
