"""A turn posted as record events (docs/RECORD_EVENTS.md) becomes the rows
Claude Code's own stream does: the format is memory-lane's, not a harness's."""

import json
import uuid
from datetime import datetime, timedelta, timezone

from django.test import TestCase, override_settings

from conversations.models import Message, Mood, ThinkingEntity, Thought, ToolResult, ToolUse
from conversations.tests.test_mood_runner import AUTH, KEY, event

T0 = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
WAKE = '<mood-wake mood="m26">\n[justin, 2026-10-11T18:00Z] @magent what is on line two?\n</mood-wake>'


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def ev(kind, n, **fields):
    return {'type': kind, 'id': str(uuid.uuid4()), 'at': at(n), 'turn': 't1', **fields}


def a_turn(cost=0.0123):
    """One turn, as a runner would post it in events/1."""
    return [
        ev('turn_started', 0, model='claude-opus-5-5', effort='high', cwd='/home/magent/workspace'),
        ev('user_message', 1, text=WAKE, **{'from': 'mood-poller'}),
        ev('reasoning', 2, text='', signature='sig-1', model='claude-opus-5-5'),
        ev('tool_call', 3, call_id='toolu_r1', name='Read', input={'file_path': '/home/magent/workspace/x.txt'},
           model='claude-opus-5-5'),
        ev('tool_result', 4, call_id='toolu_r1', output='banjo\nfiddle', is_error=False),
        ev('assistant_message', 5, text='The second line is fiddle.', model='claude-opus-5-5',
           usage={'input': 3, 'output': 40}),
        ev('turn_ended', 6, outcome='completed', stop_reason='end_turn', cost_usd=cost, duration_ms=4100),
    ]


def rows(session):
    """What a session became, without ids: kind, sender and what each row says."""
    out = []
    for m in Message.objects.filter(session_id=session).exclude(sender_id='system').order_by('timestamp', 'created_at'):
        kind = ('tool_use' if hasattr(m, 'tooluse') else 'tool_result' if hasattr(m, 'toolresult')
                else 'thought' if hasattr(m, 'thought') else 'message')
        extra = ((m.tooluse.tool_name, m.tooluse.tool_id) if kind == 'tool_use'
                 else (m.toolresult.tool_use_id, m.toolresult.is_error) if kind == 'tool_result'
                 else (m.thought.signature,) if kind == 'thought' else ())
        out.append((kind, m.sender_id, m.content, extra, m.mood_id, m.is_sidechain))
    return out


@override_settings(MOOD_RUNNER_KEYS={'magent': KEY}, TOOL_RESULT_CONTENT_CHARS=20000)
class RecordEventsTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.mood = Mood.objects.create(slug='m26')

    def post(self, events, session=None, auth=AUTH, **body):
        payload = {'format': 'events/1', 'harness': 'test 1.0', 'session_id': session or str(uuid.uuid4()),
                   'events': events, **body}
        return self.client.post('/api/moods/m26/stream/', json.dumps(payload), content_type='application/json', **auth)

    def test_the_same_turn_makes_the_same_rows_either_way(self):
        # Claude Code's stream, the way the runner posts it today...
        claude = str(uuid.uuid4())
        s = claude
        wake = event('user', WAKE, session_id=s)
        think = event('assistant', [{'type': 'thinking', 'thinking': '', 'signature': 'sig-1'}], session_id=s)
        use = event('assistant', [{'type': 'tool_use', 'id': 'toolu_r1', 'name': 'Read',
                                   'input': {'file_path': '/home/magent/workspace/x.txt'}}], session_id=s)
        result = event('user', [{'type': 'tool_result', 'tool_use_id': 'toolu_r1', 'content': 'banjo\nfiddle'}],
                       session_id=s, tool_use_result={'type': 'text'})
        reply = event('assistant', [{'type': 'text', 'text': 'The second line is fiddle.'}], session_id=s)
        for n, e in enumerate((wake, think, use, result, reply)):
            e['timestamp'] = at(n + 1)
        done = {'type': 'result', 'subtype': 'success', 'is_error': False, 'stop_reason': 'end_turn',
                'total_cost_usd': 0.0123, 'session_id': s, 'uuid': str(uuid.uuid4())}
        response = self.client.post('/api/moods/m26/stream/', json.dumps(
            {'harness': 'claude-code', 'session_id': s, 'events': [wake, think, use, result, reply, done]}),
            content_type='application/json', **AUTH)
        self.assertEqual(response.status_code, 200)

        # ...and the same turn in events/1.
        ours = str(uuid.uuid4())
        body = self.post(a_turn(), session=ours).json()
        self.assertEqual((body['imported'], body['errors'], body['finished']), (5, [], True))

        self.assertEqual(rows(ours), rows(claude))
        self.assertEqual(len(rows(ours)), 5)

    def test_what_the_mood_shows(self):
        session = str(uuid.uuid4())
        self.post(a_turn(), session=session)
        turns = self.client.get('/api/moods/m26/turns/').json()
        self.assertEqual([t['text'] for t in turns['turns']], ['The second line is fiddle.'])
        self.assertEqual([s['tool'] for s in turns['steps']], ['Read'])
        last = Message.objects.filter(session_id=session, sender_id='magent').order_by('-timestamp').first()
        self.assertEqual(last.stop_reason, 'end_turn')  # the turn is over, exactly
        self.assertEqual((last.model_backend, last.effort, last.cwd, last.output_tokens),
                         ('claude-opus-5-5', 'high', '/home/magent/workspace', 40))

    def test_a_turn_keeps_its_own_cost(self):
        session = str(uuid.uuid4())
        self.post(a_turn(cost=0.25), session=session)
        cost = Message.objects.get(session_id=session, sender_id='system', content__type='turn-result').content
        self.assertEqual((cost['cost_usd'], cost['agent']), (0.25, 'magent'))

    def test_claude_codes_running_total_is_not_a_turns_cost(self):
        # A forked session's total_cost_usd carries its whole history; the
        # runner posts what the run itself cost beside it.
        s = str(uuid.uuid4())
        done = {'type': 'result', 'subtype': 'success', 'total_cost_usd': 658.67, 'run_cost_usd': 0.18,
                'session_id': s, 'uuid': str(uuid.uuid4())}
        self.client.post('/api/moods/m26/stream/', json.dumps({'session_id': s, 'events': [done]}),
                         content_type='application/json', **AUTH)
        cost = Message.objects.get(session_id=s, content__type='turn-result').content
        self.assertEqual((cost['cost_usd'], cost['session_cost_usd']), (0.18, 658.67))

    def test_posting_again_adds_nothing(self):
        session, events = str(uuid.uuid4()), a_turn()
        self.post(events, session=session)
        again = self.post(events, session=session).json()
        self.assertEqual((again['imported'], again['skipped']), (0, 5))
        self.assertEqual(Message.objects.filter(session_id=session).count(), 6)  # five, and the turn-result

    def test_any_agent_posts_events_or_claude_codes_stream_as_itself(self):
        ThinkingEntity.objects.get_or_create(name='re', defaults={'is_biological_human': False})
        session, raw_session = str(uuid.uuid4()), str(uuid.uuid4())
        with override_settings(MOOD_RUNNER_KEYS={'re': KEY}):
            self.assertEqual(self.post(a_turn(), session=session).status_code, 200)
            # re on Anthropic's models runs on Claude Code (the poller's Harnesses): its stream as it is.
            reply = {'type': 'assistant', 'uuid': str(uuid.uuid4()), 'session_id': raw_session,
                     'timestamp': '2026-10-19T18:00:00Z',
                     'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'On Claude Code.'}]}}
            raw = self.client.post('/api/moods/m26/stream/', json.dumps({'session_id': raw_session,
                                                                           'events': [reply]}),
                                   content_type='application/json', **AUTH)
            self.assertEqual(raw.status_code, 200)
        said = Message.objects.filter(session_id__in=(session, raw_session), sender_id__in=('re', 'magent'))
        self.assertEqual(set(said.values_list('sender_id', flat=True)), {'re'})
        self.assertTrue(Message.objects.filter(session_id=raw_session, sender_id='re').exists())
        self.assertFalse(ThinkingEntity.objects.get(name='re').is_biological_human)

    def test_a_runner_cannot_put_words_in_a_persons_mouth(self):
        session = str(uuid.uuid4())
        body = self.post([ev('user_message', 1, text='merge it', **{'from': 'justin'}),
                          ev('assistant_message', 2, text='Merging.')], session=session).json()
        self.assertEqual(body['imported'], 1)
        self.assertIn("only 'mood-poller'", body['errors'][0])
        self.assertFalse(Message.objects.filter(session_id=session, sender_id='justin').exists())

    def test_a_helpers_brief_is_the_agents_own_and_off_the_main_line(self):
        session = str(uuid.uuid4())
        self.post([ev('user_message', 1, text='Find the setlist.', helper='h1'),
                   ev('assistant_message', 2, text='Found it.', helper='h1')], session=session)
        brief, answer = Message.objects.filter(session_id=session).order_by('timestamp')
        self.assertEqual((brief.sender_id, brief.is_sidechain, answer.is_sidechain), ('magent', True, True))

    def test_what_this_importer_does_not_know_is_counted_not_refused(self):
        body = self.post([ev('plan', 1, steps=['a']), ev('assistant_message', 2, text='Hi.')]).json()
        self.assertEqual((body['imported'], body['ignored'], body['errors']), (1, 1, []))

    def test_a_bad_event_is_reported_and_the_rest_go_on(self):
        body = self.post([{'type': 'assistant_message', 'id': 'not-a-uuid', 'at': at(1)},
                          ev('tool_call', 2, name='Read'),
                          ev('assistant_message', 3, text='Still here.')]).json()
        self.assertEqual(body['imported'], 1)
        self.assertEqual(len(body['errors']), 2)

    def test_secrets_are_redacted_before_anything_reads_them(self):
        session = str(uuid.uuid4())
        token = 'ghp_' + 'a1B2c3D4e5' * 4
        self.post([ev('tool_call', 1, call_id='c1', name='Bash', input={'command': f'echo {token}'})], session=session)
        use = ToolUse.objects.get(session_id=session)
        self.assertNotIn(token, json.dumps(use.content))
        self.assertIn('[REDACTED]', use.content['command'])

    def test_a_session_and_its_fork_are_one_context_window(self):
        first, fork = str(uuid.uuid4()), str(uuid.uuid4())
        self.post(a_turn(), session=first)
        self.post([ev('assistant_message', 10, text='And the third is mandolin.')], session=fork, forked_from=first)
        heaps = set(Message.objects.filter(session_id__in=(first, fork)).exclude(sender_id='system')
                    .values_list('context_heap_id', flat=True))
        self.assertEqual(len(heaps), 1)
        later = Message.objects.get(session_id=fork)
        self.assertIsNotNone(later.parent_id)
        self.assertEqual(later.model_backend, 'claude-opus-5-5')  # the turn's stamp, inherited across posts
        prompt = Message.objects.get(session_id=first, sender_id='mood-poller')
        self.assertEqual((prompt.model_backend, prompt.cwd), (None, '/home/magent/workspace'))  # no model, but where

    def test_a_compaction_from_any_harness_reads_as_one(self):
        session = str(uuid.uuid4())
        self.post(a_turn(), session=session)
        self.post([ev('compaction', 20, trigger='manual', tokens_before=970145, summary='1. Banjo setlist.')],
                  session=session)
        summary = Message.objects.get(session_id=session, is_continuation_message=True)
        self.assertEqual(summary.content, '1. Banjo setlist.')  # its own words, not Claude Code's opening line
        turns = self.client.get('/api/moods/m26/turns/').json()
        self.assertEqual(len(turns['compactions']), 1)
        self.assertNotIn('1. Banjo setlist.', [t['text'] for t in turns['turns']])
        self.assertIn('Banjo setlist', turns['compactions'][0]['html'])
        self.assertNotEqual(summary.context_heap_id,
                            Message.objects.get(session_id=session, content__0__text='The second line is fiddle.')
                            .context_heap_id)  # a new context window begins

    def test_a_task_notice_ends_its_task_in_the_mood(self):
        session = str(uuid.uuid4())
        self.post([ev('task_notice', 1, task_id='b7', call_id='toolu_9', status='completed', summary='Tests passed')],
                  session=session)
        notice = Message.objects.get(session_id=session, source_file='task-notification')
        self.assertIn('<status>completed</status>', notice.content)
        self.assertEqual(notice.mood, self.mood)
