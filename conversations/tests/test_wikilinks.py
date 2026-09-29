"""PickiPedia reaches Motions through their [[wikilinks]] without storing them."""

import uuid

from django.test import TestCase

from conversations.models import ConversationParticipant, Message, Motion, ThinkingEntity
from conversations.services.motion_view import wiki_title, wikilinks_in


class WikilinksInTest(TestCase):

    def test_titles_are_normalised_like_mediawiki(self):
        self.assertEqual(wiki_title('old-time_music'), 'Old-time music')
        self.assertEqual(wiki_title('Bill Monroe#Early life'), 'Bill Monroe')
        self.assertEqual(wiki_title('  Cryptograss:Magenta  26 Million '), 'Cryptograss:Magenta 26 Million')

    def test_only_the_first_letter_is_case_insensitive(self):
        # As on the wiki: [[bill monroe]] is a different page from [[Bill Monroe]].
        self.assertEqual(wiki_title('bill monroe'), 'Bill monroe')

    def test_ordered_deduplicated_and_labels_ignored(self):
        text = 'See [[Bill Monroe|Monroe]], then [[bill_Monroe]] and [[Blue Grass Boys]].'
        self.assertEqual(wikilinks_in(text), ['Bill Monroe', 'Blue Grass Boys'])

    def test_code_is_literal(self):
        self.assertEqual(wikilinks_in('Write `[[Page]]` like this:\n```\n[[Other]]\n```'), [])


class WikilinksEndpointTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        tool = ConversationParticipant.objects.create(name='tool-result')
        cls.m26 = Motion.objects.create(slug='m26')
        cls.jams = Motion.objects.create(slug='jams')

        def say(sender, motion, content):
            return Message.objects.create(id=uuid.uuid4(), sender=sender, motion=motion, content=content)

        cls.first = say(cls.justin, cls.m26, 'Is [[Bill Monroe]] on the page yet?')
        say(cls.magent, cls.jams, [{'type': 'text', 'text': 'Added to [[bill_Monroe|his page]] and [[Jam:Friday]].'}])
        say(tool, cls.m26, '[[Bill Monroe]] appears in a tool result')       # machinery
        say(cls.magent, cls.m26, '<command-name>[[Bill Monroe]]</command-name>')  # wrapper
        Message.objects.create(id=uuid.uuid4(), sender=cls.justin, content='[[Bill Monroe]] before Motions')

    def get(self, **params):
        return self.client.get('/api/wikilinks/', params).json()['links']

    def test_backlinks_for_one_page_across_motions(self):
        links = self.get(page='bill_Monroe')
        self.assertEqual(sorted(l['motion'] for l in links), ['jams', 'm26'])
        self.assertEqual({l['page'] for l in links}, {'Bill Monroe'})

    def test_all_links_newest_first(self):
        links = self.get()
        self.assertEqual([(l['motion'], l['page']) for l in links],
                         [('jams', 'Bill Monroe'), ('jams', 'Jam:Friday'), ('m26', 'Bill Monroe')])

    def test_since_is_incremental(self):
        links = self.get(since=self.first.created_at.isoformat())
        self.assertEqual({l['motion'] for l in links}, {'jams'})
