"""Context taken from one Mood to another: an agent's ```handoff block, a line in each Mood, the context in its next wake."""

import uuid

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import handoff, voice

BLOCK = ('Taking it over there.\n\n```handoff\nto: #jams-and-events\nabout: the Saturday setlist, as far as we got\n'
         '---\nSettled: Salty Dog first. Open: who sings Wildwood Flower. See #m-0f132ada.\n```\n')


class HandoffTest(TestCase):

    def setUp(self):
        cache.clear()
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.scout = ThinkingEntity.objects.create(name='scout', is_biological_human=False)
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.here = Mood.objects.create(slug='magenta-interface', title='magenta-interface')
        self.there = Mood.objects.create(slug='jams-and-events', title='jams-and-events')

    def said(self, who, text, mood=None):
        content = text if who.is_biological_human else [{'type': 'text', 'text': text}]
        return Message.objects.create(id=uuid.uuid4(), sender=who, mood=mood or self.here, timestamp=1, content=content)

    def events(self, mood):
        return [m.content for m in Message.objects.filter(mood=mood, source_file='handoff')]

    def test_the_block_is_read(self):
        rest, blocks = handoff.split(BLOCK)
        self.assertEqual(rest, 'Taking it over there.')
        self.assertEqual(blocks, [{'to': 'jams-and-events', 'about': 'the Saturday setlist, as far as we got', 'for': '',
                                   'context': 'Settled: Salty Dog first. Open: who sings Wildwood Flower. See #m-0f132ada.'}])

    def test_an_agents_block_leaves_a_line_in_each_mood(self):
        message = self.said(self.magent, BLOCK)  # saved: taken there at once (signals.py)
        [there] = self.events(self.there)
        self.assertEqual((there['type'], there['by'], there['for'], there['from_mood'], there['message']),
                         ('handoff', 'magent', 'magent', 'magenta-interface', str(message.id)))
        self.assertIn('Salty Dog', there['context'])
        [here] = self.events(self.here)
        self.assertEqual((here['type'], here['to_mood'], here['about']),
                         ('handoff-sent', 'jams-and-events', 'the Saturday setlist, as far as we got'))
        self.assertEqual(handoff.from_message(message), [])  # once per message
        self.assertEqual(len(self.events(self.there)), 1)

    def test_for_another_agent_and_for_nobody_known(self):
        self.said(self.magent, BLOCK.replace('about:', 'for: scout\nabout:'))
        self.said(self.magent, BLOCK.replace('about:', 'for: ghost\nabout:'))
        self.assertEqual(sorted(e['for'] for e in self.events(self.there)), ['magent', 'scout'])

    def test_a_person_a_mood_that_isnt_and_this_mood_make_nothing(self):
        self.said(self.justin, BLOCK)
        self.said(self.magent, BLOCK.replace('#jams-and-events', 'nowhere'))
        self.said(self.magent, BLOCK.replace('#jams-and-events', 'magenta-interface'))
        self.assertEqual(Message.objects.filter(source_file='handoff').count(), 0)

    def test_the_block_is_neither_shown_nor_read_aloud_and_the_lines_are(self):
        self.said(self.magent, BLOCK)
        page = Client().get('/api/moods/magenta-interface/turns/').json()
        self.assertNotIn('Salty Dog', str(page['turns']))
        self.assertEqual([e['type'] for e in page['events']], ['handoff-sent'])
        there = Client().get('/api/moods/jams-and-events/turns/').json()['events'][0]
        self.assertEqual((there['about'], there['from_mood']), ('the Saturday setlist, as far as we got', 'magenta-interface'))
        self.assertIn('<p>', there['html'])
        self.assertEqual(voice.script_for(BLOCK)[0], 'Taking it over there.')


class WakeTest(TestCase):

    def test_whats_handed_to_this_agent_since_it_last_spoke(self):
        from poller.mood_poller import handoffs_for
        page = {'turns': [{'sender': 'magent', 'created_at': '2026-10-07T21:00:00+00:00'}],
                'events': [
                    {'type': 'handoff', 'for': 'magent', 'by': 'magent', 'from_mood': 'a', 'about': 'old',
                     'context': 'before it spoke', 'created_at': '2026-10-07T20:00:00+00:00'},
                    {'type': 'handoff', 'for': 'magent', 'by': 'magent', 'from_mood': 'a', 'about': 'the setlist',
                     'context': 'Salty Dog first.', 'created_at': '2026-10-07T21:05:00+00:00'},
                    {'type': 'handoff', 'for': 'scout', 'by': 'magent', 'from_mood': 'a', 'about': 'not yours',
                     'context': '', 'created_at': '2026-10-07T21:06:00+00:00'}]}
        [line] = handoffs_for(page, 'magent')
        self.assertTrue(line.startswith('[magent, from #a, 2026-10-07T21:05Z] the setlist'))
        self.assertIn('Salty Dog first.', line)

    def test_every_wake_says_how(self):
        from poller.mood_poller import wake_frames
        for frame in wake_frames('general'):
            if frame['kind'] != 'screen':
                self.assertIn('```handoff', frame['text'], frame['kind'])
