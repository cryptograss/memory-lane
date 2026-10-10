"""What an agent is doing right now, read from the record alone."""

import time
import uuid

from django.test import TestCase

from conversations.models import (
    ConversationParticipant, Message, Mood, ThinkingEntity, Thought, ToolResult, ToolUse,
)
from django.core.cache import cache

from conversations.services.mood_view import HELD_FOR, activity, held_in, set_held

NOW = 1_790_000_000.0


class ActivityTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.tool = ConversationParticipant.objects.create(name='tool-result', participant_type='tool')
        cls.mood = Mood.objects.create(slug='m26')

    def setUp(self):
        cache.delete('held:m26')  # the cache is shared, not rolled back with the test

    def add(self, sender, seconds_ago, content='x', model=Message, **fields):
        return model.objects.create(id=uuid.uuid4(), sender=sender, mood=self.mood, content=content,
                                    timestamp=int((NOW - seconds_ago) * 1000), **fields)

    def now(self):
        return activity(self.mood, now=NOW)

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
        from conversations.services.mood_view import background_tasks
        session = uuid.uuid4()
        self.add(self.magent, 60, {'command': 'bash tools/preview.sh', 'description': 'Run the preview'},
                 model=ToolUse, tool_name='Bash', tool_id='tb1', stop_reason='tool_use', session_id=session)
        self.add(self.tool, 59, 'Command running in background with ID: bx1. Output is being written to: /tmp/x',
                 model=ToolResult, tool_use_id='tb1', session_id=session)
        self.add(self.magent, 50, {'description': 'Review it', 'prompt': 'review'},
                 model=ToolUse, tool_name='Agent', tool_id='tb2', stop_reason='tool_use', session_id=session)
        self.add(self.tool, 49, 'Async agent launched successfully.\nagentId: ag7 (internal)', model=ToolResult,
                 tool_use_id='tb2', session_id=session)
        running = background_tasks(self.mood, now=NOW)
        self.assertEqual([(t['id'], t['kind'], t['label']) for t in running],
                         [('bx1', 'command', 'Run the preview'), ('ag7', 'helper', 'Review it')])
        self.add(self.justin, 10, '<task-notification>\n<task-id>ag7</task-id>\n<status>completed</status>')
        self.assertEqual([t['id'] for t in background_tasks(self.mood, now=NOW)], ['bx1'])
        # Output that merely quotes a start or an ending is neither.
        self.add(self.tool, 5, 'rows: "Command running in background with ID: zz9" and '
                                '<task-id>bx1</task-id><status>stopped</status>', model=ToolResult,
                 tool_use_id='tb3', session_id=session)
        self.assertEqual([t['id'] for t in background_tasks(self.mood, now=NOW)], ['bx1'])

    def test_a_task_whose_ending_was_never_heard_ages_out(self):
        from conversations.services.mood_view import background_tasks
        session = uuid.uuid4()
        for tool_id, task, ago, text in [
                ('to1', 'bold1', 3 * 3600, 'Command running in background with ID: bold1. Output is being written to: /tmp/x'),
                ('to2', 'bnew2', 3600, 'Command running in background with ID: bnew2. Output is being written to: /tmp/y'),
                ('to3', 'agold', 3 * 3600, 'Async agent launched successfully.\nagentId: agold (internal)')]:
            self.add(self.magent, ago + 1, {'description': task}, model=ToolUse, tool_name='Bash', tool_id=tool_id,
                     stop_reason='tool_use', session_id=session)
            self.add(self.tool, ago, text, model=ToolResult, tool_use_id=tool_id, session_id=session)
        # A command past Claude Code's two-hour cap is gone; a helper may still be at work.
        self.assertEqual([t['id'] for t in background_tasks(self.mood, now=NOW)], ['bnew2', 'agold'])

    def test_a_queued_notice_that_a_task_finished_is_kept_and_ends_it(self):
        import json
        from conversations.models import Era
        from conversations.services.mood_view import background_tasks
        from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2
        session = uuid.uuid4()
        self.mood.claim(session)
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
        self.assertEqual(first[0].mood, self.mood)
        self.assertEqual(background_tasks(self.mood, now=NOW), [])

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

    def compacted(self, seconds_ago):
        """/compact's lines as Claude Code writes them once the summary is done."""
        stdout = ConversationParticipant.objects.get_or_create(name='stdout', defaults={'participant_type': 'system'})[0]
        self.add(self.justin, seconds_ago, '<local-command-caveat>The command below was run directly in Claude Code'
                                           '</local-command-caveat>')
        self.add(self.justin, seconds_ago, {'type': 'slash_command', 'command_name': '/compact',
                                            'command_message': 'compact', 'command_args': 'great run'})
        self.add(self.magent, seconds_ago, 'This session is being continued from a previous conversation that ran '
                                           'out of context. The summary below covers the earlier portion.')
        self.add(stdout, seconds_ago, {'type': 'command_output', 'stdout': 'Compacted '})

    def test_a_compaction_once_done_is_not_thinking(self):
        self.add(self.magent, 900, [{'type': 'text', 'text': 'done'}], stop_reason='end_turn')
        self.add(self.justin, 70, '/compact great run @magent', source_file='mood-web')
        self.assertEqual(self.now()['doing'], 'waking')  # while it compacts
        self.compacted(5)
        self.assertIsNone(self.now())

    def test_a_turn_after_a_compaction_counts_from_its_own_start(self):
        self.compacted(300)
        self.add(self.justin, 40, '<mood-wake mood="m26" reason="quiet">nothing said lately</mood-wake>')
        self.add(self.magent, 30, [{'type': 'thinking'}], model=Thought, stop_reason='tool_use')
        self.assertEqual(self.now(), {'agent': 'magent', 'doing': 'thinking', 'since': NOW - 40})

    def test_a_web_post_naming_an_agent_is_waking_it(self):
        self.add(self.justin, 3, '@magent can you see?', source_file='mood-web')
        self.assertEqual(self.now(), {'agent': 'magent', 'doing': 'waking', 'since': NOW - 3})

    def test_a_mention_the_runner_holds_says_so_and_why(self):
        # Asked 20 minutes ago -- longer than an unexplained wait is shown.
        self.add(self.justin, 1200, '@magent and another thing', source_file='mood-web')
        self.assertIsNone(self.now())
        set_held('m26', 'magent', '30 wakes this hour already', '2026-10-02T23:15:00+00:00', now=NOW - 5)
        self.assertEqual(self.now(), {'agent': 'magent', 'doing': 'held', 'why': '30 wakes this hour already',
                                      'until': '2026-10-02T23:15:00+00:00', 'since': NOW - 1200})
        # A newer post is waking again, until the runner has looked at it.
        self.add(self.justin, 2, '@magent still there?', source_file='mood-web')
        self.assertEqual(self.now()['doing'], 'waking')

    def test_a_hold_lapses_unless_renewed_or_lifted(self):
        self.add(self.justin, 200, '@magent hello', source_file='mood-web')
        set_held('m26', 'magent', 'hushed here', now=NOW - 100)
        self.assertEqual(self.now()['doing'], 'held')
        # A runner that died says nothing more: its hold lapses.
        self.assertEqual(held_in('m26', now=NOW - 100 + HELD_FOR - 1).keys(), {'magent'})
        self.assertEqual(held_in('m26', now=NOW - 100 + HELD_FOR + 1), {})
        set_held('m26', 'magent', '', now=NOW - 1)
        self.assertEqual(self.now()['doing'], 'waking')

    def test_a_web_post_naming_nobody_wakes_nobody(self):
        self.add(self.justin, 3, 'just a note', source_file='mood-web')
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
        self.add(self.justin, NOW - time.time() + 1, '@magent now', source_file='mood-web')  # 1s ago, really
        body = self.client.get('/api/moods/m26/turns/').json()
        self.assertEqual(body['activity']['doing'], 'waking')
        self.assertEqual({p['name'] for p in self.client.get('/api/moods/').json()['people']},
                         {'justin', 'magent'})
