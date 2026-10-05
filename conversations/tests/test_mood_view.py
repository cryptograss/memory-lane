"""Tests for the read-only Mood view: rendering, filtering, and the API."""

import json
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from conversations.models import ConversationParticipant, Message, Mood, ThinkingEntity
from conversations.services.mood_view import prose, render_html, turns


class RenderHtmlTest(TestCase):

    def test_escapes_markup_before_anything_else(self):
        out = render_html('<script>alert(1)</script> & done')
        self.assertNotIn('<script>', out)
        self.assertIn('&lt;script&gt;', out)
        self.assertIn('&amp; done', out)

    def test_quotes_cannot_break_out_of_an_attribute(self):
        # Escaping without quote=True let a " in a link target close href.
        for attack in ('[[x"onmouseover="alert(1)|hover]]',
                       'https://x.test/"onmouseover="alert(1)',
                       '[hover](https://x.test/"onmouseover="alert(1))',
                       "[[x'onmouseover='alert(1)]]"):
            with self.subTest(attack=attack):
                out = render_html(attack)
                self.assertNotIn('"onmouseover', out)
                self.assertNotIn("'onmouseover", out)

    def test_no_pass_can_reach_into_markup_another_made(self):
        # Each linker ran over the HTML the previous ones produced, so a URL
        # inside a wikilink's href, or a mention inside a URL, rewrote the
        # tag and let text out of the attribute.
        attacks = [
            '[[https://x.test/a/onmouseover=alert(1)//|label]]',
            '[[https://x.test/" autofocus onfocus=alert(1)]]',
            '[[https://x.test/x/onclick=alert(1)/]] tail',
            'https://x.test/?who=@magent&x=1',
            '[a @magent b](https://x.test/@magent?q=[[Page]])',
            '[[Page|https://x.test/y]] and [y](https://x.test/[[z]])',
            '**https://x.test/** *@magent* `code` [[A|**b**]]',
            'line one\nhttps://x.test/second-line',
            'x\x02 0\x02 y \x01 0\x01 \x00 0\x00 https://x.test/',
        ]
        for attack in attacks:
            with self.subTest(attack=attack):
                self.assertSafe(render_html(attack, mentionable={'magent'}))

    def test_combinations_of_markup_stay_well_formed(self):
        import itertools
        pieces = ['[[', ']]', '|', '[', '](', ')', 'https://x.test/', '@magent', '**', '*', '`', '![', '/moods/media/' + 'a' * 64 + '.png',
                  '"', "'", '<', '>', '/', '=', 'onclick=alert(1)', ' ', '\n']
        for combo in itertools.product(pieces, repeat=4):
            text = ''.join(combo)
            self.assertSafe(render_html(text, mentionable={'magent'}), text)

    def assertSafe(self, out, source=''):
        """Only the tags and attributes the renderer makes; hrefs only http(s)."""
        from html.parser import HTMLParser
        allowed = {'p': set(), 'br': set(), 'strong': set(), 'em': set(), 'code': set(), 'pre': set(),
                   'ul': set(), 'ol': set(), 'li': set(), 'h4': set(), 'table': set(), 'thead': set(),
                   'tbody': set(), 'tr': set(), 'th': set(), 'td': set(),
                   'a': {'href', 'class', 'target', 'rel', 'data-yarn'}, 'span': {'class', 'data-who'},
                   'img': {'src', 'alt', 'loading'}}
        fixed = {'target': '_blank', 'rel': 'noopener'}  # the renderer's own, never a post's
        case = self

        class Check(HTMLParser):
            def handle_starttag(self, tag, attrs):
                case.assertIn(tag, allowed, (source, out))
                for name, value in attrs:
                    case.assertIn(name, allowed[tag], (source, out))
                    if name in fixed:
                        case.assertEqual(value, fixed[name], (source, out))
                    if name == 'data-yarn':  # the page builds a player from it
                        case.assertRegex(value, r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', (source, out))
                    if name in ('href', 'src'):  # decoded: entities are fine
                        case.assertRegex(value, r'^(https?://|/moods/media/[0-9a-f]{64}\.(png|jpg|gif|webp)$)',
                                         (source, out))
        Check().feed(out)

    def test_a_url_at_the_start_of_a_line_links(self):
        self.assertIn('<a href="https://x.test/b" target="_blank" rel="noopener">', render_html('a\nhttps://x.test/b'))

    def test_a_quoted_url_links_without_the_quote(self):
        out = render_html('see "https://pickipedia.xyz/wiki/Tony_Rice" there')
        self.assertIn('href="https://pickipedia.xyz/wiki/Tony_Rice"', out)

    def test_inline_markdown(self):
        out = render_html('say **bold** and *soft* and `code`')
        self.assertIn('<strong>bold</strong>', out)
        self.assertIn('<em>soft</em>', out)
        self.assertIn('<code>code</code>', out)

    @override_settings(PICKIPEDIA_URL='https://pickipedia.xyz')
    def test_wikilinks_resolve_to_pickipedia(self):
        out = render_html('see [[Tony Rice]] and [[Kuba Hejhal|Kuba]]')
        self.assertIn('href="https://pickipedia.xyz/wiki/Tony_Rice"', out)
        self.assertIn('>Tony Rice</a>', out)
        self.assertIn('href="https://pickipedia.xyz/wiki/Kuba_Hejhal"', out)
        self.assertIn('>Kuba</a>', out)

    def test_lists_and_paragraphs(self):
        out = render_html('intro\n\n- one\n- two\n\n1. first\n2. second\n\nouter')
        self.assertIn('<p>intro</p>', out)
        self.assertIn('<ul><li>one</li><li>two</li></ul>', out)
        self.assertIn('<ol><li>first</li><li>second</li></ol>', out)
        self.assertIn('<p>outer</p>', out)

    def test_fenced_code_is_left_alone(self):
        out = render_html('before\n\n```\n**not bold** [[not a link]]\n```\n\nafter')
        self.assertIn('<pre><code>**not bold** [[not a link]]</code></pre>', out)
        self.assertNotIn('<strong>', out)
        self.assertNotIn('wikilink', out)

    def test_mentions_render_only_known_names(self):
        out = render_html('hey @Justin and @nobody, mail a@b.com, see https://x.y/@z. cc @magent.',
                          mentionable={'justin', 'magent'})
        self.assertIn('<span class="mention" data-who="justin">@Justin</span>', out)
        self.assertIn('<span class="mention" data-who="magent">@magent</span>.', out)
        self.assertIn('@nobody', out)
        self.assertNotIn('data-who="nobody"', out)
        self.assertIn('a@b.com', out)
        self.assertNotIn('data-who="b', out)
        self.assertNotIn('data-who="z"', out)

    def test_mentions_without_mentionable_are_plain(self):
        self.assertNotIn('mention', render_html('hi @justin'))

    def test_mentions_inside_code_are_literal(self):
        from conversations.services.mood_view import mentions_in
        text = 'run `@justin` and ```\n@justin\n```'
        self.assertNotIn('class="mention"', render_html(text, mentionable={'justin'}))
        # The count must agree with the rendering.
        self.assertEqual(mentions_in(text, {'justin'}), [])
        self.assertEqual(mentions_in(text + ' but @justin here', {'justin'}), ['justin'])

    def test_mentions_in(self):
        from conversations.services.mood_view import mentions_in
        self.assertEqual(mentions_in('@Magent @justin @magent @nobody.', {'justin', 'magent'}),
                         ['magent', 'justin'])
        self.assertEqual(mentions_in('nothing here', {'justin'}), [])

    def test_markdown_links(self):
        out = render_html('see [the PR](https://github.com/x/y/pull/10), then.')
        self.assertIn('<a href="https://github.com/x/y/pull/10" target="_blank" rel="noopener">the PR</a>, then.', out)

    def test_bare_url_stops_before_punctuation(self):
        out = render_html('(at https://example.com/a). And https://example.com/b, ok')
        self.assertIn('<a href="https://example.com/a" target="_blank" rel="noopener">https://example.com/a</a>).', out)
        self.assertIn('<a href="https://example.com/b" target="_blank" rel="noopener">https://example.com/b</a>, ok', out)

    def test_inline_code_is_literal(self):
        out = render_html('write `**bold**` and `[[x]]` and `https://a.b` literally')
        self.assertIn('<code>**bold**</code>', out)
        self.assertIn('<code>[[x]]</code>', out)
        self.assertIn('<code>https://a.b</code>', out)
        self.assertNotIn('<strong>', out)
        self.assertNotIn('<a ', out)

    def test_markdown_tables(self):
        out = render_html('intro\n| when | block | where |\n|---|---:|---|\n| mine | **26,078,743** | hunter |\nafter')
        self.assertIn('<table><thead><tr><th>when</th><th>block</th><th>where</th></tr></thead>', out)
        self.assertIn('<tbody><tr><td>mine</td><td><strong>26,078,743</strong></td><td>hunter</td></tr></tbody></table>', out)
        self.assertIn('<p>intro</p>', out)
        self.assertIn('<p>after</p>', out)

    def test_headings_become_small(self):
        self.assertIn('<h4>Where we are</h4>', render_html('## Where we are'))

    def test_bare_urls_link(self):
        out = render_html('at https://example.com/x?a=1&b=2 now')
        self.assertIn('<a href="https://example.com/x?a=1&amp;b=2" target="_blank" rel="noopener">', out)


class ProseAndTurnsTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name="justin", is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name="magent", is_biological_human=False)
        cls.tool = ConversationParticipant.objects.create(name="tool-result")
        cls.system = ConversationParticipant.objects.create(name="system")
        cls.mood = Mood.objects.create(slug="m", title="M")

        script = [
            (cls.justin, "hello there"),
            (cls.magent, {"command": "ls", "description": "list"}),          # tool call
            (cls.tool, "file1\nfile2"),                                         # tool result
            (cls.magent, [{"type": "thinking", "thinking": "hmm"}]),           # thinking
            (cls.magent, [{"type": "text", "text": "reply **one**"}]),
            (cls.system, "System notice"),
            (cls.justin, "<command-name>/clear</command-name>"),               # wrapper
            (cls.magent, [{"text": "untyped block"}]),
        ]
        base = timezone.now() - timedelta(minutes=10)
        cls.ids = []
        for i, (sender, content) in enumerate(script):
            m = Message.objects.create(id=uuid.uuid4(), sender=sender, content=content, mood=cls.mood)
            Message.objects.filter(id=m.id).update(created_at=base + timedelta(seconds=i))
            cls.ids.append(m.id)

    def test_prose_shapes(self):
        self.assertEqual(prose("plain"), "plain")
        self.assertEqual(prose({"command": "ls"}), "")
        self.assertEqual(prose([{"type": "text", "text": "a"}, {"type": "tool_use"}]), "a")
        self.assertEqual(prose([{"type": "thinking", "thinking": "x"}]), "")
        self.assertEqual(prose(None), "")

    def test_turns_keep_only_readable_conversation(self):
        texts = [t for _, t in turns(self.mood)]
        self.assertEqual(texts, ["hello there", "reply **one**", "untyped block"])

    def test_turns_after(self):
        first = Message.objects.get(id=self.ids[0])
        texts = [t for _, t in turns(self.mood, after=first)]
        self.assertEqual(texts, ["reply **one**", "untyped block"])


class MentionsApiTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name="justin", is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name="magent", is_biological_human=False)
        tool = ConversationParticipant.objects.create(name="tool-result")
        a = Mood.objects.create(slug="a")
        b = Mood.objects.create(slug="b")
        base = timezone.now() - timedelta(hours=2)
        rows = [
            (a, cls.magent, "@justin first", 0),
            (b, cls.magent, "no mention", 1),
            (b, cls.justin, "@magent are you there", 2),
            (a, cls.magent, "second, @Justin.", 3),
            (None, cls.magent, "@justin but not in any mood", 4),
            (a, tool, "@justin from a tool result", 5),
        ]
        cls.ids = []
        for mood, sender, text, minute in rows:
            m = Message.objects.create(id=uuid.uuid4(), sender=sender, content=text, mood=mood)
            Message.objects.filter(id=m.id).update(created_at=base + timedelta(minutes=minute))
            cls.ids.append(m.id)

    def test_newest_first_across_moods_from_thinking_entities_only(self):
        data = self.client.get('/api/mentions/justin/').json()
        self.assertEqual(data['name'], 'justin')
        self.assertEqual([(m['mood'], m['turn']['sender']) for m in data['mentions']],
                         [('a', 'magent'), ('a', 'magent')])
        self.assertEqual(data['mentions'][0]['turn']['mentions'], ['justin'])
        self.assertIn('data-who="justin"', data['mentions'][0]['turn']['html'])

    def test_since_and_limit(self):
        from urllib.parse import quote
        first = Message.objects.get(id=self.ids[0])
        # A '+' in an unencoded query string is a space; clients must encode.
        data = self.client.get(f"/api/mentions/justin/?since={quote(first.created_at.isoformat())}").json()
        self.assertEqual(len(data['mentions']), 1)
        data = self.client.get('/api/mentions/JUSTIN/?limit=1').json()
        self.assertEqual(len(data['mentions']), 1)

    def test_bots_are_mentionable(self):
        data = self.client.get('/api/mentions/magent/').json()
        self.assertEqual([(m['mood'], m['turn']['sender']) for m in data['mentions']],
                         [('b', 'justin')])

    def test_unknown_name_is_404_and_read_only(self):
        self.assertEqual(self.client.get('/api/mentions/nobody/').status_code, 404)
        self.assertEqual(self.client.post('/api/mentions/justin/').status_code, 405)

    def test_turns_carry_mentions(self):
        data = self.client.get('/api/moods/b/turns/').json()
        self.assertEqual([t['mentions'] for t in data['turns']], [[], ['magent']])


class MoodApiTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name="justin", is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name="magent", is_biological_human=False)
        cls.old = Mood.objects.create(slug="old-mood", title="Old")
        cls.new = Mood.objects.create(slug="new-mood", title="New", eth_blockheight=26_071_421)
        base = timezone.now() - timedelta(hours=1)
        for i, (mood, sender, text) in enumerate([
            (cls.old, cls.justin, "older"),
            (cls.new, cls.justin, "hi [[Tony Rice]]"),
            (cls.new, cls.magent, [{"type": "text", "text": "hello **you**"}]),
        ]):
            m = Message.objects.create(id=uuid.uuid4(), sender=sender, content=text, mood=mood)
            Message.objects.filter(id=m.id).update(created_at=base + timedelta(minutes=i))

    def test_list_is_most_recent_first(self):
        data = self.client.get('/api/moods/').json()
        self.assertEqual([m['slug'] for m in data['moods']], ['new-mood', 'old-mood'])
        new = data['moods'][0]
        self.assertEqual(new['participants'], ['justin', 'magent'])
        self.assertEqual(new['message_count'], 2)
        self.assertEqual(new['eth_blockheight'], 26_071_421)

    def test_turns_render_and_attribute(self):
        data = self.client.get('/api/moods/new-mood/turns/').json()
        self.assertEqual(data['mood']['slug'], 'new-mood')
        senders = [(t['sender'], t['is_human']) for t in data['turns']]
        self.assertEqual(senders, [('justin', True), ('magent', False)])
        self.assertIn('wikilink', data['turns'][0]['html'])
        self.assertIn('<strong>you</strong>', data['turns'][1]['html'])

    def test_turns_after_and_unknown_after_recovers(self):
        first = self.client.get('/api/moods/new-mood/turns/').json()['turns']
        since = self.client.get(f"/api/moods/new-mood/turns/?after={first[0]['id']}").json()['turns']
        self.assertEqual([t['sender'] for t in since], ['magent'])
        stale = self.client.get(f"/api/moods/new-mood/turns/?after={uuid.uuid4()}").json()['turns']
        self.assertEqual(len(stale), 2)

    def test_unknown_mood_is_404(self):
        self.assertEqual(self.client.get('/api/moods/nope/turns/').status_code, 404)
        self.assertEqual(self.client.get('/moods/nope/').status_code, 404)

    def test_page_renders(self):
        r = self.client.get('/moods/new-mood/')
        self.assertEqual(r.status_code, 200)
        # escapejs renders the hyphen as -; the browser decodes it.
        self.assertContains(r, 'const initialSlug = "new\\u002Dmood"')
        self.assertEqual(self.client.get('/moods/').status_code, 200)

    def test_read_only(self):
        self.assertEqual(self.client.post('/api/moods/').status_code, 405)
        self.assertEqual(self.client.post('/api/moods/new-mood/turns/').status_code, 405)


class YarnClipTest(TestCase):
    """A pasted Yarn clip becomes a card the page plays in place; nothing else does."""
    CLIP = 'ffb40a1a-a936-49ee-962a-ef53e0cb7237'

    def test_a_clip_page_is_a_clip(self):
        from conversations.services.mood_view import yarn_clip
        for host in ('www.yarn.co', 'yarn.co', 'getyarn.io', 'www.getyarn.io'):
            self.assertEqual(yarn_clip(f'https://{host}/yarn-clip/{self.CLIP}'), self.CLIP, host)
        for url in (f'http://www.yarn.co/yarn-clip/{self.CLIP}', f'https://yarn.co.evil.example/yarn-clip/{self.CLIP}',
                    f'https://www.yarn.co/yarn-clip/{self.CLIP}/embed', 'https://www.yarn.co/yarn-clip/not-a-uuid',
                    f'https://www.yarn.co/movie/{self.CLIP}'):
            self.assertIsNone(yarn_clip(url), url)

    def test_shown_as_a_card_that_still_links_to_yarn(self):
        out = render_html(f'Ha! https://www.yarn.co/yarn-clip/{self.CLIP}.')
        self.assertIn(f'<a class="yarn" href="https://www.yarn.co/yarn-clip/{self.CLIP}" data-yarn="{self.CLIP}" '
                      f'target="_blank" rel="noopener">▶ Yarn clip</a>.', out)
        self.assertNotIn('<iframe', out)  # the page makes the player, and only when asked

    def test_a_labelled_link_stays_a_link_and_code_stays_code(self):
        self.assertNotIn('data-yarn', render_html(f'[that scene](https://www.yarn.co/yarn-clip/{self.CLIP})'))
        self.assertNotIn('data-yarn', render_html(f'`https://www.yarn.co/yarn-clip/{self.CLIP}`'))

    def test_nothing_can_ride_along_into_the_card(self):
        for attack in (f'https://www.yarn.co/yarn-clip/{self.CLIP}"onmouseover="alert(1)',
                       f'https://www.yarn.co/yarn-clip/{self.CLIP}?x=" data-yarn="javascript:alert(1)'):
            RenderHtmlTest.assertSafe(self, render_html(attack), attack)
