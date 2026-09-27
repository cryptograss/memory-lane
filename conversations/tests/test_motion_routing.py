"""Tests for routing incoming messages into Motions at import time.

Also covers the fields the importer was silently dropping. Before this,
every get_or_create in importers_and_parsers/claude_code_v2.py listed its
own defaults and none included session_id, so 99.6% of the corpus has no
session and nothing could be grouped by conversation.
"""

import json
import uuid

from django.test import TestCase

from conversations.models import (
    Era, Message, Motion, MotionSession, ThinkingEntity,
)
from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2


def user_line(session_id, text="hello", **overrides):
    """A minimal Claude Code v2 user event."""
    record = {
        "type": "user",
        "userType": "external",
        "uuid": str(uuid.uuid4()),
        "parentUuid": None,
        "sessionId": str(session_id),
        "cwd": "/home/magent/workspace/magenta",
        "gitBranch": "main",
        "version": "2.1.220",
        "timestamp": "2026-09-27T18:00:00.000Z",
        "message": {"role": "user", "content": text},
    }
    record.update(overrides)
    return json.dumps(record)


class ImporterKeepsSessionContextTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.era = Era.objects.create(name="Test Era")
        ThinkingEntity.objects.get_or_create(name="justin", defaults={'is_biological_human': True})

    def test_session_cwd_and_branch_are_recorded(self):
        session = uuid.uuid4()
        import_line_from_claude_code_v2(user_line(session), self.era, "test.jsonl")

        msg = Message.objects.get()
        self.assertEqual(str(msg.session_id), str(session))
        self.assertEqual(msg.cwd, "/home/magent/workspace/magenta")
        self.assertEqual(msg.git_branch, "main")
        self.assertEqual(msg.client_version, "2.1.220")

    def test_missing_context_is_tolerated(self):
        # Older transcripts, and some event shapes, carry none of this.
        line = user_line(uuid.uuid4())
        record = json.loads(line)
        for key in ("sessionId", "cwd", "gitBranch", "version"):
            del record[key]

        import_line_from_claude_code_v2(json.dumps(record), self.era, "test.jsonl")

        msg = Message.objects.get()
        self.assertIsNone(msg.session_id)
        self.assertIsNone(msg.cwd)
        self.assertIsNone(msg.motion)


class MotionRoutingTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.era = Era.objects.create(name="Test Era")
        ThinkingEntity.objects.get_or_create(name="justin", defaults={'is_biological_human': True})
        cls.motion = Motion.objects.create(slug="magenta-26-million", title="Magenta 26 Million")

    def test_claimed_session_routes_at_import(self):
        session = uuid.uuid4()
        self.motion.claim(session)

        import_line_from_claude_code_v2(user_line(session), self.era, "test.jsonl")

        self.assertEqual(Message.objects.get().motion, self.motion)

    def test_unclaimed_session_routes_nowhere(self):
        import_line_from_claude_code_v2(user_line(uuid.uuid4()), self.era, "test.jsonl")
        self.assertIsNone(Message.objects.get().motion)

    def test_one_motion_collects_several_sessions(self):
        # The point of the object: a subject outlives any one runtime session.
        first, second = uuid.uuid4(), uuid.uuid4()
        self.motion.claim(first)
        self.motion.claim(second)

        import_line_from_claude_code_v2(user_line(first, "before compaction"), self.era, "a.jsonl")
        import_line_from_claude_code_v2(user_line(second, "after resume"), self.era, "b.jsonl")

        self.assertEqual(self.motion.messages.count(), 2)

    def test_claiming_moves_a_session_between_motions(self):
        session = uuid.uuid4()
        other = Motion.objects.create(slug="storage-and-transfer")
        self.motion.claim(session)
        other.claim(session)

        self.assertEqual(MotionSession.objects.count(), 1)
        self.assertEqual(MotionSession.motion_for(session), other)

    def test_motion_for_handles_nothing(self):
        self.assertIsNone(MotionSession.motion_for(None))
        self.assertIsNone(MotionSession.motion_for(uuid.uuid4()))

    def test_retiring_a_motion_drops_claims_but_keeps_messages(self):
        session = uuid.uuid4()
        self.motion.claim(session)
        import_line_from_claude_code_v2(user_line(session), self.era, "test.jsonl")

        self.motion.delete()

        self.assertEqual(MotionSession.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 1)
        self.assertIsNone(Message.objects.get().motion)
