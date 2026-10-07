"""'#': Moods and messages linked by name, as cards; and who linked (or replied) to what."""

import json
import uuid

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Message, Mood, MoodAlias, ThinkingEntity, ToolUse
from conversations.services import links, mood_auth
from conversations.services.mood_view import render_html


class LinksTest(TestCase):

    def setUp(self):
        cache.clear()
        links._memo.update(at=0.0, index=None)
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.general = Mood.objects.create(slug='general', title='general')
        self.interface = Mood.objects.create(slug='magenta-interface', title='magenta-interface')
        MoodAlias.objects.create(slug='magenta-26-million', mood=self.interface)
        self.capo = self.say(self.justin, self.general, 'Who has the capo tonight? Bring two.')

    def say(self, who, mood, text):
        content = text if who.is_biological_human else [{'type': 'text', 'text': text}]
        return Message.objects.create(id=uuid.uuid4(), sender=who, mood=mood, content=content, timestamp=1,
                                      stop_reason=None if who.is_biological_human else 'end_turn')

    def test_a_moods_name_is_a_link_to_it_by_any_name_it_has_had(self):
        out = render_html('see #general and #magenta-26-million')
        self.assertIn('<a class="moodlink" href="/moods/general/" data-mood="general">#general</a>', out)
        self.assertIn('<a class="moodlink" href="/moods/magenta-interface/" data-mood="magenta-interface">'
                      '#magenta-26-million</a>', out)

    def test_what_isnt_a_mood_or_a_message_stays_as_written(self):
        for text in ('PR #96 and #fff', 'in C# today', "it's", '`#general` in code', '# A heading',
                     'https://x.test/page#general', f'#m-{uuid.uuid4()}'):
            with self.subTest(text=text):
                self.assertNotIn('moodlink', render_html(text))
                self.assertNotIn('msglink', render_html(text))

    def test_a_message_by_id_short_id_or_permalink_is_a_card(self):
        card = (f'<a class="msglink" href="/moods/general/#m-{self.capo.id}" data-mood="general" '
                f'data-reveal="{self.capo.id}">↗ <strong>justin</strong> in #general: “Who has the capo tonight? Bring two.”</a>')
        for text in (f'#m-{self.capo.id}', f'#m-{str(self.capo.id)[:8]}',
                     f'https://magenta.cryptograss.live/moods/general/#m-{self.capo.id}'):
            with self.subTest(text=text):
                self.assertIn(card, render_html(f'see {text}.'))

    def test_who_linked_or_replied_is_kept_for_what_they_linked_to(self):
        linked = self.say(self.magent, self.interface, f'As Justin asked (#m-{str(self.capo.id)[:8]}), two capos.')
        reply = self.say(self.justin, self.general, f'↩ #m-{self.capo.id}\nI do!')
        self.say(self.justin, self.general, f'myself: #m-{self.capo.id}')  # to itself? no: a different message; kept
        ToolUse.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.general, timestamp=1, tool_name='Bash',
                               tool_id='t', content={'command': f'curl /moods/general/#m-{self.capo.id}'})  # not said
        froms = links.linked_from(self.capo.id, links.index())
        self.assertEqual([(f['id'], f['mood'], f['reply']) for f in froms][:2],
                         [(str(linked.id), 'magenta-interface', False), (str(reply.id), 'general', True)])
        self.assertEqual(len(froms), 3)
        turns = Client().get('/api/moods/general/turns/').json()['turns']
        first = next(t for t in turns if t['id'] == str(self.capo.id))
        self.assertEqual(len(first['linked_from']), 3)

    def test_the_index_keeps_up_with_whats_new(self):
        self.assertEqual(links.linked_from(self.capo.id, links.index()), [])
        cache.set('links:fresh', 0, None)  # due a look at what's new
        later = self.say(self.justin, self.interface, f'about #m-{self.capo.id}')
        self.assertEqual([f['id'] for f in links.linked_from(self.capo.id, links.index())], [str(later.id)])

    def test_links_and_cards_are_only_the_markup_the_renderer_makes(self):
        from conversations.tests.test_mood_view import RenderHtmlTest
        quote = self.say(self.justin, self.general, 'say "<script>alert(1)</script>" & **bold** #general')
        for text in (f'#m-{quote.id} and #general', f'**#general** `#m-{quote.id}` [#general](https://x.test/)',
                     f'https://evil.test/moods/general/#m-{quote.id}" onmouseover="x', f'[[#general]] #m-{quote.id}#general'):
            with self.subTest(text=text):
                RenderHtmlTest.assertSafe(self, render_html(text, mentionable={'justin'}), text)

    def test_a_cards_words_are_plain(self):
        from conversations.services.mood_view import snippet_of
        self.assertEqual(snippet_of('🎙 [voice memo · 0:27](/moods/media/ab.webm)\n\nHi, **everybody**.'),
                         '🎙 voice memo · 0:27 Hi, everybody.')
        self.assertEqual(snippet_of('![shot](/moods/media/a.png) look\n```py\nx = 1\n```\nthere'), '🖼 look there')
