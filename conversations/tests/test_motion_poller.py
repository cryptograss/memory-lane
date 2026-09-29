"""The poller wakes a turn only when one is owed, and never on top of a live session.

No Django, no network, no model: the API and the waker are fakes.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from poller.motion_poller import SILENT, ClaudeCodeWaker, MotionPoller

T0 = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)


def turn(id, sender, minutes, text='@magent are you there?'):
    return {'id': id, 'sender': sender, 'created_at': (T0 + timedelta(minutes=minutes)).isoformat(),
            'text': text}


class FakeAPI:
    def __init__(self, mentions=(), turns=None, sessions=None):
        self._mentions = list(mentions)
        self._turns = turns or {}
        self._sessions = sessions or {}
        self.since_seen = []

    def mentions(self, agent, since=None):
        self.since_seen.append(since)
        return [m for m in self._mentions if since is None or m['turn']['created_at'] > since]

    def turns_after(self, slug, message_id):
        return self._turns.get(slug, [])

    def sessions(self, slug, sender):
        return self._sessions.get(slug, [])


class FakeWaker:
    def __init__(self, local=('s-local',), reply='on it'):
        self.local = set(local)
        self.reply = reply
        self.woken = []

    def can_wake(self, session_id):
        return session_id in self.local

    def wake(self, session_id, prompt):
        self.woken.append((session_id, prompt))
        return 'fork-1', self.reply


def mention(motion, t):
    return {'motion': motion, 'turn': t}


class MotionPollerTest(TestCase):

    def make(self, api, waker=None, minutes_now=15, **kwargs):
        self.clock = [T0 + timedelta(minutes=minutes_now)]
        state = Path(tempfile.mkdtemp()) / 'state.json'
        state.write_text(json.dumps({'since': (T0 - timedelta(hours=1)).isoformat(), 'handled': [], 'wakes': []}))
        self.waker = waker or FakeWaker()
        return MotionPoller(api, self.waker, state_path=state, now=lambda: self.clock[0], **kwargs)

    def test_unanswered_mention_wakes_the_agents_last_local_session(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-remote', 's-local']})
        poller = self.make(api)

        self.assertEqual(poller.poll_once(), ['m26'])
        session, prompt = self.waker.woken[0]
        self.assertEqual(session, 's-local')
        self.assertTrue(prompt.startswith('<motion-wake motion="m26">'))
        self.assertIn('[skyler, 2026-09-29T18:00Z] @magent are you there?', prompt)
        self.assertIn(SILENT, prompt)

    def test_a_live_session_that_answered_is_left_alone(self):
        api = FakeAPI([mention('m26', turn('a', 'justin', 0))],
                      turns={'m26': [turn('b', 'magent', 1, 'here')]}, sessions={'m26': ['s-local']})
        poller = self.make(api)

        self.assertEqual(poller.poll_once(), [])
        self.assertEqual(self.waker.woken, [])

    def test_waits_out_the_grace_period_then_wakes(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-local']})
        poller = self.make(api, minutes_now=5)

        self.assertEqual(poller.poll_once(), [])
        self.clock[0] = T0 + timedelta(minutes=11)
        self.assertEqual(poller.poll_once(), ['m26'])

    def test_each_mention_is_answered_once(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-local']})
        poller = self.make(api)
        poller.poll_once()
        poller.poll_once()
        self.assertEqual(len(self.waker.woken), 1)

    def test_state_survives_a_restart(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-local']})
        poller = self.make(api)
        poller.poll_once()
        again = MotionPoller(api, self.waker, state_path=poller.state_path, now=lambda: self.clock[0])
        again.poll_once()
        self.assertEqual(len(self.waker.woken), 1)

    def test_mentions_in_one_motion_share_one_turn(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0)), mention('m26', turn('b', 'justin', 2, '@magent and?'))],
                      sessions={'m26': ['s-local']})
        poller = self.make(api)
        poller.poll_once()
        self.assertEqual(len(self.waker.woken), 1)
        self.assertIn('@magent and?', self.waker.woken[0][1])

    def test_the_agent_never_wakes_itself(self):
        api = FakeAPI([mention('m26', turn('a', 'magent', 0, 'as @magent said'))], sessions={'m26': ['s-local']})
        self.make(api).poll_once()
        self.assertEqual(self.waker.woken, [])

    def test_wakes_are_capped_per_hour(self):
        api = FakeAPI([mention(f'm{i}', turn(f't{i}', 'skyler', 0)) for i in range(3)],
                      sessions={f'm{i}': ['s-local'] for i in range(3)})
        poller = self.make(api, max_wakes_per_hour=2)
        poller.poll_once()
        self.assertEqual(len(self.waker.woken), 2)
        self.clock[0] += timedelta(minutes=61)
        poller.poll_once()
        self.assertEqual(len(self.waker.woken), 3)

    def test_a_session_on_another_machine_is_not_this_pollers_to_wake(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-remote']})
        self.make(api).poll_once()
        self.assertEqual(self.waker.woken, [])

    def test_dry_run_wakes_nothing_and_saves_nothing(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-local']})
        poller = self.make(api, dry_run=True)
        before = poller.state_path.read_text()
        poller.poll_once()
        self.assertEqual(self.waker.woken, [])
        self.assertEqual(poller.state_path.read_text(), before)

    def test_since_does_not_pass_a_mention_still_owed(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0)), mention('m27', turn('b', 'skyler', 12))],
                      sessions={'m26': ['s-local'], 'm27': ['s-local']})
        poller = self.make(api)
        poller.poll_once()  # m26 woken; m27 still inside its grace period
        self.assertEqual(poller.state['since'], turn('a', 'skyler', 0)['created_at'])
        self.clock[0] = T0 + timedelta(minutes=23)
        poller.poll_once()
        self.assertEqual([s for s, _ in self.waker.woken], ['s-local', 's-local'])

    def test_first_run_starts_from_now_not_history(self):
        poller = MotionPoller(FakeAPI(), FakeWaker(), state_path=None, now=lambda: T0)
        self.assertEqual(poller.state['since'], T0.isoformat())


class ClaudeCodeWakerTest(TestCase):

    def test_forks_under_a_new_id_with_read_only_tools(self):
        cmd = ClaudeCodeWaker(claude='claude').command('s-old', 's-new', '<motion-wake>')
        self.assertEqual(cmd[:7], ['claude', '-p', '--resume', 's-old', '--fork-session', '--session-id', 's-new'])
        self.assertEqual(cmd[cmd.index('--tools') + 1], 'Read,Grep,Glob')
        self.assertEqual(cmd[cmd.index('--permission-mode') + 1], 'dontAsk')
        self.assertIn('mcp__pickipedia__update-page', cmd)
        self.assertEqual(cmd[-2:], ['--', '<motion-wake>'])

    def test_finds_a_local_session_and_its_last_cwd(self):
        projects = Path(tempfile.mkdtemp())
        (projects / '-home-magent').mkdir()
        path = projects / '-home-magent' / 'abc.jsonl'
        path.write_text('{"cwd": "/one"}\n{"type": "attachment"}\n{"cwd": "/two"}\n')
        waker = ClaudeCodeWaker(projects_dir=projects)
        self.assertTrue(waker.can_wake('abc'))
        self.assertFalse(waker.can_wake('nope'))
        self.assertEqual(waker.cwd_of(path), '/two')
