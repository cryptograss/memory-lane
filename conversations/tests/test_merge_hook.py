"""GitHub's webhook: a merge ticks a to-do list at once, and open pages hear of it at their next poll."""

import hashlib
import hmac
import json
from unittest import mock
from urllib.parse import urlencode

from django.core.cache import cache
from django.test import TestCase, override_settings

from conversations.models import Mood
from conversations.services import todo

SECRET = 'not-a-real-secret'
PAGE = """<pre>
- task: Merge memory-lane#131
  link: https://github.com/jMyles/memory-lane/pull/131
</pre>"""


def signed(body):
    raw = json.dumps(body).encode()
    return raw, 'sha256=' + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


@override_settings(GITHUB_WEBHOOK_SECRET=SECRET)
class MergeHookTest(TestCase):

    def setUp(self):
        cache.clear()
        quiet = mock.patch.object(todo, '_in_background', lambda fn, *args: None)  # no thread into the test database
        quiet.start()
        self.addCleanup(quiet.stop)
        self.mood = Mood.objects.create(slug='magenta-interface', title='magenta interface')

    def hook(self, body, signature=None, event='pull_request'):
        raw, good = signed(body)
        return self.client.post('/api/github/hook/', raw, content_type='application/json',
                                HTTP_X_HUB_SIGNATURE_256=signature or good, HTTP_X_GITHUB_EVENT=event)

    def test_a_merge_ticks_the_list_now_and_open_pages_are_told(self):
        open_still = mock.Mock(return_value=None)  # GitHub, asked, says nothing merged
        with mock.patch.object(todo, '_raw', return_value=PAGE), mock.patch.object(todo, '_github', open_still):
            self.assertFalse(todo.for_mood(self.mood)['items'][0]['done'])
            before = self.client.get('/api/moods/magenta-interface/turns/').json()['todo_stamp']
            merged = {'action': 'closed', 'pull_request': {'number': 131, 'merged': True},
                      'repository': {'full_name': 'jMyles/memory-lane'}}
            self.assertEqual(self.hook(merged).json(), {'ticked': 'jMyles/memory-lane#131'})
            item = todo.for_mood(self.mood)['items'][0]
            self.assertEqual((item['done'], item.get('merged')), (True, True))
            self.assertNotEqual(self.client.get('/api/moods/magenta-interface/turns/').json()['todo_stamp'], before)

    def test_a_form_delivery_too_and_the_mood_is_told(self):
        """GitHub's default for a new webhook is a form, the JSON in "payload": read as well. And the
        Mood whose list links the pull request gets a line saying it merged; the last delivery is kept."""
        merged = {'action': 'closed', 'pull_request': {'number': 131, 'merged': True, 'title': 'Moods: #125 to #130 in one',
                                                       'merged_by': {'login': 'jMyles'}},
                  'repository': {'full_name': 'jMyles/memory-lane'}}
        raw = urlencode({'payload': json.dumps(merged)}).encode()
        signature = 'sha256=' + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        with mock.patch.object(todo, '_raw', return_value=PAGE), mock.patch.object(todo, '_github', mock.Mock(return_value=None)), \
                mock.patch.object(todo, '_in_background', lambda fn, *args: fn(*args)):
            answer = self.client.post('/api/github/hook/', raw, content_type='application/x-www-form-urlencoded',
                                      HTTP_X_HUB_SIGNATURE_256=signature, HTTP_X_GITHUB_EVENT='pull_request')
            self.assertEqual(answer.json(), {'ticked': 'jMyles/memory-lane#131'})
            again = self.client.post('/api/github/hook/', raw, content_type='application/x-www-form-urlencoded',
                                     HTTP_X_HUB_SIGNATURE_256=signature, HTTP_X_GITHUB_EVENT='pull_request')
            self.assertEqual(again.status_code, 200)
            told = self.client.get('/api/moods/magenta-interface/todo/').json()
        self.assertEqual(told['hook']['outcome'], 'ticked jMyles/memory-lane#131')
        events = [e for e in self.client.get('/api/moods/magenta-interface/turns/').json()['events'] if e['type'] == 'merged']
        self.assertEqual([(e['title'], e['by'], e['about']) for e in events],
                         [('Moods: #125 to #130 in one', 'jMyles', 'Merge memory-lane#131')])  # once, sent twice

    def test_only_with_the_signature(self):
        body = {'action': 'closed', 'pull_request': {'number': 1, 'merged': True}, 'repository': {'full_name': 'a/b'}}
        self.assertEqual(self.hook(body, signature='sha256=' + '0' * 64).status_code, 403)
        self.assertEqual(todo.last_refused()['outcome'], 'signed')  # another secret: said, apart
        self.assertEqual(self.hook({'zen': 'hi'}, event='ping').json(), {'ok': True, 'event': 'ping'})
        self.assertEqual(todo.last_heard()['event'], 'ping')
        unmerged = {'action': 'closed', 'pull_request': {'number': 2, 'merged': False}, 'repository': {'full_name': 'a/b'}}
        self.assertNotIn('ticked', self.hook(unmerged).json())
        with override_settings(GITHUB_WEBHOOK_SECRET=''):
            self.assertEqual(self.hook(body).status_code, 503)


@override_settings(GITHUB_WEBHOOK_SECRET=SECRET)
class MovedRepositoryTest(TestCase):
    """memory-lane moved to the cryptograss org: GitHub says cryptograss/memory-lane#131,
    and the list's link still says jMyles/memory-lane/pull/131. The same pull request."""

    def setUp(self):
        cache.clear()
        self.mood = Mood.objects.create(slug='magenta-interface', title='magenta interface')

    def test_a_merge_in_the_new_place_ticks_and_tells_the_old_link(self):
        merged = {'action': 'closed', 'pull_request': {'number': 131, 'merged': True, 'title': 'One',
                                                       'merged_by': {'login': 'jMyles'}},
                  'repository': {'full_name': 'cryptograss/memory-lane'}}
        raw, good = signed(merged)
        with mock.patch.object(todo, '_raw', return_value=PAGE), mock.patch.object(todo, '_github', mock.Mock(return_value=None)), \
                mock.patch.object(todo, '_in_background', lambda fn, *args: fn(*args) if fn is todo.announce_merge else None):
            self.client.post('/api/github/hook/', raw, content_type='application/json',
                             HTTP_X_HUB_SIGNATURE_256=good, HTTP_X_GITHUB_EVENT='pull_request')
            self.assertTrue(todo.for_mood(self.mood)['items'][0]['done'])
        events = [e for e in self.client.get('/api/moods/magenta-interface/turns/').json()['events'] if e['type'] == 'merged']
        self.assertEqual(len(events), 1)

    def test_open_work_matches_what_was_said_before_it_moved(self):
        from conversations.services import repos, work
        self.assertEqual(repos.same('jMyles/memory-lane'), 'cryptograss/memory-lane')
        self.assertEqual(repos.same('cryptograss/pickipedia'), 'cryptograss/pickipedia')
        import requests
        refused = requests.HTTPError(response=mock.Mock(status_code=422))
        asked = []

        def search(query):
            asked.append(query)
            if 'repo:jMyles' in query:
                raise refused
            return []
        with mock.patch.object(work, '_search', search):
            self.assertEqual(work._scoped('is:pr is:open'), [])
        self.assertEqual(asked[-1], 'is:pr is:open org:cryptograss')  # the old names refused: the org alone
