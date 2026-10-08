"""GitHub's webhook: a merge ticks a to-do list at once, and open pages hear of it at their next poll."""

import hashlib
import hmac
import json
from unittest import mock

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

    def test_only_with_the_signature(self):
        body = {'action': 'closed', 'pull_request': {'number': 1, 'merged': True}, 'repository': {'full_name': 'a/b'}}
        self.assertEqual(self.hook(body, signature='sha256=' + '0' * 64).status_code, 403)
        self.assertEqual(self.hook({'zen': 'hi'}, event='ping').json(), {'ok': True, 'event': 'ping'})
        unmerged = {'action': 'closed', 'pull_request': {'number': 2, 'merged': False}, 'repository': {'full_name': 'a/b'}}
        self.assertNotIn('ticked', self.hook(unmerged).json())
        with override_settings(GITHUB_WEBHOOK_SECRET=''):
            self.assertEqual(self.hook(body).status_code, 503)
