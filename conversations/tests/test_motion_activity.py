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

    def test_background_tasks_show_until_they_end(self):
        from conversations.services.motion_view import background_tasks
        session = uuid.uuid4()
        self.add(self.magent, 60, {'command': 'bash tools/preview.sh', 'description': 'Run the preview'},
                 model=ToolUse, tool_name='Bash', tool_id='tb1', stop_reason='tool_use', session_id=session)
        self.add(self.tool, 59, 'Command running in background with ID: bx1. Output is being written to: /tmp/x',
                 model=ToolResult, tool_use_id='tb1', session_id=session)
        self.add(self.magent, 50, {'description': 'Review it', 'prompt': 'review'},
                 model=ToolUse, tool_name='Agent', tool_id='tb2', stop_reason='tool_use', session_id=session)
        self.add(self.tool, 49, 'Async agent launched successfully.\nagentId: ag7 (internal)', model=ToolResult,
                 tool_use_id='tb2', session_id=session)
        running = background_tasks(self.motion, now=NOW)
        self.assertEqual([(t['id'], t['kind'], t['label']) for t in running],
                         [('bx1', 'command', 'Run the preview'), ('ag7', 'helper', 'Review it')])
        self.add(self.justin, 10, '<task-notification>\n<task-id>ag7</task-id>\n<status>completed</status>')
        self.assertEqual([t['id'] for t in background_tasks(self.motion, now=NOW)], ['bx1'])
        # Output that merely quotes a start or an ending is neither.
        self.add(self.tool, 5, 'rows: "Command running in background with ID: zz9" and '
                                '<task-id>bx1</task-id><status>stopped</status>', model=ToolResult,
                 tool_use_id='tb3', session_id=session)
        self.assertEqual([t['id'] for t in background_tasks(self.motion, now=NOW)], ['bx1'])

    def test_a_queued_notice_that_a_task_finished_is_kept_and_ends_it(self):
        import json
        from conversations.models import Era
        from conversations.services.motion_view import background_tasks
        from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2
        session = uuid.uuid4()
        self.motion.claim(session)
        self.add(self.magent, 60, {'description': 'Dry run'}, model=ToolUse, tool_name='Bash', tool_id='tq1',
                 stop_reason='tool_use', session_id=session)
        self.add(self.tool, 59, 'Command running in background with ID: bq9. Output is being written to: /tmp/x',
                 model=ToolResult, tool_use_id='tq1', session_id=session)
        line = json.dumps({'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': '2026-10-01T15:36:06.952Z',
                           'sessionId': str(session),
                           'content': '<task-notification>\n<task-id>bq9</task-id>\n<status>completed</status>'})
        era = Era.objects.create(name='e')
        first = import_line_from_claude_code_v2(line, era, 'a.jsonl', 'justin')
        again = import_line_from_claude_code_v2(line, era, 'a.jsonl', 'justin')  # a replay
        self.assertEqual((first[1], again[1]), (True, False))
        self.assertEqual(first[0].motion, self.motion)
        self.assertEqual(background_tasks(self.motion, now=NOW), [])

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
