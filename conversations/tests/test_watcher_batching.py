"""The watcher's batching must not lose lines.

Two threads share pending_lines: the filesystem-event thread appends and
the main loop's timer flushes. The original flush_batch POSTed
self.pending_lines and then assigned [] afterwards, so any line appended
during the request was silently wiped. That fires on the first burst of
lines after an idle period, which is exactly when a person's prompt
arrives; on 2026-09-29 it had dropped 2 of the last 12 human prompts.

These tests simulate the other thread by appending from inside the
mocked POST. No Django, no database, no filesystem watching.
"""

import os
import tempfile
from pathlib import Path
from unittest import TestCase, mock

import requests

from watcher.conversation_watcher import ConversationWatcher

POST = 'watcher.conversation_watcher.requests.post'


def _session_dir(jsonl_text):
    """A per-user watch directory holding one session file."""
    watch_dir = Path(tempfile.mkdtemp()) / 'project-logs' / 'justin'
    watch_dir.mkdir(parents=True)
    (watch_dir / 's.jsonl').write_text(jsonl_text)
    return watch_dir


def make_watcher(**overrides):
    kwargs = dict(
        watch_dir=tempfile.mkdtemp() + '/project-logs/justin',
        era=None,
        remote_endpoint='http://ingest.test/api/ingest/',
        batch_size=10,
        batch_interval=2.0,
    )
    kwargs.update(overrides)
    return ConversationWatcher(**kwargs)


def ok_response():
    response = mock.Mock()
    response.status_code = 200
    response.json.return_value = {'imported': 1, 'skipped': 0, 'errors': []}
    response.text = ''
    return response


class WatcherBatchingTest(TestCase):

    def test_line_appended_during_post_survives(self):
        watcher = make_watcher()
        watcher.pending_lines = ['{"a": 1}']

        def post_while_other_thread_appends(*args, **kwargs):
            watcher.import_line('{"b": 2}', 'x.jsonl')
            return ok_response()

        with mock.patch(POST, side_effect=post_while_other_thread_appends) as post:
            watcher.flush_batch()

        self.assertEqual(post.call_args.kwargs['json']['lines'], ['{"a": 1}'])
        self.assertEqual(watcher.pending_lines, ['{"b": 2}'])

    def test_failed_post_requeues_in_order(self):
        watcher = make_watcher()
        watcher.pending_lines = ['1', '2']
        with mock.patch(POST, side_effect=requests.ConnectionError('down')):
            watcher.flush_batch()
        watcher.import_line('3', 'x.jsonl')  # batch not full, interval not passed: no flush
        self.assertEqual(watcher.pending_lines, ['1', '2', '3'])

    def test_non_200_requeues(self):
        watcher = make_watcher()
        watcher.pending_lines = ['1']
        bad = mock.Mock()
        bad.status_code = 503
        bad.text = 'unavailable'
        with mock.patch(POST, return_value=bad):
            watcher.flush_batch()
        self.assertEqual(watcher.pending_lines, ['1'])

    def test_requeue_is_bounded_and_drops_oldest(self):
        watcher = make_watcher()
        watcher.max_pending = 3
        watcher.pending_lines = ['1', '2', '3', '4']
        with mock.patch(POST, side_effect=requests.ConnectionError('down')):
            watcher.flush_batch()
        self.assertEqual(watcher.pending_lines, ['2', '3', '4'])

    def test_empty_batch_does_not_post(self):
        watcher = make_watcher()
        with mock.patch(POST) as post:
            watcher.flush_batch()
        post.assert_not_called()

    def test_full_batch_flushes_once_with_every_line(self):
        watcher = make_watcher(batch_size=3)
        with mock.patch(POST, return_value=ok_response()) as post:
            for i in range(3):
                watcher.import_line(str(i), 'x.jsonl')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['json']['lines'], ['0', '1', '2'])
        self.assertEqual(watcher.pending_lines, [])

    def test_default_scan_tracks_from_end_without_posting(self):
        watch_dir = _session_dir('{"a": 1}\n{"b": 2}\n')
        watcher = make_watcher(watch_dir=str(watch_dir))
        with mock.patch(POST) as post, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('WATCHER_REPLAY_FROM_START', None)
            watcher.scan_existing_files()
        post.assert_not_called()
        self.assertEqual(watcher.file_positions[str(watch_dir / 's.jsonl')], len('{"a": 1}\n{"b": 2}\n'))

    def test_replay_ingests_every_existing_line_then_tracks_from_end(self):
        watch_dir = _session_dir('{"a": 1}\n{"b": 2}\n{"c": 3}\n')
        watcher = make_watcher(watch_dir=str(watch_dir), batch_size=2)
        with mock.patch(POST, return_value=ok_response()) as post, \
                mock.patch.dict(os.environ, {'WATCHER_REPLAY_FROM_START': '1'}):
            watcher.scan_existing_files()
        sent = [line for call in post.call_args_list for line in call.kwargs['json']['lines']]
        self.assertEqual(sent, ['{"a": 1}', '{"b": 2}', '{"c": 3}'])
        self.assertEqual(watcher.pending_lines, [])
        self.assertEqual(watcher.file_positions[str(watch_dir / 's.jsonl')], len('{"a": 1}\n{"b": 2}\n{"c": 3}\n'))

    def test_sent_count_is_logged_so_loss_is_visible(self):
        watcher = make_watcher()
        watcher.pending_lines = ['1', '2']
        with mock.patch(POST, return_value=ok_response()), \
                self.assertLogs('watcher', level='INFO') as logs:
            watcher.flush_batch()
        self.assertTrue(any('sent=2' in line for line in logs.output))
