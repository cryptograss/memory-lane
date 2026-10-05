"""PickiPedia reaches Moods through their [[wikilinks]] without storing them."""

import uuid

from django.test import TestCase

from conversations.models import ConversationParticipant, Message, Mood, ThinkingEntity
from conversations.services.mood_view import pickipedia_page, render_html, wiki_title, wikilinks_in


class WikilinksInTest(TestCase):

    def test_titles_are_normalised_like_mediawiki(self):
        self.assertEqual(wiki_title('old-time_music'), 'Old-time music')
        self.assertEqual(wiki_title('Bill Monroe#Early life'), 'Bill Monroe')
        self.assertEqual(wiki_title('  Cryptograss:Magenta  26 Million '), 'Cryptograss:Magenta 26 Million')

    def test_only_the_first_letter_is_case_insensitive(self):
        # As on the wiki: [[bill monroe]] is a different page from [[Bill Monroe]].
        self.assertEqual(wiki_title('bill monroe'), 'Bill monroe')

    def test_a_leading_colon_links_rather_than_categorises(self):
        self.assertEqual(wiki_title(':Category:Jams'), 'Category:Jams')

    def test_ordered_deduplicated_and_labels_ignored(self):
        text = 'See [[Bill Monroe|Monroe]], then [[bill_Monroe]] and [[Blue Grass Boys]].'
        self.assertEqual(wikilinks_in(text), ['Bill Monroe', 'Blue Grass Boys'])

    def test_code_is_literal(self):
        self.assertEqual(wikilinks_in('Write `[[Page]]` like this:\n```\n[[Other]]\n```'), [])


class PageAddressTest(TestCase):
    """A PickiPedia page's full address is a wikilink written longhand."""

    def test_an_article_address_is_its_page(self):
        self.assertEqual(pickipedia_page('https://pickipedia.xyz/wiki/Tony_Rice'), ('Tony Rice', ''))
        self.assertEqual(pickipedia_page('https://pickipedia.xyz/wiki/Cryptograss:Magenta_26_Million#Speaking_in_a_Mood'),
                         ('Cryptograss:Magenta 26 Million', 'Speaking in a Mood'))
        self.assertEqual(pickipedia_page('https://www.pickipedia.xyz/wiki/B%C3%A9la_Fleck'), ('Béla Fleck', ''))
        self.assertEqual(pickipedia_page('https://pickipedia.cryptograss.live/wiki/Bill_Monroe'), ('Bill Monroe', ''))

    def test_anything_else_is_just_an_address(self):
        for url in ('https://pickipedia.xyz/index.php?title=Tony_Rice&action=edit',
                    'https://pickipedia.xyz/wiki/Tony_Rice?action=history',
                    'https://pickipedia.xyz/', 'https://en.wikipedia.org/wiki/Tony_Rice',
                    'https://pickipedia.xyz.evil.example/wiki/Tony_Rice'):
            self.assertIsNone(pickipedia_page(url), url)

    def test_shown_as_a_wikilink_titled_by_its_page(self):
        html = render_html('Have you heard https://pickipedia.xyz/wiki/Tony_Rice_(guitarist)? And '
                           'https://pickipedia.xyz/wiki/Cryptograss:Magenta_26_Million#Speaking_in_a_Mood.')
        self.assertIn('<a class="wikilink" href="https://pickipedia.xyz/wiki/Tony_Rice_(guitarist)" target="_blank" rel="noopener">'
                      'Tony Rice (guitarist)</a>?', html)
        self.assertIn('>Cryptograss:Magenta 26 Million § Speaking in a Mood</a>.', html)

    def test_a_labelled_link_keeps_its_label_and_others_stay_addresses(self):
        html = render_html('[the etiquette](https://pickipedia.xyz/wiki/Cryptograss:Magenta_26_Million) and '
                           'https://pickipedia.xyz/index.php?title=Tony_Rice&action=edit')
        self.assertIn('>the etiquette</a>', html)
        self.assertIn('>https://pickipedia.xyz/index.php?title=Tony_Rice&amp;action=edit</a>', html)

    def test_a_title_can_never_become_markup(self):
        html = render_html('https://pickipedia.xyz/wiki/%3Cscript%3Ealert(1)%3C/script%3E')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_addresses_count_as_links_in_order(self):
        text = ('First https://pickipedia.xyz/wiki/Bill_Monroe, then [[Blue Grass Boys]], then '
                '[a label](https://pickipedia.xyz/wiki/Tony_Rice) and [[Bill Monroe]] again.')
        self.assertEqual(wikilinks_in(text), ['Bill Monroe', 'Blue Grass Boys', 'Tony Rice'])
        self.assertEqual(wikilinks_in('`https://pickipedia.xyz/wiki/Example`'), [])


class WikilinksEndpointTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        tool = ConversationParticipant.objects.create(name='tool-result')
        cls.m26 = Mood.objects.create(slug='m26')
        cls.jams = Mood.objects.create(slug='jams')

        def say(sender, mood, content):
            return Message.objects.create(id=uuid.uuid4(), sender=sender, mood=mood, content=content)

        cls.first = say(cls.justin, cls.m26, 'Is [[Bill Monroe]] on the page yet?')
        say(cls.magent, cls.jams, [{'type': 'text', 'text': 'Added to [[bill_Monroe|his page]] and [[Jam:Friday]].'}])
        say(tool, cls.m26, '[[Bill Monroe]] appears in a tool result')       # machinery
        say(cls.magent, cls.m26, '<command-name>[[Bill Monroe]]</command-name>')  # wrapper
        Message.objects.create(id=uuid.uuid4(), sender=cls.justin, content='[[Bill Monroe]] before Moods')

    def get(self, **params):
        return self.client.get('/api/wikilinks/', params).json()

    def test_backlinks_for_one_page_across_moods(self):
        links = self.get(page='bill_Monroe')['links']
        self.assertEqual(sorted(l['mood'] for l in links), ['jams', 'm26'])
        self.assertEqual({l['page'] for l in links}, {'Bill Monroe'})

    def test_all_links_oldest_first_and_read_to_the_end(self):
        body = self.get()
        self.assertEqual([(l['mood'], l['page']) for l in body['links']],
                         [('m26', 'Bill Monroe'), ('jams', 'Bill Monroe'), ('jams', 'Jam:Friday')])
        self.assertIsNone(body['next_since'])

    def test_since_is_incremental(self):
        links = self.get(since=self.first.created_at.isoformat())['links']
        self.assertEqual({l['mood'] for l in links}, {'jams'})

    def test_paging_never_splits_a_message_and_loses_nothing(self):
        first = self.get(limit=1)
        self.assertEqual([l['page'] for l in first['links']], ['Bill Monroe'])
        second = self.get(limit=1, since=first['next_since'])
        self.assertEqual([l['page'] for l in second['links']], ['Bill Monroe', 'Jam:Friday'])

    def test_unparseable_since_is_an_error_not_everything(self):
        response = self.client.get('/api/wikilinks/?since=2026-09-29T18:00:00 00:00')
        self.assertEqual(response.status_code, 400)
