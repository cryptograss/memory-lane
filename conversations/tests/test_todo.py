"""A Mood's to-do list: YAML on PickiPedia, shown beside the Mood, pointed to in every wake."""

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Mood, MoodAlias
from conversations.services import todo

PAGE = """A running list for #magenta-interface.
<pre>
- task: Merge memory-lane#113
  who: justin
  kind: merge
  link: https://github.com/jMyles/memory-lane/pull/113
- task: Redeploy maybelle
  kind: deploy
  who: [justin, skyler]
  done: true
- Tick high-volume on the deploy-logs bot password
- note: no task, so not an item
</pre>
[[Category:Mood to-do lists]]
"""


class Answer:
    def __init__(self, status, text='', body=None):
        self.status_code, self.text, self.body = status, text, body

    def json(self):
        return self.body


class FakeWiki:
    """PickiPedia's raw pages, and GitHub's pull requests (closed: {number: merged?}; single: {number: merged?})."""

    def __init__(self, pages, closed=None, single=None, github_down=False):
        self.pages, self.asked = pages, []
        self.closed, self.single, self.github_down, self.github = closed or {}, single or {}, github_down, []

    def get(self, url, params=None, **kw):
        if url.startswith('https://api.github.com/'):
            self.github.append(url)
            if self.github_down:
                raise ConnectionError('down')
            if url.endswith('/pulls'):
                return Answer(200, '', [{'number': n, 'merged_at': '2026-10-08T15:22:36Z' if m else None}
                                       for n, m in self.closed.items()])
            number = int(url.rsplit('/', 1)[1])
            return Answer(200, '', {'number': number, 'merged_at': '2026-10-08T15:22:36Z' if self.single.get(number) else None})
        self.asked.append(params['title'])
        return Answer(200, self.pages[params['title']]) if params['title'] in self.pages else Answer(404)


class ParseTest(TestCase):

    def test_the_list_in_its_pre(self):
        items = todo.parse(PAGE)
        self.assertEqual([i['task'] for i in items],
                         ['Merge memory-lane#113', 'Redeploy maybelle', 'Tick high-volume on the deploy-logs bot password'])
        self.assertEqual((items[0]['who'], items[0]['kind'], items[0]['done']), (['justin'], 'merge', False))
        self.assertEqual((items[1]['who'], items[1]['done']), (['justin', 'skyler'], True))

    def test_a_list_in_its_todo_tags(self):
        items = todo.parse(PAGE.replace('<pre>', '<todo>').replace('</pre>', '</todo>'))
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]['link'], 'https://github.com/jMyles/memory-lane/pull/113')

    def test_a_list_without_its_pre_and_an_empty_one(self):
        self.assertEqual(todo.parse('- task: one\n  who: a, b')[0]['who'], ['a', 'b'])
        self.assertEqual(todo.parse('<pre>\n</pre>'), [])

    def test_what_cant_be_read_says_why(self):
        with self.assertRaisesRegex(todo.Unreadable, 'line 2'):
            todo.parse('<pre>\n- task: one\n  who: [unclosed\n</pre>')
        with self.assertRaisesRegex(todo.Unreadable, 'a list'):
            todo.parse('<pre>\ntask: one\n</pre>')


class ForMoodTest(TestCase):

    def setUp(self):
        cache.clear()
        self.mood = Mood.objects.create(slug='magenta-interface', title='magenta-interface')

    def test_the_list_its_page_and_kept_a_minute(self):
        wiki = FakeWiki({'Cryptograss:Moods/magenta-interface/todo': PAGE})
        found = todo.for_mood(self.mood, http=wiki)
        self.assertTrue(found['exists'])
        self.assertEqual(len(found['items']), 3)
        self.assertEqual(found['page'], 'https://pickipedia.xyz/wiki/Cryptograss%3AMoods/magenta-interface/todo')
        todo.for_mood(self.mood, http=wiki)
        self.assertEqual(len(wiki.asked), 1)

    def test_a_renamed_moods_list_is_found_under_its_old_name(self):
        MoodAlias.objects.create(mood=self.mood, slug='motions')
        wiki = FakeWiki({'Cryptograss:Moods/motions/todo': PAGE})
        found = todo.for_mood(self.mood, http=wiki)
        self.assertEqual((found['exists'], len(found['items'])), (True, 3))
        self.assertIn('motions', found['page'])

    def test_no_page_yet_and_a_page_that_cant_be_read(self):
        self.assertEqual(todo.for_mood(self.mood, http=FakeWiki({}))['exists'], False)
        cache.clear()
        broken = todo.for_mood(self.mood, http=FakeWiki({'Cryptograss:Moods/magenta-interface/todo': '<pre>\n- [x\n</pre>'}))
        self.assertIn('problem', broken['error'])

    def test_a_merged_pull_request_ticks_itself(self):
        page = PAGE.replace('<pre>', '<pre>\n- task: Merge maybelle-config#167\n  link: https://github.com/cryptograss/maybelle-config/pull/167')
        wiki = FakeWiki({'Cryptograss:Moods/magenta-interface/todo': page},
                        closed={113: True, 99: False}, single={167: True})
        items = {i['task']: i for i in todo.for_mood(self.mood, http=wiki)['items']}
        self.assertEqual((items['Merge memory-lane#113']['done'], items['Merge memory-lane#113']['merged']), (True, True))
        self.assertTrue(items['Merge maybelle-config#167']['merged'])  # not among the recent: asked on its own
        self.assertNotIn('merged', items['Redeploy maybelle'])  # done by hand, as written
        asked = len(wiki.github)
        cache.delete('todo:magenta-interface')
        todo.for_mood(self.mood, http=wiki)
        self.assertEqual(len(wiki.github), asked)  # kept: GitHub isn't asked again for a while

    def test_an_open_one_stays_open_and_github_down_changes_nothing(self):
        wiki = FakeWiki({'Cryptograss:Moods/magenta-interface/todo': PAGE}, closed={})
        self.assertFalse(todo.for_mood(self.mood, http=wiki)['items'][0]['done'])
        cache.clear()
        down = FakeWiki({'Cryptograss:Moods/magenta-interface/todo': PAGE}, github_down=True)
        self.assertFalse(todo.for_mood(self.mood, http=down)['items'][0]['done'])

    def test_the_endpoint(self):
        cache.set('todo:magenta-interface', {'page': 'p', 'edit': 'e', 'exists': True, 'items': []}, 60)
        self.assertEqual(Client().get('/api/moods/magenta-interface/todo/').json()['edit'], 'e')
        self.assertEqual(Client().get('/api/moods/nowhere/todo/').status_code, 404)


class WakeTest(TestCase):

    def test_every_wake_points_to_the_moods_list(self):
        from poller.mood_poller import rules_block, wake_frames
        self.assertIn('Cryptograss:Moods/general/todo', '\n'.join(rules_block('', 'normal', 'general')))
        for frame in wake_frames('general'):
            if frame['kind'] != 'screen':
                self.assertIn('Cryptograss:Moods/general/todo', frame['text'], frame['kind'])
