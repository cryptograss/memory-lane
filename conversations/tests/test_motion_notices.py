"""Notices (mentions, answers), recent changes, search, and the rules page."""

import time
import uuid

from django.test import TestCase

from conversations.models import ConversationParticipant, Message, Motion, ThinkingEntity, ToolUse
from conversations.services import settings as knobs


class NoticesTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.m26 = Motion.objects.create(slug='m26', title='Magenta 26 Million')
        cls.dk = Motion.objects.create(slug='delivery-kid', title='delivery-kid')

    def add(self, sender, content, motion=None, seconds=0, model=Message, **fields):
        return model.objects.create(id=uuid.uuid4(), sender=sender, motion=motion or self.m26, content=content,
                                    timestamp=int((time.time() + seconds) * 1000), **fields)

    def notices(self, name='justin'):
        return self.client.get(f'/api/notices/{name}/').json()['notices']

    def test_an_answer_is_the_agents_finished_turn_after_your_words(self):
        self.add(self.justin, '@magent what time is soundcheck?', seconds=1, source_file='motion-web')
        self.add(self.magent, [{'type': 'text', 'text': 'Checking the schedule:'}], seconds=2, stop_reason='tool_use')
        self.add(self.magent, [{'type': 'text', 'text': 'Soundcheck is at 5.'}], seconds=3, stop_reason='end_turn')
        found = self.notices()
        self.assertEqual([(n['kind'], n['turn']['text']) for n in found], [('answer', 'Soundcheck is at 5.')])
        self.assertEqual(self.notices('skyler'), [])  # skyler didn't ask

    def test_the_last_person_to_speak_is_the_one_answered(self):
        self.add(self.justin, 'first', seconds=1)
        self.add(self.skyler, '@magent and mine?', seconds=2)
        self.add(self.magent, [{'type': 'text', 'text': 'Yours, Sky.'}], seconds=3, stop_reason='end_turn')
        self.assertEqual(self.notices('justin'), [])
        self.assertEqual([n['kind'] for n in self.notices('skyler')], ['answer'])

    def test_a_mention_by_someone_else_but_not_your_own_or_a_silence(self):
        self.add(self.skyler, '@justin bring the capo', motion=self.dk, seconds=1)
        self.add(self.justin, '@justin note to self', seconds=2)
        self.add(self.magent, [{'type': 'text', 'text': '<silent>not for me</silent>'}], seconds=3, stop_reason='end_turn')
        found = self.notices()
        self.assertEqual([(n['kind'], n['motion']) for n in found], [('mention', 'delivery-kid')])

    def test_recent_changes_mix_what_was_said_renames_and_settings(self):
        self.add(self.justin, 'hello there', seconds=1)
        self.add(self.magent, [{'type': 'text', 'text': 'Working on it:'}], seconds=2, stop_reason='tool_use')
        self.add(self.magent, [{'type': 'text', 'text': 'Done.'}], seconds=3, stop_reason='end_turn')
        knobs.change('mention_effort', 'max', motion=self.m26, agent=self.magent, by=self.justin)
        events = self.client.get('/api/motions/recent/').json()['events']
        kinds = [e['kind'] for e in events]
        self.assertIn('said', kinds)
        self.assertIn('answered', kinds)
        self.assertIn('set', kinds)
        self.assertNotIn('Working on it:', [e.get('text') for e in events])  # progress lines are left out
        self.assertEqual(next(e for e in events if e['kind'] == 'said')['title'], 'Magenta 26 Million')

    def test_search_finds_what_was_said_here_or_anywhere(self):
        self.add(self.justin, 'Who has the CAPO tonight?', seconds=1)
        self.add(self.skyler, 'the capo is in the van', motion=self.dk, seconds=2)
        self.add(self.magent, {'command': 'grep capo notes.txt'}, model=ToolUse, tool_name='Bash', tool_id='t1', seconds=3)
        everywhere = self.client.get('/api/search/?q=capo').json()['hits']
        self.assertEqual(sorted(h['motion'] for h in everywhere), ['delivery-kid', 'm26'])  # no tool calls
        here = self.client.get('/api/search/?q=capo&motion=m26').json()['hits']
        self.assertEqual([h['text'] for h in here], ['Who has the CAPO tonight?'])
        self.assertEqual(self.client.get('/api/search/?q=c').status_code, 400)

    def test_the_rules_page_shows_what_each_wake_says_and_this_moods_rules(self):
        knobs.change('rules', 'Keep it short after midnight.', motion=self.m26, agent=self.magent, by=self.justin)
        page = self.client.get('/motions/m26/rules/').content.decode()
        self.assertIn('Keep it short after midnight.', page)
        for title in ('full tools', 'look, not touch', 'nobody asks it', 'quiet a long while', 'The screen'):
            self.assertIn(title, page)
        self.assertIn('&lt;silent&gt;a few words on why&lt;/silent&gt;', page)  # the wake's own words, escaped


class WakeFramesTest(TestCase):
    """The rules page and the poller say the same thing: one source."""

    def test_the_frames_are_built_from_what_the_poller_sends(self):
        from poller.motion_poller import CONSIDER_ASK, rules_block, wake_footer, wake_frames
        frames = {f['kind']: f['text'] for f in wake_frames('m26', rules='Be brief.')}
        self.assertTrue(frames['mention-full'].endswith('\n'.join(wake_footer(full=True))))
        self.assertTrue(frames['mention-look'].endswith('\n'.join(wake_footer(full=False))))
        self.assertIn(CONSIDER_ASK, frames['consider'])
        self.assertIn('\n'.join(rules_block('Be brief.')), frames['quiet'])
