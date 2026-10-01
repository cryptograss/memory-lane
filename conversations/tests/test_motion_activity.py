"""What an agent is doing right now, read from the record alone."""

import time
import uuid

from django.test import TestCase

from conversations.models import (
    ConversationParticipant, Message, Motion, ThinkingEntity, Thought, ToolResult, ToolUse,
)
from conversations.services.motion_view import activity

NOW = 1_790_000_000.0


class ActivityTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.tool = ConversationParticipant.objects.create(name='tool-result', participant_type='tool')
        cls.motion = Motion.objects.create(slug='m26')

    def add(self, sender, seconds_ago, content='x', model=Message, **fields):
        return model.objects.create(id=uuid.uuid4(), sender=sender, motion=self.motion, content=content,
                                    timestamp=int((NOW - seconds_ago) * 1000), **fields)

    def now(self):
        return activity(self.motion, now=NOW)

    def test_nothing_said_is_nothing_underway(self):
        self.assertIsNone(self.now())

    def test_a_prompt_then_a_thought_is_thinking_since_the_prompt(self):
        self.add(self.justin, 40, 'justin: look at this')
        self.add(self.magent, 30, [{'type': 'thinking'}], model=Thought, stop_reason='tool_use')
        self.assertEqual(self.now(), {'agent': 'magent', 'doing': 'thinking', 'since': NOW - 40})

    def test_a_tool_call_says_which(self):
        self.add(self.justin, 40, 'go')
        self.add(self.magent, 10, {}, model=ToolUse, tool_name='Bash', tool_id='t1', stop_reason='tool_use')
        self.assertEqual(self.now()['doing'], 'running a command')
        self.add(self.tool, 5, 'out', model=ToolResult, tool_use_id='t1')
        self.add(self.magent, 2, {}, model=ToolUse, tool_name='mcp__pickipedia__get-page', tool_id='t2',
                 stop_reason='tool_use')
        self.assertEqual(self.now()['doing'], 'on PickiPedia')

    def test_a_helper_finishing_does_not_end_the_turn(self):
        self.add(self.justin, 40, 'go')
        self.add(self.magent, 30, {}, model=ToolUse, tool_name='Agent', tool_id='t1', stop_reason='tool_use')
        self.add(self.magent, 10, {}, model=ToolUse, tool_name='mcp__pickipedia__search-page', tool_id='t2',
                 stop_reason='tool_use', is_sidechain=True)
        self.add(self.magent, 5, [{'type': 'text', 'text': 'found it'}], stop_reason='end_turn', is_sidechain=True)
        self.assertEqual(self.now(), {'agent': 'magent', 'doing': 'working with helpers', 'since': NOW - 40})

    def test_a_long_turn_counts_from_its_start(self):
        self.add(self.magent, 400, [{'type': 'text', 'text': 'earlier'}], stop_reason='end_turn')
        self.add(self.justin, 300, 'a big job')
        for i in range(80):
            self.add(self.magent, 299 - i, {}, model=ToolUse, tool_name='Bash', tool_id=f't{i}', stop_reason='tool_use')
        self.assertEqual(self.now()['since'], NOW - 300)

    def test_end_turn_is_the_end(self):
        self.add(self.justin, 40, 'go')
        self.add(self.magent, 5, [{'type': 'text', 'text': 'done'}], stop_reason='end_turn')
        self.assertIsNone(self.now())

    def test_the_harness_bookkeeping_after_a_turn_is_not_activity(self):
        system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
        self.add(self.justin, 40, 'go')
        self.add(self.magent, 5, [{'type': 'text', 'text': 'done'}], stop_reason='end_turn')
        self.add(system, 4, '')  # Claude Code's turn-duration line
        self.assertIsNone(self.now())

    def test_a_web_post_naming_an_agent_is_waking_it(self):
        self.add(self.justin, 3, '@magent can you see?', source_file='motion-web')
        self.assertEqual(self.now(), {'agent': 'magent', 'doing': 'waking', 'since': NOW - 3})

    def test_a_web_post_naming_nobody_wakes_nobody(self):
        self.add(self.justin, 3, 'just a note', source_file='motion-web')
        self.assertIsNone(self.now())

    def test_a_streak_that_went_quiet_long_ago_is_a_dead_session(self):
        self.add(self.justin, 900, 'go')
        self.add(self.magent, 800, [{'type': 'thinking'}], model=Thought, stop_reason='tool_use')
        self.assertIsNone(self.now())

    def test_older_rows_without_stop_reason_end_on_a_quiet_reply(self):
        self.add(self.justin, 90, 'go')
        self.add(self.magent, 60, [{'type': 'text', 'text': 'here'}])
        self.assertIsNone(self.now())
        self.add(self.magent, 5, [{'type': 'text', 'text': 'and a moment ago'}])
        self.assertIsNotNone(self.now())

    def test_the_turns_endpoint_carries_it(self):
        self.add(self.justin, NOW - time.time() + 1, '@magent now', source_file='motion-web')  # 1s ago, really
        body = self.client.get('/api/motions/m26/turns/').json()
        self.assertEqual(body['activity']['doing'], 'waking')
        self.assertEqual({p['name'] for p in self.client.get('/api/motions/').json()['people']},
                         {'justin', 'magent'})
