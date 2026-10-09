"""A merged pull request's item, ticked on the wiki too: one line added, nothing else moved."""

import difflib
import os
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from conversations.models import Mood
from conversations.services import todo, todo_ticks

HERE = os.path.dirname(__file__)
LINK = 'https://github.com/jMyles/memory-lane/pull/138'
BOT = {'PICKIPEDIA_TODO_BOT_USER': 'DrivingThatTrain@magenta-todo', 'PICKIPEDIA_TODO_BOT_PASSWORD': 'not-a-password'}


class TickedTest(TestCase):

    def test_the_real_list_gains_one_line_and_loses_none(self):
        text = open(os.path.join(HERE, 'todo_page_sample.txt')).read()
        new = todo_ticks.ticked(text, LINK)
        changes = [d for d in difflib.ndiff(text.splitlines(), new.splitlines()) if d[:1] in '+-']
        self.assertEqual(changes, ['+   done: true'])
        item = next(i for i in todo.parse(new) if i['link'] == LINK)
        self.assertTrue(item['done'])
        self.assertEqual(todo_ticks.ticked(new, LINK), new)  # done already: as it is

    def test_done_false_turned_true_and_a_link_not_there_left_be(self):
        text = '<todo>\n- task: A\n  link: "https://github.com/a/b/pull/1"\n  done: false\n- task: B\n</todo>\n'
        self.assertEqual(todo_ticks.ticked(text, 'https://github.com/a/b/pull/1'),
                         '<todo>\n- task: A\n  link: "https://github.com/a/b/pull/1"\n  done: true\n- task: B\n</todo>\n')
        self.assertEqual(todo_ticks.ticked(text, 'https://github.com/a/b/pull/2'), text)


class FakeWiki:
    def __init__(self, text, conflicts=0, overwritten=0):
        self.text, self.conflicts, self.overwritten, self.edits, self.headers = text, conflicts, overwritten, [], {}

    def get(self, url, params=None, timeout=None):
        if params.get('type') == 'login':
            return mock.Mock(json=lambda: {'query': {'tokens': {'logintoken': 'L'}}})
        if params.get('meta') == 'tokens':
            return mock.Mock(json=lambda: {'query': {'tokens': {'csrftoken': 'C'}}})
        rev = {'revid': 9400 + len(self.edits), 'timestamp': 'T', 'slots': {'main': {'content': self.text}}}
        return mock.Mock(json=lambda: {'query': {'pages': [{'revisions': [rev]}]}})

    def post(self, url, data=None, timeout=None):
        if data['action'] == 'login':
            return mock.Mock(json=lambda: {'login': {'result': 'Success'}})
        self.edits.append(data)
        if self.conflicts:
            self.conflicts -= 1
            return mock.Mock(json=lambda: {'error': {'code': 'editconflict'}})
        if self.overwritten:  # "Success", then another edit in the same second puts the old text back
            self.overwritten -= 1
            return mock.Mock(json=lambda: {'edit': {'result': 'Success'}})
        self.text = data['text']
        return mock.Mock(json=lambda: {'edit': {'result': 'Success'}})


@override_settings(**BOT)
class TickTest(TestCase):

    def setUp(self):
        cache.clear()

    def test_a_bot_edit_marked_so_against_the_revision_read_once_more_on_a_conflict(self):
        wiki = FakeWiki(f'<todo>\n- task: Merge it\n  link: {LINK}\n</todo>\n', conflicts=1)
        self.assertTrue(todo_ticks.tick('Cryptograss:Moods/x/todo', LINK, http=wiki, settle=0))
        last = wiki.edits[-1]
        self.assertEqual((last['bot'], last['nocreate'], last['baserevid']), (1, 1, 9401))
        self.assertIn('  done: true', wiki.text)
        self.assertFalse(todo_ticks.tick('Cryptograss:Moods/x/todo', LINK, http=wiki, settle=0))  # done: nothing to edit

    def test_several_in_one_edit_and_again_if_one_didnt_stay(self):
        other = 'https://github.com/jMyles/memory-lane/pull/139'
        page = f'<todo>\n- task: A\n  link: {other}\n- task: B\n  link: {LINK}\n</todo>\n'
        wiki = FakeWiki(page, overwritten=1)
        self.assertTrue(todo_ticks.tick('T', [other, LINK], http=wiki, settle=0))
        self.assertEqual(len(wiki.edits), 2)  # the first "succeeded" and didn't stay: made again
        self.assertEqual(wiki.text.count('done: true'), 2)
        self.assertIn(other, wiki.edits[0]['summary'])
        self.assertIn(LINK, wiki.edits[0]['summary'])

    def test_merged_seen_ticks_it_once_a_day(self):
        Mood.objects.create(slug='magenta-interface', title='magenta interface')
        page = f'<todo>\n- task: Merge it\n  link: {LINK}\n</todo>\n'
        ticks = []
        with mock.patch.object(todo, '_raw', return_value=page), mock.patch.object(todo, '_merged', return_value=True), \
                mock.patch.object(todo, '_in_background', lambda fn, *args: ticks.append(args)):
            todo.for_mood(Mood.objects.get())
            todo.forget(Mood.objects.get())
            todo.for_mood(Mood.objects.get())
        title = 'Cryptograss:Moods/magenta-interface/todo'
        self.assertEqual(ticks, [(title,)])  # one edit's worth, for that page
        self.assertEqual(todo_ticks._waiting.pop(title), {LINK})

    def test_not_set_up_not_ticked(self):
        with override_settings(PICKIPEDIA_TODO_BOT_USER=''):
            ticks = []
            with mock.patch.object(todo, '_in_background', lambda fn, *args: ticks.append(args)):
                todo_ticks.tick_later('T', LINK)
            self.assertEqual(ticks, [])
