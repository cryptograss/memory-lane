"""The poller wakes a turn only when one is owed, and never on top of a live session.

No Django, no network, no model: the API and the waker are fakes.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from poller.motion_poller import SILENT, ClaudeCodeWaker, MotionPoller, project_dir_name

T0 = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)


def turn(id, sender, minutes, text='@magent are you there?'):
    return {'id': id, 'sender': sender, 'created_at': (T0 + timedelta(minutes=minutes)).isoformat(),
            'text': text}


class FakeAPI:
    def __init__(self, mentions=(), turns=None, sessions=None):
        self._mentions = list(mentions)
        self._turns = turns or {}
        self._sessions = sessions or {}

    def mentions(self, agent, since=None):
        return [m for m in self._mentions if since is None or m['turn']['created_at'] > since]

    def turns_after(self, slug, message_id):
        everything = self._turns.get(slug, []) + [m['turn'] for m in self._mentions if m['motion'] == slug]
        after = next((t['created_at'] for t in everything if t['id'] == message_id), None)
        return [t for t in self._turns.get(slug, []) if after is None or t['created_at'] > after]

    def sessions(self, slug, sender):
        return self._sessions.get(slug, [])


class FakeWaker:
    def __init__(self, local=('s-local',), reply='on it', fail=()):
        self.local = set(local)
        self.reply = reply
        self.fail = set(fail)
        self.woken = []

    def can_wake(self, session_id):
        return session_id in self.local

    def wake(self, session_id, prompt):
        self.woken.append((session_id, prompt))
        if session_id in self.fail:
            raise RuntimeError('No conversation found')
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

    def test_unanswered_mention_wakes_the_agents_newest_session(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-local', 's-older']})
        poller = self.make(api)

        self.assertEqual(poller.poll_once(), ['m26'])
        session, prompt = self.waker.woken[0]
        self.assertEqual(session, 's-local')
        self.assertTrue(prompt.startswith('<motion-wake motion="m26">'))
        self.assertIn('[skyler, 2026-09-29T18:00Z] @magent are you there?', prompt)
        self.assertIn(SILENT, prompt)

    def test_only_the_poller_holding_the_newest_session_answers(self):
        # Two containers each hold a session in the Motion; only one may speak.
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-remote', 's-local']})
        poller = self.make(api)
        self.assertEqual(poller.poll_once(), [])
        self.assertEqual(self.waker.woken, [])

    def test_a_live_session_that_answered_is_left_alone(self):
        api = FakeAPI([mention('m26', turn('a', 'justin', 0))],
                      turns={'m26': [turn('b', 'magent', 1, 'here')]}, sessions={'m26': ['s-local']})
        poller = self.make(api)

        self.assertEqual(poller.poll_once(), [])
        self.assertEqual(self.waker.woken, [])

    def test_a_follow_up_after_the_answer_is_still_owed(self):
        api = FakeAPI([mention('m26', turn('a', 'justin', 0)), mention('m26', turn('c', 'justin', 6, '@magent and?'))],
                      turns={'m26': [turn('b', 'magent', 5, 'here')]}, sessions={'m26': ['s-local']})
        poller = self.make(api, minutes_now=20)

        self.assertEqual(poller.poll_once(), ['m26'])
        prompt = self.waker.woken[0][1]
        self.assertIn('@magent and?', prompt)
        self.assertNotIn('are you there', prompt)

    def test_waits_out_the_grace_period_then_wakes(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-local']})
        poller = self.make(api, minutes_now=5)

        self.assertEqual(poller.poll_once(), [])
        self.clock[0] = T0 + timedelta(minutes=11)
        self.assertEqual(poller.poll_once(), ['m26'])

    def test_a_mention_from_the_web_wakes_at_once(self):
        web = dict(turn('a', 'skyler', 0), via='web')
        api = FakeAPI([mention('m26', web)], sessions={'m26': ['s-local']})
        poller = self.make(api, minutes_now=0)
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

    def test_a_restart_during_the_turn_does_not_wake_twice(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0))], sessions={'m26': ['s-local']})
        poller = self.make(api)
        seen = {}

        class Interrupted(FakeWaker):
            def wake(inner, session_id, prompt):
                # Another process starting now reads the state file as it is.
                seen['state'] = json.loads(poller.state_path.read_text())
                return super().wake(session_id, prompt)

        poller.waker = Interrupted()
        poller.poll_once()
        self.assertIn('a', seen['state']['handled'])
        self.assertEqual(len(seen['state']['wakes']), 1)

    def test_a_failed_wake_is_not_retried_and_does_not_block_other_motions(self):
        api = FakeAPI([mention('m1', turn('a', 'skyler', 0)), mention('m2', turn('b', 'skyler', 0))],
                      sessions={'m1': ['s-broken'], 'm2': ['s-local']})
        waker = FakeWaker(local=('s-broken', 's-local'), fail=('s-broken',))
        poller = self.make(api, waker=waker)

        self.assertEqual(poller.poll_once(), ['m2'])
        poller.poll_once()
        self.assertEqual([s for s, _ in waker.woken], ['s-broken', 's-local'])
        self.assertEqual(len(poller.state['wakes']), 2)  # failures count toward the cap

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

    def test_a_corrupt_state_file_starts_fresh(self):
        state = Path(tempfile.mkdtemp()) / 'state.json'
        state.write_text('{"since": ')
        poller = MotionPoller(FakeAPI(), FakeWaker(), state_path=state, now=lambda: T0)
        self.assertEqual(poller.state['since'], T0.isoformat())

    def test_a_huge_mention_is_cut_to_fit_an_argument(self):
        api = FakeAPI([mention('m26', turn('a', 'skyler', 0, '@magent ' + 'x' * 300_000))],
                      sessions={'m26': ['s-local']})
        poller = self.make(api)
        poller.poll_once()
        self.assertLess(len(self.waker.woken[0][1]), 110_000)


class ClaudeCodeWakerTest(TestCase):

    def test_forks_under_a_new_id_with_no_tools_at_all(self):
        cmd = ClaudeCodeWaker(claude='claude').command('s-old', 's-new', '<motion-wake>')
        self.assertEqual(cmd[:7], ['claude', '-p', '--resume', 's-old', '--fork-session', '--session-id', 's-new'])
        self.assertEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertIn('--strict-mcp-config', cmd)
        self.assertEqual(cmd[-2:], ['--', '<motion-wake>'])

    def test_resumes_from_the_directory_the_session_is_filed_under(self):
        # The session's last cwd is elsewhere (a worktree); resume must run
        # from the cwd whose project folder holds the file.
        home = Path(tempfile.mkdtemp())
        started, moved = home / 'workspace', home / 'workspace' / 'wt'
        moved.mkdir(parents=True)
        projects = home / 'projects'
        folder = projects / project_dir_name(str(started))
        folder.mkdir(parents=True)
        (folder / 'abc.jsonl').write_text(json.dumps({'cwd': str(started)}) + '\n'
                                          + '{"type": "attachment"}\n'
                                          + json.dumps({'cwd': str(moved)}) + '\n')
        waker = ClaudeCodeWaker(projects_dir=projects)
        self.assertEqual(waker.cwd_for('abc'), str(started))
        self.assertTrue(waker.can_wake('abc'))
        self.assertFalse(waker.can_wake('nope'))

    def test_a_session_whose_directory_is_gone_cannot_be_woken(self):
        projects = Path(tempfile.mkdtemp())
        gone = '/nonexistent/worktree'
        (projects / project_dir_name(gone)).mkdir()
        (projects / project_dir_name(gone) / 'abc.jsonl').write_text(json.dumps({'cwd': gone}) + '\n')
        self.assertFalse(ClaudeCodeWaker(projects_dir=projects).can_wake('abc'))

    def test_project_dir_name_matches_claude_codes(self):
        self.assertEqual(project_dir_name('/home/magent/workspace/memory-lane/.claude/worktrees/importer-cleanup'),
                         '-home-magent-workspace-memory-lane--claude-worktrees-importer-cleanup')
        self.assertTrue(os.path.isdir(os.path.expanduser('~/.claude/projects/' + project_dir_name(os.path.expanduser('~')))))
